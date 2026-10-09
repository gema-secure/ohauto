"""
端到端离线演示 —— 不用真机，验证整条管线
========================================

在模拟设备上跑完：拉起应用 → 语义定位 → 点击/输入 → 断言 → 自动探索
→ 生成报告与可重放脚本。

用途：
    1. 验证代码逻辑正确（CI 可跑）
    2. 新人理解整条链路
    3. 后续改动的回归基准

运行:
    python examples/offline_demo.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto.driver import Driver
from ohauto.matcher import ON
from ohauto.sim import FakeHdc
from ohauto import action, report
from ohauto.explorer import Explorer, SafetyPolicy, Budget

OUT = os.path.join(HERE, '_out')


def line(t=''):
    print(t)


def main():
    line('=' * 70)
    line('  OpenHarmony UI 自动化能力层 · 端到端离线演示')
    line('=' * 70)

    # ---------------------------------------------------------- 1 建立驱动
    line('\n【1】建立 Driver（挂在模拟设备上）')
    sim = FakeHdc(start_page='login', verbose=False)
    d = Driver(bundle='com.demo.app', ability='EntryAbility',
               hdc=sim, artifact_dir=os.path.join(OUT, 'artifacts'),
               verbose=True)

    # ---------------------------------------------------------- 2 控件树
    line('\n【2】解析控件树')
    tree = d.refresh()
    line(f'  根节点: {tree.type}  尺寸 {tree.rect.width}x{tree.rect.height}')
    from ohauto.layout import flatten
    line('  可交互控件:')
    for n in flatten(tree, only_visible=True, only_interactive=True):
        line(f'    {n.type:<12} id={n.id:<20} label={n.label!r:<18} center={n.center}')

    # ---------------------------------------------------------- 3 语义定位
    line('\n【3】语义定位（多属性匹配器）')
    for desc, m in [
        ('按文本「登录」', ON.text('登录')),
        ('按 id', ON.id('username')),
        ('按类型+可点击', ON.type('Button').clickable(True)),
        ('文本含「密码」', ON.text_contains('密码')),
        ('按无障碍描述', ON.descr('微信')),
    ]:
        hits = m.filter(flatten(tree))
        line(f'  {desc:<18} -> {len(hits)} 个命中'
             + (f'  首个={hits[0].label!r}@{hits[0].center}' if hits else ''))

    # ---------------------------------------------------------- 4 识别危险控件
    line('\n【4】安全策略：识别危险控件（自动探索前必须做）')
    pol = SafetyPolicy()
    for n in flatten(tree, only_visible=True):
        if not n.is_interactive():
            continue
        bad, pat = pol.is_dangerous(n)
        if bad:
            line(f'  [拦截] {n.label!r}  命中危险模式 /{pat}/')
    line('  -> 自动探索会跳过上述控件，避免真机上产生不可逆影响')

    # ---------------------------------------------------------- 5 执行链路
    line('\n【5】执行操作链路：输入账号 → 点击登录 → 断言跳转')
    d.input(ON.id('username'), 'alice')
    d.input(ON.id('password'), 'secret123')
    d.tap(ON.text('登录'))
    line(f'  当前页面: {sim.current}')
    d.assert_exists(ON.text('首页'))
    line('  断言「首页」出现 -> 通过')

    # ---------------------------------------------------------- 6 DSL 执行
    line('\n【6】用 YAML 风格的 DSL 执行一个用例')
    sim2 = FakeHdc(start_page='login')
    d2 = Driver(bundle='com.demo.app', hdc=sim2,
                artifact_dir=os.path.join(OUT, 'artifacts_dsl'), verbose=False)
    case = {
        'name': '登录并进入订单页',
        'steps': [
            {'start': True},
            {'waitFor': {'text': '登录'}},
            {'input': {'id': 'username', 'value': 'bob'}},
            {'input': {'id': 'password', 'value': 'pw'}},
            {'tap': {'text': '登录'}},
            {'assert': {'exists': {'text': '首页'}}},
            {'tap': {'text': '订单'}},
            {'assert': {'exists': {'text': '我的订单'}}},
            {'swipe': {'direction': 'up', 'scale': 0.5}},
            {'back': True},
            {'assert': {'exists': {'text': '首页'}}},
        ],
    }
    rep = action.run_case(d2, case)
    line(f"  用例「{rep.name}」: 通过 {rep.passed}/{rep.total}，"
         f"结论={'通过' if rep.ok else '未通过'}，耗时 {rep.elapsed_ms}ms")
    for e in rep.errors:
        line(f"    失败步骤 {e['step']} [{e.get('action')}]: {e.get('error')}")

    # ---------------------------------------------------------- 7 自动探索
    line('\n【7】自动探索：让 Driver 自己点，摸出页面状态图')
    sim3 = FakeHdc(start_page='login')
    d3 = Driver(bundle='com.demo.app', hdc=sim3,
                artifact_dir=os.path.join(OUT, 'artifacts_explore'), verbose=False)
    ex = Explorer(d3, artifact_dir=os.path.join(OUT, 'artifacts_explore'),
                  verbose=True)
    graph = ex.explore(4, Budget(max_pages=4, max_actions_per_page=5), return_back=False)
    line('\n  生成的 Mermaid 状态图:')
    for l in ex.to_mermaid().splitlines():
        line('    ' + l)

    gpath = ex.save_graph(os.path.join(OUT, 'graph.json'))
    cpath = ex.generate_case(os.path.join(OUT, 'generated_case.yaml'),
                             name='自动探索生成的回归用例')
    line(f'  状态图: {gpath}')
    line(f'  生成用例: {cpath}')

    # ---------------------------------------------------------- 8 报告
    line('\n【8】生成报告（JSON / Markdown / HTML）')
    data = report.collect(d, run_report=rep,
                          extra={'note': '离线模拟演示，非真机数据'})
    paths = report.write_all(data, os.path.join(OUT, 'report'), stem='demo')
    for k, v in paths.items():
        line(f'  {k:<9} {v}')

    # ---------------------------------------------------------- 9 留痕转脚本
    line('\n【9】把执行留痕反推成可重放脚本')
    steps = action.trace_to_steps(d)
    line('  从真实执行轨迹还原出的步骤:')
    for i, s in enumerate(steps, 1):
        line(f'    {i}. {s}')

    # ---------------------------------------------------------- 汇总
    line('\n' + '=' * 70)
    line('  演示完成')
    line('=' * 70)
    s = d.summary()
    line(f"  真实 Driver: 共 {s['steps_total']} 步，失败 {s['steps_failed']}，"
         f"成功率 {s['success_rate'] * 100:.1f}%")
    line(f"  模拟设备记录到的操作: {len(sim.actions)} 次")
    line(f'  产物目录: {OUT}')
    line()
    line('  下一步（接上真机后）:')
    line('    python -m ohauto.doctor                       # 环境自检')
    line('    python examples/dump_tree.py --bundle <包名>   # 核对真实控件树')
    line('    python examples/run_case.py examples/cases/login.yaml --bundle <包名>')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
