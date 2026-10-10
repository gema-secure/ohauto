"""失败归因闭环回归钉子：失败步 → 归因 → 结果/报告。

背景：`diagnose_failed_step()` 早就就绪、`StepResult` 的
`trees` / `locator_id` 字段也早就备好，但**生产链路一次都没调用过** ——
归因只能靠 examples 手动跑，于是「崩溃 ✅ / 白屏 ✅」拿不出证据链。

本文件钉住四件事：

1. **默认关**：历史行为一字不变（`to_dict()` 里没有 `verdict` 键）；
2. **开启后失败步才有结论**，成功步一步都不归因（别白花开销）；
3. 归因引擎**自己抛异常时必须降级** —— 被测用例挂了是事实，
   不能因为归因崩了就把整轮执行也带崩；
4. 结论要能**走到报告里**（md / html 都有「失败步归因」一节）。

用 `run_step` 替身注入失败，不依赖真机、不依赖归因引擎。
"""
import os
import shutil
import tempfile
import unittest

from ohauto import report
from ohauto.runner import FailureKind, Runner, StepAttempt, StepResult


class FakeVerdict:
    """最小 Verdict 替身 —— 测试聚焦「接没接上」，不测归因引擎本身。"""

    def __init__(self, category='LOCATOR'):
        self.category = category

    def to_dict(self):
        return {'category': self.category, 'category_cn': '定位失败',
                'confidence': 0.6, 'evidence': ['测试替身证据：快照里没有该控件'],
                'suggestion': '测试替身建议：换用 text 匹配'}

    def __str__(self):
        return '[定位失败] 置信度 0.60（步骤 ?） — 测试替身建议：换用 text 匹配'


class DriverStub:
    """只提供 runner / report 会读到的字段。"""
    bundle = 'com.demo.app'
    ability = 'EntryAbility'
    artifact_dir = None
    hdc = None


CASE = {'name': '闭环用例', 'steps': [
    {'start': True},        # 1 成功
    {'tap': {'id': 'ok'}},  # 2 成功
    {'tap': {'id': 'boom'}},  # 3 失败
]}


def _runner_with_failing_step3(diagnoser=None, **kw):
    """造一个 Runner：第 3 步失败，其余成功（不碰真机）。"""
    r = Runner(verbose=False, diagnoser=diagnoser, **kw)

    def fake_run_step(driver, index, action, arg):
        return StepResult(index=index, action=action, ok=(index != 3))

    r.run_step = fake_run_step
    return r


class TestDiagnoseDefaultOff(unittest.TestCase):
    """默认关 —— 不许改变既有行为。"""

    def test_no_verdict_and_no_key(self):
        r = _runner_with_failing_step3()
        self.assertFalse(r.diagnose_failures)
        res = r.run_case(DriverStub(), CASE)
        self.assertEqual([s.ok for s in res.steps], [True, True, False])
        self.assertTrue(all(s.verdict is None for s in res.steps))
        self.assertTrue(all(not s.diagnosed for s in res.steps))
        # 「没做归因」不该在报告里冒出一个空 verdict 键
        for s in res.steps:
            self.assertNotIn('verdict', s.to_dict())

    def test_injected_diagnoser_not_called_when_off(self):
        calls = []
        r = _runner_with_failing_step3(
            diagnoser=lambda step, drv: calls.append(step.index))
        r.run_case(DriverStub(), CASE)
        self.assertEqual(calls, [], '开关关着就不该调用归因')


