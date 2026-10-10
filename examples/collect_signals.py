"""
采集设备结果信号（信号采集）
======================

把「设备上发生了什么」采下来：崩溃日志 / 截图 / 控件树 / hilog，
客观识别异常（崩溃 / 白屏 / 无响应 / 无窗口）并标注置信度，
结果结构化落盘成 JSON，交给下游归因引擎。

**本命令不做归因判断** —— 它只输出证据，不下「这是应用缺陷」这种结论。

用法::

    # 真机：采集当前设备信号
    python examples/collect_signals.py --bundle com.ohos.note

    # 无真机也能看整条链路（模拟设备注入一次崩溃）
    python examples/collect_signals.py --bundle com.ohos.note --sim

    # 指定产物目录 / 采集窗口回看时长
    python examples/collect_signals.py --bundle com.example.app \\
        --out ./_out/signals --lookback 300

产物::

    <out>/signals.json          结构化结果（可直接喂给 B4）
    <out>/cppcrash-*.log        崩溃日志原文
    <out>/screen.png            失败时刻截图
    <out>/layout.json           采集到的控件树
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto.hdc import Hdc, HdcError                       # noqa: E402
from ohauto.signals import collect_signals                 # noqa: E402


def rule(title: str) -> None:
    print(f'\n{title}')
    print('-' * 68)


def show(sig) -> None:
    print('=' * 68)
    print(f'  被测应用 : {sig.bundle}')
    print(f'  采集时刻 : {sig.captured_at}（本机）')
    print(f'  设备时间 : {sig.device_time or "<未取到>"}')
    print(f'  采集窗口 : {sig.window_start} 起')
    print(f'  进程存活 : {sig.process_alive}')
    print('=' * 68)

    rule(f'崩溃记录（{len(sig.crashes)} 条）')
    if not sig.crashes:
        print('  窗口内没有属于本应用的崩溃')
    for c in sig.crashes:
        fg = {True: '前台', False: '后台', None: '未知'}[c.foreground]
        print(f'  [{c.kind}] {c.source_file}')
        print(f'    Module name : {c.module_name or "<未给出>"}')
        print(f'    时间        : {c.timestamp or "<未解析出>"}')
        print(f'    原因        : {c.reason or "<未解析出>"}')
        print(f'    信号        : {c.signal or "-"}   前台: {fg}')
        print(f'    pid/uid     : {c.pid} / {c.uid}')
        for fr in c.stack_head[:3]:
            print(f'    {fr[:96]}')
        print(f'    原文落盘    : {c.raw_path or "<未落盘>"}')

    rule(f'异常信号（{len(sig.anomalies)} 条，每条都带来源与置信度）')
    if not sig.anomalies:
        print('  未识别到异常')
    for a in sig.anomalies:
        print(f'  {a.kind:<13} [{a.source:<10}] 置信度 {a.confidence:.2f}')
        print(f'    {a.evidence}')

    for kind in ('CRASH', 'WHITE_SCREEN', 'NO_RESPONSE', 'NO_WINDOW'):
        score = sig.anomaly_score(kind)
        if score:
            print(f'  → {kind} 合成置信度 {score:.2f}'
                  f'（{len(sig.of_kind(kind))} 条独立证据）')

    rule('产物')
    for p in sig.screenshots:
        print(f'  截图     : {p}')
    if sig.layout_path:
        print(f'  控件树   : {sig.layout_path}（{sig.layout_nodes} 个节点）')
    for c in sig.crashes:
        if c.raw_path:
            print(f'  崩溃日志 : {c.raw_path}')

    rule(f'hilog 关键行（{len(sig.hilog_tail)} 行）')
    for ln in sig.hilog_tail[:10]:
        print(f'  {ln[:150]}')
    if len(sig.hilog_tail) > 10:
        print(f'  ... 另有 {len(sig.hilog_tail) - 10} 行')

    rule(f'降级说明（{len(sig.warnings)} 条）')
    if not sig.warnings:
        print('  无（全部采集步骤正常）')
    for w in sig.warnings:
        print(f'  ! {w}')


def make_sim(args):
    """无真机演示：模拟设备 + 注入一次崩溃，把整条链路走一遍。"""
    from ohauto.sim import FakeHdc
    sim = FakeHdc(start_page='login', screen=(720, 1280))
    sim.inject_crash(bundle=args.bundle)
    sim.hilog_text = '\n'.join([
        '09-16 15:14:47.777  9639  9639 E C03f00/MUSL-SIGCHAIN: '
        'signal_chain_handler call 2 rd sigchain action for signal: 11',
        f'09-16 15:14:47.777  9639  9639 I C02d11/DfxSignalHandler: '
        f'DFX_SigchainHandler :: sig(11), pid(9639).',
        '09-16 15:14:41.827  9639  9639 I C01317/AppKit: App main thread create',
    ])
    return sim


def main() -> int:
    ap = argparse.ArgumentParser(description='采集设备结果信号（信号采集）')
    ap.add_argument('--bundle', required=True, help='被测应用包名')
    ap.add_argument('--out', default=os.path.join(HERE, '_out', 'signals'),
                    help='产物目录（默认 examples/_out/signals）')
    ap.add_argument('--hdc', default=None, help='hdc 可执行文件路径')
    ap.add_argument('--lookback', type=float, default=600.0,
                    help='采集窗口回看秒数（默认 600）')
    ap.add_argument('--hilog-lines', type=int, default=500,
                    help='hilog 取缓冲区末尾多少行')
    ap.add_argument('--no-screenshot', action='store_true', help='不采集截图')
    ap.add_argument('--no-layout', action='store_true', help='不采集控件树')
    ap.add_argument('--probe-rounds', type=int, default=1,
                    help='控件树稳定性探测轮数（>1 会额外付出设备往返）')
    ap.add_argument('--sim', action='store_true',
                    help='用模拟设备演示（不需要真机，会注入一次崩溃）')
    args = ap.parse_args()

    if args.sim:
        dev = make_sim(args)
        print('[模式] 模拟设备（已注入一次崩溃）')
    else:
        try:
            dev = Hdc(hdc_path=args.hdc)
            print(f'[模式] 真机 {dev.hdc_path}')
        except HdcError as e:
            print(f'找不到 hdc：{e}', file=sys.stderr)
            print('\n提示：加 --sim 可以在没有真机的情况下看整条链路。',
                  file=sys.stderr)
            return 2

    sig = collect_signals(
        dev, args.bundle,
        out_dir=os.path.abspath(args.out),
        lookback_s=args.lookback,
        hilog_lines=args.hilog_lines,
        collect_screenshot=not args.no_screenshot,
        collect_layout=not args.no_layout,
        probe_rounds=args.probe_rounds,
    )

    show(sig)

    path = os.path.join(os.path.abspath(args.out), 'signals.json')
    sig.to_json(path)
    print(f'\n已写出: {path}')
    print('（这份 JSON 就是喂给归因引擎的输入）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
