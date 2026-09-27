"""A3 定位器健康度与自愈 的单元测试（主攻方向★）。

验收场景全部按分工卡 A3 原文设计：
- 降级链五级逐级可测；
- 「人为改 5 个控件 id，不人工干预自动恢复，成功率 ≥ 80%」；
- 连续失败达阈值自动触发自愈（结构变化场景，降级链救不回来的那种）。
不依赖真机、不依赖真网络。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect                     # noqa: E402
from ohauto.locator import LocatorManager, LocatorSpec         # noqa: E402
from ohauto.vision import HybridLocator, MockProvider, VisualTarget  # noqa: E402


def node(type_, id='', text='', bounds=(0, 0, 0, 0), clickable=False,
         descr='', hint='', parent=None):
    n = LayoutNode(type=type_, id=id, text=text, descr=descr, hint=hint,
                   clickable=clickable, rect=Rect(*bounds))
    if parent is not None:
        n.parent = parent
        parent.children.append(n)
    return n


def five_widget_page(ids):
    """5 个不同类型可交互控件的页面（验收场景的舞台）。"""
    root = node('root', bounds=(0, 0, 720, 1280))
    col = node('Column', parent=root)
    for t, i in zip(('Button', 'TextInput', 'Checkbox', 'Switch', 'Search'),
                    ids):
        node(t, id=i, bounds=(10, 10, 110, 50), clickable=True, parent=col)
    return root


CLUES = [{'id': ids, 'type': t, 'description': f'控件{k}'}
         for k, (t, ids) in enumerate(zip(('Button', 'TextInput', 'Checkbox',
                                           'Switch', 'Search'),
                                          ['w1', 'w2', 'w3', 'w4', 'w5']))]


class TestChainBasics(unittest.TestCase):
    def test_l1_id_exact_hit(self):
        m = LocatorManager()
        page = five_widget_page(['w1', 'w2', 'w3', 'w4', 'w5'])
        m.register(CLUES[0], page, page_signature='P1')
        r = m.locate(CLUES[0], page, page_signature='P1')
        self.assertEqual(r.level, 'id_exact')
        self.assertEqual(r.rect.left, 10)
        self.assertEqual(r.channel, 'tree')
        # 契约字段完整性：rect / channel / confidence / health 四个都有
        self.assertIsNotNone(r.health)
        self.assertGreater(r.confidence, 0.9)

    def test_l2_fuzzy_id_hit_after_prefix_change(self):
        """id 改了前缀：L1 落空，L2 模糊 id（包含匹配）接住。"""
        m = LocatorManager()
        page = five_widget_page(['w1', 'w2', 'w3', 'w4', 'w5'])
        m.register(CLUES[0], page, page_signature='P1')
        new_page = five_widget_page(['btn_w1_new', 'w2', 'w3', 'w4', 'w5'])
        r = m.locate(CLUES[0], new_page, page_signature='P1')
        self.assertEqual(r.level, 'id_fuzzy_text')
        h = m.health(r.locator_id)
        self.assertGreaterEqual(h.degrade_count, 1)

    def test_l3_path_type_hit_after_id_renamed(self):
        """id 彻底改名、结构没动：L3 层级路径+类型接住 —— 这正是
        B 交付的 type_fingerprint/control_key 的用武之地。"""
        m = LocatorManager()
        page = five_widget_page(['w1', 'w2', 'w3', 'w4', 'w5'])
        m.register(CLUES[2], page, page_signature='P1')
        new_page = five_widget_page(['w1', 'w2', 'renamed_3', 'w4', 'w5'])
        r = m.locate(CLUES[2], new_page, page_signature='P1')
        self.assertEqual(r.level, 'path_type')
        self.assertEqual(r.rect.left, 10)

    def test_chain_miss_without_fallback_returns_none(self):
        """无兜底坐标的 spec 全链路落空 → None（不瞎编结果）。"""
        m = LocatorManager()
        page = five_widget_page(['w1', 'w2', 'w3', 'w4', 'w5'])
        spec = m.register(CLUES[0], page, page_signature='P1')
        spec.fallback_center = None          # 显式去掉兜底
        empty = node('root')
        self.assertIsNone(m.locate(CLUES[0], empty, page_signature='P1'))
        h = m.health(spec.locator_id)
        self.assertEqual(h.consecutive_failures, 1)
        self.assertEqual(h.last_failure_reason, '全链路落空(5级)')


class TestAcceptance5Ids(unittest.TestCase):
    """分工卡验收原文：人为改 5 个控件 id，不人工干预自动恢复，
    成功率 ≥ 80%。"""

    def test_five_renamed_ids_auto_recover(self):
        m = LocatorManager()
        page = five_widget_page(['w1', 'w2', 'w3', 'w4', 'w5'])
        specs = [m.register(c, page, page_signature='P1') for c in CLUES]

        # 人为改动：5 个 id 全部换掉（结构不动）
        new_page = five_widget_page(['x1', 'x2', 'x3', 'x4', 'x5'])
        successes = 0
        for clue in CLUES:
            r = m.locate(clue, new_page, page_signature='P1')
            self.assertIsNotNone(r, f'{clue["description"]} 定位失败')
            successes += 1 if r.level in ('id_exact', 'id_fuzzy_text',
                                          'path_type', 'repaired') else 0
        self.assertEqual(successes, 5)
        for s in specs:
            self.assertGreaterEqual(m.health(s.locator_id).success_rate, 0.8)
            self.assertGreater(m.health(s.locator_id).degrade_count, 0,
                               '走了降级链才算「自愈」，直连成功不算')


class TestSelfHeal(unittest.TestCase):
    def _original_page(self):
        root = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=root)
        node('Button', id='pay_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=col)
        return root

    def _restructured_page(self):
        """结构也变了：Button 挪进新容器链，id 也换 —— 降级链 L1-L3 全灭。"""
        root = node('root', bounds=(0, 0, 720, 1280))
        stack = node('Stack', parent=root)
        row = node('Row', parent=stack)
        node('Button', id='confirm_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=row)
        return root

    def test_repair_triggers_after_threshold_without_manual_action(self):
        m = LocatorManager(failure_threshold=3)
        page = self._original_page()
        spec = m.register({'id': 'pay_btn', 'type': 'Button',
                           'description': '支付按钮'}, page,
                          page_signature='P1')
        new_page = self._restructured_page()

        # 第 1、2 次：降级链全灭 → 坐标兜底（记失败+告警）
        r1 = m.locate('支付按钮', new_page, page_signature='P1')
        self.assertEqual(r1.level, 'coordinate')
        self.assertTrue(r1.warning)
        self.assertEqual(m.health(spec.locator_id).consecutive_failures, 1)
        m.locate('支付按钮', new_page, page_signature='P1')
        self.assertEqual(m.health(spec.locator_id).consecutive_failures, 2)

        # 第 3 次：达到阈值，就地自愈并重试 —— 不人工干预
        r3 = m.locate('支付按钮', new_page, page_signature='P1')
        self.assertEqual(r3.level, 'repaired')
        self.assertIn('自愈', r3.warning)
        self.assertEqual(m.spec(spec.locator_id).target_id, 'confirm_btn')
        self.assertEqual(m.spec(spec.locator_id).generation, 1)
        self.assertEqual(m.health(spec.locator_id).consecutive_failures, 0)

        # 第 4 次：新一代定位器直连命中
        r4 = m.locate('支付按钮', new_page, page_signature='P1')
        self.assertEqual(r4.level, 'id_exact')

    def test_repair_report_lists_failures_when_ambiguous(self):
        """自愈无法定人（同类型多候选、无同文案）→ 如实报失败，不瞎换。"""
        m = LocatorManager(failure_threshold=1)
        page = self._original_page()
        spec = m.register({'id': 'pay_btn', 'type': 'Button',
                           'description': '支付按钮'}, page,
                          page_signature='P1')
        # 新页面：两个 Button，改 id 又无文案 —— 无法安全定人
        root = node('root')
        col = node('Column', parent=root)
        node('Button', id='a_btn', bounds=(0, 0, 10, 10), clickable=True,
             parent=col)
        node('Button', id='b_btn', bounds=(20, 0, 30, 10), clickable=True,
             parent=col)
        m.locate('支付按钮', root, page_signature='P1')   # 触发阈值=1
        rep = m.repair_locators('P1', page=root)
        self.assertEqual(rep.repaired, [])
        self.assertIn(spec.locator_id, rep.failed)
        self.assertEqual(m.spec(spec.locator_id).generation, 0,
                        '失败的自愈不能改定位器')

    def test_standalone_repair_uses_fresh_page(self):
        """C 集成日手动触发的口径：locate 只管记账（没到阈值不自动修），
        手动 repair 拿新鲜页面把定位器换到新一代。"""
        m = LocatorManager(failure_threshold=99)   # 关掉自动自愈
        page = self._original_page()
        m.register({'id': 'pay_btn', 'type': 'Button', 'description': '支付按钮'},
                   page, page_signature='P1')
        m.locate('支付按钮', self._restructured_page(), page_signature='P1')
        rep = m.repair_locators('P1', page=self._restructured_page(),
                                force=True)
        self.assertEqual(len(rep.repaired), 1)
        self.assertIn('confirm_btn', rep.details[0])


class TestHealthBookkeeping(unittest.TestCase):
    def test_success_and_failure_accounting(self):
        m = LocatorManager()
        page = five_widget_page(['w1', 'w2', 'w3', 'w4', 'w5'])
        spec = m.register(CLUES[0], page, page_signature='P1')
        m.locate(CLUES[0], page, page_signature='P1')
        h = m.health(spec.locator_id)
        self.assertEqual((h.attempts, h.successes, h.success_rate),
                         (1, 1, 1.0))
        m.record_locator_failure(spec.locator_id, '手动注入的失败')
        h = m.health(spec.locator_id)
        self.assertEqual((h.attempts, h.successes), (2, 1))
        self.assertAlmostEqual(h.success_rate, 0.5)
        self.assertEqual(h.last_failure_reason, '手动注入的失败')

    def test_all_health_snapshot(self):
        m = LocatorManager()
        page = five_widget_page(['w1', 'w2', 'w3', 'w4', 'w5'])
        spec = m.register(CLUES[0], page, page_signature='P1')
        m.locate(CLUES[0], page, page_signature='P1')
        snap = m.all_health()
        self.assertIn(spec.locator_id, snap)
        self.assertIn('success_rate', snap[spec.locator_id])


class TestVisionLevel(unittest.TestCase):
    def test_l4_vision_hit_when_tree_clues_all_dead(self):
        """id/text/descr 全无 + 控件挪了位置 + 尺寸变了（control_key 的
        geo 兜底也变）→ 只剩视觉。MockProvider 按描述关键词命中 hint。"""
        root = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=root)
        node('Image', hint='支付图标按钮', bounds=(10, 10, 110, 50),
             clickable=True, parent=col)

        m = LocatorManager(vision=HybridLocator(provider=MockProvider(),
                                                verbose=False))
        page = node('root', bounds=(0, 0, 720, 1280))
        col0 = node('Column', parent=page)
        node('Image', hint='支付图标按钮', bounds=(10, 10, 110, 50),
             clickable=True, parent=col0)
        m.register({'type': 'Image', 'description': '支付图标按钮'},
                   page, page_signature='P1')

        # 新页面：容器链不同 + 尺寸不同 → L3 的 control_key/父链全灭
        root2 = node('root', bounds=(0, 0, 720, 1280))
        stack2 = node('Stack', parent=root2)
        node('Image', hint='支付图标按钮', bounds=(10, 10, 130, 60),
             clickable=True, parent=stack2)

        r = m.locate('支付图标按钮', root2, page_signature='P1',
                     image_path='unused.png', screen_size=(720, 1280))
        self.assertIsNotNone(r)
        self.assertEqual(r.level, 'vision')
        self.assertFalse(r.uncertain, 'Mock 命中来自控件树 hint，属于混合通道')

    def test_no_vision_configured_skips_l4(self):
        """没配视觉 → L4 直接跳过，不抛错（可选能力不绑架主链路）。"""
        m = LocatorManager()          # vision=None
        page = node('root')
        m.register({'type': 'Button', 'description': '某按钮'}, page,
                   page_signature='P1')
        r = m.locate('某按钮', node('root'), page_signature='P1')
        # 无兜底坐标 → 全落空 → None，而不是异常
        self.assertIsNone(r)


if __name__ == '__main__':
    unittest.main()
