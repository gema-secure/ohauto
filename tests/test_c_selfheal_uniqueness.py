"""自愈换代的「候选唯一性」—— 真机形态下的第 2 档线索（子树文案）。

背景：离线受控树上自愈 10/10，一上真机就报「重探索后无唯一匹配候选」。
根因是候选挑选只有三档 —— `control_key` 精确同款 / 自带 text·descr 同文案 /
候选池只剩一个。而真机上**可交互容器自身 id 与文案常常全空、文案在子节点**
（id 覆盖 5.62%），于是这类目标直接掉到最后一档：同类型兄弟好几个 → 放弃。

这一组钉子守新增的第 2 档（`LayoutNode.text_deep`，子树文案）与
「失败时要留下并列候选」这两件事；同时守住老档位的优先级没被改动。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect                      # noqa: E402
from ohauto.locator import LocatorManager                       # noqa: E402


def _row(label: str, cid: str = '') -> LayoutNode:
    """一个真机形态的可交互容器：自身无 id 无文案，文案在子节点。"""
    row = LayoutNode(type='Row', id=cid, text='', clickable=True,
                     rect=Rect(0, 0, 200, 60))
    row.children.append(LayoutNode(type='Text', text=label,
                                   rect=Rect(10, 10, 180, 40), parent=row))
    return row


def _tree(*rows: LayoutNode) -> LayoutNode:
    root = LayoutNode(type='Column', rect=Rect(0, 0, 720, 1280))
    for r in rows:
        r.parent = root
        root.children.append(r)
    return root


def _sibling(label: str) -> LayoutNode:
    n = LayoutNode(type='Row', text='', clickable=True, rect=Rect(0, 0, 40, 40))
    n.children.append(LayoutNode(type='Text', text=label,
                                 rect=Rect(0, 0, 30, 30), parent=n))
    return n


class TestSecondTierUsesSubtreeText(unittest.TestCase):
    """第 2 档：无 id、无自带文案的容器，靠子树文案定人。"""

    def _setup(self, labels):
        tree = _tree(*[_row(lab) for lab in labels])
        m = LocatorManager()
        target = tree.children[1]                    # 中间那个
        lid = m.register(target, tree, 'P').locator_id   # 页面签名与下面 repair 对齐
        return m, lid, tree

    def test_spec_keeps_subtree_text(self):
        m, lid, _ = self._setup(['甲', '乙', '丙'])
        self.assertEqual(m.spec(lid).text_deep, '乙')

    def test_unique_subtree_text_wins(self):
        """三个同类型兄弟、子树文案各不相同 → 能唯一选出中间那个。

        旧实现（只有 control_key / 自带文案两档）在这里必然返回 None，
        真机上就是这样报「线索全失效」的。
        """
        m, lid, _ = self._setup(['甲', '乙', '丙'])
        rep = m.repair_locators(page_signature='P', page=_tree(*[
            _row('甲'), _sibling('丙'), _row('乙')]), force=True)
        self.assertIn(lid, rep.repaired, rep.failed)
        # 换代后线索回填到实况节点：子树文案仍是「乙」，代次 +1
        self.assertEqual(m.spec(lid).text_deep, '乙')
        self.assertEqual(m.spec(lid).generation, 1)

    def test_ties_are_reported_not_swallowed(self):
        """两个兄弟子树文案一样 → 仍然不瞎选，但要说清并列了几个。"""
        m, lid, _ = self._setup(['甲', '乙', '丙'])
        dup = _tree(*[_row('乙'), _row('乙'), _row('甲')])
        rep = m.repair_locators(page_signature='P', page=dup, force=True)
        self.assertNotIn(lid, rep.repaired)
        self.assertIn('并列候选', rep.failed[lid])
        # 并列的就是那两个「乙」（同文案的兄弟及其子节点都在候选池里，
        # 所以只断言"确实不止一个" —— 关键是没瞎选、且留了痕）
        self.assertGreaterEqual(len(rep.ambiguous[lid]), 2)

    def test_ambiguous_entries_are_human_readable(self):
        m, lid, _ = self._setup(['甲', '乙'])
        rep = m.repair_locators(page_signature='P',
                                page=_tree(_sibling('乙'), _sibling('乙')),
                                force=True)
        self.assertTrue(all('«' in t or '#' in t for t in rep.ambiguous[lid]))


class TestOlderTiersStillWin(unittest.TestCase):
    """零回归：老档位优先级不变（有 id 用 id、有自带文案用文案）。"""

    def test_control_key_is_tried_before_subtree_text(self):
        tree = _tree(_row('甲', cid='row_a'), _row('乙', cid='row_b'))
        m = LocatorManager()
        lid = m.register(tree.children[1], tree, 'P').locator_id
        rep = m.repair_locators(page_signature='P',
                                page=_tree(_row('乙', cid='row_b')), force=True)
        self.assertIn(lid, rep.repaired)

    def test_own_text_still_works(self):
        tree = _tree(LayoutNode(type='Button', text='确认', clickable=True,
                                rect=Rect(0, 0, 100, 50)))
        m = LocatorManager()
        lid = m.register(tree.children[0], tree, 'P').locator_id
        rep = m.repair_locators(page_signature='P', page=_tree(
            LayoutNode(type='Button', text='确认', clickable=True,
                       rect=Rect(0, 0, 100, 50))), force=True)
        self.assertIn(lid, rep.repaired)


class TestSnapshotCoversTheNewClue(unittest.TestCase):
    """回滚必须把 text_deep 一起还原 —— 否则「报告说回滚了、状态却变了」。"""

    def test_rollback_restores_subtree_text(self):
        tree = _tree(_row('甲'), _row('乙'))
        m = LocatorManager()
        lid = m.register(tree.children[0], tree, 'P').locator_id
        before = m.spec(lid).text_deep
        # 新树上只有一个同类型候选但子树文案不同 → 验证档位过不去（L1/L3 都命中不了）
        rep = m.repair_locators(page_signature='P', page=_tree(_row('丁')),
                                force=True)
        self.assertNotIn(lid, rep.repaired)
        self.assertEqual(m.spec(lid).text_deep, before)
        self.assertEqual(m.spec(lid).generation, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
