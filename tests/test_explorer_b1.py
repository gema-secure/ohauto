"""B1 / B2 改造的单测 —— 页面双签名、覆盖度度量、四档优先级、Budget 契约。

全部基于 `FakeHdc` / 纯控件树字典，**不需要真机**（任务卡约定：A、B 全在 sim 上开发自测）。

这里刻意把「双签名」当成一号被测对象：任务卡说它是「B1 最该先定下来的东西，
定错了后面全白做」，所以它的行为必须有测试钉住，而不是靠注释解释。
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

from ohauto.driver import Driver                                    # noqa: E402
from ohauto.explorer import (Budget, Explorer, PageSignature,       # noqa: E402
                             Priority, SafetyPolicy, StateGraph,
                             build_page_signature, classify_priority,
                             control_key, detect_dialog, type_fingerprint)
from ohauto.layout import parse_layout                              # noqa: E402
from ohauto.sim import FakeHdc                                      # noqa: E402


# ---------------------------------------------------------------- 构造工具

def _b(l, t, r, bo):
    """真机 bounds 写法，别用别的形态（sim.py 里为这个踩过坑）。"""
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='true', **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _tree(children, root_type='Root', root_extra=None):
    a = {'type': root_type, 'id': 'root', 'bounds': _b(0, 0, 1080, 2340),
         'visible': 'true'}
    if root_extra:
        a.update(root_extra)
    return {'attributes': a, 'children': children}


def _login_like():
    """一张普通的登录页。"""
    return _tree([
        _node('Text', 'title', '欢迎登录', _b(240, 200, 840, 280), 'false'),
        _node('TextInput', 'username', '', _b(120, 400, 960, 500)),
        _node('Button', 'btn_login', '登录', _b(120, 720, 960, 820)),
        _node('Button', 'btn_delete', '注销账号', _b(120, 980, 960, 1060)),
    ])


def _chain_app(n=7):
    """造一个 n 页的线性应用（login → p2 → ... → pn）。

    每页结构不同（多一个语义化标题节点），这样结构签名能把页面区分开；
    每页两个可交互控件，便于算覆盖度。
    """
    names = ['login'] + [f'p{i}' for i in range(2, n + 1)]
    pages, trans = {}, {}
    for i, name in enumerate(names):
        pages[name] = _tree([
            _node(f'Head{i}', f'{name}_head', f'页面{name}', _b(40, 120, 500, 200), 'false'),
            _node('Button', f'{name}_next', '下一步', _b(120, 400, 960, 500)),
            _node('Button', f'{name}_info', f'查看详情{i}', _b(120, 560, 960, 660)),
        ])
        if i + 1 < len(names):
            trans[(name, f'{name}_next')] = names[i + 1]
    return pages, trans


class _StubDriver:
    """只给 Explorer 用的最小 driver 桩：不碰设备，只提供 bundle/ability。"""
    bundle = 'com.demo.app'
    ability = 'EntryAbility'
    artifact_dir = None
    root = None


def _no_sleep(_seconds: float) -> None:
    """注入空实现：等待逻辑的正确性不依赖真实时间流逝。

    探索器每页都要「回到入口再走回来」，真等下去一个用例就是十几秒；
    注入空 sleep 后整组测试从分钟级压回秒级（driver.py 文档里的既有做法）。
    """
    return None


# ================================================================ 双签名

class TestPageSignature(unittest.TestCase):

    def test_content_signature_reacts_to_text_but_structural_does_not(self):
        """★ B1 的核心论据：文案变化不该产生「新页面」。"""
        a = parse_layout(_tree([
            _node('Text', 'tv', '未读 3 条', _b(0, 0, 100, 100), 'false')]))
        b = parse_layout(_tree([
            _node('Text', 'tv', '未读 5 条', _b(0, 0, 100, 100), 'false')]))

        sa = build_page_signature(a)
        sb = build_page_signature(b)

        self.assertNotEqual(sa.content, sb.content, '内容签名对文案变化必须敏感')
        self.assertEqual(sa.content_key == sb.content_key, False)
        self.assertEqual(sa.structural, sb.structural,
                         '结构签名只看控件类型序列，文案变了也必须一致')
        self.assertEqual(sa.structural_key, sb.structural_key)

    def test_structural_signature_reacts_to_type_sequence(self):
        a = parse_layout(_tree([
            _node('Button', 'b1', 'x', _b(0, 0, 100, 100))]))
        b = parse_layout(_tree([
            _node('Button', 'b1', 'x', _b(0, 0, 100, 100)),
            _node('Button', 'b2', 'y', _b(0, 200, 100, 300))]))
        self.assertNotEqual(build_page_signature(a).structural_key,
                            build_page_signature(b).structural_key)

    def test_page_path_participates_in_both_signatures(self):
        """页面归属变了就是不同的页面，哪怕控件树一模一样。"""
        tree = _tree([_node('Button', 'b1', 'x', _b(0, 0, 100, 100))])
        a = build_page_signature(parse_layout(tree))
        b = build_page_signature(parse_layout(_tree([
            _node('Button', 'b1', 'x', _b(0, 0, 100, 100),
                  pagePath='/pages/other')])))
        self.assertNotEqual(a.content_key, b.content_key)
        self.assertNotEqual(a.structural_key, b.structural_key)

    def test_bundle_and_ability_participate(self):
        tree = _tree([_node('Button', 'b1', 'x', _b(0, 0, 100, 100))])
        s1 = build_page_signature(parse_layout(tree), bundle='com.a', ability='A')
        s2 = build_page_signature(parse_layout(tree), bundle='com.b', ability='A')
        s3 = build_page_signature(parse_layout(tree), bundle='com.a', ability='B')
        self.assertNotEqual(s1.content_key, s2.content_key)
        self.assertNotEqual(s1.content_key, s3.content_key)

    def test_node_page_path_overrides_driver_context(self):
        """真机节点自带 pagePath 时以节点为准（C1 报告实测存在该字段）。"""
        root = parse_layout(_tree([
            _node('Button', 'b1', 'x', _b(0, 0, 100, 100),
                  pagePath='/pages/real')]))
        sig = build_page_signature(root, bundle='com.a', ability='A')
        self.assertEqual(sig.page_path, '/pages/real')

    def test_control_key_ignores_coordinates(self):
        """★ 红线第 5 条：坐标会失效（滚动/折叠/旋转），不能当控件身份。"""
        a = parse_layout(_tree([
            _node('Button', 'btn', '提交', _b(0, 0, 100, 100))])).children[0]
        b = parse_layout(_tree([
            _node('Button', 'btn', '提交', _b(500, 900, 600, 1000))])).children[0]
        self.assertEqual(control_key(a), control_key(b))

    def test_control_key_distinguishes_different_controls(self):
        n1 = parse_layout(_tree([_node('Button', 'a', 'x', _b(0, 0, 9, 9))])).children[0]
        n2 = parse_layout(_tree([_node('Button', 'b', 'x', _b(0, 0, 9, 9))])).children[0]
        self.assertNotEqual(control_key(n1), control_key(n2))

    def test_type_fingerprint_is_cross_page_stable(self):
        """type 级稳定 ID 用于「跨页面找同类控件」（A 的降级链要用）。"""
        n1 = parse_layout(_tree([_node('Button', 'a', '登录', _b(0, 0, 9, 9))])).children[0]
        n2 = parse_layout(_tree([_node('Button', 'b', '提交', _b(0, 0, 9, 9))])).children[0]
        n3 = parse_layout(_tree([_node('Text', 'c', '登录', _b(0, 0, 9, 9))])).children[0]
        self.assertEqual(type_fingerprint(n1), type_fingerprint(n2))
        self.assertNotEqual(type_fingerprint(n1), type_fingerprint(n3))


class TestStateGraphDoubleIndex(unittest.TestCase):

    def test_content_index_dedups_same_page(self):
        g = StateGraph()
        root = parse_layout(_login_like())
        sig = build_page_signature(root)
        s1 = g.add_state(sig, '登录页')
        s2 = g.add_state(sig, '登录页')
        self.assertIs(s1, s2)
        self.assertEqual(s1.visits, 2)
        self.assertEqual(len(g.states), 1)

    def test_text_variant_makes_new_state_but_shares_structural_bucket(self):
        """两个「同结构、不同文案」的页面：状态图里是两条，结构索引里是一类。"""
        g = StateGraph()
        ra = parse_layout(_tree([_node('Text', 'tv', '未读 3 条', _b(0, 0, 100, 100), 'false')]))
        rb = parse_layout(_tree([_node('Text', 'tv', '未读 5 条', _b(0, 0, 100, 100), 'false')]))
        sa, sb = build_page_signature(ra), build_page_signature(rb)
        a = g.add_state(sa, 'A')
        b = g.add_state(sb, 'B')

        self.assertEqual(len(g.states), 2, '内容签名不同 → 两个状态')
        self.assertEqual(sa.structural_key, sb.structural_key)
        same = g.find_by_structural(sa.structural_key)
        self.assertEqual({s.sid for s in same}, {a.sid, b.sid})

    def test_backward_compatible_with_plain_string_signature(self):
        g = StateGraph()
        s = g.add_state('raw-signature', '旧调用方式')
        self.assertEqual(len(g.states), 1)
        self.assertEqual(s.title, '旧调用方式')
        self.assertEqual(s.structural_key, '')

    def test_to_dict_carries_both_keys(self):
        g = StateGraph()
        g.add_state(build_page_signature(parse_layout(_login_like())), '登录页')
        d = g.to_dict()['states'][0]
        self.assertTrue(d['content_key'])
        self.assertTrue(d['structural_key'])


# ================================================================ 弹窗识别

class TestDialogDetection(unittest.TestCase):

    def test_plain_page_is_not_dialog(self):
        v = detect_dialog(parse_layout(_login_like()))
        self.assertFalse(v.is_dialog)
        self.assertEqual(v.evidence, [])

    def test_dialog_by_type_hint(self):
        root = parse_layout(_tree([
            _node('Dialog', 'dlg', '', _b(100, 800, 980, 1500)),
            _node('Button', 'ok', '确定', _b(200, 1300, 400, 1400)),
        ]))
        v = detect_dialog(root)
        self.assertTrue(v.is_dialog)
        self.assertTrue(any('弹窗语义' in e for e in v.evidence), v.evidence)

    def test_dialog_by_multi_window_requires_overlap(self):
        """★ 真机缺陷回归（2026-09-23）：判据是「窗口**互相重叠**」，不是「有多个窗口」。

        真机 `dumpLayout` 返回的是**整个窗口栈**：状态栏 `[0,0][720,72]`、
        系统窗 `[0,0][720,32]`、导航栏 `[0,1208][720,1280]`、应用 `[0,72][720,1208]`
        —— 四个 `hostWindowId`，但彼此只是把屏幕切成几块。

        原判据「≥2 个 hostWindowId 即多窗口叠加」在真机上**恒为真**，
        7/7 张真机样本全部被误判成弹窗（见 `test_real_device_20260923.py`）。
        """
        # ① 真机窗口栈的常态：互不重叠 → **不能**判弹窗
        stack = parse_layout(_tree([
            _node('Text', 'sb', 'x', _b(0, 0, 720, 72), 'false', hostWindowId='7'),
            _node('Button', 'appbtn', 'y', _b(0, 200, 720, 800), hostWindowId='14'),
            _node('Button', 'nav', 'z', _b(0, 1208, 720, 1280), hostWindowId='8'),
        ]))
        v = detect_dialog(stack)
        self.assertFalse(v.is_dialog, f'系统窗口栈不是弹窗：{v.evidence}')

        # ② 真弹窗盖在应用内容区上 → 判弹窗
        overlay = parse_layout(_tree([
            _node('Button', 'appbtn', 'y', _b(0, 200, 720, 800), hostWindowId='14'),
            _node('Column', 'dlg', '', _b(100, 300, 620, 700), 'false',
                  hostWindowId='22'),
        ]))
        v2 = detect_dialog(overlay)
        self.assertTrue(v2.is_dialog)
        self.assertTrue(any('多窗口' in e for e in v2.evidence), v2.evidence)

    def test_dialog_by_mask_node(self):
        """近全屏 + zIndex>0 的遮挡节点。"""
        root = parse_layout(_tree([
            _node('Stack', 'mask', '', _b(0, 0, 1080, 2340), 'false',
                  zIndex='10', opacity='0.5'),
        ]))
        v = detect_dialog(root)
        self.assertTrue(v.is_dialog)
        self.assertTrue(any('遮挡节点' in e for e in v.evidence), v.evidence)

    def test_fullscreen_node_without_zindex_is_not_evidence(self):
        """全屏但有没 zIndex/opacity 的普通容器不能误判成遮罩。"""
        root = parse_layout(_tree([
            _node('Stack', 'container', '', _b(0, 0, 1080, 2340), 'false'),
        ]))
        self.assertFalse(detect_dialog(root).is_dialog)

    def test_evidence_is_kept_for_explainability(self):
        """判据必须能解释 —— B4 的归因要拿它当证据。"""
        root = parse_layout(_tree([
            _node('Popup', 'p', '', _b(0, 0, 1080, 2340), 'false'),
        ]))
        self.assertTrue(detect_dialog(root).evidence)


# ================================================================ 优先级

class TestPriority(unittest.TestCase):

    def _node_of(self, **kw):
        return parse_layout(_tree([
            _node('Button', kw.pop('cid', 'b1'), kw.pop('text', '普通'),
                  _b(0, 0, 200, 100), **kw)])).children[0]

    def test_dangerous_is_blocked_by_default(self):
        n = self._node_of(cid='del', text='删除全部')
        self.assertIsNone(classify_priority(n, dangerous=True, allow_dangerous=False))

    def test_dangerous_goes_urgent_when_allowed(self):
        n = self._node_of(cid='del', text='删除全部')
        self.assertEqual(classify_priority(n, dangerous=True, allow_dangerous=True),
                         Priority.URGENT)

    def test_dialog_control_is_high(self):
        n = self._node_of(text='普通按钮')
        self.assertEqual(classify_priority(n, in_dialog=True), Priority.HIGH)

    def test_keyword_control_is_high(self):
        for kw in ('登录', '搜索', '查看详情', '提交'):
            n = self._node_of(text=kw)
            self.assertEqual(classify_priority(n), Priority.HIGH, kw)

    def test_exhausted_control_is_low(self):
        n = self._node_of(text='搜索')
        self.assertEqual(classify_priority(n, exhausted=True), Priority.LOW,
                         '已点过且没引起变化的控件要降到兜底档，哪怕它带关键词')

    def test_plain_interactive_is_medium(self):
        n = self._node_of(text='其他')
        self.assertEqual(classify_priority(n), Priority.MEDIUM)

    def test_priority_values_are_the_documented_ones(self):
        """档位是固定值不是权重 —— 数字变了必须有人知道。"""
        self.assertEqual((Priority.URGENT, Priority.HIGH, Priority.MEDIUM,
                          Priority.NORMAL, Priority.LOW),
                         (100, 80, 40, 20, 10))


# ================================================================ Budget 契约

class TestBudget(unittest.TestCase):

    def test_four_caps_all_present(self):
        b = Budget()
        self.assertEqual((b.max_pages, b.max_actions_per_page,
                          b.max_seconds, b.max_steps), (8, 6, 300.0, 200))

    def test_invalid_values_rejected(self):
        for kw in ({'max_pages': 0}, {'max_actions_per_page': -1},
                   {'max_seconds': 0}, {'max_steps': 0}):
            with self.assertRaises(ValueError, msg=kw):
                Budget(**kw)

    def test_to_dict(self):
        self.assertEqual(Budget(max_pages=3).to_dict()['max_pages'], 3)


# ================================================================ 覆盖度

class TestCoverage(unittest.TestCase):

    def test_denominator_not_inflated_by_text_change(self):
        """★ 同一页面文案变了，分母不能跟着翻倍（覆盖率虚高的根源）。"""
        ex = Explorer(_StubDriver(), verbose=False)
        ra = parse_layout(_tree([
            _node('Button', 'b1', '未读 3 条', _b(0, 0, 200, 100)),
            _node('Button', 'b2', '确定', _b(0, 200, 200, 300)),
        ]))
        rb = parse_layout(_tree([
            _node('Button', 'b1', '未读 5 条', _b(0, 0, 200, 100)),
            _node('Button', 'b2', '确定', _b(0, 200, 200, 300)),
        ]))
        ex._note_page(build_page_signature(ra), ra)
        ex._note_page(build_page_signature(rb), rb)
        self.assertEqual(ex.coverage.interactive_total, 2,
                         '文案变化不该被算成两个页面、四个控件')

    def test_blocked_dangerous_controls_stay_in_denominator(self):
        """被安全策略拦下的控件**仍然算可交互** —— 否则拦得越多数字越好看。"""
        ex = Explorer(_StubDriver(), verbose=False)
        root = parse_layout(_login_like())
        ex._note_page(build_page_signature(root), root)
        total = ex.coverage.interactive_total
        cands = ex._candidates(root)
        self.assertNotIn('btn_delete', {n.id for n in cands})
        self.assertIn('btn_delete', {n.id for n in ex._interactive_nodes(root)})
        self.assertGreaterEqual(total, 3)

    def test_blocked_count_is_reported(self):
        """被拦下的危险控件要单独报出来，否则读的人不知道为什么不是 100%。"""
        ex = Explorer(_StubDriver(), verbose=False)
        root = parse_layout(_login_like())
        ex._note_page(build_page_signature(root), root)
        self.assertEqual(ex.coverage.blocked, 1)
        self.assertIn('blocked', ex.coverage.to_dict())

    def test_ratio_and_dict(self):
        ex = Explorer(_StubDriver(), verbose=False)
        root = parse_layout(_login_like())
        sig = build_page_signature(root)
        ex._note_page(sig, root)
        self.assertEqual(ex.coverage.ratio, 0.0)
        ex._done.add(f'{sig.structural_key}::{control_key(root.children[1])}')
        self.assertEqual(ex.coverage.ratio, 1 / ex.coverage.interactive_total)
        self.assertIn('ratio', ex.coverage.to_dict())

    def test_zero_denominator_is_zero_not_crash(self):
        ex = Explorer(_StubDriver(), verbose=False)
        self.assertEqual(ex.coverage.ratio, 0.0)

    def test_dialog_controls_all_go_high(self):
        """任务卡原话是「弹窗内可交互控件**一律**提为最高优先级」——
        一律就是不看关键词，普通控件在弹窗里也是 HIGH。"""
        ex = Explorer(_StubDriver(), verbose=False)
        ex.dialog = detect_dialog(parse_layout(_tree([
            _node('Dialog', 'd', '', _b(0, 0, 1080, 2340), 'false')])))
        root = parse_layout(_tree([
            _node('Button', 'plain', '其他', _b(0, 0, 200, 100)),
            _node('Button', 'kw', '搜索', _b(0, 200, 200, 300)),
        ]))
        for n in root.children:
            self.assertEqual(ex.priority_of(n), Priority.HIGH, n.id)

    def test_dialog_control_outranks_plain_page_control(self):
        """对照：同样一个不带关键词的控件，弹窗里的档位要高于普通页面上的。"""
        plain = parse_layout(_tree([
            _node('Button', 'plain', '其他', _b(0, 0, 200, 100))])).children[0]
        ex = Explorer(_StubDriver(), verbose=False)
        self.assertEqual(ex.priority_of(plain), Priority.MEDIUM)
        ex.dialog = detect_dialog(parse_layout(_tree([
            _node('Dialog', 'd', '', _b(0, 0, 1080, 2340), 'false')])))
        self.assertEqual(ex.priority_of(plain), Priority.HIGH)


# ================================================================ 探索闭环

class _SimCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_b1_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sim_driver(self, start_page='login'):
        sim = FakeHdc(start_page=start_page)
        return sim, Driver(bundle='com.demo.app', hdc=sim,
                           artifact_dir=self.tmp, verbose=False, sleep_fn=_no_sleep)


class TestExploreOnSim(_SimCase):

    def test_contract_signature_is_explore_max_pages_budget(self):
        """★ 对外契约：`explore(max_pages, budget)` —— 两个参数都要能按位置传。"""
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        g = ex.explore(5, Budget(max_pages=5, max_actions_per_page=6,
                                 max_seconds=60, max_steps=100))
        self.assertEqual(len(g.states), 3)

    def test_deprecated_kwargs_still_work_but_warn(self):
        """散参数已弃用：还能用，但必须**明确告警**，不能悄悄接受。"""
        import warnings
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            ex.explore(max_pages=5, max_actions_per_page=6)
        self.assertEqual(len(ex.graph.states), 3, '弃用不等于不能跑')
        self.assertTrue(any(issubclass(w.category, DeprecationWarning)
                            for w in caught), [str(w.message) for w in caught])
        self.assertTrue(any('Budget' in str(w.message) for w in caught),
                        '告警里必须给出迁移方向，否则等于没告警')

    def test_contract_path_emits_no_warning(self):
        """走契约形态（max_pages + Budget）时不该有任何弃用告警。"""
        import warnings
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            ex.explore(5, Budget(max_pages=5, max_actions_per_page=6))
        self.assertEqual([w for w in caught
                          if issubclass(w.category, DeprecationWarning)], [])

    def test_explicit_arg_wins_over_budget_without_mutating_it(self):
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        b = Budget(max_pages=9)
        ex.explore(3, b)
        self.assertEqual(b.max_pages, 9, '调用方的 Budget 不该被就地改写')
        self.assertEqual(ex.last_budget.max_pages, 3)

    def test_dangerous_controls_never_clicked(self):
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        ex.explore(5, Budget(max_pages=5, max_actions_per_page=10))
        clicked = {a.get('node') for a in sim.actions if a['action'] == 'click'}
        self.assertNotIn('btn_danger_delete', clicked)
        self.assertNotIn('btn_logout', clicked)

    def test_urgent_controls_clicked_first_when_allowed(self):
        """放开危险控件后它们要**置顶**，而不是被顺手跳过。"""
        sim, d = self._sim_driver()
        ex = Explorer(d, policy=SafetyPolicy(allow_dangerous=True),
                      artifact_dir=self.tmp, verbose=False)
        root = d.refresh()
        ex.dialog = detect_dialog(root)
        cands = ex._candidates(root)
        self.assertEqual(cands[0].id, 'btn_danger_delete',
                         'URGENT(100) 必须排在最前面')

    def test_return_back_true_still_reaches_every_page(self):
        """★ 回归：`return_back=True` 曾经会把探索器带偏。

        原因是「队列里待探索的页面」和「设备实际停在的页面」脱节 ——
        点完 A 页的候选、back 回到 A，再轮到 B 页时设备还在 A，
        于是拿 A 的候选控件去点，页面状态图直接失真。
        修法是每页开探前先照记录下来的路径回溯（`_navigate_to`）。
        """
        pages, trans = _chain_app(6)
        sim = FakeHdc(start_page='login', seed=pages, transitions=trans)
        d = Driver(bundle='com.demo.app', hdc=sim,
                   artifact_dir=self.tmp, verbose=False, sleep_fn=_no_sleep)
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        g = ex.explore(8, Budget(max_pages=8, max_actions_per_page=4,
                                 max_seconds=120, max_steps=200))
        self.assertEqual(len(g.states), 6, '开了返回栈回溯就该把 6 页都走到')
        self.assertTrue(all(s.reachable for s in g.states.values()))

    def test_coverage_is_reported_and_bounded(self):
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        ex.explore(5, Budget(max_pages=5, max_actions_per_page=10))
        cov = ex.coverage
        self.assertGreater(cov.interactive_total, 0)
        self.assertLessEqual(cov.interactive_visited, cov.interactive_total)
        self.assertTrue(0.0 < cov.ratio <= 1.0)
        self.assertEqual(cov.pages, 3)

    def test_low_priority_controls_are_retried_last(self):
        """点了没反应的控件要被降到 LOW，而不是每轮都排在前面空转。"""
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        ex.explore(5, Budget(max_pages=5, max_actions_per_page=10))
        self.assertTrue(ex._exhausted, '至少应有一个「点了无变化」的控件被标记')

    def test_save_graph_carries_coverage_and_budget(self):
        import json
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        ex.explore(5, Budget(max_pages=5, max_actions_per_page=6), return_back=False)
        p = ex.save_graph(os.path.join(self.tmp, 'g.json'))
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn('coverage', data)
        self.assertIn('budget', data)
        self.assertIn('structural_key', data['states'][0])


class TestCoverageAcceptance(_SimCase):
    """B1 的验收标准本身：多页面应用上覆盖度 ≥ 90%。"""

    def test_seven_page_app_coverage_at_least_90pct(self):
        pages, trans = _chain_app(7)
        sim = FakeHdc(start_page='login', seed=pages, transitions=trans)
        d = Driver(bundle='com.demo.app', hdc=sim,
                   artifact_dir=self.tmp, verbose=False, sleep_fn=_no_sleep)
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)

        g = ex.explore(10, Budget(max_pages=10, max_actions_per_page=6,
                                  max_seconds=120, max_steps=200))

        self.assertEqual(len(g.states), 7, '七页应用要全部发现')
        cov = ex.coverage
        self.assertEqual(cov.interactive_total, 14, '每页两个可交互控件')
        self.assertGreaterEqual(cov.ratio, 0.9,
                                f'覆盖度 {cov.ratio:.1%} 未达 90%')
        self.assertEqual(cov.pages, 7)

    def test_no_dead_loop_on_self_linking_page(self):
        """自环页面（点了不动）不能把探索器卡死。"""
        pages = {
            'login': _tree([
                _node('Button', 'stay', '刷新', _b(120, 400, 960, 500)),
                _node('Button', 'go', '下一步', _b(120, 600, 960, 700)),
            ]),
            'p2': _tree([_node('Button', 'x', '返回', _b(0, 0, 100, 100))]),
        }
        sim = FakeHdc(start_page='login', seed=pages,
                      transitions={('login', 'go'): 'p2'})
        d = Driver(bundle='com.demo.app', hdc=sim,
                   artifact_dir=self.tmp, verbose=False, sleep_fn=_no_sleep)
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        g = ex.explore(5, Budget(max_pages=5, max_actions_per_page=4,
                                 max_seconds=60, max_steps=60))
        self.assertEqual(len(g.states), 2)
        self.assertLessEqual(len(g.transitions), 12)


class TestBackVerification(_SimCase):
    """返回栈回溯：用双签名配合判断「是否真的回到了原页」。

    这一组刻意**自带 seed**，不去改 `sim.pages` —— `FakeHdc()` 不传 seed 时
    `self.pages` 就是模块级的 `PAGES`，改它会把污染带到后面所有用例
    （踩过一次：一个用例把 login 页砍成两个节点，后面所有探索用例集体失真）。
    """

    def _two_page_sim(self):
        pages = {
            'login': _tree([
                _node('Text', 'title', '欢迎登录', _b(240, 200, 840, 280), 'false'),
                _node('Button', 'btn_next', '下一步', _b(120, 400, 960, 500)),
            ]),
            'p2': _tree([
                _node('Text', 'p2_title', '第二页', _b(240, 200, 840, 280), 'false'),
                _node('Button', 'p2_act', '查看详情', _b(120, 400, 960, 500)),
            ]),
        }
        sim = FakeHdc(start_page='login', seed=pages,
                      transitions={('login', 'btn_next'): 'p2'})
        d = Driver(bundle='com.demo.app', hdc=sim,
                   artifact_dir=self.tmp, verbose=False, sleep_fn=_no_sleep)
        return sim, d

    def test_explore_back_verdicts_are_recorded(self):
        sim, d = self._two_page_sim()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        g = ex.explore(4, Budget(max_pages=4, max_actions_per_page=4,
                                 max_seconds=60, max_steps=60))
        self.assertEqual(len(g.states), 2)
        self.assertTrue(ex.back_verdicts, '每次探测后都应有一条返回核对结论')
        self.assertIn('exact', ex.back_verdicts)

    def test_content_changed_counts_as_returned(self):
        """结构一致、内容不同 → 算回到原页（列表/计数变化不该判成导航失败）。"""
        sim, d = self._two_page_sim()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        root = d.refresh()
        cur = ex.graph.add_state(ex._page_signatures(root), '当前页')

        import copy
        pages2 = copy.deepcopy(sim.pages['login'])
        for c in pages2['children']:                      # 只改文案，不动结构
            if c['attributes'].get('type') == 'Text':
                c['attributes']['text'] = '欢迎回来'
        sim.pages['login'] = pages2

        verdict = ex._back_and_verify(cur, 'tmp')
        self.assertEqual(verdict, 'content-changed')

    def test_structural_change_is_lost_and_marked_unreachable(self):
        """返回后连结构都变了、照路径也回不去 → lost，并且不再装作已探索。"""
        sim, d = self._two_page_sim()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        root = d.refresh()
        cur = ex.graph.add_state(ex._page_signatures(root), '当前页')
        cur.path = [{'id': '不存在的控件'}]        # 路径断掉，回溯必然失败

        import copy
        pages2 = copy.deepcopy(sim.pages['login'])
        pages2['children'] = pages2['children'][:1]       # 结构也变了
        sim.pages['login'] = pages2

        self.assertEqual(ex._back_and_verify(cur, 'tmp'), 'lost')
        self.assertEqual(ex.back_verdicts[-1], 'lost')

    def test_navigate_to_entry_page_reenters(self):
        """入口页的回溯就是回到起点。"""
        sim, d = self._two_page_sim()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        root = d.refresh()
        entry = ex.graph.add_state(ex._page_signatures(root), '入口页')
        sim.current = 'p2'
        self.assertTrue(ex._navigate_to(entry))
        self.assertEqual(sim.current, 'login')

    def test_navigate_to_follows_recorded_path(self):
        """★ 回溯靠的是记录下来的控件序列，不是 back —— 连着点两层也回得去。"""
        sim, d = self._two_page_sim()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        d.refresh()
        p2_sig = None
        d.tap(ON_id('btn_next'))                  # login -> p2
        p2_sig = ex._page_signatures()
        p2 = ex.graph.add_state(p2_sig, '第二页', path=[{'id': 'btn_next'}])

        sim.current = 'login'                     # 假装设备跑别处去了
        self.assertTrue(ex._navigate_to(p2))
        self.assertEqual(sim.current, 'p2')


def ON_id(cid):
    """小工具：按 id 拿匹配器（测试里少写点样板）。"""
    from ohauto.matcher import ON
    return ON.id(cid)


# ================================================================ 真机缺陷回归（2026-09-22）
#
# ⚠️ `unittest.main()` 只允许出现在**文件末尾**（见文件最后几行）。
# 曾经它写在这里，后面那 3 个测试类在「单文件直跑」时全部不执行，
# 而输出照样是 `OK` —— 没跑一条，却显示全绿。discovery 不受影响，所以平时发现不了。

class TestPageTitleExcludesStatusBar(_SimCase):
    """★ 真机缺陷回归（C 在 DAYU200 上抓到）：标题全取成状态栏文本。

    真机状态栏永远在 `top≈32` 且带文本（`没有 SIM 卡` 在 `[43,32][170,50]`），
    而原实现取「最靠上的文本」，于是六个页面的标题全成了同一句
    —— `content_key` 各不相同（双签名是对的），报告里却分不出谁是谁。
    模拟设备状态栏文本为空，这个缺陷自测**暴露不出来**，所以按真机形态构造夹具。
    """

    def _status_bar_page(self):
        return _tree([
            # 真机状态栏形态：top 很小、有文本
            _node('Text', 'status_bar', '没有 SIM 卡', _b(43, 32, 170, 50), 'false'),
            _node('Text', 'nav_title', '计算器', _b(40, 200, 400, 280), 'false'),
            # 正文提示的面积是标题的 30 倍 —— 按面积排会把标题挤掉（C 踩过）
            _node('Text', 'body_hint', '按 = 出结果', _b(20, 900, 1060, 1300), 'false'),
            _node('Button', 'btn_1', '1', _b(120, 1500, 400, 1700)),
        ])

    def test_title_prefers_topmost_text_below_status_bar(self):
        sim = FakeHdc(start_page='login',
                      seed={'login': self._status_bar_page()}, transitions={})
        d = Driver(bundle='com.demo.app', hdc=sim, artifact_dir=self.tmp,
                   verbose=False, sleep_fn=_no_sleep)
        d.refresh()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        self.assertEqual(ex._page_title(), '计算器',
                         '必须剔掉状态栏，且不能被大块正文挤掉')

    def test_status_bar_constant_matches_real_device(self):
        """真机 `SystemUi_StatusBar` 实测 `[0,0,720,72]` —— 这个数字有出处。"""
        self.assertEqual(Explorer.STATUS_BAR_MAX_TOP, 72)


class TestCoveragePagesSemantics(_SimCase):
    """★ 口径订正（2026-09-22）：`pages` 是界面**状态**数，`pages_structural` 才是**页面**数。

    真机计算器是单页应用，点 5 次 → 报告说「发现页面数 6」。
    根因是 `pages=len(graph.states)`，而 states 按**内容签名**去重 ——
    「点一下、正文文字变了」就被算成新页面。

    KPI「发现页面数」取 `pages_structural`（结构签名归并），这条测试把它钉住。
    """

    def _two_state_one_page_app(self):
        """两种状态、同一个页面结构 —— 就是「单页应用点了按钮文字变了」的最小复现。"""
        def page(text):
            return _tree([
                _node('Text', 'display', text, _b(40, 200, 1040, 400), 'false'),
                _node('Button', 'btn_calc', '=', _b(120, 1500, 400, 1700)),
            ])
        return {'login': page('显示: 0'), 'v2': page('显示: 1')}, \
               {('login', 'btn_calc'): 'v2', ('v2', 'btn_calc'): 'login'}

    def test_single_page_app_counts_as_one_page(self):
        pages, trans = self._two_state_one_page_app()
        sim = FakeHdc(start_page='login', seed=pages, transitions=trans)
        d = Driver(bundle='com.demo.app', hdc=sim, artifact_dir=self.tmp,
                   verbose=False, sleep_fn=_no_sleep)
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        ex.explore(6, Budget(max_pages=6, max_actions_per_page=4))

        cov = ex.coverage
        self.assertGreaterEqual(cov.pages, 2, '两种界面状态 = 两个 state')
        self.assertEqual(cov.pages_structural, 1,
                         '结构签名相同 → 只有一个「页面」；KPI 用这个数')
        self.assertIn('pages_structural', cov.to_dict())

    def test_pages_structural_backed_by_structural_index(self):
        ex = Explorer(_StubDriver(), verbose=False)
        self.assertEqual(ex.coverage.pages_structural, 0)
        root = parse_layout(_tree([
            _node('Text', 'tv', 'x', _b(0, 0, 100, 100), 'false')]))
        ex.graph.add_state(build_page_signature(root), 'p')
        self.assertEqual(ex.coverage.pages_structural, 1)


# ================================================================ C 复核缺陷回归（2026-09-23）

class TestRefreshSignatureRegression(_SimCase):
    """★ C 复核缺陷（原 `explorer.py:795`）：**刷新后的页面签名被丢弃**。

    原写法：

        root, _ = self._observe()        # ← 第二个返回值（页面签名）被丢掉
        ...
        uid = f'{sig.structural_key}::{control_key(node)}'

    循环内只出现 `new_sig` / `back_sig`，`sig` 从未重新赋值 ——
    所以这个 `uid` 用的始终是**入口页**的签名。四条后果都很静默：

      ① `_done` 按入口页记账 → 跨页同 key 控件被误合并，后续页面**少探索**；
      ② 「点了没反应就降档」在非入口页静默失效；
      ③ 覆盖度明细里每页 `visited` **恒为 0**；
      ④ 状态图（挑战 #3）可信度受影响。

    一直没被抓到，是因为现有测试里那个无效控件恰好落在入口页 ——
    所以这条回归测试**专门盯着非入口页**。
    """

    def _explore(self, n=5):
        pages, trans = _chain_app(n)
        sim = FakeHdc(start_page='login', seed=pages, transitions=trans)
        d = Driver(bundle='com.demo.app', hdc=sim, artifact_dir=self.tmp,
                   verbose=False, sleep_fn=_no_sleep)
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        g = ex.explore(n + 3, Budget(max_pages=n + 3, max_actions_per_page=6,
                                     max_seconds=120, max_steps=200))
        return ex, g

    def test_non_entry_pages_are_accounted_by_their_own_signature(self):
        ex, g = self._explore(5)
        entry_struct = next(st.structural_key for st in g.states.values()
                            if st.sid == 'S1')
        others = {st.structural_key for st in g.states.values() if st.sid != 'S1'}
        self.assertTrue(others, '至少要发现一个非入口页')

        done_prefixes = {uid.split('::')[0] for uid in ex._done}
        self.assertTrue(done_prefixes & others,
                        '非入口页的控件必须按**它自己的**结构签名记账，'
                        f'而不是入口页的；done={done_prefixes} others={others}')
        self.assertNotEqual(done_prefixes, {entry_struct},
                            '仍然全部记在入口页名下 → 签名没接住')

    def test_per_page_visited_is_not_always_zero(self):
        """★ C 报的症状③：覆盖度明细里每页 `visited` 恒为 0。"""
        ex, g = self._explore(5)
        per_page = ex.coverage.to_dict()['per_page']
        visited = {k: v.get('visited', 0) for k, v in per_page.items()}
        self.assertTrue(any(v > 0 for v in visited.values()),
                        f'没有任何一页登记过已访控件：{visited}')

    def test_entry_page_signature_is_not_frozen_into_the_loop(self):
        """★ 钉住机制本身：循环里拿到的签名必须随当前页变化。

        做法是直接对照 —— 入口页签名在 `_done` 里**不应该**独自承包所有记录。
        """
        ex, g = self._explore(4)
        entry_struct = next(st.structural_key for st in g.states.values()
                            if st.sid == 'S1')
        entry_done = [u for u in ex._done if u.startswith(entry_struct + '::')]
        self.assertLess(len(entry_done), len(ex._done),
                        '所有已访控件都挂在入口页名下 = 缺陷复现')


if __name__ == '__main__':
    unittest.main(verbosity=2)


# ================================================================ 页面路由（状态层）

class _CollidingSig(PageSignature):
    """把 `content_key` 固定成给定值 —— 只为构造「同一个 key、不同路由」这个场景。

    真实 `PageSignature` 的 `content_key` 把 `page_path` 也算进哈希，
    两条路由必然是两把 key，所以这个分支在生产路径上够不着。
    """

    def __init__(self, key, page_path=''):
        super().__init__(content='c', structural='s')
        self._key = key
        self.page_path = page_path

    @property
    def content_key(self):
        return self._key


class TestPagePathReachesTheArtifact(_SimCase):
    """设备自报的页面路由要从签名层一路透到**落盘产物**。

    只把字段挂在 dataclass 上是不够的：消费方（页级归并、覆盖率报告）
    读的是 `save_graph()` 落下的 JSON —— 序列化那一环断了，字段等于没有，
    而字段本身还在，读代码时看不出来。
    """

    def _page(self, path='pages/Index'):
        root = _tree([_node('Button', 'b', 'x', _b(10, 10, 100, 100))])
        root['attributes']['pagePath'] = path
        return root

    def _sig(self, path='pages/Index', bundle='com.demo.app'):
        return build_page_signature(parse_layout(self._page(path)), bundle=bundle)

    def test_signature_carries_the_device_reported_route(self):
        self.assertEqual(self._sig().page_path, 'pages/Index')

    def test_route_participates_in_both_signatures(self):
        """路由是**身份**的一部分：换个页面就该是另一个 key。"""
        a, b = self._sig('pages/A'), self._sig('pages/B')
        self.assertNotEqual(a.content_key, b.content_key)
        self.assertNotEqual(a.structural_key, b.structural_key)

    def test_state_layer_keeps_the_route(self):
        g = StateGraph()
        st = g.add_state(self._sig(), '首页')
        self.assertEqual(st.page_path, 'pages/Index')

    def test_to_dict_exports_the_route(self):
        g = StateGraph()
        st = g.add_state(self._sig(), '首页')
        self.assertIn('page_path', st.to_dict())
        self.assertEqual(st.to_dict()['page_path'], 'pages/Index')

    def test_saved_graph_carries_the_route(self):
        sim, d = self._sim_driver()
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False)
        ex.graph.add_state(self._sig(), '首页')
        path = ex.save_graph(os.path.join(self.tmp, 'g.json'))
        with open(path, encoding='utf-8') as fh:
            payload = json.load(fh)
        routes = [s.get('page_path') for s in payload['states']]
        self.assertIn('pages/Index', routes,
                      '落盘产物里读不到 page_path，消费方就只能拿 structural_key 反推')

    def test_legacy_bare_signature_is_still_accepted(self):
        """旧调用方式（裸字符串签名）不炸，路由留空。"""
        g = StateGraph()
        st = g.add_state('raw-signature', '旧式')
        self.assertEqual(st.page_path, '')
        self.assertIn('page_path', st.to_dict())

    def test_existing_state_backfills_an_empty_route(self):
        """`add_state` 命中已有 state 时要把**空的**路由补上。

        真实 `PageSignature` 把 `page_path` 算进 `content_key`，两条路由必然
        两个 key —— 所以"同 key、先空后有"在生产路径上够不着。这里用桩签名
        构造该情形，守的是**公开方法的契约**（`add_state` 不是私有方法）。
        """
        g = StateGraph()
        first = g.add_state(_CollidingSig('k-same', ''), '先到')
        self.assertEqual(first.page_path, '')
        again = g.add_state(_CollidingSig('k-same', 'pages/Index'), '后到')
        self.assertIs(again, first)
        self.assertEqual(first.page_path, 'pages/Index')
        self.assertEqual(first.visits, 2)

    def test_existing_route_is_not_overwritten(self):
        """非空则保留 —— 先到的那次可信观测不该被无声改写。"""
        g = StateGraph()
        first = g.add_state(_CollidingSig('k-same', 'pages/Index'), '先到')
        g.add_state(_CollidingSig('k-same', 'pages/Other'), '后到')
        self.assertEqual(first.page_path, 'pages/Index')

