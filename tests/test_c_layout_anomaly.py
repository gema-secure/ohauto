"""布局异常（LAYOUT_ANOMALY）判据的回归钉子。

判据是「可见控件**越出屏幕**」，不是「越出父容器」—— 这个选择是**真机数据**定的：
7 个真机样本里，「越出父容器」每个都有 11~13 个命中、「子比父大」每个都有 8~9 个，
两条都等于「永远报警」；而「越出屏幕」在 7/7 样本上都是 0（见
`tools/verify_layout_anomaly_real.py`）。

所以本文件最重要的用例是**负例 2**：越出父容器但仍在屏内 —— **必须不报**。
这条一旦被改回去，判据就在真机上彻底失效（而所有正例测试仍然会通过，
测不出来），所以它比正例更重要。
"""
import glob
import os
import unittest

from ohauto.layout import parse_layout
from ohauto.signals import (LAYOUT_OVERFLOW_MIN_PX, Signals,
                            _judge_layout_anomaly)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'fixtures', 'real_20260919')


def _root(children, bounds='[0,0][720,1280]'):
    return {'attributes': {'type': 'Root', 'bounds': bounds}, 'children': children}


def _node(type_, bounds, **attrs):
    a = {'type': type_, 'bounds': bounds}
    a.update(attrs)
    return {'attributes': a, 'children': []}


def _judge(tree):
    sig = Signals(bundle='com.demo.app')
    _judge_layout_anomaly(sig, parse_layout(tree))
    return sig.anomalies


class TestLayoutAnomalyPositive(unittest.TestCase):
    """真越出屏幕 → 必须报。"""

    def test_widget_beyond_right_edge(self):
        tree = _root([_node('Button', '[700,900][900,1000]')])
        got = _judge(tree)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, 'LAYOUT_ANOMALY')
        self.assertIn('越出屏幕', got[0].evidence)

    def test_widget_beyond_bottom_edge(self):
        tree = _root([_node('Button', '[100,1200][300,1400]')])
        self.assertEqual(len(_judge(tree)), 1)

    def test_widget_left_of_screen(self):
        tree = _root([_node('Button', '[-50,300][100,400]')])
        self.assertEqual(len(_judge(tree)), 1)

    def test_evidence_carries_count_and_coords(self):
        tree = _root([
            _node('Button', '[700,900][900,1000]'),
            _node('Text', '[0,1250][720,1300]'),
        ])
        got = _judge(tree)
        self.assertEqual(len(got), 1, '多个越界控件应合并成一条 Anomaly')
        self.assertIn('2 个可见控件越出屏幕', got[0].evidence)
        self.assertIn('720,1280', got[0].evidence)      # 屏幕边界要写出来
        self.assertEqual(got[0].source, 'layout')

    def test_only_worst_three_listed(self):
        kids = [_node('Button', '[700,%d][900,%d]' % (100 + i * 10, 200 + i * 10))
                for i in range(5)]
        got = _judge(_root(kids))
        self.assertIn('5 个可见控件越出屏幕', got[0].evidence)
        self.assertIn('最严重的 3 个', got[0].evidence)


class TestLayoutAnomalyNegative(unittest.TestCase):
    """反例 —— 判据能不能用全看这些。"""

    def test_parent_overflow_within_screen_does_not_report(self):
        """⚠️ **最关键的一条**：越出父容器但没出屏幕，必须不报。

        真机上「越出父容器」是常态（滚动列表的内容坐标 ≠ 可视区坐标），
        报它等于永远报警。如果这条测试挂了，说明判据被改回了几何越界。
        """
        tree = _root([
            {'attributes': {'type': 'Column', 'bounds': '[0,100][720,300]'},
             'children': [
                 # 子节点跑到父容器上方之外，但仍在屏幕内
                 _node('Text', '[0,50][720,150]'),
                 _node('Text', '[0,260][720,400]'),
             ]},
        ])
        self.assertEqual(_judge(tree), [],
                         '越出父容器但在屏幕内不该报 —— 真机上这是常态')

    def test_zero_size_widget_ignored(self):
        """面积 0 的节点看不见，越界也不构成可见缺陷。"""
        tree = _root([_node('Row', '[700,900][700,900]')])
        self.assertEqual(_judge(tree), [])

    def test_tiny_overflow_within_tolerance(self):
        """1~2px 是渲染取整，不算异常。"""
        over = LAYOUT_OVERFLOW_MIN_PX
        tree = _root([_node('Button', '[0,0][%d,100]' % (720 + over))])
        self.assertEqual(_judge(tree), [])

    def test_empty_tree_does_not_report(self):
        """空树/无窗口是 NO_WINDOW 的活，不该在这里再报一条。"""
        self.assertEqual(_judge(_root([], bounds='[0,0][0,0]')), [])

    def test_widget_inside_screen_ok(self):
        tree = _root([_node('Button', '[100,100][300,200]')])
        self.assertEqual(_judge(tree), [])


class TestLayoutAnomalyOnRealFixtures(unittest.TestCase):
    """7 个真机样本零误报 —— 判据的误报基线，可离线复现。"""

    def test_no_false_positive_on_real_pages(self):
        files = [p for p in sorted(glob.glob(os.path.join(FIXTURES, '*.json')))
                 if not p.endswith('.meta.json')]
        self.assertGreaterEqual(len(files), 7, '真机夹具不该少于 7 个')
        bad = []
        for p in files:
            with open(p, encoding='utf-8') as f:
                root = parse_layout(f.read())
            sig = Signals(bundle='com.demo.app')
            _judge_layout_anomaly(sig, root)
            if sig.anomalies:
                bad.append('%s → %s' % (os.path.basename(p),
                                        sig.anomalies[0].evidence[:80]))
        self.assertEqual(bad, [], '真机正常页面出现布局异常误报：\n' + '\n'.join(bad))


if __name__ == '__main__':
    unittest.main()
