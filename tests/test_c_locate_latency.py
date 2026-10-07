"""`tools/measure_locate_latency.py` 的离线部分钉子。

采集那半（`measure_round`）要真机，离线测不了；**统计与报告那半**是纯函数，
而且它是「P50 1157ms 里 94% 来自设备侧」这句话的唯一算法来源 ——
它错了，对外口径就错了，所以单独钉。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import measure_locate_latency as ml                              # noqa: E402


class TestPercentile(unittest.TestCase):

    def test_single_sample(self):
        self.assertEqual(ml.percentile([7.0], 50), 7.0)

    def test_p50_is_a_real_sample_not_an_average(self):
        """最近秩：给出来的必须是**真出现过的**值，不是插值出来的。"""
        got = ml.percentile([1.0, 100.0, 3.0, 4.0, 5.0], 50)
        self.assertIn(got, [1.0, 3.0, 4.0, 5.0, 100.0])

    def test_p100_is_the_max(self):
        self.assertEqual(ml.percentile([5.0, 1.0, 9.0], 100), 9.0)

    def test_empty_is_zero_not_an_exception(self):
        self.assertEqual(ml.percentile([], 50), 0.0)

    def test_rejects_out_of_range_q(self):
        for q in (0, -1, 101):
            with self.subTest(q=q):
                with self.assertRaises(ValueError):
                    ml.percentile([1.0], q)


class TestShare(unittest.TestCase):

    def test_ratio(self):
        self.assertAlmostEqual(ml.share(50, 200), 0.25)

    def test_zero_denominator_does_not_explode(self):
        """一轮都没跑成时也要能出报告，不能 ZeroDivisionError。"""
        self.assertEqual(ml.share(10, 0), 0.0)


class TestSummarize(unittest.TestCase):

    def _samples(self, n=20, device=1091.0, cat=67.0, host=1.0):
        return [{'device_ms': device, 'cat_ms': cat, 'host_ms': host}] * n

    def test_p50_p95_and_total(self):
        s = ml.summarize(self._samples())
        self.assertEqual(s['n'], 20)
        self.assertEqual(s['device_p50'], 1091.0)
        self.assertEqual(s['total_p50'], 1159.0)
        self.assertEqual(s['total_p95'], 1159.0)

    def test_device_share_matches_the_headline_94_percent(self):
        """「94% 来自设备侧」这句对外口径，从这条式子来。"""
        s = ml.summarize(self._samples())
        self.assertAlmostEqual(s['device_share'], 0.9413, places=3)
        self.assertLess(s['host_share'], 0.01)       # 宿主侧 < 1%

    def test_missing_segment_counts_as_zero(self):
        s = ml.summarize([{'device_ms': 100.0}, {'cat_ms': 20.0}])
        self.assertEqual(s['n'], 2)
        self.assertGreater(s['total_p50'], 0)

    def test_none_and_empty_input(self):
        s = ml.summarize([])
        self.assertEqual(s['n'], 0)
        self.assertEqual(s['total_p50'], 0.0)
        self.assertEqual(s['device_share'], 0.0)

    def test_non_dict_rows_are_ignored(self):
        s = ml.summarize([{'device_ms': 10.0}, 'junk', None])
        self.assertEqual(s['n'], 1)

    def test_mean_is_averaged_over_rounds(self):
        s = ml.summarize([{'device_ms': 100.0}, {'device_ms': 200.0}])
        self.assertEqual(s['total_mean'], 150.0)


class TestJsonlRoundTrip(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, text):
        p = os.path.join(self.tmp.name, 'samples.jsonl')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        return p

    def test_reads_one_record_per_line(self):
        p = self._write('\n'.join(json.dumps({'device_ms': i}) for i in range(3)) + '\n')
        self.assertEqual(len(ml.load_jsonl(p)), 3)

    def test_skips_blank_and_broken_lines(self):
        """现场采集中断过会留下半行 —— 分析要能继续，不要整份报废。"""
        p = self._write('{"device_ms": 5}\n\n{ not json\n{"device_ms": 7}\n')
        rows = ml.load_jsonl(p)
        self.assertEqual([r['device_ms'] for r in rows], [5, 7])


class TestRender(unittest.TestCase):

    def test_report_states_the_verdict_and_the_caveat(self):
        text = ml.render(ml.summarize(
            [{'device_ms': 1091.0, 'cat_ms': 67.0, 'host_ms': 1.0}] * 20))
        self.assertIn('未达标', text)          # 端到端确实超阈值
        self.assertIn('94%', text)             # 但归因写在报告里
        self.assertIn('少 dump', text)          # 优化方向
        self.assertIn('不混用', text)           # 口径

    def test_empty_summary_is_a_plain_sentence(self):
        self.assertIn('没有样本', ml.render(ml.summarize([])))


class TestCli(unittest.TestCase):
    """`--analyze` 必须能独立跑通（不需要设备）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_analyze_mode_exits_zero_on_a_small_sample(self):
        p = os.path.join(self.tmp.name, 's.jsonl')
        with open(p, 'w', encoding='utf-8') as f:
            for _ in range(3):
                f.write(json.dumps({'device_ms': 900.0, 'cat_ms': 60.0,
                                    'host_ms': 2.0}) + '\n')
        proc = subprocess.run([sys.executable, os.path.join(ROOT, 'tools',
                                                            'measure_locate_latency.py'),
                               '--analyze', p],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('device', proc.stdout)
        self.assertIn('样本 3 轮', proc.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
