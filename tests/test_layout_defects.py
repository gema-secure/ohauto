"""缺陷修复验收钉子。

覆盖以下几类缺陷：

- record_locator_failure 幂等 —— locate() 落空 + 执行器回写
         必须只计一次（修复前 consecutive_failures == 2）；
- C 2.1  自愈验证失败必须真回滚 —— spec 线索逐字段相等、generation 不
         自增、consecutive_failures 不清零（修复前「报已回滚但没回滚」）；
- B @A   A5 摘要器/控件清单双向覆盖 + 借子节点文案 —— 真机样本
         app_settings（0 id）/ sample_calc（19/20 只有 id）上必须可用；
- C 2.3  vision L1 补 deep 线索 + 取最紧匹配；保守版（默认）要求最紧
         候选文案与指令完全相等，激进版由 l1_require_exact=False 打开。

全部离线运行：不需要真机、不需要 key、不需要网络。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect, parse_layout          # noqa: E402
from ohauto.locator import LocatorManager                        # noqa: E402
from ohauto.treesum import control_catalog, summarize_tree       # noqa: E402
from ohauto.vision import (TieredVisionLocator,                  # noqa: E402
                           VisionConfigError, OpenAICompatibleProvider)


def node(type_, id='', text='', bounds=(0, 0, 0, 0), clickable=False,
         descr='', hint='', parent=None):
    n = LayoutNode(type=type_, id=id, text=text, descr=descr, hint=hint,
                   clickable=clickable, rect=Rect(*bounds))
    if parent is not None:
        n.parent = parent
        parent.children.append(n)
    return n


def real_tree(name):
    path = os.path.join(ROOT, 'datasets', 'gallery_13app', name)
    with open(path, 'r', encoding='utf-8') as f:
        return parse_layout(json.load(f))


# ================================================================ C 2.2 幂等

class TestFailureIdempotency(unittest.TestCase):
    """验收原文：locate() 落空 + 执行器回写后
    consecutive_failures == 1（修复前是 2）。"""

    def _manager_and_spec(self):
        m = LocatorManager()
        page = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=page)
        node('Button', id='pay_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=col)
        spec = m.register({'id': 'pay_btn', 'type': 'Button',
                           'description': '支付按钮'}, page,
                          page_signature='P1')
        spec.fallback_center = None          # 去掉坐标兜底 → 全链路可落空
        return m, spec

    def test_internal_record_plus_sink_writeback_counts_once(self):
        m, spec = self._manager_and_spec()
        empty = node('root')
        self.assertIsNone(m.locate('支付按钮', empty, page_signature='P1'))
        # 执行器按契约回写同一次失败
        m.record_locator_failure(spec.locator_id, '执行器回写：定位失败')
        h = m.health(spec.locator_id)
        self.assertEqual(h.consecutive_failures, 1,
                         '同一次定位失败被记了两次 —— 幂等闸失效')
        self.assertEqual(h.attempts, 1)

    def test_writeback_without_internal_record_still_counts(self):
        """反向保护：没有内部记账时（如 locate 命中但执行失败），回写必须照常计数。"""
        m, spec = self._manager_and_spec()
        m.record_locator_failure(spec.locator_id, '独立回写')
        self.assertEqual(m.health(spec.locator_id).consecutive_failures, 1)

    def test_next_attempt_counts_again(self):
        """幂等只覆盖同一次定位尝试：下一个代次照常重新计数。"""
        m, spec = self._manager_and_spec()
        empty = node('root')
        self.assertIsNone(m.locate('支付按钮', empty, page_signature='P1'))
        m.record_locator_failure(spec.locator_id, '第1次回写')
        self.assertIsNone(m.locate('支付按钮', empty, page_signature='P1'))
        m.record_locator_failure(spec.locator_id, '第2次回写')
        h = m.health(spec.locator_id)
        self.assertEqual(h.consecutive_failures, 2)
        self.assertEqual(h.attempts, 2)

    def test_threshold_not_half_triggered(self):
        """端到端：阈值=4 时，4 次真实失败（每次内部+回写）才触发自愈，
        而不是修复前的 2 次就触发。"""
        m = LocatorManager(failure_threshold=4)
        page = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=page)
        node('Button', id='pay_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=col)
        spec = m.register({'id': 'pay_btn', 'type': 'Button',
                           'description': '支付按钮'}, page,
                          page_signature='P1')
        spec.fallback_center = None
        empty = node('root')
        for _ in range(3):
            self.assertIsNone(m.locate('支付按钮', empty, page_signature='P1'))
            m.record_locator_failure(spec.locator_id, '回写')
            self.assertLess(m.health(spec.locator_id).consecutive_failures, 4)
        self.assertIsNone(m.locate('支付按钮', empty, page_signature='P1'))
        self.assertEqual(m.health(spec.locator_id).consecutive_failures, 4)


# ================================================================ C 2.1 真回滚

class TestRepairRollback(unittest.TestCase):
    """验证失败必须把 spec 恢复原状。
    用 monkeypatch 强制验证环节判不中（_lv_id_exact / _lv_path_type 都
    返回空），专测「验证失败分支」的回滚不变量。"""

    def _pages(self):
        page = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=page)
        node('Button', id='pay_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=col)
        # 结构也变了：Button 挪进新容器链、id 换掉 —— 逼出自愈换代
        new_page = node('root', bounds=(0, 0, 720, 1280))
        stack = node('Stack', parent=new_page)
        row = node('Row', parent=stack)
        node('Button', id='confirm_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=row)
        return page, new_page

    def test_verify_failure_rolls_back_spec_fully(self):
        # threshold=99：locate 只记账不自动自愈，repair 用 force=True 手动触发
        # （C 集成日口径），把「验证失败分支」单独隔离出来测。
        m = LocatorManager(failure_threshold=99)
        page, new_page = self._pages()
        spec = m.register({'id': 'pay_btn', 'type': 'Button',
                           'description': '支付按钮'}, page,
                          page_signature='P1')
        m.locate('支付按钮', new_page, page_signature='P1')   # 失败 1 次（不触发自愈）
        h = m.health(spec.locator_id)
        before = (spec.target_id, spec.text, spec.descr, spec.target_type,
                  spec.type_fp, spec.control_key, spec.parent_types,
                  spec.fallback_center)
        failures_before = h.consecutive_failures
        self.assertEqual(failures_before, 1)

        with mock.patch.object(LocatorManager, '_lv_id_exact',
                               lambda self, spec, page_: None), \
             mock.patch.object(LocatorManager, '_lv_path_type',
                               lambda self, spec, page_: (None, '')):
            rep = m.repair_locators('P1', page=new_page, force=True)

        self.assertIn(spec.locator_id, rep.failed)
        self.assertIn('已回滚', rep.failed[spec.locator_id])
        after = (spec.target_id, spec.text, spec.descr, spec.target_type,
                 spec.type_fp, spec.control_key, spec.parent_types,
                 spec.fallback_center)
        self.assertEqual(before, after, '验证失败的换代没有真回滚 —— spec 被污染')
        self.assertEqual(spec.generation, 0, '验证失败的换代不应自增 generation')
        self.assertEqual(h.repairs, 0, '验证失败不应计入 repairs')
        self.assertEqual(h.consecutive_failures, failures_before,
                         '验证失败不应清零 consecutive_failures（会推迟下次自愈）')

    def test_verify_success_still_commits(self):
        """对照组：验证通过时代照常提交（换代生效、计数清零）。"""
        m = LocatorManager(failure_threshold=99)
        page, new_page = self._pages()
        spec = m.register({'id': 'pay_btn', 'type': 'Button',
                           'description': '支付按钮'}, page,
                          page_signature='P1')
        m.locate('支付按钮', new_page, page_signature='P1')
        rep = m.repair_locators('P1', page=new_page, force=True)
        self.assertIn(spec.locator_id, rep.repaired)
        self.assertEqual(spec.generation, 1)
        self.assertEqual(spec.target_id, 'confirm_btn')
        self.assertEqual(m.health(spec.locator_id).consecutive_failures, 0)


# ================================================================ B @A A5 清单

class TestTreesumBorrowChildText(unittest.TestCase):
    """B@A：真机上 app_settings 0 个 id（标签在子 Text 上）、
    sample_calc 19/20 只有 id —— 清单/摘要必须双向覆盖 + 借子节点文案。"""

    def test_real_app_settings_borrowed_text_present(self):
        tree = real_tree('app_settings.json')
        catalog = control_catalog(tree)
        self.assertIn('deep=', catalog,
                      '真机可交互容器无自身文案，必须借子节点文案')
        # 「蓝牙」是设置页第一行的子 Text 标签：修复前清单里找不到它
        self.assertIn('蓝牙', catalog)
        # 借来的文案要截断，不能把整页文案灌进一行
        for line in catalog.splitlines():
            self.assertLessEqual(len(line), 220, f'清单行过长: {line!r}')

    def test_real_sample_calc_ids_kept(self):
        tree = real_tree('sample_calc.json')
        catalog = control_catalog(tree)
        self.assertIn('id=', catalog, '只有 id 的控件不能被丢')

    def test_summary_lines_show_deep_for_bare_containers(self):
        root = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=root)
        row = node('Flex', bounds=(0, 0, 720, 80), clickable=True, parent=col)
        node('Text', text='蓝牙', bounds=(20, 20, 100, 60), parent=row)
        node('Text', text='已关闭', bounds=(500, 20, 600, 60), parent=row)
        text = summarize_tree(root, header=False)
        self.assertIn('deep=', text)
        self.assertIn('蓝牙', text)

    def test_real_app_settings_compression_under_half(self):
        tree = real_tree('app_settings.json')
        out = summarize_tree(tree)
        head = out.splitlines()[0]
        ratio = float(head.split('（')[1].rstrip('%）'))
        self.assertLess(ratio, 50.0, '真机树压缩率必须 < 50%（A5 验收口径）')


# ================================================================ C 2.3 vision L1

class _ExplodingProvider:
    """只要被调用就炸 —— 用来证明 L1 真的没调模型。"""

    name = 'exploding'

    def locate(self, *a, **kw):
        raise AssertionError('L1 应当免调模型，却打到了 Provider')


class TestVisionL1DeepAndTightest(unittest.TestCase):

    def _tree(self):
        # 真机形态：可交互容器无文案，文案在子 Text 上
        root = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=root)
        row = node('Flex', bounds=(0, 0, 720, 80), clickable=True, parent=col)
        node('Text', text='蓝牙', bounds=(20, 20, 100, 60), parent=row)
        node('Text', text='已关闭', bounds=(500, 20, 600, 60), parent=row)
        return root

    def test_hints_carry_deep(self):
        loc = TieredVisionLocator(provider=_ExplodingProvider(), verbose=False)
        hints = loc._hints(self._tree())
        self.assertTrue(hints, 'hints 不应为空')
        flex = [h for h in hints if h['type'] == 'Flex']
        self.assertTrue(flex and flex[0]['deep'] == '蓝牙 已关闭',
                        f'线索缺 deep 或拼接不对: {flex}')

    def test_static_candidates_match_via_deep(self):
        loc = TieredVisionLocator(provider=_ExplodingProvider(), verbose=False)
        hints = loc._hints(self._tree())
        cands = loc._static_candidates(hints, '蓝牙')
        self.assertTrue(cands, '修复前这里命中 0 条（线索全空壳）')

    def test_l1_conservative_exact_hit_skips_model(self):
        # 可交互 Text 自身带文案「WiFi」→ deep 与指令完全相等 → L1 直接返回
        root = node('root', bounds=(0, 0, 720, 1280))
        col = node('Column', parent=root)
        node('Text', text='WiFi', bounds=(0, 0, 100, 40), clickable=True,
             parent=col)
        loc = TieredVisionLocator(provider=_ExplodingProvider(), verbose=False)
        r = loc.locate(root, 'unused.png', 'WiFi', 720, 1280)
        self.assertIsNotNone(r)
        self.assertEqual(r.source, 'tree')
        self.assertEqual(r.rect.area, 100 * 40)

    def test_l1_conservative_upgrades_when_not_exact(self):
        # 最紧候选 deep='蓝牙 已关闭' ≠ 指令 '蓝牙 已关闭 开关' → 保守版升级
        # L2 → Provider 被调用 → _ExplodingProvider 炸 → 证明真的升级了
        root = self._tree()
        loc = TieredVisionLocator(provider=_ExplodingProvider(), verbose=False)
        with self.assertRaises(AssertionError):
            loc.locate(root, 'unused.png', '蓝牙 已关闭 开关', 720, 1280)

    def test_l1_aggressive_returns_tightest_without_model(self):
        root = self._tree()
        loc = TieredVisionLocator(provider=_ExplodingProvider(), verbose=False,
                                  l1_require_exact=False)
        r = loc.locate(root, 'unused.png', '蓝牙', 720, 1280)
        self.assertIsNotNone(r)
        self.assertEqual(r.source, 'tree')
        self.assertEqual((r.rect.left, r.rect.top, r.rect.right, r.rect.bottom),
                         (0, 0, 720, 80), '激进版应返回最紧（面积最小）候选')


# ================================================================ 环境变量兼容

class TestEnvCompat(unittest.TestCase):

    def test_ohauto_llm_group_accepted(self):
        env = {'OHAUTO_LLM_BASE_URL': 'https://x', 'OHAUTO_LLM_API_KEY': 'k',
               'OHAUTO_LLM_MODEL': 'm'}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(OpenAICompatibleProvider.available())
            p = OpenAICompatibleProvider.from_env()
            self.assertEqual(p.model, 'm')

    def test_vision_group_has_priority(self):
        env = {'OHAUTO_VISION_MODEL': 'vision-model',
               'OHAUTO_LLM_BASE_URL': 'https://x', 'OHAUTO_LLM_API_KEY': 'k',
               'OHAUTO_LLM_MODEL': 'llm-model'}
        with mock.patch.dict(os.environ, env, clear=False):
            p = OpenAICompatibleProvider.from_env()
            self.assertEqual(p.model, 'vision-model')

    def test_missing_config_still_raises(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for n in ('OHAUTO_VISION_BASE_URL', 'OHAUTO_VISION_API_KEY',
                      'OHAUTO_VISION_MODEL', 'OHAUTO_LLM_BASE_URL',
                      'OHAUTO_LLM_API_KEY', 'OHAUTO_LLM_MODEL',
                      'OH_LLM_BASE_URL', 'OH_LLM_API_KEY', 'OH_LLM_MODEL'):
                os.environ.pop(n, None)
            self.assertFalse(OpenAICompatibleProvider.available())
            with self.assertRaises(VisionConfigError):
                OpenAICompatibleProvider.from_env()


if __name__ == '__main__':
    unittest.main()
