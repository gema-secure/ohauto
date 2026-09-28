"""C 派活 B-0 / B-1 的钉子测试（2026-09-27）。

来源：`C交付-给B-2026-09-27/` 的 `派活-B0-空壳用例校验补闸.md` 与
`派活-B1-explorergenerate_case不可重放.md`。

    B-0  generator.py::validate_case   空壳用例（只 start/waitIdle/screenshot）被放行
    B-1  explorer.py::generate_case    把边集当轨迹串 → 产物必然重放失败

除了 C 给的两条钉子，这里多加了两条我自己认为更关键的：

    ★ B-0 端到端：provider 吐空壳 → GenerationOutcome 必须**判不可执行**，且不进修复链路
    ★ B-1 真跑：产物**真的能执行**（不是只检查「相邻边首尾相接」这种静态形状）——
      静态能对上但跑不起来的用例，正是这次要根除的那类东西。

全部离线，不碰设备/网络。
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 模块级 helper 造的临时目录统一挂到进程根目录下，退出时整体清理（评审 P2：只建不删）
_TMP_ROOT = tempfile.mkdtemp(prefix='ohauto_b01_root_')
atexit.register(shutil.rmtree, _TMP_ROOT, True)
sys.path.insert(0, ROOT)

from ohauto.action import load_case, run_steps                        # noqa: E402
from ohauto.driver import Driver                                      # noqa: E402
from ohauto.explorer import (Explorer, StateGraph, Transition,        # noqa: E402
                             _all_chains, _chain_transitions,
                             _usable_transitions)
from ohauto.generator import (Case, Generator, LLMProvider,           # noqa: E402
                              RejectReason, validate_case)
from ohauto.sim import FakeHdc                                        # noqa: E402

BUNDLE = 'com.demo.app'


def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='true'):
    return {'attributes': {'type': type_, 'id': cid, 'text': text,
                           'bounds': bounds, 'clickable': clickable,
                           'visible': 'true', 'enabled': 'true'}}


def _chain_app(n=4):
    """n 页线性应用：每页一个「下一步」按钮通向下一页。"""
    names = ['login'] + [f'p{i}' for i in range(2, n + 1)]
    pages, trans = {}, {}
    for i, name in enumerate(names):
        pages[name] = {'attributes': {'type': 'Root', 'id': f'{name}Root',
                                      'bounds': _b(0, 0, 1080, 2340),
                                      'visible': 'true'},
                       'children': [
                           _node(f'Head{i}', f'{name}_head', f'页面{name}',
                                 _b(40, 120, 500, 200), 'false'),
                           _node('Button', f'{name}_next', '下一步',
                                 _b(120, 400, 960, 500)),
                       ]}
        if i + 1 < len(names):
            trans[(name, f'{name}_next')] = names[i + 1]
    return pages, trans


def _no_sleep(_s):
    return None


def _branch_stub(logs=None):
    """C 的 verify 脚本用的那种桩：只有 graph.transitions / driver / log。"""
    obj = SimpleNamespace()
    obj.graph = SimpleNamespace(transitions=[
        Transition(src='S1', dst='S2', control='btn_to_S2',
                   control_spec={'text': 'btn_to_S2'}, ok=True),
        Transition(src='S1', dst='S3', control='btn_to_S3',
                   control_spec={'text': 'btn_to_S3'}, ok=True),
    ])
    obj.driver = SimpleNamespace(bundle=BUNDLE, ability='EntryAbility')
    obj.log = (logs if logs is not None else []).append
    return obj


def _gen(stub, name='用例'):
    out = os.path.join(tempfile.mkdtemp(prefix='cases_', dir=_TMP_ROOT),
                       'case.yaml')
    Explorer.generate_case(stub, out, name=name)
    return out


# ================================================================ B-0 用例级总闸

class TestEmptyCaseGate(unittest.TestCase):
    """★ C 派活 B-0（P0）：空壳用例必须被**用例级**总闸拦下。

    L3 真机实测的后果：**空壳 2/2 全过、真引用控件 0/3** ——
    逐步检查抓不到它，因为每一步都合法，**无从证伪**。
    """

    def _issues(self, steps):
        return validate_case(Case(name='t', bundle=BUNDLE, steps=steps))

    def test_three_step_shell_is_rejected(self):
        """★ C 给的原句：`{'start'},{'waitIdle':2},{'screenshot':'x'}` → issues 非空。"""
        issues = self._issues([{'start': True}, {'waitIdle': 2},
                               {'screenshot': 'x'}])
        self.assertTrue(issues, '三步空壳用例被放行了')
        self.assertEqual(issues[0].reason, RejectReason.NO_SUBSTANCE)
        self.assertIn('空壳', issues[0].detail)
        self.assertIn('不会失败', issues[0].detail)

    def test_start_only_is_rejected(self):
        self.assertEqual([i.reason for i in self._issues([{'start': True}])],
                         [RejectReason.NO_SUBSTANCE])

    def test_back_only_is_rejected(self):
        """`back` 也不会因为应用的行为而失败（真失败只是设备问题）→ 不算做事。"""
        self.assertEqual(
            [i.reason for i in self._issues([{'start': True}, {'back': True}])],
            [RejectReason.NO_SUBSTANCE])

    def test_named_wait_counts_as_substantive(self):
        """★ C 问的那个点：`waitFor` / `waitGone` **算**实质动作，别误杀。

        C 建议的白名单把 `waitGone` 划进「不引用控件」。我按**判据**更正：
        两者都是**条件等待** —— 条件不成立就失败，失败原因指向被测应用，
        所以它们是能失败的步骤（`start + waitFor(某控件出现)` 是有效的冒烟测试）。
        """
        for act in ({'waitFor': {'id': 'a', 'timeout': 8000}},
                    {'waitGone': {'text': '加载中'}}):
            with self.subTest(act=act):
                self.assertEqual(
                    self._issues([{'start': True}, act]), [], act)

    def test_wait_idle_alone_is_not_enough(self):
        """但 `waitIdle` 不一样：它只是等稳定，永远不会因为应用行为而失败。"""
        self.assertEqual(
            [i.reason for i in self._issues([{'start': True},
                                             {'waitIdle': True}])],
            [RejectReason.NO_SUBSTANCE])

    def test_shell_and_missing_start_both_reported(self):
        """两条问题都要报 —— 只报一条会把排查带偏。"""
        reasons = [i.reason for i in self._issues([{'waitIdle': 1}])]
        self.assertIn(RejectReason.NO_SUBSTANCE, reasons)
        self.assertIn(RejectReason.DSL_INVALID, reasons)

    def test_normal_cases_still_pass(self):
        """反向确认：该放行的照放 —— 交互、断言、跨页用例都不受影响。"""
        for steps in (
            [{'start': True}, {'tap': {'id': 'btn_login'}}],
            [{'start': True}, {'input': {'id': 'u', 'value': 'x'}},
             {'tap': {'id': 'btn_login'}}],
            [{'start': True}, {'swipe': {'direction': 'up', 'scale': 0.5}}],
            [{'start': True}, {'assert': {'exists': {'text': '首页'}}}],
        ):
            with self.subTest(steps=steps):
                self.assertEqual(self._issues(steps), [])

    def test_issue_is_fatal_so_the_gate_actually_blocks(self):
        """★ 光有 issue 不够：它必须是 `fatal`，否则生成链路会照样放行。"""
        self.assertTrue(self._issues([{'start': True}])[0].fatal)

    def test_specific_problems_are_reported_before_the_gate(self):
        """★ 总闸必须排在**所有逐步检查之后**。

        生成链路取「首个 fatal」当失败原因 —— 如果总闸抢先报，
        「你用了硬编码坐标」这种更具体、更可操作的问题就被盖住了，
        修的人会去「把用例做实」而不是去删坐标，等于修错方向。
        （这条是我改完跑全量时被 4 条老测试打回来才发现的。）
        """
        issues = self._issues([{'start': True}, {'tap_xy': {'x': 1, 'y': 2}}])
        self.assertEqual(issues[0].reason, RejectReason.HARDCODED_COORD, issues)
        self.assertIn(RejectReason.NO_SUBSTANCE, [i.reason for i in issues],
                      '两条问题都要报 —— 逐个修完才算过关')

    def test_illegal_action_reported_before_the_gate(self):
        issues = self._issues([{'start': True}, {'teleport': {}}])
        self.assertEqual(issues[0].reason, RejectReason.DSL_INVALID, issues)


class _ShellProvider(LLMProvider):
    """两阶段都吐空壳 —— 模拟 L3 实测里模型对负样本的行为。"""

    name = 'shell'

    def complete(self, prompt: str) -> str:
        if '拆成测试点清单' in prompt:
            return json.dumps({'test_points': [
                {'kind': '功能', 'title': '登录', 'precondition': '应用已启动',
                 'expect': '进入首页'}]}, ensure_ascii=False)
        return json.dumps({'name': '空壳', 'steps': [
            {'start': True}, {'waitIdle': 2}, {'screenshot': 'x'}]},
            ensure_ascii=False)


class TestEmptyCaseGateEndToEnd(unittest.TestCase):
    """★ 端到端：provider 吐空壳 → 生成器必须判**不可执行**。

    这条比 `validate_case` 的单测更关键 —— 它证明总闸真的接在了生成链路上，
    而不是一个没人调的函数。
    """

    def test_generator_rejects_the_shell(self):
        g = Generator(provider=_ShellProvider(), bundle=BUNDLE)
        out = g.generate('用户能登录')
        self.assertFalse(out.ok, f'空壳用例被当成了可执行：{out.case}')
        self.assertEqual(out.reason, RejectReason.NO_SUBSTANCE, out.detail)
        self.assertTrue(out.issues)

    def test_shell_counts_into_the_reason_breakdown(self):
        """批量报告里能看见这类失败 —— 否则「可执行率」又会虚高。"""
        g = Generator(provider=_ShellProvider(), bundle=BUNDLE)
        rep = g.generate_many(['d1', 'd2'])
        self.assertIn(RejectReason.NO_SUBSTANCE.value, rep.reasons(), rep.reasons())
        self.assertEqual(rep.executable, 0)


# ================================================================ B-1 可重放

class TestChainTransitions(unittest.TestCase):
    """把**边集**切成首尾相接的路径 —— B-1 的核心算法。"""

    def _t(self, src, dst, label):
        return Transition(src=src, dst=dst, control=label,
                          control_spec={'text': label}, ok=True)

    def test_branch_keeps_only_the_contiguous_edge(self):
        edges = [self._t('S1', 'S2', 'a'), self._t('S1', 'S3', 'b')]
        picked, skipped = _chain_transitions(edges)
        self.assertEqual([t.dst for t in picked], ['S2'])
        self.assertEqual([t.dst for t in skipped], ['S3'])

    def test_linear_graph_is_fully_chained(self):
        edges = [self._t('S1', 'S2', 'a'), self._t('S2', 'S3', 'b'),
                 self._t('S3', 'S4', 'c')]
        picked, skipped = _chain_transitions(edges)
        self.assertEqual(len(picked), 3)
        self.assertEqual(skipped, [])

    def test_out_of_order_input_still_chains(self):
        """★ 原缺陷的根：**边集没有顺序保证** —— 输入顺序反过来也必须串对。"""
        edges = [self._t('S2', 'S3', 'b'), self._t('S1', 'S2', 'a'),
                 self._t('S3', 'S4', 'c')]
        picked, skipped = _chain_transitions(edges)
        self.assertEqual([t.dst for t in picked], ['S2', 'S3', 'S4'])
        self.assertEqual(skipped, [])

    def test_self_loop_and_failed_edges_are_excluded(self):
        graph = SimpleNamespace(transitions=[
            self._t('S1', 'S2', 'ok'),
            Transition(src='S2', dst='S2', control='self',
                       control_spec={'text': 'self'}, ok=True),
            Transition(src='S2', dst='S3', control='failed',
                       control_spec={'text': 'failed'}, ok=False),
            Transition(src='S2', dst='S4', control='nospec'),
        ])
        self.assertEqual([t.control for t in _usable_transitions(graph)], ['ok'])

    def test_all_chains_covers_every_edge_exactly_once(self):
        edges = [self._t('S1', 'S2', 'a'), self._t('S1', 'S3', 'b'),
                 self._t('S3', 'S4', 'c'), self._t('X1', 'X2', 'd')]
        chains = _all_chains(edges)
        flat = [t for c in chains for t in c]
        self.assertEqual(len(flat), len(edges), '拆链不许丢边、也不许重复')
        for c in chains:                       # 每条链内部必须首尾相接
            for prev, nxt in zip(c, c[1:]):
                self.assertEqual(prev.dst, nxt.src)


class TestGenerateCaseProduct(unittest.TestCase):
    """★ C 的两条钉子：产物首尾相接 + 舍弃的边有痕迹。"""

    def test_branch_product_is_contiguous(self):
        path = _gen(_branch_stub())
        case = load_case(path)
        taps = [s['tap']['text'] for s in case['steps'] if 'tap' in s]
        self.assertEqual(taps, ['btn_to_S2'],
                         '同一来源页扇出的两条边不能都串进来')

    def test_skipped_edge_is_recorded_in_the_product(self):
        """★ 硬要求：**不许静默丢** —— 产物里必须写清舍弃了哪条边。"""
        logs = []
        path = _gen(_branch_stub(logs))
        case = load_case(path)
        note = str(case.get('note', ''))
        self.assertIn('btn_to_S3', note, f'产物 note 没交代舍弃的边：{note!r}')
        self.assertIn('未纳入', note)
        self.assertTrue(any('btn_to_S3' in m for m in logs),
                        f'日志里也要有痕迹：{logs}')

    def test_fully_connected_graph_says_nothing_was_skipped(self):
        stub = _branch_stub()
        stub.graph.transitions = [
            Transition(src='S1', dst='S2', control='a',
                       control_spec={'text': 'a'}, ok=True),
            Transition(src='S2', dst='S3', control='b',
                       control_spec={'text': 'b'}, ok=True),
        ]
        case = load_case(_gen(stub))
        self.assertIn('覆盖全部', str(case.get('note', '')))
        self.assertEqual(len([s for s in case['steps'] if 'tap' in s]), 2)

    def test_empty_graph_does_not_pretend_to_have_coverage(self):
        stub = _branch_stub()
        stub.graph.transitions = []
        case = load_case(_gen(stub))
        self.assertEqual([s for s in case['steps'] if 'tap' in s], [])
        self.assertIn('没有可用', str(case.get('note', '')))

    def test_every_tap_step_is_followed_by_a_wait(self):
        case = load_case(_gen(_branch_stub()))
        kinds = [next(iter(s)) for s in case['steps']]
        for i, k in enumerate(kinds):
            if k == 'tap':
                self.assertEqual(kinds[i + 1], 'waitIdle',
                                 '点击之后要等界面稳定，否则下一步会打在动效上')


class TestGenerateCaseIsActuallyReplayable(unittest.TestCase):
    """★★ 比形状检查更硬的一条：**产物真的能执行**。

    「相邻边首尾相接」只是静态形状；静态能对上但跑不起来的用例，
    正是 B-1 要根除的那类东西。所以这里把探索产物拿一台**新的模拟设备**跑一遍。
    """

    def _explore(self, n=4):
        pages, trans = _chain_app(n)
        sim = FakeHdc(start_page='login', seed=pages, transitions=trans)
        d = Driver(bundle=BUNDLE, hdc=sim, artifact_dir=None, verbose=False,
                   sleep_fn=_no_sleep)
        ex = Explorer(d, verbose=False)
        ex.explore(n + 2, __import__('ohauto.explorer', fromlist=['Budget'])
                   .Budget(max_pages=n + 2, max_actions_per_page=4))
        return sim, d, ex

    def test_explored_product_runs_green_on_a_fresh_device(self):
        _sim, _d, ex = self._explore(4)
        tmp = tempfile.mkdtemp(prefix='ohauto_b01_')
        self.addCleanup(shutil.rmtree, tmp, True)
        path = ex.generate_case(os.path.join(tmp, 'case.yaml'), name='探索回归')
        case = load_case(path)
        self.assertGreaterEqual(
            len([s for s in case['steps'] if 'tap' in s]), 2,
            '四页应用的探索产物应当至少有两条跳转')

        pages, trans = _chain_app(4)
        sim2 = FakeHdc(start_page='login', seed=pages, transitions=trans)
        d2 = Driver(bundle=BUNDLE, hdc=sim2, artifact_dir=None, verbose=False,
                    sleep_fn=_no_sleep)
        rep = run_steps(d2, case['steps'])
        self.assertEqual(rep.failed, 0,
                         f'产物重放失败：{rep.errors}\n用例：{case}')
        self.assertGreater(rep.passed, 0)

    def test_generate_cases_covers_all_edges_across_files(self):
        _sim, _d, ex = self._explore(4)
        tmp = tempfile.mkdtemp(prefix='ohauto_b01_')
        self.addCleanup(shutil.rmtree, tmp, True)
        paths = ex.generate_cases(tmp, name_prefix='探索回归')
        self.assertGreaterEqual(len(paths), 1)

        seen, taps_total = set(), 0
        for p in paths:
            case = load_case(p)
            taps = [s['tap'] for s in case['steps'] if 'tap' in s]
            taps_total += len(taps)
            seen.update(json.dumps(t, ensure_ascii=False, sort_keys=True)
                        for t in taps)
            # 每条产物自己也要能跑
            pages, trans = _chain_app(4)
            sim2 = FakeHdc(start_page='login', seed=pages, transitions=trans)
            d2 = Driver(bundle=BUNDLE, hdc=sim2, artifact_dir=None,
                        verbose=False, sleep_fn=_no_sleep)
            rep = run_steps(d2, case['steps'])
            self.assertEqual(rep.failed, 0, f'{os.path.basename(p)} 跑不通：{rep.errors}')

        edges = len(_usable_transitions(ex.graph))
        self.assertGreaterEqual(len(seen), edges,
                                f'拆出的用例没有覆盖全部 {edges} 条边')


NO_SUBSTANCE_CN = RejectReason.NO_SUBSTANCE.cn


if __name__ == '__main__':
    unittest.main(verbosity=2)
