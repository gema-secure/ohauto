"""A6 bounds 解析加固 + A5 控件树摘要器 的单元测试。

全部不需要真机：A5 的验收直接跑在 C 提供的真机 fixture
（real_dayu200_usb_dialog.json，DAYU200 实测 dump）上。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect, parse_layout          # noqa: E402
from ohauto.treesum import (summarize_tree, summary_lines,        # noqa: E402
                            count_nodes, is_summary_worthy)

REAL_FIXTURE = os.path.join(HERE, 'fixtures', 'real_dayu200_usb_dialog.json')


def real_tree() -> LayoutNode:
    with open(REAL_FIXTURE, 'r', encoding='utf-8') as f:
        return parse_layout(json.load(f))


def make_tree(spec):
    """spec: 嵌套 tuple (type, id, text, attrs_dict, [children])，快速造树。"""
    def build(t):
        type_, id_, text, attrs, children = t
        n = LayoutNode(type=type_, id=id_, text=text, attributes=dict(attrs))
        for k, v in attrs.items():
            if k == 'bounds':
                n.rect = Rect.parse(v)
            elif k == 'clickable':
                n.clickable = str(v).lower() == 'true'
            elif k == 'scrollable':
                n.scrollable = str(v).lower() == 'true'
        for c in children:
            child = build(c)
            child.parent = n
            n.children.append(child)
        return n
    return build(spec)


# ================================================================ A6

class TestA6BoundsDictForms(unittest.TestCase):
    """每种形态各一条单测；含负数与超大坐标的用例不崩。"""

    def test_standard_keys(self):
        r = Rect.parse({'left': 1, 'top': 2, 'right': 3, 'bottom': 4})
        self.assertEqual((r.left, r.top, r.right, r.bottom), (1, 2, 3, 4))

    def test_xy_keys(self):
        r = Rect.parse({'x': 10, 'y': 20, 'x2': 110, 'y2': 220})
        self.assertEqual((r.left, r.top, right := r.right, bottom := r.bottom),
                         (10, 20, 110, 220))

    def test_start_end_keys(self):
        r = Rect.parse({'startX': 5, 'startY': 6, 'endX': 75, 'endY': 106})
        self.assertEqual((r.left, r.top, r.right, r.bottom), (5, 6, 75, 106))

    def test_left_plus_width_derivation(self):
        """只给 left+width / top+height 要自行推 right/bottom。"""
        r = Rect.parse({'left': 37, 'top': 280, 'width': 81, 'height': 81})
        self.assertEqual((r.left, r.top, r.right, r.bottom), (37, 280, 118, 361))
        self.assertEqual(r.center, (77, 320))

    def test_mixed_startX_plus_width(self):
        r = Rect.parse({'startX': 0, 'startY': 0, 'width': 720, 'height': 1280})
        self.assertEqual((r.right, r.bottom), (720, 1280))

    def test_missing_corner_degrades_to_empty_not_zero_rect(self):
        """缺角凑不齐 → 空矩形，而不是把 right 静默当 0（旧实现的隐患）。"""
        r = Rect.parse({'left': 37, 'top': 280})
        self.assertEqual((r.left, r.top, r.right, r.bottom), (0, 0, 0, 0))

    def test_negative_and_huge_do_not_crash(self):
        r1 = Rect.parse({'left': -50, 'top': -50, 'width': 100, 'height': 100})
        self.assertEqual((r1.right, r1.bottom), (50, 50))
        big = 2 ** 31
        r2 = Rect.parse({'left': 0, 'top': 0, 'width': big, 'height': big})
        self.assertEqual(r2.right, big)

    def test_float_values_accepted(self):
        r = Rect.parse({'left': 1.6, 'top': 2.2, 'width': 3.9, 'height': 4.1})
        self.assertEqual((r.left, r.top, r.right, r.bottom), (1, 2, 5, 6))


class TestA6BoundsAliasKeys(unittest.TestCase):
    """五种键名别名：bounds / rect / rectInScreen / visibleBounds / region。"""

    def _attr_bounds(self, key):
        raw = {'type': 'Button',
               key: '[10,20][110,220]',
               'clickable': 'true'}
        return parse_layout({'attributes': raw, 'children': []})

    def test_bounds(self):
        self.assertEqual(self._attr_bounds('bounds').rect.right, 110)

    def test_rect(self):
        self.assertEqual(self._attr_bounds('rect').rect.right, 110)

    def test_rect_in_screen(self):
        self.assertEqual(self._attr_bounds('rectInScreen').rect.right, 110)

    def test_visible_bounds(self):
        self.assertEqual(self._attr_bounds('visibleBounds').rect.right, 110)

    def test_region(self):
        self.assertEqual(self._attr_bounds('region').rect.right, 110)

    def test_bounds_still_wins_over_rect(self):
        """'bounds' 保持第一优先：两个键都在时以真机标准键为准。"""
        raw = {'type': 'Button', 'bounds': '[0,0][1,1]', 'rect': '[0,0][9,9]'}
        self.assertEqual(parse_layout({'attributes': raw, 'children': []}).rect.right, 1)

    def test_region_as_dict_form(self):
        raw = {'type': 'Button', 'region': {'left': 1, 'top': 2, 'width': 3, 'height': 4}}
        node = parse_layout({'attributes': raw, 'children': []})
        self.assertEqual((node.rect.right, node.rect.bottom), (4, 6))


# ================================================================ A5

class TestA5Treesum(unittest.TestCase):
    def _toy_tree(self):
        """root(deep) > container(空壳) > btn(有id) / txt(有text) / plain"""
        return make_tree(
            ('root', '', '', {},
             [('Column', '', '', {'bounds': '[0,0][720,1280]'},
               [('Stack', '', '', {'bounds': '[0,0][720,60]'},
                 [('Button', 'btn_ok', '', {'bounds': '[10,10][60,50]',
                                            'clickable': 'true'}, []),
                  ('Text', '', '用户名', {'bounds': '[0,70][100,90]'}, []),
                  ('Row', '', '', {'bounds': '[0,100][720,110]'}, [])])])]))

    def test_real_fixture_lines_under_half_nodes(self):
        """验收：真机 103 节点可压到几十行 —— 行数 < 节点数 50%。"""
        root = real_tree()
        total = count_nodes(root)
        lines = summary_lines(root)
        self.assertLess(len(lines), total * 0.5,
                        f'摘要 {len(lines)} 行应 < 全树 {total} 节点的 50%')

    def test_real_fixture_no_interactive_node_lost(self):
        """验收：不丢任何可交互节点（clickable/scrollable/longClickable/
        focusable 任一为真的原始节点必须出现在摘要里）。"""
        root = real_tree()
        interactive = []
        for n in root.walk():
            attrs = n.attributes
            if (n.clickable or n.scrollable
                    or str(attrs.get('longClickable', '')).lower() == 'true'
                    or str(attrs.get('focusable', '')).lower() == 'true'):
                interactive.append(n)
        lines = summary_lines(root)
        # 摘要行里必须能找到每个可交互节点的坐标
        rects_in_summary = [ln for ln in lines]
        for n in interactive:
            key = f'[{n.rect.left},{n.rect.top}][{n.rect.right},{n.rect.bottom}]'
            self.assertTrue(any(key in ln for ln in rects_in_summary),
                            f'可交互节点丢失: {n.type} {key}')

    def test_plain_nodes_take_no_line(self):
        root = self._toy_tree()
        # 保留的只有 Button(id+clickable) 和 Text(text)；
        # Column/Stack/Row 无标识不可交互，不占行
        lines = summary_lines(root)
        self.assertEqual(len(lines), 2)
        joined = '\n'.join(lines)
        self.assertIn('Button', joined)
        self.assertIn('用户名', joined)
        self.assertNotIn('Column', joined)
        self.assertNotIn('Stack', joined)

    def test_children_indent_follows_absolute_depth(self):
        """「子节点缩进不上移」：Button 的缩进按原树绝对深度（depth=3）。"""
        root = self._toy_tree()
        lines = summary_lines(root)
        btn_line = next(ln for ln in lines if 'Button' in ln)
        self.assertTrue(btn_line.startswith('  ' * 3),
                        f'Button 应有 3 层缩进: {btn_line!r}')

    def test_max_depth_prunes(self):
        root = self._toy_tree()
        # Button/Text 在绝对深度 3：depth<=2 全剪掉
        self.assertEqual(summary_lines(root, max_depth=2), [])
        # depth=3 时两者都保留
        self.assertEqual(len(summary_lines(root, max_depth=3)), 2)

    def test_keep_invisible_option(self):
        root = make_tree(
            ('root', '', '', {},
             [('Text', '', '隐藏文字', {'bounds': '[0,0][1,1]'}, [])]))
        # 原始节点 visible 默认 True；造一个显式不可见的
        inv = LayoutNode(type='Text', text='不可见')
        inv.visible = False
        inv.parent = root
        root.children.append(inv)
        self.assertNotIn('不可见', '\n'.join(summary_lines(root)))
        self.assertIn('不可见', '\n'.join(summary_lines(root, keep_invisible=True)))

    def test_header_reports_ratio(self):
        root = real_tree()
        text = summarize_tree(root)
        first = text.splitlines()[0]
        self.assertIn('全树', first)
        self.assertIn('%', first)

    def test_summary_worthy_by_id_text_descr_hint(self):
        n = LayoutNode(type='Row')
        self.assertFalse(is_summary_worthy(n))
        n.id = 'x'
        self.assertTrue(is_summary_worthy(n))

    def test_deep_tree_no_recursion_crash(self):
        """3000 层深链不炸栈（迭代实现）。"""
        root = cur = LayoutNode(type='N')
        for _ in range(3000):
            child = LayoutNode(type='N')
            child.parent = cur
            cur.children.append(child)
            cur = child
        self.assertIsInstance(summary_lines(root, max_depth=10), list)


if __name__ == '__main__':
    unittest.main()
