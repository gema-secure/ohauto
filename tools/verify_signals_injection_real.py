"""真机受控异常注入：验证信号判据**在真机上真的会命中**。

为什么需要这个：判据（`_judge_crash` / `_judge_white_screen` / `_judge_no_response` /
`_collect_layout` / `_judge_layout_anomaly`）此前只有两类证据 ——
单测里的夹具、以及「正常页面上不误报」。**「真出现异常时能不能报」一直没有真机证据**，
因为异常在真机上不可控。本工装把可控的两类做成注入：

| 场景 | 注入方式 | 期望命中的判据 |
|---|---|---|
| `crash` | `kill -11 <pid>`（SIGSEGV）杀被测应用 | `CRASH` —— **1.0**（系统落 cppcrash 日志）+ 0.9（进程消失） |
| `no_window` | `power-shell suspend` 息屏 → 锁屏不进无障碍树 | `NO_WINDOW` |

> **为什么用 `-11` 而不是 `-9`**：实测（2026-09-23）`kill -9`（SIGKILL）**不会**让
> `faultlogger` 落日志 —— 它只杀了进程，判据只能给 0.35 的「进程消失」弱证据。
> `kill -11`（SIGSEGV）会被系统当作真实崩溃捕获，落一份
> `cppcrash-<bundle>-<uid>-<时间戳>`，判据走到 1.0 那条路。
> **要验的是「真崩溃能不能被认出来」，就别用那个连日志都不产生的信号。**

**真机造不出来的三类（白屏 / 无响应 / 布局异常）不在本工装内** ——
不要为了凑数去假造，如实写在输出里。

用法（设备在位）：
    python tools/verify_signals_injection_real.py                    # 跑全部
    python tools/verify_signals_injection_real.py --scene crash
    python tools/verify_signals_injection_real.py --scene no_window
    python tools/verify_signals_injection_real.py --signals 9        # 对照组：不产生日志的信号
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto.hdc import Hdc                              # noqa: E402
from ohauto.runner import DeviceGuard                   # noqa: E402
from ohauto.signals import FAULTLOGGER_DIR, collect_signals   # noqa: E402
from preflight import require_device, default_target                  # noqa: E402

DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
DEFAULT_BUNDLE = 'ohos.samples.distributedmusicplayer'
DEFAULT_ABILITY = 'ohos.samples.distributedmusicplayer.MainAbility'
OUT = os.path.join(HERE, '_out', 'signals_injection')

#: 采集窗口的回看秒数（基线用）。**必须短** —— `collect_signals` 默认回看 600s，
#: 会把上一次实验留下的崩溃日志也算进基线，让「注入前无异常」这个前提失效
#: （第一版工装就踩了这个：基线直接报出 1 条 CRASH）。
BASELINE_LOOKBACK_S = 5.0

#: 注入后等日志落盘的上限。**不要用固定 sleep** —— 实测 faultlogger 落盘要几秒，
#: 固定等 3s 会读到「还没写出来」的空目录（第一版就是这样，注入后反而 0 条）。
FAULT_WAIT_S = 20.0
FAULT_POLL_S = 1.5


def _sh(hdc, cmd, quiet=True):
    r = hdc.shell(cmd)
    if not quiet:
        print(f'    $ {cmd}\n      -> {(r.stdout or "").strip()[:120]}')
    return r


def _fault_names(hdc) -> set:
    return set((_sh(hdc, f'ls {FAULTLOGGER_DIR}').stdout or '').split())


def _wait_new_fault(hdc, before: set, timeout: float = FAULT_WAIT_S):
    """轮询等 faultlogger 出现新文件，返回 (新文件名集合, 等待秒数)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        new = _fault_names(hdc) - before
        if new:
            return new, time.time() - t0
        time.sleep(FAULT_POLL_S)
    return set(), time.time() - t0


def _launch(hdc, bundle, ability):
    """拉起应用：force-stop → Home → start 三步。

    ⚠️ 只 `aa start` 会复用实例、回到上次停留的页面（真机实测踩过），
    那样测的就不是「刚启动」的状态。
    """
    _sh(hdc, f'aa force-stop {bundle}')
    _sh(hdc, 'uitest uiInput keyEvent Home')
    time.sleep(0.6)
    hdc.start_ability(bundle, ability)
    time.sleep(2.5)          # 等应用起来并稳定


def _collect(hdc, bundle, tag, since=None):
    print(f'  [采集] {tag} …')
    sig = collect_signals(hdc, bundle, out_dir=os.path.join(OUT, tag),
                          since=since, collect_screenshot=False)
    print(f'         节点数={sig.layout_nodes} 进程存活={sig.process_alive} '
          f'窗口内故障日志={len(sig.faults_in_window)}')
    if sig.warnings:
        for w in sig.warnings:
            print(f'         ⚠️ {w}')
    return sig


def _report(title, sig, expect_kind):
    hits = [a for a in sig.anomalies if a.kind == expect_kind]
    print(f'  [判定] {title}')
    if hits:
        for a in hits:
            print(f'         ✅ {a.kind} conf={a.confidence} — {a.evidence[:150]}')
    else:
        print(f'         ❌ 未报出 {expect_kind}')
        print(f'         实际异常: {[(a.kind, a.confidence) for a in sig.anomalies] or "(无)"}')
    return bool(hits)


