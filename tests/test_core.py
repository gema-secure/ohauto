"""L1/L2 单元测试 —— 控件树解析、匹配器、坐标解析、DSL、报告。

全部测试**不需要真机**，用 fixtures 里的控件树样例驱动，
保证在没有设备的机器上也能验证除「实际操作」之外的全部逻辑。
"""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect, flatten, parse_layout   # noqa: E402
from ohauto.matcher import ON, Matcher                              # noqa: E402
from ohauto.action import (spec_to_matcher, dump_case, load_case,   # noqa: E402
                           trace_to_steps, DslError)
from ohauto.vision import MockProvider, HybridLocator               # noqa: E402

FIXTURE = os.path.join(HERE, 'fixtures', 'login_page.json')


def login_tree() -> LayoutNode:
    with open(FIXTURE, 'r', encoding='utf-8') as f:
        return parse_layout(json.load(f))


# ================================================================ 解析

class TestRect(unittest.TestCase):
    def test_center_is_midpoint(self):
        r = Rect(0, 0, 100, 200)
        self.assertEqual(r.center, (50, 100))

    def test_size(self):
        r = Rect(10, 20, 110, 220)
        self.assertEqual((r.width, r.height), (100, 200))
        self.assertEqual(r.area, 20000)

    def test_parse_from_json_string(self):
        """dumpLayout 的 bounds 是 JSON 字符串，这是最容易踩的坑。"""
        r = Rect.parse('{"bottom":361,"left":37,"right":118,"top":280}')
        self.assertEqual((r.left, r.top, r.right, r.bottom), (37, 280, 118, 361))
        self.assertEqual(r.center, (77, 320))

    def test_parse_from_dict(self):
        r = Rect.parse({'left': 1, 'top': 2, 'right': 3, 'bottom': 4})
        self.assertEqual((r.left, r.top, r.right, r.bottom), (1, 2, 3, 4))

    def test_parse_from_list(self):
        r = Rect.parse([1, 2, 3, 4])
        self.assertEqual((r.left, r.right), (1, 3))

    # ------------------------------------------------------ 真机格式（回归）
    # 2026-09-16 在润和 DAYU200 / OpenHarmony 5.0.3.135 上实测发现：
    # 真机 dumpLayout 的 bounds 是 "[left,top][right,bottom]"，两个方括号
    # 之间没有分隔符。旧的降级实现先删括号再 split，会把 "0,0][720,1280"
    # 变成 "0,0720,1280"，只剩 3 个数字，于是返回全 0 矩形 —— 结果是
    # 真机上所有控件坐标都退化成 0，点击全部落到屏幕左上角。
    # 而 sim.py 当时造的是 JSON 对象串，所以 113 个单测全绿也照样漏掉。
    # 以下用例锁死该格式，防止回归。

    def test_parse_from_openharmony_bracket_pair(self):
        """真机格式 "[l,t][r,b]" —— 必须解析成功，不能退化成空矩形。"""
        r = Rect.parse('[0,0][720,1280]')
        self.assertEqual((r.left, r.top, r.right, r.bottom), (0, 0, 720, 1280))
        self.assertEqual(r.center, (360, 640))

    def test_bracket_pair_does_not_glue_adjacent_numbers(self):
        """专门盯住 "[0,0][720,1280]" 里 0 与 720 被粘成 0720 的回归。"""
        r = Rect.parse('[0,0][720,1280]')
        self.assertEqual(r.width, 720, '相邻两对方括号之间的数字被粘在一起了')
        self.assertNotEqual(r.area, 0)

    def test_parse_bracket_pair_with_whitespace(self):
        r = Rect.parse('[ 10 , 20 ][ 30 , 40 ]')
        self.assertEqual((r.left, r.top, r.right, r.bottom), (10, 20, 30, 40))

    def test_parse_bracket_pair_negative_and_offscreen(self):
        """控件被滑出屏幕时坐标可能为负，也要能吃下。"""
        r = Rect.parse('[-120,0][240,300]')
        self.assertEqual((r.left, r.top, r.right, r.bottom), (-120, 0, 240, 300))

    def test_parse_comma_separated_plain(self):
        r = Rect.parse('37,280,118,361')
        self.assertEqual((r.left, r.top, r.right, r.bottom), (37, 280, 118, 361))

    def test_parse_json_array_string(self):
        r = Rect.parse('[37,280,118,361]')
        self.assertEqual((r.left, r.top, r.right, r.bottom), (37, 280, 118, 361))

    def test_all_supported_bounds_shapes_agree(self):
        """四种形态表达同一个矩形时，解析结果必须一致。"""
        expect = (37, 280, 118, 361)
        for raw in ('[37,280][118,361]',
                    '{"left":37,"top":280,"right":118,"bottom":361}',
                    '[37,280,118,361]',
                    '37,280,118,361'):
            with self.subTest(raw=raw):
                r = Rect.parse(raw)
                self.assertEqual((r.left, r.top, r.right, r.bottom), expect)

    def test_parse_garbage_returns_zero_rect(self):
        self.assertEqual(Rect.parse('不是坐标').area, 0)
        self.assertEqual(Rect.parse(None).area, 0)

    def test_contains(self):
        r = Rect(0, 0, 100, 100)
        self.assertTrue(r.contains(50, 50))
        self.assertFalse(r.contains(150, 50))

    def test_iou_identical(self):
        r = Rect(0, 0, 100, 100)
        self.assertAlmostEqual(r.overlap_ratio(r), 1.0)

    def test_iou_disjoint(self):
        self.assertEqual(Rect(0, 0, 10, 10).overlap_ratio(Rect(100, 100, 110, 110)), 0.0)

    def test_iou_partial(self):
        a, b = Rect(0, 0, 100, 100), Rect(50, 0, 150, 100)
        # 交集 50*100=5000，并集 2*10000-5000=15000
        self.assertAlmostEqual(a.overlap_ratio(b), 5000 / 15000, places=4)


