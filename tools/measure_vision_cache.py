# -*- coding: utf-8 -*-
"""B14 视觉缓存命中率测量 —— 带视觉定位的探索运行 + 命中率出数。

口径说明（对外引用必带）：
- 阈值临时调高（默认 3 → 99）：**强制视觉介入**，否则树候选丰富的页面
  永远不触发视觉调用、缓存无从测量。这是测量口径，不是产品默认行为；
- 缓存键 = （页面指纹, 指令）——**页面重访即命中**，导航到新页面即未命中；
- 命中率 = cache_hits / (cache_hits + cache_misses)，由 TieredVisionLocator
  自带统计出口给出。

用法::

    OHAUTO_VISION_BASE_URL=... OHAUTO_VISION_API_KEY=... OHAUTO_VISION_MODEL=... \\
    python tools/measure_vision_cache.py --target <串号> \\
        --bundle com.ohos.settings --ability com.ohos.settings.MainAbility
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.driver import Driver                             # noqa: E402
from ohauto.explorer import Budget, Explorer                 # noqa: E402
from ohauto.hdc import Hdc                                   # noqa: E402
from ohauto.vision import OpenAICompatibleProvider, TieredVisionLocator  # noqa: E402


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description='B14 视觉缓存命中率测量')
    ap.add_argument('--target', required=True)
    ap.add_argument('--bundle', default='com.ohos.settings')
    ap.add_argument('--ability', default='com.ohos.settings.MainAbility')
    ap.add_argument('--max-pages', type=int, default=4)
    ap.add_argument('--actions-per-page', type=int, default=3)
    ap.add_argument('--out', default='tools/_out/b14_cache')
    args = ap.parse_args(argv)

    key = os.environ.get('OHAUTO_VISION_API_KEY', '')
    if not key:
        print('❌ 未配置 OHAUTO_VISION_API_KEY —— 真模型测量必须带 key')
        return 2

    provider = OpenAICompatibleProvider.from_env()
    # 视觉接口 = **Provider 本体**（枚举式 locate(image, instruction, w, h)），
    # 不是 TieredVisionLocator——后者是「找单个指定元素」的 API，
    # 签名与探索器的 duck-type 调用不匹配（首跑实测踩到）。
    vision = provider

    hdc = Hdc(target=args.target)
    # 息屏/锁屏会让视觉看到锁屏界面（首跑实测：视觉正确建议「上滑解锁」，
    # 但管道执行的是 tap）——先唤醒 + 解锁 + 钉住息屏
    from ohauto.runner import DeviceGuard
    DeviceGuard(hdc, verbose=True).ensure_awake()
    hdc.shell('aa force-stop %s; aa start -a %s -b %s'
              % (args.bundle, args.ability, args.bundle), timeout=30)
    time.sleep(5)

    # 测量口径：阈值调高强制视觉介入（见文件头口径说明）
    keep = Explorer.BLIND_PAGE_THRESHOLD
    Explorer.BLIND_PAGE_THRESHOLD = 99
    d = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
               artifact_dir=os.path.join(args.out, 'artifacts'), verbose=False)
    ex = Explorer(d, artifact_dir=args.out, verbose=True,
                  vision_locator=vision)
    t0 = time.time()
    ex.explore(args.max_pages,
               Budget(max_pages=args.max_pages,
                      max_actions_per_page=args.actions_per_page),
               return_back=True)
    Explorer.BLIND_PAGE_THRESHOLD = keep

    stats = dict(ex.vision_stats)
    ch, cm = stats.get('cache_hits', 0), stats.get('cache_misses', 0)
    rate = ch / (ch + cm) if (ch + cm) else 0.0
    stats['cache_hit_rate'] = round(rate, 3)
    stats['elapsed_min'] = round((time.time() - t0) / 60, 1)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'b14_result.json'), 'w',
              encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print('=' * 56)
    print('  B14 视觉缓存命中率: %.1f%%（%d 命中 / %d 未命中）'
          % (rate * 100, ch, cm))
    print('  KPI ≥60%% → %s' % ('✅ 达标' if rate >= 0.6 else '❌ 未达标'))
    print('  明细: %s' % os.path.join(args.out, 'b14_result.json'))
    print('=' * 56)
    return 0 if rate >= 0.6 else 1


if __name__ == '__main__':
    sys.exit(main())
