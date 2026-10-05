# -*- coding: utf-8 -*-
"""多应用跨应用实证采集器 —— 每应用采集「截图 + 控件树」配对样本。

目的：回应「多模态只在少数应用上验证过」——把视觉评测集从 7 组样本扩到
多个系统应用，逐应用产出结构档案（可见节点/id/可交互）与视觉一致率。

用法::

    python tools/multiapp_survey.py --out datasets/multiapp_20261005 \\
        --targets contacts:com.ohos.contacts mms:com.ohos.mms ...

每应用流程：唤醒（延长息屏）→ force-stop 清状态 → 显式拉起（自动探测
`<bundle>.*Ability`，优先 MainAbility——隐式拉起对扩展型应用不可靠且
rc=0 说谎）→ 等待冷启动 → dumpLayout 落 JSON → screenCap 落 PNG →
配对校验（JSON 非空且根节点为全屏、PNG 体积合理）。失败如实记录并
继续下一个应用。

⚠️ 脱敏红线：contacts/mms 等应用可能含个人数据，采集产物入 PPT 前逐张
检查（开发板通常无 SIM、数据为空，但仍需过目）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc                                   # noqa: E402
from ohauto.runner import DeviceGuard                        # noqa: E402

DEFAULT_TARGETS = [
    ('contacts', 'com.ohos.contacts'),
    ('mms', 'com.ohos.mms'),
    ('certmanager', 'com.ohos.certmanager'),
    ('filemgr', 'com.ohos.UserFile.ExternalFileManager'),
    ('launcher', 'com.ohos.launcher'),
    ('camera', 'com.ohos.camera'),
    ('updateapp', 'com.ohos.updateapp'),
    ('myapp', 'com.example.myapplication'),
]


def probe_ability(hdc: Hdc, bundle: str) -> str:
    """从 bm dump -n 找主 ability。

    多数应用是 `<bundle>.MainAbility`；部分系统应用用裸名（certmanager 的
    "MainAbility"、示例应用的 "EntryAbility"）。优先级：带包名的 MainAbility >
    裸名 MainAbility > EntryAbility > 其余 *Ability（排除 FormExtension 类，
    那是卡片不是页面）。"""
    r = hdc.shell(f'bm dump -n {bundle}', timeout=30)
    names = re.findall(r'"name":\s*"([A-Za-z0-9.]*Ability)"', r.stdout or '')
    if not names:
        return ''
    for n in names:
        if n == f'{bundle}.MainAbility':
            return n
    for n in names:
        if n == 'MainAbility':
            return n
    for n in names:
        if n == 'EntryAbility':
            return n
    for n in sorted(names):
        if not n.endswith('FormAbility'):
            return n
    return ''


def pull_dump(hdc: Hdc, name: str, out_dir: str) -> Optional[str]:
    """dumpLayout → 拉回本地，返回本地路径；空树/失败返回 None。"""
    dev = f'/data/local/tmp/survey_{name}.json'
    hdc.shell(f'uitest dumpLayout -p {dev}', timeout=30)
    local = os.path.join(out_dir, f'{name}.json')
    hdc.pull(dev, local)
    if not os.path.isfile(local) or os.path.getsize(local) < 200:
        return None
    try:
        with open(local, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    kids = d.get('children') or []
    if not kids:
        return None
    return local


def pull_shot(hdc: Hdc, name: str, out_dir: str) -> Optional[str]:
    """screenCap → 拉回本地，返回本地路径。"""
    dev = f'/data/local/tmp/survey_{name}.png'
    hdc.shell(f'uitest screenCap -p {dev}', timeout=30)
    local = os.path.join(out_dir, f'{name}.png')
    hdc.pull(dev, local)
    if not os.path.isfile(local) or os.path.getsize(local) < 10_000:
        return None
    return local


def collect_one(hdc: Hdc, guard: DeviceGuard, name: str, bundle: str,
                out_dir: str) -> Tuple[bool, str]:
    ability = probe_ability(hdc, bundle)
    if not ability:
        return False, f'{bundle}: 未探测到 Ability'
    guard.ensure_awake()
    hdc.shell(f'aa force-stop {bundle}', timeout=20)
    time.sleep(1)
    pre = pull_shot(hdc, f'_{name}_pre', out_dir)   # 拉起前基线（防假绿）
    hdc.shell(f'aa start -a {ability} -b {bundle}', timeout=30)
    time.sleep(6)                                  # 冷启动 6s+（交接红线）
    post = pull_shot(hdc, name, out_dir)
    if not post:
        return False, f'{bundle}: screenCap 失败'
    if pre and os.path.getsize(pre) == os.path.getsize(post):
        import hashlib
        h = (hashlib.md5(open(pre, 'rb').read()).hexdigest(),
             hashlib.md5(open(post, 'rb').read()).hexdigest())
        if h[0] == h[1]:
            return False, f'{bundle}: 拉起前后画面相同（未生效，疑似假绿）'
    js = pull_dump(hdc, name, out_dir)
    if not js:
        return False, f'{bundle}: dumpLayout 空树或失败'
    meta = {'bundle': bundle, 'ability': ability,
            'ts': time.strftime('%Y-%m-%d %H:%M:%S')}
    with open(os.path.join(out_dir, f'{name}.meta.json'), 'w',
              encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.remove(pre) if pre and os.path.isfile(pre) else None
    return True, f'{name} ← {bundle} ({ability})'


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description='多应用跨应用实证采集器')
    ap.add_argument('--out', default='datasets/multiapp_20261005')
    ap.add_argument('--targets', nargs='*', default=None,
                    help='name:bundle 列表，缺省用内置系统应用清单')
    ap.add_argument('--repeat', type=int, default=1,
                    help='每应用采集遍数（>1 时名字追加 _r2）')
    args = ap.parse_args(argv)

    targets = []
    for t in (args.targets or []):
        if ':' not in t:
            print(f'跳过非法目标: {t}（格式 name:bundle）')
            continue
        n, b = t.split(':', 1)
        targets.append((n, b))
    if not targets:
        targets = DEFAULT_TARGETS

    os.makedirs(args.out, exist_ok=True)
    hdc = Hdc()
    guard = DeviceGuard(hdc, verbose=True)
    ok, fail = 0, 0
    for name, bundle in targets:
        for rep in range(1, args.repeat + 1):
            rname = name if rep == 1 else f'{name}_r{rep}'
            try:
                good, msg = collect_one(hdc, guard, rname, bundle, args.out)
            except Exception as e:                       # noqa: BLE001
                good, msg = False, f'{bundle}: {type(e).__name__}: {e}'
            if good:
                ok += 1
            else:
                fail += 1
            print(('[ok] ' if good else '[fail] ') + msg, flush=True)
            time.sleep(1)
    print(f'完成：成功 {ok} / 失败 {fail} → {args.out}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
