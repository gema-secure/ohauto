"""2C 内存趋势曲线**并入报告** —— 钉住 runner 采样接线与 Markdown 渲染口径。

规划原文（docs/发展规划与改进建议.md §2 2C）验收句是「note_stability 类
用例可在真机产出内存趋势曲线**并入报告**」。采集与分析已在
tests/test_h_perf_asserts.py 钉过；本文件钉的是**最后一公里**：

1. `Runner(collect_perf=False)`（默认）零额外设备往返，曲线为 None；
2. 开启后每步采一轮，曲线挂 `CaseResult.perf`，多用例合并进
   `SuiteResult.perf`（斜率**重算**而非平均）；
3. 采了但缺样本时**曲线照样在场**（`pss_n=0` + `pss_missing=N`），
   绝不退回 None —— 「缺样本 ≠ 没采」；
4. 报告只在有曲线时出「内存趋势」一节，且缺样本/样本不足如实披露。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import report                                   # noqa: E402
from ohauto.driver import Driver                            # noqa: E402
from ohauto.perf import analyze_samples                     # noqa: E402
from ohauto.runner import Runner, SuiteResult               # noqa: E402
from ohauto.sim import FakeHdc                              # noqa: E402

BUNDLE = 'com.example.app'

#: 五步用例（登录 -> 首页），每步会各采一轮 —— 曲线长度可预期。
CASE = {'name': '登录流程', 'bundle': BUNDLE, 'steps': [
    {'start': True},
    {'waitFor': {'id': 'username'}},
    {'input': {'id': 'username', 'value': 'alice'}},
    {'tap': {'id': 'btn_login'}},
    {'waitFor': {'id': 'tv_title'}},
]}


def _no_sleep(_s: float) -> None:
    pass


def _driver(sim=None) -> Driver:
    hdc = sim if sim is not None else FakeHdc(start_page='login')
    hdc.target = 'sim'
    return Driver(bundle=BUNDLE, hdc=hdc, artifact_dir=None,
                  verbose=False, sleep_fn=_no_sleep)


class _NoPssHdc(FakeHdc):
    """pidof 正常、hidumper 输出无 PSS 结构 —— 钉「采了但缺样本」。"""

    def shell(self, cmd: str, **kw):
        if cmd.startswith('hidumper') and '--mem' in cmd:
            return self._R('随便什么输出，没有 Pss 表头')
        return super().shell(cmd, **kw)


def _curve(pss_values, load=0.5, bundle=BUNDLE):
    """按分析口径造一条曲线（与 PerfChannel.curve() 同构）。"""
    series = [{'ts': f't{i}', 'pss_kb': v, 'load1': load}
              for i, v in enumerate(pss_values)]
    return {'bundle': bundle, 'series': series,
            'analysis': analyze_samples(series)}


# ================================================================ runner 接线

class TestRunnerPerfWiring(unittest.TestCase):

    def test_collect_perf_off_by_default(self):
        """默认关：曲线为 None，且**一次 hidumper 都不下发**。"""
        sim = FakeHdc(start_page='login')
        res = Runner(verbose=False).run_case(_driver(sim), CASE)
        self.assertIsNone(res.perf)
        self.assertFalse([c for c in sim.calls if 'hidumper' in c],
                         f'默认不该有内存采样往返：{sim.calls}')

    def test_collect_perf_samples_once_per_step(self):
        sim = FakeHdc(start_page='login')
        res = Runner(collect_perf=True, verbose=False).run_case(_driver(sim), CASE)
        self.assertIsNotNone(res.perf)
        self.assertEqual(len(res.perf['series']), 5)
        self.assertEqual(res.perf['analysis']['samples'], 5)
        self.assertEqual(res.perf['analysis']['pss_n'], 5)
        self.assertEqual(res.perf['bundle'], BUNDLE)
        self.assertTrue([c for c in sim.calls if 'hidumper' in c])

    def test_missing_samples_keep_curve_alive(self):
        """采了但缺 PSS：曲线仍在（pss_n=0 + pss_missing=5），斜率不判。"""
        res = Runner(collect_perf=True, verbose=False).run_case(
            _driver(_NoPssHdc(start_page='login')), CASE)
        self.assertIsNotNone(res.perf, '采过就必须留痕，不能退回「没采」')
        a = res.perf['analysis']
        self.assertEqual(a['pss_n'], 0)
        self.assertEqual(a['pss_missing'], 5)
        self.assertIsNone(a['slope_pct'])
        self.assertTrue(a['insufficient'])

    def test_single_case_curve_passes_through_unmerged(self):
        suite = Runner(collect_perf=True, verbose=False).run_suite(
            [(_driver(), CASE)])
        self.assertIsNotNone(suite.perf)
        self.assertEqual(suite.perf['analysis']['samples'], 5)
        self.assertNotIn('merged_cases', suite.perf)

    def test_multi_case_curves_are_merged_and_resloped(self):
        """多用例：序列拼接、`merged_cases` 记账、斜率按合并样本**重算**。"""
        suite = Runner(collect_perf=True, verbose=False).run_suite(
            [(_driver(), CASE), (_driver(), CASE)])
        self.assertIsNotNone(suite.perf)
        self.assertEqual(len(suite.perf['series']), 10)
        self.assertEqual(suite.perf['analysis']['samples'], 10)
        self.assertEqual(suite.perf['merged_cases'], 2)

    def test_refresh_perf_serves_incremental_suite_callers(self):
        """增量拼装 suite 的调用方调 refresh_perf 就能补上汇总曲线。"""
        suite = SuiteResult()
        suite.cases.append(
            Runner(collect_perf=True, verbose=False).run_case(_driver(), CASE))
        self.assertIsNone(suite.perf)          # 拼完还没算
        suite.refresh_perf()
        self.assertIsNotNone(suite.perf)
        self.assertEqual(suite.perf['analysis']['samples'], 5)

    def test_curve_rides_into_suite_dict(self):
        suite = Runner(collect_perf=True, verbose=False).run_suite(
            [(_driver(), CASE)])
        self.assertIn('perf', suite.to_dict())
        self.assertIn('perf', suite.cases[0].to_dict())


# ================================================================ 报告渲染

class TestReportPerfSection(unittest.TestCase):

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix='ohauto_perf_rep_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.out, ignore_errors=True)

    def _md(self, data) -> str:
        p = report.to_markdown(data, os.path.join(self.out, 'r.md'))
        with open(p, encoding='utf-8') as f:
            return f.read()

    def _data(self, **extra):
        d = {'bundle': BUNDLE, 'generated_at': 'T', 'summary': {}, 'steps': []}
        d.update(extra)
        return d

    def test_no_curve_no_section(self):
        text = self._md(self._data())
        self.assertNotIn('## 内存趋势', text)

    def test_curve_renders_section_with_numbers(self):
        text = self._md(self._data(perf=_curve([50000, 51000, 52000, 53000])))
        self.assertIn('## 内存趋势', text)
        self.assertIn('有效 PSS 4', text)
        self.assertIn('MB', text)
        self.assertIn('斜率：', text)

    def test_suite_and_case_curve_sources_are_picked_up(self):
        for key in ('suite', 'case'):
            text = self._md(self._data(**{key: {'perf': _curve(
                [50000, 51000, 52000, 53000])}}))
            self.assertIn('## 内存趋势', text, f'{key}.perf 应被采纳')

    def test_insufficient_samples_are_disclosed(self):
        text = self._md(self._data(perf=_curve([50000, 51000])))
        self.assertIn('不判定', text)

    def test_leak_suspect_is_flagged(self):
        text = self._md(self._data(perf=_curve(
            [50000] * 4 + [90000] * 4)))
        self.assertIn('疑似泄漏', text)

    def test_missing_samples_visible_in_trend_line(self):
        text = self._md(self._data(perf=_curve(
            [50000, None, 52000, 53000, 54000, None])))
        self.assertIn('缺样本 2', text)
        self.assertIn('·', text)

    def test_empty_series_does_not_create_heading(self):
        text = self._md(self._data(perf=_curve([])))
        self.assertNotIn('## 内存趋势', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)