"""真机验证：布局异常（LAYOUT_ANOMALY）判据在**真实控件树**上的表现。

为什么要单独跑一次真机：`_judge_layout_anomaly` 的判据是「可见子控件越出父容器」，
这条在真机上极易误报（滚动容器、装饰性溢出）。**误报率只能靠真机数据看**，
单测里手搓的树自证不了任何东西。

用法（设备在位）：
    python tools/verify_layout_anomaly_real.py
    python tools/verify_layout_anomaly_real.py --bundle com.ohos.note \\
        --ability com.ohos.note.MainAbility
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto.hdc import Hdc                              # noqa: E402
from ohauto.runner import DeviceGuard                   # noqa: E402
from ohauto.signals import collect_signals              # noqa: E402
from preflight import require_device, default_target                  # noqa: E402

DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
DEFAULT_BUNDLE = 'com.ohos.settings'
DEFAULT_ABILITY = 'com.ohos.settings.MainAbility'


def scan_fixtures() -> int:
    """对比三种候选判据在真机样本上的命中数 —— 判据就是按这张表选的。

    不开设备、不联网，直接扫 `datasets/gallery_13app/`。
    """
    import glob

    from ohauto.layout import parse_layout

    files = [p for p in sorted(glob.glob(os.path.join(
        os.path.dirname(HERE), 'datasets', 'gallery_13app', '*.json')))
        if not p.endswith('.meta.json')]
    print('%-24s %-8s %-12s %-10s %s' % ('样本', '节点数', '越出父容器', '子比父大', '越出屏幕'))
    print('-' * 74)
    totals = [0, 0, 0]
    for p in files:
        with open(p, encoding='utf-8') as f:
            root = parse_layout(f.read())
        oob = big = off = 0
        for n in root.walk():
            if n is root or not n.visible or n.rect.area <= 0:
                continue
            pp = n.parent
            if pp is not None and (n.rect.left < pp.rect.left
                                   or n.rect.top < pp.rect.top
                                   or n.rect.right > pp.rect.right
                                   or n.rect.bottom > pp.rect.bottom):
                oob += 1
                if n.rect.area > pp.rect.area:
                    big += 1
            if (n.rect.left < root.rect.left or n.rect.top < root.rect.top
                    or n.rect.right > root.rect.right
                    or n.rect.bottom > root.rect.bottom):
                off += 1
        totals[0] += oob
        totals[1] += big
        totals[2] += off
        print('%-24s %-8d %-12d %-10d %d'
              % (os.path.basename(p), sum(1 for _ in root.walk()), oob, big, off))
    print('-' * 74)
    print('合计：越出父容器 %d ｜ 子比父大 %d ｜ **越出屏幕 %d**'
          % (totals[0], totals[1], totals[2]))
    print()
    print('判读：前两条在**每个**正常样本上都命中（等于永远报警，没有区分度），')
    print('      所以 `_judge_layout_anomaly` 用的是第三条「越出屏幕」。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--out', default=os.path.join(HERE, '_out', 'layout_anomaly'))
    ap.add_argument('--scan-fixtures', action='store_true',
                    help='不开设备：扫真机夹具，对比三种候选判据的命中数')
    args = ap.parse_args()

    if args.scan_fixtures:
        return scan_fixtures()

    hdc = require_device(target=args.target)
    print(f'[0] 设备 {args.target}')

    # 息屏即锁屏，锁屏后控件树全空 —— 复用生产类，不抄逻辑
    guard = DeviceGuard(hdc, verbose=False)
    print(f'[1] 唤醒并钉住息屏超时: {guard.ensure_awake()}')

    # ⚠️ 必须 force-stop → Home → start 三步：`aa start` 是**复用实例**的，
    #    只 start 会停在上一轮页面，拿到的不是入口页（真机实测踩过）。
    print(f'[2] 拉起 {args.bundle}（先 force-stop 清状态）…')
    hdc.shell(f'aa force-stop {args.bundle}')
    hdc.shell('uitest uiInput keyEvent Home')
    hdc.start_ability(args.bundle, args.ability)

    print('[3] 采集信号（只采控件树，不采截图，聚焦布局判据）…')
    sig = collect_signals(hdc, args.bundle, out_dir=args.out,
                          collect_screenshot=False)

    print(f'    控件树节点数: {sig.layout_nodes}')
    print(f'    控件树文件  : {sig.layout_path}')
    print(f'    警告        : {sig.warnings or "(无)"}')

    la = [a for a in sig.anomalies if a.kind == 'LAYOUT_ANOMALY']
    other = [(a.kind, a.confidence) for a in sig.anomalies if a.kind != 'LAYOUT_ANOMALY']
    print(f'[4] 布局异常: {len(la)} 条')
    for a in la:
        print(f'    conf={a.confidence} {a.evidence}')
    print(f'    其它异常: {other or "(无)"}')

    print()
    print('判读：真机正常页面**不该**报 LAYOUT_ANOMALY（判据 = 可见控件越出屏幕）。')
    print('      若报了，先跑 `--scan-fixtures` 看它属于哪一类，')
    print('      再收紧判据 —— **不要**用调低置信度的办法把误报糊过去。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