class TestDiagnoseOnFailure(unittest.TestCase):
    """开启后：只给失败步归因，且结论能被报告消费。"""

    def test_failed_step_gets_verdict(self):
        r = _runner_with_failing_step3(
            diagnoser=lambda step, drv: FakeVerdict(),
            diagnose_failures=True)
        res = r.run_case(DriverStub(), CASE)
        self.assertIsNone(res.steps[0].verdict, '成功步不该有结论')
        self.assertIsNone(res.steps[1].verdict, '成功步不该有结论')
        self.assertIsInstance(res.steps[2].verdict, FakeVerdict)
        self.assertTrue(res.steps[2].diagnosed)
        self.assertEqual(res.steps[2].to_dict()['verdict']['category'], 'LOCATOR')
        self.assertEqual(r.diagnose_errors, [])

    def test_diagnoser_called_once_per_failure(self):
        seen = []

        def spy(step, drv):
            seen.append((step.index, drv))
            return FakeVerdict()

        r = _runner_with_failing_step3(diagnoser=spy, diagnose_failures=True)
        d = DriverStub()
        r.run_case(d, CASE)
        self.assertEqual([i for i, _ in seen], [3], '只该对失败步调一次')
        self.assertIs(seen[0][1], d, '归因必须拿到当时那个 driver')

    def test_execution_result_unaffected(self):
        """归因只加信息，不改判定 —— ok/通过步数/级联都必须原样。"""
        base = _runner_with_failing_step3()
        res_off = base.run_case(DriverStub(), CASE)

        r_on = _runner_with_failing_step3(
            diagnoser=lambda step, drv: FakeVerdict(), diagnose_failures=True)
        res_on = r_on.run_case(DriverStub(), CASE)

        self.assertEqual(res_off.passed, res_on.passed)
        self.assertEqual(res_off.failed, res_on.failed)
        self.assertEqual(res_off.ok, res_on.ok)
        self.assertEqual([s.ok for s in res_off.steps],
                         [s.ok for s in res_on.steps])


class TestDiagnoseDegrades(unittest.TestCase):
    """归因是**旁路** —— 它挂了不能拖垮执行。"""

    def test_exception_is_swallowed_and_recorded(self):
        def boom(step, drv):
            raise RuntimeError('归因引擎故意炸了')

        r = _runner_with_failing_step3(diagnoser=boom, diagnose_failures=True)
        res = r.run_case(DriverStub(), CASE)

        # 执行结果完全不受影响
        self.assertEqual([s.ok for s in res.steps], [True, True, False])
        self.assertIsNone(res.steps[2].verdict)
        self.assertNotIn('verdict', res.steps[2].to_dict())
        # 但也不能静默 —— 必须留下痕迹
        self.assertEqual(len(r.diagnose_errors), 1)
        self.assertIn('归因引擎故意炸了', r.diagnose_errors[0])
        self.assertIn('步骤 3', r.diagnose_errors[0])


class TestVerdictReachesReport(unittest.TestCase):
    """结论要走到报告里 —— 藏在内存里的结论等于没做。"""

    def _case_result(self):
        r = _runner_with_failing_step3(
            diagnoser=lambda step, drv: FakeVerdict(), diagnose_failures=True)
        return r.run_case(DriverStub(), CASE)

    def test_markdown_has_diagnosis_section(self):
        data = report.collect(DriverStub(), run_report=self._case_result())
        out = tempfile.mkdtemp(prefix='ohauto_close_')
        self.addCleanup(shutil.rmtree, out, True)
        path = report.to_markdown(data, os.path.join(out, 'r.md'))
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('失败步归因', text)
        self.assertIn('测试替身证据', text)
        self.assertIn('测试替身建议', text)

    def test_html_has_diagnosis_section(self):
        data = report.collect(DriverStub(), run_report=self._case_result())
        out = tempfile.mkdtemp(prefix='ohauto_close_')
        self.addCleanup(shutil.rmtree, out, True)
        path = report.to_html(data, os.path.join(out, 'r.html'))
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('失败步归因', text)

    def test_report_without_diagnosis_has_no_section(self):
        """没开启归因时不该冒出空标题。"""
        r = _runner_with_failing_step3()
        data = report.collect(DriverStub(), run_report=r.run_case(DriverStub(), CASE))
        out = tempfile.mkdtemp(prefix='ohauto_close_')
        self.addCleanup(shutil.rmtree, out, True)
        path = report.to_markdown(data, os.path.join(out, 'r.md'))
        with open(path, encoding='utf-8') as f:
            self.assertNotIn('失败步归因', f.read())


