# -*- coding: utf-8 -*-
"""挑战 #6 沉淀链路的钉子：driver 留痕带 node_spec + 沉淀规则 + 红线。

这里钉的是三件事（都是 09-25 剖析出的真卡点）：
  1. Driver 留痕必须带 node_spec（id/text/text_deep）—— 否则沉淀只能退化成 type 歧义；
  2. 沉淀反解：id 优先、文案兜底、解析不出给 unresolved —— **不猜 type、不猜坐标**；
  3. 红线：产物里永远不允许出现 tap_xy / 坐标。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ohauto import action
from ohauto.driver import Driver, Step
from ohauto.matcher import ON
from ohauto.sim import FakeHdc
from tools.trace_to_case import steps_to_case


def _make_driver() -> Driver:
    d = Driver(bundle='com.demo.app', ability='EntryAbility',
               hdc=FakeHdc(start_page='login', verbose=False),
               verbose=False, default_timeout=250, poll_interval=50,
               sleep_fn=lambda s: None)
    d.start()
    return d


class TestDriverNodeSpec(unittest.TestCase):
    """留痕必须自带可定位规格 —— 挑战 #6 的原料。"""

    def test_tap_records_id_spec(self):
        d = _make_driver()
        d.tap(ON.id('username'))
        step = d.steps[-1]
        self.assertIsNotNone(step.node_spec, 'tap 留痕必须带 node_spec')
        self.assertEqual(step.node_spec.get('id'), 'username')

    def test_tap_records_text_when_no_id(self):
        d = _make_driver()
        d.tap(ON.text('登录'))
        step = d.steps[-1]
        self.assertIsNotNone(step.node_spec)
        # FakeHdc 的登录按钮同时有 id 和 text —— 不假设它无 id，
        # 只验证文案确实被记下（沉淀时 id 优先，规则另有钉子）
        self.assertTrue((step.node_spec or {}).get('text'))

    def test_to_dict_carries_node_spec(self):
        d = _make_driver()
        d.tap(ON.id('username'))
        dmp = d.steps[-1].to_dict()
        self.assertIn('node_spec', dmp)
        self.assertEqual(dmp['node_spec'].get('id'), 'username')

    def test_node_spec_never_has_coords(self):
        """红线：规格里不允许出现任何坐标痕迹。"""
        d = _make_driver()
        d.tap(ON.id('username'))
        banned = {'x', 'y', 'coords', 'tap_xy', 'left', 'top', 'center'}
        self.assertFalse(banned & set(d.steps[-1].node_spec or {}))


class TestStepsToCase(unittest.TestCase):
    """沉淀反解规则：id 优先 / 文案兜底 / unresolved 不猜。"""

    def test_id_wins_over_text(self):
        s = Step(index=1, kind='tap', ok=True,
                 node_spec={'id': 'btn_go', 'text': '下一页'})
        case = steps_to_case([s])
        self.assertEqual(case['steps'][1], {'tap': {'id': 'btn_go'}})

    def test_text_fallback(self):
        s = Step(index=1, kind='tap', ok=True,
                 node_spec={'text': '下一页', 'type': 'Button'})
        case = steps_to_case([s])
        self.assertEqual(case['steps'][1], {'tap': {'text': '下一页'}})

    def test_type_never_used(self):
        """只有 type 时必须给 unresolved —— type 歧义是沉淀跑不通的根因。"""
        s = Step(index=1, kind='tap', ok=True, node_spec={'type': 'Image'})
        case = steps_to_case([s])
        self.assertIn('unresolved', case['steps'][1])
        self.assertEqual(case['stats']['unresolved'], 1)

    def test_unresolved_carries_clue_not_guess(self):
        s = Step(index=1, kind='tap', ok=True, node_path='Row > Image',
                 node_spec=None)
        case = steps_to_case([s])
        u = case['steps'][1]['unresolved']
        self.assertEqual(u['action'], 'tap')
        self.assertEqual(u['node_path'], 'Row > Image')

    def test_input_carries_value(self):
        s = Step(index=1, kind='input', ok=True, value='alice',
                 node_spec={'id': 'username'})
        case = steps_to_case([s])
        self.assertEqual(case['steps'][1],
                         {'input': {'id': 'username', 'value': 'alice'}})

    def test_case_starts_with_start(self):
        s = Step(index=1, kind='tap', ok=True, node_spec={'id': 'a'})
        case = steps_to_case([s])
        self.assertEqual(case['steps'][0], {'start': True})

    def test_red_line_no_tap_xy_in_dump(self):
        """产物全文不允许出现 tap_xy（红线第 5 条）。"""
        steps = [Step(index=1, kind='tap', ok=True,
                      node_spec={'id': 'a', 'text': 'b', 'type': 'Button'}),
                 Step(index=2, kind='tap', ok=True, node_spec={'type': 'Image'})]
        case = steps_to_case(steps)
        import json
        blob = json.dumps(case, ensure_ascii=False)
        self.assertNotIn('tap_xy', blob)
        self.assertNotIn('coords', blob)


class TestCompatWithB(unittest.TestCase):
    """与 B 的 action.trace_to_steps 的关系：路径不同、产物同向，都无坐标。"""

    def test_both_paths_emit_no_coordinates(self):
        d = _make_driver()
        d.tap(ON.id('username'))
        d.input(ON.id('username'), 'alice')
        # B 的库内路径
        legacy = action.trace_to_steps(d)
        blob = repr(legacy)
        self.assertNotIn('tap_xy', blob)
        # C 的工装路径
        mine = steps_to_case([s for s in d.steps if s.ok])
        self.assertNotIn('tap_xy', repr(mine))


if __name__ == '__main__':
    unittest.main()
