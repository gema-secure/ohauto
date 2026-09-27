"""一键端到端演示 —— 一条命令跑完完整链路，并产出可提交的报告。

链路：拉起 → 语义定位 → 点击/输入 → 断言 → 自动探索 → 生成用例 → 报告 → 失败归因

为什么要有它
------------
答辩 / 演示时最怕两件事：

1. 要敲七八条命令、中间任何一条翻车就演砸；
2. 「跑通了」没有产物 —— 评委要的是看得见的报告，不是终端上的几行字。

所以这个脚本：一条命令、每步有横幅、产出落在 `_out/demo_full_chain/`
（JSON / Markdown / HTML 三种报告 + 状态图 + 生成的用例）。

**离线模式（默认）用 `FakeHdc`，任何机器都能跑、CI 可跑、现场绝不翻车**；
**真机模式（`--real`）用真设备**，跑出来的是真机证据。两种模式都会在
开头明说自己在哪种模式下 —— 不允许拿离线结果冒充真机。

用法::

    python tools/demo_full_chain.py                     # 离线（默认）
    python tools/demo_full_chain.py --real              # 真机（设备在位）
    python tools/demo_full_chain.py --real --bundle <包名> --ability <ability>
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

OUT = os.path.join(HERE, '_out', 'demo_full_chain')
sys.path.insert(0, HERE)
from preflight import require_device, default_target                       # noqa: E402

DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
DEFAULT_BUNDLE = 'ohos.samples.distributedmusicplayer'
DEFAULT_ABILITY = 'ohos.samples.distributedmusicplayer.MainAbility'

# 离线演示用的剧本 —— 只有两条：一条测试点清单、一条用例草案。
# 用 ScriptedProvider 是为了**完全可复现**（不联网、不烧钱、不因模型抽风翻车）。
DESC = '验证登录页：输入账号密码后点击登录，应能进入首页'
REPLY_POINTS = """{"test_points": [
  {"kind": "功能", "title": "正常登录", "precondition": "应用停在登录页",
   "expect": "点击登录后进入首页"}
]}"""
REPLY_CASE = """{"name": "登录并进入首页", "steps": [
  {"start": true},
  {"input": {"id": "username", "value": "alice"}},
  {"input": {"id": "password", "value": "pw"}},
  {"tap": {"text": "登录"}},
  {"assert": {"exists": {"text": "首页"}}}
]}"""


def banner(n: int, title: str) -> None:
    print('\n' + '=' * 74)
    print('  [步骤 %d] %s' % (n, title))
    print('=' * 74)


def pick_provider():
    """有 LLM key 就用真模型，没有就用剧本 —— **并把用的是哪个当场说出来**。

    为什么不无脑用真模型：演示现场最怕「模型刚好抽风 → 演砸」。
    但更怕**用剧本却不说** —— 那样就成了拿假数据充证据。所以这里返回
    (provider, 人类可读的说明)，说明会被打进日志和报告。
    """
    from ohauto.generator import ScriptedProvider, default_provider

    try:
        return default_provider(), '真实 LLM（环境变量已配置）'
    except Exception as e:                                  # ProviderNotConfigured
        return (ScriptedProvider([REPLY_POINTS, REPLY_CASE]),
                'ScriptedProvider（剧本，**未配置 LLM key**：%s）'
                % type(e).__name__)


def generate_case(driver, page, out_dir: str, use_page: bool = True):
    """阶段：自然语言 → 用例（六阶段，见 docs/用例生成说明.md）。

    `use_page=False` 时**不做控件存在性校验** —— 跨页面用例在生成阶段拿不到
    后续页面（本例的用例最后要断言「首页」，而生成时手上只有登录页），
    这时做控件校验必然误报 `CONTROL_MISSING`。这正是
    `validate_case(allow_missing_controls=True)` 要解决的问题：
    生成阶段只做**红线与 DSL 校验**，控件到底在不在交给执行时的干跑把关。
    """
    from ohauto.generator import Generator

    provider, note = pick_provider()
    print('  模型来源: %s' % note)
    gen = Generator(provider=provider, bundle=getattr(driver, 'bundle', '') or '',
                    page=page if use_page else None)
    out = gen.generate(DESC, page=page if use_page else None)
    if out.ok and out.case is not None:
        path = out.case.save(os.path.join(out_dir, 'generated_case.yaml'))
        print('  用例「%s」%d 步，已落盘: %s'
              % (out.case.name, out.case.step_count, path))
        if out.case.repaired:
            for n in out.case.repair_notes:
                print('    修复: %s' % n)
        return out.case, note
    print('  ⚠️ 未生成可执行用例：[%s] %s' % (out.reason, out.detail))
    print('    这本身也是一项能力：不可执行的用例会被校验拦下并**给出原因分类**，'
          '而不是拿去执行。原因分类见 docs/用例生成说明.md §八。')
    return None, note


def execute_generated(case, driver) -> None:
    """执行刚生成的用例 —— 「生成」和「执行」之间必须真的能跑通。

    光生成不执行，等于只演示了一半；而生成物直接喂给执行器、中间不发明
    新格式，是 `generator.Case.to_dict()` 的设计前提（见 docs/用例生成说明.md）。
    """
    from ohauto.runner import Runner

    if case is None:
        print('  （本轮没生成可执行用例，跳过执行）')
        return
    res = Runner(verbose=False).run_case(driver, case.to_dict())
    print('  用例「%s」: %d 步，通过 %d / 失败 %d，结论=%s'
          % (res.name, res.total, res.passed, res.failed,
             '通过' if res.ok else '未通过'))
    for s in res.steps:
        if not s.ok:
            print('    失败步骤 %d %s' % (s.index, s.target))


def demo_attribution(driver, out_dir: str):
    """阶段：失败步归因（挑战 #5 闭环）—— 故意跑一步必然失败的用例。

    为什么要故意失败：归因引擎的价值只有在**有失败**时才看得见。
    跑一条全绿的用例，闭环那一节永远是空的。

    接线说明（2026-09-25 修复）：本演示**复用 `tools/wire_locator_sink.py`**
    的接线（locator_sink 回写 + locator_id_resolver 只读反查，该模块五项
    自测全绿）。不接线的老版本会让归因结论打「没能回写定位器自愈，缺
    locator_id」—— 那不是闭环没接，是这条演示路径没复用接线，评审会误读。
    前提：当前页要有带 id 的控件（离线 FakeHdc 的 login 页满足；真机
    musicplayer 实测 id 全空，此时如实说明并跳过接线，不假装）。
    """
    from ohauto import LocatorManager
    from ohauto.runner import Runner
    from wire_locator_sink import make_locator_id_resolver, make_locator_sink

    bad_case = {'name': '归因演示（第 2 步引用不存在的控件）',
                'steps': [{'start': True},
                          {'tap': {'id': 'btn_definitely_missing'}},
                          {'assert': {'exists': {'text': '登录'}}}]}

    lm = None
    probe_id = next((n.id for n in driver.refresh().walk() if n.id), '')
    if probe_id:
        lm = LocatorManager()
        lm.register({'id': probe_id}, driver.refresh())
        # 靶子与 wire_locator_sink 自测同款：id 已注册（能反查到 locator_id），
        # 但 text 不匹配（必然定位失败）→ 归因结论可以带上 locator_id。
        bad_case['steps'][1] = {'tap': {'id': probe_id,
                                        'text': '根本不匹配的文案'}}

    runner = Runner(diagnose_failures=True, verbose=False,
                    **({'locator_sink': make_locator_sink(lm),
                        'locator_id_resolver': make_locator_id_resolver(lm)}
                       if lm is not None else {}))
    res = runner.run_case(driver, bad_case)
    for s in res.steps:
        if not s.ok:
            v_lid = getattr(s.verdict, 'locator_id', '') if s.verdict else ''
            print('  步骤 %d 失败，归因: %s' % (s.index, s.verdict_cn or '(无)'))
            if v_lid:
                print('    归因结论带 locator_id: %s（回写已生效，自愈有输入）'
                      % v_lid)
    if lm is None:
        print('  （当前页没有带 id 的控件，locator_id 反查接线未在此演示 ——')
        print('    接线本身见 tools/wire_locator_sink.py，五项自测全绿）')
    if runner.diagnose_errors:
        for e in runner.diagnose_errors:
            print('  ⚠️ %s' % e)
    return res


def run_offline(out_dir: str) -> int:
    from ohauto import action, report
    from ohauto.driver import Driver
    from ohauto.explorer import Budget, Explorer
    from ohauto.matcher import ON
    from ohauto.sim import FakeHdc

    print('模式: **离线**（FakeHdc 模拟设备，不连真机、不联网）')

    banner(1, '拉起应用')
    sim = FakeHdc(start_page='login', verbose=False)
    d = Driver(bundle='com.demo.app', ability='EntryAbility', hdc=sim,
               artifact_dir=os.path.join(out_dir, 'artifacts'), verbose=False)
    d.start()
    print('  已拉起，当前页 %r' % sim.current)

    banner(2, '语义定位（多属性匹配器）')
    page = d.refresh()
    for desc, m in [('按 id', ON.id('username')),
                    ('按文本', ON.text('登录')),
                    ('模糊文本', ON.text_contains('密码'))]:
        hits = m.filter([n for n in page.walk()])
        print('  %-10s -> %d 个命中%s'
              % (desc, len(hits), ('  首个=%r' % hits[0].label) if hits else ''))

    banner(3, '执行：输入 → 点击 → 断言')
    d.input(ON.id('username'), 'alice')
    d.input(ON.id('password'), 'secret123')
    d.tap(ON.text('登录'))
    d.assert_exists(ON.text('首页'))
    print('  断言「首页」出现 -> 通过')

    banner(4, '自动探索（页面状态图）')
    sim2 = FakeHdc(start_page='login', verbose=False)
    d2 = Driver(bundle='com.demo.app', hdc=sim2,
                artifact_dir=os.path.join(out_dir, 'artifacts_explore'),
                verbose=False)
    ex = Explorer(d2, artifact_dir=os.path.join(out_dir, 'artifacts_explore'),
                  verbose=False)
    graph = ex.explore(4, Budget(max_pages=4, max_actions_per_page=5),
                       return_back=False)
    gpath = ex.save_graph(os.path.join(out_dir, 'graph.json'))
    print('  探索到 %d 个页面状态、%d 条跳转；状态图: %s'
          % (len(getattr(graph, 'states', {}) or {}),
             len(getattr(graph, 'transitions', []) or []), gpath))

    banner(5, '自然语言 → 用例（六阶段）')
    # ⚠️ 必须用**全新的登录页**来生成：上面第 3 步已经把应用点到首页了，
    #    拿首页的控件树去生成「验证登录页」的用例，必然报 CONTROL_MISSING ——
    #    第一版就踩了这一个（看起来像生成失败，其实是演示脚本自己的错）。
    sim3 = FakeHdc(start_page='login', verbose=False)
    d3 = Driver(bundle='com.demo.app', ability='EntryAbility', hdc=sim3,
                artifact_dir=os.path.join(out_dir, 'artifacts_gen'), verbose=False)
    # 剧本用例是**跨页面**的（断言登录后的「首页」），所以这里跳过控件存在性校验
    case, note = generate_case(d3, d3.refresh(), out_dir, use_page=False)

    banner(6, '执行刚生成的用例（生成 → 执行的闭环）')
    execute_generated(case, d3)

    banner(7, '失败步归因（挑战 #5 闭环）')
    demo_attribution(
        Driver(bundle='com.demo.app', hdc=FakeHdc(start_page='login', verbose=False),
               artifact_dir=os.path.join(out_dir, 'artifacts_attr'), verbose=False),
        out_dir)

    banner(8, '生成报告（JSON / Markdown / HTML）')
    data = report.collect(d, run_report=action.run_case(
        Driver(bundle='com.demo.app', hdc=FakeHdc(start_page='login', verbose=False),
               artifact_dir=os.path.join(out_dir, 'artifacts_rep'), verbose=False),
        {'name': '登录并进入订单页', 'steps': [
            {'start': True}, {'input': {'id': 'username', 'value': 'bob'}},
            {'input': {'id': 'password', 'value': 'pw'}}, {'tap': {'text': '登录'}},
            {'assert': {'exists': {'text': '首页'}}}]}),
        extra={'note': '离线演示，非真机数据', 'generator_provider': note})
    paths = report.write_all(data, os.path.join(out_dir, 'report'), stem='demo')
    for k, v in paths.items():
        print('  %-9s %s' % (k, v))
    print('\n产物目录: %s' % out_dir)
    return 0


def run_real(out_dir: str, target: str, bundle: str, ability: str) -> int:
    from ohauto import report
    from ohauto.driver import Driver
    from ohauto.explorer import SafetyPolicy
    from ohauto.hdc import Hdc
    from ohauto.layout import flatten
    from ohauto.runner import DeviceGuard

    print('模式: **真机**（设备 %s）' % target)
    # 设备预检：不在场时如实报「设备连接：失败」退出，绝不带着空树往下跑
    hdc = require_device(target=target)

    banner(0, '唤醒 + 钉住息屏超时（息屏即锁屏，锁屏后控件树全空）')
    guard = DeviceGuard(hdc, verbose=False)
    print('  ensure_awake = %s' % guard.ensure_awake())

    banner(1, '拉起应用（force-stop → Home → start 三步）')
    hdc.shell(f'aa force-stop {bundle}')
    hdc.shell('uitest uiInput keyEvent Home')
    hdc.start_ability(bundle, ability)
    d = Driver(bundle=bundle, ability=ability, hdc=hdc,
               artifact_dir=os.path.join(out_dir, 'artifacts'), verbose=False)
    page = d.refresh()
    print('  控件树 %d 个节点' % sum(1 for _ in page.walk()))

    banner(2, '语义定位 + 安全策略（先识别危险控件）')
    pol = SafetyPolicy()
    nodes = [n for n in flatten(page, only_visible=True, only_interactive=True)]
    safe = []
    for n in nodes:
        bad, pat = pol.is_dangerous(n)
        if bad:
            print('  [拦截] %r 命中危险模式 /%s/' % (n.label, pat))
        else:
            safe.append(n)
    print('  可交互 %d 个，排除危险后剩余 %d 个' % (len(nodes), len(safe)))

    banner(3, '点击 + 断言（坐标从当前控件树动态取，不写死像素）')
    # 注意：这里**不做自动探索** —— 真机上探索会真的去点一串控件（哪怕有安全策略），
    # 演示现场不该冒这个险；探索那一步在离线模式里已经完整演示过。
    if safe:
        tgt = safe[0]
        # 不用 `label` 打印：真机上可点控件自身 id/text/descr 常全空，label 会兜到
        # 一个没有意义的字符串（musicplayer 上实测就打出了 'nan'）。
        desc = ' '.join(x for x in (tgt.type or '?',
                                    ('id=%s' % tgt.id) if tgt.id else '',
                                    ('text=%r' % tgt.text) if tgt.text else '')
                        if x)
        print('  点击目标: %s @ %s' % (desc, tgt.center))
        d.tap(tgt)
        after = d.refresh()
        print('  点击后控件树 %d 个节点' % sum(1 for _ in after.walk()))
    else:
        print('  ⚠️ 没有安全的可点击控件，跳过点击')

    banner(5, '自然语言 → 用例（六阶段）')
    case, note = generate_case(d, d.refresh(), out_dir)

    banner(6, '执行刚生成的用例（生成 → 执行的闭环）')
    execute_generated(case, d)

    banner(7, '失败步归因（挑战 #5 闭环）')
    demo_attribution(d, out_dir)

    banner(8, '生成报告（JSON / Markdown / HTML）')
    data = report.collect(d, extra={'note': '真机演示', 'device': target,
                                    'bundle': bundle})
    paths = report.write_all(data, os.path.join(out_dir, 'report'), stem='demo_real')
    for k, v in paths.items():
        print('  %-9s %s' % (k, v))
    print('\n产物目录: %s' % out_dir)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--real', action='store_true', help='用真机（默认离线）')
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--out', default=OUT)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    print('=' * 74)
    print('  一键端到端演示 · ohauto')
    print('=' * 74)
    try:
        if args.real:
            return run_real(args.out, args.target, args.bundle, args.ability)
        return run_offline(args.out)
    finally:
        print('\n' + '=' * 74)
        print('  演示结束')
        print('=' * 74)


if __name__ == '__main__':
    raise SystemExit(main())
