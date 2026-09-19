"""
批量执行用例 —— 本版交付入口
============================

在 `run_case.py`（跑单个用例，失败即中断）之上加一层：
批量执行 + 单步重试 + 设备自愈 + 级联识别 + 产物轮转，并给出 执行引擎 验收指标。

用法:
    # 跑一个用例
    python examples/run_suite.py examples/cases/login.yaml --bundle com.example.app

    # 批量跑多个用例
    python examples/run_suite.py examples/cases/*.yaml --bundle com.example.app

    # ★ 执行引擎 验收：把同一个用例循环到 50 步，量成功率
    python examples/run_suite.py examples/cases/note_stability.yaml --repeat 5 \\
        --bundle com.ohos.note --ability MainAbility

    # 对照实验：关掉重试，看引擎到底救回了多少步
    python examples/run_suite.py examples/cases/note_stability.yaml --repeat 5 --no-retry

    # 无真机自检（CI 用）
    python examples/run_suite.py examples/cases/login.yaml --sim --repeat 5

退出码：0 = 达标；1 = 未达标（可直接用于 CI 质量门禁）
"""
import argparse
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import action, report                        # noqa: E402
from ohauto.driver import Driver                         # noqa: E402
from ohauto.runner import (DeviceGuard, FailureKind,     # noqa: E402
                           RetryPolicy, Runner, SuiteResult)


