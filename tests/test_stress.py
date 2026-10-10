"""压测用例生成的单测 —— 边缘安全算法、四类场景、以及与 driver 的一致性交叉验证。

本模块的验收标准原文：
    「能生成并在真机跑通至少 1 个压测场景」
    「**swipe 起止点离屏幕左右边缘各留 150–200px**，否则会误触系统返回手势」

B 不碰真机（分工约定），所以「真机跑通」这一条我做到的最强形式是：
**用 C 的契约接口 `run(cases, {'sim': True})` 真的把压测用例跑一遍**，
而不是只断言「生成的步骤长什么样」。见 `TestStressRunsThroughRunner`。

本文件里最重要的一组是 `TestSwipeMathMatchesDriver`：
我复刻了 `driver.swipe` 的坐标算法来做边缘校验，**复刻错了校验就是自欺欺人**，
所以拿 `FakeHdc` 真正收到的 swipe 参数去逐点核对。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import run                                              # noqa: E402
from ohauto.driver import Driver                                    # noqa: E402
from ohauto.generator import (Case, Generator, STRESS_KIND_CN,      # noqa: E402
                              SWIPE_EDGE_MARGIN_MAX, SWIPE_EDGE_MARGIN_MIN,
                              SWIPE_EDGE_MARGIN_PX, StressKind, StressSpec,
                              build_stress_case, check_swipe_safety, clamp_margin,
                              generate_stress, pick_stress_target,
                              split_stress_cases, stress_cases,
                              stress_safety_report, swipe_endpoints,
                              swipe_safe_scale, validate_case)
from ohauto.matcher import ON                                       # noqa: E402
from ohauto.sim import FakeHdc                                      # noqa: E402


# ---------------------------------------------------------------- 构造工具

def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='true', **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _page():
    """一张带「刷新」按钮（适合压测）和危险控件的页面。"""
    return {'attributes': {'type': 'Root', 'id': 'root',
                           'bounds': _b(0, 0, 1080, 2340), 'visible': 'true'},
            'children': [
                _node('Text', 'tv_title', '我的订单', _b(40, 120, 500, 200), 'false'),
                _node('Button', 'btn_refresh', '刷新列表', _b(120, 700, 960, 800)),
                _node('Button', 'btn_other', '其他', _b(120, 860, 960, 940)),
                _node('Button', 'btn_delete', '删除全部', _b(120, 1020, 960, 1100)),
                _node('List', 'feed', '', _b(0, 1200, 1080, 2100)),
            ]}


class _SimCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_stress_')
        self.sim = FakeHdc(start_page='login')
        self.driver = Driver(bundle='com.demo.app', hdc=self.sim,
                             artifact_dir=self.tmp, verbose=False,
                             sleep_fn=lambda *_: None)
        self.page = self.driver.refresh()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ================================================================ ★ 与 driver 的一致性

class TestSwipeMathMatchesDriver(_SimCase):
    """复刻的坐标算法必须与 `driver.swipe` 逐点一致，否则校验没有意义。"""

    def _sim_swipe(self, direction, scale, anchor=None):
        """真跑一次 driver.swipe，取 FakeHdc 实际收到的坐标。

        注意比对的是**前四位**：`hdc.swipe` 还会追加一个时长参数（默认 600ms），
        它不属于坐标算法。这个 5 元组本身就是「复刻是否准确」的证据 ——
        四个坐标逐点相等，只有时长那一项是 driver 之外给的默认值。
        """
        before = len(self.sim.actions)
        self.driver.swipe(direction, scale, anchor=anchor)
        recs = self.sim.actions[before:]
        sw = [a for a in recs if a.get('action') == 'swipe']
        self.assertTrue(sw, '模拟设备没有记录到 swipe 调用')
        args = [int(v) for v in sw[-1]['args']]
        return tuple(args[:4])

    def test_fullscreen_swipes_match(self):
        screen = self.driver.screen_size()
        for direction in ('up', 'down', 'left', 'right'):
            for scale in (0.3, 0.6, 0.9):
                with self.subTest(direction=direction, scale=scale):
                    self.assertEqual(
                        self._sim_swipe(direction, scale),
                        swipe_endpoints(direction, scale, screen))

    def test_anchored_swipe_matches(self):
        """锚定到控件时，复刻的算法也要对得上（用的是控件自身的 rect）。"""
        node = self.driver.require(ON.id('btn_login'))
        r = node.rect
        for direction in ('up', 'left'):
            with self.subTest(direction=direction):
                self.assertEqual(
                    self._sim_swipe(direction, 0.5, anchor=node),
                    swipe_endpoints(direction, 0.5, self.driver.screen_size(),
                                    anchor_rect=(r.left, r.top, r.right, r.bottom)))

    def test_vertical_swipe_keeps_x_at_center(self):
        """垂直滑动的 x 恒等于中线 —— 这就是它天然不碰左右边缘的原因。"""
        screen = self.driver.screen_size()
        for scale in (0.2, 0.6, 0.95):
            x1, _, x2, _ = swipe_endpoints('up', scale, screen)
            self.assertEqual(x1, screen[0] // 2)
            self.assertEqual(x2, screen[0] // 2)


# ================================================================ 边缘安全

class TestSwipeSafety(unittest.TestCase):

    def test_safe_scale_formula(self):
        self.assertEqual(swipe_safe_scale(1080, margin=150), 0.7222)
        self.assertEqual(swipe_safe_scale(1080, margin=180), 0.6667)
        self.assertEqual(swipe_safe_scale(1080, margin=200), 0.6296)
        self.assertEqual(swipe_safe_scale(720, margin=180), 0.5)
        self.assertEqual(swipe_safe_scale(720, margin=200), 0.4444)

    def test_vertical_has_no_scale_limit(self):
        self.assertEqual(swipe_safe_scale(1080, direction='up'), 1.0)
        self.assertEqual(swipe_safe_scale(1080, direction='down'), 1.0)

    def test_too_narrow_screen_has_no_safe_horizontal_range(self):
        self.assertEqual(swipe_safe_scale(300, margin=180), 0.0)

    def test_vertical_swipe_is_always_safe(self):
        """垂直滑动 x 在中线，scale 再大也碰不到左右边缘。"""
        for scale in (0.5, 0.9, 1.0):
            for d in ('up', 'down'):
                res = check_swipe_safety(d, scale, (1080, 2340))
                self.assertTrue(res.ok, (d, scale, res.reason))
                self.assertGreaterEqual(res.distance_px, SWIPE_EDGE_MARGIN_PX)

    def test_horizontal_small_scale_is_safe(self):
        res = check_swipe_safety('left', 0.6, (1080, 2340), margin=180)
        self.assertTrue(res.ok)
        self.assertEqual(res.distance_px, 216)

    def test_horizontal_large_scale_is_flagged(self):
        """★ scale 一大就会贴边 —— 这正是规模放大后必然贴边的根因。"""
        res = check_swipe_safety('left', 0.9, (1080, 2340), margin=180)
        self.assertFalse(res.ok)
        self.assertEqual(res.distance_px, 54)
        self.assertIn('系统返回手势', res.reason)
        self.assertIn('安全 scale 上限', res.reason)

    def test_boundary_scale_passes_exactly(self):
        safe = swipe_safe_scale(1080, margin=180)
        res = check_swipe_safety('left', safe, (1080, 2340), margin=180)
        self.assertTrue(res.ok, res.reason)
        self.assertEqual(res.distance_px, 180)

    def test_safety_dict_is_report_ready(self):
        d = check_swipe_safety('right', 0.9, (1080, 2340)).to_dict()
        for k in ('ok', 'direction', 'scale', 'distance_px', 'needed_px',
                  'endpoints', 'reason'):
            self.assertIn(k, d)

    def test_margin_is_clamped_into_the_documented_range(self):
        notes = []
        self.assertEqual(clamp_margin(50, notes), SWIPE_EDGE_MARGIN_MIN)
        self.assertEqual(clamp_margin(999, notes), SWIPE_EDGE_MARGIN_MAX)
        self.assertEqual(clamp_margin(SWIPE_EDGE_MARGIN_PX, notes), SWIPE_EDGE_MARGIN_PX)
        self.assertEqual(clamp_margin('abc', notes), SWIPE_EDGE_MARGIN_PX)
        self.assertEqual(len(notes), 2, '越界必须留痕，不能悄悄改掉')
        self.assertTrue(all('边缘留白' in n for n in notes))


# ================================================================ 目标控件挑选

class TestPickTarget(unittest.TestCase):

    def test_prefers_hint_keywords(self):
        spec = pick_stress_target(_page())
        self.assertEqual(spec, {'id': 'btn_refresh'})

    def test_never_picks_a_dangerous_control(self):
        """★ 压测把「删除」点 200 次是灾难 —— 危险控件必须被排除。"""
        page = {'attributes': {'type': 'Root', 'bounds': _b(0, 0, 1080, 2340),
                               'visible': 'true'},
                'children': [_node('Button', 'btn_delete', '删除全部',
                                   _b(120, 700, 960, 800))]}
        self.assertIsNone(pick_stress_target(page))

    def test_empty_page_returns_none(self):
        self.assertIsNone(pick_stress_target(None))


# ================================================================ 四类场景

class TestBuildStressCase(unittest.TestCase):

    SCREEN = (1080, 2340)

    def _spec(self, kind, **kw):
        kw.setdefault('target', {'id': 'btn_refresh'})
        return StressSpec(kind=kind, **kw)

    def test_repeat_tap_expands_rounds(self):
        case = build_stress_case(self._spec(StressKind.REPEAT_TAP, rounds=30),
                                 bundle='com.demo.app', screen=self.SCREEN,
                                 page=_page())
        self.assertEqual(case.steps[0], {'start': True})
        self.assertEqual(len(case.steps), 1 + 30 + 1)      # start + 30 tap + waitIdle
        self.assertEqual(sum(1 for s in case.steps if 'tap' in s), 30)

    def test_swipe_loop_alternates_directions(self):
        case = build_stress_case(
            self._spec(StressKind.SWIPE_LOOP, rounds=4, directions=('up', 'down')),
            screen=self.SCREEN)
        dirs = [s['swipe']['direction'] for s in case.steps if 'swipe' in s]
        self.assertEqual(dirs, ['up', 'down', 'up', 'down'])

    def test_nav_loop_taps_then_backs(self):
        case = build_stress_case(self._spec(StressKind.NAV_LOOP, rounds=5),
                                 screen=self.SCREEN)
        self.assertEqual(len(case.steps), 1 + 2 * 5)
        self.assertEqual(case.steps[1], {'tap': {'id': 'btn_refresh'}})
        self.assertEqual(case.steps[2], {'back': True})

    def test_long_run_mixes_three_actions_per_round(self):
        case = build_stress_case(self._spec(StressKind.LONG_RUN, rounds=3),
                                 screen=self.SCREEN)
        self.assertEqual(len(case.steps), 1 + 3 * 3)

    def test_all_four_kinds_pass_static_validation(self):
        """压测用例走**同一套**校验 —— 它也是用例，不另开旁路。"""
        for kind in StressKind:
            with self.subTest(kind=kind.value):
                case = build_stress_case(self._spec(kind, rounds=6),
                                         bundle='com.demo.app',
                                         screen=self.SCREEN, page=_page())
                self.assertEqual(validate_case(case, _page()), [],
                                 f'{STRESS_KIND_CN[kind]} 的用例没过静态校验')

    def test_horizontal_scale_is_pressed_down_with_note(self):
        """★ 生成的用例里，水平滑动必须已经被压到安全 scale。"""
        case = build_stress_case(
            self._spec(StressKind.SWIPE_LOOP, rounds=6, directions=('left', 'right'),
                       scale=0.95),
            screen=self.SCREEN, margin=180)
        for s in case.steps:
            if 'swipe' not in s:
                continue
            res = check_swipe_safety(s['swipe']['direction'], s['swipe']['scale'],
                                     self.SCREEN, margin=180)
            self.assertTrue(res.ok, f'生成的滑动不安全：{s} {res.reason}')
        self.assertTrue(any('压到' in n for n in case.notes), case.notes)

    def test_vertical_swipe_scale_is_not_touched(self):
        case = build_stress_case(
            self._spec(StressKind.SWIPE_LOOP, rounds=2, directions=('up', 'down'),
                       scale=0.95),
            screen=self.SCREEN)
        self.assertEqual(case.steps[1]['swipe']['scale'], 0.95)
        self.assertEqual(case.steps[2]['swipe']['scale'], 0.95)

    def test_rounds_are_capped_by_step_budget(self):
        """不许悄悄生成一个上万步的用例 —— 超预算要截断并留痕。"""
        case = build_stress_case(self._spec(StressKind.REPEAT_TAP, rounds=5000),
                                 screen=self.SCREEN, page=_page(), max_steps=100)
        self.assertLessEqual(len(case.steps), 100)
        self.assertTrue(any('按步数预算截断' in n for n in case.notes), case.notes)

    def test_target_is_auto_picked_when_absent(self):
        case = build_stress_case(StressSpec(kind=StressKind.REPEAT_TAP, rounds=3),
                                 screen=self.SCREEN, page=_page())
        self.assertEqual(case.steps[1], {'tap': {'id': 'btn_refresh'}})
        self.assertTrue(any('自动选中' in n for n in case.notes), case.notes)

    def test_missing_target_raises_with_a_clear_message(self):
        with self.assertRaises(ValueError) as cm:
            build_stress_case(StressSpec(kind=StressKind.REPEAT_TAP, rounds=3),
                              screen=self.SCREEN, page=None)
        self.assertIn('目标控件', str(cm.exception))

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            build_stress_case(StressSpec(kind='乱写', rounds=1), screen=self.SCREEN)

    def test_case_is_named_and_carries_performance_testpoint(self):
        case = build_stress_case(self._spec(StressKind.REPEAT_TAP, rounds=7),
                                 screen=self.SCREEN, page=_page())
        self.assertIn('重复点击', case.name)
        self.assertIn('7', case.name)
        self.assertEqual(case.test_point.kind.value, '性能')

    def test_safety_report_covers_generated_horizontal_swipes(self):
        case = build_stress_case(
            self._spec(StressKind.SWIPE_LOOP, rounds=4, directions=('left', 'right'),
                       scale=0.9),
            screen=self.SCREEN, margin=180)
        rep = stress_safety_report(case, self.SCREEN, margin=180)
        self.assertEqual(len(rep), 4)
        self.assertTrue(all(r.ok for r in rep), [r.reason for r in rep])

    def test_safety_report_is_empty_for_vertical_only(self):
        case = build_stress_case(self._spec(StressKind.SWIPE_LOOP, rounds=4,
                                            directions=('up', 'down')),
                                 screen=self.SCREEN)
        self.assertEqual(stress_safety_report(case, self.SCREEN), [])


class TestSplitForSoak(unittest.TestCase):

    def test_split_into_identical_structured_cases(self):
        cases = stress_cases('长时间运行', rounds=40, chunks=4,
                             target={'id': 'btn_refresh'}, screen=(1080, 2340),
                             page=_page())
        self.assertEqual(len(cases), 4)
        for c in cases:
            self.assertIn('长稳第', ' '.join(c.notes))
            self.assertEqual(validate_case(c, _page()), [])
        self.assertEqual(len(cases[0].steps), 1 + 3 * 10)

    def test_split_keeps_at_least_one_round_per_chunk(self):
        cases = split_stress_cases(
            StressSpec(kind=StressKind.NAV_LOOP, rounds=2, target={'id': 'x'},
                       name='长稳'), chunks=8, screen=(1080, 2340))
        self.assertEqual(len(cases), 8)
        for c in cases:
            self.assertGreaterEqual(len(c.steps), 3)      # start + 1 轮(2 步)


# ================================================================ 入口与落盘

class TestEntrypoints(_SimCase):

    def test_generate_stress_by_chinese_kind(self):
        case = generate_stress('重复点击', rounds=5, bundle='com.demo.app',
                               target={'id': 'username'},
                               screen=self.driver.screen_size(), page=self.page)
        self.assertIsInstance(case, Case)
        self.assertEqual(validate_case(case, self.page), [])

    def test_generator_method_uses_driver_screen(self):
        from ohauto.generator import ScriptedProvider
        g = Generator(provider=ScriptedProvider([]), bundle='com.demo.app',
                      page=self.page)
        case = g.generate_stress('连续滑动', rounds=5, driver=self.driver,
                                 directions=('up', 'down'))
        self.assertEqual(validate_case(case, self.page), [])

    def test_case_round_trips_through_disk(self):
        from ohauto import action
        case = generate_stress('重复点击', rounds=3, bundle='com.demo.app',
                               target={'id': 'username'},
                               screen=self.driver.screen_size(), page=self.page)
        path = case.save(os.path.join(self.tmp, 'stress.json'))
        loaded = action.load_case(path)
        self.assertEqual(loaded['name'], case.name)
        self.assertEqual(len(loaded['steps']), len(case.steps))


# ================================================================ ★ 验收：真的跑一遍

class TestStressRunsThroughRunner(_SimCase):
    """★ B5 的验收：**能跑通至少 1 个压测场景**。

    B 不碰真机，所以这里用 C 的契约接口 `run(cases, device={'sim': True})`
    把压测用例真的执行一遍 —— 比断言「步骤长什么样」强得多。
    真机那一遍留给集成日（交付包 §4 C-6）。
    """

    def _run(self, cases, **kw):
        return run([c.to_dict() for c in cases],
                   {'sim': True, 'start_page': 'login', 'screen': (1080, 2340)},
                   verbose=False, **kw)

    def test_repeat_tap_scenario_pass_on_sim(self):
        case = generate_stress('重复点击', rounds=5, bundle='com.demo.app',
                               target={'id': 'username'}, page=self.page,
                               screen=(1080, 2340))
        rep = self._run([case], default_timeout=3000, poll_interval=50)
        self.assertTrue(rep.ok, f'压测场景没跑通：{rep.to_dict()}')
        self.assertGreaterEqual(rep.passed, 5)

    def test_swipe_scenario_pass_on_sim(self):
        case = generate_stress('连续滑动', rounds=4, bundle='com.demo.app',
                               directions=('up', 'down'), page=self.page,
                               screen=(1080, 2340))
        rep = self._run([case], default_timeout=3000, poll_interval=50)
        self.assertTrue(rep.ok, f'滑动压测没跑通：{rep.to_dict()}')

    def test_nav_loop_scenario_pass_on_sim(self):
        """反复进出：点进首页再退回登录页，跑 3 轮。"""
        case = generate_stress('页面反复进出', rounds=3, bundle='com.demo.app',
                               target={'id': 'btn_login'}, page=self.page,
                               screen=(1080, 2340))
        rep = self._run([case], default_timeout=3000, poll_interval=50)
        self.assertTrue(rep.ok, f'反复进出没跑通：{rep.to_dict()}')

    def test_soak_chunks_all_pass(self):
        cases = stress_cases('长时间运行', rounds=4, chunks=2,
                             target={'id': 'btn_login'},
                             screen=(1080, 2340), page=self.page)
        rep = self._run(cases, default_timeout=3000, poll_interval=50)
        self.assertTrue(rep.ok, f'分段的长时间运行没跑通：{rep.to_dict()}')

    def test_horizontal_swipe_scenario_keeps_clear_of_edges(self):
        """跑一遍水平滑动压测，顺便核对模拟设备收到的坐标确实没贴边。

        这里把**已有的 sim** 当设备传进去（而不是 `{'sim': True}`）——
        后者会让 `run()` 自己新建一台模拟设备，我就看不到它实际收到的坐标了。
        """
        case = generate_stress('连续滑动', rounds=4, bundle='com.demo.app',
                               directions=('left', 'right'), scale=0.95,
                               margin=180, page=self.page, screen=(1080, 2340))
        rep = run([case.to_dict()], self.sim, verbose=False,
                  default_timeout=3000, poll_interval=50)
        self.assertTrue(rep.ok, rep.to_dict())
        swipes = [a for a in self.sim.actions if a.get('action') == 'swipe']
        self.assertTrue(swipes, '这轮压测应当真的产生了滑动')
        for a in swipes:
            x1, x2 = int(a['args'][0]), int(a['args'][2])
            self.assertGreaterEqual(min(x1, x2), 180, f'起止点贴到左边缘了：{a}')
            self.assertLessEqual(max(x1, x2), 1080 - 180, f'贴到右边缘了：{a}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
