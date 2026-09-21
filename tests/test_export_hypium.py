"""hypium 导出器的单元测试 —— 子进程调 CLI 做端到端断言。

不 import tools/（tools 不是包），与 test_emulator_cli.py 同风格：
真实起进程跑 `python tools/export_hypium.py`，对产物文件做内容断言。
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yaml                                            # noqa: E402

EXPORT = os.path.join(ROOT, 'tools', 'export_hypium.py')
CASE_LOGIN = os.path.join(ROOT, 'examples', 'cases', 'login.yaml')
PY = sys.executable


def _run(*args):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    return subprocess.run([PY, EXPORT, *args], capture_output=True,
                          env=env, timeout=60)


def _tmp_case(case):
    d = tempfile.mkdtemp(prefix='ohauto_hypium_case_')
    p = os.path.join(d, 'case.yaml')
    with open(p, 'w', encoding='utf-8') as f:
        yaml.safe_dump(case, f, allow_unicode=True)
    return p, os.path.join(d, 'out.test.ets')


class TestExportLogin(unittest.TestCase):
    """真实用例（login.yaml，13 步含 1 个不支持动作）端到端导出。"""

    @classmethod
    def setUpClass(cls):
        cls.out = os.path.join(tempfile.mkdtemp(prefix='ohauto_hypium_'),
                               'login.test.ets')
        r = _run(CASE_LOGIN, '--out', cls.out)
        cls.rc = r.returncode
        cls.stderr = r.stderr.decode('utf-8', 'replace')
        cls.src = (open(cls.out, encoding='utf-8').read()
                   if os.path.exists(cls.out) else '')

    def test_exit_zero(self):
        self.assertEqual(self.rc, 0, self.stderr)

    def test_generated_file_exists(self):
        self.assertTrue(os.path.exists(self.out), self.stderr)

    def test_uses_new_api_only(self):
        # 设备 API 15：老 API（UiDriver/By）自 9 起废弃，绝不能出现在产物里
        self.assertIn('@kit.TestKit', self.src)
        self.assertNotIn('UiDriver', self.src)
        self.assertNotIn('BY.', self.src)

    def test_driver_create_without_await(self):
        # static create(): Driver —— 无 await（多写 await 会编译失败）
        self.assertIn('const driver = Driver.create();', self.src)

    def test_key_calls_present(self):
        for kw in ("startAbility", "waitForComponent(On.text('登录')",
                   "On.id('username')", "inputText('test_user')",
                   "click()", "screenCap(", "pressBack()"):
            self.assertIn(kw, self.src)

    def test_no_quote_nesting_in_throw(self):
        # On 链含单引号（On.text('登录')），外层 Error 必须用双引号，
        # 否则生成的 .ets 是语法错误（真机编译期才炸，成本极高）
        for line in self.src.splitlines():
            s = line.strip()
            if s.startswith('throw new Error(') and "On.text(" in s \
                    and s.endswith("');"):
                self.fail(f'单引号嵌套: {line}')

    def test_unsupported_step_has_placeholder(self):
        # swipe 无法自动导出 → 占位 + 明确报错，且不阻断后续步骤
        self.assertIn('步骤 10 (swipe) 未导出', self.src)
        self.assertIn('⚠️', self.src)
        self.assertIn('step 11: screenshot', self.src)
        self.assertIn('step 13: assert', self.src)

    def test_waitfor_checks_undefined(self):
        # 官方注释：waitForComponent 超时「or undefined」而非抛错
        # —— 生成代码必须判空，否则后续步骤在 undefined 上炸出难懂的错
        self.assertIn('if (c2 === undefined)', self.src)


class TestUnsupportedAndErrors(unittest.TestCase):
    """不支持的 action 与非法用例 —— 语义必须诚实。"""

    def test_home_rejected_with_advice(self):
        case = {'name': 't', 'bundle': 'b', 'ability': 'A',
                'steps': [{'home': True}]}
        p, out = _tmp_case(case)
        r = _run(p, '--out', out)
        self.assertEqual(r.returncode, 0)            # 不阻断整体导出
        with open(out, encoding='utf-8') as f:
            src = f.read()
        self.assertIn('未导出', src)
        self.assertIn('triggerKey', src)             # 报错里给出替代线索

    def test_missing_bundle_rejected(self):
        case = {'name': 't', 'ability': 'A', 'steps': [{'back': True}]}
        p, out = _tmp_case(case)
        r = _run(p, '--out', out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('bundle', r.stderr.decode('utf-8', 'replace'))

    def test_unknown_locator_field_fails_export(self):
        # 定位字段拼错属于用例级错误：静默丢字段会让导出件语义跑偏，
        # 所以必须在导出期整体失败，而不是少生成一个条件
        case = {'name': 't', 'bundle': 'b', 'ability': 'A',
                'steps': [{'tap': {'foo': 'bar'}}]}
        p, out = _tmp_case(case)
        r = _run(p, '--out', out)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('foo', r.stderr.decode('utf-8', 'replace'))

    def test_multi_field_locator_is_chained(self):
        # 多字段 = AND 语义，hypium 对应 On 链式
        case = {'name': 't', 'bundle': 'b', 'ability': 'A',
                'steps': [{'tap': {'id': 'btn', 'clickable': True}}]}
        p, out = _tmp_case(case)
        r = _run(p, '--out', out)
        self.assertEqual(r.returncode, 0)
        with open(out, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("On.id('btn').clickable(true)", src)


if __name__ == '__main__':
    unittest.main()