def expand(paths):
    """展开通配符，支持 shell 没展开的情况（Windows 下常见）。"""
    out = []
    for p in paths:
        if any(c in p for c in '*?['):
            hits = sorted(glob.glob(p))
            if not hits:
                print(f'[警告] 通配符没匹配到任何文件: {p}')
            out.extend(hits)
        else:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cases', nargs='+', help='用例文件（YAML/JSON），支持通配符')
    ap.add_argument('--bundle', default='', help='被测应用包名（覆盖用例里的）')
    ap.add_argument('--ability', default='', help='入口 Ability（覆盖用例里的）')
    ap.add_argument('--repeat', type=int, default=1,
                    help='把每个用例重复执行 N 次，用于连续执行压测')
    ap.add_argument('--out', default=os.path.join(HERE, '_out', 'suite'),
                    help='产物与报告目录')
    ap.add_argument('--hdc', default=None, help='hdc 路径')
    ap.add_argument('--sim', action='store_true',
                    help='用模拟设备执行（不需要真机，CI 用）')
    ap.add_argument('--no-retry', action='store_true',
                    help='关闭重试（对照实验，量化重试救回了多少步）')
    ap.add_argument('--stop-on-fail', action='store_true',
                    help='单步失败即中断该用例（默认继续跑完，便于统计）')
    ap.add_argument('--kpi', type=float, default=0.95,
                    help='执行引擎 验收阈值：独立成功率（默认 0.95）')
    ap.add_argument('--timeout', type=int, default=8000,
                    help='waitFor 默认超时（毫秒）。刻意调小可制造「苛刻条件」，'
                         '用来量化重试机制的兜底能力')
    ap.add_argument('--poll', type=int, default=300,
                    help='waitFor 轮询间隔（毫秒）')
    ap.add_argument('--artifact-budget', type=int, default=60,
                    help='每个用例保留的产物文件上限，0 表示不限制')
    ap.add_argument('--no-hdc-restart', action='store_true',
                    help='禁止设备自愈时重启 hdc 服务（更保守）')
    ap.add_argument('--quiet', action='store_true', help='少打印')
    args = ap.parse_args()

    files = expand(args.cases)
    if not files:
        print('没有可执行的用例文件。')
        return 1

    # ---------------------------------------------------------- 装载用例
    loaded = []
    for f in files:
        try:
            loaded.append((f, action.load_case(f)))
        except Exception as e:
            print(f'[失败] 用例装载失败 {f}: {e}')
            return 1

    bundle = args.bundle or loaded[0][1].get('bundle') or 'com.unknown'
    ability = args.ability or loaded[0][1].get('ability') or 'EntryAbility'
    verbose = not args.quiet

    print('=' * 72)
    print('  ohauto 批量执行 —— 执行引擎健壮性')
    print('=' * 72)
    print(f'  用例              : {len(files)} 个' + (f' × {args.repeat} 次'
                                                    if args.repeat > 1 else ''))
    print(f'  应用              : {bundle}')
    print(f'  模式              : {"模拟设备" if args.sim else "真机"}')
    print(f'  重试              : {"关闭（对照）" if args.no_retry else "开启"}')
    print(f'  产物上限/用例     : {args.artifact_budget or "不限"}')
    print('=' * 72)
    print()

    # ---------------------------------------------------------- 设备
    cases = loaded
    from ohauto.sim import FakeHdc
    if args.sim:
        hdc = FakeHdc(start_page='login', verbose=False)
        cases = [(f, _adapt_for_sim(c)) for f, c in loaded]
        print('  （模拟模式：定位条件已映射到模拟设备的控件上）')
        print()
    else:
        hdc = None
        if args.hdc:
            from ohauto.hdc import Hdc
            hdc = Hdc(hdc_path=args.hdc)
        try:
            from ohauto.hdc import Hdc as _H
            probe = hdc or _H()
            targets = probe.list_targets()
            if not targets:
                print('未检测到设备。请先连接真机并开启 USB 调试。')
                print('想先验证链路？加 --sim 参数。')
                return 1
            print(f'在线设备: {targets}\n')
        except Exception as e:
            print(f'设备检测失败: {e}')
            return 1

    # ---------------------------------------------------------- 组装 Runner
    policy = RetryPolicy.no_retry() if args.no_retry else RetryPolicy()
    guard = DeviceGuard(hdc if hdc is not None else _lazy_hdc(),
                        allow_hdc_restart=not args.no_hdc_restart,
                        verbose=verbose)
    runner = Runner(policy=policy, guard=guard,
                    continue_on_fail=not args.stop_on_fail,
                    artifact_budget=args.artifact_budget,
                    verbose=verbose)

    # ---------------------------------------------------------- 执行
    items = []
    for rep in range(1, args.repeat + 1):
        for f, case in cases:
            c = dict(case)
            if args.repeat > 1:
                c['name'] = f'{case.get("name") or os.path.basename(f)} #{rep}'
            d = Driver(bundle=bundle, ability=ability, hdc=hdc,
                       artifact_dir=None if args.sim else args.out,
                       default_timeout=args.timeout, poll_interval=args.poll,
                       verbose=False)
            if not args.sim and not d.hdc.target:
                try:
                    d.hdc.target = d.hdc.list_targets()[0] or None
                except Exception:
                    pass
            items.append((d, c))

    suite = runner.run_suite(items, guard=guard)

    # ---------------------------------------------------------- 汇总
    _print_summary(suite, guard, args)

    # ---------------------------------------------------------- 报告
    data = report.collect(items[0][0] if items else None,
                          extra={'suite': suite.to_dict(),
                                 'kpi_target': args.kpi,
                                 'retry_enabled': not args.no_retry,
                                 'mode': 'sim' if args.sim else 'device'})
    paths = report.write_all(data, args.out, stem='suite_report')
    print('\n报告:')
    for k, v in paths.items():
        print(f'  {k:<9} {v}')

    return 0 if suite.kpi_ok(args.kpi) else 1


def _lazy_hdc():
    from ohauto.hdc import Hdc
    return Hdc()


def _adapt_for_sim(case):
    """把用例定位条件映射到模拟设备的控件上（与 run_case.py 同逻辑）。"""
    import copy
    c = copy.deepcopy(case)
    mapping = {
        'username': {'id': 'username'},
        'password': {'id': 'password'},
        '登录': {'text': '登录'},
        '首页': {'text': '首页'},
        '我的订单': {'text': '我的订单'},
        '订单': {'text': '订单'},
    }
    ordered = sorted(mapping.items(), key=lambda kv: -len(kv[0]))

    def fix(spec):
        if not isinstance(spec, dict):
            return spec
        for key in ('id', 'text', 'text_contains', 'label', 'descr'):
            v = spec.get(key)
            if v is None:
                continue
            sv = str(v)
            for k, repl in ordered:
                if sv == k or k in sv:
                    out = dict(spec)
                    out.pop(key, None)
                    out.update(repl)
                    return out
        return spec

    for st in c.get('steps', []):
        if not isinstance(st, dict):
            continue
        for act in ('tap', 'waitFor', 'waitGone', 'input'):
            if act in st and isinstance(st[act], dict):
                if act == 'input':
                    val = st[act].get('value')
                    spec = {k: v for k, v in st[act].items() if k != 'value'}
                    st[act] = {**fix(spec), 'value': val}
                else:
                    st[act] = fix(st[act])
        if 'assert' in st and isinstance(st['assert'], dict):
            for k, v in st['assert'].items():
                if isinstance(v, dict):
                    st['assert'][k] = fix(v)
    return c


