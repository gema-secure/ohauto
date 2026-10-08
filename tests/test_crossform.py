"""跨形态差异比对 —— 单元测试。

**全部离线**，不需要设备、不需要 DevEco。三条数据来源：

1. 手工构造的最小控件树 —— 用于精确验证每一条判据的**正例与反例**
2. `sim.FakeHdc`（响应式 + 靶子页）—— 验证「采集→比对」全链路
3. `fixtures/crossform/` 的真机样本 —— 验证解析器与判定基准不自相矛盾

★ 本文件的核心关切不是「功能写没写」，而是**判据对不对**。
所以每个测试都显式写出「为什么这个边界是这样」，并且正例反例成对出现 ——
只测正例的测试在跨形态场景里毫无价值（真问题往往藏在"我以为不算"的边界上）。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.crossform import (                                   # noqa: E402
    CompareReport, DiffKind, Element, IdentityPolicy, Severity,
    collect_elements, compare_forms, geometry_delta, identity_of)
from ohauto.crossform_report import (                            # noqa: E402
    to_html, to_json, to_markdown, write_all)
from ohauto.devices import FormProfile                           # noqa: E402
from ohauto.layout import Rect, parse_layout                     # noqa: E402

FIXTURE_DIR = os.path.join(HERE, 'fixtures', 'crossform')


# ---------------------------------------------------------------- 构造工具

def node(type_, cid='', text='', bounds=None, clickable=True,
         visible=True, descr='', children=None):
    """造一个 dumpLayout 风格的节点 dict。"""
    a = {'type': type_, 'id': cid, 'text': text, 'clickable':
         str(clickable).lower(), 'visible': str(visible).lower()}
    if bounds is not None:
        a['bounds'] = f'[{bounds[0]},{bounds[1]}][{bounds[2]},{bounds[3]}]'
    if descr:
        a['descr'] = descr
    n = {'attributes': a}
    if children:
        n['children'] = children
    return n


def tree(w, h, kids, root_id='root'):
    """造一棵带根节点的控件树。"""
    return {'attributes': {'type': 'Root', 'id': root_id,
                           'bounds': f'[0,0][{w},{h}]', 'visible': 'true'},
            'children': list(kids)}


def profile(w, h, name='p', kind='phone', cutouts=(), **kw):
    return FormProfile(name=name, device='Test', kind=kind,
                       width=w, height=h, cutouts=cutouts, **kw)


def parse(t):
    return parse_layout(t)


# ============================================================ 1. 身份策略


class TestIdentity(unittest.TestCase):
    """身份 key 三级降级的正确性 —— 这是跨形态比对能不能工作的前提。"""

    def setUp(self):
        self.pol = IdentityPolicy()

    def _one(self, t, type_=None):
        """按类型取身份；不指定类型时取第一个非根**叶子**节点。

        注意不能用「第一个非根节点」—— 先序遍历下容器（Row/Column）
        会排在叶子之前，拿到的是容器的身份，测试会莫名其妙失败。
        """
        root = parse(t)
        nodes = [n for n in root.walk() if n.parent is not None]
        if type_:
            nodes = [n for n in nodes if n.type == type_]
        else:
            nodes = [n for n in nodes if not n.children]
        return identity_of(nodes[0], self.pol) if nodes else None

    def test_id_wins_over_text(self):
        # id 是最高优先级 —— 开发者给的代码标识最稳定
        t = tree(100, 100, [node('Button', 'btn_a', '登录',
                                 (0, 0, 10, 10))])
        self.assertEqual(self._one(t), 'id:btn_a')

    def test_text_used_when_no_id(self):
        t = tree(100, 100, [node('Button', '', '登录', (0, 0, 10, 10))])
        self.assertEqual(self._one(t), 'tp:Button|登录')

    def test_descr_used_as_text_fallback(self):
        # 图标按钮常常只有 descr、没有 text —— 它同样是稳定语义标识
        t = tree(100, 100, [node('Image', '', '', (0, 0, 10, 10),
                                 descr='微信快捷登录')])
        self.assertEqual(self._one(t), 'tp:Image|微信快捷登录')

    def test_hierarchy_is_last_resort(self):
        # 无 id、无文本 → 退到「类型@父容器类型链」
        inner = node('Button', '', '', (0, 0, 10, 10))
        row = node('Row', '', '', (0, 0, 50, 50), children=[inner])
        t = tree(100, 100, [row])
        self.assertEqual(self._one(t, 'Button'), 'hp:Button@Root>Row')

    def test_hierarchy_truncated_by_max_depth(self):
        # 路径深度可配，超深时截断 —— 防止深层嵌套让身份彻底失稳
        pol = IdentityPolicy(max_path_depth=1)
        inner = node('Button', '', '', (0, 0, 10, 10))
        row = node('Row', '', '', (0, 0, 50, 50), children=[inner])
        col = node('Column', '', '', (0, 0, 60, 60), children=[row])
        t = tree(100, 100, [col])
        root = parse(t)
        btn = [n for n in root.walk() if n.type == 'Button'][0]
        # 只保留最近 1 层父容器（Row），不要再往上到 Column/Root
        self.assertEqual(identity_of(btn, pol), 'hp:Button@Row')

    def test_nothing_usable_returns_empty(self):
        # 三样都没有 → 空串（调用方跳过，不参与比对），不抛异常
        t = tree(100, 100, [node('Button', '', '', (0, 0, 10, 10))])
        pol = IdentityPolicy(use_id=True, use_text=True,
                             use_hierarchy=False)
        root = parse(t)
        btn = [n for n in root.walk() if n.type == 'Button'][0]
        self.assertEqual(identity_of(btn, pol), '')

    def test_fallback_order_reflects_switches(self):
        pol = IdentityPolicy(use_id=False, use_text=True,
                             use_hierarchy=True)
        self.assertEqual(pol.fallback_order(), ('text', 'hierarchy'))
        pol2 = IdentityPolicy(use_text=False)
        self.assertEqual(pol2.fallback_order(), ('id', 'hierarchy'))

    def test_text_only_policy_ignores_id(self):
        # 应用 id 大面积重复/无意义时，可以关掉 id 只用文本
        pol = IdentityPolicy(use_id=False)
        t = tree(100, 100, [node('Button', 'btn_a', '登录',
                                 (0, 0, 10, 10))])
        root = parse(t)
        btn = [n for n in root.walk() if n.type == 'Button'][0]
        self.assertEqual(identity_of(btn, pol), 'tp:Button|登录')


# ============================================================ 2. 元素收集


class TestCollectElements(unittest.TestCase):

    def test_invisible_excluded_by_default(self):
        t = tree(100, 100, [
            node('Button', 'a', 'A', (0, 0, 10, 10), visible=True),
            node('Button', 'b', 'B', (0, 0, 10, 10), visible=False),
        ])
        got = collect_elements(parse(t), IdentityPolicy())
        self.assertIn('id:a', got)
        self.assertNotIn('id:b', got)

    def test_invisible_included_when_disabled(self):
        t = tree(100, 100, [
            node('Button', 'a', 'A', (0, 0, 10, 10), visible=True),
            node('Button', 'b', 'B', (0, 0, 10, 10), visible=False),
        ])
        got = collect_elements(parse(t),
                               IdentityPolicy(only_visible=False))
        self.assertIn('id:b', got)

    def test_duplicate_identity_keeps_first(self):
        # 列表项复用同一 id 时保留第一个 —— 不合并（合并会造出不存在的元素）
        t = tree(100, 100, [
            node('ListItem', 'row', 'X', (0, 0, 10, 10)),
            node('ListItem', 'row', 'Y', (0, 20, 10, 30)),
        ])
        got = collect_elements(parse(t), IdentityPolicy())
        # 根节点（id:root）也在映射里，所以按 id:row 单独断言
        self.assertEqual(got['id:row'].bounds, (0, 0, 10, 10))

    def test_containers_excluded_when_flag_off(self):
        # 容器 = 类型在 CONTAINER_TYPES 里 **且** 不可点击。
        # 造数据时 Row 必须 clickable=False —— 否则它不是容器而是控件，
        # 被过滤掉反而不对（可点击的 Row 是有交互语义的，该参与比对）。
        inner = node('Button', 'btn', 'OK', (0, 0, 10, 10))
        row = node('Row', '', '', (0, 0, 50, 50), clickable=False,
                   children=[inner])
        t = tree(100, 100, [row])
        got = collect_elements(parse(t),
                               IdentityPolicy(include_containers=False))
        self.assertIn('id:btn', got)
        self.assertFalse(any(e.node.type == 'Row' for e in got.values()))

    def test_clickable_container_is_kept(self):
        # ★ 反例：可点击的 Row 有交互语义，即使开了「排除容器」也要保留 ——
        # 它是控件，不是布局盒子。这个边界决定了「排除容器」会不会误杀
        # 那些用可点击 Row 当按钮的应用。
        inner = node('Button', 'btn', 'OK', (0, 0, 10, 10))
        row = node('Row', 'row_clickable', '', (0, 0, 50, 50),
                   clickable=True, children=[inner])
        t = tree(100, 100, [row])
        got = collect_elements(parse(t),
                               IdentityPolicy(include_containers=False))
        self.assertIn('id:row_clickable', got)

    def test_interactive_only(self):
        t = tree(100, 100, [
            node('Button', 'btn', 'OK', (0, 0, 10, 10)),
            node('Image', 'img', '', (0, 0, 10, 10), clickable=False),
        ])
        got = collect_elements(parse(t),
                               IdentityPolicy(only_interactive=True))
        self.assertIn('id:btn', got)
        self.assertNotIn('id:img', got)

    def test_parent_rect_captured(self):
        inner = node('Button', 'btn', 'OK', (10, 10, 20, 20))
        row = node('Row', 'row', '', (0, 0, 50, 50), children=[inner])
        got = collect_elements(parse(tree(100, 100, [row])),
                               IdentityPolicy())
        self.assertEqual(got['id:btn'].parent_rect.left, 0)


# ============================================================ 3. 元素缺失


class TestMissing(unittest.TestCase):

    def _cmp(self, items_a, items_b, **pol):
        a = parse(tree(200, 200, items_a))
        b = parse(tree(200, 200, items_b))
        return compare_forms((a, profile(200, 200, 'A'), 'A'),
                             (b, profile(200, 200, 'B'), 'B'),
                             policy=IdentityPolicy(**pol) or None)

    def test_detects_missing_element(self):
        rep = self._cmp([node('Button', 'btn_a', 'A', (0, 0, 10, 10))], [])
        self.assertEqual(rep.counts()['MISSING'], 1)
        self.assertEqual(rep.by_kind(DiffKind.MISSING)[0].identity,
                         'id:btn_a')

    def test_same_element_not_reported(self):
        # 两形态都有同一元素 → 不该报缺失
        n = [node('Button', 'btn_a', 'A', (0, 0, 10, 10))]
        rep = self._cmp(n, n)
        self.assertEqual(rep.counts()['MISSING'], 0)

    def test_missing_is_high_severity(self):
        rep = self._cmp([node('Button', 'btn_a', 'A', (0, 0, 10, 10))], [])
        self.assertIs(rep.by_kind(DiffKind.MISSING)[0].severity,
                      Severity.HIGH)
        self.assertTrue(rep.has_high())

    def test_extra_element_in_target_not_reported(self):
        # 目标侧多出来的元素不是「缺失」—— 缺失是有方向的
        rep = self._cmp([], [node('Button', 'btn_new', 'N', (0, 0, 10, 10))])
        self.assertEqual(rep.counts()['MISSING'], 0)

    def test_baseline_bounds_recorded(self):
        rep = self._cmp([node('Button', 'btn_a', 'A', (5, 6, 15, 16))], [])
        d = rep.by_kind(DiffKind.MISSING)[0]
        self.assertEqual(d.baseline_bounds, (5, 6, 15, 16))
        self.assertIsNone(d.target_bounds)


# ============================================================ 4. 越界


class TestOutOfScreen(unittest.TestCase):
    """越界判据：**超出屏幕边界**，按裁切像素分级。"""

    def _cmp(self, w_a, h_a, kids_a, w_b, h_b, kids_b, **pol):
        a = parse(tree(w_a, h_a, kids_a))
        b = parse(tree(w_b, h_b, kids_b))
        return compare_forms((a, profile(w_a, h_a, 'A'), 'A'),
                             (b, profile(w_b, h_b, 'B'), 'B'),
                             policy=IdentityPolicy(**pol) or None)

    def test_fully_outside_is_high_severity(self):
        # 元素与屏幕完全无交集 → 高严重度（用户完全看不到）
        kids = [node('Button', 'btn', 'B', (300, 0, 400, 50))]
        rep = self._cmp(500, 500, kids, 100, 500, kids)
        d = rep.by_kind(DiffKind.OUT_OF_SCREEN)[0]
        self.assertIs(d.severity, Severity.HIGH)
        self.assertTrue(d.evidence['fully_invisible'])

    def test_partially_clipped_is_medium(self):
        # 元素比屏幕大、底部被切 → 中严重度，且记录裁切像素
        kids = [node('Button', 'btn', 'B', (0, 0, 100, 600))]
        rep = self._cmp(100, 600, kids, 100, 200, kids)
        d = rep.by_kind(DiffKind.OUT_OF_SCREEN)[0]
        self.assertIs(d.severity, Severity.MEDIUM)
        self.assertFalse(d.evidence['fully_invisible'])
        self.assertEqual(d.evidence['clipped_px']['bottom'], 400)

    def test_element_inside_screen_not_reported(self):
        # ★ 反例：元素完全在屏内 → 绝不能报越界
        kids = [node('Button', 'btn', 'B', (10, 10, 90, 90))]
        rep = self._cmp(100, 100, kids, 100, 100, kids)
        self.assertEqual(rep.counts()['OUT_OF_SCREEN'], 0)

    def test_clipped_px_breakdown_by_side(self):
        # 四条边分别统计，报告里要能看出是哪边被切
        kids = [node('Button', 'btn', 'B', (-10, -20, 110, 130))]
        rep = self._cmp(100, 100, kids, 100, 100, kids)
        d = rep.by_kind(DiffKind.OUT_OF_SCREEN)[0]
        cp = d.evidence['clipped_px']
        self.assertEqual(cp['left'], 10)
        self.assertEqual(cp['top'], 20)
        self.assertEqual(cp['right'], 10)
        self.assertEqual(cp['bottom'], 30)

    def test_root_node_never_reported(self):
        # ★ 根节点 = 屏幕本身，屏幕变了它必然"超出自己"，这是定义使然
        kids = [node('Button', 'btn', 'B', (0, 0, 50, 50))]
        rep = self._cmp(500, 500, kids, 100, 100, kids)
        self.assertFalse(
            any(d.identity == 'id:root'
                for d in rep.by_kind(DiffKind.OUT_OF_SCREEN)))

    def test_out_of_screen_suppresses_overflow(self):
        # ★ 去重：越界的元素不再报溢出（同一根因的两种表现）
        kids = [node('Button', 'btn', 'B', (300, 0, 400, 50))]
        rep = self._cmp(500, 500, kids, 100, 500, kids)
        # 该元素既越界又溢出父容器，但只应报越界
        ids = [d.identity for d in rep.by_kind(DiffKind.OVERFLOW)]
        self.assertNotIn('id:btn', ids)


# ============================================================ 5. 不可达


class TestUnreachable(unittest.TestCase):

    def _cmp(self, prof_b, kids_a, kids_b):
        a = parse(tree(prof_b.width, prof_b.height, kids_a))
        b = parse(tree(prof_b.width, prof_b.height, kids_b))
        return compare_forms((a, profile(prof_b.width, prof_b.height, 'A'),
                              'A'),
                             (b, prof_b, 'B'))

    def test_center_in_cutout_is_unreachable(self):
        # 元素与孔相交且中心点落在孔内 → 真正点不到，高严重度
        p = profile(500, 500, 'B', cutouts=((200, 200, 100, 100),))
        kids = [node('Button', 'btn', 'B', (150, 150, 350, 350))]
        rep = self._cmp(p, kids, kids)
        d = rep.by_kind(DiffKind.UNREACHABLE)
        self.assertEqual(len(d), 1)
        self.assertIs(d[0].severity, Severity.HIGH)
        self.assertEqual(d[0].evidence['cutout'], [200, 200, 100, 100])

    def test_overlap_but_center_outside_is_not_unreachable(self):
        # ★ 反例：元素压到孔上但中心在孔外 → 仍能点到，不算缺陷。
        # 这是最容易误报的边界：整块相交判据会把它算成问题，
        # 而实际上 uiInput 点的是中心点。
        # 挖孔 x∈[200,300], y∈[200,300]；元素中心 (150,150) 在孔外
        p = profile(500, 500, 'B', cutouts=((200, 200, 100, 100),))
        kids = [node('Button', 'btn', 'B', (0, 0, 300, 300))]
        rep = self._cmp(p, kids, kids)
        self.assertEqual(rep.counts()['UNREACHABLE'], 0)

    def test_no_cutouts_never_unreachable(self):
        p = profile(500, 500, 'B')          # 无挖孔
        kids = [node('Button', 'btn', 'B', (0, 0, 10, 10))]
        rep = self._cmp(p, kids, kids)
        self.assertEqual(rep.counts()['UNREACHABLE'], 0)

    def test_sliver_visible_area_is_unreachable(self):
        # ★ 可见区过小：元素被裁到只剩一条缝 —— 看得见但几乎点不中。
        # 元素 (0,-9900,100,100)：原面积 100×10000=1,000,000px²，
        # 屏上只剩 100×100=10,000px² → 1%，用更大裁切量取更明确的比例。
        p = profile(100, 100, 'B')
        kids = [node('Button', 'btn', 'B', (0, -99900, 100, 100))]
        rep = self._cmp(p, kids, kids)
        d = rep.by_kind(DiffKind.UNREACHABLE)
        self.assertEqual(len(d), 1)
        self.assertIn('可见区', d[0].detail)

    def test_small_but_fully_visible_is_not_unreachable(self):
        # ★ 反例（关键边界）：元素本来就小、但**完整可见**，不该报不可达。
        # 「小」不等于「点不中」，只有**被裁小**才是问题。
        # 早期只判「可见区占屏面积比例」，这条会误报。
        p = profile(500, 500, 'B')
        kids = [node('Button', 'btn', 'B', (0, 0, 10, 10))]
        rep = self._cmp(p, kids, kids)
        self.assertEqual(rep.counts()['UNREACHABLE'], 0)

    def test_enough_visible_area_not_unreachable(self):
        # 反例：被裁了但可见区仍够大 → 不算不可达
        p = profile(100, 100, 'B')
        kids = [node('Button', 'btn', 'B', (0, 0, 100, 400))]
        rep = self._cmp(p, kids, kids)
        self.assertEqual(rep.counts()['UNREACHABLE'], 0)

    def test_fully_offscreen_not_double_reported(self):
        # 完全不可见的元素只报「越界」，不该再报「不可达」
        p = profile(100, 100, 'B')
        kids = [node('Button', 'btn', 'B', (300, 300, 400, 400))]
        rep = self._cmp(p, kids, kids)
        self.assertEqual(rep.counts()['UNREACHABLE'], 0)
        self.assertEqual(rep.counts()['OUT_OF_SCREEN'], 1)


# ============================================================ 6. 溢出


class TestOverflow(unittest.TestCase):

    def _cmp(self, kids):
        a = parse(tree(500, 500, kids))
        b = parse(tree(500, 500, kids))
        return compare_forms((a, profile(500, 500, 'A'), 'A'),
                             (b, profile(500, 500, 'B'), 'B'))

    def test_child_beyond_parent_is_overflow(self):
        # 子元素右边超出父容器 → 溢出
        parent = node('Row', 'row', '', (0, 0, 100, 100),
                      children=[node('Button', 'btn', 'B', (0, 0, 150, 50))])
        rep = self._cmp([parent])
        d = rep.by_kind(DiffKind.OVERFLOW)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0].evidence['parent'], [0, 0, 100, 100])

    def test_child_inside_parent_not_reported(self):
        parent = node('Row', 'row', '', (0, 0, 100, 100),
                      children=[node('Button', 'btn', 'B', (10, 10, 90, 90))])
        rep = self._cmp([parent])
        self.assertEqual(rep.counts()['OVERFLOW'], 0)

    def test_zero_area_parent_skipped(self):
        # ★ 父容器 bounds 全 0 = 「没解析到父容器」，不是「父容器在原点且为 0」。
        # 不跳过的话任何元素都算溢出 —— 典型假阳性来源。
        p = profile(500, 500, 'B')
        inner = node('Button', 'btn', 'B', (10, 10, 50, 50))
        # 手工构造：父容器 bounds 全 0
        root = parse_layout({
            'attributes': {'type': 'Root', 'id': 'root',
                           'bounds': '[0,0][500,500]', 'visible': 'true'},
            'children': [{
                'attributes': {'type': 'Row', 'id': 'row',
                               'bounds': '[0,0][0,0]', 'visible': 'true'},
                'children': [inner]}]})
        rep = compare_forms((root, p, 'A'), (root, p, 'B'))
        ids = [d.identity for d in rep.by_kind(DiffKind.OVERFLOW)]
        self.assertNotIn('id:btn', ids)

    def test_root_child_is_not_overflow(self):
        # 根的直接子元素超出根 = 越界问题，不是溢出（根就是屏幕）
        kids = [node('Button', 'btn', 'B', (0, 0, 600, 50))]
        rep = self._cmp(kids)
        # 应报越界，不报溢出
        self.assertEqual(rep.counts()['OVERFLOW'], 0)
        self.assertEqual(rep.counts()['OUT_OF_SCREEN'], 1)


# ============================================================ 7. 报告对象


class TestCompareReport(unittest.TestCase):

    def _rep(self):
        a = parse(tree(500, 500, [
            node('Button', 'gone', 'G', (0, 0, 10, 10)),
            node('Button', 'ok', 'O', (0, 0, 10, 10)),
        ]))
        b = parse(tree(500, 500, [node('Button', 'ok', 'O', (0, 0, 10, 10))]))
        return compare_forms((a, profile(500, 500, 'A'), 'A'),
                             (b, profile(500, 500, 'B'), 'B'))

    def test_counts_always_has_four_keys(self):
        # ★ 四类都要在（含 0）—— 「查过了且为 0」与「没查」是两回事，
        # 0 值行是「已覆盖」的证据，验收依赖它
        c = self._rep().counts()
        self.assertEqual(set(c), {k.value for k in DiffKind})
        self.assertEqual(len(c), 4)

    def test_summary_contains_both_forms(self):
        s = self._rep().summary()
        self.assertIn('A', s)
        self.assertIn('B', s)

    def test_to_dict_round_trips_json(self):
        d = self._rep().to_dict()
        s = json.dumps(d, ensure_ascii=False)
        back = json.loads(s)
        self.assertEqual(back['counts']['MISSING'], 1)
        self.assertEqual(back['baseline']['size'], [500, 500])

    def test_differences_sorted_stably(self):
        # 同一份输入跑两次，差异顺序必须一致 —— 否则报告无法做 diff
        r1 = [d.identity for d in self._rep().differences]
        r2 = [d.identity for d in self._rep().differences]
        self.assertEqual(r1, r2)

    def test_warning_when_target_much_smaller(self):
        a = parse(tree(500, 500, [
            node('Button', f'b{i}', f'B{i}', (0, 0, 10, 10))
            for i in range(10)]))
        b = parse(tree(500, 500, [node('Button', 'b0', 'B0', (0, 0, 10, 10))]))
        rep = compare_forms((a, profile(500, 500, 'A'), 'A'),
                            (b, profile(500, 500, 'B'), 'B'))
        ratio_warnings = [w for w in rep.warnings if '元素数' in w]
        self.assertTrue(ratio_warnings)
        self.assertIn('元素数', ratio_warnings[0])

    def test_no_warning_on_comparable_forms(self):
        a = parse(tree(500, 500, [node('Button', 'x', 'X', (0, 0, 10, 10))]))
        b = parse(tree(500, 500, [node('Button', 'x', 'X', (0, 0, 10, 10))]))
        rep = compare_forms((a, profile(500, 500, 'A'), 'A'),
                            (b, profile(500, 500, 'B'), 'B'))
        # 2026-09-27 细化：本测试的意图是「元素数比例阈值不乱报警」；
        # 安全区未实测的能力缺口告警（2026-09-27 新增）是另一回事，
        # 由 test_capability_gap_warning_* 单独钉。
        ratio_warnings = [w for w in rep.warnings if '元素数' in w]
        self.assertEqual(ratio_warnings, [])

    def test_capability_gap_warning_when_safe_area_unmeasured(self):
        """★ 安全区未实测 → 不可达（挖孔）判据整个没生效，必须留痕。

        2026-09-27 修（评审中危）：原来这里只有注释「必须留痕」+ pass，
        报告会给人「该形态没有不可达问题」的错误印象 —— 红线⑤。
        """
        a = parse(tree(500, 500, [node('Button', 'x', 'X', (0, 0, 10, 10))]))
        b = parse(tree(500, 500, [node('Button', 'x', 'X', (0, 0, 10, 10))]))
        rep = compare_forms((a, profile(500, 500, 'A'), 'A'),
                            (b, profile(500, 500, 'B'), 'B'))
        self.assertTrue(any('判据本次未生效' in w for w in rep.warnings),
                        f'能力缺口必须写在报告告警里: {rep.warnings}')

    def test_no_capability_gap_warning_when_measured(self):
        """实测过安全区（status_bar_h>0）且确实无挖孔 → UNREACHABLE=0
        是「查过了且为 0」，不许告警（否则正常报告被噪音淹没）。"""
        a = parse(tree(500, 500, [node('Button', 'x', 'X', (0, 0, 10, 10))]))
        b = parse(tree(500, 500, [node('Button', 'x', 'X', (0, 0, 10, 10))]))
        rep = compare_forms(
            (a, profile(500, 500, 'A', status_bar_h=24), 'A'),
            (b, profile(500, 500, 'B', status_bar_h=24), 'B'))
        self.assertEqual(rep.warnings, [])


# ============================================================ 8. 几何位移


class TestGeometryDelta(unittest.TestCase):

    def test_computes_dx_dy(self):
        a = parse(tree(500, 500, [node('Button', 'btn', 'B', (10, 20, 60, 40))]))
        b = parse(tree(500, 500, [node('Button', 'btn', 'B', (15, 25, 65, 45))]))
        d = geometry_delta((a, profile(500, 500, 'A')),
                           (b, profile(500, 500, 'B')))
        self.assertEqual(d['id:btn']['dx'], 5)
        self.assertEqual(d['id:btn']['dy'], 5)

    def test_kept_left_detects_alignment_preserved(self):
        # 左对齐的元素切形态后左边缘不变 → kept_left True
        a = parse(tree(500, 500, [node('Button', 'btn', 'B', (10, 20, 60, 40))]))
        b = parse(tree(500, 900, [node('Button', 'btn', 'B', (10, 200, 60, 220))]))
        d = geometry_delta((a, profile(500, 500, 'A')),
                           (b, profile(500, 900, 'B')))
        self.assertTrue(d['id:btn']['kept_left'])
        self.assertFalse(d['id:btn']['kept_top'])

    def test_center_ratio_preserved_for_centered_element(self):
        # 居中元素：中心点占屏宽比例应保持
        a = parse(tree(100, 100, [node('Button', 'btn', 'B', (40, 0, 60, 10))]))
        b = parse(tree(400, 100, [node('Button', 'btn', 'B', (190, 0, 210, 10))]))
        d = geometry_delta((a, profile(100, 100, 'A')),
                           (b, profile(400, 100, 'B')))
        self.assertTrue(d['id:btn']['kept_center_x'])

    def test_missing_element_absent_from_delta(self):
        a = parse(tree(500, 500, [node('Button', 'gone', 'G', (0, 0, 10, 10))]))
        b = parse(tree(500, 500, []))
        d = geometry_delta((a, profile(500, 500, 'A')),
                           (b, profile(500, 500, 'B')))
        self.assertNotIn('id:gone', d)

    def test_zero_width_ratio_is_none_not_crash(self):
        # 基准宽为 0 时比例没法算 → None（不猜），不抛 ZeroDivisionError
        a = parse(tree(500, 500, [node('Button', 'btn', 'B', (10, 0, 10, 10))]))
        b = parse(tree(500, 500, [node('Button', 'btn', 'B', (10, 0, 50, 10))]))
        d = geometry_delta((a, profile(500, 500, 'A')),
                           (b, profile(500, 500, 'B')))
        self.assertIsNone(d['id:btn']['width_ratio'])


# ============================================================ 9. 与 sim 集成


class TestSimIntegration(unittest.TestCase):
    """「采集 → 比对」全链路：用 `sim.FakeHdc` 离线跑，不需要设备。

    ★ 这批测试是跨形态能力能不能进 CI 的关键 ——
    若它们离线跑不出差异，就说明差异报告只在真机上有意义，
    那么「回归测试」这件事就落空了。
    """

    SCREENS = [
        {'index': 0, 'power_status': 'POWER_STATUS_OFF', 'backlight': 1,
         'width': 2416, 'height': 2210},
        {'index': 1, 'power_status': 'POWER_STATUS_OFF', 'backlight': 4,
         'width': 1080, 'height': 2444},
    ]

    def _capture(self, state, page, responsive=True):
        from ohauto.sim import FakeHdc
        h = FakeHdc(start_page=page, screen=(1080, 2340),
                    screens=[dict(s) for s in self.SCREENS],
                    responsive=responsive)
        h.set_folded_state(state)
        p = h.dump_layout()
        layout = h.run(['shell', 'cat', p]).stdout
        screens = h.shell('hidumper -s RenderService -a screen').stdout
        return parse(layout), screens

    def test_responsive_page_has_no_diff(self):
        # 响应式页面（login）：横向等比缩放、纵向不动 → 通常无差异
        ta, _ = self._capture('open', 'login')
        tb, _ = self._capture('close', 'login')
        rep = compare_forms((ta, profile(2416, 2210, 'open'), 'open'),
                            (tb, profile(1080, 2444, 'close'), 'close'))
        self.assertEqual(sum(rep.counts().values()), 0)

    def test_static_page_detects_out_of_screen(self):
        # ★ 靶子页面（写死坐标）在展开态下必然越界
        ta, _ = self._capture('close', 'static_fixed')
        tb, _ = self._capture('open', 'static_fixed')
        rep = compare_forms((ta, profile(1080, 2444, 'close'), 'close'),
                            (tb, profile(2416, 2210, 'open'), 'open'))
        self.assertGreater(rep.counts()['OUT_OF_SCREEN'], 0)
        self.assertTrue(rep.has_high())

    def test_static_page_bottom_dock_fully_invisible(self):
        # 贴外屏底边的按钮在内屏（更矮）下完全不可见 → 高严重度
        ta, _ = self._capture('close', 'static_fixed')
        tb, _ = self._capture('open', 'static_fixed')
        rep = compare_forms((ta, profile(1080, 2444, 'close'), 'close'),
                            (tb, profile(2416, 2210, 'open'), 'open'))
        hits = [d for d in rep.by_kind(DiffKind.OUT_OF_SCREEN)
                if d.identity == 'id:btn_dock_bottom']
        self.assertEqual(len(hits), 1)
        self.assertIs(hits[0].severity, Severity.HIGH)

    def test_control_group_icon_not_reported(self):
        # ★ 对照组：右上角小图标在两形态下都在屏内，绝不该被报
        ta, _ = self._capture('close', 'static_fixed')
        tb, _ = self._capture('open', 'static_fixed')
        rep = compare_forms((ta, profile(1080, 2444, 'close'), 'close'),
                            (tb, profile(2416, 2210, 'open'), 'open'))
        self.assertFalse(any(d.identity == 'id:icon_ok'
                             for d in rep.differences))

    def test_non_responsive_flag_disables_relayout(self):
        # 关掉响应式重排 → 两形态坐标完全一样 → 无差异
        ta, _ = self._capture('open', 'login', responsive=False)
        tb, _ = self._capture('close', 'login', responsive=False)
        rep = compare_forms((ta, profile(2416, 2210, 'open'), 'open'),
                            (tb, profile(1080, 2444, 'close'), 'close'))
        self.assertEqual(sum(rep.counts().values()), 0)

    def test_screens_parse_to_expected_sizes(self):
        # 屏幕信息经真机解析器 → 点亮屏尺寸必须正确
        from tools.emulator_cli import parse_screen_info
        _, txt_open = self._capture('open', 'login')
        _, txt_close = self._capture('close', 'login')
        s_open = [s for s in parse_screen_info(txt_open)
                  if 'ON' in s['power_status']]
        s_close = [s for s in parse_screen_info(txt_close)
                   if 'ON' in s['power_status']]
        self.assertEqual((s_open[0]['width'], s_open[0]['height']),
                         (2416, 2210))
        self.assertEqual((s_close[0]['width'], s_close[0]['height']),
                         (1080, 2444))


# ============================================================ 10. 报告渲染


class TestReportRender(unittest.TestCase):

    def setUp(self):
        a = parse(tree(500, 500, [
            node('Button', 'gone', 'G', (0, 0, 10, 10)),
            node('Button', 'oos', 'O', (0, 0, 10, 10)),
        ]))
        b = parse(tree(200, 500, [
            node('Button', 'oos', 'O', (0, 0, 300, 10)),
        ]))
        self.rep = compare_forms((a, profile(500, 500, 'A_open'), 'A_open'),
                                 (b, profile(200, 500, 'B_close'),
                                  'B_close'))
        self.out = os.path.join(ROOT, '_out', 'test_crossform')
        os.makedirs(self.out, exist_ok=True)

    def test_markdown_lists_all_four_kinds(self):
        # 四类都出现在汇总表里（含 0），这是「已覆盖」的证据
        p = to_markdown(self.rep, os.path.join(self.out, 'r.md'))
        with open(p, encoding='utf-8') as f:
            md = f.read()
        for label in ('元素缺失', '越界', '不可达', '溢出'):
            self.assertIn(label, md)

    def test_markdown_has_evidence_columns(self):
        p = to_markdown(self.rep, os.path.join(self.out, 'r.md'))
        with open(p, encoding='utf-8') as f:
            md = f.read()
        self.assertIn('基准 bounds', md)
        self.assertIn('目标 bounds', md)

    def test_markdown_zero_diff_says_verified_caveat(self):
        # ★ 0 差异时必须提示「未检出≠没问题」——
        # 否则读的人会把「没测出来」当成「没问题」，这在验收语境下是致命的
        same = parse(tree(500, 500, [node('Button', 'x', 'X', (0, 0, 10, 10))]))
        rep = compare_forms((same, profile(500, 500, 'A'), 'A'),
                            (same, profile(500, 500, 'B'), 'B'))
        p = to_markdown(rep, os.path.join(self.out, 'zero.md'))
        with open(p, encoding='utf-8') as f:
            md = f.read()
        self.assertIn('未检出', md)
        self.assertIn('不等于', md)

    def test_html_escapes_dangerous_text(self):
        # 报告要能安全打开：元素文本里的尖括号必须被转义
        a = parse(tree(500, 500, [
            node('Button', 'xss', '<script>alert(1)</script>', (0, 0, 10, 10))]))
        b = parse(tree(500, 500, []))
        rep = compare_forms((a, profile(500, 500, 'A'), 'A'),
                            (b, profile(500, 500, 'B'), 'B'))
        p = to_html(rep, os.path.join(self.out, 'xss.html'))
        with open(p, encoding='utf-8') as f:
            h = f.read()
        self.assertNotIn('<script>alert(1)</script>', h)
        self.assertIn('&lt;script&gt;', h)

    def test_html_has_no_external_resources(self):
        # 离线可看：不能引用任何外部 css/js/字体
        p = to_html(self.rep, os.path.join(self.out, 'r.html'))
        with open(p, encoding='utf-8') as f:
            h = f.read()
        self.assertNotIn('http://', h)
        self.assertNotIn('https://', h)

    def test_json_matches_report_structure(self):
        p = to_json(self.rep, os.path.join(self.out, 'r.json'))
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        self.assertEqual(set(d['counts']), {k.value for k in DiffKind})
        self.assertIn('differences', d)
        self.assertIn('warnings', d)

    def test_write_all_produces_three_files(self):
        got = write_all(self.rep, self.out, stem='all3')
        self.assertEqual(set(got), {'json', 'markdown', 'html'})
        for path in got.values():
            self.assertTrue(os.path.exists(path), path)

    def test_criteria_text_matches_actual_judgement(self):
        # ★ 判据文案必须与实现一致 —— 文案错会让人按错误规则复核引擎。
        # 这里锁死「越界」的判据描述包含分级语义（曾因忘了同步文案而误导）。
        p = to_markdown(self.rep, os.path.join(self.out, 'r.md'))
        with open(p, encoding='utf-8') as f:
            md = f.read()
        self.assertIn('超出屏幕边界', md)
        self.assertNotIn('bounds 与屏幕**完全无交集**（一点都看不到）', md)


# ============================================================ 11. 跑测驱动


class TestRunnerTool(unittest.TestCase):
    """`tools/crossform_run.py` 的离线路径。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import crossform_run
        self.mod = crossform_run

    def test_capture_offline_open_and_close(self):
        c_open = self.mod.capture_offline('open', 'x_open')
        c_close = self.mod.capture_offline('close', 'x_close')
        self.assertEqual((c_open.profile.width, c_open.profile.height),
                         (2416, 2210))
        self.assertEqual((c_close.profile.width, c_close.profile.height),
                         (1080, 2444))
        self.assertGreater(c_open.element_count, 1)

    def test_capture_dumps_reloadable_layout(self):
        # ★ 往返：落盘的 layout 必须能被 parse_layout 重新读回来。
        # 不成立的话「采集时解析一次、报告时再解析一次」会不一致。
        import tempfile
        c = self.mod.capture_offline('open', 'x_open', page='static_fixed')
        with tempfile.TemporaryDirectory() as d:
            paths = c.dump(d)
            with open(paths['layout'], encoding='utf-8') as f:
                again = parse_layout(f.read())
        self.assertEqual(sum(1 for _ in again.walk()),
                         c.element_count)

    def test_offline_static_page_reports_diffs(self):
        a = self.mod.capture_offline('close', 'close', page='static_fixed')
        b = self.mod.capture_offline('open', 'open', page='static_fixed')
        rep = self.mod.build_report(a, b)
        self.assertGreater(sum(rep.counts().values()), 0)

    def test_profile_from_screens_uses_measured_size(self):
        c = self.mod.capture_offline('open', 'x')
        p = self.mod.profile_from_screens(c.screens_text, (1, 1), 'n')
        # 必须用实测值（2416x2210），而不是传入的兜底值 (1,1)
        self.assertEqual((p.width, p.height), (2416, 2210))

    def test_main_offline_exit_code_zero_when_clean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rc = self.mod.main(['--offline', '--offline-page', 'login',
                                '--out', d, '--stem', 't1'])
        self.assertEqual(rc, 0)

    def test_main_offline_exit_code_nonzero_on_high_severity(self):
        # ★ 有缺失/不可达时返回非 0 —— 这样能直接接进 CI 当门禁
        #
        # 2026-09-27 反转：原来钉 rc == 2，锁的恰是缺陷行为 ——
        # 2 是「设备不在场」专用（docs/API手册.md §三），CI 拿到 2 会按
        # 「跳过」处理而不是门禁红，真失败被静默放过。改钉 1（未达标）。
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            rc = self.mod.main(['--offline', '--offline-page', 'static_fixed',
                                '--baseline', 'close', '--target', 'open',
                                '--out', d, '--stem', 't2'])
        self.assertEqual(rc, 1)


