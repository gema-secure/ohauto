"""验收钉子：自愈幂等代次 / within 边界 / 未注册 spec。

兼容整合派活包 `C交付-给A-2026-09-27.zip`（取代 09-25、09-26 两份）：

- A-0（P0）：`locator.py::_count_failure` 的幂等键只用 `_locate_seq`，
  而执行器走 matcher/layout **从不调 locate()** → 代次恒 0 → 回写全被
  当重复记账丢掉 → `consecutive_failures` 停在 1 → 自愈永不触发。
- A-1（高危）：`matcher.py::within()` ①嵌套容器重复命中（`nth` 取错控件）
  ②`walk()` 不过滤 visible，把调用方过滤掉不可见节点重新捞回。
- A-2（高危）：`locator.py::_resolve_spec` 只补 `_specs` 不补 `_health`
  → 传未注册 `LocatorSpec` 给 `locate()` 第一行就 KeyError。

全部离线运行：不需要真机、不需要 key、不需要网络。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect, flatten            # noqa: E402
from ohauto.matcher import ON                                  # noqa: E402
from ohauto.locator import (LocatorManager, LocatorSpec,        # noqa: E402
                            LocatorHealth)


def node(type_, id='', text='', bounds=(0, 0, 10, 10), clickable=False,
         visible=True, parent=None, children=()):
    n = LayoutNode(type=type_, id=id, text=text, clickable=clickable,
                   visible=visible, rect=Rect(*bounds))
    if parent is not None:
        n.parent = parent
        parent.children.append(n)
    for c in children:
        c.parent = n
        n.children.append(c)
    return n


# ============================================================ A-0 幂等代次

class TestA0AttemptIdempotency(unittest.TestCase):
    """`record_locator_failure(locator_id, reason, attempt=None)`：

    执行器给 attempt 时 **一次执行尝试 = 一次记账**；不给时旧语义完全不变。
    """

    def _manager_and_lid(self):
        mgr = LocatorManager()
        root = node('Root', children=[])
        pay = node('Button', text='支付', clickable=True, parent=root)
        self.assertIsNotNone(pay)
        spec = mgr.register({'text': '支付'}, root, 'P_PAY')
        return mgr, spec.locator_id

    def test_a0a_signature_keeps_attempt_optional(self):
        """签名向后兼容：attempt 必须**可选**。

        分工卡把 `record_locator_failure(locator_id, reason) -> None` 列为
        W2 冻结签名 —— 加一个带缺省值的参数不破坏它，B 的两参调用照旧可用。
        """
        import inspect
        params = inspect.signature(
            LocatorManager().record_locator_failure).parameters
        self.assertIn('attempt', params)
        self.assertIsNone(params['attempt'].default)

    def test_a0b_consecutive_attempts_accumulate_to_threshold(self):
        """连续 attempt=1..6 → consecutive_failures == 6（修复前恒为 1）。"""
        mgr, lid = self._manager_and_lid()
        for n in range(1, 7):
            mgr.record_locator_failure(lid, '执行失败#%d' % n, attempt=n)
        h = mgr.health(lid)
        self.assertEqual(h.consecutive_failures, 6)
        self.assertEqual(h.attempts, 6)

    def test_a0c_same_attempt_replay_is_idempotent(self):
        """同一个 attempt 重放 → 不累加（幂等仍然成立）。"""
        mgr, lid = self._manager_and_lid()
        for n in range(1, 4):
            mgr.record_locator_failure(lid, '执行失败#%d' % n, attempt=n)
        before = mgr.health(lid).consecutive_failures
        for _ in range(5):
            mgr.record_locator_failure(lid, '同一次重放', attempt=3)
        self.assertEqual(mgr.health(lid).consecutive_failures, before)
        self.assertEqual(before, 3)

    def test_a0d_threshold_reached_so_self_heal_can_fire(self):
        """阈值口径回归：3 次连续 attempt 就必须够得着默认阈值。

        这是 A-0 的**业务后果**钉子 —— 修复前 consecutive_failures 停在 1，
        `failure_threshold=3` 永远达不到，自愈的入口条件直接不成立。
        """
        mgr = LocatorManager(failure_threshold=3)
        root = node('Root', children=[])
        node('Button', text='支付', clickable=True, parent=root)
        lid = mgr.register({'text': '支付'}, root, 'P_PAY').locator_id
        for n in (1, 2, 3):
            mgr.record_locator_failure(lid, '执行失败', attempt=n)
        self.assertGreaterEqual(mgr.health(lid).consecutive_failures,
                                mgr.failure_threshold)

    def test_a0e_two_source_keys_do_not_collide(self):
        """内部代次与执行器 attempt 是**两个键空间**，不得互相吞掉。

        两个计数器各自独立单调、取值空间又都是"第几次"：若把裸序号混在
        一个整数空间里，就会出现「内部代次恰好 == 执行器 attempt」的假
        去重窗口 —— 一次真实失败被吞，自愈被推迟。这里直接钉住这个窗口。
        """
        mgr, lid = self._manager_and_lid()
        mgr._locate_seq = 1                       # 内部路径走到代次 1
        self.assertTrue(mgr._count_failure(lid, '内部#1'))
        self.assertFalse(mgr._count_failure(lid, '内部#1重放'))   # 同源同号去重
        self.assertTrue(mgr._count_failure(lid, '执行器 attempt=1', attempt=1))
        self.assertEqual(mgr.health(lid).consecutive_failures, 2)

    def test_a0f_legacy_two_arg_path_unchanged(self):
        """旧式两参调用 + locate 内部记账的幂等口径不变。

        `locate()` 全链路落空会内部记一次；同一次定位里 B 再按老签名
        回写一次，必须仍被去重（修复前是 2，这是 09-23 那条钉子）。
        """
        mgr = LocatorManager(failure_threshold=99)
        root = node('Root', children=[])
        lid = mgr.register({'text': '不存在的文案'}, root, 'P_X').locator_id

        self.assertIsNone(mgr.locate({'text': '不存在的文案'}, root, 'P_X'))
        self.assertEqual(mgr.health(lid).consecutive_failures, 1)

        mgr.record_locator_failure(lid, '执行器旧式两参回写')
        self.assertEqual(mgr.health(lid).consecutive_failures, 1,
                         '同一次定位内的两参回写必须被去重')

        # 下一次 locate 代次推进 → 照常记账
        self.assertIsNone(mgr.locate({'text': '不存在的文案'}, root, 'P_X'))
        self.assertEqual(mgr.health(lid).consecutive_failures, 2)

    def test_a0g_unknown_id_write_is_still_a_noop(self):
        """未知 locator_id 的失败回写仍然是无副作用 no-op（B「缺 id 不调」契约）。

        A-2 的修法给错漏的账本"就地补建"，但**不能**让失败回写顺手把
        账本建出来 —— 那会让 B 侧契约形同虚设，还会给垃圾 id 留账本。
        """
        mgr = LocatorManager()
        self.assertFalse(mgr._count_failure('L_NOT_EXIST', '缺 id'))
        self.assertNotIn('L_NOT_EXIST', mgr.all_health())


# ========================================================= A-1 within 两个洞

class TestA1WithinHoles(unittest.TestCase):
    """`Matcher.within()` 的三条约束型语义（详见 matcher.py 的 docstring）。"""

    @staticmethod
    def _nested_pool():
        """真机常见形态：Scroll 套 Scroll（父子同 type），内层 3 个 Button。"""
        b1 = node('Button', text='b1')
        b2 = node('Button', text='b2')
        b3 = node('Button', text='b3')
        inner = node('Scroll', children=[b1, b2, b3])
        outer = node('Scroll', children=[inner])
        root = node('Root', children=[outer])
        return flatten(root, only_visible=True)

    def test_a1a_nested_container_hits_once(self):
        """嵌套容器不得重复命中（修复前 3 个 Button 数成 6 个）。"""
        hits = ON.type('Button').within(ON.type('Scroll')).filter(self._nested_pool())
        self.assertEqual([n.text for n in hits], ['b1', 'b2', 'b3'])

    def test_a1b_nth_out_of_range_returns_empty(self):
        """`nth` 越界必须返回空，不得回绕给出真实错控件（修复前回绕到 b1）。

        这条比"取错控件"更糟：本该"找不到"，却静默返回了一个真实存在的
        错误控件 —— 上层无从分辨，点击会落到别的按钮上。
        """
        pool = self._nested_pool()
        m = ON.type('Button').within(ON.type('Scroll'))
        self.assertEqual(m.nth(3).filter(pool), [])
        self.assertEqual([n.text for n in m.nth(2).filter(pool)], ['b3'])
        self.assertEqual([n.text for n in m.nth(-1).filter(pool)], ['b3'])

    def test_a1c_invisible_nodes_are_not_pulled_back(self):
        """调用方过滤掉的不可见节点，不得被 within 的子树展开捞回。"""
        shown = node('Button', text='shown_btn')
        hidden = node('Button', text='hidden_btn', visible=False)
        scroll = node('Scroll', children=[shown, hidden])
        root = node('Root', children=[scroll])

        pool = flatten(root, only_visible=True)
        self.assertNotIn('hidden_btn', [n.text for n in pool])   # 夹具前提

        hits = ON.type('Button').within(ON.type('Scroll')).filter(pool)
        self.assertEqual([n.text for n in hits], ['shown_btn'])

    def test_a1d_callers_full_pool_intent_is_respected(self):
        """入参本身就含不可见节点时，within **不做**二次过滤。

        第 3 条语义是"继承调用方的可见性约定"，不是"永远过滤不可见" ——
        显式要全量的调用方不能被悄悄砍掉一半候选。
        """
        shown = node('Button', text='shown_btn')
        hidden = node('Button', text='hidden_btn', visible=False)
        scroll = node('Scroll', children=[shown, hidden])
        root = node('Root', children=[scroll])

        pool = flatten(root, only_visible=False)
        hits = ON.type('Button').within(ON.type('Scroll')).filter(pool)
        self.assertEqual(sorted(n.text for n in hits),
                         ['hidden_btn', 'shown_btn'])

    def test_a1e_single_container_semantics_unchanged(self):
        """回归：单层容器 + 容器外节点的老语义不变。"""
        target = node('Button', text='登录')
        outsider = node('Button', text='登录')
        page = node('Column', id='loginPage', children=[target])
        root = node('Root', children=[page, outsider])

        pool = flatten(root, only_visible=True)
        hits = ON.text('登录').within(ON.id('loginPage')).filter(pool)
        self.assertEqual(len(hits), 1)
        self.assertIs(hits[0], target)

    def test_a1f_missing_container_still_empty(self):
        """回归：容器不存在 → 空结果（不是整池兜底）。"""
        pool = self._nested_pool()
        hits = ON.text('b1').within(ON.id('不存在的容器')).filter(pool)
        self.assertEqual(hits, [])


# ======================================================== A-2 未注册 spec

class TestA2UnregisteredSpec(unittest.TestCase):

    @staticmethod
    def _spec(lid):
        return LocatorSpec(locator_id=lid, page_signature='P_TEST',
                           description='未注册的手工 spec', text='支付')

    def test_a2a_locate_accepts_unregistered_spec(self):
        """传未注册 LocatorSpec 进 locate() 不得崩（修复前第一行 KeyError）。"""
        mgr = LocatorManager()
        root = node('Root', children=[])
        res = mgr.locate(self._spec('L_TEST_UNREG'), root, page_signature='P_TEST')
        self.assertIsNone(res)                     # 树上没有目标 → None，但没炸

    def test_a2b_health_ledger_is_created(self):
        """未注册 spec 也会在 all_health() 里建账本。"""
        mgr = LocatorManager()
        root = node('Root', children=[])
        mgr.locate(self._spec('L_TEST_HEALTH'), root, page_signature='P_TEST')
        self.assertIn('L_TEST_HEALTH', mgr.all_health())

    def test_a2c_ledger_and_registry_go_together(self):
        """注册表与账本同生共死 —— 不许再出现"只补一边"的旁路。"""
        mgr = LocatorManager()
        root = node('Root', children=[])
        mgr.locate(self._spec('L_TEST_PAIR'), root, page_signature='P_TEST')
        self.assertIn('L_TEST_PAIR', mgr._specs)
        self.assertIn('L_TEST_PAIR', mgr._health)

    def test_a2d_registered_spec_health_is_not_reset(self):
        """已注册 spec 再走一次 locate() 不得把账本冲掉（setdefault 语义保留）。"""
        mgr = LocatorManager()
        root = node('Root', children=[])
        spec = mgr.register({'text': '支付'}, root, 'P_PAY')
        mgr.record_locator_failure(spec.locator_id, '先记一笔',
                                   attempt=1)
        before = mgr.health(spec.locator_id).consecutive_failures
        mgr.locate(spec, root, page_signature='P_PAY')
        self.assertGreaterEqual(
            mgr.health(spec.locator_id).consecutive_failures, before)
        self.assertIs(mgr.spec(spec.locator_id), spec)

    def test_a2e_health_query_stays_strict(self):
        """查询接口仍然严格：未知 id 抛 KeyError，不返回空账本。

        A-2 的教训是"注册链路必须补账本"，不是"查错 id 也该给个默认可信
        对象" —— 后者会把调用方拼错 id 的 bug 藏起来。
        """
        mgr = LocatorManager()
        with self.assertRaises(KeyError):
            mgr.health('L_NOT_EXIST')

    def test_a2f_health_of_creates_missing_ledger(self):
        """`_health_of` 是账本的唯一写入口：缺失就地补建、已有则原样返回。"""
        mgr = LocatorManager()
        h1 = mgr._health_of('L_X')
        self.assertIsInstance(h1, LocatorHealth)
        h1.attempts = 7
        self.assertIs(mgr._health_of('L_X'), h1)
        self.assertEqual(mgr.health('L_X').attempts, 7)


if __name__ == '__main__':
    unittest.main()