def _print_summary(suite: SuiteResult, guard, args):
    W = 72
    print()
    print('=' * W)
    print('  执行引擎 验收指标')
    print('=' * W)
    print(f'  用例数            : {len(suite.cases)}')
    print(f'  总步数            : {suite.total}')
    print(f'  通过              : {suite.passed}')
    print(f'  失败              : {suite.failed}'
          f'（其中级联 {suite.cascade_failed}）')
    print(f'  独立失败          : {suite.independent_failed}')
    print()
    print(f'  原始成功率        : {suite.success_rate:.2%}')
    print(f'  独立成功率        : {suite.independent_success_rate:.2%}'
          f'   ← 验收口径（阈值 {args.kpi:.0%}）')
    print()

    # 重试收益
    attempts = sum(c.retry_attempts for c in suite.cases)
    rescued = sum(c.rescued_by_retry for c in suite.cases)
    print(f'  重试尝试次数      : {attempts}')
    print(f'  重试挽救步数      : {rescued}'
          + (f'（挽救率 {rescued / attempts:.1%}）' if attempts else ''))
    print()

    # 失败归因
    by_kind = suite.failures_by_kind()
    if by_kind:
        print('  失败归因分布:')
        for k, v in by_kind.items():
            hint = _KIND_HINT.get(k, '')
            print(f'    {k:<9} {v:>4} 次   {hint}')
    else:
        print('  失败归因分布      : 无失败')
    print()

    # 设备健康
    if guard is not None:
        st = guard.stats()
        print(f'  设备心跳          : 成功 {st["pings"] - st["failed_pings"]}'
              f' / 失败 {st["failed_pings"]}')
        print(f'  设备恢复          : {st["recoveries"]} 次自动恢复'
              f'，{st["hdc_restarts"]} 次重启 hdc 服务')
    print()

    # 卡顿点
    pairs = [(c.name, s) for c in suite.cases for s in c.steps]
    slow = sorted(pairs, key=lambda p: -p[1].elapsed_ms)[:5]
    if slow:
        print('  最慢的 5 个步骤（卡顿点排查用）:')
        for cname, s in slow:
            tag = f'  [{s.attempt_count} 次尝试]' if s.retried else ''
            print(f'    {cname[:18]:<20} #{s.index:<3} {s.action:<12} '
                  f'{s.target[:26]:<28} {s.elapsed_ms:>7} ms{tag}')
        print()

    # 结论
    ok = suite.kpi_ok(args.kpi)
    print('=' * W)
    if ok:
        print(f'  结论：达标 —— 独立成功率 {suite.independent_success_rate:.2%} '
              f'≥ {args.kpi:.0%}')
    else:
        print(f'  结论：未达标 —— 独立成功率 {suite.independent_success_rate:.2%} '
              f'< {args.kpi:.0%}')
        failed = [s for c in suite.cases for s in c.steps if not s.ok]
        if failed:
            print()
            print('  失败明细（前 5 条）:')
            for s in failed[:5]:
                kind = s.kind.value if s.kind else '?'
                last = s.attempts[-1] if s.attempts else None
                err = (last.error if last else '')[:110]
                print(f'    #{s.index:<3} [{kind}] {s.action} {s.target[:26]}')
                print(f'         {err}')
    print('=' * W)


_KIND_HINT = {
    FailureKind.LOCATE.value:  '控件未找到（多为界面未稳定，已重试吸收）',
    FailureKind.TIMEOUT.value: '命令/等待超时（偶发，已重试吸收）',
    FailureKind.DEVICE.value:  '设备或 hdc 连接异常（已触发设备恢复）',
    FailureKind.APP.value:     '被测应用异常（已尝试重新拉起）',
    FailureKind.ASSERT.value:  '断言不成立 —— 真失败，需人工确认',
    FailureKind.DSL.value:     '用例本身写错 —— 需修用例',
    FailureKind.UNKNOWN.value: '未归类异常 —— 需扩充分类器',
}


if __name__ == '__main__':
    raise SystemExit(main())