class TestParseLayout(unittest.TestCase):
    def setUp(self):
        self.root = login_tree()

    def test_root_parsed(self):
        self.assertEqual(self.root.type, 'Root')
        self.assertEqual(self.root.rect.width, 1080)

    def test_children_linked(self):
        col = self.root.children[0]
        self.assertEqual(col.type, 'Column')
        self.assertIs(col.parent, self.root)

    def test_attributes_subobject_flattened(self):
        """字段在 attributes 子对象里，解析后要能平铺读到。"""
        btn = self._find('btn_login')
        self.assertEqual(btn.text, '登录')
        self.assertEqual(btn.type, 'Button')
        self.assertTrue(btn.clickable)

    def test_bounds_json_string_converted(self):
        btn = self._find('btn_login')
        self.assertEqual(btn.rect.center, (540, 770))

    def test_visible_false_detected(self):
        self.assertFalse(self._find('btn_hidden').visible)

    def test_enabled_false_detected(self):
        self.assertFalse(self._find('btn_disabled').enabled)

    def test_walk_visits_all(self):
        names = {n.id for n in self.root.walk()}
        self.assertIn('username', names)
        self.assertIn('btn_login', names)
        self.assertEqual(len(names), 10)     # 含 Root 自身

    def test_flatten_excludes_invisible(self):
        ids = {n.id for n in flatten(self.root, only_visible=True)}
        self.assertNotIn('btn_hidden', ids)
        self.assertIn('btn_login', ids)

    def test_flatten_interactive_only(self):
        ids = {n.id for n in flatten(self.root, only_visible=True,
                                     only_interactive=True)}
        self.assertIn('username', ids)
        self.assertIn('btn_login', ids)

    def test_path_includes_id(self):
        self.assertIn('btn_login', self._find('btn_login').path)

    def test_label_fallback_order(self):
        """label 依次回退 text -> descr -> hint -> id -> type"""
        self.assertEqual(self._find('btn_login').label, '登录')            # text
        self.assertEqual(self._find('icon_wechat').label, '微信快捷登录')   # descr
        self.assertEqual(self._find('username').label, '请输入用户名')      # hint
        # 一个既无 text/descr/hint 的控件才回退到 id
        node = self._find('btn_hidden')
        node.text = node.descr = node.hint = ''
        self.assertEqual(node.label, 'btn_hidden')                         # id

    def test_from_json_text(self):
        with open(FIXTURE, 'r', encoding='utf-8') as f:
            tree = parse_layout(f.read())
        self.assertEqual(tree.type, 'Root')

    def test_from_file_path(self):
        tree = parse_layout(FIXTURE)
        self.assertEqual(tree.type, 'Root')

    def _find(self, cid: str) -> LayoutNode:
        for n in self.root.walk():
            if n.id == cid:
                return n
        raise AssertionError(f'未找到 {cid}')


