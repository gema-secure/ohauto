"""B4 失败归因引擎的单测 —— 四分类判据 + **20 条注入样例的准确率验收**。

本模块的验收标准原文：
    「注入四类故障各 5 例，准确率 ≥ 80%；**真崩溃必须判为「应用缺陷」**」

所以这个文件里有两层：
  1. 单条判据的单元测试（弱证据不定案、窗口核对、背景崩溃剔除……）
  2. `TestInjectedSamples20` —— 四类各 5 例的验收，附带准确率报告

全部基于 `FakeHdc` / 纯字典构造，**不需要真机**。
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

from ohauto import collect_signals                                    # noqa: E402
from ohauto.diagnose import (APP_CONFIDENCE_FLOOR, CATEGORY_CN,       # noqa: E402
                             FOUR_CATEGORIES, Category, ExecutionRecord,
                             coerce_record, diagnose, diagnose_all,
                             diagnose_failed_step, load_snapshots,
                             locator_id_note, summarize)
from ohauto.explorer import build_page_signature                       # noqa: E402
from ohauto.layout import parse_layout                                 # noqa: E402
from ohauto.runner import FailureKind, StepAttempt, StepResult         # noqa: E402
from ohauto.sim import FakeHdc                                         # noqa: E402


BUNDLE = 'com.demo.app'


# ---------------------------------------------------------------- 构造工具

def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='true', **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _tree(children, **root_extra):
    a = {'type': 'Root', 'id': 'root', 'bounds': _b(0, 0, 1080, 2340),
         'visible': 'true'}
    a.update(root_extra)
    return {'attributes': a, 'children': children}


def _good_page():
    """一张「目标控件在、页面符合预期」的普通页面。"""
    return _tree([
        _node('Text', 'tv_title', '我的订单', _b(40, 120, 500, 200), 'false'),
        _node('Button', 'btn_submit', '提交订单', _b(120, 700, 960, 800)),
    ])


def _page_key(tree, bundle=BUNDLE):
    """取一张控件树的结构签名（当作用例声明的 expected_page）。"""
    return build_page_signature(parse_layout(tree), bundle=bundle).structural_key


def _failed_step(kind=FailureKind.LOCATE, err='DriverError: 未找到控件',
                 index=2, target='btn_submit', action='tap'):
    sr = StepResult(index=index, action=action, target=target, ok=False, kind=kind)
    sr.attempts.append(StepAttempt(attempt=1, ok=False, kind=kind, error=err,
                                   elapsed_ms=8000))
    return sr


def _rescued_step(first_kind=FailureKind.LOCATE, index=2, target='btn_submit',
                  action='tap'):
    """首次失败、重试后成功 —— 时序问题的教科书形态。"""
    sr = StepResult(index=index, action=action, target=target, ok=True)
    sr.attempts.append(StepAttempt(attempt=1, ok=False, kind=first_kind,
                                   error='TimeoutError: 8000ms 内未等到控件',
                                   elapsed_ms=8000))
    sr.attempts.append(StepAttempt(attempt=2, ok=True, elapsed_ms=120))
    sr.elapsed_ms = 8200
    return sr


class _TmpCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_diag_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _signals(self, sim):
        return collect_signals(sim, BUNDLE, out_dir=self.tmp)


# ================================================================ 应用缺陷

class TestAppDefect(_TmpCase):

    def test_crash_is_app_defect_even_if_error_says_locate(self):
        """★ 验收硬要求：真崩溃必须判「应用缺陷」。

        而且这里刻意让运行器的错误文本指向「未找到控件」—— 崩溃把界面带走之后，
        UI 层的报错看起来就是定位失败。归因引擎不能被表面现象骗过去。
        """
        sim = FakeHdc(screen=(120, 260))
        sim.inject_crash(bundle=BUNDLE)
        sig = self._signals(sim)

        rec = ExecutionRecord(
            bundle=BUNDLE, case_name='提交订单',
            step=_failed_step(FailureKind.LOCATE, 'DriverError: 未找到控件 提交订单'),
            trees=[_good_page()], hilog=[])
        rec.signals = sig

        v = diagnose(rec)
        self.assertEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertGreaterEqual(v.confidence, 0.9)
        self.assertIn('CRASH', v.signals_used)
        self.assertTrue(any('崩溃日志' in e for e in v.evidence), v.evidence)

    def test_white_screen_is_app_defect(self):
        """白屏必须显式传 `screen_style='solid'` —— 默认值是真机常态（正常内容页）。"""
        sim = FakeHdc(screen=(120, 260), screen_style='solid')
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        rec.signals = self._signals(sim)
        v = diagnose(rec)
        self.assertEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertIn('WHITE_SCREEN', v.signals_used)

    def test_no_window_is_app_defect(self):
        sim = FakeHdc(screen=(120, 260), locked=True)
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step())
        rec.signals = self._signals(sim)
        v = diagnose(rec)
        self.assertEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertIn('NO_WINDOW', v.signals_used)

    def test_hilog_stack_is_app_defect(self):
        """没有崩溃日志，但 hilog 有异常栈 —— 也要判应用缺陷。"""
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(),
            trees=[_good_page()],
            hilog=['09-21 10:00:00.123  1234  1234 I C04200/App: start',
                   '09-21 10:00:01.456  1234  1234 F C04200/cppcrash: '
                   f'Fault thread info: pid=1234, tid=1234 {BUNDLE}',
                   '09-21 10:00:01.457  1234  1234 F C04200/cppcrash: '
                   '#00 pc 00000000000bda libentry.so(SIGSEGV)'])
        v = diagnose(rec)
        self.assertEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertIn('hilog', v.signals_used)
        self.assertGreaterEqual(v.confidence, APP_CONFIDENCE_FLOOR)


class TestWeakEvidenceDoesNotDecide(_TmpCase):
    """C4 明确标注「别把它单独当结论用」的弱证据，不能把失败判成应用缺陷。"""

    def test_no_response_weak_evidence_falls_through(self):
        """`frozen` + `probe_rounds>=2` 只给 0.3 置信度 → 不参与定案。"""
        sim = FakeHdc(screen=(120, 260), frozen=True)
        sig = collect_signals(sim, BUNDLE, out_dir=self.tmp, probe_rounds=2)
        self.assertTrue(sig.has('NO_RESPONSE'), '前提：夹具确实产出了 NO_RESPONSE')
        self.assertLess(sig.anomaly_score('NO_RESPONSE'), APP_CONFIDENCE_FLOOR)

        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        rec.signals = sig
        v = diagnose(rec)
        self.assertNotEqual(v.category, Category.APP_DEFECT,
                            '弱证据不能定案')
        self.assertTrue(any('低于定案门槛' in e for e in v.evidence), v.evidence)

    def test_process_gone_without_crash_log_is_not_app_defect(self):
        """进程不在、但**没有**崩溃日志（0.35）：可能是 force-stop / 系统回收。"""
        sim = FakeHdc(screen=(120, 260))
        sim.dead_bundles.add(BUNDLE)
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        rec.signals = self._signals(sim)
        v = diagnose(rec)
        self.assertNotEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertTrue(any('force-stop' in e for e in v.evidence), v.evidence)


class TestCrashFiltering(_TmpCase):
    """崩溃日志的采纳规则 —— 别把别人的崩溃、上一轮的旧日志算到这次头上。"""

    def _sig_with_crash(self, **kw):
        sim = FakeHdc(screen=(120, 260))
        sim.inject_crash(bundle=BUNDLE, **kw)
        return self._signals(sim)

    def test_crash_of_another_app_is_filtered_upstream(self):
        """别人的崩溃：C4 采集时就按 bundle 过滤掉了，归因侧连看都看不到。"""
        sim = FakeHdc(screen=(120, 260))
        sim.inject_crash(bundle='com.ohos.calendar')
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        rec.signals = self._signals(sim)
        self.assertEqual(rec.signals.crashes, [],
                         'C4 的 collect_signals 应当已经按 bundle 过滤')
        self.assertNotEqual(diagnose(rec).category, Category.APP_DEFECT)

    def test_foreign_crash_handed_in_directly_is_ignored(self):
        """纵深防御：万一上游没过滤干净，归因侧也要自己认出来并说明原因。"""
        import time
        from ohauto.signals import Anomaly, CrashRecord, Signals
        sig = Signals(bundle=BUNDLE)
        sig.crashes.append(CrashRecord(
            module_name='com.ohos.calendar', pid=1, uid=1,
            timestamp='2026-09-21 10:00:00.000', reason='Signal:SIGSEGV',
            signal='SIGSEGV', foreground=True, timestamp_epoch=time.time()))
        sig.anomalies.append(Anomaly(kind='CRASH', source='faultlog',
                                     confidence=1.0, evidence='别的应用崩了'))
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        rec.signals = sig
        v = diagnose(rec)
        self.assertNotEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertTrue(any('不是被测应用' in e for e in v.evidence), v.evidence)

    def test_old_crash_outside_step_window_is_ignored(self):
        """崩溃时间早于本步开始时间太多 → 上一轮遗留的旧日志，不能拿来定罪。"""
        sig = self._sig_with_crash()
        crash = sig.crashes[0]
        self.assertIsNotNone(crash.timestamp_epoch)
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(), trees=[_good_page()],
            started_at=crash.timestamp_epoch + 3600,   # 本步在崩溃一小时后才开始
        )
        rec.signals = sig
        v = diagnose(rec)
        self.assertNotEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertTrue(any('旧日志' in e for e in v.evidence), v.evidence)

    def test_crash_inside_step_window_is_kept(self):
        sig = self._sig_with_crash()
        crash = sig.crashes[0]
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()],
                              started_at=crash.timestamp_epoch - 5)
        rec.signals = sig
        self.assertEqual(diagnose(rec).category, Category.APP_DEFECT)

    def test_background_crash_does_not_explain_foreground_failure(self):
        """`Foreground` 是三态：None=没采到（按前台处理），False 才算后台崩溃。

        这里手工构造一条 `foreground=False` 的崩溃记录，而不是求 `FakeHdc` 支持
        ——「模拟器默认值必须等于真机常态」是硬约定，后台崩溃是异常态，
        不该为了测试去动 C 的 `sim.py`。
        """
        import time
        from ohauto.signals import Anomaly, CrashRecord, Signals
        sig = Signals(bundle=BUNDLE)
        sig.crashes.append(CrashRecord(
            module_name=BUNDLE, pid=9639, uid=20010019,
            timestamp='2026-09-21 10:00:00.000', reason='Signal:SIGSEGV',
            signal='SIGSEGV', foreground=False,
            timestamp_epoch=time.time(), stack_head=['#00 pc 0x000bda']))
        sig.anomalies.append(Anomaly(kind='CRASH', source='faultlog',
                                     confidence=1.0, evidence='后台崩溃'))

        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        rec.signals = sig
        v = diagnose(rec)
        self.assertNotEqual(v.category, Category.APP_DEFECT, v.evidence)
        self.assertTrue(any('后台崩溃' in e for e in v.evidence), v.evidence)

    def test_foreground_none_is_treated_as_foreground(self):
        """`foreground=None` 表示「日志里没这个字段」，不能当成后台崩溃踢掉。"""
        import time
        from ohauto.signals import Anomaly, CrashRecord, Signals
        sig = Signals(bundle=BUNDLE)
        sig.crashes.append(CrashRecord(
            module_name=BUNDLE, pid=9639, uid=20010019,
            timestamp='2026-09-21 10:00:00.000', reason='Signal:SIGSEGV',
            signal='SIGSEGV', foreground=None,
            timestamp_epoch=time.time()))
        sig.anomalies.append(Anomaly(kind='CRASH', source='faultlog',
                                     confidence=1.0, evidence='崩溃'))
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        rec.signals = sig
        self.assertEqual(diagnose(rec).category, Category.APP_DEFECT)


# ================================================================ 时序

class TestTiming(unittest.TestCase):

    def test_rescued_by_retry_is_timing(self):
        """控件最终能点到 → 已经证伪了「定位失败」。"""
        rec = ExecutionRecord(bundle=BUNDLE, step=_rescued_step(),
                              trees=[_good_page()])
        v = diagnose(rec)
        self.assertEqual(v.category, Category.TIMING, v.evidence)
        self.assertGreaterEqual(v.confidence, 0.9)
        self.assertTrue(any('重试后成功' in e for e in v.evidence), v.evidence)

    def test_timeout_with_target_present_is_timing(self):
        rec = ExecutionRecord(
            bundle=BUNDLE,
            step=_failed_step(FailureKind.TIMEOUT, 'TimeoutError: 8000ms 内未等到控件'),
            trees=[_good_page()],
            expected_target={'id': 'btn_submit'})
        v = diagnose(rec)
        self.assertEqual(v.category, Category.TIMING, v.evidence)
        self.assertTrue(any('等待时长' in e or '界面还没稳定' in e
                            for e in v.evidence), v.evidence)

    def test_late_appearing_control_is_timing(self):
        """失败前两张快照都没有、失败后的快照里出现了 → 界面没稳定。"""
        empty = _tree([_node('Text', 'tv_title', '我的订单', _b(40, 120, 500, 200), 'false')])
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(),
            trees=[empty, empty, _good_page()],
            expected_target={'id': 'btn_submit'})
        v = diagnose(rec)
        self.assertEqual(v.category, Category.TIMING)
        self.assertTrue(any('才出现' in e for e in v.evidence), v.evidence)

    def test_single_snapshot_cannot_claim_late_appearing(self):
        """★ 扩容样例抓到的空真缺陷回归。

        只有一张快照时，「目标在失败后的快照里才出现」无从谈起：
        `present_early = any(trees[:-1])` 对**空序列**恒为 False，原实现把
        『目标就在树里（还是 disabled）』误判成时序问题(0.75)。
        修复后「晚到」判据要求至少两张快照；本形态按「控件在树里但不可用」
        归**用例缺陷**。
        """
        disabled = _tree([_node('Button', 'btn_submit', '提交',
                                _b(120, 700, 960, 800), enabled='false')])
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[disabled],
                              expected_target={'id': 'btn_submit'})
        v = diagnose(rec)
        self.assertNotEqual(v.category, Category.TIMING,
                            '单快照不构成「晚到」证据')
        self.assertEqual(v.category, Category.CASE_DEFECT, v.evidence)

    def test_success_without_retry_is_not_a_failure(self):
        """这步没失败，就不该被归成任何失败类别。"""
        sr = StepResult(index=1, action='tap', ok=True)
        sr.attempts.append(StepAttempt(attempt=1, ok=True))
        v = diagnose(ExecutionRecord(bundle=BUNDLE, step=sr, trees=[_good_page()]))
        self.assertEqual(v.category, Category.UNKNOWN)


# ================================================================ 定位失败

class TestLocator(unittest.TestCase):

    def _record(self, kind=FailureKind.LOCATE, trees=None, page=None, **kw):
        trees = trees if trees is not None else [_good_page()]
        default_page = _page_key(trees[0]) if trees else ''
        rec = ExecutionRecord(
            bundle=BUNDLE, case_name='提交订单', step=_failed_step(kind),
            trees=trees, expected_target={'id': 'btn_missing'},
            expected_page=page if page is not None else default_page, **kw)
        return rec

    def test_missing_control_on_expected_page_is_locator(self):
        rec = self._record()
        rec.locator_id = 'L3_提交订单'          # 由 LocateResult 带过来
        v = diagnose(rec)
        self.assertEqual(v.category, Category.LOCATOR, v.evidence)
        self.assertGreaterEqual(v.confidence, 0.85)
        self.assertEqual(v.locator_id, 'L3_提交订单',
                         '归因侧只原样回写 A 给的 locator_id，不自己拼')
        self.assertTrue(any('页面是对的' in e for e in v.evidence), v.evidence)

    def test_missing_control_without_expected_page_is_weaker(self):
        """没声明期望页面 → 无法排除「根本没走到那一页」，置信度必须下调。"""
        rec = self._record()
        rec.expected_page = ''
        v = diagnose(rec)
        self.assertEqual(v.category, Category.LOCATOR)
        self.assertLess(v.confidence, 0.85)

    def test_no_tree_snapshot_falls_back_to_runner_kind(self):
        rec = self._record(trees=[])
        v = diagnose(rec)
        self.assertEqual(v.category, Category.LOCATOR, v.evidence)
        self.assertTrue(any('没有控件树快照' in e for e in v.evidence), v.evidence)

    def test_near_miss_is_reported(self):
        """树里有同类型/同前缀的控件 → 给一条「可能是 id 改了」的线索。"""
        tree = _tree([
            _node('Text', 'tv_title', '我的订单', _b(40, 120, 500, 200), 'false'),
            _node('Button', 'btn_submit_v2', '提交订单', _b(120, 700, 960, 800)),
        ])
        rec = self._record(trees=[tree], page=_page_key(tree))
        v = diagnose(rec)
        self.assertEqual(v.category, Category.LOCATOR)
        self.assertTrue(any('线索' in e for e in v.evidence), v.evidence)

    def test_locator_sink_is_called_with_the_id_from_locate(self):
        """定位失败要回写定位器自愈；id **原样用 locate 给的那个**。"""
        rec = self._record()
        rec.locator_id = 'L7_刷新按钮'
        calls = []
        diagnose(rec, locator_sink=lambda lid, why: calls.append((lid, why)))
        self.assertEqual(calls, [('L7_刷新按钮',
                                 calls[0][1])] if calls else calls)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'L7_刷新按钮')
        self.assertTrue(calls[0][1])

    def test_sink_not_called_when_locator_id_is_missing(self):
        """★ 没有 locator_id 时**不要**调用 sink —— 免得在定位器台账里塞一条空 id 假账。"""
        calls = []
        rec = self._record()                     # 没带 locator_id
        v = diagnose(rec, locator_sink=lambda lid, why: calls.append(lid))
        self.assertEqual(v.category, Category.LOCATOR)
        self.assertEqual(calls, [])
        self.assertTrue(any('缺 locator_id' in e or '没有 locator_id' in e
                            for e in v.evidence), v.evidence)
        self.assertIn('没能回写', v.suggestion)

    def test_sink_not_called_for_other_categories(self):
        calls = []
        diagnose(ExecutionRecord(bundle=BUNDLE, step=_failed_step(FailureKind.DSL),
                                 trees=[_good_page()]),
                 locator_sink=lambda lid, why: calls.append(lid))
        self.assertEqual(calls, [])


# ================================================================ 用例缺陷

class TestCaseDefect(unittest.TestCase):

    def test_dsl_error_is_case_defect(self):
        rec = ExecutionRecord(
            bundle=BUNDLE,
            step=_failed_step(FailureKind.DSL, 'DslError: 不支持的动作: xxx'),
            trees=[_good_page()])
        v = diagnose(rec)
        self.assertEqual(v.category, Category.CASE_DEFECT, v.evidence)
        self.assertGreaterEqual(v.confidence, 0.9)

    def test_assert_failed_but_control_exists_is_case_defect(self):
        """控件在树里，断言还是不成立 → 期望值写错了，不是应用的问题。"""
        rec = ExecutionRecord(
            bundle=BUNDLE,
            step=_failed_step(FailureKind.ASSERT, 'DriverError: 断言失败'),
            trees=[_good_page()], expected_target={'text': '提交订单'})
        v = diagnose(rec)
        self.assertEqual(v.category, Category.CASE_DEFECT, v.evidence)
        self.assertTrue(any('期望值' in e or '断言逻辑' in e for e in v.evidence),
                        v.evidence)

    def test_missing_precondition_is_case_defect(self):
        rec = ExecutionRecord(
            bundle=BUNDLE,
            step=_failed_step(FailureKind.ASSERT, 'DriverError: 断言失败'),
            trees=[_good_page()], precondition={'id': 'need_login_first'})
        v = diagnose(rec)
        self.assertEqual(v.category, Category.CASE_DEFECT, v.evidence)
        self.assertTrue(any('前置条件' in e for e in v.evidence), v.evidence)

    def test_wrong_landing_page_is_case_defect(self):
        """落点都不是目标页 —— 这时候说「定位失败」是错的归因。"""
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(),
            trees=[_good_page()], expected_target={'id': 'btn_missing'},
            expected_page='deadbeef' * 5)
        v = diagnose(rec)
        self.assertEqual(v.category, Category.CASE_DEFECT, v.evidence)
        self.assertTrue(any('expected_page' in e for e in v.evidence), v.evidence)

    def test_disabled_target_is_case_defect(self):
        """目标控件在、但 disabled → 前置状态没准备好。"""
        tree = _tree([
            _node('Button', 'btn_submit', '提交订单', _b(120, 700, 960, 800),
                  enabled='false'),
        ])
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(FailureKind.ASSERT, '断言失败'),
            trees=[tree], expected_target={'id': 'btn_submit'},
            expected_page=_page_key(tree))
        v = diagnose(rec)
        self.assertEqual(v.category, Category.CASE_DEFECT, v.evidence)
        self.assertTrue(any('enabled=false' in e for e in v.evidence), v.evidence)

    def test_overlay_covering_the_click_point_is_case_defect(self):
        """★ 真遮挡：另一个节点 zIndex 更高**且盖住了点击落点** → 用例该先关掉弹窗。

        旧版这条测试用的是「单节点 zIndex>0」，而那正是 复核出来的反向判据
        （zIndex 大表示它**在上层**，恰好说明没被盖住）。现在按真实机制判：
        uiInput 打的是控件中心坐标，中心被上层节点接走，这一击才会打到别人身上。
        """
        tree = _tree([
            _node('Button', 'btn_submit', '提交订单', _b(120, 700, 960, 800)),
            _node('Stack', 'mask', '', _b(0, 0, 1080, 2340), 'false', zIndex='10'),
        ])
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(FailureKind.ASSERT, '断言失败'),
            trees=[tree], expected_target={'id': 'btn_submit'},
            expected_page=_page_key(tree))
        v = diagnose(rec)
        self.assertEqual(v.category, Category.CASE_DEFECT, v.evidence)
        self.assertTrue(any('落点被上层节点接走' in e for e in v.evidence), v.evidence)


# ================================================================ 输入与输出

class TestContract(unittest.TestCase):

    def test_accepts_step_result_directly(self):
        rec = _failed_step()
        v = diagnose(rec)
        self.assertEqual(v.step_index, 2)

    def test_accepts_dict(self):
        v = diagnose({'bundle': BUNDLE, 'step': _failed_step(),
                      'trees': [_good_page()],
                      'expected_target': {'id': 'btn_missing'},
                      'expected_page': _page_key(_good_page())})
        self.assertEqual(v.category, Category.LOCATOR, v.evidence)

    def test_accepts_case_result_and_picks_independent_failure(self):
        """级联失败是前面某步带崩的，归因要打在**第一个独立失败**上。"""
        from ohauto.runner import CaseResult
        res = CaseResult(name='批量提交', bundle=BUNDLE)
        s1 = _failed_step(FailureKind.LOCATE, '未找到控件')
        s1.index = 1
        s2 = StepResult(index=2, action='tap', ok=False, kind=FailureKind.LOCATE,
                        cascade=True, cascade_of=1)
        s2.attempts.append(StepAttempt(attempt=1, ok=False,
                                       kind=FailureKind.LOCATE, error='未找到控件'))
        res.steps = [s1, s2]
        rec = coerce_record(res)
        self.assertIs(rec.step, s1)
        self.assertEqual(rec.case_name, '批量提交')

    def test_unknown_when_no_evidence_at_all(self):
        v = diagnose(ExecutionRecord(bundle=BUNDLE))
        self.assertEqual(v.category, Category.UNKNOWN)
        self.assertTrue(any('没有拿到可用证据' in e or '没有' in e
                            for e in v.evidence), v.evidence)
        self.assertTrue(v.suggestion)

    def test_verdict_dict_is_report_ready(self):
        v = diagnose(_failed_step())
        d = v.to_dict()
        for k in ('category', 'category_cn', 'confidence', 'evidence', 'suggestion'):
            self.assertIn(k, d)
        self.assertIn(d['category_cn'], CATEGORY_CN.values())

    def test_suggestion_never_advises_hiding_a_defect(self):
        """应用缺陷的建议里必须明确写「别绕过去」—— 这是这个模块的存在意义。"""
        sim = FakeHdc(screen=(120, 260))
        sim.inject_crash(bundle=BUNDLE)
        rec = ExecutionRecord(bundle=BUNDLE, step=_failed_step(),
                              trees=[_good_page()])
        tmp_out = tempfile.mkdtemp(prefix='ohauto_diag2_')
        self.addCleanup(shutil.rmtree, tmp_out, True)
        rec.signals = collect_signals(sim, BUNDLE,
                                      out_dir=tmp_out)
        v = diagnose(rec)
        self.assertIn('不要通过加等待或改用例把它绕过去', v.suggestion)

    def test_summarize(self):
        vs = [diagnose(_failed_step()), diagnose(_rescued_step())]
        s = summarize(vs)
        self.assertEqual(s['total'], 2)
        self.assertEqual(sum(s['by_category'].values()), 2)


# ================================================================ ★ 验收：四类各 5 例

class TestInjectedSamples20(_TmpCase):
    """验收本身：注入四类故障各 5 例，准确率 ≥ 80%。

    样例全部在 `FakeHdc` 上造，**不需要真机** —— 这也是「B 不碰真机」这条
    分工约定下唯一可行的做法。

    ★ 扩容：原 20 条**原样保留**（对应验收原文，动一条
    对比基线就断了），另增 `_extra_samples()` 四类各 5 例——机制不变、
    表面参数变化（故障类型 / 动作形态 / 快照序列），由
    `test_accuracy_of_40_expanded_samples` 跑 4×10 = 40 条的扩容验收。
    """

    def _sig(self, **sim_kw):
        sim = FakeHdc(screen=(120, 260), **sim_kw)
        return self._signals(sim)

    def _samples(self):
        """返回 [(用例名, ExecutionRecord, 期望类别)]，共 20 条。"""
        good = _good_page()
        page = _page_key(good)
        empty = _tree([_node('Text', 'tv_title', '我的订单',
                             _b(40, 120, 500, 200), 'false')])
        out = []

        # ---------------- 应用缺陷 × 5
        sim = FakeHdc(screen=(120, 260)); sim.inject_crash(bundle=BUNDLE)
        r = ExecutionRecord(bundle=BUNDLE, case_name='A1 崩溃',
                            step=_failed_step(FailureKind.LOCATE, '未找到控件'),
                            trees=[good]); r.signals = self._signals(sim)
        out.append(('A1 真崩溃（错误文本却像定位失败）', r, Category.APP_DEFECT))

        r = ExecutionRecord(bundle=BUNDLE, case_name='A2 白屏', step=_failed_step(),
                            trees=[good]); r.signals = self._sig(screen_style='solid')
        out.append(('A2 白屏', r, Category.APP_DEFECT))

        r = ExecutionRecord(bundle=BUNDLE, case_name='A3 无窗口', step=_failed_step())
        r.signals = self._sig(locked=True)
        out.append(('A3 无窗口（锁屏）', r, Category.APP_DEFECT))

        sim = FakeHdc(screen=(120, 260)); sim.inject_freeze(bundle=BUNDLE)
        r = ExecutionRecord(bundle=BUNDLE, case_name='A4 无响应', step=_failed_step())
        r.signals = self._signals(sim)
        out.append(('A4 无响应（freeze 目录有新文件）', r, Category.APP_DEFECT))

        r = ExecutionRecord(
            bundle=BUNDLE, case_name='A5 日志异常栈', step=_failed_step(),
            trees=[good],
            hilog=[f'F C04200/cppcrash: Fault thread info: pid=999 {BUNDLE}',
                   'F C04200/cppcrash: #00 pc 0x000bda libentry.so(SIGSEGV)'])
        out.append(('A5 hilog 异常栈', r, Category.APP_DEFECT))

        # ---------------- 时序问题 × 5
        out.append(('T1 首次未找到、重试成功',
                    ExecutionRecord(bundle=BUNDLE, case_name='时序1',
                                    step=_rescued_step(FailureKind.LOCATE),
                                    trees=[good]), Category.TIMING))
        out.append(('T2 首次超时、重试成功',
                    ExecutionRecord(bundle=BUNDLE, case_name='时序2',
                                    step=_rescued_step(FailureKind.TIMEOUT),
                                    trees=[good]), Category.TIMING))
        out.append(('T3 超时但控件在树里',
                    ExecutionRecord(bundle=BUNDLE, case_name='时序3',
                                    step=_failed_step(FailureKind.TIMEOUT,
                                                      'TimeoutError: 8000ms 内未等到控件'),
                                    trees=[good], expected_target={'id': 'btn_submit'}),
                    Category.TIMING))
        out.append(('T4 控件在失败后的快照里才出现',
                    ExecutionRecord(bundle=BUNDLE, case_name='时序4',
                                    step=_failed_step(),
                                    trees=[empty, empty, good],
                                    expected_target={'id': 'btn_submit'}),
                    Category.TIMING))
        out.append(('T5 无快照的超时（弱证据）',
                    ExecutionRecord(bundle=BUNDLE, case_name='时序5',
                                    step=_failed_step(FailureKind.TIMEOUT, 'timeout'),
                                    trees=[], expected_target={'id': 'btn_submit'}),
                    Category.TIMING))

        # ---------------- 定位失败 × 5
        out.append(('L1 目标不在树、页面符合预期',
                    ExecutionRecord(bundle=BUNDLE, case_name='定位1',
                                    step=_failed_step(), trees=[good],
                                    expected_target={'id': 'btn_missing'},
                                    expected_page=page), Category.LOCATOR))
        out.append(('L2 运行器未分类但控件确实不在',
                    ExecutionRecord(bundle=BUNDLE, case_name='定位2',
                                    step=_failed_step(FailureKind.UNKNOWN, '其他异常'),
                                    trees=[good], expected_target={'id': 'btn_missing'},
                                    expected_page=page), Category.LOCATOR))
        out.append(('L3 三张快照都没有目标',
                    ExecutionRecord(bundle=BUNDLE, case_name='定位3',
                                    step=_failed_step(), trees=[good, good, empty],
                                    expected_target={'id': 'btn_missing'},
                                    expected_page=page), Category.LOCATOR))
        out.append(('L4 树里有同前缀控件的线索',
                    ExecutionRecord(bundle=BUNDLE, case_name='定位4',
                                    step=_failed_step(), trees=[_tree([
                                        _node('Button', 'btn_submit_v2', '提交',
                                              _b(120, 700, 960, 800))])],
                                    expected_target={'id': 'btn_submit'},
                                    expected_page=_page_key(_tree([
                                        _node('Button', 'btn_submit_v2', '提交',
                                              _b(120, 700, 960, 800))]))),
                    Category.LOCATOR))
        out.append(('L5 未声明期望页面（弱）',
                    ExecutionRecord(bundle=BUNDLE, case_name='定位5',
                                    step=_failed_step(), trees=[good],
                                    expected_target={'id': 'btn_missing'}),
                    Category.LOCATOR))

        # ---------------- 用例缺陷 × 5
        out.append(('C1 步骤非法（DSL 错误）',
                    ExecutionRecord(bundle=BUNDLE, case_name='用例1',
                                    step=_failed_step(FailureKind.DSL,
                                                      'DslError: 不支持的动作: xxx'),
                                    trees=[good]), Category.CASE_DEFECT))
        out.append(('C2 断言不成立但控件在树里',
                    ExecutionRecord(bundle=BUNDLE, case_name='用例2',
                                    step=_failed_step(FailureKind.ASSERT, '断言失败'),
                                    trees=[good],
                                    expected_target={'text': '提交订单'}),
                    Category.CASE_DEFECT))
        out.append(('C3 前置条件控件不存在',
                    ExecutionRecord(bundle=BUNDLE, case_name='用例3',
                                    step=_failed_step(FailureKind.ASSERT, '断言失败'),
                                    trees=[good],
                                    precondition={'id': 'need_login_first'}),
                    Category.CASE_DEFECT))
        out.append(('C4 落点页面与声明不符',
                    ExecutionRecord(bundle=BUNDLE, case_name='用例4',
                                    step=_failed_step(), trees=[good],
                                    expected_target={'id': 'btn_missing'},
                                    expected_page='0' * 40), Category.CASE_DEFECT))
        out.append(('C5 目标控件 disabled',
                    ExecutionRecord(bundle=BUNDLE, case_name='用例5',
                                    step=_failed_step(FailureKind.ASSERT, '断言失败'),
                                    trees=[_tree([_node('Button', 'btn_submit', '提交',
                                                        _b(120, 700, 960, 800),
                                                        enabled='false')])],
                                    expected_target={'id': 'btn_submit'},
                                    expected_page=_page_key(_tree([
                                        _node('Button', 'btn_submit', '提交',
                                              _b(120, 700, 960, 800),
                                              enabled='false')]))),
                    Category.CASE_DEFECT))

        return out

    def _extra_samples(self):
        """扩容：四类各 5 例。

        与原 20 条的关系是「同一机制的表面参数变化」，不是新机制 ——
        故障类型（jscrash / appfreeze / OOM / SIGABRT）、动作形态（input）、
        快照序列（4 张 / 全有 / 全缺）、错误文本形态（超时 / 断言 / 定位）。
        换参数不走运地漏判，说明判据对「表面」过敏，这正是扩容要暴露的。
        """
        good = _good_page()
        page = _page_key(good)
        empty = _tree([_node('Text', 'tv_title', '我的订单',
                             _b(40, 120, 500, 200), 'false')])
        # 含输入框的登录页 —— input 动作形态的样例用（_good_page 没有输入框）
        login = _tree([
            _node('Text', 'tv_title', '用户登录', _b(40, 120, 500, 200), 'false'),
            _node('TextInput', 'username', '', _b(120, 400, 960, 500)),
            _node('Button', 'btn_submit', '提交订单', _b(120, 700, 960, 800)),
        ])
        login_page = _page_key(login)
        out = []

        # ---------------- 应用缺陷 × 5（故障类型面）
        out.append(('A6 jscrash 异常栈',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩A6',
                                    step=_failed_step(),
                                    trees=[good],
                                    hilog=[f'F C04200/jscrash: Fault thread info: pid=310 {BUNDLE}',
                                           'F C04200/jscrash: Error name: TypeError, '
                                           'Error message: undefined is not callable']),
                    Category.APP_DEFECT))
        out.append(('A7 appfreeze（卡死栈）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩A7',
                                    step=_failed_step(),
                                    trees=[good],
                                    hilog=[f'F C04200/appfreeze: Fault thread info: pid=410 {BUNDLE}',
                                           'F C04200/appfreeze: #00 pc 0x000bda libentry.so']),
                    Category.APP_DEFECT))
        out.append(('A8 SIGABRT 崩溃（超时形态错误文本）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩A8',
                                    step=_failed_step(FailureKind.TIMEOUT,
                                                      'TimeoutError: 8000ms 内未等到控件'),
                                    trees=[good],
                                    hilog=[f'F C04200/cppcrash: Fault thread info: pid=512 {BUNDLE}',
                                           'F C04200/cppcrash: #00 pc 0x000bda libentry.so(SIGABRT)']),
                    Category.APP_DEFECT))
        out.append(('A9 OOM（内存耗尽）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩A9',
                                    step=_failed_step(),
                                    trees=[good],
                                    hilog=['I C04200/MemMgr: LowMemory Killer: '
                                           f'kill {BUNDLE}',
                                           f'F C04200/cppcrash: Out of memory, pid=613 {BUNDLE}']),
                    Category.APP_DEFECT))
        sim = FakeHdc(screen=(120, 260)); sim.inject_crash(bundle=BUNDLE)
        r = ExecutionRecord(bundle=BUNDLE, case_name='扩A10 崩溃+断言形态',
                            step=_failed_step(FailureKind.ASSERT, '断言失败'),
                            trees=[good])
        r.signals = self._signals(sim)
        out.append(('扩A10 崩溃（错误文本像断言失败）', r, Category.APP_DEFECT))

        # ---------------- 时序 × 5（动作形态 / 快照序列面）
        out.append(('T6 断言失败后重试成功',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩T6',
                                    step=_rescued_step(FailureKind.ASSERT),
                                    trees=[good]), Category.TIMING))
        out.append(('T7 目标在第四张快照才出现',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩T7',
                                    step=_failed_step(),
                                    trees=[empty, empty, empty, good],
                                    expected_target={'id': 'btn_submit'}),
                    Category.TIMING))
        out.append(('T8 超时但控件在树里（input 动作）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩T8',
                                    step=_failed_step(FailureKind.TIMEOUT,
                                                      'TimeoutError: 8000ms 内未等到控件',
                                                      target='username', action='input'),
                                    trees=[login],
                                    expected_target={'id': 'username'}),
                    Category.TIMING))
        out.append(('T9 首次定位落空、重试成功（第 3 步）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩T9',
                                    step=_rescued_step(FailureKind.TIMEOUT,
                                                       index=3, target='btn_login'),
                                    trees=[good]), Category.TIMING))
        out.append(('T10 超时 + 目标在最后一张快照出现',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩T10',
                                    step=_failed_step(FailureKind.TIMEOUT,
                                                      'TimeoutError: 8000ms 内未等到控件'),
                                    trees=[empty, empty, good],
                                    expected_target={'id': 'btn_submit'}),
                    Category.TIMING))

        # ---------------- 定位失败 × 5（错误形态 / 快照面）
        out.append(('L6 超时形态、目标确实不在树',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩L6',
                                    step=_failed_step(FailureKind.TIMEOUT,
                                                      'TimeoutError: 8000ms 内未等到控件'),
                                    trees=[good],
                                    expected_target={'id': 'btn_missing'},
                                    expected_page=page), Category.LOCATOR))
        out.append(('L7 input 目标不在树',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩L7',
                                    step=_failed_step(target='username2',
                                                      action='input'),
                                    trees=[login],
                                    expected_target={'id': 'username2'},
                                    expected_page=login_page), Category.LOCATOR))
        out.append(('L8 目标在所有快照都缺席',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩L8',
                                    step=_failed_step(),
                                    trees=[good, good, good],
                                    expected_target={'id': 'btn_missing'},
                                    expected_page=page), Category.LOCATOR))
        out.append(('L9 同前缀线索（input 控件改名）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩L9',
                                    step=_failed_step(target='input_search',
                                                      action='input'),
                                    trees=[_tree([_node('TextInput', 'input_search_v2', '',
                                                        _b(120, 400, 960, 500))])],
                                    expected_target={'id': 'input_search'},
                                    expected_page=_page_key(_tree([
                                        _node('TextInput', 'input_search_v2', '',
                                              _b(120, 400, 960, 500))]))),
                    Category.LOCATOR))
        out.append(('L10 未声明期望页面（超时形态）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩L10',
                                    step=_failed_step(FailureKind.TIMEOUT,
                                                      'TimeoutError: 8000ms 内未等到控件'),
                                    trees=[good],
                                    expected_target={'id': 'btn_missing'}),
                    Category.LOCATOR))

        # ---------------- 用例缺陷 × 5（前置 / 落点 / 控件状态面）
        out.append(('C6 前置条件不满足（定位形态）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩C6',
                                    step=_failed_step(),
                                    trees=[good],
                                    precondition={'id': 'need_login_first'}),
                    Category.CASE_DEFECT))
        out.append(('C7 DSL 错误（缺参数）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩C7',
                                    step=_failed_step(FailureKind.DSL,
                                                      'DslError: swipe 缺少方向参数'),
                                    trees=[good]), Category.CASE_DEFECT))
        out.append(('C8 落点页面与声明不符（断言形态）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩C8',
                                    step=_failed_step(FailureKind.ASSERT, '断言失败'),
                                    trees=[good],
                                    expected_target={'id': 'btn_submit'},
                                    expected_page='0' * 40), Category.CASE_DEFECT))
        out.append(('C9 目标控件 disabled（定位形态）',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩C9',
                                    step=_failed_step(target='btn_submit'),
                                    trees=[_tree([_node('Button', 'btn_submit', '提交',
                                                        _b(120, 700, 960, 800),
                                                        enabled='false')])],
                                    expected_target={'id': 'btn_submit'},
                                    expected_page=_page_key(_tree([
                                        _node('Button', 'btn_submit', '提交',
                                              _b(120, 700, 960, 800),
                                              enabled='false')]))),
                    Category.CASE_DEFECT))
        out.append(('C10 前置条件 + 超时形态',
                    ExecutionRecord(bundle=BUNDLE, case_name='扩C10',
                                    step=_failed_step(FailureKind.TIMEOUT,
                                                      'TimeoutError: 8000ms 内未等到控件'),
                                    trees=[good],
                                    precondition={'id': 'vip_only'}),
                    Category.CASE_DEFECT))

        return out

    def _accuracy_report(self, samples):
        """跑一批样例并生成准确率报告（原 20 条与扩容 40 条共用一套口径）。"""
        per_cat = {}
        correct = 0
        lines = []
        for name, rec, want in samples:
            v = diagnose(rec)
            per_cat.setdefault(want.value, [0, 0])
            per_cat[want.value][1] += 1
            hit = v.category is want
            if hit:
                correct += 1
                per_cat[want.value][0] += 1
            lines.append(f'  {"✓" if hit else "✗"} {name:<28} '
                         f'期望={CATEGORY_CN[want]} 实判={v.category_cn}'
                         f'({v.confidence:.2f})')
            if not hit:
                lines.append(f'      证据：{v.evidence[:2]}')

        acc = correct / len(samples)
        report = (f'\n归因准确率 {correct}/{len(samples)} = {acc:.0%}\n'
                  + '\n'.join(lines)
                  + '\n  分类明细：' + '  '.join(
                      f'{CATEGORY_CN[Category(k)]} {v[0]}/{v[1]}'
                      for k, v in sorted(per_cat.items())))
        return acc, report

    def test_accuracy_of_20_injected_samples(self):
        samples = self._samples()
        self.assertEqual(len(samples), 20, '四类各 5 例')

        acc, report = self._accuracy_report(samples)
        print(report)
        self.assertGreaterEqual(acc, 0.80, f'归因准确率未达 80%{report}')

    def test_accuracy_of_40_expanded_samples(self):
        """★ 扩容验收：四类各 10 例，准确率 ≥ 80%。

        样例 = 原 20 条 + `_extra_samples()` 20 条。分类明细随测试输出打印，
        离线 KPI 工具（tools/eval_kpi_offline.py）复用同一份样例集出报告。
        """
        samples = self._samples() + self._extra_samples()
        self.assertEqual(len(samples), 40, '四类各 10 例')

        acc, report = self._accuracy_report(samples)
        print(report)
        self.assertGreaterEqual(acc, 0.80, f'扩容归因准确率未达 80%{report}')

    def test_every_injected_crash_is_app_defect(self):
        """★ 验收硬要求单独再钉一遍：**真崩溃必须判为应用缺陷**。"""
        for name, rec, want in self._samples():
            if want is not Category.APP_DEFECT:
                continue
            v = diagnose(rec)
            self.assertEqual(v.category, Category.APP_DEFECT,
                             f'{name} 被误判为 {v.category_cn}：{v.evidence}')

    def test_accuracy_is_reported_per_category(self):
        vs = diagnose_all([r for _, r, _ in self._samples()])
        s = summarize(vs)
        self.assertEqual(s['total'], 20)
        self.assertEqual(set(s['by_category']) - {c.value for c in Category}, set())

    def test_locator_samples_report_missing_locator_id_honestly(self):
        """样例里没带 A 的 locator_id → 结论必须**明说回写不了**，而不是编一个。"""
        for _, rec, want in self._samples():
            if want is not Category.LOCATOR:
                continue
            self.assertTrue(locator_id_note(rec), rec.case_name)
            rec.locator_id = 'L1_x'
            self.assertEqual(locator_id_note(rec), '')


# ================================================================ 真机缺陷回归
#
# ⚠️ `unittest.main()` 只允许出现在**文件末尾**（见文件最后几行）。
# 曾经它被写在这里，后面定义的那 6 个测试类在「单文件直跑」时全部不会被执行，
# 而命令行输出照样是 `OK` —— 一次测试都没有跑，却显示全绿。
# pytest / unittest discovery 不受影响，所以这种错平时发现不了。

class TestNoSnapshotRegression(_TmpCase):
    """★ 真机缺陷回归（C 在 DAYU200 上抓到）：**缺控件树快照时会判错类别**。

    真机对照：

        有快照 → category=LOCATOR       confidence=0.85   ✓
        无快照 → category=CASE_DEFECT   confidence=0.80   ✗ 类别判错

    根因在 `_page_expectation()`：它「遍历所有快照、没命中就 return 'mismatch'」，
    而快照列表为空时循环一次都不执行，直接掉到 `return 'mismatch'` ——
    于是「落点与 expected_page 不符 → 用例缺陷」这条判据被凭空触发。

    **这比"置信度低"严重**：不是判得不确定，是判错了方向。
    修法：没有可用快照就返回 'unknown'（核不了落点就别假装核过）。
    """

    def _rec(self, trees):
        return ExecutionRecord(
            bundle=BUNDLE, case_name='提交订单', step=_failed_step(),
            trees=trees, expected_target={'id': 'btn_missing'},
            expected_page=_page_key(_good_page()))

    def test_category_is_the_same_with_or_without_snapshot(self):
        with_snap = diagnose(self._rec([_good_page()]))
        without = diagnose(self._rec([]))
        self.assertEqual(with_snap.category, Category.LOCATOR)
        self.assertEqual(without.category, Category.LOCATOR,
                         f'缺快照不该改变类别：{without.evidence}')
        self.assertLess(without.confidence, with_snap.confidence,
                        '缺快照只能给弱结论 —— 弱在置信度上，不能弱在类别上')

    def test_no_snapshot_does_not_blame_the_case(self):
        v = diagnose(self._rec([]))
        self.assertNotEqual(v.category, Category.CASE_DEFECT, v.evidence)
        self.assertFalse(any('落点页面与用例声明' in e for e in v.evidence),
                         '核不了落点，就不能说落点不符')

    def test_declared_page_mismatch_still_fires_when_snapshot_exists(self):
        """反向确认：**有快照且确实不符**时，用例缺陷这条判据必须照常触发。"""
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(), trees=[_good_page()],
            expected_target={'id': 'btn_missing'}, expected_page='0' * 40)
        self.assertEqual(diagnose(rec).category, Category.CASE_DEFECT)


# ================================================================ 复核缺陷回归

class TestZIndexSemantics(unittest.TestCase):
    """★ 复核发现的缺陷（原 `diagnose.py:309-314`）：zIndex / opacity 判据**语义反**。

    原文：

        if z is not None and z > 0:
            return False, f'控件 zIndex={z:g}（被上层覆盖）'      # ← 反了
        if op is not None and op < 1.0:
            return False, f'控件 opacity={op:g}（被遮挡/半透明）'  # ← 过急

    `zIndex` 越大表示控件**在上层**，它恰恰说明控件没被盖住。
    方向一反，一个渲染属性就被说成「用例写错了」——**归因方向错了**，
    会把结论交给错误的人去修。
    """

    def _verdict(self, tree, kind=FailureKind.LOCATE):
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(kind, '未找到控件'),
            trees=[tree], expected_target={'id': 'btn_submit'},
            expected_page=_page_key(tree))
        return diagnose(rec)

    def test_high_zindex_without_occluder_is_not_occluded(self):
        """★ C 给的验收：`zIndex=5` 且无更高节点的控件，断言判为**可点**。"""
        tree = _tree([_node('Button', 'btn_submit', '提交订单',
                            _b(120, 700, 960, 800), zIndex='5')])
        v = self._verdict(tree)
        self.assertFalse(any('被上层覆盖' in e for e in v.evidence),
                         f'zIndex>0 不等于被覆盖：{v.evidence}')

    def test_equal_zindex_is_not_occluded(self):
        """同层（zIndex 相同）不算遮挡 —— 只有「更高且盖住落点」才算。"""
        tree = _tree([
            _node('Button', 'btn_submit', '提交订单', _b(120, 700, 960, 800), zIndex='5'),
            _node('Stack', 'sib', '', _b(0, 0, 1080, 2340), 'false', zIndex='5'),
        ])
        v = self._verdict(tree)
        self.assertFalse(any('落点被上层节点接走' in e for e in v.evidence), v.evidence)

    def test_semi_transparent_is_only_suspect_not_blocked(self):
        """0.9 的不透明度照样能点 —— 只记「疑似」，不判死。"""
        tree = _tree([_node('Button', 'btn_submit', '提交订单',
                            _b(120, 700, 960, 800), opacity='0.9')])
        v = self._verdict(tree)
        self.assertFalse(any('存在但不可用' in e for e in v.evidence), v.evidence)
        self.assertTrue(any('疑似' in e or '半透明' in e for e in v.evidence), v.evidence)

    def test_almost_invisible_is_blocked(self):
        """但真的近乎透明（< 0.5）时，判不可用是合理的。"""
        tree = _tree([_node('Button', 'btn_submit', '提交订单',
                            _b(120, 700, 960, 800), opacity='0.2')])
        v = self._verdict(tree)
        self.assertTrue(any('opacity' in e and '不可用' in e for e in v.evidence),
                        v.evidence)

    def test_partial_overlap_of_a_higher_node_is_not_enough(self):
        """★ 判据是「盖住**点击落点**」，不是「矩形有重叠」。

        两个卡片上下相切是常态，按交集算会把正常布局判成遮挡。
        """
        tree = _tree([
            _node('Button', 'btn_submit', '提交订单', _b(120, 700, 960, 800)),
            # 这条高 zIndex 节点只压住控件的左边缘，不覆盖中心 (540, 750)
            _node('Stack', 'edge', '', _b(100, 700, 200, 800), 'false', zIndex='99'),
        ])
        v = self._verdict(tree)
        self.assertFalse(any('落点被上层节点接走' in e for e in v.evidence), v.evidence)


class TestOomWordBoundary(unittest.TestCase):
    """★ 复核发现的缺陷（原 `diagnose.py:92`）：裸 `r'oom'` 命中 Zoom / room。

    这些模式是 `re.I` 下 `re.search` 的，裸 `oom` 会把应用里常见的
    「缩放」「房间」相关日志当成 OOM 崩溃痕迹 —— 给应用缺陷塞假证据。
    """

    def _with_hilog(self, text):
        rec = ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(FailureKind.LOCATE, '未找到控件'),
            trees=[_good_page()], expected_target={'id': 'btn_submit'},
            expected_page=_page_key(_good_page()), hilog=text)
        return diagnose(rec)

    def test_zoom_and_room_are_not_oom(self):
        for text in ('Zoom in failed: scale=2.0', 'Enter room 3 ok',
                     'zoomIn animation done', 'boom level 0'):
            v = self._with_hilog(text)
            self.assertNotEqual(v.category, Category.APP_DEFECT,
                                f'{text!r} 不该判成应用缺陷：{v.evidence}')
            self.assertFalse(any('异常栈' in e for e in v.evidence), v.evidence)

    def test_real_oom_is_still_detected(self):
        """带词边界的真 OOM 必须照旧命中，别把判据一起修没了。"""
        for text in ('Out of memory: killed process 1234', 'OOM killer invoked'):
            v = self._with_hilog(text)
            self.assertTrue(any('异常栈' in e for e in v.evidence), v.evidence)
            self.assertEqual(v.category, Category.APP_DEFECT, v.evidence)


class TestEnvironmentCategory(unittest.TestCase):
    """★ 复核发现的缺陷（原 `diagnose.py:53-60`）：五类里没有「环境问题」。

    没有这一类时，设备掉线 / hdc 断链 / 应用拉不起来 → 全部落到
    `UNKNOWN @ 0.00`，还被建议「请补做结果信号采集」——**设备都没连上，
    采不到任何东西**。归因的价值一半在结论、一半在「把结论交给对的人」。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _rec(self, kind, err, **kw):
        return ExecutionRecord(
            bundle=BUNDLE, step=_failed_step(kind, err),
            trees=[_good_page()], expected_target={'id': 'btn_submit'},
            expected_page=_page_key(_good_page()), **kw)

    def test_device_kind_is_environment_not_unknown(self):
        v = diagnose(self._rec(FailureKind.DEVICE, 'device not found: USB 已断开'))
        self.assertEqual(v.category, Category.ENVIRONMENT, v.evidence)
        self.assertNotEqual(v.category, Category.UNKNOWN)
        self.assertGreaterEqual(v.confidence, 0.8)

    def test_suggestion_points_at_device_not_at_more_collection(self):
        """★ 建议必须指向「检查设备连接」，而不是「补采集」。"""
        v = diagnose(self._rec(FailureKind.DEVICE, 'hdc server 连接已断开'))
        self.assertIn('设备', v.suggestion)
        self.assertNotIn('补做结果信号采集', v.suggestion)

    def test_start_ability_failure_without_crash_is_environment(self):
        v = diagnose(self._rec(FailureKind.APP,
                               'failed to start ability: resolve ability failed'))
        self.assertEqual(v.category, Category.ENVIRONMENT, v.evidence)
        self.assertTrue(any('没有崩溃证据' in e for e in v.evidence), v.evidence)

    def test_plain_timeout_is_not_environment(self):
        """★ 反向确认：**裸超时仍然归时序** —— 环境类不许抢走时序的领地。

        这是「修过头」的护栏：链接层线索必须足够硬才提前定案。
        """
        v = diagnose(self._rec(FailureKind.TIMEOUT,
                               'TimeoutError: 8000ms 内未等到控件'))
        self.assertNotEqual(v.category, Category.ENVIRONMENT, v.evidence)

    def test_crash_still_wins_over_environment(self):
        """真崩溃仍然是应用缺陷 —— 环境类不能越过「崩溃一票否决」。"""
        sim = FakeHdc(screen=(120, 260))
        sim.inject_crash(bundle=BUNDLE)
        rec = self._rec(FailureKind.DEVICE, 'device not found')
        rec.signals = collect_signals(sim, BUNDLE, out_dir=self.tmpdir)
        self.assertEqual(diagnose(rec).category, Category.APP_DEFECT)

    def test_four_categories_are_unchanged(self):
        """★ 本模块口径的「四分类」没有被新增类别稀释。"""
        self.assertEqual([c.value for c in FOUR_CATEGORIES],
                         ['LOCATOR', 'TIMING', 'APP_DEFECT', 'CASE_DEFECT'])
        self.assertEqual(CATEGORY_CN[Category.ENVIRONMENT], '环境问题')


