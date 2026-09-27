# -*- coding: utf-8 -*-
"""定位失败回写链路的单测（分工卡第五章契约的最后一环）。

契约：执行失败时调用 A 的 `record_locator_failure` 回写，让自愈有输入。

两边接口对不上（runner 有规格、A 要 locator_id），所以解耦成：
  runner 递出**规格** → `tools/wire_locator_sink.make_locator_sink` 反查成 id
这些测试同时守住 runner 侧的钩子行为和反查侧的选择逻辑。

★ 最要紧的一条是「**非 LOCATE 失败不许回写**」：
设备掉线、应用崩溃同样会让步骤失败，但它们不是「这个定位器找不到了」。
混进去会污染健康度账本 → 自愈在错误时机换代 → **把好好的定位器换掉**。
这属于「记录型机制记错比不记更危险」，必须有测试钉死。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import LocatorManager, Runner                      # noqa: E402
from ohauto import runner as R                                 # noqa: E402
from ohauto.driver import Driver                               # noqa: E402
from ohauto.sim import FakeHdc, FaultPlan                      # noqa: E402

sys.path.insert(0, os.path.join(ROOT, 'tools'))
from wire_locator_sink import make_locator_sink, _find_locator_id   # noqa: E402


def _driver(faults=None, page='login'):
    sim = FakeHdc(start_page=page, verbose=False, faults=faults)
    return Driver(bundle='com.demo.app', ability='EntryAbility', hdc=sim,
                  default_timeout=250, poll_interval=50, verbose=False,
                  sleep_fn=lambda s: None)


class TestRunnerSinkHook(unittest.TestCase):
    """runner 侧：什么时候该调钩子、什么时候不该。"""

    def setUp(self):
        self.calls = []

        def sink(spec, reason):
            self.calls.append((spec, reason))

        self.sink = sink
        self.runner = Runner(locator_sink=sink, verbose=False,
                             artifact_budget=0, sleep_fn=lambda s: None)

    def test_locate_failure_calls_sink_with_spec(self):
        d = _driver()
        self.runner.run_case(d, {'name': 't', 'steps': [
            {'tap': {'id': 'no_such_thing'}}]})
        self.assertEqual(len(self.calls), 1, '定位失败必须回写一次')
        spec, reason = self.calls[0]
        self.assertEqual(spec, {'id': 'no_such_thing'},
                         '递出去的应该是 DSL 里那个原始规格')
        self.assertTrue(reason, '回写必须带原因')

    def test_successful_step_does_not_call_sink(self):
        d = _driver()
        self.runner.run_case(d, {'name': 't', 'steps': [
            {'tap': {'id': 'username'}}]})
        self.assertEqual(self.calls, [], '成功步不该回写')

    def test_non_locate_failure_does_not_call_sink(self):
        """★ 设备类失败不能回写 —— 否则会污染健康度、让自愈错误换代。"""
        # 让每次 hdc 调用都按 device 类失败 → 步骤应被判为 DEVICE 而不是 LOCATE
        plan = FaultPlan().fail_every('any', every=1, kind='device')
        d = _driver(faults=plan)
        try:
            res = self.runner.run_case(d, {'name': 't', 'steps': [
                {'tap': {'id': 'username'}}]})
            kinds = [getattr(s.kind, 'value', None)
                     for s in res.steps if not s.ok]
            self.assertTrue(kinds, '故障没生效，用例竟然通过了 —— 测试本身无效')
        except Exception:
            pass
        self.assertEqual(self.calls, [],
                         '设备类失败被当成了定位失败回写')

    def test_sink_exception_does_not_change_verdict(self):
        """回写是旁路：它抛异常绝不能把 LOCATE 改成别的类别。"""
        def boom(spec, reason):
            raise RuntimeError('sink 故意炸')

        runner = Runner(locator_sink=boom, verbose=False,
                        artifact_budget=0, sleep_fn=lambda s: None)
        d = _driver()
        res = runner.run_case(d, {'name': 't', 'steps': [
            {'tap': {'id': 'no_such_thing'}}]})
        step = [s for s in res.steps if not s.ok][0]
        self.assertIs(step.kind, R.FailureKind.LOCATE,
                      'sink 抛异常把原判定改掉了')

    def test_no_sink_configured_is_fine(self):
        """没配 sink 时一切照旧（不能因为缺钩子就崩）。"""
        runner = Runner(verbose=False, artifact_budget=0,
                        sleep_fn=lambda s: None)
        d = _driver()
        res = runner.run_case(d, {'name': 't', 'steps': [
            {'tap': {'id': 'no_such_thing'}}]})
        self.assertFalse(res.ok)
        self.assertIs([s for s in res.steps if not s.ok][0].kind,
                      R.FailureKind.LOCATE)


class TestReverseLookup(unittest.TestCase):
    """反查侧：规格 → locator_id。"""

    def setUp(self):
        self.d = _driver()
        self.lm = LocatorManager()
        self.lm.register({'id': 'username'}, self.d.refresh())

    def test_find_by_id(self):
        lid = _find_locator_id(self.lm, {'id': 'username'})
        self.assertTrue(lid, '按 id 反查失败')
        self.assertEqual(self.lm.spec(lid).target_id, 'username')

    def test_unknown_spec_returns_empty(self):
        self.assertEqual(_find_locator_id(self.lm, {'id': 'nope'}), '')
        self.assertEqual(_find_locator_id(self.lm, {}), '')

    def test_no_type_only_lookup(self):
        """只给 type 不许反查 —— 一页里同类型控件成堆，必然撞车。"""
        self.assertEqual(_find_locator_id(self.lm, {'type': 'TextInput'}), '')

    def test_sink_skips_unknown_and_reports_known(self):
        sink = make_locator_sink(self.lm, verbose=False)
        sink({'id': 'nope'}, 'x')
        self.assertEqual(sink.stats['skipped'], 1)
        self.assertEqual(sink.stats['reported'], 0)

        before = self.lm.health(list(self.lm.all_health())[0]).consecutive_failures
        sink({'id': 'username'}, 'y')
        self.assertEqual(sink.stats['reported'], 1)
        lid = list(self.lm.all_health())[0]
        self.assertEqual(self.lm.health(lid).consecutive_failures, before + 1,
                         '健康度没被写进去')


class TestEndToEnd(unittest.TestCase):
    """跑真链路：runner 失败 → sink → 健康度账本。"""

    def test_failure_lands_in_health_ledger(self):
        d = _driver()
        lm = LocatorManager()
        lm.register({'id': 'username'}, d.refresh())
        lid = list(lm.all_health())[0]
        self.assertEqual(lm.health(lid).consecutive_failures, 0)

        runner = Runner(locator_sink=make_locator_sink(lm), verbose=False,
                        artifact_budget=0, sleep_fn=lambda s: None)
        # 用已注册的 id + 一个匹配不上的 text → 定位必失败，且规格能反查到
        runner.run_case(d, {'name': 't', 'steps': [
            {'tap': {'id': 'username', 'text': '绝对匹配不上的文案'}}]})

        self.assertGreaterEqual(lm.health(lid).consecutive_failures, 1,
                                '执行失败没有流到健康度账本')


if __name__ == '__main__':
    unittest.main(verbosity=2)