# ================================================================ 匹配器

class TestIsInteractiveCallTrap(unittest.TestCase):
    """★ `is_interactive` 是**方法**，不是 property —— 漏括号会静默出错。

    本类是一份"可执行的文档"。

    同一个类里 `clickable` / `scrollable` / `visible` / `path` 都是
    property，**只有 `is_interactive` 是普通方法**。漏掉括号拿到的是
    方法对象本身，而方法对象**恒为真值** —— 于是静默得出
    「所有节点都可交互」，不报错、不告警。

    实测来源（2026-09-19）：真机采集统计报「169 个节点全部可交互」，
    而原始 JSON 里只有 11 个 `clickable=true`。
    是**交叉核对原始数据**才发现的 —— 光看统计数字永远看不出来。

    这类"静默错误"比崩溃危险得多：崩溃至少你会知道。
    """

    @staticmethod
    def _node(type_, **attrs):
        a = {'type': type_, 'bounds': '[0,0][10,10]'}
        a.update({k: str(v).lower() if isinstance(v, bool) else v
                  for k, v in attrs.items()})
        return parse_layout(json.dumps({'attributes': a}))

    def test_is_a_method_not_a_property(self):
        """锁住设计事实：若改成 property，必须同步改所有调用点。"""
        self.assertFalse(
            isinstance(LayoutNode.is_interactive, property),
            'is_interactive 当前是方法。若要改为 property 以统一风格，'
            '必须同步修改 crossform.py / explorer.py / matcher.py 等'
            '所有 is_interactive() 调用点，且属对外接口变更，需通知 A、B。')

    def test_bare_reference_is_truthy_this_is_the_trap(self):
        """★ 显式演示陷阱机制：不调用 → 恒真。"""
        container = self._node('Column', clickable=False, scrollable=False)
        self.assertFalse(container.is_interactive(),
                         'Column 且 clickable=false → 不该判为可交互')
        self.assertTrue(
            bool(container.is_interactive),
            '★ 漏括号时拿到方法对象，恒为真值 —— 这就是坑的机制')

    def test_container_is_not_interactive(self):
        self.assertFalse(self._node('Row', clickable=False).is_interactive())
        self.assertFalse(self._node('Column', clickable=False).is_interactive())

    def test_clickable_node_is_interactive(self):
        self.assertTrue(self._node('Row', clickable=True).is_interactive())

    def test_scrollable_node_is_interactive(self):
        self.assertTrue(self._node('Row', scrollable=True).is_interactive())

    def test_interactive_by_type_even_without_flags(self):
        """Button 即使 clickable=false 也按类型兜底算可交互。"""
        self.assertTrue(self._node('Button', clickable=False).is_interactive())

    def test_sum_with_bare_reference_counts_everything(self):
        """★ 直接复现踩到的错误写法，把它的后果钉死。

        `sum(1 for n in nodes if n.is_interactive)` 会恒等于节点总数，
        因为方法对象恒真。正确写法必须带括号。
        """
        nodes = [self._node('Column', clickable=False),
                 self._node('Row', clickable=False),
                 self._node('Button', clickable=False)]
        wrong = sum(1 for n in nodes if n.is_interactive)      # ❌ 漏括号
        right = sum(1 for n in nodes if n.is_interactive())    # ✅
        self.assertEqual(wrong, 3, '漏括号 → 全部计入（错误）')
        self.assertEqual(right, 1, '带括号 → 只有 Button（正确）')


