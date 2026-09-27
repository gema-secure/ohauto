"""B3 自然语言转用例的单测 —— 两阶段生成、schema 兜底、红线扫描、自动修复、批量预取。

任务卡 B3 的验收标准原文：
    「20 条描述生成用例，可执行率 ≥ 80%；**不可执行的给出原因分类**」

所以本文件的重头是 `TestTwentyDescriptions` —— 20 条描述走完整链路，
统计可执行率并打印原因分类。其余分组把每个判据单独钉住。

全部基于 `ScriptedProvider` / `FakeHdc`：**不真调模型、不需要真机**。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import Future

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import Driver                                      # noqa: E402
from ohauto.generator import (Case, Generator, GenerationError,       # noqa: E402
                              GenerationReport, LLMProvider, NullProvider,
                              ProviderError, RejectReason, ScriptedProvider,
                              SchemaError, TestPoint, TestPointKind,
                              _prefetch, build_case_prompt, control_catalog,
                              dry_run, fallback_test_points, generate,
                              generate_many, normalize_kind, parse_json_payload,
                              parse_test_points, parse_test_points_lenient,
                              repair_locally, validate_case)
from ohauto.sim import FakeHdc                                        # noqa: E402


# ---------------------------------------------------------------- 构造工具

def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='true', **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _login_page():
    return {'attributes': {'type': 'Root', 'id': 'loginRoot',
                           'bounds': _b(0, 0, 1080, 2340), 'visible': 'true'},
            'children': [
                _node('Text', 'title', '欢迎登录', _b(240, 200, 840, 280), 'false'),
                _node('TextInput', 'username', '', _b(120, 400, 960, 500)),
                _node('TextInput', 'password', '', _b(120, 540, 960, 640)),
                _node('Button', 'btn_login', '登录', _b(120, 720, 960, 820)),
                _node('Button', 'btn_forget', '忘记密码', _b(120, 860, 960, 940)),
            ]}


def _j(obj):
    return json.dumps(obj, ensure_ascii=False)


def _plan_reply(titles=('正常登录',)):
    return _j({'test_points': [{'kind': '功能', 'title': t,
                                'precondition': '应用已启动', 'expect': '进入首页'}
                               for t in titles]})


def _good_case(name='登录流程', steps=None):
    return _j({'name': name, 'steps': steps if steps is not None else [
        {'start': True},
        {'waitFor': {'id': 'username'}},
        {'input': {'id': 'username', 'value': 'alice'}},
        {'tap': {'id': 'btn_login'}},
        {'assert': {'exists': {'text': '欢迎登录'}}},
    ]})


class _ImmediateExecutor:
    """同步立刻完成的假执行器 —— **不开线程**，行为完全确定。

    用它就能在单测里断言「预取确实发生了」（提交数领先于消费数），
    而不必依赖线程调度、也就不会 flaky。
    """

    def __init__(self):
        self.submitted = 0

    def submit(self, fn, *a, **kw):
        self.submitted += 1
        f: Future = Future()
        try:
            f.set_result(fn(*a, **kw))
        except BaseException as e:            # noqa: BLE001 - 原样传给调用方
            f.set_exception(e)
        return f

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def shutdown(self, *a, **kw):
        return None


class _StageAwareProvider(LLMProvider):
    """按**提示词内容**判断处在哪一阶段，而不是靠调用顺序 —— 更接近真实行为。"""

    name = 'stage-aware'

    def __init__(self, plan=None, case=None, repair=None):
        self.plan_reply = plan if plan is not None else _plan_reply()
        self.case_reply = case if case is not None else _good_case()
        self.repair_reply = repair
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if '拆成测试点清单' in prompt:
            return self.plan_reply
        if 'Action DSL 用例' in prompt:
            return self.case_reply
        if '最小改动' in prompt:
            if self.repair_reply is None:
                raise ProviderError('这个假 provider 不提供修复回复')
            return self.repair_reply
        raise ProviderError(f'假 provider 不认识这个提示词：{prompt[:60]}')


class _SimCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_gen_')
        self.sim = FakeHdc(start_page='login')
        self.driver = Driver(bundle='com.demo.app', hdc=self.sim,
                             artifact_dir=self.tmp, verbose=False,
                             sleep_fn=lambda *_: None)
        self.page = self.driver.refresh()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ================================================================ JSON 解析

class TestJsonPayload(unittest.TestCase):

    def test_plain_json(self):
        self.assertEqual(parse_json_payload('{"a": 1}'), {'a': 1})

    def test_fenced_json(self):
        self.assertEqual(parse_json_payload('```json\n{"a": 1}\n```'), {'a': 1})

    def test_bare_fence_and_surrounding_prose(self):
        text = '好的，这是结果：\n```\n{"a": 1}\n```\n需要我继续吗？'
        self.assertEqual(parse_json_payload(text), {'a': 1})

    def test_trailing_comma(self):
        self.assertEqual(parse_json_payload('{"a": [1, 2,],}'), {'a': [1, 2]})

    def test_line_comments(self):
        self.assertEqual(parse_json_payload('{\n// 注释\n"a": 1\n}'), {'a': 1})

    def test_single_quotes(self):
        self.assertEqual(parse_json_payload("{'a': 'x'}"), {'a': 'x'})

    def test_smart_quotes(self):
        self.assertEqual(parse_json_payload('{“a”: “x”}'), {'a': 'x'})

    def test_no_json_at_all_raises_schema_error(self):
        with self.assertRaises(SchemaError):
            parse_json_payload('模型今天不想干活')

    def test_empty_raises(self):
        for bad in (None, '', '   '):
            with self.assertRaises(SchemaError):
                parse_json_payload(bad)

    def test_python_literal_fallback(self):
        """模型偶尔吐出 Python 字面量（None/True），最后一级兜底要接住。"""
        self.assertEqual(parse_json_payload("{'a': None}"), {'a': None})


class TestFallbackTestPoints(unittest.TestCase):

    def test_extracts_six_kinds(self):
        text = """
