"""`trace_to_steps` 的完备性钉子：**任何一条成功留痕都必须有产物**。

判据不是「某几个 kind 处理了」，而是「有没有 kind 会凭空消失」。
缺了步骤的脚本恰恰是「看起来能跑」的那种 —— 它会把一条永远测不到东西的
用例混进回归集，比当场报错更坏。

这条口子曾经真实存在：`waitGone` / `longPress` / `fling` / `tap_xy` 以及
`input` 缺 `value` 的情形都会掉进 `elif` 链的末尾，一声不响地消失。

全部离线：只用 `Step` 桩，不碰设备、不碰网络。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.action import (UNRESOLVED_ACTION, run_steps,          # noqa: E402
                           trace_to_steps)
from ohauto.driver import Step                                    # noqa: E402

BUNDLE = 'com.demo.app'

#: driver 实际会记录的**全部** kind（由 `_new_step()` 的调用点枚举而来）。
#: 这张表就是「完备性」的分母 —— driver 加动作时同步加，钉子才会跟着变严。
RECORDED_KINDS = (
    'start', 'tap', 'longPress', 'input', 'swipe', 'back', 'home',
    'waitFor', 'waitGone', 'tap_xy', 'fling',
    'assert.exists', 'assert.gone', 'assert.text',
)

#: DSL 认识的动作名（对 `_exec_one` 分派表取并集）。
#: 用它守「产物不许发明动作」—— 产物里出现表外的名字，执行时必挂。
DSL_ACTIONS = frozenset({
    'start', 'launch', 'stop', 'tap', 'click', 'tap_xy', 'double_tap',
    'doubleclick', 'long_press', 'longclick', 'input', 'inputtext', 'fill',
    'swipe', 'fling', 'waitFor', 'wait_for', 'waitGone', 'wait_gone',
    'waitIdle', 'wait_idle', 'assert', 'screenshot', 'screencap',
    'back', 'key_back', 'home', 'key_home', 'key',
})


def _step(kind, **kw):
    kw.setdefault('ok', True)
    return Step(index=kw.pop('index', 1), kind=kind, **kw)


class _TraceDriver:
    """只提供 `steps` 的桩 —— `trace_to_steps` 只读这一个字段。"""

    def __init__(self, steps):
        self.steps = steps


def _one_of_every_kind():
    """每个 kind 一条留痕：有的能还原、有的只能占位，但**都必须有产物**。"""
    return [
        _step('start', index=1),
        _step('tap', index=2, node_path='Column > Button#btn_ok'),
        _step('longPress', index=3, node_path='Column > Button#btn_ok'),
        _step('input', index=4, node_path='Row > TextInput#username',
              value='alice'),
        _step('input', index=5, node_path='Row > TextInput#username'),
        _step('swipe', index=6, target='up', value=0.6),
        _step('back', index=7),
        _step('home', index=8),
        _step('waitFor', index=9, node_path='Button#btn_ok'),
        _step('waitGone', index=10, node_path='Button#btn_gone'),
        _step('tap_xy', index=11, value=(540, 770), coords=(540, 770)),
        _step('fling', index=12, value=2),
        _step('assert.exists', index=13, target='id=btn_ok'),
        _step('assert.gone', index=14, target="text='加载中'"),
        _step('assert.text', index=15, target='id=tv_title', value='首页'),
        _step('assert.color', index=16, target='id=tv_title'),
        _step('somethingBrandNew', index=17),
    ]


def _action_of(step):
    """取产物动作名；占位步骤返回 `UNRESOLVED_ACTION`。"""
    return next(iter(step))


class TestNoKindIsSilentlyDropped(unittest.TestCase):
    """★ 主钉子：除「调用方显式关掉」之外，一步都不许少。"""

    def test_every_successful_trace_has_a_product(self):
        steps = _one_of_every_kind()
        out = trace_to_steps(_TraceDriver(steps),
                             include_waits=True, include_asserts=True)
        self.assertEqual(len(out), len(steps),
                         '有留痕没有产物 —— 这就是静默丢')

    def test_fixture_covers_every_recorded_kind(self):
        """分母守卫：`RECORDED_KINDS` 里的每个 kind 都必须真的被夹具走过。

        加了 `RECORDED_KINDS` 却忘了往夹具里放样本 → 这条会红，
        于是「完备性」不会因为有人只改表不改样本而悄悄放宽。
        """
        kinds = {s.kind for s in _one_of_every_kind()}
        missing = set(RECORDED_KINDS) - kinds
        self.assertFalse(missing, f'夹具漏了这些 kind：{sorted(missing)}')

    def test_product_is_either_executable_or_an_explicit_placeholder(self):
        out = trace_to_steps(_TraceDriver(_one_of_every_kind()),
                             include_waits=True, include_asserts=True)
        for step in out:
            name = _action_of(step)
            if name == UNRESOLVED_ACTION:
                body = step[name]
                self.assertTrue(body.get('reason'),
                                f'占位必须说明为什么还原不了：{step}')
                continue
            self.assertIn(name, DSL_ACTIONS,
                          f'产物发明了 DSL 不认识的动作：{name}')

    def test_placeholder_records_the_original_kind(self):
        """占位要能追溯到「原来是哪一步」—— 否则人工补规格时无从下手。"""
        out = trace_to_steps(_TraceDriver(_one_of_every_kind()),
                             include_waits=True, include_asserts=True)
        origins = {s[UNRESOLVED_ACTION]['original'] for s
                   in out if UNRESOLVED_ACTION in s}
        self.assertIn('tap_xy', origins)
        self.assertIn('fling', origins)
        self.assertIn('somethingBrandNew', origins)

    def test_failed_traces_are_still_skipped(self):
        """只有**成功**的留痕才进产物 —— 失败步进脚本等于把错当对。"""
        out = trace_to_steps(_TraceDriver([
            _step('start', index=1),
            _step('tap', index=2, node_path='Button#x', ok=False),
        ]), include_waits=True, include_asserts=True)
        self.assertEqual(len(out), 1)


class TestNewlyTranslatedKinds(unittest.TestCase):
    """与已支持项**同构**的 kind 直译成可执行步骤，而不是退化成占位。"""

    def test_wait_gone_is_translated_not_placeholder(self):
        """`waitGone` 与 `waitFor` 同形（都是带超时的具名等待）。

        它此前既没被处理、也没被列进缺陷清单 —— 是最容易漏掉的一条：
        「页面还在转圈」正是回归脚本最该等的东西。
        """
        out = trace_to_steps(_TraceDriver([
            _step('waitGone', node_path='Button#btn_loading'),
        ]))
        self.assertEqual(out, [{'waitGone': {'id': 'btn_loading'}}])

    def test_long_press_is_translated_not_placeholder(self):
        out = trace_to_steps(_TraceDriver([
            _step('longPress', node_path='Column > Image#avatar'),
        ]))
        self.assertEqual(out, [{'long_press': {'id': 'avatar'}}])

    def test_wait_gone_falls_back_to_the_matcher_text(self):
        """留痕里没有可解析路径时，用匹配器描述兜底 —— 与 `waitFor` 同款。"""
        out = trace_to_steps(_TraceDriver([
            _step('waitGone', target="id=btn_loading"),
        ]))
        self.assertEqual(out, [{'waitGone': 'id=btn_loading'}])


class TestCoordinateTracesStayOut(unittest.TestCase):
    """`tap_xy` 只给占位：坐标是探索期的偶然，不是回归期的契约。"""

    def test_tap_xy_becomes_a_placeholder(self):
        out = trace_to_steps(_TraceDriver([
            _step('tap_xy', value=(540, 770), coords=(540, 770)),
        ]))
        self.assertEqual(len(out), 1)
        self.assertIn(UNRESOLVED_ACTION, out[0])

    def test_no_coordinate_leaks_into_the_product(self):
        out = trace_to_steps(_TraceDriver(_one_of_every_kind()),
                             include_waits=True, include_asserts=True)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn('540', blob)
        self.assertNotIn('770', blob)
        self.assertNotIn('"x"', blob)
        self.assertNotIn('"y"', blob)


class TestExplicitOptOutIsNotSilentLoss(unittest.TestCase):
    """`include_*` 关掉某一类是**调用方的选择**，与「没处理」不是一回事。"""

    def test_include_waits_false_drops_only_the_waits(self):
        with_waits = trace_to_steps(
            _TraceDriver(_one_of_every_kind()),
            include_waits=True, include_asserts=True)
        without = trace_to_steps(
            _TraceDriver(_one_of_every_kind()),
            include_waits=False, include_asserts=True)
        lost = len(with_waits) - len(without)
        self.assertEqual(lost, 2, '关掉等待应当只少 waitFor / waitGone 两条')
        self.assertFalse([s for s in without
                          if next(iter(s)) in ('waitFor', 'waitGone')])

    def test_include_asserts_false_drops_only_the_asserts(self):
        with_asserts = trace_to_steps(
            _TraceDriver(_one_of_every_kind()),
            include_waits=True, include_asserts=True)
        without = trace_to_steps(
            _TraceDriver(_one_of_every_kind()),
            include_waits=True, include_asserts=False)
        self.assertEqual(len(with_asserts) - len(without), 4,
                         '关掉断言应当少 4 条（三种可还原 + 一种未知断言）')

    def test_other_kinds_survive_both_switches(self):
        """两个开关都关掉时，**非等待非断言**的留痕仍一条不少。"""
        out = trace_to_steps(_TraceDriver(_one_of_every_kind()),
                             include_waits=False, include_asserts=False)
        names = [next(iter(s)) for s in out]
        for kept in ('start', 'tap', 'long_press', 'input', 'swipe',
                     'back', 'home', UNRESOLVED_ACTION):
            self.assertIn(kept, names, f'{kept} 不该被这两个开关波及')


class TestProductsDoNotInventActions(unittest.TestCase):
    """产物必须是 DSL 认得的动作 —— 否则「有产物」只是把静默丢变成静默挂。"""

    def test_executable_steps_do_not_hit_unsupported_action(self):
        class _D:
            bundle = BUNDLE
            ability = 'EntryAbility'

            def log(self, *a, **kw):
                pass

        out = trace_to_steps(_TraceDriver(_one_of_every_kind()),
                             include_waits=True, include_asserts=True)
        executable = [s for s in out if UNRESOLVED_ACTION not in s]
        rep = run_steps(_D(), executable)
        for err in rep.errors:
            self.assertNotIn('不支持的动作', err['error'],
                             f'产物里出现了 DSL 不认识的动作：{err}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