class TestMatcher(unittest.TestCase):
    def setUp(self):
        self.root = login_tree()
        self.pool = flatten(self.root)

    def hits(self, m: Matcher):
        return m.filter(self.pool)

    def test_text_exact(self):
        self.assertEqual(len(self.hits(ON.text('登录'))), 1)

    def test_text_is_case_sensitive_but_exact(self):
        self.assertEqual(len(self.hits(ON.text('登录 '))), 0)

    def test_text_contains(self):
        # 仅 btn_forget 的 text 含「密码」；两个 TextInput 的提示语在 hint 字段，
        # 不属于 text，故不命中 —— 这正是 hint 与 text 必须分开的原因。
        self.assertEqual(len(self.hits(ON.text_contains('密码'))), 1)

    def test_text_matches_regex(self):
        self.assertEqual(len(self.hits(ON.text_matches(r'^忘记'))), 1)

    def test_text_in(self):
        self.assertEqual(len(self.hits(ON.text_in(['登录', '忘记密码']))), 2)

    def test_id(self):
        self.assertEqual(len(self.hits(ON.id('btn_login'))), 1)

    def test_id_fuzzy(self):
        # 含 'login' 的 id：loginPage 与 btn_login
        self.assertEqual(len(self.hits(ON.id('login', exact=False))), 2)

    def test_type(self):
        self.assertEqual(len(self.hits(ON.type('Button'))), 4)   # 含 hidden / disabled

    def test_type_fuzzy(self):
        # 含 'text' 的类型：Text 1 个 + TextInput 2 个
        self.assertEqual(len(self.hits(ON.type('text', exact=False))), 3)

    def test_combined_and(self):
        m = ON.type('Button').id('btn_login')
        self.assertEqual(len(self.hits(m)), 1)

    def test_combined_no_match(self):
        m = ON.type('TextInput').id('btn_login')
        self.assertEqual(len(self.hits(m)), 0)

    def test_clickable_filter(self):
        self.assertTrue(all(n.clickable for n in self.hits(ON.clickable(True))))

    def test_visible_filter(self):
        # 注意：默认 pool 是 flatten(only_visible=True)，会把不可见节点滤掉。
        # 要查「不可见节点」必须显式用全集。
        pool = flatten(self.root, only_visible=False)
        ids = {n.id for n in ON.visible(False).filter(pool)}
        self.assertEqual(ids, {'btn_hidden'})

    def test_visible_filter_on_default_pool_is_empty(self):
        self.assertEqual(len(self.hits(ON.visible(False))), 0)

    def test_size_at_least_filters_tiny(self):
        ids = {n.id for n in self.hits(ON.size_at_least(44, 44))}
        self.assertIn('btn_login', ids)
        self.assertNotIn('btn_hidden', ids)      # 0x0

    def test_nth(self):
        m = ON.type('Button').nth(0)
        self.assertEqual(len(self.hits(m)), 1)

    def test_nth_out_of_range(self):
        self.assertEqual(len(self.hits(ON.type('Button').nth(99))), 0)

    def test_within_container(self):
        m = ON.text('登录').within(ON.id('loginPage'))
        self.assertEqual(len(self.hits(m)), 1)

    def test_within_nonexistent_container(self):
        m = ON.text('登录').within(ON.id('不存在的容器'))
        self.assertEqual(len(self.hits(m)), 0)

    def test_label_contains_searches_all_fields(self):
        self.assertEqual(len(self.hits(ON.label('微信'))), 1)   # 命中 descr

    def test_descr(self):
        self.assertEqual(len(self.hits(ON.descr('快捷'))), 1)

    def test_area_in(self):
        m = ON.text('登录').area_in(Rect(0, 700, 1080, 900))
        self.assertEqual(len(self.hits(m)), 1)

    def test_custom_predicate(self):
        m = Matcher().custom(lambda n: n.id.endswith('_login'), 'endswith_login')
        self.assertEqual(len(self.hits(m)), 1)

    def test_str_is_readable(self):
        self.assertIn('id=btn_login', str(ON.id('btn_login')))


# ================================================================ DSL

