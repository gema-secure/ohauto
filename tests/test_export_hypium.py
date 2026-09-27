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
        if os.path.exists(cls.out):
            with open(cls.out, encoding='utf-8') as f:
                cls.src = f.read()
        else:
            cls.src = ''

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
        # 定位链必须是大写 ON.：On 是类（只有实例方法，无 static），
        # 写成 On.text(...) 会被 ArkTS 拒绝 —— Property 'text' does not exist on type 'typeof On'
        for kw in ("startAbility", "waitForComponent(ON.text('登录')",
                   "ON.id('username')", "inputText('test_user')",
                   "click()", "screenCap(", "pressBack()"):
            self.assertIn(kw, self.src)

    def test_no_quote_nesting_in_throw(self):
        # ON 链含单引号（ON.text('登录')），外层 Error 必须用双引号，
        # 否则生成的 .ets 是语法错误（真机编译期才炸，成本极高）
        for line in self.src.splitlines():
            s = line.strip()
            if s.startswith('throw new Error(') and "ON.text(" in s \
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
        # 多字段 = AND 语义，hypium 对应 ON 链式
        case = {'name': 't', 'bundle': 'b', 'ability': 'A',
                'steps': [{'tap': {'id': 'btn', 'clickable': True}}]}
        p, out = _tmp_case(case)
        r = _run(p, '--out', out)
        self.assertEqual(r.returncode, 0)
        with open(out, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("ON.id('btn').clickable(true)", src)


class TestTextContainsIsReachable(unittest.TestCase):
    """`text_contains` 必须真的出现在产物里 —— 它曾是一个**永不可达**的分支。

    ★ 背景（2026-09-26 全项目评审发现）：`_on_chain()` 的循环元组写的是
    `('text', 'id', 'type', 'descr')`，**漏了 `text_contains`**。

    后果链很隐蔽：`_ON_FIELDS` 里有这个字段（第 93 行的未知字段检查因此放行），
    但处理它的 `if key == 'text_contains'` 所在的循环**永远走不到它** ——
    既不报错，也不出现在产物里，文本条件被**静默丢弃**。

    这恰好撞上 `_on_chain()` docstring 自己写的那句
    「静默丢弃会让导出件与原用例语义不一致」。
    """

    def setUp(self):
        self._dirs = []

    def tearDown(self):
        import shutil
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _export(self, step_arg):
        case = {'name': 't', 'bundle': 'b', 'ability': 'A',
                'steps': [{'tap': step_arg}]}
        p, out = _tmp_case(case)
        self._dirs.append(os.path.dirname(p))
        r = _run(p, '--out', out)
        return r, out

    def _read_src(self, out):
        with open(out, encoding='utf-8') as f:
            return f.read()

    def test_contains_chain_is_generated(self):
        r, out = self._export({'text_contains': '支付'})
        self.assertEqual(r.returncode, 0,
                         r.stderr.decode('utf-8', 'replace'))
        src = self._read_src(out)
        self.assertIn("ON.text('支付', MatchPattern.CONTAINS)", src,
                      'text_contains 必须导出成带 CONTAINS 的 text 链，'
                      '而不是被丢掉')

    def test_not_degraded_to_exact_match(self):
        """不许退化成精确匹配 —— 包含 ≠ 相等，语义被改了都不知道。"""
        r, out = self._export({'text_contains': '支付'})
        self.assertEqual(r.returncode, 0)
        src = self._read_src(out)
        self.assertNotIn("ON.text('支付')", src,
                         '只导出 ON.text(...) 说明 CONTAINS 丢了')

    def test_matches_generated_header_import(self):
        """用到的 MatchPattern 必须在生成头部已经 import（否则编译不过）。"""
        r, out = self._export({'text_contains': '支付'})
        self.assertEqual(r.returncode, 0)
        src = self._read_src(out)
        self.assertIn('MatchPattern', src.split('\n')[0:12] and src[:800],
                      '生成头部必须 import MatchPattern')

    def test_combined_with_clickable(self):
        r, out = self._export({'text_contains': '支付', 'clickable': True})
        self.assertEqual(r.returncode, 0)
        src = self._read_src(out)
        self.assertIn("ON.text('支付', MatchPattern.CONTAINS).clickable(true)",
                      src)

    def test_empty_condition_is_rejected_not_silently_emitted(self):
        """只剩 index/timeout 时，早前会拼出 `ON.` —— 能生成、却编译不过。

        宁可在这一步失败，也不能让「生成成功」掩盖「语义是空的」。
        """
        r, out = self._export({'index': 1})
        self.assertNotEqual(r.returncode, 0,
                            '没有任何定位字段时必须报错，'
                            '不能产出一份看起来正常的 .ets')
        if os.path.exists(out):
            src = self._read_src(out)
            self.assertNotIn('ON.', src,
                             '产物里不许出现裸的 ON.（非法 .ets）')


if __name__ == '__main__':
    unittest.main()
