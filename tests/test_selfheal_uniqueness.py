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


def _btn(cid: str, x: int) -> LayoutNode:
    """真机计算器按键形态：有 id、**没有文案**（树里读不到字）。"""
    return LayoutNode(type='Button', id=cid, text='', clickable=True,
                      rect=Rect(x, 600, x + 100, 660))


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


class _StubVision:
    """假视觉通道：`locate()` 直接返回预设目标（不碰网络）。"""

    def __init__(self, target):
        self._target = target
        self.calls = []

    def locate(self, root, image_path, instruction, w, h):
        self.calls.append((image_path, instruction, w, h))
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


def _vt(rect, confidence=0.9, uncertain=False):
    from ohauto.vision import VisualTarget
    return VisualTarget(label='7', rect=rect, confidence=confidence,
                        uncertain=uncertain)


class TestFifthTierSwitchesChannel(unittest.TestCase):
    """第 5 档：树内线索全灭时**换通道**（视觉），而不是去猜。

    真机现场：计算器键盘的 `L001_7` —— 唯一可辨识的线索是 id，场景把 id 改了，
    而按键在控件树里连文案都没有（子树文案为空），同类型兄弟 18 个。
    树内四档全灭 → 视觉兜底（「7」在截图上是有字面图形的）。
    """

    def _scenario(self, target):
        """原树靶子 id=7；新树三个 Button（id 全不同、无文案）→ 树内四档全灭。"""
        old = _tree(_btn('7', 0))
        m = LocatorManager(vision=_StubVision(target))
        lid = m.register(old.children[0], old, 'P').locator_id
        new = _tree(_btn('D', 0), _btn('C', 200), _btn('7_new', 400))
        return m, lid, new

    def test_unique_vision_mapping_repairs(self):
        m, lid, new = self._scenario(_vt(Rect(400, 600, 500, 660)))
        rep = m.repair_locators(page_signature='P', page=new, force=True,
                                image_path='x.png', screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [lid])
        self.assertIn('via=vision', rep.details[0])
        self.assertEqual(m.spec(lid).target_id, '7_new')
        self.assertEqual(m.spec(lid).generation, 1)

    def test_unlabeled_target_is_not_repaired_and_not_guessed(self):
        """不给图 → 老行为：如实失败，不瞎选。"""
        m, lid, new = self._scenario(_vt(Rect(400, 600, 500, 660)))
        rep = m.repair_locators(page_signature='P', page=new, force=True)
        self.assertEqual(rep.repaired, [])
        self.assertIn(lid, rep.failed)

    def test_uncertain_vision_is_refused(self):
        m, lid, new = self._scenario(
            _vt(Rect(400, 600, 500, 660), uncertain=True))
        rep = m.repair_locators(page_signature='P', page=new, force=True,
                                image_path='x.png', screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [])

    def test_low_confidence_vision_is_refused(self):
        m, lid, new = self._scenario(_vt(Rect(400, 600, 500, 660), confidence=0.1))
        rep = m.repair_locators(page_signature='P', page=new, force=True,
                                image_path='x.png', screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [])

    def test_vision_box_matching_two_nodes_is_refused(self):
        """视觉框同时对上两个候选（IoU 并列）→ 仍然不选。"""
        old = _tree(_btn('7', 0))
        m = LocatorManager(vision=_StubVision(_vt(Rect(400, 600, 500, 660))))
        lid = m.register(old.children[0], old, 'P').locator_id
        twin_a = _btn('D', 400)
        twin_b = _btn('C', 400)                 # 同一个位置，IoU 必然并列
        rep = m.repair_locators(page_signature='P',
                                page=_tree(twin_a, twin_b), force=True,
                                image_path='x.png', screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [])

    def test_container_and_child_with_same_bounds_prefers_the_child(self):
        """★ 真机实测形状：容器与子节点 **bounds 完全相同**（计算器的 GridItem
        与它里面的按键），视觉框对两者的 IoU 都是 1.0。这不是"两个候选"，
        是同一个区域的两层 —— 取更深的那层，容器只是壳。
        """
        old = _tree(_btn('7', 0))
        m = LocatorManager(vision=_StubVision(_vt(Rect(400, 600, 500, 660))))
        lid = m.register(old.children[0], old, 'P').locator_id
        # 新树：GridItem 容器与 Button 子节点同 rect，另外两个干扰按键
        box = LayoutNode(type='GridItem', rect=Rect(400, 600, 500, 660))
        inner = _btn('7_new', 400)
        inner.parent = box
        box.children.append(inner)
        root = _tree(_btn('D', 0), _btn('C', 200))
        box.parent = root
        root.children.append(box)
        rep = m.repair_locators(page_signature='P', page=root, force=True,
                                image_path='x.png', screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [lid])
        self.assertEqual(m.spec(lid).target_id, '7_new')

    def test_vision_box_far_from_every_node_is_refused(self):
        m, lid, new = self._scenario(_vt(Rect(0, 0, 20, 20)))
        rep = m.repair_locators(page_signature='P', page=new, force=True,
                                image_path='x.png', screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [])

    def test_vision_exception_does_not_break_the_repair_pass(self):
        """视觉是增强项：它炸了也不能把自愈这一趟带崩。"""
        m, lid, new = self._scenario(RuntimeError('provider down'))
        rep = m.repair_locators(page_signature='P', page=new, force=True,
                                image_path='x.png', screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [])
        self.assertIn(lid, rep.failed)

    def test_tier_one_still_wins_without_touching_vision(self):
        """有树内线索时不换通道 —— 便宜的先走（也省一次模型调用）。"""
        old = _tree(_btn('7', 0))
        stub = _StubVision(_vt(Rect(0, 0, 10, 10)))
        m = LocatorManager(vision=stub)
        lid = m.register(old.children[0], old, 'P').locator_id
        rep = m.repair_locators(page_signature='P', page=_tree(_btn('7', 0)),
                                force=True, image_path='x.png',
                                screen_size=(720, 1280))
        self.assertEqual(rep.repaired, [lid])
        self.assertNotIn('via=vision', rep.details[0])
        self.assertEqual(stub.calls, [])