class TestSpecToMatcher(unittest.TestCase):
    def setUp(self):
        self.pool = flatten(login_tree())

    def hits(self, spec):
        return spec_to_matcher(spec).filter(self.pool)

    def test_text_spec(self):
        self.assertEqual(len(self.hits({'text': '登录'})), 1)

    def test_id_spec(self):
        self.assertEqual(len(self.hits({'id': 'username'})), 1)

    def test_bare_string_is_text(self):
        self.assertEqual(len(self.hits('登录')), 1)

    def test_combined_spec(self):
        self.assertEqual(len(self.hits({'type': 'Button', 'text': '登录'})), 1)

    def test_within_spec(self):
        self.assertEqual(len(self.hits({'text': '登录',
                                        'within': {'id': 'loginPage'}})), 1)

    def test_nth_spec(self):
        self.assertEqual(len(self.hits({'type': 'Button', 'nth': 0})), 1)

    def test_size_at_least_dict(self):
        ids = {n.id for n in self.hits({'type': 'Button',
                                        'size_at_least': {'w': 100, 'h': 50}})}
        self.assertIn('btn_login', ids)

    def test_unknown_key_raises(self):
        with self.assertRaises(DslError) as cm:
            spec_to_matcher({'不认识的字段': 1})
        self.assertIn('未知的匹配器字段', str(cm.exception))

    def test_non_dict_raises(self):
        with self.assertRaises(DslError):
            spec_to_matcher(123)

    def test_passthrough_matcher(self):
        m = ON.id('username')
        self.assertIs(spec_to_matcher(m), m)


class TestCaseSerialization(unittest.TestCase):
    def test_roundtrip_json(self):
        case = {'name': '登录', 'bundle': 'com.x', 'steps': [
            {'start': True}, {'tap': {'id': 'btn_login'}}]}
        text = dump_case(case)
        self.assertIn('btn_login', text)

    def test_load_from_dict(self):
        case = {'name': 'a', 'steps': []}
        self.assertEqual(load_case(case)['name'], 'a')

    def test_load_from_json_text(self):
        txt = '{"name":"b","steps":[{"start":true}]}'
        self.assertEqual(load_case(txt)['name'], 'b')

    def test_chinese_preserved(self):
        case = {'name': '中文用例名', 'steps': [{'tap': {'text': '登录'}}]}
        self.assertIn('中文用例名', dump_case(case))


# ================================================================ 视觉融合

class TestVision(unittest.TestCase):
    def setUp(self):
        self.root = login_tree()

    def test_mock_provider_matches_hint(self):
        p = MockProvider()
        hints = [{'type': 'Button', 'id': 'btn_login', 'text': '登录',
                  'bounds': {'left': 120, 'top': 720, 'right': 960, 'bottom': 820}}]
        got = p.locate('x.png', '登录', 1080, 2340, hints)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].center, (540, 770))

    def test_hybrid_tree_channel(self):
        loc = HybridLocator(provider=MockProvider(), verbose=False)
        t = loc.locate(self.root, 'x.png', '登录', 1080, 2340)
        self.assertIsNotNone(t)
        self.assertEqual(t.center, (540, 770))

    def test_hybrid_returns_none_when_absent(self):
        loc = HybridLocator(provider=MockProvider(), verbose=False)
        t = loc.locate(self.root, 'x.png', '完全不存在的目标', 1080, 2340)
        self.assertIsNone(t)

    def test_hybrid_vision_only_when_no_tree_hit(self):
        """控件树无命中 + 视觉有命中 -> 采用视觉结果。"""
        opts = {'left': 5, 'top': 5, 'right': 500, 'bottom': 600}

        class P(MockProvider):
            def locate(self, *a, **k):
                from ohauto.vision import VisualTarget
                return [VisualTarget('猜测目标', Rect.parse(opts), 0.9)]

        loc = HybridLocator(provider=P(), verbose=False)
        t = loc.locate(self.root, 'x.png', '不存在的文字', 1080, 2340)
        self.assertIsNotNone(t)
        self.assertEqual(t.source, 'vision')

    def test_hybrid_crosscheck_boosts_confidence(self):
        """视觉与控件树重叠时标记为 hybrid 并提升置信度。"""
        from ohauto.vision import VisualTarget

        class P(MockProvider):
            def locate(self, *a, **k):
                # 与 btn_login 高度重叠
                return [VisualTarget('按钮', Rect(120, 720, 960, 820), 0.6)]

        loc = HybridLocator(provider=P(), verbose=False)
        t = loc.locate(self.root, 'x.png', '登录', 1080, 2340)
        self.assertEqual(t.source, 'hybrid')
        self.assertGreater(t.confidence, 0.6)


