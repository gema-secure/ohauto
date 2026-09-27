# -*- coding: utf-8 -*-
"""镜像就绪后的一键验证：启动 Mate X7 → 切折叠态 → 抓屏幕信息与控件树。

这是 跨形态测试链路的**端到端确认**：
如果这个脚本能跑通，说明「模拟器命令行 + 折叠态切换 + 控件树导出」
整条链路可用，跨形态 的后续工作就有地基了。

## ★ 两个血的教训（写死在这里，别再踩）

**教训 1：不能用「`-start` 进程退出」判断启动成功。**
`Emulator.exe -start` 是**前台常驻进程**，机器不死它不退出。
早期版本用 `run(timeout=300)` 等它，结果是撑满 300s 后超时，
**并把已经跑起来的模拟器一起杀掉**。
→ 正确判据：**设备能被 `hdc list targets` 看见，且能 `hdc shell echo`**。

**教训 2：启动模拟器必须有「长期存活的后台任务」持有它。**
它依附调用者所在会话 —— 调用方进程一退，模拟器跟着死。
而且**不能用 `DETACHED_PROCESS`**（实测：完全脱离控制台会无报错静默退出，
日志只留一行 `Windows Hypervisor Platform accelerator is operational`）。
→ 推荐用法（`run_in_background` 的 shell 里）：

    "C:/.../Emulator.exe" -start "Mate X7" > _out/emu.log 2>&1

## ★ 判定标准：看**效果**，不是看 rc

折叠态"命令成功"（`Scenario simulation success.`）**不等于**形态真的变了。
Mate X7 是**双屏**设备，两块屏分辨率恒定不变：

    screen[0] = 2416x2210（内屏）  screen[1] = 1080x2444（外屏）

折叠态切换的真实表现是**两块屏的 powerStatus 互换**：

    open      → screen[0] ON  / screen[1] OFF
    half-open → screen[0] ON  / screen[1] OFF
    close     → screen[0] OFF / screen[1] ON   ← 切到外屏

所以本脚本判定「切换生效」的依据是 **active screen 变了**，不是 rc。

用法：
    python tools/verify_emulator.py                  # 全流程
    python tools/verify_emulator.py --status-only    # 只看状态，不启动
    python tools/verify_emulator.py --skip-fold      # 跳过折叠态验证
    python tools/verify_emulator.py --screens-only   # 只抓屏幕信息

退出码：0 全通过 / 1 有步骤失败 / 2 前置条件不满足
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import emulator_cli as ec                                        # noqa: E402

#: 跨形态 要对照的两个形态 —— 折叠屏（可切态）+ 平板（宽比例）
PRIMARY = 'Mate X7'           # foldable，要验证折叠态切换
SECONDARY = 'MatePad Pro 13'  # tablet，跨形态 的宽比例对照

#: Mate X7 支持的折叠态（实测自 -help：FoldableFold 三种）
FOLD_SEQUENCE = ['open', 'half-open', 'close']


def hdr(t):
    print()
    print('=' * 62)
    print(t)
    print('=' * 62)


def step(n, total, text):
    print(f'\n[{n}/{total}] {text}')
    print('-' * 62)


def check_environment():
    """前置条件：协议 + 镜像。返回 (ok, 问题列表)。"""
    probs = ec.preflight()
    return (not probs), probs


def show_status():
    hdr('环境状态')
    print(f'Emulator 版本 : {ec.version()}')
    print(f'可执行文件    : {ec.find_emulator()}')
    print(f'hdc 可执行文件: {ec.find_hdc()}')
    print()
    lic = ec.license_status()
    print('协议：')
    for k, v in lic.items():
        print(f'  [{"已接受" if v else "未接受"}] {k}')
    print()
    insts = ec.list_instances(details=True)
    print(f'实例（{len(insts)} 个）：')
    for i in insts:
        n = os.path.basename(str(i.get('instancePath', '')))
        print(f'  - {n:<18} type={i.get("deviceType"):<10} '
              f'ram={i.get("hw.ramSize", "?")}MB')
    print()
    imgs = ec.list_images()
    dl = [i for i in imgs if str(i.get('downloaded')).lower() == 'true']
    print(f'镜像：{len(dl)}/{len(imgs)} 已下载')
    for i in dl:
        print(f'  [已下载] {i.get("deviceType"):<12} {i.get("osVersion")}')
    print()
    mem = ec.available_memory_gb()
    print(f'可用内存：{mem:.2f} GB' if mem else '可用内存：读不到')
    print()
    tgt = ec.list_targets()
    print(f'在线设备：{tgt if tgt else "（无）"}')


def ensure_running(name, wait_timeout=240):
    """确保实例在跑。返回 (ok, 信息字符串)。

    ★ 不看 `-start` 的退出码 —— 看设备能不能被 hdc 看见。
    """
    # 已经有设备在线就直接用（可能是别处拉起来的）
    dev = ec.list_targets()
    if dev and ec.device_ready():
        print(f'  ✅ 设备已在线：{dev[0]}（无需重新启动）')
        return True, dev[0]

    print(f'等待设备上线：{name}（冷启动实测约 40–60s，上限 {wait_timeout}s）…')
    t0 = time.time()
    dev = ec.wait_for_device(timeout=wait_timeout, interval=5)
    if not dev:
        print(f'  ❌ {wait_timeout}s 内没等到设备')
        print('     排查：① 模拟器是不是没在后台任务里跑（它依附调用者会话）')
        print('           ② 内存够不够（实例要 4 GB）')
        print('           ③ 后台任务的日志里有没有报错')
        return False, None

    print(f'  ✅ 设备上线：{dev}（耗时 {time.time() - t0:.1f}s）')
    if ec.device_ready():
        print('  ✅ shell 通道可用')
    else:
        print('  ⚠️ 设备在列表里但 shell 不通，后面可能失败')
    return True, dev


def show_screens(tag=''):
    """抓屏幕信息并打印。返回 screens 列表。"""
    screens = ec.screen_info()
    if not screens:
        print('  ⚠️ 没抓到屏幕信息（hidumper 无输出？）')
        return []
    act = ec.active_screen(screens)
    print(f'  当前点亮屏幕：'
          + (f'screen[{act["index"]}] {act["width"]}x{act["height"]}'
             if act else '（没有 ON 的屏）'))
    for s in screens:
        mark = '★' if act and s['index'] == act['index'] else ' '
        print(f'    {mark} screen[{s["index"]}]  {s["width"]}x{s["height"]}  '
              f'{s["power_status"]}  backlight={s["backlight"]}')
    return screens


def verify_folded_states(name):
    """★ 跨形态 核心：逐个试折叠态，每次抓屏幕信息看**真实效果**。

    返回 [(state, cmd_ok, active_screen_key_or_None), ...]
    """
    results = []
    for st in FOLD_SEQUENCE:
        print(f'\n  → 切到 {st!r} …')
        r = ec.set_folded_state(name, st)
        ok = r['rc'] == 0
        out = (r['stdout'] + r['stderr']).strip()
        print(f'     foldedState rc={r["rc"]}  {out.splitlines()[0] if out else ""}')
        if not ok:
            for line in out.splitlines()[:4]:
                print(f'     {line}')
            results.append((st, False, None))
            continue

        # 给渲染层一点时间响应形态变化
        time.sleep(6)

        screens = ec.screen_info()
        act = ec.active_screen(screens)
        if act:
            key = f'{act["width"]}x{act["height"]}'
            print(f'     点亮屏 = screen[{act["index"]}] {key}')
            results.append((st, True, key))
        else:
            print('     ⚠️ 没有 ON 的屏 —— 解析有问题或真的全黑')
            results.append((st, True, None))

    return results


def main():
    ap = argparse.ArgumentParser(description='模拟器端到端验证')
    ap.add_argument('--status-only', action='store_true',
                    help='只看状态，不做启动')
    ap.add_argument('--screens-only', action='store_true',
                    help='只抓屏幕信息（需设备已在线）')
    ap.add_argument('--instance', default=PRIMARY,
                    help=f'要启动的实例名（默认 {PRIMARY}）')
    ap.add_argument('--skip-fold', action='store_true',
                    help='跳过折叠态验证（非折叠设备用）')
    ap.add_argument('--wait-timeout', type=int, default=240,
                    help='等设备上线的秒数上限（默认 240）')
    args = ap.parse_args()

    show_status()

    if args.screens_only:
        hdr('屏幕信息')
        show_screens()
        return 0

    ok, probs = check_environment()
    if not ok:
        hdr('❌ 前置条件不满足，先解决这些')
        for p in probs:
            print(f'  - {p}')
        return 2
    if args.status_only:
        hdr('✅ 前置条件都满足（--status-only 到此为止）')
        return 0

    hdr('端到端验证')
    ok_run, _ = ensure_running(args.instance, args.wait_timeout)
    if not ok_run:
        print('\n❌ 设备没起来，后面的折叠态验证跳过')
        return 1

    if args.skip_fold:
        hdr('✅ 设备就绪（已跳过折叠态验证）')
        return 0

    step(1, 2, f'折叠态切换验证（{args.instance}）')
    print('\n  基线屏幕状态：')
    base = show_screens()
    base_act = ec.active_screen(base)
    base_key = f'{base_act["width"]}x{base_act["height"]}' if base_act else None

    results = verify_folded_states(args.instance)

    step(2, 2, '汇总')
    print(f'\n  {"折叠态":<12} {"命令":<6} {"点亮屏":<14}')
    print('  ' + '-' * 36)
    keys = set()
    for st, ok, key in results:
        print(f'  {st:<12} {"✅" if ok else "❌":<6} {key or "-":<14}')
        if key:
            keys.add(key)

    print()
    all_ok = all(ok for _, ok, _ in results)
    if not all_ok:
        print('  ❌ 有折叠态切换失败，看上面的输出')
        return 1

    if len(keys) > 1:
        print(f'  ✅✅ 折叠态切换**真实生效**：检出 {len(keys)} 种不同点亮屏')
        print(f'      {", ".join(sorted(keys))}')
        print(f'      （基线也是 {base_key}）')
        print('      → 跨形态差异分析有真实数据来源了')
        return 0

    if len(keys) == 1:
        only = next(iter(keys))
        if base_key and base_key != only:
            print(f'  ⚠️ 折叠态命令都成功，但点亮屏只有一种：{only}')
            print(f'     基线是 {base_key} —— 说明至少切换过一次')
        else:
            print(f'  ⚠️ 折叠态命令都成功，但点亮屏始终是 {only}（基线也是它）')
            print('     说明**形态没有真的切换** —— 可能这两个态在物理上同屏，')
            print('     也可能需要设备侧配合。此时不能算「验证通过」。')
        return 1               # 没验证出「形态真实切换」= 未达标，不能返回 0

    print('  ⚠️ 折叠态命令成功，但没能解析出屏幕信息')
    print('     下一步：手工跑 `--screens-only` 看原始输出')
    return 1                   # 拿不到结论 ≠ 通过


if __name__ == '__main__':
    sys.exit(main())