def scene_crash(hdc, bundle, ability, signal: int = 11) -> bool:
    print('\n' + '=' * 74)
    print(f'  场景 1/2：CRASH —— kill -{signal} 杀被测应用'
          f'（{"SIGSEGV，会产生 faultlog" if signal == 11 else "非 SIGSEGV"}）')
    print('=' * 74)
    t_launch = time.time()
    _launch(hdc, bundle, ability)

    # 基线窗口只回看几秒 —— 目的是「注入前没有异常」，不是「历史上没有异常」
    base = _collect(hdc, bundle, 'crash_baseline',
                    since=t_launch - BASELINE_LOOKBACK_S)
    base_hits = [a for a in base.anomalies if a.kind == 'CRASH']
    print(f'  [基线] CRASH 异常 {len(base_hits)} 条（期望 0）')

    pid = (_sh(hdc, f'pidof {bundle}').stdout or '').strip().split()
    if not pid:
        print('  ⚠️ 拿不到 pid（应用没起来？），跳过本场景')
        return False

    before_faults = _fault_names(hdc)
    print(f'  [注入] kill -{signal} {pid[0]}  ({bundle})')
    t_inject = time.time()
    _sh(hdc, f'kill -{signal} {pid[0]}')

    new, waited = _wait_new_fault(hdc, before_faults)
    if new:
        print(f'  [落盘] 系统新增崩溃日志：{sorted(new)}（等了 {waited:.1f}s）')
    else:
        print(f'  [落盘] ⚠️ 等了 {waited:.1f}s 仍无新崩溃日志 —— '
              f'只能拿「进程消失」的弱证据（conf≈0.35）')

    after = _collect(hdc, bundle, 'crash_after', since=t_inject)
    ok = _report('注入后应报 CRASH', after, 'CRASH')
    print(f'  [基线复核] 注入前 CRASH {len(base_hits)} 条 → 注入后 '
          f'{[(a.kind, a.confidence) for a in after.anomalies]}')
    return ok and not base_hits


def scene_no_window(hdc, bundle, ability) -> bool:
    print('\n' + '=' * 74)
    print('  场景 2/2：NO_WINDOW —— 息屏（锁屏不进无障碍树）')
    print('=' * 74)
    t_launch = time.time()
    _launch(hdc, bundle, ability)

    base = _collect(hdc, bundle, 'nowindow_baseline',
                    since=t_launch - BASELINE_LOOKBACK_S)
    base_hits = [a for a in base.anomalies if a.kind == 'NO_WINDOW']
    print(f'  [基线] NO_WINDOW 异常 {len(base_hits)} 条（期望 0）')

    print('  [注入] power-shell suspend（息屏 → 锁屏）')
    t_inject = time.time()
    _sh(hdc, 'power-shell suspend')
    # 息屏 → 锁屏有个过渡，控件树变空需要一点时间；轮询到「无窗口」或超时
    deadline = time.time() + 12.0
    while time.time() < deadline:
        if not DeviceGuard(hdc, verbose=False).has_window():
            break
        time.sleep(1.5)

    after = _collect(hdc, bundle, 'nowindow_after', since=t_inject)
    ok = _report('息屏后应报 NO_WINDOW', after, 'NO_WINDOW')

    print('  [恢复] 唤醒 + 钉住息屏超时')
    guard = DeviceGuard(hdc, verbose=False)
    print(f'         ensure_awake = {guard.ensure_awake()}')
    return ok and not base_hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--scene', default='all',
                    choices=['all', 'crash', 'no_window'])
    ap.add_argument('--signals', type=int, default=11,
                    help='崩溃注入用的信号：11=SIGSEGV（默认，系统会落 faultlog）；'
                         '9=SIGKILL（不落日志，只能拿到 0.35 的弱证据，可作对照组）')
    args = ap.parse_args()

    hdc = require_device(target=args.target)
    print(f'[0] 设备 {args.target}｜被测应用 {args.bundle}')
    print('    提示：崩溃判据依赖设备时间窗口，跑之前先 '
          '`python tools/sync_device_time.py --target <序列号>`')

    results = {}
    if args.scene in ('all', 'crash'):
        results['crash'] = scene_crash(hdc, args.bundle, args.ability,
                                       signal=args.signals)
    if args.scene in ('all', 'no_window'):
        results['no_window'] = scene_no_window(hdc, args.bundle, args.ability)

    print('\n' + '=' * 74)
    print('  结论')
    print('=' * 74)
    for k, v in results.items():
        print(f'  {k:<12} {"✅ 注入后判据命中" if v else "❌ 未命中（见上面输出）"}')
    print()
    print('  ⚠️ 真机**造不出来**的类别（不假装跑过）：')
    print('     白屏      —— 需要一个真的白屏页面，真机上没有可复现的构造手段')
    print('     无响应    —— 需要应用真的卡住，真机不可控；判据本身置信度只有 0.3')
    print('     布局异常  —— 需要控件真的越出屏幕，真机上没见过真实案例')
    print('     这三类的正例目前只有单测支撑；要补只能用模拟器受控构造。')
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