# ================================================================ 报告

class TestReport(unittest.TestCase):
    def _fake_driver(self):
        class S:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class D:
            bundle, ability = 'com.demo', 'EntryAbility'
            steps = [
                S(index=1, kind='start', target='com.demo', value=None, ok=True,
                  elapsed_ms=900, screenshot=None, layout_json=None,
                  node_path=None, coords=None, error=None, extra={}),
                S(index=2, kind='tap', target='id=btn_login', value=None, ok=True,
                  elapsed_ms=120, screenshot=None, layout_json=None,
                  node_path='Column > Button#btn_login', coords=(540, 770),
                  error=None, extra={}),
                S(index=3, kind='assert.exists', target="text='首页'", value=None,
                  ok=False, elapsed_ms=8000, screenshot=None, layout_json=None,
                  node_path=None, coords=None,
                  error='8000ms 内未等到控件', extra={}),
            ]

            def steps_to_dict(self):
                return [s.__dict__ for s in self.steps]

            def summary(self):
                return {'bundle': self.bundle, 'steps_total': 3,
                        'steps_failed': 1, 'success_rate': 0.6667,
                        'total_elapsed_ms': 9020,
                        'failed_steps': [self.steps[2].__dict__]}
        return D()

    def test_collect_shape(self):
        from ohauto.report import collect
        d = collect(self._fake_driver())
        self.assertEqual(d['bundle'], 'com.demo')
        self.assertEqual(len(d['steps']), 3)
        self.assertEqual(d['summary']['steps_failed'], 1)

    def test_write_all_creates_three_files(self):
        import tempfile
        from ohauto.report import collect, write_all
        data = collect(self._fake_driver(),
                       run_report={'name': '登录回归', 'bundle': 'com.demo',
                                   'total': 3, 'passed': 2, 'failed': 1,
                                   'ok': False, 'elapsed_ms': 9020, 'errors': []})
        with tempfile.TemporaryDirectory() as td:
            paths = write_all(data, td, stem='r')
            for k in ('json', 'markdown', 'html'):
                self.assertTrue(os.path.exists(paths[k]), k)
            with open(paths['json'], encoding='utf-8') as f:
                self.assertEqual(json.load(f)['bundle'], 'com.demo')
            with open(paths['html'], encoding='utf-8') as f:
                h = f.read()
            self.assertIn('UI 自动化执行报告', h)
            self.assertIn('com.demo', h)
            self.assertIn('未通过', h)          # 来自 case.ok == False
            with open(paths['markdown'], encoding='utf-8') as f:
                md = f.read()
            self.assertIn('登录回归', md)
            self.assertIn('未通过', md)


# ================================================================ 留痕转脚本

class TestTraceToSteps(unittest.TestCase):
    def test_basic_conversion(self):
        class S:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class D:
            steps = [
                S(kind='start', ok=True, coords=None, node_path=None,
                  value=None, target=''),
                S(kind='tap', ok=True, coords=(10, 20),
                  node_path='Column > Button#btn_ok', value=None, target=''),
                S(kind='input', ok=True, coords=(5, 6),
                  node_path='Row > TextInput#username', value='alice',
                  target=''),
                S(kind='assert.exists', ok=True, coords=None, node_path=None,
                  value=None, target=''),
            ]
        steps = trace_to_steps(D())
        kinds = [next(iter(s)) for s in steps]
        self.assertEqual(kinds, ['start', 'tap', 'input'])   # 断言不自动带
        self.assertEqual(steps[1]['tap'], {'id': 'btn_ok'})
        self.assertEqual(steps[2]['input']['id'], 'username')
        self.assertEqual(steps[2]['input']['value'], 'alice')


