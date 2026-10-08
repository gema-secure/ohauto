# -*- coding: utf-8 -*-
"""B15 应用长稳编排器 v2 —— 带前台核验的混合套件 2 小时连续压力。

v1 的教训（09-29）：hmos.settings 是**扩展型应用（无 MainAbility）**，
`aa start -b` 的隐式拉起时灵时不灵，而 **rc 照样是 0**（shell 通道成功 ≠
应用拉起）——v1 的重放全落在拨号盘/桌面上空转，31/31 是假绿。

v2 的每周期流程（约 2 分钟）::

    Home → (扰动轮: 折叠切换) → 点设置图标 → **前台核验**（主页标记
    WLAN/显示和亮度 存在）→ 重放冻结套件（start 步已剥除）→ 探索爆发

前台核验失败 → 本周期记 error 并跳过重放（**绝不在错误的前台上空转**）。
增量落盘：每周期写 cycles.jsonl（B11 的教训）。

用法::

    python tools/b15_app_stability.py --target 127.0.0.1:5555 \\
        --bundle com.huawei.hmos.settings --suite-dir tools/_out/b15_suite \\
        --cycles 90 --out tools/_out/b15_run \\
        --fold-instance "Mate X7" --fold-every 10

先 --cycles 1 自测，再上 2 小时全量。判据见项目内部 B15 执行设计（仓库外）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.action import load_case                          # noqa: E402
from ohauto.driver import Driver                             # noqa: E402
from ohauto.explorer import Budget, Explorer                 # noqa: E402
from ohauto.hdc import Hdc                                   # noqa: E402
from ohauto.runner import Runner                             # noqa: E402

#: 设置主页的标记文本（出现任一即认定「设置在前台」）。子页面不算——
#: 每周期从主页开始，保证周期间可比。
MAIN_MARKERS = ('WLAN', '显示和亮度', '声音和振动')
#: 桌面上设置图标的文案
ICON_TEXT = '设置'


def _log(msg: str) -> None:
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg), flush=True)


def _dump_texts_and_icon(hdc: Hdc) -> Tuple[set, Optional[Tuple[int, int]]]:
    """dump 一次，返回 (全部 text 集合, 桌面设置图标的点击中心)。"""
    hdc.shell('uitest dumpLayout -p /data/local/tmp/b15_fg.json', timeout=30)
    r = hdc.shell('cat /data/local/tmp/b15_fg.json', timeout=30)
    raw = (r.stdout or '').strip()
    texts: set = set()
    icon: Optional[Tuple[int, int]] = None
    if not raw:
        return texts, icon
    try:
        d = json.loads(raw)
    except ValueError:
        return texts, icon

    path: List[dict] = []
    found: List[Optional[Tuple[int, int]]] = [None]

    def walk(n: dict) -> None:
        a = n.get('attributes') or {}
        txt = a.get('text') or ''
        if txt:
            texts.add(txt)
        path.append(n)
        if txt == ICON_TEXT and found[0] is None:
            # 从文本节点向上找第一个可点击祖先（图标热区在容器上，
            # 文本节点自身 clickable=false，点它无效——实测踩过）
            for anc in reversed(path):
                aa = anc.get('attributes') or {}
                if str(aa.get('clickable')).lower() == 'true':
                    m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]',
                                 aa.get('bounds') or '')
                    if m:
                        found[0] = ((int(m.group(1)) + int(m.group(3))) // 2,
                                    (int(m.group(2)) + int(m.group(4))) // 2)
                    break
        for c in (n.get('children') or []):
            walk(c)
        path.pop()

    walk(d)
    return texts, found[0]


def _ensure_settings_foreground(hdc: Hdc, attempts: int = 2,
                                start_cmd: Optional[str] = None) -> bool:
    """验证主页标记；不在则拉起。

    两条拉起路径：`start_cmd`（真机型：应用有 MainAbility，aa start 可靠）
    或图标点击（模拟器 hmos 变体：无 MainAbility，只能点桌面图标）。
    验证一律以主页标记为准（WLAN 等）。"""
    for attempt in range(1, attempts + 1):
        if start_cmd:
            hdc.shell(start_cmd, timeout=30)
            # 冷启动时间不定（真机实测 6s+），逐次加长等待；
            # 重试**不再重复 force-stop**——那会把刚拉起的应用又杀掉，
            # 核验永远追不上冷启动（首测 4s×2 次就是这个死循环）。
            time.sleep(4 + 4 * attempt)
        texts, icon = _dump_texts_and_icon(hdc)
        if any(m in texts for m in MAIN_MARKERS):
            return True                      # 已在设置主页（如上一周期残留）
        if icon is None:
            _log('前台核验 %d/%d：未见设置主页标记，也无设置图标可点'
                 % (attempt, attempts))
            continue
        hdc.shell('uitest uiInput click %d %d' % icon, timeout=20)
        time.sleep(4)
        texts, _ = _dump_texts_and_icon(hdc)
        if any(m in texts for m in MAIN_MARKERS):
            return True
        _log('前台核验 %d/%d：点击后仍未见设置主页标记' % (attempt, attempts))
    return False


def _strip_start_steps(case: Dict[str, Any]) -> Dict[str, Any]:
    """剥掉 `start` 步——它的隐式拉起不可靠且会 force-stop 掉刚核验的前台。
    前台由编排器的核验流程负责。"""
    case['steps'] = [st for st in case.get('steps', []) if 'start' not in st]
    return case


def _fold(hdc_target: str, instance: str, state: str) -> bool:
    try:
        from tools import emulator_cli
        r = emulator_cli.set_folded_state(instance, state)
        return r.get('rc') == 0
    except Exception as e:                                  # noqa: BLE001
        _log('折叠切换失败: %s: %s' % (type(e).__name__, e))
        return False


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description='B15 应用长稳编排器 v2（前台核验版）')
    ap.add_argument('--target', required=True, help='模拟器 hdc 目标')
    ap.add_argument('--bundle', required=True)
    ap.add_argument('--suite-dir', required=True)
    ap.add_argument('--cycles', type=int, default=90)
    ap.add_argument('--out', default='tools/_out/b15_run')
    ap.add_argument('--fold-instance', default=None)
    ap.add_argument('--fold-every', type=int, default=10)
    ap.add_argument('--fold-states', default='close,open')
    ap.add_argument('--start-cmd', default=None,
                    help='真机型显式拉起命令（aa start ...），缺省走图标点击路径')
    args = ap.parse_args(argv)

    cases = sorted(glob.glob(os.path.join(args.suite_dir, '*.yaml')))
    if not cases:
        print('冻结套件为空: %s' % args.suite_dir)
    os.makedirs(args.out, exist_ok=True)
    cycles_path = os.path.join(args.out, 'cycles.jsonl')

    runner = Runner(continue_on_fail=False, verbose=False)
    totals: Dict[str, int] = {'steps': 0, 'passed': 0, 'failed': 0,
                              'cycles': 0, 'fg_fail': 0}
    t0 = time.time()
    fold_queue = [s.strip() for s in args.fold_states.split(',') if s.strip()]
    hdc = Hdc(target=args.target)

    for cycle in range(1, args.cycles + 1):
        perturb = bool(args.fold_instance and args.fold_every
                       and cycle % args.fold_every == 0)
        t_c = time.time()
        rec: Dict[str, Any] = {'cycle': cycle,
                               'ts': datetime.now().isoformat(timespec='seconds'),
                               'perturb': bool(perturb), 'fold': None}

        # ---- 折叠扰动（在桌面态切换，切完再开设置）
        if perturb:
            state = fold_queue[(cycle // max(1, args.fold_every) - 1)
                               % len(fold_queue)]
            rec['fold'] = {'state': state,
                           'cmd_ok': _fold(args.target, args.fold_instance,
                                           state)}
            _log('周期 %d：折叠扰动 → %s' % (cycle, state))
            time.sleep(6)

        # ---- 前台核验（失败绝不重放）
        if not _ensure_settings_foreground(hdc, start_cmd=args.start_cmd):
            totals['fg_fail'] += 1
            rec['error'] = '前台核验失败——未在设置主页，本周期跳过重放'
            rec['totals'] = dict(totals)
            with open(cycles_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            _log('周期 %d：❌ 前台核验失败，跳过' % cycle)
            continue
        rec['fg_verified'] = True

        # ---- replay 冻结套件（无 start 步，前台已核验）
        cycle_steps = cycle_pass = 0
        case_results = []
        for idx, cf in enumerate(cases):
            try:
                if idx > 0 and args.start_cmd:
                    # 用例间复位：force-stop + 显式拉起 → 每条用例都从设置
                    # 主页出发。**顺序依赖缺陷的根治**（首轮 90 败：轨迹-3
                    # 单测 4/4 过、跟在轨迹-1 后每周期必挂——其规格依赖
                    # 前序页面状态）。
                    hdc.shell(args.start_cmd, timeout=30)
                    time.sleep(4)
                d = Driver(bundle=args.bundle, ability='',
                           hdc=Hdc(target=args.target), verbose=False)
                res = runner.run_case(d, _strip_start_steps(load_case(cf)))
                name = os.path.basename(cf)
                case_results.append({'case': name, 'passed': res.passed,
                                     'total': res.total, 'ok': res.ok})
                cycle_steps += res.total
                cycle_pass += res.passed
            except Exception as e:              # noqa: BLE001
                case_results.append({'case': os.path.basename(cf),
                                     'error': '%s: %s' % (type(e).__name__, e)})
        rec['replay'] = {'steps': cycle_steps, 'passed': cycle_pass,
                         'cases': case_results}

        # ---- 探索爆发
        try:
            d = Driver(bundle=args.bundle, ability='',
                       hdc=Hdc(target=args.target),
                       artifact_dir=os.path.join(args.out, 'burst'),
                       verbose=False)
            ex = Explorer(d, artifact_dir=os.path.join(args.out, 'burst'),
                          verbose=False)
            g = ex.explore(2, Budget(max_pages=2, max_actions_per_page=2),
                           return_back=False)
            rec['burst'] = {'states': len(g.states),
                            'transitions': len(g.transitions)}
        except Exception as e:                  # noqa: BLE001
            rec['burst'] = {'error': '%s: %s' % (type(e).__name__, e)}

        rec['elapsed_min'] = round((time.time() - t_c) / 60, 1)
        totals['steps'] += cycle_steps
        totals['passed'] += cycle_pass
        totals['failed'] += cycle_steps - cycle_pass
        totals['cycles'] += 1
        rec['totals'] = dict(totals)
        with open(cycles_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        _log('周期 %d/%d: replay %d/%d 步 | 累计 %d 步（失败 %d）| %.1f 分钟'
             % (cycle, args.cycles, cycle_pass, cycle_steps,
                totals['steps'], totals['failed'], rec['elapsed_min']))

    ok = totals['failed'] == 0 and totals['fg_fail'] == 0
    _log('全部周期完成: %d 周期 / %d 步（失败 %d，前台核验失败 %d）→ %s'
         % (totals['cycles'], totals['steps'], totals['failed'],
            totals['fg_fail'], '达标' if ok else '未达标'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
