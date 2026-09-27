# -*- coding: utf-8 -*-
"""失败快照要能进报告（C-2 的最后一环）。

链路有三段，任何一段断了快照都看不见 —— 所以测试要覆盖到第三段：

    runner._capture_trees()      抓 2 张控件树
      ↓ 回填
    driver.Step.layout_json      ← ★ 这一段之前是断的
      ↓ report.collect()
    报告里的「控件树快照」

★ 那段为什么容易断：报告的数据源是 **driver.Step**（`report.collect()` 取的是
`driver.summary()['failed_steps']`，而那是 `driver.Step.to_dict()`），
而快照是 **runner** 抓的。两层不通 —— 快照会安静地只活在
`runner.StepResult.trees` 里，报告一个字都不显示。

顺带一提：`driver.Step.layout_json` 这个字段**一直在 `to_dict` 的输出清单里，
却从来没有任何地方给它赋过值**，所以报告里的「控件树」从来就是空的。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import Driver, Runner, report                    # noqa: E402
from ohauto.sim import FakeHdc                               # noqa: E402


class SnapshotReportCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_snaprep_')
        sim = FakeHdc(start_page='login', verbose=False)
        self.d = Driver(bundle='com.demo.app', ability='EntryAbility', hdc=sim,
                        artifact_dir=self.tmp, default_timeout=250,
                        poll_interval=50, verbose=False,
                        sleep_fn=lambda s: None)
        self.runner = Runner(verbose=False, artifact_budget=0,
                             sleep_fn=lambda s: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_failing(self):
        return self.runner.run_case(self.d, {'name': 't', 'steps': [
            {'tap': {'id': 'no_such_control_xyz'}}]})

    # ---------------------------------------------------------------- 回填

    def test_snapshot_lands_on_driver_step(self):
        """★ 核心：快照必须回填到 driver.Step（报告的数据源）。"""
        self._run_failing()
        withsnap = [s for s in self.d.steps if getattr(s, 'layout_json', None)]
        self.assertTrue(withsnap, '快照没回填到 driver.Step —— 报告里会看不到')
        step = withsnap[-1]
        self.assertTrue(os.path.isfile(step.layout_json),
                        f'layout_json 指向的文件不存在: {step.layout_json}')
        self.assertTrue(step.extra.get('layout_after'),
                        '缺「稳定后」那张（用来区分控件自始不存在 vs 界面未稳定）')
        self.assertNotEqual(step.layout_json, step.extra.get('layout_after'),
                            '两个字段指向了同一张快照，等于只有一张')

    def test_successful_step_has_no_snapshot(self):
        self.runner.run_case(self.d, {'name': 't', 'steps': [
            {'tap': {'id': 'username'}}]})
        filled = [s for s in self.d.steps if getattr(s, 'layout_json', None)]
        self.assertEqual(filled, [], '成功步不该留快照')

    def test_snapshot_survives_in_to_dict(self):
        """回填后要能被 to_dict 导出（否则 report 读不到）。"""
        self._run_failing()
        dicts = [s.to_dict() for s in self.d.steps]
        self.assertTrue(any(d.get('layout_json') for d in dicts),
                        'to_dict 没把 layout_json 带出来')

    # ---------------------------------------------------------------- 渲染

    def test_markdown_report_shows_snapshots(self):
        res = self._run_failing()
        data = report.collect(self.d, run_report=res)
        path = os.path.join(self.tmp, 'r.md')
        report.to_markdown(data, path)
        with open(path, encoding='utf-8') as fh:

            md = fh.read()
        self.assertIn('控件树快照', md, 'Markdown 报告里没有控件树快照')

    def test_html_report_shows_snapshots(self):
        res = self._run_failing()
        data = report.collect(self.d, run_report=res)
        path = os.path.join(self.tmp, 'r.html')
        report.to_html(data, path)
        with open(path, encoding='utf-8') as fh:

            html = fh.read()
        self.assertIn('class="trees"', html, 'HTML 报告里没有快照列表')

    def test_report_without_snapshot_still_renders(self):
        """没有快照时报告要照常出（不能因为缺字段就崩或留个空标题）。"""
        data = report.collect(self.d)
        data['summary'] = {'failed_steps': [
            {'index': 1, 'kind': 'LOCATE', 'target': 'x', 'error': '找不到'}]}
        path = os.path.join(self.tmp, 'r2.md')
        report.to_markdown(data, path)
        with open(path, encoding='utf-8') as fh:

            md = fh.read()
        self.assertIn('找不到', md)
        self.assertNotIn('控件树快照', md, '没有快照却渲染了快照小节')


if __name__ == '__main__':
    unittest.main(verbosity=2)
