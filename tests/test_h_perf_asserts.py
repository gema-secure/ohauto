"""2C 性能与资源采集 —— 钉住采样/分析/通道与四条断言原语的口径。

钉的规则（docs/发展规划与改进建议.md §2 2C）：
1. 采样失败记 None + 警告，绝不编造、绝不中断（缺样本 ≠ 正常）；
2. 缺样本的内存断言**不判通过**；
3. PSS 斜率判据：前后半均值涨幅超阈值报嫌疑，样本不足不判；
4. 断言留痕走 Step（kind/extra），失败进 DriverError。
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

from ohauto.driver import Driver, DriverError           # noqa: E402
from ohauto.matcher import ON                            # noqa: E402
from ohauto.perf import (analyze_samples, device_sample,  # noqa: E402
                         MIN_PSS_SAMPLES, PSS_SLOPE_THRESHOLD)
from ohauto.signals import PerfChannel, collect_signals  # noqa: E402
from ohauto.sim import FakeHdc, _b, _node                # noqa: E402


def _no_sleep(_s: float) -> None:
    pass


class _R:
    def __init__(self, out: str = ''):
        self.stdout = out


class _BrokenHdc:
    """shell 一律抛异常 —— 钉「采样是旁路，失败降级为警告」。"""

    def shell(self, cmd: str, **kw):
        raise RuntimeError('设备不在场')


class _GarbageHdc:
    """pidof 正常、hidumper 输出没有 PSS 结构 —— 钉「缺样本 ≠ 正常」。"""

    def __init__(self):
        self.tmp_dir = '/data/local/tmp'

    def shell(self, cmd: str, **kw):
        if cmd.startswith('pidof'):
            return _R('12345')
        if cmd.startswith('hidumper'):
            return _R('随便什么输出，没有 Pss 表头')
        return _R('')


# 复选框页夹具：cb_read 勾选、cb_push 未勾选（checked 缺省 false）。
_CHECK_PAGE = {
    'attributes': {'type': 'Root', 'id': 'checkRoot',
                   'bounds': _b(0, 0, 1080, 2340), 'visible': 'true'},
    'children': [
        _node('CheckBox', 'cb_read', '已阅读并同意', _b(120, 300, 960, 380),
              'false', checked='true'),
        _node('CheckBox', 'cb_push', '接收推送', _b(120, 420, 960, 500), 'false'),
        _node('Button', 'btn_disabled', '置灰按钮', _b(120, 540, 960, 620),
              'true', enabled='false'),
    ],
}


class TestDeviceSample(unittest.TestCase):

    def setUp(self):
        self.sim = FakeHdc(start_page='login')

    def test_full_sample_against_sim(self):
        """模拟设备全链采样：PSS 解析（表头+两行 Total）、loadavg、存活。"""
        s = device_sample(self.sim, 'com.demo.app')
        self.assertEqual(41322, s['pss_kb'])
        self.assertEqual(0.5, s['load1'])
        self.assertTrue(s['alive'])
        self.assertEqual('12345', s['pid'])
        self.assertEqual([], s['warn'])

    def test_dead_bundle_short_circuits(self):
        """进程不在：alive=False 即返回，不再付 hidumper/loadavg 往返。"""
        sim = FakeHdc(start_page='login', dead_bundles=['com.demo.app'])
        s = device_sample(sim, 'com.demo.app')
        self.assertFalse(s['alive'])
        self.assertIsNone(s['pss_kb'])
        self.assertIn('被测应用进程不在运行', s['warn'])
        self.assertNotIn('shell:hidumper', sim.calls)

    def test_broken_hdc_never_raises(self):
        s = device_sample(_BrokenHdc(), 'com.demo.app')
        self.assertIsNone(s['pss_kb'])
        self.assertIsNone(s['alive'])
        self.assertTrue(any(w.startswith('pidof 失败') for w in s['warn']))

    def test_garbage_hidumper_marks_missing_sample(self):
        """hidumper 输出解析不到 PSS → None + 警告，绝不编造数值。"""
        s = device_sample(_GarbageHdc(), 'com.demo.app')
        self.assertIsNone(s['pss_kb'])
        self.assertIn('没解析到 PSS', ' '.join(s['warn']))


class TestAnalyzeSamples(unittest.TestCase):

    def test_flat_series_no_leak(self):
        a = analyze_samples([{'pss_kb': 40000, 'load1': 0.5}] * 8)
        self.assertFalse(a['leak_suspect'])
        self.assertEqual(0.0, a['slope_pct'])

    def test_rising_series_flags_suspect(self):
        samples = [{'pss_kb': v, 'load1': 1.0} for v in [100] * 4 + [200] * 4]
        a = analyze_samples(samples)
        self.assertTrue(a['leak_suspect'])
        self.assertEqual(100.0, a['slope_pct'])
        self.assertEqual((100, 200), (a['pss_min'], a['pss_max']))

    def test_insufficient_samples_refuse_to_judge(self):
        """有效 PSS 不足 4 个：不判斜率（slope_pct=None），不报嫌疑。"""
        a = analyze_samples([{'pss_kb': 100, 'load1': None}] * (MIN_PSS_SAMPLES - 1))
        self.assertTrue(a['insufficient'])
        self.assertIsNone(a['slope_pct'])
        self.assertFalse(a['leak_suspect'])

    def test_missing_samples_counted(self):
        samples = ([{'pss_kb': None, 'load1': None}] * 3
                   + [{'pss_kb': 100, 'load1': 0.5}] * 6)
        a = analyze_samples(samples)
        self.assertEqual(3, a['pss_missing'])
        self.assertEqual(6, a['pss_n'])
        self.assertFalse(a['leak_suspect'])

    def test_load_stats(self):
        samples = [{'pss_kb': 100, 'load1': l} for l in [0.2, 0.4, 1.0, 0.8]]
        a = analyze_samples(samples)
        self.assertEqual(0.6, a['load_mean'])
        self.assertEqual(1.0, a['load_peak'])

    def test_threshold_is_the_documented_constant(self):
        samples = [{'pss_kb': 100, 'load1': None}] * 4 + \
                  [{'pss_kb': 100 * (1 + PSS_SLOPE_THRESHOLD / 100 + 0.01), 'load1': None}] * 4
        self.assertTrue(analyze_samples(samples)['leak_suspect'])


class TestPerfChannel(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_perf_')
        self.sim = FakeHdc(start_page='login')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sample_accumulates_and_curves(self):
        ch = PerfChannel(self.sim, 'com.demo.app')
        for i in range(6):
            self.sim.pss_kb = 40000 + i * 3000
            ch.sample(rounds_done=i)
        c = ch.curve()
        self.assertEqual(6, len(c['series']))
        self.assertEqual(55000, c['series'][-1]['pss_kb'])
        self.assertEqual(6, c['analysis']['pss_n'])
        self.assertFalse(c['analysis']['leak_suspect'])

    def test_sample_never_raises_on_broken_hdc(self):
        ch = PerfChannel(_BrokenHdc(), 'com.demo.app')
        s = ch.sample()
        self.assertIsNone(s['pss_kb'])
        self.assertTrue(s['warn'])
        self.assertEqual(1, len(ch.samples))

    def test_flush_jsonl_default_keeps_samples(self):
        ch = PerfChannel(self.sim, 'com.demo.app')
        ch.sample()
        ch.sample()
        path = os.path.join(self.tmp, 'telemetry.jsonl')
        self.assertEqual(2, ch.flush(path))
        with open(path, encoding='utf-8') as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(2, len(lines))
        self.assertEqual(2, len(ch.samples), '默认不清空 —— 曲线要能随时重出')
        self.assertEqual(ch.curve()['series'][0]['pss_kb'],
                         lines[0]['pss_kb'])

    def test_flush_clear_true_and_empty_noop(self):
        ch = PerfChannel(self.sim, 'com.demo.app')
        self.assertEqual(0, ch.flush(os.path.join(self.tmp, 'empty.jsonl')))
        ch.sample()
        path = os.path.join(self.tmp, 't.jsonl')
        self.assertEqual(1, ch.flush(path, clear=True))
        self.assertEqual([], ch.samples)
        self.assertEqual(0, ch.flush(path), '清空后重复 flush 不得重复落盘')

    def test_attach_writes_signals_perf(self):
        from ohauto.signals import Signals
        sig = Signals(bundle='com.demo.app')
        PerfChannel(self.sim, 'com.demo.app').attach(sig, rounds_done=3)
        self.assertEqual(41322, sig.perf['pss_kb'])
        self.assertEqual(3, sig.perf['rounds_done'])
        self.assertIn('perf', sig.to_dict())


class TestCollectSignalsPerf(unittest.TestCase):

    def test_opt_in_samples_perf(self):
        sim = FakeHdc(start_page='login')
        sig = collect_signals(sim, 'com.demo.app', collect_perf=True)
        self.assertIsNotNone(sig.perf)
        self.assertEqual(41322, sig.perf['pss_kb'])
        self.assertIn('perf', sig.to_dict())

    def test_default_off_no_roundtrips(self):
        """默认关：诊断链路零额外往返，perf=None（没采 ≠ 缺样本）。"""
        sim = FakeHdc(start_page='login')
        sig = collect_signals(sim, 'com.demo.app')
        self.assertIsNone(sig.perf)
        self.assertNotIn('perf', sig.to_dict())
        self.assertNotIn('shell:hidumper --mem', sim.calls)


class TestAssertPrimitives(unittest.TestCase):

    def _driver(self, sim=None):
        sim = sim or FakeHdc(start_page='login')
        d = Driver(bundle='com.demo.app', hdc=sim, verbose=False,
                   sleep_fn=_no_sleep)
        return d, sim

    def test_checked_true_and_false(self):
        sim = FakeHdc(start_page='check', seed={'check': _CHECK_PAGE})
        d, _ = self._driver(sim)
        node = d.assert_checked(ON.id('cb_read'))
        self.assertTrue(node.checked)
        node = d.assert_checked(ON.id('cb_push'), expected=False)
        self.assertFalse(node.checked)

    def test_checked_mismatch_raises_and_records(self):
        sim = FakeHdc(start_page='check', seed={'check': _CHECK_PAGE})
        d, _ = self._driver(sim)
        with self.assertRaises(DriverError) as ctx:
            d.assert_checked(ON.id('cb_push'))
        self.assertIn('checked=True', str(ctx.exception))
        self.assertIn('实际: False', str(ctx.exception))
        step = d.steps[-1]
        self.assertEqual('assert.checked', step.kind)
        self.assertFalse(step.ok)

    def test_enabled_default_true_and_disabled_control(self):
        sim = FakeHdc(start_page='check', seed={'check': _CHECK_PAGE})
        d, _ = self._driver(sim)
        d.assert_enabled(ON.id('cb_read'))
        d.assert_enabled(ON.id('btn_disabled'), expected=False)
        with self.assertRaises(DriverError):
            d.assert_enabled(ON.id('btn_disabled'))

    def test_count_exact_and_zero(self):
        d, _ = self._driver()
        self.assertEqual(1, d.assert_count(ON.id('btn_login'), 1))
        self.assertEqual(0, d.assert_count(ON.id('no_such_thing'), 0))

    def test_count_mismatch_times_out(self):
        d, _ = self._driver()
        with self.assertRaises(DriverError) as ctx:
            d.assert_count(ON.id('btn_login'), 2, timeout=300, interval=50)
        self.assertIn('控件数量应为 2', str(ctx.exception))
        self.assertIn('实际: 1', str(ctx.exception))
        self.assertFalse(d.steps[-1].ok)

    def test_memory_below_pass_returns_sample(self):
        d, sim = self._driver()
        s = d.assert_memory_below(50000)
        self.assertEqual(41322, s['pss_kb'])
        step = d.steps[-1]
        self.assertEqual('assert.memory_below', step.kind)
        self.assertEqual(41322, step.extra['pss_kb'])
        self.assertIn('pss_kb', step.to_dict())

    def test_memory_over_threshold_fails_with_numbers(self):
        d, _ = self._driver()
        with self.assertRaises(DriverError) as ctx:
            d.assert_memory_below(40000)
        self.assertIn('阈值: 40000 KB', str(ctx.exception))
        self.assertIn('实际: 41322 KB', str(ctx.exception))
        self.assertFalse(d.steps[-1].ok)

    def test_missing_sample_never_passes(self):
        """缺样本不判通过 —— 「没采到」绝不能伪装成「采到了且达标」。"""
        d, _ = self._driver(_GarbageHdc())
        with self.assertRaises(DriverError) as ctx:
            d.assert_memory_below(999999)
        self.assertIn('未采到 PSS', str(ctx.exception))
        self.assertFalse(d.steps[-1].ok)

    def test_memory_assert_supports_other_bundle(self):
        d, _ = self._driver()
        s = d.assert_memory_below(50000, bundle='com.other.app')
        self.assertEqual(41322, s['pss_kb'])


if __name__ == '__main__':
    unittest.main()
