"""生成器的两处离线判据：干跑断言语义、压测屏幕尺寸来源。

这两条的共同点是**离线全绿但真机失效**：干跑把「判不了」当成了「写错了」，
屏幕尺寸把「模拟器数字」当成了「真机典型值」。
所以钉子必须钉在判据本身，而不是钉在「跑通了」。

全部离线：假 provider + 假干跑器，不学真调模型、不需要真机。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import Driver                                      # noqa: E402
from ohauto.generator import (DEFAULT_SCREEN, Case, DryRunResult,     # noqa: E402
                              Generator, RejectReason, ScriptedProvider,
                              StressSpec, StressKind, build_stress_case,
                              dry_run, stress_safety_report, swipe_safe_scale)
from ohauto.sim import FakeHdc                                        # noqa: E402

BUNDLE = 'com.demo.app'


def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='true', **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _login_page():
    return {'attributes': {'type': 'Root', 'id': 'loginRoot',
                           'bounds': _b(0, 0, 720, 1280), 'visible': 'true'},
            'children': [
                _node('Text', 'title', '欢迎登录', _b(40, 120, 680, 200), 'false'),
                _node('TextInput', 'username', '', _b(60, 300, 660, 380)),
                _node('Button', 'btn_login', '登录', _b(60, 500, 660, 600)),
            ]}


def _j(obj):
    return json.dumps(obj, ensure_ascii=False)


def _plan_reply():
    return _j({'test_points': [{'kind': '功能', 'title': '正常登录',
                                'precondition': '应用已启动', 'expect': '进入首页'}]})


def _case_reply(steps):
    return _j({'name': '登录流程', 'steps': steps})


class _Provider(ScriptedProvider):
    """按**提示词内容**分派两阶段回复（不靠调用顺序）。"""

    def __init__(self, case_json):
        super().__init__([])
        self.case_json = case_json
        self.repair_reply = None

    def complete(self, prompt):
        if '拆成测试点清单' in prompt:
            return _plan_reply()
        if 'Action DSL 用例' in prompt:
            return self.case_json
        raise AssertionError(f'假 provider 收到意外提示词：{prompt[:60]}')


def _driver(screen=(720, 1280)):
    """模拟设备 + Driver —— 只提供这两条判据需要的部分。"""
    sim = FakeHdc(screen=screen, seed={'login': _login_page()})
    return Driver(bundle=BUNDLE, hdc=sim, artifact_dir=None,
                  verbose=False, sleep_fn=lambda _: None)


# ---------------------------------------------------------------- 干跑消费端

class TestDryRunAssertIsNotACaseDefect(unittest.TestCase):
    """★ 干跑跳过副作用步，所以断言失败**证明不了用例有问题**。

    误判的代价是具体的：`start → tap → assert` 这种正常用例，
    只要调用方传了 driver，断言就必然挂在「还没导航过去」的页面上 ——
    于是「凡带导航后断言的用例全被误杀」，而修复方向还被指成了「改用例」。
    """

    _SUBSTANTIVE = [{'start': True}, {'tap': {'id': 'btn_login'}}]

    def _gen(self, runner):
        return Generator(provider=_Provider(_case_reply(self._SUBSTANTIVE)),
                         bundle=BUNDLE, page=None, max_repair=0,
                         dry_run_runner=runner)

    def _run(self, runner):
        # 必须传 driver —— 干跑只在有 driver 时进行，不传的话判据根本不会跑。
        g = self._gen(runner)
        return g.generate('用户能登录', driver=_driver())

    def test_assert_only_failure_is_graded_needs_device(self):
        def runner(case, driver):
            return DryRunResult(ok=False, executed=1, skipped=2, failures=[
                {'step_index': 3, 'action': 'assert',
                 'error': "AssertionError: 断言不成立", 'kind': 'assert'}])

        out = self._run(runner)
        self.assertEqual(out.reason, RejectReason.ASSERT_NEEDS_DEVICE,
                         f'断言失败不该判成干跑失败：{out.detail}')
        self.assertNotEqual(out.reason, RejectReason.DRYRUN_FAILED)

    def test_reason_has_a_chinese_label(self):
        """原因分类表要认得它 —— 否则报告里会显示成空白。"""
        self.assertTrue(RejectReason.ASSERT_NEEDS_DEVICE.cn)
        self.assertIn('真机', RejectReason.ASSERT_NEEDS_DEVICE.cn)

    def test_note_explains_that_offline_cannot_decide(self):
        def runner(case, driver):
            return DryRunResult(ok=False, executed=1, skipped=2, failures=[
                {'step_index': 3, 'action': 'assert', 'error': 'x',
                 'kind': 'assert'}])

        out = self._run(runner)
        self.assertTrue(any('真机' in n for n in out.case.repair_notes),
                        out.case.repair_notes)

    def test_real_dry_run_failure_is_still_rejected(self):
        """反向钉子：**非断言**的干跑失败照旧判 DRYRUN_FAILED。

        少了这条，「别误杀断言」很容易被写成「干脆别判干跑失败」。
        """
        def runner(case, driver):
            return DryRunResult(ok=False, executed=0, skipped=3, failures=[
                {'step_index': 2, 'action': 'waitFor',
                 'error': 'DriverError: 等不到控件', 'kind': 'error'}])

        out = self._run(runner)
        self.assertEqual(out.reason, RejectReason.DRYRUN_FAILED)

    def test_assert_failure_does_not_mask_a_real_error(self):
        """断言挂了、另一步是真错误 → 仍按真错误判。

        只看「第一条失败的 kind」会被顺序骗过去：断言恰好排第一时，
        后面的超时就被这条豁免吃掉了。
        """
        def runner(case, driver):
            return DryRunResult(ok=False, executed=1, skipped=2, failures=[
                {'step_index': 2, 'action': 'assert', 'error': 'x',
                 'kind': 'assert'},
                {'step_index': 3, 'action': 'waitFor', 'error': '超时',
                 'kind': 'error'}])

        out = self._run(runner)
        self.assertEqual(out.reason, RejectReason.DRYRUN_FAILED)

    def test_real_dry_run_on_navigation_assert_marks_assert(self):
        """端到端：真干跑器也确实把断言失败标成 `kind='assert'`。

        这条守的是「消费端的判据有上游供给」—— 只钉消费端的话，
        上游哪天不标 kind 了，豁免就悄悄失效、又变回全量误杀。
        """
        d = _driver()
        case = Case(name='登录', bundle=BUNDLE, steps=[
            {'start': True},
            {'tap': {'id': 'btn_login'}},
            {'assert': {'text': {'id': 'title', 'equals': '这个文案不存在'}}},
        ])
        res = dry_run(case, d)
        self.assertFalse(res.ok)
        self.assertEqual(res.failures[0]['kind'], 'assert',
                         '断言失败必须带 kind=assert，否则消费端无从区分')
        self.assertEqual(res.skipped, 2, '副作用步一步都不许跑')


# ---------------------------------------------------------------- 屏幕尺寸

class TestScreenFallbackIsTheRealDevice(unittest.TestCase):
    """压测几何全按屏幕边长算，回落的尺寸必须是**真机典型值**。

    回落成模拟器的随手数字，离线校验会照样全绿，而真机上
    「起止点离左右边缘留 150–200px」这条硬约束静默失效 ——
    恰好是最不该失效的那条（失效要误触系统返回手势）。
    """

    def test_default_screen_matches_the_real_device(self):
        self.assertEqual(DEFAULT_SCREEN, (720, 1280))

    def test_build_stress_case_records_where_the_size_came_from(self):
        """点击类压测允许回落默认屏，但出处必须记账（可追溯）。"""
        case = build_stress_case(StressSpec(kind=StressKind.REPEAT_TAP,
                                            rounds=2,
                                            target={'id': 'btn_refresh'}))
        self.assertTrue(any('屏幕尺寸' in n for n in case.notes), case.notes)
        self.assertTrue(any('回落' in n for n in case.notes), case.notes)

    def test_swipe_stress_without_screen_is_rejected(self):
        """滑动压测必须给实测屏 —— 拒绝回落写死值（换设备静默失效的根源）。"""
        with self.assertRaises(ValueError):
            build_stress_case(StressSpec(kind=StressKind.SWIPE_LOOP,
                                         rounds=2,
                                         directions=('left',),
                                         scale=0.6))

    def test_geometry_follows_the_screen_not_a_wider_screen(self):
        """屏幕换掉，起止点必须跟着换 —— 否则「跟屏」只是个装饰。"""
        narrow = build_stress_case(StressSpec(kind=StressKind.SWIPE_LOOP,
                                              rounds=2,
                                              directions=('left',),
                                              scale=0.6),
                                   screen=(720, 1280))
        wide = build_stress_case(StressSpec(kind=StressKind.SWIPE_LOOP,
                                            rounds=2,
                                            directions=('left',),
                                            scale=0.6),
                                 screen=(1080, 2340))
        self.assertNotEqual(narrow.steps, wide.steps,
                            '屏幕尺寸没参与几何计算')

    def test_safety_report_requires_screen_for_swipes(self):
        case = build_stress_case(StressSpec(kind=StressKind.SWIPE_LOOP,
                                            rounds=2,
                                            directions=('left', 'right'),
                                            scale=0.6),
                                 screen=(720, 1280))
        with self.assertRaises(ValueError):
            stress_safety_report(case)      # 含滑动步骤却不给屏 → 拒绝判定
        report = stress_safety_report(case, (720, 1280))
        self.assertTrue(report, '水平滑动应当逐条进报告')
        self.assertTrue(all(r.ok for r in report),
                        [r.to_dict() for r in report if not r.ok])
        self.assertTrue(all(r.distance_px >= r.needed_px for r in report),
                        '按真机典型屏算出来不该越界')

    def test_generator_uses_the_measured_screen_when_available(self):
        """能从设备实测就拿实测 —— 回落只在拿不到时生效。"""
        d = _driver(screen=(720, 1280))
        g = Generator(provider=_Provider(
            _case_reply([{'start': True}, {'tap': {'id': 'btn_login'}}])),
            bundle=BUNDLE, page=None, max_repair=0)
        case = g.generate_stress('连续滑动', rounds=2, driver=d,
                                 directions=('left',))
        self.assertTrue(any('设备实测' in n for n in case.notes), case.notes)
        swipes = [s['swipe'] for s in case.steps if 'swipe' in s]
        self.assertTrue(swipes)
        self.assertAlmostEqual(swipes[0]['scale'], swipe_safe_scale(720),
                               places=3,
                               msg='几何必须按 720 宽算 —— 按 1080 算会放行 0.6')


if __name__ == '__main__':
    unittest.main(verbosity=2)
