# -*- coding: utf-8 -*-
"""B14 视觉缓存命中率测量 —— 多轮重放 + 命中率出数。

口径说明（对外引用必带）：
- 阈值临时调高（默认 3 → 99）：**强制视觉介入**，否则树候选丰富的页面
  永远不触发视觉调用、缓存无从测量。这是测量口径，不是产品默认行为；
- 缓存键 = 页面**结构指纹**——内容微变时稳定（content_key 会永不命中，
  已修），同页重访即命中，导航到新页面即未命中；
- **单遍探索的命中率恒为 0**（结构性：探索器每个状态只处理一次，无重访
  即无命中）——正确测量是 `--rounds N` 多轮模式：每轮 force-stop + 显式
  重启 → 相同页面集合重现，视觉页缓存跨轮留存 → 第 2 轮起全命中，
  N 轮后命中率 ≈ (N-1)/N，3 轮即 67% ≥60%（KPI）。

用法::

    OHAUTO_VISION_BASE_URL=... OHAUTO_VISION_API_KEY=... OHAUTO_VISION_MODEL=... \\
    python tools/measure_vision_cache.py --target <串号> \\
        --bundle com.ohos.settings --ability com.ohos.settings.MainAbility \\
        --rounds 3
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.driver import Driver                             # noqa: E402
from ohauto.explorer import Budget, Explorer                 # noqa: E402
from ohauto.hdc import Hdc                                   # noqa: E402
from ohauto.vision import OpenAICompatibleProvider           # noqa: E402


def _reset_exploration_state(ex: Explorer) -> None:
    """新一轮前重置探索台账（状态图/覆盖度/tarpit/弹窗判定），**保留**
    视觉页缓存与 vision_stats——缓存跨轮留存正是 B14 的测量口径。

    台账清单与 ``Explorer.__init__`` 一一对应；放在工具侧而不是加公开
    reset 方法，是为了不改探索器本体（B 侧模块，测量工装自兜底）。"""
    from ohauto.explorer import DialogVerdict, StateGraph
    ex.graph = StateGraph()
    ex._universe = set()
    ex._done = set()
    ex._per_page = {}
    ex._exhausted = set()
    ex._blocked = set()
    ex._tarpit_family = {}
    ex.tarpit_hits = []
    ex.back_verdicts = []
    ex.skipped = []
    ex.dialog = DialogVerdict()
    ex.last_budget = None


def _snapshot(stats: Dict[str, Any]) -> Dict[str, int]:
    return {k: int(stats.get(k, 0))
            for k in ('calls', 'cache_hits', 'cache_misses', 'errors',
                      'suggested', 'tapped_ok')}


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description='B14 视觉缓存命中率测量（多轮版）')
    ap.add_argument('--target', required=True)
    ap.add_argument('--bundle', default='com.ohos.settings')
    ap.add_argument('--ability', default='com.ohos.settings.MainAbility')
    ap.add_argument('--max-pages', type=int, default=4)
    ap.add_argument('--actions-per-page', type=int, default=3)
    ap.add_argument('--rounds', type=int, default=1,
                    help='重放轮数（≥2 才有缓存命中可测；3 轮即 67%%）')
    ap.add_argument('--out', default='tools/_out/b14_cache')
    args = ap.parse_args(argv)
    if args.rounds < 1:
        print('❌ --rounds 必须 ≥1')
        return 2

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
    from ohauto.runner import DeviceGuard
    guard = DeviceGuard(hdc, verbose=True)

    # 测量口径：阈值调高强制视觉介入（见文件头口径说明）
    keep = Explorer.BLIND_PAGE_THRESHOLD
    Explorer.BLIND_PAGE_THRESHOLD = 99
    try:
        d = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
                   artifact_dir=os.path.join(args.out, 'artifacts'), verbose=False)
        ex = Explorer(d, artifact_dir=args.out, verbose=True,
                      vision_locator=vision)

        rounds: List[Dict[str, Any]] = []
        t0 = time.time()
        prev = _snapshot(ex.vision_stats)
        for rnd in range(1, args.rounds + 1):
            # 每轮冷启动：唤醒（防息屏）→ force-stop → MainAbility 显式拉起
            # （`aa start -b` 隐式拉起对扩展型应用不可靠且 rc=0 说谎）→
            # 等 8s+（冷启动 6s+，交接红线）
            guard.ensure_awake()
            hdc.shell('aa force-stop %s; aa start -a %s -b %s'
                      % (args.bundle, args.ability, args.bundle), timeout=30)
            time.sleep(8)
            if rnd > 1:
                _reset_exploration_state(ex)

            ex.explore(args.max_pages,
                       Budget(max_pages=args.max_pages,
                              max_actions_per_page=args.actions_per_page),
                       return_back=True)

            cur = _snapshot(ex.vision_stats)
            rec: Dict[str, Any] = {'round': rnd,
                                   'pages': len(ex.graph.states),
                                   'elapsed_min': round((time.time() - t0) / 60, 1)}
            rec.update({k: cur[k] - prev[k] for k in cur})
            rounds.append(rec)
            prev = cur

            # 增量落盘（长跑被杀只丢未完成的轮）
            total = _snapshot(ex.vision_stats)
            rate = (total['cache_hits'] / (total['cache_hits'] + total['cache_misses'])
                    if (total['cache_hits'] + total['cache_misses']) else 0.0)
            _write_result(args.out, rounds, total, rate, t0, args.rounds)
            print('  [轮 %d/%d] 本轮 命中 %d / 未命中 %d（页 %d，%.1f 分钟）'
                  '→ 累计命中率 %.1f%%'
                  % (rnd, args.rounds, rec['cache_hits'], rec['cache_misses'],
                     rec['pages'], rec['elapsed_min'], rate * 100), flush=True)

        total = _snapshot(ex.vision_stats)
        rate = (total['cache_hits'] / (total['cache_hits'] + total['cache_misses'])
                if (total['cache_hits'] + total['cache_misses']) else 0.0)
        result = _write_result(args.out, rounds, total, rate, t0, args.rounds)
    finally:
        Explorer.BLIND_PAGE_THRESHOLD = keep
    print('=' * 56)
    print('  B14 视觉缓存命中率（%d 轮）: %.1f%%（%d 命中 / %d 未命中）'
          % (args.rounds, rate * 100, total['cache_hits'], total['cache_misses']))
    print('  KPI ≥60%% → %s' % ('✅ 达标' if rate >= 0.6 else '❌ 未达标'))
    print('  明细: %s' % result)
    print('=' * 56)
    return 0 if rate >= 0.6 else 1


def _write_result(out_dir: str, rounds: List[Dict[str, Any]],
                  total: Dict[str, int], rate: float, t0: float,
                  n_rounds: int) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, 'b14_result.json')
    stats = dict(total)
    stats['cache_hit_rate'] = round(rate, 3)
    stats['rounds_requested'] = n_rounds
    stats['rounds_done'] = len(rounds)
    stats['per_round'] = rounds
    stats['elapsed_min'] = round((time.time() - t0) / 60, 1)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    return path


if __name__ == '__main__':
    sys.exit(main())
