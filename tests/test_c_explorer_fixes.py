# -*- coding: utf-8 -*-
"""真机冒烟中对 explorer 的两处修正。

这两条都是**模拟设备测不出、真机才暴露**的问题，所以测试也按真机形态构造：
真机控件树里状态栏永远在最上方且带文本，这是关键前提。

1. `_page_title()` 原来按 `(top, -area)` 取「最靠上的」文本，而真机状态栏
   固定在 top≈32 且带文本（如「没有 SIM 卡」）—— 实测 6 个页面的标题
   全是同一句状态栏文案，报告里完全分不出谁是谁。

2. `CoverageReport.pages` 取 `len(StateGraph.states)`（内容签名去重），
   而该类 docstring 写的是「按结构签名归并页面」。单页应用只要点一下
   改了正文文字就会被算成新页面（实测计算器：点 5 次得到 6 个 state）。
   已**追加** `pages_structural` 字段而不改 `pages` 的既有语义。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.explorer import Explorer, CoverageReport       # noqa: E402
from ohauto.layout import parse_layout                      # noqa: E402


def _b(l, t, r, bo):
    """真机 bounds 写法 —— 别用别的形态（sim.py 为这个踩过坑）。"""
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': 'false', 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _tree(children):
    return {'attributes': {'type': 'Root', 'id': 'root',
                           'bounds': _b(0, 0, 720, 1280), 'visible': 'true'},
            'children': children}


#: 真机形态：状态栏在 top=32，正文标题在 top=87
REAL_PAGE = _tree([
    _node('Text', 'status', '没有 SIM 卡', _b(43, 32, 170, 50)),
    _node('Text', 'battery', '11%', _b(519, 32, 565, 50)),
    # 正文字号大（面积大）但 top 比标题低一点，用来验证「面积优先」
    _node('Text', 'page_title', '计算器', _b(104, 87, 189, 132)),
    _node('Text', 'hint', '按 = 出结果', _b(36, 157, 684, 342)),
])


class _StubDriver:
    bundle = 'com.demo.app'
    ability = 'EntryAbility'
    artifact_dir = None

    def __init__(self, root=None):
        self.root = root

    def refresh(self, *a, **kw):
        return self.root


def _explorer(tree):
    return Explorer(_StubDriver(parse_layout(tree)), verbose=False)


class TestPageTitleIgnoresStatusBar(unittest.TestCase):

    def test_title_is_not_status_bar_text(self):
        """★ 真机那个坑：标题必须不是状态栏文案。"""
        title = _explorer(REAL_PAGE)._page_title()
        self.assertNotEqual(title, '没有 SIM 卡',
                            '标题取了状态栏 —— 真机上所有页面会重名')
        self.assertNotEqual(title, '11%')
        self.assertEqual(title, '计算器')

    def test_title_falls_back_to_default_when_only_status_bar(self):
        """整棵树只有状态栏文本时，宁可给空标题让调用方回退，也别取状态栏。"""
        only_status = _tree([
            _node('Text', 'status', '没有 SIM 卡', _b(43, 32, 170, 50)),
        ])
        self.assertEqual(_explorer(only_status)._page_title(), '')

    def test_title_prefers_higher_text_over_larger_text(self):
        """★ 靠上优先，**不是**面积优先 —— 标题栏固定在页顶。

        这条是被真机数据修正过的：一度改成「面积优先」，结果真机计算器上
        正文提示「按 = 出结果」的面积是标题「计算器」的 30 倍，把标题挤掉了。
        """
        tree = _tree([
            _node('Text', 'small_top', '小字在上', _b(100, 200, 200, 220)),
            _node('Text', 'big_lower', '大字在下', _b(100, 300, 600, 380)),
        ])
        self.assertEqual(_explorer(tree)._page_title(), '小字在上')


class TestCoverageReportStructuralPages(unittest.TestCase):

    def test_field_exists_and_defaults_to_zero(self):
        r = CoverageReport(interactive_total=10, interactive_visited=5, pages=6)
        self.assertEqual(r.pages_structural, 0)

    def test_to_dict_carries_both_page_counts(self):
        r = CoverageReport(interactive_total=20, interactive_visited=8,
                           pages=6, pages_structural=1)
        d = r.to_dict()
        self.assertEqual(d['pages'], 6)
        self.assertEqual(d['pages_structural'], 1)
        self.assertAlmostEqual(d['ratio'], 0.4)

    def test_two_counts_differ_for_single_page_app(self):
        """单页应用的形态：状态多、页面少 —— 两个数必须能分别读出来。"""
        r = CoverageReport(interactive_total=20, interactive_visited=2,
                           pages=6, pages_structural=1)
        self.assertGreater(r.pages, r.pages_structural,
                           '单页应用的状态数应多于页面数')


if __name__ == '__main__':
    unittest.main(verbosity=2)
