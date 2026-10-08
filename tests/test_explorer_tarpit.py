"""Tarpit 防粘滞的单测 —— 「点一下、文案变一点」的页面不许把 BFS 队列灌满。

背景见 explorer.py 的 TarpitPolicy 段与
项目内部调研档案（仓库外）§三（HapTest --simk）。

红线（也是这里的验收口径）：
  拦截只影响「要不要入队探索」，**不影响记账** —— 状态照加、边照记、
  相似度和原因照存，报告里查得到「为什么没探这一页」。绝不静默。

全部基于 `FakeHdc` / 纯控件树字典，**不需要真机**。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import Driver                                    # noqa: E402
from ohauto.explorer import (Budget, Explorer, PageSignature,       # noqa: E402
                             TarpitPolicy, content_similarity)
from ohauto.layout import parse_layout                              # noqa: E402
from ohauto.sim import FakeHdc                                      # noqa: E402


def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='true', **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _tree(children):
    a = {'type': 'Root', 'id': 'root', 'bounds': _b(0, 0, 1080, 2340),
         'visible': 'true'}
    return {'attributes': a, 'children': children}


def _list_page(title, changed_item=None):
    """一张 21 节点的列表页。changed_item 指定哪一个条目的文案变了。

    21 行内容里只改 1 行 → 两页相似度 20/22 ≈ 0.909 ≥ 0.90（阈值）。
    """
    children = [_node('Text', 'title', title, _b(40, 120, 500, 200), 'false')]
    for i in range(19):
        text = f'条目{i}' + ('（已更新）' if i == changed_item else '')
        children.append(_node('Text', f'item_{i}', text,
                              _b(40, 240 + i * 60, 1000, 280 + i * 60), 'false'))
    children.append(_node('Button', 'btn_next', '下一页', _b(120, 1400, 960, 1500)))
    return _tree(children)


def _counter_page(n):
    """计数器页：结构恒定，只有数字变（每页只改 1/4 行，相似度 ~0.5 < 阈值）。"""
    return _tree([
        _node('Text', 'tv_count', f'计数 {n}', _b(40, 120, 500, 200), 'false'),
        _node('Button', 'btn_add', '加一', _b(120, 400, 960, 500)),
    ])


def _chain_app():
    """两页结构完全不同的普通应用（对照组：正常页面必须照常入队）。"""
    pages = {
        'login': _tree([
            _node('TextInput', 'username', '', _b(120, 400, 960, 500)),
            _node('Button', 'btn_login', '登录', _b(120, 720, 960, 820)),
        ]),
        'home': _tree([
            _node('Text', 'tv_title', '首页', _b(40, 120, 500, 200), 'false'),
            _node('Button', 'btn_logout', '退出登录', _b(120, 980, 960, 1060)),
            _node('Button', 'btn_settings', '设置', _b(120, 800, 960, 900)),
        ]),
    }
    trans = {('login', 'btn_login'): 'home'}
    return pages, trans


class _SimCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_tarpit_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _explore(self, pages, trans, tarpit=None, max_pages=8,
                 start_page='login'):
        sim = FakeHdc(start_page=start_page, seed=pages, transitions=trans)
        d = Driver(bundle='com.demo.app', hdc=sim, artifact_dir=self.tmp,
                   verbose=False, sleep_fn=lambda _s: None)
        ex = Explorer(d, artifact_dir=self.tmp, verbose=False,
                      tarpit_policy=tarpit)
        g = ex.explore(max_pages, Budget(max_pages=max_pages,
                                         max_actions_per_page=6,
                                         max_seconds=60, max_steps=100),
                       return_back=False)
        return ex, g


# ================================================================ 相似度函数

class TestContentSimilarity(unittest.TestCase):

    def test_identical_is_one(self):
        s = PageSignature(content='a;b;c', structural='x')
        self.assertEqual(content_similarity(s, PageSignature(content='a;b;c',
                                                             structural='y')), 1.0)

    def test_disjoint_is_zero(self):
        s = PageSignature(content='a;b', structural='x')
        self.assertEqual(content_similarity(s, PageSignature(content='c;d',
                                                             structural='y')), 0.0)

    def test_partial_change_is_fraction(self):
        """4 行改 1 行 = 交集 3 / 并集 5 = 0.6 —— 开关切换不该被当成「没走出原页」。"""
        s = PageSignature(content='a;b;c;d', structural='x')
        t = PageSignature(content='a;b;c;e', structural='y')
        self.assertAlmostEqual(content_similarity(s, t), 0.6)

    def test_policy_rejects_bad_threshold(self):
        with self.assertRaises(ValueError):
            TarpitPolicy(sim_threshold=1.5)
        with self.assertRaises(ValueError):
            TarpitPolicy(max_family_states=0)


# ================================================================ 探索行为

class TestTarpitOnSim(_SimCase):

    def test_near_identical_variant_is_recorded_but_not_enqueued(self):
        """列表翻页（21 行只改 1 行）：状态照加、边照记，但**不入队探索**。"""
        pages = {'login': _list_page('第 1 页'),
                 'p2': _list_page('第 1 页', changed_item=5)}
        trans = {('login', 'btn_next'): 'p2'}
        ex, g = self._explore(pages, trans)

        self.assertEqual(len(g.states), 2, '状态必须照常登记，不许丢')
        self.assertEqual(len(ex.tarpit_hits), 1)
        hit = ex.tarpit_hits[0]
        self.assertIn('相似度', hit['reason'])
        # 边上要有可查的证据 —— 「为什么没探这一页」必须写进报告
        edges = [t for t in g.transitions if t.dst == hit['dst'] and t.src == hit['src']]
        self.assertTrue(edges, '跳转边必须保留')
        self.assertIn('tarpit', edges[0].note)
        # 没有从 p2 出发的任何边 —— 它没被探索
        p2_sid = hit['dst']
        self.assertFalse([t for t in g.transitions if t.src == p2_sid],
                         '被拦截的状态不许被展开')

    def test_genuinely_new_page_is_still_enqueued(self):
        """结构不同的正常新页面：零拦截，照常入队（对照，防「防粘滞误杀」）。"""
        pages, trans = _chain_app()
        ex, g = self._explore(pages, trans)
        self.assertEqual(ex.tarpit_hits, [])
        # home 被探索过：btn_logout 的点击必然留下一条件边（自环也算）
        self.assertTrue(any(t.src != t.dst or t.note == '页面未变化'
                            for t in g.transitions))
        self.assertEqual(len(g.states), 2)

    def test_family_cap_blocks_endless_same_structure_chain(self):
        """计数器链：每跳相似度都低于阈值，但同结构状态到上限就封顶。"""
        names = [f'c{i}' for i in range(1, 7)]          # c1..c6
        pages = {n: _counter_page(i) for i, n in enumerate(names, 1)}
        trans = {(names[i - 2], 'btn_add'): names[i - 1] for i in range(2, 7)}
        ex, g = self._explore(pages, trans, start_page='c1')

        # 入口 c1 + 入队 c2/c3/c4（族计数到 4）→ c5 被封顶拦截，c6 永不发现
        self.assertEqual(len(g.states), 5, 'c5 照加不入队，c6 根本到不了')
        self.assertEqual(len(ex.tarpit_hits), 1)
        self.assertIn('同族封顶', ex.tarpit_hits[0]['reason'])
        self.assertIn('上限', ex.tarpit_hits[0]['reason'])

    def test_disabled_policy_restores_old_behavior(self):
        """enabled=False：完全恢复旧行为，小变体照样入队。"""
        pages = {'login': _list_page('第 1 页'),
                 'p2': _list_page('第 1 页', changed_item=5)}
        trans = {('login', 'btn_next'): 'p2'}
        ex, g = self._explore(pages, trans, tarpit=TarpitPolicy(enabled=False))
        self.assertEqual(ex.tarpit_hits, [])
        p2 = next((s for s in g.states.values() if s.path),
                  None)                                # 有路径 = 被导航进入过
        self.assertIsNotNone(p2)
        # p2 被展开过：它的 btn_next 点击留下了自环（无转出定义 → 页面未变化）
        self.assertTrue(any(t.src == p2.sid and t.dst == p2.sid
                            for t in g.transitions), 'p2 必须被探索')

    def test_tarpit_evidence_lands_in_saved_graph(self):
        """save_graph 的产物里必须带 tarpit 台账 —— 报告链路的最后一环。"""
        pages = {'login': _list_page('第 1 页'),
                 'p2': _list_page('第 1 页', changed_item=5)}
        trans = {('login', 'btn_next'): 'p2'}
        ex, _g = self._explore(pages, trans)
        path = os.path.join(self.tmp, 'graph.json')
        ex.save_graph(path)
        with open(path, encoding='utf-8') as f:
            payload = json.load(f)
        self.assertIn('tarpit', payload)
        self.assertEqual(len(payload['tarpit']['hits']), 1)
        self.assertTrue(payload['tarpit']['policy']['enabled'])


if __name__ == '__main__':
    unittest.main()
