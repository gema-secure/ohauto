"""样本量的钉子 —— 描述集必须能凑到规划要求的条数，且**不许悄悄凑不满**。

背景（见阶段性总结的「KPI 层」未落实第 2 条）：
`executable_rate` 只有 3 条描述（入口页实测只派生得出 **1** 条正样本
+ 2 条固定负样本），而规划原文要求「**20 条**描述生成后逐条执行」。
分母不是 20 时，报出来的百分比既不可比，又容易被读成「已按规划验过」。

这组钉子守三件事：
1. 多页模式下描述**按页并集**派生（入口页 1 条 → 三页 6 条）；
2. 并集去重且保序（同一控件在多页都出现时只出一条）；
3. `--require-samples` 不足即**停止**（退出码 2），不产出会被误读的百分比。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import eval_nl_generation_real as ev                        # noqa: E402
from ohauto.layout import LayoutNode, Rect                  # noqa: E402


def _tree(items):
    """按 (id, 文案, 是否可点) 造一棵最小树。"""
    root = LayoutNode(type='Column', rect=Rect(0, 0, 720, 1280))
    for cid, text, clickable in items:
        n = LayoutNode(type='Button', id=cid, text=text, clickable=clickable,
                       rect=Rect(10, 10, 100, 60), parent=root)
        root.children.append(n)
    return root


INDEX = _tree([('btn_go_second', '进入第二页', True),
               ('tv_probe_always', '无条件渲染', False)])
SECOND = _tree([('btn_go_third', '去第三页', True),
                ('tv_second_title', '第二页', False)])
THIRD = _tree([('btn_inc', '加一', True), ('btn_back', '返回', True)])


class TestUnionAcrossPages(unittest.TestCase):

    def test_entry_page_alone_is_thin(self):
        """入口页只有 1 条 —— 这正是「3 条样本」的来源，固化成事实。"""
        self.assertEqual(ev.build_descriptions(INDEX, 20), ['点击「进入第二页」'])

    def test_union_covers_every_page(self):
        got = ev.build_descriptions_from_pages(
            [('Index', INDEX), ('Second', SECOND), ('Third', THIRD)], 20)
        self.assertEqual(got, ['点击「进入第二页」', '点击「去第三页」',
                               '点击「加一」', '点击「返回」'])

    def test_duplicates_across_pages_are_merged(self):
        """同一个控件在多页都出现时只出一条，且保持首次出现的顺序。"""
        same = _tree([('btn_common', '公共按钮', True)])
        got = ev.build_descriptions_from_pages(
            [('A', INDEX), ('B', same), ('C', same)], 20)
        self.assertEqual(got.count('点击「公共按钮」'), 1)
        self.assertEqual(got[0], '点击「进入第二页」')

    def test_maximum_is_respected(self):
        got = ev.build_descriptions_from_pages(
            [('Index', INDEX), ('Second', SECOND), ('Third', THIRD)], 2)
        self.assertEqual(len(got), 2)

    def test_empty_input_is_empty_not_an_exception(self):
        self.assertEqual(ev.build_descriptions_from_pages([], 20), [])
        self.assertEqual(ev.build_descriptions_from_pages(None, 20), [])

    def test_all_pages_yield_nothing(self):
        """整页没有可交互且带文案/id 的控件时返回空 —— 由调用方报「没有描述」。"""
        blank = _tree([('', '', True)])
        self.assertEqual(ev.build_descriptions_from_pages([('X', blank)], 20), [])


class TestPromptsFile(unittest.TestCase):
    """补充描述文件：空行与 `#` 注释必须跳过 —— 注释被当成描述会直接污染 KPI。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, text):
        p = os.path.join(self.tmp.name, 'p.txt')
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        return p

    def test_reads_one_description_per_line(self):
        p = self._write('点击 7\n点击 8\n')
        self.assertEqual(ev.load_prompts_file(p), ['点击 7', '点击 8'])

    def test_blank_lines_and_comments_are_skipped(self):
        p = self._write('# 出处：真机树 sample_calc.json\n\n点击 7\n   \n# 口径说明\n点击 8\n')
        self.assertEqual(ev.load_prompts_file(p), ['点击 7', '点击 8'])

    def test_order_is_preserved(self):
        p = self._write('甲\n乙\n丙\n')
        self.assertEqual(ev.load_prompts_file(p), ['甲', '乙', '丙'])

    def test_shipped_prompts_file_is_usable(self):
        """仓库里那份补充描述必须真的读得出 5 条（不含注释）。"""
        p = os.path.join(ROOT, 'tools', 'b8_prompts_calc.txt')
        self.assertTrue(os.path.isfile(p), p)
        got = ev.load_prompts_file(p)
        self.assertEqual(len(got), 5, got)
        self.assertTrue(all(not d.startswith('#') for d in got))


class TestShortSampleGate(unittest.TestCase):
    """`--require-samples` 不足时必须**停止**，而不是产出一个好看的百分比。"""

    def test_gate_returns_none_when_enough(self):
        self.assertIsNone(ev.sample_gate(20, 20))
        self.assertIsNone(ev.sample_gate(25, 20))

    def test_gate_returns_none_when_no_floor_is_set(self):
        """0 = 旧行为（不设下限）—— 3 条样本照样能跑，不改变既有口径。"""
        self.assertIsNone(ev.sample_gate(3, 0))

    def test_gate_stops_on_the_real_3_sample_measurement(self):
        """★ 把实测的「3 条」固化：它必须触发停止。"""
        stop = ev.sample_gate(1, 20)          # 1 条正样本（实测入口页派生的就是 1）
        self.assertIsNotNone(stop)
        self.assertIn('1 条', stop)
        self.assertIn('20', stop)

    def test_gate_message_says_how_to_fix_it(self):
        """只报错不够 —— 操作者要能照着把样本补到 20。"""
        stop = ev.sample_gate(1, 20)
        for hint in ('--multi-page', '--prompts', 'calculator'):
            self.assertIn(hint, stop)

    def test_cli_exposes_the_floor(self):
        """闸必须从命令行可达（否则纯函数测绿了、工装却用不上）。"""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            ev.main(['--help'])
        self.assertIn('--require-samples', buf.getvalue())

    def test_gate_list_is_the_official_20(self):
        """规划原文的门槛值写进代码注释与帮助里 —— 20 这个数字不是随手取的。"""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            ev.main(['--help'])
        self.assertIn('20', buf.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
