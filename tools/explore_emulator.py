# -*- coding: utf-8 -*-
"""折叠屏模拟器上的自动探索 —— 探索器 × 折叠态 的第一份证据（A7）。

为什么要有这个
--------------
此前模拟器链路（指南-折叠屏模拟器.md）验证到「折叠态切换 + 控件树采集 +
跨形态差异比对」为止，**探索器从没在模拟器上跑过**。本工具补上这一环：
在同一个折叠屏模拟器实例上，对同一个应用按不同折叠态分别跑一次自动探索，
产出每态的页面图 / 覆盖度 / tarpit 台账，并给出跨形态对照。

⚠️ **证据口径（指南术语更正，2026-09-23）**
模拟器实例（Mate X7）**不是真机**。本工具的产出是「探索器在折叠屏形态
切换下的行为证据」（高地 6 的中间形态证据），**不能**写成「真机跑通」；
真机值仍按规划 B7 由 `explore_coverage.py` 在 DAYU200 上出。

跨形态红线：**折叠态切换后必须重新采集，之前的控件树 / 坐标缓存一律作废**
—— 本工具每个折叠态都新建 Driver、重启被测应用，从机制上保证不复用。

用法::

    python tools/explore_emulator.py                          # 默认 open,close 探索系统设置
    python tools/explore_emulator.py --states open            # 只跑一个折叠态
    python tools/explore_emulator.py --bundle <包名> --ability <Ability>

退出码：全部折叠态都产出状态图 → 0；任一态失败 → 1。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import emulator_cli                                                    # noqa: E402
from preflight import require_device                                   # noqa: E402
from ohauto.driver import Driver                                      # noqa: E402
from ohauto.explorer import Budget, Explorer                          # noqa: E402
from ohauto.runner import DeviceGuard                                 # noqa: E402

#: 折叠态切换后系统要重新完成布局——立即采集约 1/3 概率拿到空树（2026-09-18 实测）
FOLD_SETTLE_SEC = 6.0
EMPTY_TREE_RETRIES = 3


def _screen_on_and_awake(hdc) -> None:
    """折叠态切换可能连带熄屏（指南坑 2：判据是屏幕电源状态）——先唤醒。"""
    DeviceGuard(hdc, verbose=False).ensure_awake()


def _wait_real_tree(drv: Driver, what: str) -> bool:
    """刷新控件树并确认不是「只有根节点的空树」（空树陷阱，见 crossform_run）。"""
    for attempt in range(1, EMPTY_TREE_RETRIES + 1):
        try:
            drv.refresh()
            n = sum(1 for _ in drv.root.walk())
        except Exception as e:                                # noqa: BLE001
            n = 0
            print(f'  [{what}] 第 {attempt}/{EMPTY_TREE_RETRIES} 次刷新异常: '
                  f'{type(e).__name__}: {str(e)[:70]}')
        if n > 1:
            return True
        print(f'  [{what}] 第 {attempt}/{EMPTY_TREE_RETRIES} 次拿到空树'
              f'（{n} 节点），等系统重绘后重试')
        time.sleep(FOLD_SETTLE_SEC * attempt)
    return False


def explore_one_state(state: str, args, hdc, out_dir: str) -> Dict[str, Any]:
    """在指定折叠态下重启被测应用并探索一次，返回该态的摘要。"""
    print(f'\n=== 折叠态 [{state}] ===')
    if state != 'current':
        r = emulator_cli.set_folded_state(args.instance, state)
        out = str(r.get('stdout', '') or r.get('stderr', '')).strip()[:80]
        print(f'  [fold] {args.instance} -> {state} :: {out}')
        time.sleep(FOLD_SETTLE_SEC)

    _screen_on_and_awake(hdc)

    # 每个折叠态新建 Driver：折叠后坐标系整体作废，绝不复用（红线）
    d = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
               artifact_dir=os.path.join(out_dir, state), verbose=False)
    try:
        if not _wait_real_tree(d, f'{state}·预热'):
            return {'state': state, 'ok': False, 'error': '控件树持续为空'}

        hdc.shell('aa force-stop ' + args.bundle)
        hdc.start_ability(args.bundle, args.ability)
        time.sleep(args.app_settle)
        if not _wait_real_tree(d, f'{state}·应用'):
            return {'state': state, 'ok': False, 'error': '应用启动后控件树为空'}

        ex = Explorer(d, artifact_dir=d.artifact_dir, verbose=True)
        budget = Budget(max_pages=args.max_pages,
                        max_actions_per_page=args.actions_per_page,
                        max_seconds=args.seconds)
        ex.explore(args.max_pages, budget, return_back=False)
        cov = ex.coverage
        graph_path = os.path.join(out_dir, state, 'graph.json')
        ex.save_graph(graph_path)

        summary = {
            'state': state, 'ok': True,
            'screen': str(d.screen_size()),
            'pages': cov.pages, 'pages_structural': cov.pages_structural,
            'coverage': cov.to_dict(),
            'transitions': len(ex.graph.transitions),
            'tarpit_hits': len(ex.tarpit_hits),
            'budget': ex.last_budget.to_dict() if ex.last_budget else {},
            'graph': graph_path,
        }
        print(f'  [结果] 页面状态 {cov.pages}（结构归并 {cov.pages_structural}），'
              f'覆盖度 {cov.ratio:.0%}，跳转 {summary["transitions"]} 条，'
              f'tarpit 拦截 {summary["tarpit_hits"]} 次')
        return summary
    finally:
        try:
            d.close()
        except Exception:                                     # noqa: BLE001
            pass


def _to_markdown(results: Dict[str, Any], states: List[Dict[str, Any]]) -> str:
    ok_states = [s for s in states if s.get('ok')]
    lines = ['# 折叠屏模拟器 × 自动探索 报告', '',
             f'> 生成时间：{results["generated_at"]}；实例：{results["instance"]}'
             f'（{results["bundle"]}）',
             '> ⚠️ 模拟器实例**不是真机**：本报告是探索器在折叠形态切换下的'
             '行为证据（高地 6 中间形态），真机值由 `explore_coverage.py` 出。'
             '折叠态切换后每个态均新建 Driver、重启应用，坐标缓存零复用。', '']
    if len(ok_states) >= 2:
        lines += ['## 跨形态对照', '',
                  '| 指标 | ' + ' | '.join(s['state'] for s in ok_states) + ' |',
                  '|---|' + '---|' * len(ok_states)]
        rows = [('屏幕', 'screen'), ('页面状态数', 'pages'),
                ('结构归并页面数', 'pages_structural'),
                ('跳转边数', 'transitions'), ('tarpit 拦截', 'tarpit_hits')]
        for label, key in rows:
            lines.append('| ' + label + ' | '
                         + ' | '.join(str(s.get(key, '-')) for s in ok_states) + ' |')
        lines.append('| 覆盖度 | '
                     + ' | '.join(f'{s["coverage"]["ratio"]:.0%}' for s in ok_states)
                     + ' |')
        lines.append('')
    for s in states:
        if s.get('ok'):
            lines.append(f"## 折叠态 {s['state']}", )
            lines.append(f'- 屏幕：{s["screen"]}；页面状态 {s["pages"]}'
                         f'（结构归并 {s["pages_structural"]}）；'
                         f'覆盖度 {s["coverage"]["ratio"]:.0%}'
                         f'（{s["coverage"]["interactive_visited"]}/'
                         f'{s["coverage"]["interactive_total"]}，'
                         f'危险控件拦截 {s["coverage"]["blocked"]}）')
            lines.append(f'- 跳转 {s["transitions"]} 条；tarpit 拦截 '
                         f'{s["tarpit_hits"]} 次；状态图 `{s["graph"]}`')
        else:
            lines.append(f"## 折叠态 {s['state']}", )
            lines.append(f'- ❌ 失败：{s.get("error")}')
        lines.append('')
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--instance', default='Mate X7')
    ap.add_argument('--states', default='open,close',
                    help='逗号分隔的折叠态（open/half-open/close/current）')
    ap.add_argument('--bundle', default='com.huawei.hmos.settings')
    ap.add_argument('--ability', default='com.huawei.hmos.settings.MainAbility')
    ap.add_argument('--target', default='127.0.0.1:5555')
    ap.add_argument('--max-pages', type=int, default=6)
    ap.add_argument('--actions-per-page', type=int, default=5)
    ap.add_argument('--seconds', type=float, default=180.0)
    ap.add_argument('--app-settle', type=float, default=3.0)
    ap.add_argument('--out', default=os.path.join(HERE, '_out', 'explore_emulator'))
    args = ap.parse_args(argv)

    states = [s.strip() for s in args.states.split(',') if s.strip()]
    os.makedirs(args.out, exist_ok=True)
    hdc = require_device(target=args.target)

    print('=' * 70)
    print(f'  模拟器自动探索：{args.instance} × 折叠态 {states}')
    print(f'  被测应用：{args.bundle}/{args.ability}')
    print('=' * 70)

    results: Dict[str, Any] = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'instance': args.instance, 'bundle': args.bundle,
        'ability': args.ability, 'caveat': '模拟器实例不是真机证据',
        'states': [],
    }
    all_ok = True
    for state in states:
        try:
            s = explore_one_state(state, args, hdc, args.out)
        except Exception as e:                                # noqa: BLE001
            s = {'state': state, 'ok': False,
                 'error': f'{type(e).__name__}: {e}'}
            print(f'  [错误] 折叠态 {state} 探索失败：{s["error"]}')
        results['states'].append(s)
        all_ok &= bool(s.get('ok'))

    json_path = os.path.join(args.out, 'explore_emulator.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    md_path = os.path.join(args.out, 'explore_emulator.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(_to_markdown(results, results['states']))

    print('\n' + '=' * 70)
    print(f'  报告：{json_path}')
    print(f'        {md_path}')
    print(f'  总体：{"全部折叠态探索成功" if all_ok else "有折叠态失败"}')
    print('=' * 70)
    return 0 if all_ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