class TestSuiteInheritsDiagnosis(unittest.TestCase):
    """批量路径必须同样接上（run_suite 复用 run_case，这里钉死这个前提）。"""

    def test_suite_cases_get_verdicts(self):
        r = _runner_with_failing_step3(
            diagnoser=lambda step, drv: FakeVerdict(), diagnose_failures=True)
        suite = r.run_suite([(DriverStub(), CASE)])
        self.assertEqual(len(suite.cases), 1)
        self.assertIsInstance(suite.cases[0].steps[2].verdict, FakeVerdict)
        self.assertEqual(suite.failed, 1)


class TestStepVerdictProperties(unittest.TestCase):

    def test_verdict_cn_empty_without_verdict(self):
        self.assertEqual(StepResult(index=1, action='tap').verdict_cn, '')

    def test_verdict_cn_renders_verdict(self):
        sr = StepResult(index=2, action='tap', ok=False, verdict=FakeVerdict())
        self.assertIn('定位失败', sr.verdict_cn)


class TestLocatorIdWiring(unittest.TestCase):
    """闭环接线的最后一环：归因结论要能带上 `locator_id`。

    没有 id 时归因只能说「没能回写定位器自愈」（`diagnose.py:929`），
    读者看不出该修哪个定位器。所以执行器要把规格反查成 id 再交给归因。

    ⚠️ 这里**只测「补 id」，不测回写** —— 回写只有 `_report_locator_failure`
    一条路，两条路都写会把同一次失败计两遍，破坏 A 修好的幂等。
    """

    CASE_TAP = {'name': 'w', 'steps': [{'tap': {'id': 'username'}}]}

    def _runner(self, resolver, **kw):
        r = Runner(verbose=False, locator_id_resolver=resolver,
                   diagnose_failures=True, **kw)

        def fail_locate(driver, index, action, arg):
            sr = StepResult(index=index, action=action, ok=False,
                            kind=FailureKind.LOCATE, arg=arg)
            sr.attempts = [StepAttempt(attempt=1, ok=False,
                                       kind=FailureKind.LOCATE, error='找不到')]
            return sr

        r.run_step = fail_locate
        return r

    def test_resolver_fills_locator_id(self):
        r = self._runner(lambda spec: 'L001_' + str(spec.get('id')))
        res = r.run_case(DriverStub(), self.CASE_TAP)
        self.assertEqual(res.steps[0].locator_id, 'L001_username')

    def test_verdict_carries_locator_id(self):
        """归因结论要带 id —— 否则读者不知道该修哪个定位器。"""
        r = self._runner(lambda spec: 'L001_username')
        res = r.run_case(DriverStub(), self.CASE_TAP)
        sr = res.steps[0]
        self.assertTrue(sr.diagnosed)
        self.assertEqual(getattr(sr.verdict, 'locator_id', ''), 'L001_username')
        self.assertIn('L001_username', sr.verdict_cn)
        self.assertNotIn('没能回写', sr.verdict_cn,
                         '有 id 时归因不该再说「没能回写」')

    def test_without_resolver_id_stays_empty(self):
        """不接反查也能跑 —— 只是归因结论里没有 id（降级，不是故障）。"""
        r = self._runner(None)
        res = r.run_case(DriverStub(), self.CASE_TAP)
        self.assertIsNone(res.steps[0].locator_id)

    def test_only_fills_for_locate_kind(self):
        """设备掉线、崩溃同样会失败，但不该被当成「定位器找不到了」。"""
        def fail_assert(driver, index, action, arg):
            return StepResult(index=index, action=action, ok=False,
                              kind=FailureKind.ASSERT, arg=arg)

        r = self._runner(lambda spec: 'L001_x')
        r.run_step = fail_assert
        res = r.run_case(DriverStub(), self.CASE_TAP)
        self.assertIsNone(res.steps[0].locator_id,
                          '非定位失败不该补 locator_id，否则污染健康度账本')

    def test_resolver_exception_does_not_break(self):
        def boom(spec):
            raise RuntimeError('反查炸了')

        r = self._runner(boom)
        res = r.run_case(DriverStub(), self.CASE_TAP)
        self.assertIsNone(res.steps[0].locator_id)
        self.assertFalse(res.ok, '执行判定不受反查失败影响')


if __name__ == '__main__':
    unittest.main()