# ============================================================ 12. 真机夹具
#
# ★ 这一节和前面 11 节的性质不同：前面是「我构造场景、验证判据」，
#   这一节是「真机给了一份数据，验证引擎能解释它」。
#
# 夹具是 DevEco 模拟器 Mate X7 的**系统桌面**（SCBDesktop）在
# open / close 两态下的真实控件树（2026-09-18 采集）。
# 详见 tests/fixtures/crossform/README-matex7-desktop.md。
#
# 为什么必须有这一节：前面所有测试用的都是我自己造的树，
# 树长什么样由我的理解决定 —— 如果我对 dumpLayout 格式的理解本身错了，
# 全部测试照样全绿。真机夹具是**唯一能证伪这种系统性误解**的东西。
#
# ⚠️ 因此本节的数字（100 / 89 / 11 / 1）是**现象记录**，不是「期望的正确值」。
#    真机数据变了（换固件、换桌面版本）这些断言会失败 —— 那是**好事**，
#    说明引擎的行为确实跟着真实世界变了，该人来复核而不是悄悄放过。

class TestRealFixture(unittest.TestCase):
    """基于真机夹具的离线回归 —— 不需要设备。"""

    OPEN_LAYOUT = 'matex7_desktop_open_20260918.json'
    OPEN_SCREENS = 'matex7_desktop_open_screens_20260918.txt'
    CLOSE_LAYOUT = 'matex7_desktop_close_20260918.json'
    CLOSE_SCREENS = 'matex7_desktop_close_screens_20260918.txt'

    #: Mate X7 实测形态规格（内屏 / 外屏），写死是为了让「设备规格漂了」也报错
    OPEN_SIZE = (2416, 2210)
    CLOSE_SIZE = (1080, 2444)

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        try:
            import crossform_run as cr
        except ImportError as e:                      # pragma: no cover
            raise unittest.SkipTest(f'跑测驱动不可用: {e}')
        cls.cr = cr

        for name in (cls.OPEN_LAYOUT, cls.CLOSE_LAYOUT, cls.OPEN_SCREENS,
                     cls.CLOSE_SCREENS):
            if not os.path.exists(os.path.join(FIXTURE_DIR, name)):
                raise unittest.SkipTest(f'真机夹具缺失: {name}')

    # ------------------------------------------------------------ 载入工具

    @classmethod
    def _read(cls, name):
        with open(os.path.join(FIXTURE_DIR, name), encoding='utf-8') as f:
            return f.read()

    @classmethod
    def _form(cls, tag):
        """(控件树, 形态档案) —— 形态档案**只从 screens 实测原文推**。"""
        layout = parse(cls._read(f'matex7_desktop_{tag}_20260918.json'))
        screens = cls._read(f'matex7_desktop_{tag}_screens_20260918.txt')
        return layout, cls.cr.profile_from_screens(screens, (0, 0), tag)

    @classmethod
    def _report(cls):
        ro, po = cls._form('open')
        rc, pc = cls._form('close')
        return compare_forms((ro, po, 'open'), (rc, pc, 'close'))

    # ------------------------------------------------------ 夹具本身的解析

    def test_open_fixture_parses_to_expected_node_count(self):
        """展开态 100 个节点 —— 解析器没有把子节点吃掉。"""
        root, _ = self._form('open')
        self.assertEqual(sum(1 for _ in root.walk()), 100)

    def test_close_fixture_parses_to_expected_node_count(self):
        """折叠态 89 个节点。"""
        root, _ = self._form('close')
        self.assertEqual(sum(1 for _ in root.walk()), 89)

    def test_root_node_has_empty_type_and_id(self):
        """★ 真实 dumpLayout 的根节点 `type` / `id` **都是空字符串**。

        这条是真机教给我们的：早期一版按「根节点有 id」写的判根逻辑
        在这里会静默失效。引擎改成 `parent is None` 判根，本测试锁住这一点。
        """
        for tag in ('open', 'close'):
            root, _ = self._form(tag)
            self.assertEqual(root.type, '', f'{tag} 根节点 type 应为空串')
            self.assertEqual(root.id, '', f'{tag} 根节点 id 应为空串')
            self.assertIsNone(root.parent)

    def test_no_non_root_node_lacks_parent(self):
        """反向保证：只有根节点没有父节点 —— 否则 `parent is None` 判根会误伤。"""
        root, _ = self._form('open')
        orphans = [n for n in root.walk()
                   if n is not root and n.parent is None]
        self.assertEqual(orphans, [])

    # -------------------------------------------------- 屏幕解析（形态真源）

    def test_open_profile_comes_from_lit_inner_screen(self):
        """展开态：内屏点亮 → 2416x2210。"""
        _, prof = self._form('open')
        self.assertEqual((prof.width, prof.height), self.OPEN_SIZE)

    def test_close_profile_comes_from_lit_outer_screen(self):
        """折叠态：外屏点亮 → 1080x2444。"""
        _, prof = self._form('close')
        self.assertEqual((prof.width, prof.height), self.CLOSE_SIZE)

    def test_both_fixtures_declare_a_lit_screen(self):
        """两种形态都必须有且只有一块 ON 的屏 —— 否则形态判定无从谈起。"""
        from tools.emulator_cli import parse_screen_info    # noqa: PLC0415
        for tag, want in (('open', self.OPEN_SIZE),
                          ('close', self.CLOSE_SIZE)):
            screens = parse_screen_info(
                self._read(f'matex7_desktop_{tag}_screens_20260918.txt'))
            lit = [s for s in screens
                   if 'ON' in str(s.get('power_status', '')).upper()]
            self.assertEqual(len(lit), 1,
                             f'{tag} 应恰好有一块点亮的屏，实际 {len(lit)}')
            self.assertEqual((lit[0]['width'], lit[0]['height']), want)

    def test_resolution_is_constant_and_only_power_status_swaps(self):
        """★ 折叠态**不改分辨率**，只是两块屏的 powerStatus 互换。

        内屏恒 2416x2210、外屏恒 1080x2444，两个夹具的 screens 原文里
        都能看到这两组数。谁把「分辨率变了」当判据，谁就会在真机上失灵。
        """
        from tools.emulator_cli import parse_screen_info    # noqa: PLC0415
        for tag in ('open', 'close'):
            screens = parse_screen_info(
                self._read(f'matex7_desktop_{tag}_screens_20260918.txt'))
            by_size = {(s['width'], s['height']): s for s in screens}
            self.assertIn(self.OPEN_SIZE, by_size)
            self.assertIn(self.CLOSE_SIZE, by_size)

    # ------------------------------------------------------------ 比对结果

    def test_compare_runs_and_counts_match_recorded_run(self):
        """★ 真机实测基线：缺失 11 / 越界 0 / 不可达 0 / 溢出 1。

        与 `_out/crossform/matex7_real/crossform.json` 一致。
        数字变了说明引擎行为变了 —— 必须人复核，不能让 CI 悄悄绿过去。
        """
        rep = self._report()
        self.assertEqual(
            rep.counts(),
            {'MISSING': 11, 'OUT_OF_SCREEN': 0,
             'UNREACHABLE': 0, 'OVERFLOW': 1})

    def test_element_identity_counts(self):
        """展开态 89 个可识别身份、折叠态 81 个 —— 身份策略没把树压瘪。"""
        rep = self._report()
        self.assertEqual(rep.baseline_elements, 89)
        self.assertEqual(rep.target_elements, 81)

    def test_report_has_high_severity_so_ci_gate_closes(self):
        """11 处 MISSING 都是 HIGH → `has_high()` 必须为真，CI 门禁才拦得住。"""
        rep = self._report()
        self.assertTrue(rep.has_high())
        self.assertEqual(len(rep.by_kind(DiffKind.MISSING)), 11)

    def test_summary_line_matches_documented_output(self):
        """报告摘要行与 README 记录的一致（给人看的那一行）。"""
        rep = self._report()
        self.assertEqual(
            rep.summary(),
            'open (2416x2210) -> close (1080x2444) | '
            '缺失 11 / 越界 0 / 不可达 0 / 溢出 1')

    def test_no_spurious_warning_on_real_data(self):
        """真机这份数据不该触发**阈值类**告警 —— 有告警说明阈值定得不合理。

        2026-09-27 注：夹具的 profile 没实测过安全区（status_bar/nav_bar
        全 0），会带一条**能力缺口**告警（挖孔判据未生效）—— 那不是
        误报，是事实陈述，由 test_capability_gap_warning_* 单独钉。
        """
        rep = self._report()
        self.assertEqual([w for w in rep.warnings if '元素数' in w], [])

    # ------------------------------------------------- 差异内容的成因核对

    def test_swiper_second_page_is_reported_missing(self):
        """① 桌面第二页网格：展开态 [1108,170,2162,1933]，窄屏下整页不渲染。"""
        rep = self._report()
        hit = [d for d in rep.differences
               if d.identity == 'id:SwiperPage_Grid_WorkSpace_1']
        self.assertEqual(len(hit), 1)
        self.assertIs(hit[0].kind, DiffKind.MISSING)
        self.assertEqual(hit[0].baseline_bounds, (1108, 170, 2162, 1933))
        # 该网格左边界 1108 已经超过外屏宽 1080 → 必然出屏
        self.assertGreater(hit[0].baseline_bounds[0], self.CLOSE_SIZE[0])

    def test_dock_items_are_reported_missing(self):
        """② 底部 Dock 栏：展开态横铺在 x∈[721,1491]，外屏宽仅 1080。"""
        rep = self._report()
        ids = {d.identity for d in rep.differences
               if d.identity.startswith('id:DOCK_')}
        self.assertEqual(
            ids,
            {'id:DOCK_RESIDENT_BG', 'id:DOCK_recent_BG',
             'id:DOCK_recent_DIVIDER'})
        resident = next(d for d in rep.differences
                        if d.identity == 'id:DOCK_RESIDENT_BG')
        self.assertEqual(resident.baseline_bounds, (721, 2079, 1222, 2317))

    def test_package_qualified_ids_appear_in_missing_list(self):
        """★ 裸 id 匹配的已知短板：真实 id 里带包名 + 实例号。

        如 `AppIcon_Image_com.huawei.hmos.photos...photos0_436207618_1`，
        切形态后可能被重新生成 → 身份对不上 → 报成「缺失」。
        本测试**如实锁住这个现象**，不粉饰：它是短板，不是特性。
        """
        rep = self._report()
        blobs = [d.identity for d in rep.differences
                 if d.identity.startswith('id:AppIcon_Image_')]
        self.assertTrue(blobs, '应至少有一条带包名的长 id 缺失')
        self.assertTrue(any('com.huawei.hmos.photos' in b for b in blobs))
        # 同一个图库图标，它的**浮层图**和**容器**各报一条 → 说明
        # 「一个视觉元素在报告里占两行」是真实现象（按 id 匹配的必然结果，
        # 因为两者 id 不同但都带同一串实例号）。这里如实锁住，不粉饰。
        whole = [d.identity for d in rep.differences
                 if 'com.huawei.hmos.photos' in d.identity]
        self.assertTrue(
            any('AppIcon_Image_' in i for i in whole),
            f'应有图像层缺失，实际 {whole}')
        self.assertTrue(
            any('Container_' in i for i in whole),
            f'应有容器层缺失，实际 {whole}')

    def test_overflow_is_the_metaball_and_marked_new_in_target(self):
        """③ 智慧语音悬浮球：折叠态**新出现**，子元素远大于父容器。

        关键在 `is_new_in_target=True` —— 这条**不能**读成「跨形态退化」，
        它是新增元素。真机上这类「子大于父」常见于动画/悬浮层
        （父容器只是逻辑锚点，不做裁剪），未必是缺陷。
        """
        rep = self._report()
        ov = rep.by_kind(DiffKind.OVERFLOW)
        self.assertEqual(len(ov), 1)
        d = ov[0]
        self.assertEqual(d.identity, 'id:LiveMetaBallBaseVm')
        self.assertTrue(d.evidence['is_new_in_target'])
        self.assertIsNone(d.baseline_bounds,
                          '新元素在基准形态不该有 bounds')
        self.assertEqual(d.target_bounds, (321, 0, 759, 121))
        # ⚠️ evidence 里的 `parent` 走的是 JSON 往返（`to_dict` → 序列化
        # → 读回），所以是 **list 不是 tuple** —— 别按 bounds 的类型去断言。
        self.assertEqual(list(d.evidence['parent']), [496, 12, 584, 100])
        # 子元素确实大于父容器（这是它被判溢出的直接原因）
        cw, ch = d.target_bounds[2] - d.target_bounds[0], \
            d.target_bounds[3] - d.target_bounds[1]
        p = list(d.evidence['parent'])
        pw, ph = p[2] - p[0], p[3] - p[1]
        self.assertGreater(cw * ch, pw * ph)

    def test_root_node_is_not_reported_as_clipped(self):
        """★ 根节点 = 屏幕本身，屏幕变了它必然「超出自己」。

        这是**定义使然**而非缺陷。早期没跳过它，每次多报一条
        `id:staticRoot 被裁切 234px`。这里锁死「真机数据上根节点不出现」。
        """
        rep = self._report()
        for d in rep.differences:
            self.assertNotIn('staticRoot', d.identity)
            self.assertNotEqual(d.identity, 'id:SCBDesktop')

    def test_no_garbage_identity_in_any_difference(self):
        """每条差异的身份都必须可用（非空、带 `id:`/`tp:`/`hp:` 前缀）。

        否则报告里会出现「某元素有问题」但说不清是哪个元素。
        """
        rep = self._report()
        for d in rep.differences:
            self.assertTrue(d.identity, '身份不能为空')
            self.assertTrue(
                d.identity.startswith(('id:', 'tp:', 'hp:')),
                f'身份前缀不可识别: {d.identity!r}')

    def test_identity_prefix_distribution_is_recorded(self):
        """身份降级各档都真的用上了 —— 若全是 `id:` 说明降级没生效。"""
        rep = self._report()
        prefixes = {d.identity.split(':', 1)[0] for d in rep.differences}
        self.assertIn('id', prefixes)
        self.assertIn('hp', prefixes)   # 层级路径降级确实在兜底

    # -------------------------------------------------------- 输出可复现

    def test_all_three_formats_render_on_real_data(self):
        """真机数据能渲染出三种格式，且落盘成功、内容含关键事实。

        注意三个 `to_*` 都**必须带 path**（它们同时负责写盘）——
        不是纯函数。这里顺带把「路径参数必需」这件事锁住。
        """
        import tempfile                                     # noqa: PLC0415
        rep = self._report()
        with tempfile.TemporaryDirectory() as d:
            paths = write_all(rep, d, 'real')
            for key in ('markdown', 'json', 'html'):
                self.assertTrue(os.path.exists(paths[key]), key)
            md = self._read(paths['markdown'])
            js = self._read(paths['json'])
            html = self._read(paths['html'])

        # 报告必须让人看见「检出了什么」——关键身份与计数都要在
        self.assertIn('LiveMetaBallBaseVm', md)
        # 汇总表里的数字（去掉粗体标记再比，避免被 `**11**` 绊住）
        plain = md.replace('**', '')
        self.assertIn('| 元素缺失 | 11 |', plain)
        self.assertIn('| 溢出 | 1 |', plain)
        self.assertIn('高严重度 11 处', plain)
        data = json.loads(js)
        self.assertEqual(data['counts']['MISSING'], 11)
        self.assertEqual(data['counts']['OVERFLOW'], 1)
        self.assertTrue(data['has_high'])
        self.assertIn('LiveMetaBallBaseVm', html)
        # 三种格式各自的文件都要有内容，不能是空壳
        for text, name in ((md, 'md'), (js, 'json'), (html, 'html')):
            self.assertGreater(len(text), 200, f'{name} 内容过短')

    def test_renderers_are_pure_when_given_same_report(self):
        """同一报告渲染两次，Markdown 正文必须一致（时间戳除外）。"""
        import tempfile                                     # noqa: PLC0415
        rep = self._report()
        with tempfile.TemporaryDirectory() as d:
            p1 = write_all(rep, d, 'a', generated_at='FIXED')['markdown']
            p2 = write_all(rep, d, 'b', generated_at='FIXED')['markdown']
            self.assertEqual(self._read(p1), self._read(p2))

    def test_two_runs_are_byte_identical(self):
        """★ 同一份夹具跑两次结果必须完全一致 —— 比对过程无随机性。

        否则 CI 里会出现「这次红了下次绿了」，没人会再信这个门禁。
        """
        self.assertEqual(
            json.dumps(self._report().to_dict(), ensure_ascii=False,
                       sort_keys=True),
            json.dumps(self._report().to_dict(), ensure_ascii=False,
                       sort_keys=True))

    def test_geometry_delta_works_on_real_tree(self):
        """几何位移量化在真机上可用：至少有些元素被识别为同身份。"""
        ro, po = self._form('open')
        rc, pc = self._form('close')
        delta = geometry_delta((ro, po), (rc, pc))
        self.assertGreater(len(delta), 0)
        for identity, g in list(delta.items())[:20]:
            self.assertIsInstance(identity, str)
            self.assertIn('dx', g)
            self.assertIn('width_ratio', g)


if __name__ == '__main__':
    unittest.main(verbosity=2)