- [功能] 正确账号密码可登录
- [边界] 用户名留空时提交应提示
1. [异常] 密码错误应提示且停留在登录页
* 【界面】按钮在窄屏下不遮挡输入框
(4) [性能] 登录响应在 2 秒内
- [兼容性] 折叠态下布局不溢出
"""
        pts = fallback_test_points(text)
        self.assertEqual(len(pts), 6)
        self.assertEqual([p['kind'] for p in pts],
                         ['功能', '边界', '异常', '界面', '性能', '兼容性'])

    def test_ignores_prose_lines(self):
        self.assertEqual(fallback_test_points('这是一段解释，不是测试点。'), [])


class TestTestPointParsing(unittest.TestCase):

    def test_strict_schema(self):
        pts = parse_test_points({'test_points': [
            {'kind': '功能', 'title': '登录', 'precondition': 'A', 'expect': 'B'}]})
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].kind, TestPointKind.FUNCTION)
        self.assertEqual(pts[0].kind_cn, '功能')

    def test_accepts_bare_list_and_single_dict(self):
        self.assertEqual(len(parse_test_points(
            [{'kind': '边界', 'title': 'x'}])), 1)
        self.assertEqual(len(parse_test_points(
            {'kind': '边界', 'title': 'x'})), 1)

    def test_string_items_are_kept(self):
        pts = parse_test_points({'test_points': ['随便写的一条']})
        self.assertEqual(pts[0].title, '随便写的一条')

    def test_items_without_title_are_dropped(self):
        with self.assertRaises(SchemaError):
            parse_test_points({'test_points': [{'kind': '功能'}]})

    def test_non_list_raises(self):
        with self.assertRaises(SchemaError):
            parse_test_points({'test_points': '不是数组'})

    def test_kind_alias_normalization(self):
        self.assertEqual(normalize_kind('Functional')[0], TestPointKind.FUNCTION)
        self.assertEqual(normalize_kind('edge')[0], TestPointKind.BOUNDARY)
        self.assertEqual(normalize_kind('negative')[0], TestPointKind.EXCEPTION)

    def test_unknown_kind_is_not_forced_into_a_category(self):
        """归不到六类就留空 —— 不要硬塞一个，那是伪造信息。"""
        kind, raw = normalize_kind('玄学')
        self.assertIsNone(kind)
        self.assertEqual(raw, '玄学')

    def test_lenient_never_raises(self):
        for bad in ('', '模型胡言乱语', '{bad json', '```json\n{}\n```'):
            pts = parse_test_points_lenient(bad)
            self.assertIsInstance(pts, list)

    def test_lenient_falls_back_to_regex(self):
        pts = parse_test_points_lenient('- [边界] 用户名留空应提示')
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].kind, TestPointKind.BOUNDARY)


# ================================================================ 静态校验（含红线）

class TestValidateCase(unittest.TestCase):

    def _case(self, steps):
        return Case(name='t', bundle='com.demo.app', steps=steps)

    def _page(self):
        return _login_page()

    def test_valid_case_has_no_issue(self):
        issues = validate_case(self._case([
            {'start': True}, {'tap': {'id': 'btn_login'}}]), self._page())
        self.assertEqual(issues, [])

    def test_missing_start_is_invalid(self):
        issues = validate_case(self._case([{'tap': {'id': 'btn_login'}}]),
                               self._page())
        self.assertEqual(issues[0].reason, RejectReason.DSL_INVALID)
        self.assertIn('start', issues[0].detail)

    def test_tap_xy_is_hardcoded_coord(self):
        """★ 红线第 5 条：坐标禁止硬编码，必须由代码守住而不是靠提示词。"""
        issues = validate_case(self._case([
            {'start': True}, {'tap_xy': {'x': 540, 'y': 770}}]), self._page())
        self.assertEqual(issues[0].reason, RejectReason.HARDCODED_COORD)

    def test_coord_fields_in_args_are_caught(self):
        issues = validate_case(self._case([
            {'start': True}, {'swipe': {'startX': 10, 'endX': 20}}]), self._page())
        self.assertEqual(issues[0].reason, RejectReason.HARDCODED_COORD)

    def test_sleep_is_hardcoded_wait(self):
        """★ 红线第 5 条：固定等待。action.py 本没有 sleep，模型会自己发明，得拦下。"""
        for act in ('sleep', 'wait', 'delay', 'pause'):
            issues = validate_case(self._case([{'start': True}, {act: 1000}]),
                                   self._page())
            self.assertEqual(issues[0].reason, RejectReason.HARDCODED_WAIT, act)

    def test_waitfor_with_timeout_is_allowed(self):
        """具名等待带 timeout 是超时保护，不是固定等待 —— 不能误杀。"""
        issues = validate_case(self._case([
            {'start': True},
            {'waitFor': {'id': 'btn_login', 'timeout': 8000}}]), self._page())
        self.assertEqual(issues, [])

    def test_unknown_action_is_invalid(self):
        issues = validate_case(self._case([{'start': True}, {'teleport': {}}]),
                               self._page())
        self.assertEqual(issues[0].reason, RejectReason.DSL_INVALID)

    def test_missing_control_is_reported(self):
        issues = validate_case(self._case([
            {'start': True}, {'tap': {'id': 'btn_不存在的'}}]), self._page())
        self.assertEqual(issues[0].reason, RejectReason.CONTROL_MISSING)
        self.assertEqual(issues[0].step_index, 2)

    def test_allow_missing_controls_skips_only_that_check(self):
        c = self._case([{'start': True}, {'tap': {'id': 'btn_不存在的'}}])
        self.assertEqual(validate_case(c, self._page(),
                                       allow_missing_controls=True), [])

    def test_no_page_means_control_check_is_skipped(self):
        c = self._case([{'start': True}, {'tap': {'id': 'btn_不存在的'}}])
        self.assertEqual(validate_case(c, None), [])

    def test_empty_steps(self):
        issues = validate_case(self._case([]), None)
        self.assertEqual(issues[0].reason, RejectReason.EMPTY)

    def test_assert_and_input_specs_are_checked(self):
        issues = validate_case(self._case([
            {'start': True},
            {'input': {'id': 'nope', 'value': 'x'}},
            {'assert': {'exists': {'text': '也不存在'}}},
        ]), self._page())
        self.assertEqual([i.reason for i in issues],
                         [RejectReason.CONTROL_MISSING, RejectReason.CONTROL_MISSING])


# ================================================================ 干跑

class TestDryRun(_SimCase):

    def test_only_readonly_steps_execute(self):
        """★ 干跑只跑无副作用步骤 —— 有副作用的一步都不许跑。"""
        case = Case(name='t', bundle='com.demo.app', steps=[
            {'start': True},
            {'input': {'id': 'username', 'value': 'alice'}},
            {'tap': {'id': 'btn_login'}},
            {'waitFor': {'id': 'btn_login'}},
            {'assert': {'exists': {'text': '欢迎登录'}}},
        ])
        res = dry_run(case, self.driver)
        self.assertTrue(res.ok, res.failures)
        self.assertEqual(res.executed, 2)          # waitFor + assert
        self.assertEqual(res.skipped, 3)           # start + input + tap
        # 应用状态没被改：还停在登录页
        self.assertEqual(self.sim.current, 'login')
        self.assertEqual([a for a in self.sim.actions if a['action'] == 'click'], [])

    def test_assert_failure_is_recorded(self):
        case = Case(name='t', bundle='com.demo.app', steps=[
            {'start': True},
            {'assert': {'exists': {'text': '这个文案不存在'}}},
        ])
        res = dry_run(case, self.driver)
        self.assertFalse(res.ok)
        self.assertEqual(res.failures[0]['kind'], 'assert')


# ================================================================ 自动修复

class TestRepair(unittest.TestCase):

    def _page(self):
        return _login_page()

    def test_coordinate_replaced_by_nearest_control(self):
        """坐标落在某个控件范围内 → 换成该控件的匹配器（而不是丢掉这一步）。"""
        case = Case(name='t', steps=[
            {'start': True}, {'tap_xy': {'x': 540, 'y': 770}}])   # btn_login 中心
        fixed, notes = repair_locally(case, validate_case(case, self._page()),
                                      self._page())
        self.assertTrue(notes)
        self.assertEqual(fixed.steps[1], {'tap': {'id': 'btn_login'}})
        self.assertEqual(validate_case(fixed, self._page()), [])

    def test_coordinate_outside_any_control_is_left_alone(self):
        case = Case(name='t', steps=[
            {'start': True}, {'tap_xy': {'x': 5, 'y': 2300}}])
        fixed, notes = repair_locally(case, validate_case(case, self._page()),
                                      self._page())
        self.assertEqual(notes, [])
        self.assertIn('tap_xy', fixed.steps[1])

    def test_sleep_replaced_by_waitidle(self):
        case = Case(name='t', steps=[{'start': True}, {'sleep': 2000}])
        fixed, notes = repair_locally(case, validate_case(case, self._page()),
                                      self._page())
        self.assertTrue(notes)
        self.assertEqual(fixed.steps[1], {'waitIdle': True})

    def test_missing_id_fixed_by_same_prefix(self):
        case = Case(name='t', steps=[
            {'start': True}, {'tap': {'id': 'btn_log'}}])
        fixed, notes = repair_locally(case, validate_case(case, self._page()),
                                      self._page())
        self.assertTrue(notes)
        self.assertEqual(fixed.steps[1], {'tap': {'id': 'btn_login'}})
        self.assertEqual(validate_case(fixed, self._page()), [])

    def test_input_value_survives_spec_swap(self):
        page = _login_page()
        case = Case(name='t', steps=[
            {'start': True}, {'input': {'id': 'user', 'value': 'alice'}}])
        fixed, _ = repair_locally(case, validate_case(case, page), page)
        self.assertEqual(fixed.steps[1],
                         {'input': {'id': 'username', 'value': 'alice'}})

    def test_repair_is_marked_on_the_case(self):
        case = Case(name='t', steps=[{'start': True}, {'sleep': 100}])
        fixed, _ = repair_locally(case, validate_case(case, self._page()),
                                  self._page())
        self.assertTrue(fixed.repaired)
        self.assertTrue(fixed.repair_notes)


# ================================================================ 两阶段主流程

class TestGenerateFlow(_SimCase):

    def test_two_stage_happy_path(self):
        g = Generator(provider=_StageAwareProvider(), bundle='com.demo.app',
                      page=self.page)
        out = g.generate('用户输入正确的账号密码后能登录成功')
        self.assertTrue(out.ok, out.detail)
        self.assertEqual(out.test_points[0].kind, TestPointKind.FUNCTION)
        self.assertTrue(out.case.steps)
        self.assertEqual(out.case.steps[0], {'start': True})
        self.assertFalse(out.case.repaired, '本来就没问题，不该被标记成修复过')

    def test_contract_entry_returns_case(self):
        case = generate('用户能登录', provider=_StageAwareProvider(),
                        bundle='com.demo.app', page=self.page)
        self.assertIsInstance(case, Case)
        self.assertTrue(case.steps)

    def test_no_test_point(self):
        g = Generator(provider=_StageAwareProvider(plan='模型今天不想干活'),
                      bundle='com.demo.app', page=self.page)
        out = g.generate('随便什么需求')
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, RejectReason.NO_TEST_POINT)

    def test_provider_failure_is_classified_not_raised(self):
        """★ LLM 失败必须降级：记原因、返回结果，绝不抛出去把批量搞崩。"""
        g = Generator(provider=NullProvider(), bundle='com.demo.app', page=self.page)
        out = g.generate('随便什么需求')
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, RejectReason.PROVIDER)
        self.assertIn('模拟模型不可用', out.detail)

    def test_schema_failure_is_classified(self):
        g = Generator(provider=_StageAwareProvider(case='完全不是 JSON 的一段话'),
                      bundle='com.demo.app', page=self.page)
        out = g.generate('用户能登录')
        self.assertEqual(out.reason, RejectReason.SCHEMA)

    def test_unfixable_control_missing_is_rejected_with_reason(self):
        bad = _good_case(steps=[{'start': True},
                                {'tap': {'id': 'btn_zzz_不存在'}}])
        g = Generator(provider=_StageAwareProvider(case=bad),
                      bundle='com.demo.app', page=self.page, max_repair=1)
        out = g.generate('用户能登录')
        self.assertFalse(out.ok)
        self.assertEqual(out.reason, RejectReason.CONTROL_MISSING)
        self.assertTrue(out.detail)

    def test_local_repair_makes_it_executable(self):
        """本地修好了 → 算可执行，并且标记 repaired。"""
        bad = _good_case(steps=[{'start': True},
                                {'sleep': 3000},
                                {'tap': {'id': 'btn_login'}}])
        g = Generator(provider=_StageAwareProvider(case=bad),
                      bundle='com.demo.app', page=self.page, max_repair=1)
        out = g.generate('用户能登录')
        self.assertTrue(out.ok, out.detail)
        self.assertTrue(out.case.repaired)

    def test_llm_repair_round_is_used_when_needed(self):
        bad = _good_case(steps=[{'start': True}, {'tap': {'id': 'btn_zzz_不存在'}}])
        g = Generator(provider=_StageAwareProvider(case=bad, repair=_good_case()),
                      bundle='com.demo.app', page=self.page, max_repair=1)
        out = g.generate('用户能登录')
        self.assertTrue(out.ok, out.detail)
        self.assertTrue(out.case.repaired)
        self.assertTrue(any('回 LLM 重写' in n for n in out.case.repair_notes))

    def test_generate_case_raises_on_failure(self):
        g = Generator(provider=NullProvider(), bundle='com.demo.app', page=self.page)
        with self.assertRaises(GenerationError) as cm:
            g.generate_case('用户能登录')
        self.assertEqual(cm.exception.outcome.reason, RejectReason.PROVIDER)

    def test_dry_run_failure_is_classified(self):
        """干跑失败要单独成一类原因。

        这里刻意用「控件存在、但断言文本对不上」：它能过静态校验
        （控件确实在树里），只在**真的执行**时才暴露 —— 这正是干跑该抓的东西。
        用「引用不存在的文案」是抓不到的，那在静态校验就成 CONTROL_MISSING 了。
        """
        bad = _good_case(steps=[
            {'start': True},
            {'assert': {'text': {'id': 'title', 'equals': '这里不是欢迎登录'}}},
        ])
        g = Generator(provider=_StageAwareProvider(case=bad),
                      bundle='com.demo.app', page=self.page, max_repair=0)
        out = g.generate('用户能登录', driver=self.driver)
        self.assertEqual(out.reason, RejectReason.DRYRUN_FAILED)
        self.assertIn('干跑失败', out.detail)

    def test_case_round_trips_to_executable_dsl(self):
        """生成的用例要能**真的被执行器吃下去** —— 生成与执行之间不另发明格式。"""
        from ohauto import action
        g = Generator(provider=_StageAwareProvider(), bundle='com.demo.app',
                      page=self.page)
        case = g.generate_case('用户能登录')
        loaded = action.load_case(case.to_dsl())
        self.assertEqual(loaded['name'], case.name)
        self.assertEqual(len(loaded['steps']), len(case.steps))
        path = case.save(os.path.join(self.tmp, 'generated_case.json'))
        self.assertTrue(os.path.exists(path))
        rep = action.run_case(self.driver, action.load_case(path))
        self.assertGreater(rep.passed, 0)

    def test_case_to_dict_is_runner_ready(self):
        g = Generator(provider=_StageAwareProvider(), bundle='com.demo.app',
                      page=self.page)
        d = g.generate_case('用户能登录').to_dict()
        self.assertEqual(sorted(d), ['ability', 'bundle', 'name', 'steps'])


# ================================================================ 批量与预取

class TestBatchAndPrefetch(unittest.TestCase):

    def _gen(self, replies, **kw):
        return Generator(provider=_StageAwareProvider(**replies),
                         bundle='com.demo.app', **kw)

    def test_prefetch_submits_next_before_consuming_current(self):
        """★ 预取的意义：第 i 条还没处理完，第 i+1 条就已经发出去了。"""
        ex = _ImmediateExecutor()

        def fetch(text):
            return text.upper()

        counts = []
        results = []
        for desc, reply in _prefetch(['a', 'b', 'c'], fetch, lambda: ex):
            counts.append(ex.submitted)
            results.append((desc, reply))

        self.assertEqual([r[0] for r in results], ['a', 'b', 'c'])
        self.assertEqual([r[1] for r in results], ['A', 'B', 'C'])
        # 拿到第 1 条时，第 2 条已经提交；拿到第 2 条时第 3 条已提交；
        # 第 3 条之后没有下一条，所以停在 3。这就是"预取"的可观测证据。
        self.assertEqual(counts, [2, 3, 3])

    def test_generate_many_stops_after_consecutive_failures(self):
        """★ 连续失败到阈值就停 —— 降级可以，但不能无声地刷 200 次失败。"""
        g = Generator(provider=NullProvider(), bundle='com.demo.app',
                      max_consecutive_failures=2)
        rep = g.generate_many(['a', 'b', 'c', 'd', 'e'], prefetch=False)
        self.assertEqual(rep.total, 2)
        self.assertEqual(rep.executable, 0)
        self.assertEqual(rep.reasons()[RejectReason.PROVIDER.value], 2)

    def test_generate_many_reports_rate_and_reason_categories(self):
        rep = self._gen({}).generate_many(['登录', '退出'], prefetch=False)
        self.assertEqual(rep.total, 2)
        self.assertEqual(rep.executable, 2)
        self.assertEqual(rep.executable_rate, 1.0)
        self.assertTrue(rep.ok())

    def test_prefetch_path_also_works(self):
        rep = self._gen({}).generate_many(['登录', '退出'], prefetch=True,
                                          executor_factory=lambda: _ImmediateExecutor())
        self.assertEqual(rep.executable, 2)

    def test_report_dict_is_report_ready(self):
        d = self._gen({}).generate_many(['登录'], prefetch=False).to_dict()
        for k in ('total', 'executable', 'executable_rate', 'reasons_cn', 'outcomes'):
            self.assertIn(k, d)


# ================================================================ ★ 验收：20 条描述

class TestTwentyDescriptions(_SimCase):
    """任务卡 B3 的验收本身：20 条描述 → 可执行率 ≥ 80% + 原因分类。"""

    # 17 条能生成可执行用例，3 条故意造得不可执行（覆盖三类不同原因）
    DESCRIPTIONS = [
        '用户输入正确的账号密码后能登录成功',
        '用户名留空时点击登录应给出提示',
        '密码错误时应提示并停留在登录页',
        '点击忘记密码进入重置流程',
        '连续快速点击登录按钮不应重复提交',
        '登录接口在 2 秒内返回',
        '折叠态下登录表单不溢出',
        '输入超长用户名不应崩溃',
        '登录成功后展示首页标题',
        '未登录直接访问首页应跳回登录',
        '输入框中粘贴文本应正常回显',
        '键盘弹出时登录按钮不被遮挡',
        '登录失败后可以立即重试',
        '退出登录后回到登录页',                 # ← 含本地可修的固定等待
        '用户名支持中文输入',                   # ← 含本地可修的固定等待
        '登录页元素在深色模式下可辨认',          # ← 含坐标，且坐标在控件内可本地修复
        '快速切换前后台后登录状态不丢',
        # ---- 以下 3 条注定不可执行，各对应一种原因分类
        '坐标点在不存在的区域内',                # HARDCODED_COORD（修不掉）
        '引用一个根本不存在的控件',              # CONTROL_MISSING（修不掉）
        '模型这次返回的不是 JSON',               # SCHEMA
    ]

    # 哪条描述走哪条剧本 —— 用**精确的整句**当键，不做子串猜测
    #（第一版用 `'固定等待' in desc` 猜，结果没有任何一条描述命中，静默失灵）。
    SCRIPT = {
        '退出登录后回到登录页': 'sleep_fixable',
        '用户名支持中文输入': 'sleep_fixable',
        '登录页元素在深色模式下可辨认': 'coord_fixable',
        '坐标点在不存在的区域内': 'coord_unfixable',
        '引用一个根本不存在的控件': 'missing_unfixable',
        '模型这次返回的不是 JSON': 'schema_broken',
    }

    def _provider_for(self, desc: str):
        """按描述的剧本造一个会「按计划犯特定错误」的 provider。"""
        mode = self.SCRIPT.get(desc)
        if mode == 'coord_unfixable':
            return _StageAwareProvider(case=_good_case(steps=[
                {'start': True}, {'tap_xy': {'x': 3, 'y': 2333}}]))   # 落点在空白区
        if mode == 'missing_unfixable':
            return _StageAwareProvider(
                case=_good_case(steps=[{'start': True},
                                       {'tap': {'id': 'btn_zzz_不存在'}}]),
                repair='这不是 JSON')           # 连 LLM 修复也修不好
        if mode == 'schema_broken':
            return _StageAwareProvider(case='模型今天不想干活，随便说点什么')
        if mode == 'sleep_fixable':
            return _StageAwareProvider(case=_good_case(steps=[
                {'start': True}, {'sleep': 1500}, {'tap': {'id': 'btn_login'}}]))
        if mode == 'coord_fixable':
            return _StageAwareProvider(case=_good_case(steps=[
                {'start': True}, {'tap_xy': {'x': 540, 'y': 770}},   # btn_login 中心
                {'tap': {'id': 'btn_login'}}]))
        return _StageAwareProvider()

    def test_executable_rate_at_least_80pct(self):
        outcomes = []
        lines = []
        for desc in self.DESCRIPTIONS:
            g = Generator(provider=self._provider_for(desc),
                          bundle='com.demo.app', page=self.page, max_repair=1)
            out = g.generate(desc, driver=self.driver)
            outcomes.append(out)
            mark = '✓' if out.ok else '✗'
            tail = (f'steps={out.case.step_count}'
                    f'{"（已修复）" if out.ok and out.case.repaired else ""}'
                    if out.ok else f'{out.reason.cn}：{out.detail[:50]}')
            lines.append(f'  {mark} {desc[:22]:<24} {tail}')

        rep = GenerationReport(outcomes=outcomes)
        report = (f'\n可执行率 {rep.executable}/{rep.total} = {rep.executable_rate:.0%}'
                  f'（要求 ≥80%）；本地/LLM 修复救回 {rep.repaired} 条\n'
                  + '\n'.join(lines)
                  + '\n  失败原因分类：' + (json.dumps(rep.reasons_cn(), ensure_ascii=False)
                                           or '（无）'))
        print(report)

        self.assertEqual(rep.total, 20)
        self.assertGreaterEqual(rep.executable_rate, 0.80, report)
        self.assertTrue(rep.ok(), report)
        self.assertGreater(rep.repaired, 0, '自动修复至少要真的救回过一条')

    def test_unexecutable_ones_all_carry_a_classified_reason(self):
        """★ 验收明写「不可执行的给出原因分类」—— 一个都不许只有一句"失败"。

        注意口径：`reason` + `detail` 是**每一类**都必须有的；
        逐步骤的 `issues` 只对「校验类」原因才存在（PROVIDER / SCHEMA / NO_TEST_POINT
        这些在生成阶段就断了，根本没有步骤可指）——先要求清楚，再断言。
        """
        step_level = {RejectReason.CONTROL_MISSING, RejectReason.HARDCODED_COORD,
                      RejectReason.HARDCODED_WAIT, RejectReason.DSL_INVALID,
                      RejectReason.DRYRUN_FAILED, RejectReason.EMPTY}
        reasons = set()
        for desc in self.DESCRIPTIONS:
            g = Generator(provider=self._provider_for(desc),
                          bundle='com.demo.app', page=self.page, max_repair=1)
            out = g.generate(desc, driver=self.driver)
            if out.ok:
                continue
            self.assertIsNotNone(out.reason, desc)
            self.assertTrue(out.reason.cn, desc)
            self.assertTrue(out.detail, desc)
            if out.reason in step_level:
                self.assertTrue(out.issues, f'{desc} 报了校验类原因却没有逐条问题')
                for i in out.issues:
                    self.assertTrue(i.detail)
            reasons.add(out.reason)
        self.assertTrue(reasons, '这组样例里应当有不可执行的')
        self.assertGreaterEqual(len(reasons), 3, f'原因分类应当有多种：{reasons}')

    def test_report_dict_lists_every_outcome(self):
        rep = generate_many(self.DESCRIPTIONS[:3],
                            provider=_StageAwareProvider(), bundle='com.demo.app',
                            page=self.page, prefetch=False)
        self.assertEqual(len(rep.to_dict()['outcomes']), 3)


# ================================================================ 提示词与清单

class TestPromptAndCatalog(unittest.TestCase):

    def test_rules_forbid_hardcoding_explicitly(self):
        p = build_case_prompt('需求', TestPoint(title='x'),
                              catalog='Button id=a', bundle='b', ability='A')
        self.assertIn('禁止硬编码坐标', p)
        self.assertIn('禁止固定等待', p)
        self.assertIn('只输出 JSON', p)

    def test_catalog_lists_controls(self):
        cat = control_catalog(_login_page())
        self.assertIn('btn_login', cat)
        self.assertIn('username', cat)

    def test_catalog_handles_none(self):
        self.assertIn('未提供', control_catalog(None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