class TestRealDeviceFixture(unittest.TestCase):
    """用真机抓下来的控件树做回归 —— 这是「模拟器通过 ≠ 真机可用」的护栏。

    fixture 来源：润和 DAYU200 / OpenHarmony 5.0.3.135 / API 15 /
    uitest 5.0.1.2，屏幕 720x1280，抓取时机是插入 USB 后弹出的
    「USB 连接方式」对话框页面。该页面含 Button / Radio / ListItem /
    Scroll / Dialog 等真实控件，是很好的坐标解析样本。
    """

    FIXTURE_REAL = os.path.join(HERE, 'fixtures', 'real_dayu200_usb_dialog.json')

    @classmethod
    def setUpClass(cls):
        with open(cls.FIXTURE_REAL, 'r', encoding='utf-8') as f:
            cls.data = json.load(f)
        cls.root = parse_layout(cls.data)

    def test_root_rect_matches_device_screen(self):
        """根节点 bounds 必须等于设备原生分辨率，不能是空的。"""
        self.assertEqual(
            (self.root.rect.left, self.root.rect.top,
             self.root.rect.right, self.root.rect.bottom), (0, 0, 720, 1280))
        self.assertEqual(self.root.rect.center, (360, 640))

    def test_node_count(self):
        self.assertEqual(len(list(self.root.walk())), 103)

    # 独立于 layout.py 的参考正则，用来判断「解析有没有丢数字」
    REF_BOUNDS_RE = re.compile(
        r'\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]')

    def test_bounds_parse_matches_independent_reference(self):
        """核心护栏：解析结果必须与独立正则抠出的四个数逐一相等。

        这里刻意**不**断言「面积非零」—— 真机上确实存在零宽/零高的折叠
        元素（例如 `[54,568][666,568]` 高度为 0、`[218,0][218,72]` 宽度为 0），
        那是真实布局状态，不是解析失败。要盯的是「解析把数字丢了」：
        旧实现下 `[0,0][720,1280]` 会变成 (0,0,0,0)，这条会全部命中。
        """
        bad = []
        for n in self.root.walk():
            raw = (n.attributes or {}).get('bounds', '') or ''
            m = self.REF_BOUNDS_RE.search(raw)
            if not m:
                continue
            expect = tuple(int(g) for g in m.groups())
            got = (n.rect.left, n.rect.top, n.rect.right, n.rect.bottom)
            if got != expect:
                bad.append((n.type, n.id, raw, expect, got))
        self.assertEqual(bad, [],
                         f'{len(bad)} 个节点的 bounds 解析结果与原始值不符：{bad[:3]}')

    def test_nodes_with_real_area_have_usable_centers(self):
        """有实际面积的节点，中心点必须落在屏幕内 —— 否则点击打不中。"""
        checked = 0
        for n in self.root.walk():
            if n.rect.area == 0:
                continue
            checked += 1
            x, y = n.center
            self.assertTrue(
                self.root.rect.contains(x, y),
                f'{n.type}#{n.id} 的中心 {(x, y)} 落在屏幕外，bounds={n.rect.to_dict()}')
        self.assertEqual(checked, 93, '有面积的节点数变了，检查 fixture 是否被改动')

    def test_known_button_center(self):
        """已知控件的中心坐标要与 JSON 里 [42,687][678,747] 的中位点一致。

        这个坐标是实测拿来点掉对话框的坐标，端到端验证过确实生效。
        """
        btns = [n for n in self.root.walk()
                if n.id == 'advanced_dialog_button_0']
        self.assertEqual(len(btns), 1)
        self.assertEqual(btns[0].text, '确定')
        self.assertEqual(btns[0].center, (360, 717))

    def test_locate_by_text_returns_clickable_target(self):
        """按文案定位是人写用例的主要方式，必须能直接拿到可点坐标。"""
        hits = [n for n in self.root.walk() if n.text == '仅充电']
        self.assertTrue(hits, '未能在真实控件树里按文案定位到「仅充电」')
        x, y = hits[0].center
        self.assertTrue(self.root.rect.contains(x, y),
                        f'定位出的中心点 {(x, y)} 落在屏幕之外')


if __name__ == '__main__':
    unittest.main(verbosity=2)
