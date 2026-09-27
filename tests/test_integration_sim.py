"""端到端集成测试 —— 基于模拟设备，无需真机。

验证 Driver / DSL / 探索器 / 报告的**编排逻辑**是否正确。
渲染正确性必须上真机，这里覆盖的是除渲染之外的全部链路。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import Driver, DriverError        # noqa: E402
from ohauto.matcher import ON                        # noqa: E402
from ohauto.sim import FakeHdc, PAGES                # noqa: E402
from ohauto import action, report                    # noqa: E402
from ohauto.explorer import Explorer, SafetyPolicy, Budget   # noqa: E402


class SimTestCase(unittest.TestCase):
    """每个测试用一台全新的模拟设备。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_test_')
        self.sim = FakeHdc(start_page='login')
        self.d = Driver(bundle='com.demo.app', hdc=self.sim,
                        artifact_dir=self.tmp, verbose=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ================================================================ 基础链路

class TestDriverOnSim(SimTestCase):

    def test_refresh_reads_tree(self):
        tree = self.d.refresh()
        self.assertEqual(tree.type, 'Root')
        self.assertEqual(tree.rect.width, 1080)

    def test_require_finds_control(self):
        node = self.d.require(ON.id('username'))
        self.assertEqual(node.center, (540, 450))

    def test_require_raises_with_diagnostics(self):
        """定位失败必须给出线索，而不是只报「找不到」。"""
        with self.assertRaises(DriverError) as cm:
            self.d.require(ON.id('根本不存在'))
        msg = str(cm.exception)
        self.assertIn('未找到控件', msg)
        self.assertIn('可交互控件摘要', msg)     # 诊断信息必须带上

    def test_tap_triggers_transition(self):
        self.d.tap(ON.text('登录'))
        self.assertEqual(self.sim.current, 'home')

    def test_tap_coords_are_center_of_control(self):
        self.d.tap(ON.text('登录'))
        click = [a for a in self.sim.actions if a['action'] == 'click'][-1]
        self.assertEqual((click['x'], click['y']), (540, 770))
        self.assertEqual(click['node'], 'btn_login')

    def test_input_lands_on_correct_field(self):
        self.d.input(ON.id('username'), 'alice')
        typed = [a for a in self.sim.actions if a['action'] == 'inputText'][-1]
        self.assertEqual(typed['node'], 'username')
        self.assertEqual(typed['text'], 'alice')

    def test_input_two_fields_do_not_cross(self):
        self.d.input(ON.id('username'), 'alice')
        self.d.input(ON.id('password'), 'pw123')
        typed = [a for a in self.sim.actions if a['action'] == 'inputText']
        self.assertEqual([t['node'] for t in typed], ['username', 'password'])
        self.assertEqual([t['text'] for t in typed], ['alice', 'pw123'])

    def test_assert_exists_passes(self):
        self.d.tap(ON.text('登录'))
        self.d.assert_exists(ON.text('首页'))

    def test_assert_exists_raises_on_timeout(self):
        with self.assertRaises(DriverError):
            self.d.assert_exists(ON.text('不存在的页面'), timeout=600)

    def test_assert_text_mismatch_raises(self):
        with self.assertRaises(DriverError) as cm:
            self.d.assert_text(ON.id('btn_login'), '注册')
        self.assertIn('文本不匹配', str(cm.exception))

    def test_assert_text_passes(self):
        self.d.assert_text(ON.id('btn_login'), '登录')

    def test_back_returns_previous_page(self):
        self.d.tap(ON.text('登录'))          # -> home
        self.assertEqual(self.sim.current, 'home')
        self.d.back()
        self.assertEqual(self.sim.current, 'login')

    def test_start_resets_to_first_page(self):
        self.d.tap(ON.text('登录'))
        self.d.start()
        self.assertEqual(self.sim.current, 'login')

    def test_swipe_recorded(self):
        self.d.swipe('up', 0.5)
        self.assertTrue(any(a['action'] == 'swipe' for a in self.sim.actions))

    def test_screenshot_written_to_disk(self):
        p = self.d.screenshot()
        self.assertTrue(p and os.path.exists(p))
        self.assertGreater(os.path.getsize(p), 0)

    def test_named_screenshot_lands_in_artifact_dir(self):
        """配了 artifact_dir 时，相对文件名必须落到那里，不能污染当前目录。"""
        p = self.d.screenshot('named.png')
        self.assertEqual(os.path.dirname(os.path.abspath(p)),
                         os.path.abspath(self.tmp))
        self.assertTrue(os.path.exists(p))

    def test_named_screenshot_without_artifact_dir_goes_to_default_dir(self):
        """没配 artifact_dir 又传相对名 → 必须落到默认产物目录，**绝不能是当前目录**。

        实测踩到的静默污染：从项目根跑契约入口 `run(cases, device)`（不传
        out_dir），用例里的 `screenshot: "01_list.png"` 就把图丢在了项目根。
        项目根里累计清理出 7 个这类文件。
        """
        import contextlib
        import io
        from ohauto.driver import DEFAULT_ARTIFACT_DIR
        d = Driver(bundle='com.demo.app', hdc=FakeHdc(start_page='login'),
                   artifact_dir=None, verbose=True)
        before = set(os.listdir(os.getcwd()))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            p = d.screenshot('ohauto_leak_check.png')
        # 文件必须落在默认目录
        self.assertEqual(os.path.dirname(os.path.abspath(p)),
                         os.path.abspath(DEFAULT_ARTIFACT_DIR))
        # 当前目录不能多出任何东西
        self.assertEqual(set(os.listdir(os.getcwd())) - before, set(),
                         '截图污染了进程当前目录')
        # 且要有提示，让人知道东西去哪了
        self.assertIn('默认目录', buf.getvalue())
        if p and os.path.exists(p):
            os.remove(p)          # 别真留垃圾

    def test_sim_screen_cap_does_not_write_to_cwd(self):
        """模拟设备截图不得写进进程当前目录。

        早期 FakeHdc.screen_cap 写的是 os.getcwd()，每跑一次测试就在
        「你恰好所在的目录」留下 `_sim_login.png` 之类的垃圾 ——
        项目根里实测攒了 5 个，因为文件小反而很久没人发现。
        """
        import tempfile
        before = set(os.listdir(os.getcwd()))
        self.d.screenshot()
        leaked = {f for f in (set(os.listdir(os.getcwd())) - before)
                  if f.startswith('_sim_')}
        self.assertEqual(leaked, set(), f'模拟设备把文件写进了当前目录: {leaked}')
        # 顺带确认它确实写去了临时目录。
        # ⚠️ 别写死文件名：sim.py 的命名规律是
        #       ohauto_sim_<页面名>_<screen_style>.png
        # 早期没有 style 后缀，于是这里写死 'ohauto_sim_login.png' 时，
        # 断言会靠**上一次运行残留的旧文件**假通过 —— 干净环境（CI）下必红。
        # 实测：2026-09-21 手工删掉那个残留文件后，这条立刻 FAILED。
        # 所以只按前缀匹配，下次再加 style 也不会红。
        prefix = f'ohauto_sim_{self.sim.current}_'
        in_tmp = [f for f in os.listdir(tempfile.gettempdir())
                  if f.startswith(prefix) and f.endswith('.png')]
        self.assertTrue(in_tmp, f'模拟设备截图没落到临时目录（找 {prefix}*.png）')

    def test_steps_recorded_with_coords(self):
        self.d.tap(ON.text('登录'))
        tap_step = [s for s in self.d.steps if s.kind == 'tap'][0]
        self.assertEqual(tap_step.coords, (540, 770))
        self.assertIn('btn_login', tap_step.node_path or '')

    def test_summary_counts(self):
        self.d.tap(ON.text('登录'))
        s = self.d.summary()
        self.assertGreater(s['steps_total'], 0)
        self.assertEqual(s['steps_failed'], 0)
        self.assertEqual(s['success_rate'], 1.0)


# ================================================================ 等待机制

class TestWaitMechanisms(SimTestCase):

    def test_wait_for_returns_existing(self):
        node = self.d.wait_for(ON.text('登录'), timeout=2000)
        self.assertEqual(node.id, 'btn_login')

    def test_wait_for_times_out(self):
        with self.assertRaises(DriverError):
            self.d.wait_for(ON.text('不存在'), timeout=500)

    def test_wait_gone(self):
        self.d.wait_gone(ON.text('不存在'), timeout=400)

    def test_wait_idle_stable_page(self):
        """页面不动时，wait_idle 应较快返回 True。"""
        self.assertTrue(self.d.wait_idle(stable_rounds=2, interval=50,
                                         timeout=3000))


# ================================================================ DSL

class TestDslOnSim(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_dsl_')
        self.sim = FakeHdc(start_page='login')
        self.d = Driver(bundle='com.demo.app', hdc=self.sim,
                        artifact_dir=self.tmp, verbose=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_case_passes(self):
        case = {
            'name': '登录到订单',
            'steps': [
                {'start': True},
                {'waitFor': {'text': '登录'}},
                {'input': {'id': 'username', 'value': 'bob'}},
                {'tap': {'text': '登录'}},
                {'assert': {'exists': {'text': '首页'}}},
                {'tap': {'text': '订单'}},
                {'assert': {'exists': {'text': '我的订单'}}},
                {'back': True},
                {'assert': {'exists': {'text': '首页'}}},
            ],
        }
        rep = action.run_case(self.d, case)
        self.assertTrue(rep.ok, msg=str(rep.errors))
        self.assertEqual(rep.passed, rep.total)
        self.assertEqual(self.sim.current, 'home')

    def test_failing_assert_reported_not_raised(self):
        """run_case 应把失败记录下来，而不是抛异常中断整个流程。"""
        case = {'name': '注定失败', 'steps': [
            {'start': True},
            {'assert': {'exists': {'id': '根本没有'}, 'timeout': 300}},
        ]}
        rep = action.run_case(self.d, case)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.failed, 1)
        self.assertIn('根本没有', str(rep.errors))

    def test_stop_on_error_false_continues(self):
        case = {'name': '容错', 'steps': [
            {'assert': {'exists': {'id': '没有'}, 'timeout': 250}},
            {'tap': {'text': '登录'}},
        ]}
        rep = action.run_case(self.d, case, stop_on_error=False)
        self.assertEqual(rep.failed, 1)
        self.assertEqual(rep.passed, 1)
        self.assertEqual(self.sim.current, 'home')      # 后续步骤仍然执行了

    def test_gone_assert(self):
        case = {'name': '断言消失', 'steps': [
            {'assert': {'gone': {'text': '不存在的控件'}}},
        ]}
        self.assertTrue(action.run_case(self.d, case).ok)

    def test_assert_text_in_case(self):
        case = {'name': '断言文本', 'steps': [
            {'assert': {'text': {'id': 'btn_login', 'equals': '登录'}}},
        ]}
        self.assertTrue(action.run_case(self.d, case).ok)

    def test_serialize_and_reload_case(self):
        case = {'name': '往返', 'bundle': 'com.demo.app', 'steps': [
            {'start': True}, {'tap': {'id': 'btn_login'}}]}
        text = action.dump_case(case)
        back = action.load_case(text)
        self.assertEqual(back['name'], '往返')
        self.assertEqual(back['steps'][1]['tap']['id'], 'btn_login')

    def test_unknown_action_recorded_as_error(self):
        """run_steps 是「执行并出报告」，不是「抛异常」——
        未知动作应被记录为失败步骤，而不是让整个流程崩掉。"""
        rep = action.run_steps(self.d, [{'飞起来': {}}])
        self.assertFalse(rep.ok)
        self.assertIn('不支持的动作', str(rep.errors))

    def test_unknown_action_raises_at_executor_level(self):
        """底层执行器遇到未知动作应当抛错，便于尽早暴露 DSL 拼写问题。"""
        with self.assertRaises(action.DslError):
            action._exec_one(self.d, '飞起来', {})


# ================================================================ 安全策略

class TestSafetyPolicy(unittest.TestCase):

    def setUp(self):
        self.sim = FakeHdc(start_page='login')
        self.d = Driver(bundle='com.demo.app', hdc=self.sim, verbose=False)

    def test_dangerous_control_detected(self):
        from ohauto.layout import flatten
        pol = SafetyPolicy()
        tree = self.d.refresh()
        bad = [n.label for n in flatten(tree, only_visible=True)
               if pol.is_dangerous(n)[0]]
        self.assertIn('注销账号', bad)

    def test_allow_dangerous_disables_check(self):
        from ohauto.layout import flatten
        pol = SafetyPolicy(allow_dangerous=True)
        tree = self.d.refresh()
        bad = [n for n in flatten(tree, only_visible=True)
               if pol.is_dangerous(n)[0]]
        self.assertEqual(bad, [])

    def test_candidate_rejects_invisible(self):
        from ohauto.layout import flatten
        pol = SafetyPolicy()
        tree = self.d.refresh()
        hidden = [n for n in flatten(tree, only_visible=False)
                  if not n.visible]
        for n in hidden:
            ok, why = pol.is_clickable_candidate(n)
            self.assertFalse(ok)
            self.assertEqual(why, 'invisible')

    def test_language_agnostic_patterns(self):
        pol = SafetyPolicy()
        from ohauto.layout import LayoutNode
        for txt in ['删除', 'Delete', '支付', 'Logout', '注销']:
            n = LayoutNode(type='Button', text=txt)
            self.assertTrue(pol.is_dangerous(n)[0], txt)


# ================================================================ 探索

class TestExplorerOnSim(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_exp_')
        self.sim = FakeHdc(start_page='login')
        self.d = Driver(bundle='com.demo.app', hdc=self.sim,
                        artifact_dir=self.tmp, verbose=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovers_all_pages(self):
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        g = ex.explore(5, Budget(max_pages=5, max_actions_per_page=6), return_back=False)
        titles = {s.title for s in g.states.values()}
        self.assertIn('欢迎登录', titles)
        self.assertIn('首页', titles)
        self.assertIn('我的订单', titles)
        self.assertEqual(len(g.states), 3)

    def test_dangerous_control_never_clicked(self):
        """核心安全保证：探索过程绝不能点到危险控件。"""
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        ex.explore(5, Budget(max_pages=5, max_actions_per_page=10), return_back=False)
        clicked_ids = {a.get('node') for a in self.sim.actions
                       if a['action'] == 'click'}
        self.assertNotIn('btn_danger_delete', clicked_ids)
        self.assertNotIn('btn_logout', clicked_ids)

    def test_transitions_recorded(self):
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        g = ex.explore(5, Budget(max_pages=5, max_actions_per_page=6), return_back=False)
        pairs = {(t.src, t.dst) for t in g.transitions if t.src != t.dst}
        self.assertTrue(pairs)          # 至少发现一条跨页跳转

    def test_mermaid_is_valid_shape(self):
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        ex.explore(3, Budget(max_pages=3, max_actions_per_page=3), return_back=False)
        m = ex.to_mermaid()
        self.assertTrue(m.startswith('stateDiagram-v2'))
        self.assertIn('[*] -->', m)
        self.assertNotIn('Traceback', m)

    def test_save_graph_and_generate_case(self):
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        ex.explore(4, Budget(max_pages=4, max_actions_per_page=6), return_back=False)
        gpath = ex.save_graph(os.path.join(self.tmp, 'g.json'))
        cpath = ex.generate_case(os.path.join(self.tmp, 'c.yaml'))
        self.assertTrue(os.path.exists(gpath))
        self.assertTrue(os.path.exists(cpath))
        import json
        with open(gpath, encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn('mermaid', data)
        self.assertTrue(data['states'])

    def test_generated_case_is_replayable(self):
        """生成出来的用例应能被重新执行 —— 这是「用例沉淀」的闭环。"""
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        ex.explore(4, Budget(max_pages=4, max_actions_per_page=6), return_back=False)
        cpath = ex.generate_case(os.path.join(self.tmp, 'c.yaml'))

        sim2 = FakeHdc(start_page='login')
        d2 = Driver(bundle='com.demo.app', hdc=sim2, verbose=False)
        case = action.load_case(cpath)
        rep = action.run_case(d2, case)
        self.assertGreater(rep.passed, 0)


# ================================================================ 报告

class TestReportOnSim(SimTestCase):

    def test_full_pipeline_produces_three_reports(self):
        self.d.start()
        self.d.tap(ON.text('登录'))
        self.d.assert_exists(ON.text('首页'))

        data = report.collect(self.d, extra={'note': '测试'})
        out = os.path.join(self.tmp, 'rep')
        paths = report.write_all(data, out, stem='t')

        for k in ('json', 'markdown', 'html'):
            self.assertTrue(os.path.exists(paths[k]), k)

        import json
        with open(paths['json'], encoding='utf-8') as f:
            j = json.load(f)
        self.assertEqual(j['bundle'], 'com.demo.app')
        self.assertEqual(j['note'], '测试')
        self.assertEqual(j['summary']['steps_failed'], 0)

    def test_report_contains_failure_detail(self):
        """失败步骤的错误信息与诊断线索必须进报告。"""
        try:
            self.d.assert_exists(ON.id('没有任何控件'), timeout=300)
        except DriverError:
            pass
        data = report.collect(self.d)
        out = os.path.join(self.tmp, 'rep2')
        paths = report.write_all(data, out, stem='f')
        with open(paths['markdown'], encoding='utf-8') as f:
            md = f.read()
        self.assertIn('失败明细', md)
        self.assertIn('未找到', md)
        self.assertIn('可交互控件', md)      # 诊断线索也要留下

    def test_one_assertion_yields_one_step(self):
        """一次断言只应产生一条步骤记录，不能被 waitFor 重复计数。"""
        try:
            self.d.assert_exists(ON.id('没有任何控件'), timeout=250)
        except DriverError:
            pass
        assert_steps = [s for s in self.d.steps if s.kind.startswith('assert')]
        wait_steps = [s for s in self.d.steps if s.kind == 'waitFor']
        self.assertEqual(len(assert_steps), 1)
        self.assertEqual(len(wait_steps), 0)

    def test_explicit_waitfor_still_recorded(self):
        """显式调用 wait_for 时仍需留痕。"""
        self.d.wait_for(ON.text('登录'), timeout=1500)
        self.assertEqual(len([s for s in self.d.steps if s.kind == 'waitFor']), 1)

    def test_trace_to_steps_roundtrip(self):
        self.d.input(ON.id('username'), 'alice')
        self.d.tap(ON.text('登录'))
        steps = action.trace_to_steps(self.d)
        kinds = [next(iter(s)) for s in steps]
        self.assertIn('input', kinds)
        self.assertIn('tap', kinds)
        # 还原出的步骤应可再执行
        sim2 = FakeHdc(start_page='login')
        d2 = Driver(bundle='com.demo.app', hdc=sim2, verbose=False)
        rep = action.run_steps(d2, steps)
        self.assertGreater(rep.passed, 0)


# ================================================================ 跨形态
#
# 这批测试专门盯着**模拟器与真机不一致**这类问题 —— 它比"功能没写"更危险：
# 测试全绿，上真机才发现判据是错的。所以这里的断言都对着**真机实测结论**写，
# 而不是对着"代码怎么写的"写。

#: Mate X7 真机的两块屏（实测自 hidumper，见
#: tests/fixtures/crossform/renderservice_screen_harmonyos7_20260918_1916.txt）
MATE_X7_SCREENS = [
    {'index': 0, 'power_status': 'POWER_STATUS_ON', 'backlight': 1,
     'width': 2416, 'height': 2210},          # 内屏（展开态点亮）
    {'index': 1, 'power_status': 'POWER_STATUS_OFF', 'backlight': 4,
     'width': 1080, 'height': 2444},          # 外屏（折叠态点亮）
]


class TestSimScreenDump(unittest.TestCase):
    """★ `FakeHdc` 的 hidumper 输出必须与真机**同格式**。

    踩过的坑：早期只返回一句 `screen size: W x H`，与真机
    `hidumper -s RenderService -a screen` 的多屏结构完全不同。
    后果是 `parse_screen_info()` 在模拟环境里永远解析到空列表 ——
    跨形态 的折叠态判据（看 powerStatus 互换）**根本测不了**，
    只有上真机才暴露。这就是「模拟器不保真 = 测试全绿反而危险」的又一例。
    """

    def setUp(self):
        self.sim = FakeHdc(screens=MATE_X7_SCREENS)

    def test_renderservice_output_has_two_screens(self):
        raw = self.sim.shell(
            'hidumper -s RenderService -a screen').stdout
        self.assertIn('screen[0]:', raw)
        self.assertIn('screen[1]:', raw)
        self.assertIn('powerStatus=', raw)
        self.assertIn('render resolution=', raw)
        self.assertIn('activeMode:', raw)
        # 真机末尾有这两行
        self.assertIn('foldScreenIds_ size is 0', raw)

    def test_output_matches_real_fixture_shape(self):
        """逐字段对照真机原文的格式（字段名/顺序不能变）。"""
        raw = self.sim.shell('hidumper -s RenderService -a screen').stdout
        line = [l for l in raw.splitlines() if l.startswith('screen[0]:')][0]
        for token in ('id=', 'powerStatus=', 'backlight=', 'screenType=',
                      'render resolution=', 'physical resolution=',
                      'isVirtual=', 'skipFrameInterval='):
            with self.subTest(token=token):
                self.assertIn(token, line)

    def test_single_screen_default_still_works(self):
        """不传 `screens` 时退回单屏 —— 既有调用方行为不变。"""
        sim = FakeHdc(screen=(1080, 2340))
        raw = sim.shell('hidumper -s RenderService -a screen').stdout
        self.assertIn('1080x2340', raw)
        self.assertNotIn('screen[1]:', raw)

    def test_legacy_screen_size_form_kept(self):
        """非 RenderService 的老写法保留（某些裁剪版本只有 DisplayManager）。"""
        sim = FakeHdc(screen=(720, 1280))
        out = sim.shell('hidumper -s DisplayManagerService -a screen').stdout
        self.assertEqual(out, 'screen size: 720 x 1280')


class TestSimFoldedState(unittest.TestCase):
    """★ 折叠态模拟必须复现真机的真实行为。

    真机结论：`-foldedState` **不改分辨率**，只改两块屏的 `powerStatus`：

        open / half-open → screen[0] 内屏 ON（2416x2210）
        close            → screen[1] 外屏 ON（1080x2444）

    如果模拟器改成"切折叠态就换分辨率"，下游就会写出「比对分辨率」的
    错误判据 —— 而在真机上那个判据永远为假。
    """

    def setUp(self):
        self.sim = FakeHdc(screens=[dict(s) for s in MATE_X7_SCREENS])

    def test_open_lights_inner_screen(self):
        self.sim.set_folded_state('open')
        s = self.sim.screens
        self.assertEqual(s[0]['power_status'], 'POWER_STATUS_ON')
        self.assertEqual(s[1]['power_status'], 'POWER_STATUS_OFF')

    def test_half_open_lights_inner_screen(self):
        """★ `half-open` 在双屏折叠机上**点亮同一块屏**（物理上不可区分）。"""
        self.sim.set_folded_state('half-open')
        s = self.sim.screens
        self.assertEqual(s[0]['power_status'], 'POWER_STATUS_ON')
        self.assertEqual(s[1]['power_status'], 'POWER_STATUS_OFF')

    def test_close_lights_outer_screen(self):
        self.sim.set_folded_state('close')
        s = self.sim.screens
        self.assertEqual(s[0]['power_status'], 'POWER_STATUS_OFF')
        self.assertEqual(s[1]['power_status'], 'POWER_STATUS_ON')

    def test_resolution_never_changes(self):
        """★ 分辨率恒定 —— 这是真机行为，也是「别用分辨率做判据」的由来。"""
        sizes = []
        for st in ('open', 'half-open', 'close'):
            self.sim.set_folded_state(st)
            sizes.append([(s['width'], s['height']) for s in self.sim.screens])
        self.assertEqual(sizes[0], sizes[1])
        self.assertEqual(sizes[1], sizes[2])

    def test_screen_size_follows_active_screen(self):
        """截图尺寸跟随点亮屏 —— 切到外屏后应变成 1080x2444。"""
        self.sim.set_folded_state('open')
        self.assertEqual(self.sim.screen_size, (2416, 2210))
        self.sim.set_folded_state('close')
        self.assertEqual(self.sim.screen_size, (1080, 2444))

    def test_single_screen_device_ignores_folded_state(self):
        """单屏设备切折叠态不造假动作 —— 不做无意义的状态变更。"""
        sim = FakeHdc(screen=(1080, 2340))
        before = [dict(s) for s in sim.screens]
        sim.set_folded_state('close')
        self.assertEqual(sim.screens, before)


class TestSimToRealParserRoundtrip(unittest.TestCase):
    """★★ **模拟端输出 → 真实解析器** 必须喂得通。

    这是本轮最重要的护栏：模拟和解析如果各写一套正则，迟早不一致。
    这里直接拿**生产代码的解析器**（`emulator_cli.parse_screen_info`）
    去解析模拟器的输出，保证两边永远对齐。
    """

    def setUp(self):
        import sys as _s
        _s.path.insert(0, os.path.join(ROOT, 'tools'))
        import emulator_cli as ec
        self.ec = ec
        self.sim = FakeHdc(screens=[dict(s) for s in MATE_X7_SCREENS])

    def _parse(self):
        raw = self.sim.shell(
            'hidumper -s RenderService -a screen').stdout
        return self.ec.parse_screen_info(raw)

    def test_parses_both_screens(self):
        screens = self._parse()
        self.assertEqual(len(screens), 2)
        self.assertEqual(screens[0]['width'], 2416)
        self.assertEqual(screens[0]['height'], 2210)
        self.assertEqual(screens[1]['width'], 1080)
        self.assertEqual(screens[1]['height'], 2444)

    def test_active_screen_flips_on_close(self):
        """★ 全链路：切折叠态 → 模拟输出 → 真解析器 → active_screen 翻转。"""
        self.sim.set_folded_state('open')
        act = self.ec.active_screen(self._parse())
        self.assertEqual(act['index'], 0)
        self.assertEqual((act['width'], act['height']), (2416, 2210))

        self.sim.set_folded_state('close')
        act = self.ec.active_screen(self._parse())
        self.assertEqual(act['index'], 1)
        self.assertEqual((act['width'], act['height']), (1080, 2444))

    def test_open_and_half_open_indistinguishable(self):
        """★ 真机行为：这两个态点亮同一块屏 → 判据上无法区分。

        写下来是为了防止有人以为「-foldedState 支持 3 个值」就等于
        「能测出 3 种形态」。实际在双屏折叠机上是 2 种。
        """
        seen = set()
        for st in ('open', 'half-open', 'close'):
            self.sim.set_folded_state(st)
            act = self.ec.active_screen(self._parse())
            seen.add((act['width'], act['height']))
        self.assertEqual(len(seen), 2, '应是 2 种点亮屏（内屏/外屏）')

    def test_fixture_file_parses_too(self):
        """真机夹具本身也要能被解析器读通（夹具是基准，不能是死数据）。"""
        import re as _re
        p = os.path.join(HERE, 'fixtures', 'crossform',
                         'renderservice_screen_harmonyos7_20260918_1916.txt')
        if not os.path.exists(p):
            self.skipTest('夹具文件不存在')
        with open(p, encoding='utf-8') as f:
            text = f.read()
        blocks = _re.split(r'### === 样本 [AB][：:][^\n]*===', text)
        blocks = [b for b in blocks[1:] if 'screen[' in b]
        self.assertEqual(len(blocks), 2, '夹具应有 open / close 两段样本')
        results = []
        for b in blocks:
            act = self.ec.active_screen(self.ec.parse_screen_info(b))
            results.append((act['width'], act['height']))
        self.assertIn((2416, 2210), results)
        self.assertIn((1080, 2444), results)


if __name__ == '__main__':
    unittest.main(verbosity=2)
