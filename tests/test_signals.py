"""信号采集 结果信号采集 —— 单元测试。

**全部测试都不需要真机**，一律由 `FakeHdc` 驱动，在无设备的机器上必须全绿
（任务卡第七章一，这是验收的一部分）。

解析器的开发依据是 `fixtures/signals/` 下的**真机产出样本**，不是自己编的日志：

    cppcrash_real.log        真机崩溃日志（379 KB，SIGSEGV）—— 解析器主依据
    cppcrash_temp.log        temp/ 目录那份（不完整，缺 Module name）
    screen_normal.png        真机正常页面截图（277 KB）—— 白屏检测**负样本**
    layout_normal.json       正常页面控件树（151 节点）
    layout_no_window.json    锁屏/无窗口控件树（仅 1 个零尺寸节点）
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.signals import (                                    # noqa: E402
    Anomaly, CrashRecord, Signals, analyze_screenshot, collect_signals,
    decode_png, is_white_screen, parse_crash_log, parse_fault_filename,
    parse_ls_line, read_png_meta, FAULTLOGGER_DIR, FREEZE_DIR,
    _PNG_BPP_SOLID as _PNG_BPP_SOLID_LIMIT)
from ohauto.sim import FakeHdc, FaultPlan, _write_png           # noqa: E402

FIX = os.path.join(HERE, 'fixtures', 'signals')
CRASH_REAL = os.path.join(FIX, 'cppcrash_real.log')
CRASH_TEMP = os.path.join(FIX, 'cppcrash_temp.log')
SCREEN_NORMAL = os.path.join(FIX, 'screen_normal.png')
LAYOUT_NORMAL = os.path.join(FIX, 'layout_normal.json')
LAYOUT_NO_WINDOW = os.path.join(FIX, 'layout_no_window.json')

# 真机实测的崩溃日志文件名（任务卡第三章一）
CRASH_NAME = 'cppcrash-com.ohos.note-20010019-20260916151446'
BUNDLE = 'com.ohos.note'

#: 模块级临时根目录：out_dir() 造的所有目录都挂它下面，进程退出整体清（评审 P2）
_SIGNALS_TMP_ROOT = tempfile.mkdtemp(prefix='ohauto_signals_test_')
atexit.register(shutil.rmtree, _SIGNALS_TMP_ROOT, True)

#: ★★ 采集窗口的**固定锚点**（样本文件名里的时刻是 2026-09-16 15:14:46）。
#:
#: 凡是拿真机样本做端到端测试的，窗口一律用 `since=CRASH_SAMPLE_SINCE`
#: 而**不要**用 `lookback_s=`。
#:
#: 为什么：`lookback_s` 的语义是「相对**现在**往前推」，
#: 而样本文件名里的时间戳是**写死的**。于是窗口起点会随时间推移
#: 逐渐逼近并越过样本时刻 —— 测试会**自己过期**。
#:
#: 实测踩坑（2026-09-19）：这组测试写于 9/17，用 `lookback_s=3*86400`。
#: 到 9/19 21:59 时窗口起点变成 9/16 21:59，
#: 越过了样本的 9/16 15:14:46 → 3 个测试开始失败。
#: **上午 15:05 还是全绿，晚上就红了** —— CI 里最难查的那类问题。
#: 用固定 `since` 后，窗口永远覆盖样本，与当前时间无关。
CRASH_SAMPLE_SINCE = '2026-09-16 15:00:00'


def read(path: str) -> str:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def out_dir() -> str:
    """产物目录一律用临时目录 —— 测试绝不往项目目录写文件。

    全部挂在进程根目录下，退出时整体清理（评审 P2：只建不删 ×56 调用点）。
    """
    return tempfile.mkdtemp(prefix='cases_', dir=_SIGNALS_TMP_ROOT)


# ================================================================ 1. 解析真机崩溃日志

class TestParseRealCrashLog(unittest.TestCase):
    """解析器的主依据是真机样本，不是编造的日志。"""

    @classmethod
    def setUpClass(cls):
        cls.text = read(CRASH_REAL)
        cls.rec = parse_crash_log(cls.text, raw_path='local.log',
                                  source_file=CRASH_NAME)

    def test_header_fields(self):
        """真机实测的 11 个字段必须全部解析出来（任务卡第三章三）。"""
        r = self.rec
        self.assertEqual(r.module_name, 'com.ohos.note')
        self.assertEqual(r.pid, 9639)
        self.assertEqual(r.uid, 20010019)
        self.assertEqual(r.timestamp, '2026-09-16 15:14:46.000')
        self.assertEqual(r.reason,
                         'Signal:SIGSEGV(SI_USER)@0x000025d9 from:9689:0')
        self.assertIs(r.foreground, True)
        self.assertEqual(r.process_life_time, '6s')

    def test_reason_is_parsed_into_signal(self):
        """`Reason:Signal:...` 是 B4 判「应用缺陷」的核心依据。"""
        self.assertEqual(self.rec.signal, 'SIGSEGV')

    def test_stack_head_is_collected(self):
        """栈顶几帧够 B4 判断崩在系统库还是应用自己的代码。"""
        head = self.rec.stack_head
        self.assertTrue(head, '必须采到调用栈')
        self.assertLessEqual(len(head), 6)
        self.assertTrue(head[0].startswith('#00'))
        self.assertTrue(all(h.startswith('#') for h in head))

    def test_extra_fields_kept_leniently(self):
        """宽松模式：不认识的真机字段也原样留着，不丢证据。"""
        self.assertEqual(self.rec.extra.get('Build info'), 'OpenHarmony 5.0.3.135')
        self.assertIn('Version', self.rec.extra)

    def test_timestamp_epoch_is_device_clock(self):
        """真机 Timestamp 是 2026-09-16 15:14:46，换算出来必须落在那一天。"""
        self.assertIsNotNone(self.rec.timestamp_epoch)
        self.assertEqual(
            time.strftime('%Y-%m-%d', time.localtime(self.rec.timestamp_epoch)),
            '2026-09-16')

    def test_to_dict_is_json_serializable(self):
        json.dumps(self.rec.to_dict(), ensure_ascii=False)


class TestParseTempCrashLog(unittest.TestCase):
    """`temp/` 那份日志不完整 —— 解析器必须能处理缺字段，而不是崩掉。

    （真机上 `temp/` 是**不读**的：写入中的文件可能只读到半截。
    这里只用来验证解析器对「缺字段」的容忍度。）
    """

    @classmethod
    def setUpClass(cls):
        cls.rec = parse_crash_log(
            read(CRASH_TEMP), source_file='cppcrash-9639-1789542886703')

    def test_unknown_module_name_stays_empty_and_is_not_guessed(self):
        """★ temp 文件名里只有 pid，没有 bundle 段。

        这时**不能猜**：`module_name` 留空，把不确定写在明面上，
        让 B4 知道这条证据不能当铁证用。用「看起来合理的默认值」填
        （比如从 Process name 反推）就等于编造了归因依据。
        """
        self.assertEqual(self.rec.module_name, '')
        self.assertEqual(parse_fault_filename(
            'cppcrash-9639-1789542886703').get('bundle'), None)

    def test_missing_foreground_is_none_not_false(self):
        """★ 关键：取不到的字段必须是 None，不能填 False。

        填 False 会把「没采到这个字段」误报成「后台崩溃」—— 那是**结论**，
        不是客观证据，B4 会因此判错。
        """
        self.assertIsNone(self.rec.foreground)
        self.assertEqual(self.rec.reason,
                         'Signal:SIGSEGV(SI_USER)@0x000025d9 from:9689:0')

    def test_reason_and_timestamp_still_parsed(self):
        self.assertEqual(self.rec.signal, 'SIGSEGV')
        self.assertEqual(self.rec.timestamp, '2026-09-16 15:14:46.000')


class TestParseFaultFilename(unittest.TestCase):
    """命名规律（任务卡第三章一）。freeze/ 的格式**未验证**，必须能降级。"""

    def test_faultlogger_name(self):
        got = parse_fault_filename(CRASH_NAME)
        self.assertEqual(got['bundle'], 'com.ohos.note')
        self.assertEqual(got['uid'], 20010019)
        self.assertEqual(got['kind'], 'cppcrash')
        self.assertIsNotNone(got['timestamp_epoch'])

    def test_temp_name_uses_millisecond_epoch(self):
        got = parse_fault_filename('cppcrash-9639-1789542886703')
        self.assertEqual(got['pid'], 9639)
        self.assertEqual(got['timestamp_epoch'], 1789542886.703)

    def test_bundle_with_dashes_is_not_truncated(self):
        """包名本身可能含 '-'；uid/时间戳是行尾锚定的，不会误切。"""
        got = parse_fault_filename('cppcrash-com.example-my-app-100-20260916151446')
        self.assertEqual(got['bundle'], 'com.example-my-app')
        self.assertEqual(got['uid'], 100)

    def test_freeze_name_is_tolerated(self):
        """freeze/ 命名未实测验证 —— 按推测解析，解析不了也要返回 {}。"""
        got = parse_fault_filename('appfreeze-com.ohos.note-20010019-20260916151446')
        self.assertEqual(got['kind'], 'appfreeze')
        self.assertEqual(got['bundle'], 'com.ohos.note')

    def test_unknown_name_returns_empty(self):
        for junk in ('', 'ls: /data/log/faultlog: Permission denied', 'README.md'):
            self.assertEqual(parse_fault_filename(junk), {})


class TestParseLsLine(unittest.TestCase):
    """`ls -l` 是采集窗口的兜底时间来源，格式解析必须宽松。"""

    def test_recent_file_line(self):
        got = parse_ls_line(f'-rw-r----- 1 root log 388096 2026-09-16 15:14 {CRASH_NAME}')
        self.assertEqual(got['name'], CRASH_NAME)
        self.assertEqual(got['size'], 388096)
        self.assertIsNotNone(got['mtime'])

    def test_noise_lines_are_ignored(self):
        for junk in ('total 12', '', 'ls: /data/log: Permission denied'):
            self.assertIsNone(parse_ls_line(junk))


# ================================================================ 2/3/4. 窗口与 bundle 过滤

class TestCollectCrashes(unittest.TestCase):

    def test_injected_crash_is_captured(self):
        """最核心的正向用例：窗口内、bundle 匹配 → 采纳。"""
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(len(sig.crashes), 1)
        self.assertEqual(sig.crashes[0].module_name, BUNDLE)
        self.assertIn('SIGSEGV', sig.crashes[0].reason)

    def test_module_name_mismatch_is_not_adopted(self):
        """★ 别的应用崩溃在同一目录里，绝不能算到我们头上（任务卡第五章三·2）。"""
        sim = FakeHdc()
        sim.inject_crash(bundle='com.ohos.calendar')
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(sig.crashes, [])
        self.assertFalse(sig.has('CRASH') and any(
            a.confidence >= 0.9 for a in sig.of_kind('CRASH')))
        self.assertTrue(any('bundle 过滤' in w for w in sig.warnings),
                        f'过滤掉的其它应用崩溃应当留下说明: {sig.warnings}')

    def test_historical_crash_outside_window_is_not_adopted(self):
        """★ 把几小时前的崩溃当成「刚才发生的」会让归因结果全错。"""
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE, epoch=time.time() - 3 * 3600)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), lookback_s=600)
        self.assertEqual(sig.crashes, [])
        self.assertEqual(sig.faults_in_window, [])

    def test_explicit_since_defines_the_window(self):
        """调用方可以显式给窗口起点（典型：这一步开始执行的时刻）。"""
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE, epoch=time.time() - 120)
        early = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                                since=time.time() - 60)
        late = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                               since=time.time() - 300)
        self.assertEqual(len(early.crashes), 0, '窗口起点之后的才算')
        self.assertEqual(len(late.crashes), 1, '窗口起点之前的也在窗口内')

    def test_window_survives_wrong_device_rtc(self):
        """★ 设备 RTC 实测可能停在几年前。

        窗口判断必须锚在**设备时钟域**：RTC 整体偏移是常量偏移，
        直接拿本机时间比会把所有崩溃都判成「窗口外」。
        """
        skew = -3 * 365 * 24 * 3600          # 设备时钟停在约 3 年前
        sim = FakeHdc(device_time=time.time() + skew)
        sim.inject_crash(bundle=BUNDLE, epoch=time.time() + skew)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(len(sig.crashes), 1, 'RTC 偏移不该让窗口判断失效')
        self.assertEqual(sig.crashes[0].module_name, BUNDLE)

    def test_pre_jump_crash_is_rejected_as_impossibly_new(self):
        """★ 真机实测：RTC 从 2026 回跳到 2017，目录里留着回跳前写的日志。

        那些日志的时间戳（2026）落在设备当前时间（2017）**之后**。只做
        「窗口下界」判断的话，2026 > 2017 会把一天前的旧崩溃当成刚发生的 ——
        这正是真机上发生的事。一个文件不可能在设备当前时间之后才写出来，
        所以「时间戳晚于设备当前时间」是时钟回跳的硬信号，必须剔除。
        """
        real_now = time.time()
        device_now = real_now - 9 * 365 * 24 * 3600      # RTC 卡在 9 年前
        sim = FakeHdc(device_time=device_now)
        # 回跳前写的旧日志：时间戳是本机真实时间，即设备的「未来」
        sim.inject_crash(bundle=BUNDLE, epoch=real_now - 3600)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), lookback_s=600)
        self.assertEqual(sig.crashes, [], '时钟回跳前的旧日志不能冒充本轮证据')
        self.assertEqual(sig.faults_in_window, [])
        self.assertTrue(any('晚于当前设备时间' in w for w in sig.warnings),
                        f'剔除理由要写在明面上: {sig.warnings}')

    def test_crash_written_after_the_rtc_jump_is_still_captured(self):
        """剔除「未来」文件不能误伤：回跳**之后**写的新崩溃必须照常采纳。

        这是同一枚硬币的另一面 —— 没有这一条，上面那条靠「全部丢弃」也能过。
        """
        real_now = time.time()
        device_now = real_now - 9 * 365 * 24 * 3600
        sim = FakeHdc(device_time=device_now)
        sim.inject_crash(bundle=BUNDLE, epoch=device_now)   # 设备域里的「现在」
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), lookback_s=600)
        self.assertEqual(len(sig.crashes), 1, '时钟回跳后写的新崩溃必须采纳')
        self.assertEqual(sig.crashes[0].module_name, BUNDLE)

    def test_timestamp_barely_ahead_of_device_clock_is_tolerated(self):
        """容差窗口内的小幅超前（`date` 与写文件之间的正常抖动）不该被剔除。"""
        sim = FakeHdc(device_time=time.time())
        sim.inject_crash(bundle=BUNDLE, epoch=time.time() + 30)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), lookback_s=600)
        self.assertEqual(len(sig.crashes), 1, '几十秒的超前属于正常抖动')

    def test_pre_jump_freeze_file_is_ignored(self):
        """freeze/ 与 faultlogger/ 是同一个洞，两处都必须堵上。"""
        real_now = time.time()
        device_now = real_now - 9 * 365 * 24 * 3600
        sim = FakeHdc(device_time=device_now)
        sim.inject_freeze(bundle=BUNDLE, epoch=real_now - 60)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), lookback_s=600,
                              collect_screenshot=False, collect_layout=False)
        self.assertFalse([a for a in sig.of_kind('NO_RESPONSE')
                          if a.source == 'faultlog'],
                         '时钟回跳前的 freeze 文件不能当成无响应证据')

    def test_large_clock_skew_is_reported(self):
        """★ 时钟偏差本身要让人看见，否则读者会以为产物里的时间戳写错了。"""
        skew = -9 * 365 * 24 * 3600
        sim = FakeHdc(device_time=time.time() + skew)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertTrue(any('设备时钟与本机相差' in w for w in sig.warnings),
                        f'大偏差应当有说明: {sig.warnings}')

    def test_small_clock_skew_stays_quiet(self):
        """几十秒的正常漂移不该刷警告 —— 否则真警告会被淹没。"""
        sim = FakeHdc(device_time=time.time() - 30)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertFalse([w for w in sig.warnings if '设备时钟与本机相差' in w],
                         f'小漂移不该报偏差: {sig.warnings}')

    def test_empty_faultlogger_returns_empty_gracefully(self):
        """没崩溃是最常见的情况 —— 空目录不该算「异常」，也不该报 warning。"""
        sig = collect_signals(FakeHdc(), BUNDLE, out_dir=out_dir())
        self.assertEqual(sig.crashes, [])
        self.assertFalse([w for w in sig.warnings if 'faultlogger' in w])

    def test_permission_denied_degrades_with_warning(self):
        """★ 非 root 设备读不到 faultlogger/（未验证项）—— 降级且不抛异常。"""
        sim = FakeHdc(faultlog_denied=True)
        sim.inject_crash(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(sig.crashes, [])
        self.assertTrue(any('Permission denied' in w or '降级' in w
                            for w in sig.warnings), sig.warnings)

    def test_crash_log_text_is_persisted_locally(self):
        """验收要求：崩溃日志原文必须落盘为本地文件路径。"""
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        raw = sig.crashes[0].raw_path
        self.assertTrue(raw and os.path.exists(raw), raw)
        self.assertIn('Module name:com.ohos.note', read(raw))


# ================================================================ 5/6. 降级

class TestDegradation(unittest.TestCase):
    """采集是旁路行为：它自己失败绝不能把正在跑的测试搞崩（任务卡第五章三·3）。"""

    def test_hilog_failure_degrades(self):
        sim = FakeHdc(hilog_text='09-16 15:14:47.0 9639 9639 E X: SIGSEGV',
                      faults=FaultPlan().fail_always('hilog', kind='timeout'))
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(sig.hilog_tail, [])
        self.assertTrue(any('hilog' in w for w in sig.warnings), sig.warnings)

    def test_hilog_goes_through_hdc_hilog(self):
        """`hilog` 必须带 `-x`，否则阻塞读会把整条流程挂死（任务卡第三章六）。

        这里的守法是：collect_signals 只能经 `hdc.hilog()` 取日志，
        不能自己拼 shell 命令绕过它（`hdc.py::Hilog()` 已经用对了 `-x -z`）。
        """
        class SpyHdc(FakeHdc):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.hilog_calls = []

            def hilog(self, lines=200, grep=None):
                self.hilog_calls.append(lines)
                return super().hilog(lines=lines, grep=grep)

        sim = SpyHdc()
        collect_signals(sim, BUNDLE, out_dir=out_dir(), collect_layout=False,
                        collect_screenshot=False)
        self.assertTrue(sim.hilog_calls, '必须经 hdc.hilog() 取日志')
        self.assertFalse([c for c in sim.calls if c.startswith('shell:hilog')],
                         '不得绕过 hdc.hilog() 自己拼 hilog 命令')

    def test_everything_fails_but_collect_signals_does_not_raise(self):
        """★ 硬要求：所有设备调用全挂，collect_signals 也必须正常返回。"""
        sim = FakeHdc(faults=FaultPlan().fail_always('any', kind='device'))
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertIsInstance(sig, Signals)
        self.assertEqual(sig.crashes, [])
        self.assertEqual(sig.screenshots, [])
        self.assertTrue(sig.warnings, '每一次降级都要留下说明')

    def test_pull_failure_falls_back_to_cat(self):
        """`file recv` 失败时用 `cat` 兜底 —— 真机上实测这是必要的。"""
        sim = FakeHdc(faults=FaultPlan().fail_always('pull', kind='hdc'))
        sim.inject_crash(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(len(sig.crashes), 1)

    def test_bad_argument_is_the_only_raise(self):
        """唯一允许抛异常的情况是参数错 —— 契约说明里的那句「除参数错外」。"""
        with self.assertRaises(ValueError):
            collect_signals(FakeHdc(), '')
        with self.assertRaises(ValueError):
            collect_signals(FakeHdc(), None)


# ================================================================ 7. 白屏

class TestWhiteScreen(unittest.TestCase):

    def test_real_screenshot_is_a_negative_sample(self):
        """用真机正常页面截图做**负样本** —— 不能误报白屏。"""
        info = analyze_screenshot(SCREEN_NORMAL)
        self.assertEqual((info['width'], info['height']), (720, 1280))
        self.assertTrue(info['decoded'], '真机截图必须能被零依赖解码器读出来')
        suspicious, conf, why = is_white_screen(info)
        self.assertFalse(suspicious, why)
        self.assertEqual(conf, 0.0)
        self.assertLess(info['dominant_ratio'], 0.5,
                        '正常页面的颜色分布是分散的')

    def test_size_heuristic_alone_also_rejects_the_real_screenshot(self):
        """思路 A（体积启发式）在解码不可用时也要能给出正确判断。"""
        suspicious, conf, _ = is_white_screen(analyze_screenshot(
            SCREEN_NORMAL, deep=False))
        self.assertFalse(suspicious)
        self.assertEqual(conf, 0.0)

    def test_solid_screenshot_is_flagged_with_confidence(self):
        """纯色页 → 报白屏，且输出的是**置信度**而不是布尔结论。"""
        path = os.path.join(tempfile.mkdtemp(), 'blank.png')
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        _write_png(path, 720, 1280)                 # sim 的纯色写入器
        info = analyze_screenshot(path)
        suspicious, conf, why = is_white_screen(info)
        self.assertTrue(suspicious, why)
        self.assertGreater(conf, 0.9)
        self.assertLessEqual(conf, 1.0)

    def test_confidences_are_between_0_and_1(self):
        for p in (SCREEN_NORMAL,):
            _, conf, _ = is_white_screen(analyze_screenshot(p))
            self.assertGreaterEqual(conf, 0.0)
            self.assertLessEqual(conf, 1.0)

    def test_unreadable_screenshot_degrades(self):
        """不是 PNG / 文件不存在 → 判据跳过，不抛异常。"""
        info = analyze_screenshot(os.path.join(FIX, 'not-a-real-file.png'))
        suspicious, conf, _ = is_white_screen(info)
        self.assertFalse(suspicious)
        self.assertEqual(conf, 0.0)
        self.assertIsNone(decode_png(os.path.join(FIX, 'cppcrash_real.log')))

    def test_png_decoder_round_trips_the_sim_writer(self):
        """零依赖解码器是 sim 写入器的逆运算，先自己对自己验证一遍。"""
        path = os.path.join(tempfile.mkdtemp(), 'rt.png')
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        _write_png(path, 40, 20, rgb=(10, 20, 30))
        self.assertEqual(read_png_meta(path)[:2], (40, 20))
        w, h, ch, px = decode_png(path)
        self.assertEqual((w, h, ch), (40, 20, 3))
        self.assertEqual(px[:3], bytes((10, 20, 30)))
        self.assertEqual(len(set(px[i:i + 3] for i in range(0, len(px), 3))), 1)

    def test_white_screen_signal_reaches_anomalies(self):
        """端到端：白屏必须作为一条 Anomaly 出现在 Signals 里，带来源与置信度。

        纯色图**必须显式**要（`screen_style='solid'`），不能再依赖默认值 ——
        默认值已改为 `'content'`（正常内容页），见下面的回归测试。

        source 是 `screenshot+layout`（2026-09-23 起）—— 判据现在会交叉验证
        控件树节点数，且把结论落在 evidence 里。本用例显式关掉了
        `collect_layout`，所以 evidence 会注明「未能取到控件树（无法交叉验证）」。
        """
        sim = FakeHdc(screen=(720, 1280), screen_style='solid')
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_layout=False)
        self.assertTrue(sig.has('WHITE_SCREEN'), sig.anomalies)
        a = sig.of_kind('WHITE_SCREEN')[0]
        self.assertIn('screenshot', a.source)
        self.assertGreater(a.confidence, 0.0)

    def test_white_screen_is_downgraded_when_layout_contradicts(self):
        """★ 白屏判据必须被控件树**交叉验证**（2026-09-23 新增）。

        单一颜色占比高 + 控件树有 20 个节点 = 矛盾信号。原实现只看前者，
        以 0.95 置信度一票否决；现在降级为「疑似」，并**把节点数写进 evidence**。

        单测直接打 `_judge_white_screen`（不走设备）—— 这条判据的输入只有
        「原始判据 + 节点数」两个东西，不必绕一圈 FakeHdc。
        """
        import ohauto.signals as S

        def _run(nodes):
            sig = S.Signals(bundle=BUNDLE)
            sig.layout_nodes = nodes
            setattr(sig, '_white_screen_raw',
                    (True, 0.98, '截图像素 99.0% 为同一颜色'))
            S._judge_white_screen(sig)
            return sig.of_kind('WHITE_SCREEN')[0]

        # ① 节点多 → 与「空白页」矛盾 → 降级
        a = _run(20)
        ev = ' '.join(a.evidence) if isinstance(a.evidence, list) else str(a.evidence)
        self.assertIn('20', ev, f'节点数必须写进 evidence：{ev}')
        self.assertIn('矛盾', ev)
        self.assertLess(a.confidence, 0.95, '与控件树矛盾时不许一票否决')
        self.assertIn('layout', a.source)

        # ② 节点少 → 与空白页一致 → 维持原判
        b = _run(1)
        self.assertGreaterEqual(b.confidence, 0.9)
        ev_b = ' '.join(b.evidence) if isinstance(b.evidence, list) else str(b.evidence)
        self.assertIn('一致', ev_b)

        # ③ 没采到控件树 → 不谎报，如实写「无法交叉验证」
        c = _run(None)
        ev_c = ' '.join(c.evidence) if isinstance(c.evidence, list) else str(c.evidence)
        self.assertIn('无法交叉验证', ev_c)
        self.assertGreaterEqual(c.confidence, 0.9)

    def test_normal_screenshot_produces_no_white_screen_signal(self):
        sim = FakeHdc(screen=(720, 1280), screen_style='content')
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_layout=False)
        self.assertFalse(sig.has('WHITE_SCREEN'), sig.anomalies)

    def test_default_sim_does_not_fabricate_white_screen(self):
        """★ 回归护栏：**默认路径 + 干净场景，不许报出任何异常**。

        这是修 P1 时补的测试。此前的缺陷是：`FakeHdc()` 默认写纯色截图，
        于是「什么都没发生」的干净场景也被判成 WHITE_SCREEN 0.99，
        而 `examples/collect_signals.py --sim`（文档推荐的离线路径）正是走默认值。
        B4 一旦消费 `sig.has('WHITE_SCREEN')`，就会把正常执行判成「应用缺陷：白屏」。

        测的是「默认值对不对」，而不是「判据分支对不对」——
        判据的真/假两个分支上面两个测试已经覆盖了，但它们挡不住默认值踩坑。
        """
        sig = collect_signals(FakeHdc(), BUNDLE, out_dir=out_dir(),
                              collect_layout=False)
        self.assertEqual(
            sig.anomalies, [],
            f'默认 FakeHdc 的干净场景不该有任何异常，实得: {sig.anomalies}')
        self.assertFalse(sig.has('WHITE_SCREEN'))

    def test_default_screen_style_is_content(self):
        """默认截图风格必须是 'content'（模拟真机正常内容页），显式钉死这个约定。"""
        self.assertEqual(FakeHdc().screen_style, 'content')

    def test_screen_style_does_not_collide_on_temp_file(self):
        """两种 style 的实例不能共用同一个 temp 文件，否则互相覆盖、读到别人的内容。"""
        solid = FakeHdc(screen=(720, 1280), screen_style='solid')
        content = FakeHdc(screen=(720, 1280), screen_style='content')
        p1 = solid.screen_cap()
        p2 = content.screen_cap()
        self.assertNotEqual(
            solid._files[p1], content._files[p2],
            '同一页面下两种 style 的截图字节不该相同 —— temp 文件被复用了')


# ================================================================ 8. 无窗口 / 无响应

class TestNoWindow(unittest.TestCase):

    def test_locked_device_is_reported_as_no_window(self):
        """锁屏时 dumpLayout 只返回 1 个零尺寸节点 —— 这是客观判据。"""
        sig = collect_signals(FakeHdc(locked=True), BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False)
        self.assertTrue(sig.has('NO_WINDOW'), sig.anomalies)
        self.assertEqual(sig.layout_nodes, 1)
        a = sig.of_kind('NO_WINDOW')[0]
        self.assertEqual(a.source, 'layout')
        self.assertGreater(a.confidence, 0.5)

    def test_normal_page_is_not_no_window(self):
        sig = collect_signals(FakeHdc(), BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False)
        self.assertFalse(sig.has('NO_WINDOW'), sig.anomalies)
        self.assertGreater(sig.layout_nodes, 1)

    def test_no_window_fixture_parses_to_a_single_node(self):
        """用真机锁屏样本核对判据本身（377 字节，仅 1 个节点）。"""
        from ohauto.layout import parse_layout
        root = parse_layout(read(LAYOUT_NO_WINDOW))
        self.assertEqual(sum(1 for _ in root.walk()), 1)
        self.assertEqual(root.rect.area, 0)

    def test_normal_layout_fixture_is_multi_node(self):
        from ohauto.layout import parse_layout
        root = parse_layout(read(LAYOUT_NORMAL))
        self.assertGreater(sum(1 for _ in root.walk()), 1)


class TestNoResponse(unittest.TestCase):
    """任务卡明确要求：多信号叠加，每信号标注来源与置信度，不要单信号硬判。"""

    def test_freeze_file_is_a_high_confidence_signal(self):
        """★ freeze/ 出现窗口内文件 —— 客观判据（尽管命名格式未实测验证）。"""
        sim = FakeHdc()
        sim.inject_freeze(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False)
        hits = sig.of_kind('NO_RESPONSE')
        self.assertTrue(hits, sig.anomalies)
        self.assertTrue(any(h.source == 'faultlog' and h.confidence >= 0.8
                            for h in hits), hits)

    def test_historical_freeze_file_is_ignored(self):
        sim = FakeHdc()
        sim.inject_freeze(bundle=BUNDLE, epoch=time.time() - 4 * 3600)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False, lookback_s=600)
        self.assertFalse([h for h in sig.of_kind('NO_RESPONSE')
                          if h.source == 'faultlog'])

    def test_hilog_keywords_add_a_medium_confidence_signal(self):
        sim = FakeHdc(hilog_text='\n'.join([
            '09-16 15:14:47.000  9639  9639 E C01317/AppKit: THREAD_BLOCK app freeze detected',
            '09-16 15:14:47.100  9639  9639 I C01317/AppKit: normal line',
        ]))
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False)
        hilog_hits = [h for h in sig.of_kind('NO_RESPONSE') if h.source == 'hilog']
        self.assertEqual(len(hilog_hits), 1)
        self.assertLess(hilog_hits[0].confidence, 0.8,
                        '关键字是启发式，只能给中等置信度')

    def test_signals_are_not_single_signal_hard_verdicts(self):
        """多信号叠加：置信度是合成的，且每条证据各自独立成 Anomaly。"""
        sim = FakeHdc(hilog_text='09-16 15:14:47.000 9639 9639 E X: ANR in com.ohos.note')
        sim.inject_freeze(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False)
        kinds = [h.source for h in sig.of_kind('NO_RESPONSE')]
        self.assertIn('faultlog', kinds)
        self.assertIn('hilog', kinds)
        both = sig.anomaly_score('NO_RESPONSE')
        single = max(h.confidence for h in sig.of_kind('NO_RESPONSE'))
        self.assertGreater(both, single, '多条独立证据叠加应当比单条更可信')
        self.assertLessEqual(both, 0.99)

    def test_anomaly_score_of_absent_kind_is_zero(self):
        self.assertEqual(Signals(bundle=BUNDLE).anomaly_score('CRASH'), 0.0)

    def test_stability_probe_is_opt_in(self):
        """默认只探一轮 —— 不额外付出设备往返。"""
        sim = FakeHdc()
        collect_signals(sim, BUNDLE, out_dir=out_dir(), collect_screenshot=False)
        self.assertEqual(
            len([c for c in sim.calls if c == 'dumpLayout']), 2,
            '默认：自己的 1 次 + DeviceGuard.has_window 的 1 次')

    def test_stability_probe_reports_repeated_identical_trees(self):
        sim = FakeHdc(frozen=True)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False, probe_rounds=3)
        hits = [h for h in sig.of_kind('NO_RESPONSE') if h.source == 'layout']
        self.assertTrue(hits, sig.anomalies)
        self.assertLessEqual(hits[0].confidence, 0.5,
                             '这条判据很弱，置信度必须压低')


# ================================================================ 进程存活与 hilog

class TestProcessAndHilog(unittest.TestCase):

    def test_dead_process_is_objective_evidence(self):
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertIs(sig.process_alive, False)
        self.assertTrue(any('pidof' in a.evidence for a in sig.of_kind('CRASH')))

    def test_alive_process_is_not_a_crash_signal(self):
        sig = collect_signals(FakeHdc(), BUNDLE, out_dir=out_dir())
        self.assertIs(sig.process_alive, True)
        self.assertFalse(sig.has('CRASH'), sig.anomalies)

    def test_hilog_tail_is_filtered_by_keywords(self):
        sim = FakeHdc(hilog_text='\n'.join([
            '09-16 15:14:41.820  9639  9639 E C05a05/SecComp: nothing interesting',
            '09-16 15:14:47.777  9639  9639 I C02d11/DfxSignalHandler: sig(11), pid(9639)',
            '09-16 15:14:47.800  9639  9639 I C01317/AppKit: ordinary line',
        ]))
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(len(sig.hilog_tail), 1)
        self.assertIn('DfxSignalHandler', sig.hilog_tail[0])

    def test_hilog_grep_can_be_overridden(self):
        sim = FakeHdc(hilog_text='alpha\nbeta\ngamma')
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), hilog_grep='beta')
        self.assertEqual(sig.hilog_tail, ['beta'])

    def test_invalid_hilog_grep_degrades_to_builtin(self):
        sim = FakeHdc(hilog_text='09-16 15:14:47.0 9639 9639 E X: ANR happened')
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), hilog_grep='[unclosed')
        self.assertTrue(any('正则无效' in w for w in sig.warnings), sig.warnings)
        self.assertEqual(len(sig.hilog_tail), 1)


# ================================================================ 9. 序列化 / 契约

class TestSignalsContract(unittest.TestCase):

    def test_to_dict_matches_the_documented_field_list(self):
        """契约字段一个都不能少 —— B4 要按它写归因规则。"""
        sig = collect_signals(FakeHdc(), BUNDLE, out_dir=out_dir())
        d = sig.to_dict()
        for key in ('bundle', 'captured_at', 'device_time', 'hilog_tail',
                    'crashes', 'anomalies', 'screenshots', 'layout_path',
                    'warnings'):
            self.assertIn(key, d)
        self.assertEqual(d['bundle'], BUNDLE)

    def test_to_dict_is_json_serializable(self):
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        text = json.dumps(sig.to_dict(), ensure_ascii=False)
        self.assertIn('com.ohos.note', text)

    def test_to_json_writes_a_readable_file(self):
        sig = collect_signals(FakeHdc(), BUNDLE, out_dir=out_dir())
        path = os.path.join(out_dir(), 'signals.json')
        sig.to_json(path)
        with open(path, 'r', encoding='utf-8') as f:
            self.assertEqual(json.load(f)['bundle'], BUNDLE)

    def test_both_clocks_are_recorded(self):
        """设备 RTC 可能不准 —— 本机时间与设备时间都要记（任务卡第六章三·5）。"""
        sig = collect_signals(FakeHdc(), BUNDLE, out_dir=out_dir())
        self.assertTrue(sig.captured_at)
        self.assertTrue(sig.device_time)
        self.assertNotEqual(sig.captured_at, sig.device_time)   # 格式不同

    def test_anomaly_and_crash_to_dict(self):
        a = Anomaly(kind='CRASH', evidence='e', source='faultlog', confidence=0.5)
        self.assertEqual(a.to_dict()['confidence'], 0.5)
        self.assertEqual(CrashRecord(module_name='x').to_dict()['module_name'], 'x')

    def test_output_is_not_written_to_cwd(self):
        """产物绝不写进程当前目录（项目里为这条踩过坑）。"""
        before = set(os.listdir(os.getcwd()))
        collect_signals(FakeHdc(), BUNDLE)          # 不传 out_dir
        leaked = {f for f in set(os.listdir(os.getcwd())) - before
                  if 'ohauto' in f or 'signals' in f or f.endswith('.log')}
        self.assertEqual(leaked, set(), f'采集把文件写进了当前目录: {leaked}')

    def test_helpers_on_signals(self):
        sig = Signals(bundle=BUNDLE)
        sig.anomalies.append(Anomaly('CRASH', 'e', 'faultlog', 1.0))
        self.assertTrue(sig.has('CRASH'))
        self.assertFalse(sig.has('WHITE_SCREEN'))
        self.assertEqual(len(sig.of_kind('CRASH')), 1)


# ================================================================ 10. 端到端（验收标准）

class TestEndToEndCrashCapture(unittest.TestCase):
    """★ 验收标准本身：**注入一次崩溃能被捕获**。

    真机上的对应操作是 `kill -11 <pid>`（任务卡第三章四）。这里用
    `FakeHdc.inject_crash()` 做同一件事，整条链路无真机可跑。
    """

    def test_injected_crash_is_fully_captured(self):
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE)

        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())

        # 1. 返回一条 CrashRecord
        self.assertEqual(len(sig.crashes), 1)
        rec = sig.crashes[0]

        # 2. module_name 等于被测应用的 bundle
        self.assertEqual(rec.module_name, BUNDLE)

        # 3. 能解析出崩溃原因与崩溃时刻
        self.assertIn('SIGSEGV', rec.reason)
        self.assertEqual(rec.signal, 'SIGSEGV')
        self.assertTrue(rec.timestamp)
        self.assertIsNotNone(rec.timestamp_epoch)

        # 4. 日志原文与截图都落盘为本地路径
        self.assertTrue(os.path.exists(rec.raw_path))
        self.assertTrue(sig.screenshots and os.path.exists(sig.screenshots[0]))
        self.assertTrue(sig.layout_path and os.path.exists(sig.layout_path))

        # 5. 异常列表里有 CRASH，且带来源与置信度
        crash_anoms = sig.of_kind('CRASH')
        self.assertTrue(crash_anoms)
        self.assertTrue(any(a.source == 'faultlog' and a.confidence >= 0.9
                            for a in crash_anoms))

    def test_real_device_crash_log_captured_end_to_end(self):
        """同一链路，但喂进去的是**真机产出的那份 379 KB 日志原文**。"""
        sim = FakeHdc()
        # 这份样本的文件名里写着 2026-09-16 15:14:46（真机采集时刻），
        # 所以窗口要开得能覆盖到它 —— 顺带验证窗口判断确实按文件名时间戳在工作。
        sim.add_fault_log({'text': read(CRASH_REAL), 'name': CRASH_NAME,
                           'bundle': BUNDLE, 'epoch': time.time()})
        sim.dead_bundles.add(BUNDLE)

        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              since=CRASH_SAMPLE_SINCE)

        self.assertEqual(len(sig.crashes), 1)
        rec = sig.crashes[0]
        self.assertEqual(rec.module_name, 'com.ohos.note')
        self.assertEqual(rec.pid, 9639)
        self.assertEqual(rec.uid, 20010019)
        self.assertEqual(rec.timestamp, '2026-09-16 15:14:46.000')
        self.assertEqual(rec.signal, 'SIGSEGV')
        self.assertIs(rec.foreground, True)
        self.assertTrue(rec.stack_head)
        # 原文真的落盘了，而且和真机样本字节一致
        with open(rec.raw_path, 'r', encoding='utf-8', errors='replace') as f:
            self.assertEqual(f.read(), read(CRASH_REAL))

    def test_crash_of_another_app_in_the_same_directory_is_ignored(self):
        """真机现场：同一目录里同时有别的应用崩溃 —— 只认自己那一条。"""
        sim = FakeHdc()
        sim.add_fault_log({'text': read(CRASH_REAL), 'name': CRASH_NAME,
                           'bundle': BUNDLE, 'epoch': time.time()})
        sim.inject_crash(bundle='com.ohos.calendar', epoch=time.time())
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              since=CRASH_SAMPLE_SINCE)
        self.assertEqual(len(sig.crashes), 1)
        self.assertEqual(sig.crashes[0].module_name, BUNDLE)
        self.assertEqual(len(sig.faults_in_window), 2,
                         '两条都在窗口内，但只有一条被采纳')

    def test_collection_is_read_only(self):
        """采集是**旁路**行为：不许点击、不许改应用状态（任务卡第二章四）。"""
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE)
        before = sim.current
        collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(sim.current, before, '采集不该改变设备页面状态')
        self.assertEqual([a for a in sim.actions if a.get('action') == 'click'], [])

    def test_faultlogger_dir_constant_matches_real_device(self):
        """路径写错就什么都采不到 —— 直接对着真机实测值断言。"""
        self.assertEqual(FAULTLOGGER_DIR, '/data/log/faultlog/faultlogger')
        self.assertEqual(FREEZE_DIR, '/data/log/faultlog/freeze')


# ================================================================ 降级支路

def _png(path: str, w: int, h: int, *, color_type: int = 2, depth: int = 8,
         rows: bytes = None, extra_ihdr: bytes = b'', filters: bytes = None,
         palette: bytes = None) -> str:
    """手工拼一张（可能**畸形**的）PNG，用来验证解码器的降级路径。"""
    import struct
    import zlib as _z

    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 1)
    if rows is None:
        rows = b''.join(b'\x00' + bytes([7, 8, 9] * w) for _ in range(h))
    if filters is not None:
        rows = filters

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', _z.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', w, h, depth, color_type, 0, 0, 0) + extra_ihdr
    body = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
    if palette is not None:
        body += chunk(b'PLTE', palette)
    body += chunk(b'IDAT', _z.compress(rows)) + chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(body)
    return path


class TestPngDecoderDegradation(unittest.TestCase):
    """畸形 / 不支持的 PNG 一律返回 None 让调用方降级 —— **绝不抛异常**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_bad_signature(self):
        p = self.path('bad_sig.png')
        with open(p, 'wb') as f:
            f.write(b'not a png at all')
        self.assertIsNone(decode_png(p))
        self.assertIsNone(read_png_meta(p))

    def test_16_bit_depth_is_not_supported(self):
        p = _png(self.path('depth16.png'), 4, 4, depth=16)
        self.assertIsNone(decode_png(p))
        self.assertEqual(read_png_meta(p)[2], 16)      # 头还是读得出来的

    def test_unknown_color_type(self):
        p = _png(self.path('ctype7.png'), 4, 4, color_type=7)
        self.assertIsNone(decode_png(p))

    def test_unknown_filter_type(self):
        rows = b''.join(b'\x09' + bytes([1, 2, 3] * 4) for _ in range(4))
        p = _png(self.path('badfilter.png'), 4, 4, rows=rows, filters=rows)
        self.assertIsNone(decode_png(p))

    def test_truncated_idat(self):
        """文件被截断（例如读到了 temp/ 里正在写入的那份）。"""
        p = _png(self.path('trunc.png'), 8, 8)
        with open(p, 'rb') as f:
            blob = f.read()
        with open(p, 'wb') as f:
            f.write(blob[:len(blob) // 2])
        self.assertIsNone(decode_png(p))

    def test_indexed_png_without_palette_is_rejected(self):
        self.assertIsNone(decode_png(_png(self.path('nopal.png'), 4, 4,
                                          color_type=3)))

    def test_indexed_png_with_palette_decodes_to_rgb(self):
        pal = bytes([0, 0, 0, 255, 255, 255])
        rows = b''.join(b'\x00' + bytes([0, 1] * 2) for _ in range(4))
        p = _png(self.path('pal.png'), 4, 4, color_type=3, rows=rows, palette=pal)
        w, h, ch, px = decode_png(p)
        self.assertEqual((w, h, ch), (4, 4, 3))
        self.assertEqual(px[:3], bytes((0, 0, 0)))
        self.assertEqual(px[3:6], bytes((255, 255, 255)))

    def test_grayscale_and_rgba_are_supported(self):
        for ctype, ch in ((0, 1), (4, 2), (6, 4)):
            p = _png(self.path(f'ct{ctype}.png'), 3, 3, color_type=ctype,
                     rows=b''.join(b'\x00' + bytes([9] * (3 * ch))
                                   for _ in range(3)))
            got = decode_png(p)
            self.assertIsNotNone(got, f'color_type={ctype} 应当支持')
            self.assertEqual(got[2], ch)

    def test_non_png_file_degrades(self):
        """真机日志不是 PNG —— 拿它当输入不能崩。"""
        self.assertIsNone(decode_png(CRASH_REAL))
        info = analyze_screenshot(CRASH_REAL)
        self.assertFalse(info['decoded'])
        self.assertFalse(is_white_screen(info)[0])


class TestWhiteScreenSizeTiers(unittest.TestCase):
    """思路 A（体积启发式）：解码不可用时也要分级给出置信度。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_very_small_bytes_per_pixel_is_flagged(self):
        p = os.path.join(self.tmp, 'solid_big.png')
        _write_png(p, 720, 1280)
        suspicious, conf, _ = is_white_screen(
            analyze_screenshot(p, deep=False))
        self.assertTrue(suspicious)
        self.assertEqual(conf, 0.8, '解码不可用时给固定档置信度')

    def test_medium_bytes_per_pixel_is_graded(self):
        p = os.path.join(self.tmp, 'solid_small.png')
        _write_png(p, 100, 100)                 # 小图 → 每像素字节数落在中间区
        info = analyze_screenshot(p, deep=False)
        self.assertLess(_PNG_BPP_SOLID_LIMIT, info['bytes_per_pixel'])
        self.assertLess(info['bytes_per_pixel'], 0.06)
        suspicious, conf, _ = is_white_screen(info)
        self.assertTrue(suspicious)
        self.assertGreater(conf, 0.0)
        self.assertLess(conf, 0.8, '中间区应当是插值出来的中等置信度')

    def test_normal_bytes_per_pixel_is_dismissed(self):
        suspicious, conf, _ = is_white_screen(
            analyze_screenshot(SCREEN_NORMAL, deep=False))
        self.assertFalse(suspicious)
        self.assertEqual(conf, 0.0)


class TestClockAndWindowParsing(unittest.TestCase):

    def test_since_accepts_datetime_and_iso_string(self):
        from datetime import datetime, timedelta
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE)
        now = datetime.now()

        r1 = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                             since=now - timedelta(minutes=5))
        r2 = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                             since=(now - timedelta(minutes=5)).isoformat())
        r3 = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                             since=now + timedelta(minutes=5))
        self.assertEqual(len(r1.crashes), 1)
        self.assertEqual(len(r2.crashes), 1)
        self.assertEqual(len(r3.crashes), 0)

    def test_garbage_since_falls_back_to_now(self):
        """解析不了的 since 退回「以当前时刻为起点」——不抛异常。"""
        sim = FakeHdc()
        sim.inject_crash(bundle=BUNDLE, epoch=time.time() - 3600)
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), since='不是时间')
        self.assertEqual(sig.crashes, [])

    def test_non_epoch_device_clock_degrades_with_warning(self):
        """设备 `date +%s` 不是数字 → 窗口判断退化到本机时间并记 warning。"""
        from ohauto.hdc import HdcError

        class OddClockHdc(FakeHdc):
            def shell(self, cmd, **kw):
                if cmd.startswith('date +%s'):
                    return self._R('not-a-number')
                if cmd.startswith('date'):
                    raise HdcError('device clock unavailable')
                return super().shell(cmd, **kw)

        sig = collect_signals(OddClockHdc(), BUNDLE, out_dir=out_dir())
        self.assertTrue(any('设备时钟' in w for w in sig.warnings), sig.warnings)
        self.assertEqual(sig.device_time, '')

    def test_mtime_is_the_timestamp_fallback(self):
        """文件名解析不出时间戳时，退回 `ls -l` 的 mtime。"""
        from ohauto.signals import _file_time_epoch
        when, how = _file_time_epoch(
            {'name': 'not-a-crash-name.txt', 'mtime': 1234567890.0})
        self.assertEqual(when, 1234567890.0)
        self.assertEqual(how, 'mtime')
        self.assertEqual(_file_time_epoch({'name': 'x.txt'}), (None, ''))

    def test_ls_line_with_year(self):
        got = parse_ls_line('-rw-r--r-- 1 root root 1024 2026-01-02 10:11 old.log')
        self.assertEqual(got['name'], 'old.log')
        self.assertEqual(got['size'], 1024)
        self.assertIsNotNone(got['mtime'])

    def test_file_line_without_a_date_keeps_the_name(self):
        """取不到时间也不能把这一行丢掉 —— 文件名本身就是证据。

        时间戳留 None，由 `_file_time_epoch` 决定怎么兜底（文件名 → mtime
        → 保守采纳并记 warning）。
        """
        got = parse_ls_line('-rw-r--r-- 1 root root 1024 weirdname')
        self.assertEqual(got['name'], 'weirdname')
        self.assertIsNone(got['mtime'])

    def test_directory_without_date_column_is_tolerated(self):
        got = parse_ls_line('drwxr-x--- 2 root root 4096 faultlogger')
        self.assertEqual(got['name'], 'faultlogger')
        self.assertIsNone(got['mtime'])


class TestMoreDegradation(unittest.TestCase):

    def test_unreadable_crash_log_still_yields_a_record(self):
        """★ 读不到日志原文时，**文件名本身就是客观证据**，不能整条丢掉。"""
        sim = FakeHdc(faults=FaultPlan().fail_always('pull', kind='hdc'))
        sim.inject_crash(bundle=BUNDLE)
        # 让 cat 也失效：pull 与 cat 全断，只剩文件名
        original = sim.shell

        def no_cat(cmd, **kw):
            if cmd.startswith('cat /data/log/faultlog/faultlogger'):
                return sim._R('', 1, 'Permission denied')
            return original(cmd, **kw)

        sim.shell = no_cat
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir())
        self.assertEqual(len(sig.crashes), 1)
        rec = sig.crashes[0]
        self.assertEqual(rec.module_name, BUNDLE)      # 来自文件名
        self.assertEqual(rec.raw_path, '')
        self.assertIsNotNone(rec.timestamp_epoch)
        self.assertTrue(any('读取失败' in w for w in sig.warnings), sig.warnings)

    def test_module_name_from_content_also_filters(self):
        """文件名段匹配、但正文 `Module name` 是别的应用 —— 同样不采纳。"""
        sim = FakeHdc()
        sim.add_fault_log({'text': read(CRASH_REAL).replace(
            'Module name:com.ohos.note', 'Module name:com.ohos.other'),
            'name': f'cppcrash-{BUNDLE}-20010019-20260916151446',
            'bundle': BUNDLE, 'epoch': time.time()})
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              since=CRASH_SAMPLE_SINCE)
        self.assertEqual(sig.crashes, [])
        self.assertTrue(any('bundle 过滤' in w for w in sig.warnings), sig.warnings)

    def test_screenshot_pull_failure_does_not_cat_fallback(self):
        """截图（二进制）拉取失败时**不许**用 cat 兜底 —— 宁可如实报失败。

        ★ 语义变更（2026-09-26 评审）：本测试原名
        `test_screenshot_pull_failure_falls_back_to_cat`，锁定的恰恰是缺陷行为 ——
        `hdc shell cat` 对二进制做 CRLF 转换（PNG 头被写成 \\x89PNG\\r\\r\\n），
        产出**损坏但不报错**的文件：白屏判据读到坏 PNG，看起来像「应用白屏」，
        实为工具损坏（hdc.py「坑 3」实测约束）。

        新语义：`_read_device_file(binary=True)` 禁用 cat 兜底，截图丢失 +
        warning 留痕 —— 丢了能看见，坏了看不出来，前者诚实得多。
        """
        sim = FakeHdc(faults=FaultPlan().fail_always('pull', kind='hdc'))
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), collect_layout=False)
        self.assertEqual(len(sig.screenshots), 0, '二进制禁用 cat 兜底后截图应如实丢失')
        self.assertTrue(any('禁用 cat 兜底' in w for w in sig.warnings),
                        sig.warnings)

    def test_unrecoverable_screenshot_is_skipped(self):
        """两条取回路径都断了才跳过 —— 而且只记 warning，不影响其它采集。"""
        sim = FakeHdc(faults=FaultPlan().fail_always('pull', kind='hdc'))
        original = sim.shell

        def no_cat(cmd, **kw):
            if cmd.startswith('cat ') and 'ohauto_shot' in cmd:
                return sim._R('', 1, 'Permission denied')
            return original(cmd, **kw)

        sim.shell = no_cat
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(), collect_layout=False)
        self.assertEqual(sig.screenshots, [])
        self.assertTrue(any('截图' in w for w in sig.warnings), sig.warnings)

    def test_empty_hilog_is_reported(self):
        """hilog 缓冲区被冲掉是真实会发生的事 —— 要看得见。"""
        sim = FakeHdc(hilog_text='')
        sig = collect_signals(sim, BUNDLE, out_dir=out_dir(),
                              collect_layout=False, collect_screenshot=False)
        self.assertEqual(sig.hilog_tail, [])
        self.assertTrue(any('hilog' in w for w in sig.warnings), sig.warnings)

    def test_unknown_fault_format_still_parsed_leniently(self):
        """jscrash 等未验证格式：字段不认识也要能采下来，不能崩。"""
        weird = ('Module name:com.ohos.note\n'
                 'Timestamp:2026-09-16 15:14:46.000\n'
                 'Reason:Js Crash: TypeError at entry/src/main.ets:42\n'
                 'Some New Field:whatever\n')
        rec = parse_crash_log(weird, source_file='jscrash-com.ohos.note-1-20260916151446')
        self.assertEqual(rec.module_name, 'com.ohos.note')
        self.assertEqual(rec.signal, '')               # 不是 Signal: 形态，不硬猜
        self.assertEqual(rec.extra.get('Some New Field'), 'whatever')

    def test_in_foreground_helper(self):
        self.assertTrue(CrashRecord(foreground=True).in_foreground)
        self.assertTrue(CrashRecord(foreground=None).in_foreground)
        self.assertFalse(CrashRecord(foreground=False).in_foreground)

    def test_layout_parse_failure_degrades(self):
        """控件树是垃圾数据时，无窗口判据必须降级而不是抛异常。"""
        class GarbageLayout(FakeHdc):
            def dump_layout(self, device_path=None, **kw):
                p = device_path or '/data/local/tmp/ohauto_layout.json'
                self._files[p] = b'<<<not json at all>>>'
                return p

        sig = collect_signals(GarbageLayout(), BUNDLE, out_dir=out_dir(),
                              collect_screenshot=False)
        self.assertIsNone(sig.layout_nodes)
        self.assertTrue(sig.warnings, '解析失败必须留下说明')


# ================================================================ FakeHdc 扩展不回归

class TestSimExtensionDoesNotChangeExistingBehaviour(unittest.TestCase):
    """sim.py 只许**新增**能力，不许改已有方法的行为（本版的测试依赖它们）。"""

    def test_default_sim_still_behaves_as_before(self):
        sim = FakeHdc()
        self.assertEqual(sim.list_targets(), ['simulator'])
        self.assertTrue(sim.is_running('com.example.app'))
        self.assertIn('simulated hilog', sim.hilog())
        self.assertEqual(sim.shell('echo hello').stdout, 'hello')
        self.assertEqual(sim.fault_dir_names('faultlogger'), [])

    def test_faultlog_commands_do_not_affect_other_paths(self):
        """faultlog 的处理只对 /data/log/faultlog 生效，别的路径行为不变。"""
        sim = FakeHdc()
        self.assertIsNone(sim._ls_fault_dir('/data/local/tmp'))
        self.assertEqual(sim.shell('ls /data/local/tmp/whatever').stdout, '')

    def test_screen_cap_default_is_unchanged(self):
        sim = FakeHdc()
        p = sim.screen_cap()
        self.assertEqual(p, '/data/local/tmp/ohauto_shot.png')
        self.assertIn(p, sim._files)


class TestParseLsLineShortDate(unittest.TestCase):
    """评审 P2：toybox 对近期文件只给 `MM-DD HH:MM`，此前代码静默返回
    size=0 / mtime=None，而 docstring 谎称做了「本设备年同一天」近似。
    现在代码真的做：size 照解析；年份补本机年；补出的未来时刻回退一年。
    """

    def test_short_date_line_keeps_size_and_mtime(self):
        got = parse_ls_line(
            '-rw-r----- 1 root log 388096 09-16 15:14 cppcrash-x.log')
        self.assertEqual(got['size'], 388096,
                         'MM-DD 行的 size 不该被丢成 0')
        self.assertIsNotNone(got['mtime'], 'MM-DD 行该有近似 mtime')
        # 近似 mtime 应落在最近一年内
        self.assertGreater(got['mtime'], 0)
        self.assertLess(got['mtime'] - time.time(), 370 * 86400)

    def test_full_date_line_unchanged(self):
        got = parse_ls_line(
            '-rw-r----- 1 root log 388096 2026-09-16 15:14 cppcrash-x.log')
        self.assertEqual(got['size'], 388096)
        self.assertEqual(got['mtime'],
                         parse_ls_line(
                             '-rw-r----- 1 root log 1 2026-09-16 15:14 y.log'
                         )['mtime'])

    def test_future_approximation_rolls_back_one_year(self):
        now = time.localtime()
        tomorrow = time.localtime(time.time() + 86400)
        if tomorrow.tm_year != now.tm_year:
            self.skipTest('跨年窗口（今天 12-31），跳过未来回退用例')
        mmdd = f'{tomorrow.tm_mon:02d}-{tomorrow.tm_mday:02d}'
        got = parse_ls_line(
            f'-rw-r----- 1 root log 7 {mmdd} 23:59 future.log')
        self.assertIsNotNone(got['mtime'])
        self.assertLessEqual(got['mtime'], time.time() + 86400,
                             '补出的未来时刻没有回退一年')


if __name__ == '__main__':
    unittest.main()
