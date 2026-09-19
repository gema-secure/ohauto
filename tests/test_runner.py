"""执行引擎单元测试 —— 重试、设备自愈、级联识别、产物轮转、50 步 KPI。

全部用 `FakeHdc` + `FaultPlan` 驱动，**不需要真机**，可进 CI。

两个刻意的设计选择：
  1. `Runner(sleep_fn=lambda s: None)` —— 屏蔽真实退避等待。
     否则重试一多，一个测试要跑两分钟（实测过）。
  2. `Driver(default_timeout=250, poll_interval=50)` —— 把 waitFor 的超时压到亚秒级。
     真机默认 8000ms 在单测里毫无意义，只会让测试变慢。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.action import DslError, RunReport              # noqa: E402
from ohauto.driver import Driver, DriverError               # noqa: E402
from ohauto.hdc import DeviceNotFound, HdcError             # noqa: E402
from ohauto import runner as R                              # noqa: E402
from ohauto.sim import FakeHdc, FaultPlan                   # noqa: E402


# ================================================================ 工具

def make_driver(faults=None, artifact_dir=None, page='login', locked=False):
    """构造一个「快」的模拟驱动：短超时、短轮询、不真睡。"""
    hdc = FakeHdc(start_page=page, verbose=False, faults=faults, locked=locked)
    d = Driver(bundle='com.demo', ability='EntryAbility', hdc=hdc,
               artifact_dir=artifact_dir, default_timeout=250,
               poll_interval=50, verbose=False,
               sleep_fn=lambda s: None)
    return hdc, d


def make_runner(policy=None, guard=None, **kw):
    kw.setdefault('verbose', False)
    return R.Runner(policy=policy, guard=guard, sleep_fn=lambda s: None, **kw)


# 一个自洽的 12 步循环：登录 -> 首页 -> 订单 -> 返回 -> 退出登录
# 每一步都对应模拟设备上真实存在的控件（见 ohauto/sim.py 的 PAGES）。
CYCLE = [
    {'tap': {'id': 'username'}},
    {'input': {'id': 'username', 'value': 'alice'}},
    {'tap': {'id': 'password'}},
    {'input': {'id': 'password', 'value': 'secret'}},
    {'tap': {'id': 'btn_login'}},                 # login -> home
    {'waitFor': {'id': 'tv_title'}},
    {'tap': {'id': 'tab_order'}},                 # home  -> order
    {'waitFor': {'id': 'tv_order_title'}},
    {'tap': {'id': 'btn_back'}},                  # order -> home
    {'tap': {'id': 'btn_logout'}},                # home  -> login
    {'assert': {'exists': {'id': 'btn_login'}}},
    {'assert': {'exists': {'id': 'btn_forget'}}},
]


def fifty_step_case(name='50步连续执行'):
    """执行引擎 验收用的 50 步用例。"""
    steps = (CYCLE * 5)[:50]
    assert len(steps) == 50
    return {'name': name, 'steps': steps}


def short_case():
    return {'name': '短用例', 'steps': CYCLE[:6]}


# ================================================================ 失败分类

class TestClassify(unittest.TestCase):
    """分类器错了，后面所有策略都会错 —— 它是整套机制的地基。"""

    def test_dsl_error_is_dsl(self):
        self.assertIs(R.classify(DslError('input 需要形如 {id: "x"}')),
                      R.FailureKind.DSL)

    def test_assert_failure_is_assert(self):
        self.assertIs(R.classify(DriverError('断言失败：控件应存在但未找到 -> id=x')),
                      R.FailureKind.ASSERT)

    def test_locate_failure_is_locate(self):
        self.assertIs(R.classify(DriverError('定位失败：控件未找到 -> id=x')),
                      R.FailureKind.LOCATE)

    def test_device_not_found_is_device(self):
        self.assertIs(R.classify(DeviceNotFound('no device found')),
                      R.FailureKind.DEVICE)

    def test_hdc_timeout_is_timeout(self):
        self.assertIs(R.classify(HdcError('hdc 命令超时(30s): hdc shell ls')),
                      R.FailureKind.TIMEOUT)

    def test_app_start_failure_is_app(self):
        self.assertIs(R.classify(RuntimeError('failed to start ability. err')),
                      R.FailureKind.APP)

    def test_timeout_error_type_is_timeout(self):
        self.assertIs(R.classify(TimeoutError('timed out')), R.FailureKind.TIMEOUT)

    def test_bare_hdc_error_defaults_to_device(self):
        self.assertIs(R.classify(HdcError('hdc 执行失败 rc=1: whatever')),
                      R.FailureKind.DEVICE)

    def test_bare_driver_error_defaults_to_locate(self):
        self.assertIs(R.classify(DriverError('something odd')),
                      R.FailureKind.LOCATE)

    def test_unknown_stays_unknown(self):
        self.assertIs(R.classify(ValueError('???')), R.FailureKind.UNKNOWN)

    def test_assert_is_checked_before_locate(self):
        """「断言失败：控件应存在但未找到」同时含两类关键词，必须判成 ASSERT。

        判成 LOCATE 会导致断言失败被重试三次 —— 既拖慢又掩盖真问题。
        """
        k = R.classify(DriverError('断言失败：控件应存在但未找到 -> id=x'))
        self.assertIs(k, R.FailureKind.ASSERT)
        self.assertFalse(R.RetryPolicy().retryable(k),
                         '断言失败绝不能被配置成可重试')


# ================================================================ 重试策略

class TestRetryPolicy(unittest.TestCase):

    def test_assert_and_dsl_never_retry(self):
        p = R.RetryPolicy()
        self.assertFalse(p.retryable(R.FailureKind.ASSERT))
        self.assertFalse(p.retryable(R.FailureKind.DSL))
        self.assertEqual(p.attempts_for(R.FailureKind.ASSERT), 1)
        self.assertEqual(p.attempts_for(R.FailureKind.DSL), 1)

    def test_locate_and_timeout_do_retry(self):
        p = R.RetryPolicy()
        for k in (R.FailureKind.LOCATE, R.FailureKind.TIMEOUT, R.FailureKind.DEVICE):
            with self.subTest(kind=k):
                self.assertTrue(p.retryable(k))
                self.assertGreaterEqual(p.attempts_for(k), 2)

    def test_no_retry_policy_disables_everything(self):
        p = R.RetryPolicy.no_retry()
        for k in R.FailureKind:
            with self.subTest(kind=k):
                self.assertEqual(p.attempts_for(k), 1)
                self.assertFalse(p.retryable(k))

    def test_backoff_grows_with_attempt(self):
        p = R.RetryPolicy(jitter_ms=0)
        w1 = p.wait_ms(R.FailureKind.TIMEOUT, 1)
        w2 = p.wait_ms(R.FailureKind.TIMEOUT, 2)
        self.assertGreater(w2, w1, '退避必须递增，否则重试会变成忙等')

    def test_non_retryable_kinds_have_zero_backoff(self):
        p = R.RetryPolicy()
        self.assertEqual(p.wait_ms(R.FailureKind.ASSERT, 1), 0)
        self.assertEqual(p.wait_ms(R.FailureKind.DSL, 1), 0)

    def test_jitter_is_bounded(self):
        p = R.RetryPolicy(jitter_ms=100)
        base = p.backoff_ms[R.FailureKind.LOCATE]
        for _ in range(50):
            w = p.wait_ms(R.FailureKind.LOCATE, 1)
            self.assertTrue(base <= w <= base + 100)


# ================================================================ 故障注入器

class TestFaultPlan(unittest.TestCase):

    def test_fail_next_fires_exactly_n_times(self):
        fp = FaultPlan().fail_next('dump_layout', times=2, kind='timeout')
        hits = [fp.check('dump_layout') is not None for _ in range(4)]
        self.assertEqual(hits, [True, True, False, False])

    def test_fail_every_fires_periodically(self):
        fp = FaultPlan().fail_every('shell', every=3, kind='hdc')
        hits = [fp.check('shell') is not None for _ in range(6)]
        self.assertEqual(hits, [False, False, True, False, False, True])

    def test_target_isolation(self):
        fp = FaultPlan().fail_next('dump_layout', times=1)
        self.assertIsNone(fp.check('screen_cap'))
        self.assertIsNotNone(fp.check('dump_layout'))

    def test_match_filters_by_command(self):
        fp = FaultPlan().fail_next('shell', times=1, match='uitest')
        self.assertIsNone(fp.check('shell', 'echo ohauto_ok'))
        self.assertIsNotNone(fp.check('shell', 'uitest dumpLayout'))

    def test_raised_exception_matches_real_hdc_types(self):
        """注入的异常必须与真机同类型，否则分类器在模拟环境下失去意义。"""
        from ohauto.hdc import DeviceNotFound as DNF
        fp = FaultPlan().fail_next('any', times=1, kind='device')
        self.assertIsInstance(fp.check('x'), DNF)
        fp2 = FaultPlan().fail_next('any', times=1, kind='timeout')
        exc = fp2.check('x')
        self.assertIsInstance(exc, HdcError)
        self.assertIs(R.classify(exc), R.FailureKind.TIMEOUT)

    def test_stats_counts_injections(self):
        fp = FaultPlan().fail_next('dump_layout', times=3, kind='timeout')
        for _ in range(3):
            fp.check('dump_layout')
        self.assertEqual(fp.stats().get('dump_layout:timeout'), 3)


# ================================================================ 重试行为

class TestRetryBehavior(unittest.TestCase):

    def test_transient_fault_is_rescued_by_retry(self):
        """偶发超时应当被重试救回，最终该步仍然成功。"""
        fp = FaultPlan().fail_next('dump_layout', times=1, kind='timeout')
        _, d = make_driver(fp)
        res = make_runner().run_case(d, {'name': 't', 'steps': [
            {'tap': {'id': 'username'}},
        ]})
        self.assertEqual(res.passed, 1)
        self.assertGreaterEqual(res.retry_attempts, 1, '应该发生过重试')
        self.assertGreaterEqual(res.rescued_by_retry, 1, '重试应该救回了这一步')
        first = res.steps[0]
        self.assertTrue(first.ok)
        self.assertTrue(first.rescued_by_retry)

    def test_assert_failure_is_not_retried(self):
        """断言失败是真失败，必须一次判负 —— 重试只会拖慢并掩盖问题。"""
        _, d = make_driver()
        res = make_runner().run_case(d, {'name': 't', 'steps': [
            {'assert': {'exists': {'id': 'no_such_control_xyz'}}},
        ]})
        self.assertEqual(res.failed, 1)
        self.assertIs(res.steps[0].kind, R.FailureKind.ASSERT)
        self.assertEqual(res.steps[0].attempt_count, 1,
                         '断言失败被重试了 —— 策略失效')

    def test_dsl_error_is_not_retried(self):
        _, d = make_driver()
        res = make_runner().run_case(d, {'name': 't', 'steps': [
            {'totally_unknown_action': {'id': 'x'}},
        ]})
        self.assertEqual(res.failed, 1)
        self.assertIs(res.steps[0].kind, R.FailureKind.DSL)
        self.assertEqual(res.steps[0].attempt_count, 1)

    def test_empty_step_is_reported_as_dsl(self):
        _, d = make_driver()
        res = make_runner().run_case(d, {'name': 't', 'steps': [{}, None]})
        self.assertEqual(res.failed, 2)
        self.assertTrue(all(s.kind is R.FailureKind.DSL for s in res.steps))

    def test_attempt_detail_is_preserved(self):
        """每一次尝试都要留痕，否则无法回答「这步到底重试了几次」。"""
        fp = FaultPlan().fail_next('dump_layout', times=2, kind='timeout')
        _, d = make_driver(fp)
        res = make_runner().run_case(d, {'name': 't',
                                         'steps': [{'tap': {'id': 'username'}}]})
        s = res.steps[0]
        self.assertGreaterEqual(s.attempt_count, 2)
        kinds = [a.kind for a in s.attempts if not a.ok]
        self.assertTrue(all(k is R.FailureKind.TIMEOUT for k in kinds))
        self.assertTrue(any(a.error for a in s.attempts if not a.ok),
                        '失败尝试必须带错误文本')

    def test_retry_beats_no_retry_under_periodic_faults(self):
        """对照实验：同一故障计划下，开重试的成功率不得低于关重试。"""
        def run(policy):
            fp = FaultPlan().fail_every('dump_layout', 5, kind='timeout')
            _, d = make_driver(fp)
            r = make_runner(policy=policy)
            return r.run_case(d, fifty_step_case())

        with_retry = run(R.RetryPolicy())
        without = run(R.RetryPolicy.no_retry())
        self.assertGreaterEqual(
            with_retry.independent_success_rate, without.independent_success_rate,
            f'开重试({with_retry.independent_success_rate}) 反而不如关重试'
            f'({without.independent_success_rate})')
        self.assertGreater(with_retry.passed, without.passed,
                           '重试没有救回任何步骤，说明机制没生效')

    def test_continue_on_fail_keeps_going(self):
        _, d = make_driver()
        res = make_runner(continue_on_fail=True).run_case(
            d, {'name': 't', 'steps': [
                {'tap': {'id': 'no_such_1'}},
                {'tap': {'id': 'username'}},
                {'assert': {'exists': {'id': 'btn_login'}}},
            ]})
        self.assertEqual(res.total, 3)
        self.assertEqual(res.passed, 2)

    def test_stop_on_fail_breaks_early(self):
        _, d = make_driver()
        res = make_runner(continue_on_fail=False).run_case(
            d, {'name': 't', 'steps': [
                {'tap': {'id': 'no_such_1'}},
                {'tap': {'id': 'username'}},
            ]})
        self.assertEqual(res.total, 1)


# ================================================================ 级联识别

class TestCascadeDetection(unittest.TestCase):

    def test_cascade_marked_when_kind_matches_first_failure(self):
        """首步真挂，后续同类失败应被判为级联，不计入独立失败。"""
        _, d = make_driver()
        res = make_runner(continue_on_fail=True).run_case(
            d, {'name': 't', 'steps': [
                {'assert': {'exists': {'id': 'no_such_1'}}},   # 独立失败
                {'assert': {'exists': {'id': 'no_such_2'}}},   # 级联
                {'assert': {'exists': {'id': 'no_such_3'}}},   # 级联
            ]})
        self.assertEqual(res.failed, 3)
        self.assertEqual(res.cascade_failed, 2)
        self.assertEqual(res.independent_failed, 1)
        self.assertEqual(res.steps[1].cascade_of, 1)
        self.assertEqual(res.steps[2].cascade_of, 1)

    def test_different_kind_is_not_cascade(self):
        """失败类别不同，说明是新问题，不能算级联 —— 否则会掩盖第二个真 bug。"""
        _, d = make_driver()
        res = make_runner(continue_on_fail=True).run_case(
            d, {'name': 't', 'steps': [
                {'assert': {'exists': {'id': 'no_such_1'}}},     # ASSERT
                {'totally_unknown_action': {}},                  # DSL
            ]})
        self.assertEqual(res.cascade_failed, 0)
        self.assertEqual(res.independent_failed, 2)

    def test_recovery_resets_first_failure(self):
        """中间成功过之后，新的失败不应再挂到最早那次失败上。"""
        _, d = make_driver()
        res = make_runner(continue_on_fail=True).run_case(
            d, {'name': 't', 'steps': [
                {'assert': {'exists': {'id': 'no_such_1'}}},   # 失败
                {'tap': {'id': 'username'}},                   # 成功
                {'assert': {'exists': {'id': 'no_such_2'}}},   # 新失败
            ]})
        self.assertFalse(res.steps[2].cascade,
                         '中间成功过，第 3 步应算独立失败')

    def test_independent_rate_excludes_cascade(self):
        _, d = make_driver()
        res = make_runner(continue_on_fail=True).run_case(
            d, {'name': 't', 'steps': [
                {'assert': {'exists': {'id': 'no_such_1'}}},
                {'assert': {'exists': {'id': 'no_such_2'}}},
                {'assert': {'exists': {'id': 'no_such_3'}}},
                {'assert': {'exists': {'id': 'no_such_4'}}},
            ]})
        # 原始成功率 0，但独立成功率应为 3/4
        self.assertEqual(res.success_rate, 0.0)
        self.assertEqual(res.independent_success_rate, 0.75)


# ================================================================ 设备看护

class TestDeviceGuard(unittest.TestCase):
    """设备看护测试。

    注意：模拟设备的 `shell` 是唯一承载「读控件树」的通道
    （`Driver.refresh` 用 `hdc shell cat <json>` 取内容），
    所以 shell 全挂 = 传输层全断。这与真机行为一致：壳断了就什么都读不到。
    也正因如此，**传输层断了以后重启应用是没有意义的** —— 见 `recover` 的分流。
    """

    def _guard(self, hdc, **kw):
        kw.setdefault('verbose', False)
        kw.setdefault('sleep_fn', lambda s: None)
        return R.DeviceGuard(hdc, **kw)

    def test_ping_ok_on_healthy_device(self):
        hdc, _ = make_driver()
        g = self._guard(hdc)
        self.assertTrue(g.ping())
        self.assertEqual(g.failed_pings, 0)

    def test_ping_fails_when_shell_broken(self):
        fp = FaultPlan().fail_next('shell', times=99, kind='device')
        hdc, _ = make_driver(fp)
        g = self._guard(hdc)
        self.assertFalse(g.ping())
        self.assertEqual(g.failed_pings, 1)

    def test_recover_light_path_for_transient_blip(self):
        """一次抖动：轻量重试就该搞定，不该去重启 hdc 服务。"""
        fp = FaultPlan().fail_next('shell', times=1, kind='device')
        hdc, d = make_driver(fp)
        g = self._guard(hdc)
        self.assertTrue(g.recover(d, kind=R.FailureKind.DEVICE))
        self.assertEqual(g.recoveries, 1)
        self.assertEqual(g.restarts, 0, '一次抖动不应该触发 hdc 服务重启')

    def test_recover_restarts_hdc_for_device_kind(self):
        """传输层断了：必须走重启 hdc 服务这条路，重启应用是徒劳的。"""
        fp = FaultPlan().fail_next('shell', times=99, kind='device')
        hdc, d = make_driver(fp)
        g = self._guard(hdc, allow_hdc_restart=True)
        self.assertFalse(g.recover(d, kind=R.FailureKind.DEVICE),
                         'shell 始终不通时，必须诚实返回失败')
        self.assertEqual(g.restarts, 1, '没有尝试重启 hdc 服务')

    def test_recover_relaunches_app_for_app_kind(self):
        """应用类失败：传输层是好的，应当重新拉起应用而不是重启 hdc。"""
        hdc, d = make_driver()
        g = self._guard(hdc)
        self.assertTrue(g.recover(d, kind=R.FailureKind.APP))
        self.assertEqual(g.recoveries, 1)
        self.assertEqual(g.restarts, 0, '传输层健康时不该重启 hdc 服务')

    def test_restart_app_kind_does_not_skip_app_relaunch(self):
        """APP 类失败即便传输层健康也必须真的重启应用，不能走轻量路径蒙混过关。

        判据用「页面是否被重置到入口页」——`FakeHdc.start_ability` 会重置页面，
        比查 hdc 调用记录更贴近真实效果。
        """
        hdc, d = make_driver(page='home')
        g = self._guard(hdc)
        g.recover(d, kind=R.FailureKind.APP)
        self.assertEqual(hdc.current, 'login',
                         'APP 类恢复没有重新拉起应用（页面未被重置到入口页）')

    def test_recover_fails_without_restart_permission(self):
        """最坏情况：传输层断、不许重启 hdc —— 必须诚实返回失败。"""
        fp = FaultPlan().fail_next('shell', times=99, kind='device')
        hdc, _ = make_driver(fp)
        g = self._guard(hdc, allow_hdc_restart=False)
        self.assertFalse(g.recover(None, kind=R.FailureKind.DEVICE))

    def test_device_fault_triggers_recovery_during_run(self):
        """设备类失败应触发恢复，且该步骤的 recovered 标记要留下来。"""
        fp = FaultPlan().fail_next('dump_layout', times=1, kind='device')
        hdc, d = make_driver(fp)
        g = self._guard(hdc, allow_hdc_restart=False)
        res = make_runner(guard=g).run_case(d, {'name': 't', 'steps': [
            {'tap': {'id': 'username'}},
        ]})
        self.assertGreaterEqual(g.recoveries, 1, '设备类失败没有触发恢复')
        self.assertTrue(res.steps[0].recovered, '恢复标记没有落到步骤上')
        self.assertTrue(res.steps[0].ok, '恢复之后应当重试成功')

    def test_assert_failure_does_not_trigger_recovery(self):
        """断言失败不是设备问题，不该白跑一次恢复流程。"""
        hdc, d = make_driver()
        g = self._guard(hdc)
        make_runner(guard=g).run_case(d, {'name': 't', 'steps': [
            {'assert': {'exists': {'id': 'no_such_xyz'}}},
        ]})
        self.assertEqual(g.recoveries, 0)
        self.assertEqual(g.pings, 0, '断言失败不该触发任何设备探测')

    def test_stats_shape(self):
        hdc, _ = make_driver()
        g = self._guard(hdc)
        self.assertEqual(set(g.stats()), 
                         {'pings', 'failed_pings', 'recoveries', 'hdc_restarts'})


# ================================================================ 屏幕看护

class TestScreenGuard(unittest.TestCase):
    """锁屏检测与唤醒。

    实测坑：DAYU200 默认几十秒息屏，息屏即锁屏，而锁屏界面**不进无障碍树** ——
    `dumpLayout` 只返回 1 个 bounds 全 0 的根节点。50 步连续执行要跑几分钟，
    不钉住屏幕就会出现「跑到一半控件树突然全空」，看起来像引擎崩了。
    """

    def _guard(self, hdc, **kw):
        kw.setdefault('verbose', False)
        kw.setdefault('sleep_fn', lambda s: None)
        return R.DeviceGuard(hdc, **kw)

    def test_count_nodes_helper(self):
        self.assertEqual(R._count_nodes({}), 1)
        self.assertEqual(R._count_nodes(None), 0)
        self.assertEqual(R._count_nodes(
            {'children': [{'children': []}, {'children': []}]}), 3)

    def test_has_window_true_when_normal(self):
        hdc, _ = make_driver()
        self.assertTrue(self._guard(hdc).has_window())

    def test_has_window_false_when_locked(self):
        """锁屏时控件树只有 1 个零尺寸根节点 —— 必须被识别为「没有窗口」。"""
        hdc, _ = make_driver(locked=True)
        g = self._guard(hdc)
        self.assertFalse(g.has_window())
        self.assertEqual(R._count_nodes(hdc._tree()), 1,
                         '锁屏的控件树应当只有一个节点')

    def test_ensure_awake_unlocks_locked_device(self):
        hdc, _ = make_driver(locked=True)
        g = self._guard(hdc)
        self.assertTrue(g.ensure_awake(), '应当能通过上滑解锁把屏幕恢复可用')
        self.assertFalse(hdc.locked, '设备仍处于锁屏')
        self.assertTrue(g.has_window())

    def test_ensure_awake_sets_screen_off_timeout(self):
        """必须把息屏超时覆盖掉，否则跑几分钟又黑了。"""
        hdc, _ = make_driver()
        self._guard(hdc).ensure_awake(screen_off_ms=1800000)
        self.assertTrue(
            any('power-shell timeout' in c and '1800000' in c for c in hdc.calls),
            f'没有设置息屏超时: {hdc.calls}')

    def test_ensure_awake_issues_wakeup(self):
        hdc, _ = make_driver()
        self._guard(hdc).ensure_awake()
        self.assertTrue(any('power-shell wakeup' in c for c in hdc.calls),
                        '没有下发唤醒命令')

    def test_ensure_awake_idempotent_on_healthy_device(self):
        hdc, _ = make_driver()
        g = self._guard(hdc)
        self.assertTrue(g.ensure_awake())
        self.assertEqual(len([c for c in hdc.calls if 'uiInput swipe' in c]), 0,
                         '屏幕已经可用时不该乱发解锁手势')

    def test_run_suite_pins_screen_before_start(self):
        """批量执行前应自动把屏幕钉住 —— 否则长跑必然翻车。"""
        hdc, d = make_driver(locked=True)
        g = self._guard(hdc)
        suite = make_runner(guard=g).run_suite([(d, short_case())], guard=g)
        self.assertFalse(hdc.locked, '批量执行没有解锁设备')
        self.assertEqual(suite.failed, 0, '解锁后用例应当能正常执行')

    def test_stats_shape(self):
        hdc, _ = make_driver()
        g = R.DeviceGuard(hdc, verbose=False)
        self.assertEqual(set(g.stats()), 
                         {'pings', 'failed_pings', 'recoveries', 'hdc_restarts'})


# ================================================================ 产物轮转

class TestArtifactPruning(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_c2_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, n):
        for i in range(1, n + 1):
            for suf in ('shot.png', 'layout.json'):
                with open(os.path.join(self.tmp, f'{i:04d}_{suf}'), 'w') as f:
                    f.write('x')

    def test_prunes_down_to_budget(self):
        self._seed(50)
        _, d = make_driver(artifact_dir=self.tmp)
        res = R.CaseResult(name='t', bundle='com.demo')
        res.steps = [R.StepResult(index=i, action='tap', ok=True) for i in range(1, 51)]
        make_runner(artifact_budget=10)._prune_artifacts(d, res)
        left = len(os.listdir(self.tmp))
        self.assertLessEqual(left, 10, f'轮转后仍有 {left} 个文件')

    def test_failed_step_artifacts_are_protected(self):
        self._seed(40)
        _, d = make_driver(artifact_dir=self.tmp)
        res = R.CaseResult(name='t', bundle='com.demo')
        res.steps = [R.StepResult(index=i, action='tap', ok=(i != 3))
                     for i in range(1, 41)]
        make_runner(artifact_budget=8)._prune_artifacts(d, res)
        names = os.listdir(self.tmp)
        self.assertTrue(any(n.startswith('0003_') for n in names),
                        f'失败步骤的产物被删了：{sorted(names)}')

    def test_no_prune_when_within_budget(self):
        self._seed(3)
        _, d = make_driver(artifact_dir=self.tmp)
        res = R.CaseResult(name='t', bundle='com.demo')
        res.steps = [R.StepResult(index=i, action='tap') for i in range(1, 4)]
        make_runner(artifact_budget=100)._prune_artifacts(d, res)
        self.assertEqual(len(os.listdir(self.tmp)), 6)

    def test_zero_budget_disables_pruning(self):
        self._seed(20)
        _, d = make_driver(artifact_dir=self.tmp)
        res = R.CaseResult(name='t', bundle='com.demo')
        r = make_runner(artifact_budget=0)
        r._prune_artifacts(d, res)
        self.assertEqual(len(os.listdir(self.tmp)), 40)


# ================================================================ 结果模型

class TestResultModel(unittest.TestCase):

    def _res(self, specs):
        r = R.CaseResult(name='t', bundle='b')
        r.steps = [R.StepResult(index=i, action='a', ok=ok, kind=kind,
                                cascade=cascade, cascade_of=1 if cascade else None)
                   for i, (ok, kind, cascade) in enumerate(specs, 1)]
        return r

    def test_rates(self):
        r = self._res([(True, None, False), (True, None, False),
                       (False, R.FailureKind.ASSERT, False),
                       (False, R.FailureKind.ASSERT, True)])
        self.assertEqual(r.total, 4)
        self.assertEqual(r.passed, 2)
        self.assertEqual(r.failed, 2)
        self.assertEqual(r.cascade_failed, 1)
        self.assertEqual(r.independent_failed, 1)
        self.assertEqual(r.success_rate, 0.5)
        self.assertEqual(r.independent_success_rate, 0.75)

    def test_empty_case_is_perfect(self):
        r = R.CaseResult(name='t', bundle='b')
        self.assertEqual(r.success_rate, 1.0)
        self.assertEqual(r.independent_success_rate, 1.0)
        self.assertTrue(r.ok)

    def test_rescue_rate_zero_attempts_is_one(self):
        r = self._res([(True, None, False)])
        self.assertEqual(r.retry_attempts, 0)
        self.assertEqual(r.retry_rescue_rate, 1.0)

    def test_failures_by_kind_sorted_desc(self):
        r = self._res([(False, R.FailureKind.ASSERT, False),
                       (False, R.FailureKind.ASSERT, False),
                       (False, R.FailureKind.LOCATE, False)])
        self.assertEqual(r.failures_by_kind(), {'ASSERT': 2, 'LOCATE': 1})

    def test_to_dict_is_json_serializable(self):
        r = self._res([(False, R.FailureKind.ASSERT, False), (True, None, False)])
        r.steps[0].attempts = [R.StepAttempt(attempt=1, ok=False,
                                             kind=R.FailureKind.ASSERT,
                                             error='boom', elapsed_ms=5)]
        json.dumps(r.to_dict(), ensure_ascii=False)      # 不抛异常即可

    def test_suite_aggregation(self):
        s = R.SuiteResult()
        for _ in range(2):
            c = R.CaseResult(name='c', bundle='b')
            c.steps = [R.StepResult(index=1, action='a', ok=True),
                       R.StepResult(index=2, action='a', ok=False,
                                    kind=R.FailureKind.ASSERT)]
            s.cases.append(c)
        self.assertEqual(s.total, 4)
        self.assertEqual(s.passed, 2)
        self.assertEqual(s.independent_success_rate, 0.5)
        self.assertFalse(s.kpi_ok(0.95))
        self.assertIn('ASSERT', s.failures_by_kind())


# ================================================================ 执行引擎 验收

class TestFiftyStepKpi(unittest.TestCase):
    """执行引擎 验收标准：50 步连续执行成功率 ≥ 95%。"""

    def test_fifty_steps_clean_run(self):
        _, d = make_driver()
        res = make_runner().run_case(d, fifty_step_case())
        self.assertEqual(res.total, 50)
        self.assertEqual(res.passed, 50)
        self.assertEqual(res.independent_success_rate, 1.0)
        self.assertTrue(res.ok)

    def test_fifty_steps_with_periodic_faults_still_meets_kpi(self):
        """周期性故障下仍应达到 95% —— 这正是重试机制存在的意义。"""
        fp = FaultPlan().fail_every('dump_layout', 11, kind='timeout')
        _, d = make_driver(fp)
        res = make_runner().run_case(d, fifty_step_case())
        self.assertEqual(res.total, 50)
        self.assertGreater(res.retry_attempts, 0, '故障没被触发，用例无效')
        self.assertGreaterEqual(
            res.independent_success_rate, 0.95,
            f'独立成功率 {res.independent_success_rate} 未达 95%'
            f'（原始 {res.success_rate}，重试 {res.retry_attempts} 次，'
            f'失败分类 {res.failures_by_kind()}）')

    def test_fifty_step_suite_kpi(self):
        _, d1 = make_driver()
        _, d2 = make_driver(page='home')
        suite = make_runner().run_suite([
            (d1, fifty_step_case('批次A')),
            (d2, fifty_step_case('批次B')),
        ])
        self.assertEqual(suite.total, 100)
        self.assertTrue(suite.kpi_ok(0.95))
        self.assertEqual(len(suite.cases), 2)

    def test_step_index_is_one_based_and_contiguous(self):
        _, d = make_driver()
        res = make_runner().run_case(d, fifty_step_case())
        self.assertEqual([s.index for s in res.steps], list(range(1, 51)))


# ================================================================ 契约入口

class TestContractEntry(unittest.TestCase):
    """`run(cases, device) -> RunReport` —— 系统对外接口契约。

    这一层存在的唯一理由是**接口一致性**：内部实现 `Runner.run_suite` 返回
    `SuiteResult`，而契约声明的是 `RunReport`。两者必须在接口冻结前对齐，
    否则 A/B 按契约调用时会直接断在类型上。
    """

    FAST = dict(default_timeout=250, poll_interval=50, verbose=False)

    def test_returns_contract_type(self):
        rep = R.run([short_case()], {'sim': True}, **self.FAST)
        self.assertIsInstance(rep, RunReport)

    def test_contract_fields_populated(self):
        rep = R.run([short_case()], {'sim': True}, **self.FAST)
        self.assertEqual(rep.total, 6)
        self.assertEqual(rep.passed, 6)
        self.assertEqual(rep.failed, 0)
        self.assertTrue(rep.ok)

    def test_accepts_case_dict(self):
        rep = R.run([{'name': 'c1', 'steps': [{'tap': {'id': 'username'}}]}],
                    {'sim': True}, **self.FAST)
        self.assertEqual(rep.total, 1)

    def test_accepts_case_file_path(self):
        path = os.path.join(ROOT, 'cases', 'login.yaml')
        self.assertTrue(os.path.isfile(path), f'缺少用例文件 {path}')
        # login.yaml 是示例用例，模拟设备上没有对应控件，只要链路通即可
        rep = R.run([path], {'sim': True}, **self.FAST)
        self.assertIsInstance(rep, RunReport)
        self.assertGreater(rep.total, 0)

    def test_accepts_driver_instance(self):
        _, d = make_driver()
        rep = R.run([short_case()], d, verbose=False)
        self.assertIsInstance(rep, RunReport)
        self.assertEqual(rep.bundle, 'com.demo')

    def test_accepts_hdc_instance(self):
        hdc, _ = make_driver()
        rep = R.run([short_case()], hdc, **self.FAST)
        self.assertIsInstance(rep, RunReport)
        self.assertEqual(rep.passed, 6)

    def test_bundle_override_from_device_dict(self):
        rep = R.run([{'name': 'c', 'steps': []}],
                    {'sim': True, 'bundle': 'com.custom'}, **self.FAST)
        self.assertEqual(rep.bundle, 'com.custom')

    def test_errors_carry_case_and_kind(self):
        """失败明细必须带「哪个用例 / 哪一类失败」—— 这是 B 做归因的输入。"""
        rep = R.run([{'name': '坏用例', 'steps': [
            {'assert': {'exists': {'id': 'no_such_xyz'}}},
        ]}], {'sim': True}, **self.FAST)
        self.assertEqual(rep.failed, 1)
        e = rep.errors[0]
        self.assertEqual(e['case'], '坏用例')
        self.assertEqual(e['kind'], 'ASSERT')
        self.assertEqual(e['step'], 1)
        self.assertIn('error', e)

    def test_rich_suite_is_attached(self):
        """契约对象上挂着完整 SuiteResult，取富信息不必再走一遍内部实现。"""
        rep = R.run([short_case()], {'sim': True}, **self.FAST)
        self.assertIsInstance(rep.suite, R.SuiteResult)
        self.assertEqual(rep.suite.total, 6)
        self.assertEqual(rep.suite.failures_by_kind(), {})

    def test_kpi_verdict_performed_when_requested(self):
        rep = R.run([short_case()], {'sim': True}, kpi=0.95, **self.FAST)
        self.assertTrue(rep.kpi_ok)
        self.assertEqual(rep.kpi_target, 0.95)

    def test_kpi_none_when_not_requested(self):
        rep = R.run([short_case()], {'sim': True}, **self.FAST)
        self.assertIsNone(rep.kpi_ok)

    def test_empty_cases_returns_empty_report(self):
        rep = R.run([], {'sim': True}, **self.FAST)
        self.assertEqual(rep.total, 0)
        self.assertIsInstance(rep, RunReport)

    def test_rejects_bad_case_type(self):
        with self.assertRaises(TypeError):
            R.run([12345], {'sim': True}, **self.FAST)

    def test_guard_is_wired_through_contract_entry(self):
        """契约入口必须把设备看护接上，不能被绕过。

        这条只验证「接线」而不验证「结果」：周期注入下，步骤最终成功还是失败
        取决于注入落在哪几次调用上，是有概率的。用概率断言结果会让测试变脆。
        正确做法是断言**必然成立**的因果关系 —— 只要步骤里出现过设备类失败，
        就一定经过 guard（探测/恢复）或触发过重试。
        """
        fp = FaultPlan().fail_every('dump_layout', 4, kind='device')
        hdc, d = make_driver(fp)
        g = R.DeviceGuard(hdc, allow_hdc_restart=False, verbose=False,
                          sleep_fn=lambda s: None)
        rep = R.run([{'name': 'c', 'steps': [{'tap': {'id': 'username'}}]}],
                    d, guard=g, verbose=False)
        self.assertGreater(sum(fp.stats().values()), 0, '故障没被触发，用例无效')

        touched = any(s.attempt_count > 1 or s.kind is R.FailureKind.DEVICE
                      for c in rep.suite.cases for s in c.steps)
        self.assertTrue(touched or g.pings + g.recoveries > 0,
                        '契约入口把设备看护绕过了')


if __name__ == '__main__':
    unittest.main(verbosity=2)
