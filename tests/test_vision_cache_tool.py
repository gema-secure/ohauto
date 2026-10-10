# -*- coding: utf-8 -*-
"""C 集成层回归钉子：B14 测量工装的多轮重放状态管理。

背景：单遍探索的视觉缓存命中率**恒为 0**（结构性：每个状态只处理一次，
无重访即无命中）——B14 的正确测量是 ``--rounds`` 多轮模式：每轮
force-stop + 重启 → 相同页面集合重现，视觉页缓存跨轮留存 → 第 2 轮起
全命中，3 轮即 67% ≥60%。

多轮模式的前提是「重置探索台账但**保留**视觉缓存与统计」——台账若不重置，
第二次 ``explore()`` 会因状态图已满（``len(states) < max_pages`` 不成立）
而空转；缓存若被重置，则退回单遍口径、命中恒 0。本文件钉住这对行为。

复现：``python -m unittest tests.test_vision_cache_tool -q``
"""
import unittest
from types import SimpleNamespace

from ohauto.explorer import Budget, DialogVerdict, StateGraph
from tools.measure_vision_cache import _reset_exploration_state, _snapshot


class TestResetExplorationState(unittest.TestCase):
    """每轮重置探索台账；视觉页缓存与 vision_stats 必须原样保留。"""

    def _fake_explorer(self) -> SimpleNamespace:
        ex = SimpleNamespace()
        ex.graph = StateGraph()
        ex._universe = {'a', 'b'}
        ex._done = {'x'}
        ex._per_page = {'p': {'visited': 1}}
        ex._exhausted = {'e'}
        ex._blocked = {'bl'}
        ex._tarpit_family = {'f': 3}
        ex.tarpit_hits = [{'src': 's1'}]
        ex.back_verdicts = ['exact']
        ex.skipped = [{'label': 'l', 'reason': 'r'}]
        ex.dialog = DialogVerdict()
        ex.last_budget = Budget()
        # 被测契约的关键：这两样跨轮留存
        ex._vision_page_cache = {'fp1': ['fake-node']}
        ex.vision_stats = {'calls': 2, 'cache_hits': 0, 'cache_misses': 2}
        return ex

    def test_ledgers_are_reset(self):
        ex = self._fake_explorer()
        _reset_exploration_state(ex)
        self.assertEqual(len(ex.graph.states), 0)
        self.assertIsInstance(ex.graph, StateGraph)
        for name in ('_universe', '_done', '_per_page', '_exhausted',
                     '_blocked', '_tarpit_family', 'tarpit_hits',
                     'back_verdicts', 'skipped'):
            self.assertEqual(len(getattr(ex, name)), 0, name)
        self.assertIsInstance(ex.dialog, DialogVerdict)
        self.assertIsNone(ex.last_budget)

    def test_vision_cache_and_stats_survive(self):
        ex = self._fake_explorer()
        _reset_exploration_state(ex)
        self.assertEqual(ex._vision_page_cache, {'fp1': ['fake-node']})
        self.assertEqual(ex.vision_stats,
                         {'calls': 2, 'cache_hits': 0, 'cache_misses': 2})

    def test_reset_is_repeatable(self):
        """连跑 3 轮（3 轮即 KPI 口径）台账每次都归零、缓存仍留存。"""
        ex = self._fake_explorer()
        for _ in range(3):
            ex._universe.add('another')
            ex.tarpit_hits.append({'src': 's2'})
            _reset_exploration_state(ex)
            self.assertEqual(len(ex._universe), 0)
            self.assertEqual(len(ex.tarpit_hits), 0)
        self.assertEqual(ex._vision_page_cache, {'fp1': ['fake-node']})


class TestSnapshot(unittest.TestCase):
    """轮间差分用快照：六个计数键取 int，缺键补 0。"""

    def test_six_keys_and_missing_default_zero(self):
        stats = {'calls': 1, 'cache_hits': '2', 'cache_misses': 3,
                 'errors': 0, 'suggested': 5, 'tapped_ok': 6}
        self.assertEqual(_snapshot(stats),
                         {'calls': 1, 'cache_hits': 2, 'cache_misses': 3,
                          'errors': 0, 'suggested': 5, 'tapped_ok': 6})
        self.assertEqual(set(_snapshot({}).values()), {0})


if __name__ == '__main__':
    unittest.main()