# ================================================================ 闭环接入辅助

class TestSnapshotLoader(unittest.TestCase):
    """`load_snapshots()` —— 闭环接入要用的「从产物目录捞控件树快照」。

    C 的复核（回执 §2.1）指出闭环没闭上，落点在 `runner.py`（**C 的文件**）。
    我能做的是把最容易写错的那段 —— 构造 `ExecutionRecord` —— 做成一个入口，
    其中「快照从哪来」必须自己会找：产物目录里混着报告 / 用例 / 图，
    **不能盲读 json**。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, obj):
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, ensure_ascii=False)

    def test_only_real_layout_trees_are_picked_up(self):
        self._write('0001_layout.json', _good_page())
        self._write('report.json', {'total': 3, 'cases': []})        # 报告
        self._write('graph.json', {'states': {}, 'transitions': []})  # 状态图
        self._write('signals.json', {'crashes': []})
        trees = load_snapshots(self.dir)
        self.assertEqual(len(trees), 1, '只应认出真正的控件树')
        self.assertEqual(trees[0].type, 'Root')

    def test_corrupt_file_does_not_break_the_loader(self):
        self._write('0001_layout.json', _good_page())
        with open(os.path.join(self.dir, '0002_layout.json'), 'w',
                  encoding='utf-8') as fh:
            fh.write('{ 这不是 json')
        self.assertEqual(len(load_snapshots(self.dir)), 1)

    def test_snapshots_are_ordered_by_four_digit_seq(self):
        """★ 排序键 = **4 位采集序号前缀**（B 交付 09-29，C 的权威命名规则）。

        `driver._art()` 产出 `f'{seq:04d}_{ext}'`，`seq` 是全局递增计数器。
        只有按这个序号排才是「执行顺序」；按文件名里有没有 layout 字样排是
        恒真的（真名全都带），等于没排。
        """
        self._write('0003_layout.json', _tree([
            _node('Text', 'other', '另一页', _b(0, 0, 100, 100), 'false')]))
        self._write('0001_layout.json', _good_page())
        self._write('0010_layout.json', _tree([
            _node('Text', 'other', '另一页', _b(0, 0, 100, 100), 'false')]))
        trees = load_snapshots(self.dir)
        self.assertEqual(len(trees), 3)
        self.assertEqual([t.type for t in trees], ['Root'] * 3)
        self.assertEqual(trees[0].children[1].id, 'btn_submit',
                         '0001 应排最前（升序返回）')

    def test_step_index_is_not_a_filename_filter(self):
        """★ `step_index` **不再筛名** —— 真名里没有步号，筛了只会筛掉全部。

        原实现拿 `3` / `03` / `step3` 去匹配文件名，而真名是
        `0001_layout.json`：既匹配不到，又会误中无关序号。
        现在它只是兼容占位，给不给结果一样（此处补上）。
        """
        self._write('0001_layout.json', _good_page())
        self._write('0002_layout.json', _good_page())
        with_index = load_snapshots(self.dir, step_index=3)
        without = load_snapshots(self.dir)
        self.assertEqual(len(with_index), 2, '给了 step_index 也不许筛掉文件')
        self.assertEqual([t.type for t in with_index], [t.type for t in without])

    def test_limit_keeps_the_most_recent_snapshots(self):
        """★ 取**序号最大**的那几张：本函数在「失败发生的那一刻」被调用，
        离现场最近的快照才有用 —— 取最早几张会把失败现场整个漏掉（此处补上）。
        """
        self._write('0001_layout.json', _good_page())
        for i in range(2, 6):
            self._write(f'{i:04d}_layout.json',
                        _tree([_node('Text', 'other', '另一页',
                                     _b(0, 0, 100, 100), 'false')]))
        trees = load_snapshots(self.dir, limit=2)
        self.assertEqual(len(trees), 2)
        for t in trees:
            self.assertIn('other', [c.id for c in t.children],
                          '限制张数时应保留序号最大的（离失败最近）')

    def test_missing_directory_returns_empty_not_raises(self):
        self.assertEqual(load_snapshots(None), [])
        self.assertEqual(load_snapshots(''), [])
        self.assertEqual(load_snapshots(os.path.join(self.dir, '不存在')), [])

    def test_limit_is_respected(self):
        for i in range(5):
            self._write(f'{i}000_layout.json', _good_page())
        self.assertEqual(len(load_snapshots(self.dir, limit=2)), 2)


class TestDiagnoseFailedStepEntry(unittest.TestCase):
    """`diagnose_failed_step()` —— 执行器侧一步到位的接入入口。

    目标是把闭环接起来的成本压到三行：

        if not sr.ok:
            v = diagnose_failed_step(sr, bundle=self.bundle,
                                     artifact_dir=adir, signals=sig)
            res.verdicts.append(v)
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, obj):
        with open(os.path.join(self.dir, name), 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, ensure_ascii=False)

    def test_loads_snapshots_from_artifact_dir_automatically(self):
        self._write('step2_layout.json', _good_page())
        rec_step = _failed_step(FailureKind.LOCATE, '未找到控件')
        v = diagnose_failed_step(rec_step, bundle=BUNDLE,
                                 expected_target={'id': 'btn_missing'},
                                 expected_page=_page_key(_good_page()),
                                 artifact_dir=self.dir)
        self.assertEqual(v.category, Category.LOCATOR, v.evidence)
        self.assertGreaterEqual(v.confidence, 0.85,
                               '有快照 + 落点核对通过 → 强证据')

    def test_confidence_ladder_is_snapshot_driven(self):
        """★ C 关心的那条：**有没有快照，定位失败的置信度差一档**。

            有快照 + 落点核对通过  0.85
            有快照但没声明期望页面 0.7（核不了落点）
            没有快照               0.6（只能信运行器分类）
        """
        step = _failed_step(FailureKind.LOCATE, '未找到控件')
        target = {'id': 'btn_missing'}
        with_snap = diagnose_failed_step(step, bundle=BUNDLE, expected_target=target,
                                         expected_page=_page_key(_good_page()),
                                         trees=[_good_page()])
        no_page = diagnose_failed_step(step, bundle=BUNDLE, expected_target=target,
                                       trees=[_good_page()])
        no_snap = diagnose_failed_step(step, bundle=BUNDLE, expected_target=target)
        self.assertGreater(with_snap.confidence, no_page.confidence)
        self.assertGreater(no_page.confidence, no_snap.confidence)
        for v in (with_snap, no_page, no_snap):
            self.assertEqual(v.category, Category.LOCATOR,
                             '缺证据只该降置信度，不该改类别')

    def test_collects_signals_when_a_device_is_given(self):
        sim = FakeHdc(screen=(120, 260))
        sim.inject_crash(bundle=BUNDLE)
        v = diagnose_failed_step(_failed_step(FailureKind.LOCATE, '未找到控件'),
                                 bundle=BUNDLE, hdc=sim, out_dir=self.dir)
        self.assertEqual(v.category, Category.APP_DEFECT, v.evidence)

    def test_locator_id_is_passed_through_and_written_back(self):
        calls = []
        step = _failed_step(FailureKind.LOCATE, '未找到控件')
        v = diagnose_failed_step(step, bundle=BUNDLE,
                                 expected_target={'id': 'btn_missing'},
                                 locator_id='L9_提交按钮',
                                 locator_sink=lambda lid, why: calls.append(lid))
        self.assertEqual(v.locator_id, 'L9_提交按钮')
        self.assertEqual(calls, ['L9_提交按钮'])

    def test_locator_id_can_be_read_from_step_extra(self):
        """`runner.StepResult` 没有 locator_id 字段 —— 允许放在 `extra` 里。"""
        step = _failed_step(FailureKind.LOCATE, '未找到控件')
        step.extra = {'locator_id': 'L5_刷新'}
        v = diagnose_failed_step(step, bundle=BUNDLE,
                                 expected_target={'id': 'btn_missing'})
        self.assertEqual(v.locator_id, 'L5_刷新')

    def test_no_locator_id_means_no_write_back(self):
        calls = []
        v = diagnose_failed_step(_failed_step(FailureKind.LOCATE, '未找到控件'),
                                 bundle=BUNDLE,
                                 expected_target={'id': 'btn_missing'},
                                 locator_sink=lambda lid, why: calls.append(lid))
        self.assertEqual(v.locator_id, '', '没有 id 就保持空，别自己拼一个')
        self.assertEqual(calls, [], '没有 id 就不许在定位器台账里塞假账')
        self.assertIn('locator_id', v.suggestion + ' '.join(v.evidence),
                      '要明说「这次回写不了」')


if __name__ == '__main__':
    unittest.main(verbosity=2)
