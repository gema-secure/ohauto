# -*- coding: utf-8 -*-
"""B15 应用长稳编排器 —— 混合套件在真实大应用上的 2 小时连续压力。

周期结构（每轮约 3 分钟）::

    replay    重放冻结套件（7 条已验证用例，~31 步）—— 延迟漂移的基准线
    burst     探索爆发（每周期全新 Explorer，2 页/2 动作）—— 真实压力源
    fold      （每 fold_every 轮一次）折叠态切换扰动 —— 重建压力
    settle    稳定等待，遥测由独立的 stability_telemetry 进程负责

判据与口径见 docs/ 下的「B15 应用长稳执行设计」。
**增量落盘**：每周期结束立刻写 cycles.jsonl 与 partial 摘要——
进程被杀只丢当前周期（B11 的教训）。

用法::

    python tools/b15_app_stability.py --target 127.0.0.1:5555 \\
        --bundle com.huawei.hmos.settings --suite-dir tools/_out/b15_suite \\
        --cycles 40 --out tools/_out/b15_run --fold-instance "Mate X7" --fold-every 10

先用 --cycles 1 自测，再上 2 小时全量。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.action import load_case                          # noqa: E402
from ohauto.driver import Driver                             # noqa: E402
from ohauto.explorer import Budget, Explorer                 # noqa: E402
from ohauto.hdc import Hdc                                   # noqa: E402
from ohauto.runner import Runner                             # noqa: E402


def _log(msg: str) -> None:
    print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), msg), flush=True)


def _fold(hdc_target: str, instance: str, state: str) -> bool:
    """折叠态切换（只作用于模拟器）。返回是否命令成功——**不等于切换生效**，
    有效性由下一周期采集的分辨率核对（verify_emulator 的教训）。"""
    try:
        from tools import emulator_cli
        r = emulator_cli.set_folded_state(instance, state)
        return r.get('rc') == 0
    except Exception as e:                                  # noqa: BLE001
        _log('折叠切换失败（不影响主流程）: %s: %s' % (type(e).__name__, e))
        return False


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description='B15 应用长稳编排器')
    ap.add_argument('--target', required=True, help='hdc 目标（模拟器 host:port）')
    ap.add_argument('--bundle', required=True)
    ap.add_argument('--suite-dir', required=True, help='冻结套件目录（*.yaml）')
    ap.add_argument('--cycles', type=int, default=40)
    ap.add_argument('--out', default='tools/_out/b15_run')
    ap.add_argument('--fold-instance', default=None,
                    help='模拟器实例名（给了才做折叠扰动）')
    ap.add_argument('--fold-every', type=int, default=10,
                    help='每 N 个周期做一次折叠切换（基准周期不受扰动）')
    ap.add_argument('--fold-states', default='close,open',
                    help='扰动时按序切换到的折叠态（扰动后回到 open）')
    args = ap.parse_args(argv)

    cases = sorted(glob.glob(os.path.join(args.suite_dir, '*.yaml')))
    if not cases:
        print('冻结套件为空: %s' % args.suite_dir)
        return 2
    os.makedirs(args.out, exist_ok=True)
    cycles_path = os.path.join(args.out, 'cycles.jsonl')

    runner = Runner(continue_on_fail=False, verbose=False)
    totals = {'steps': 0, 'passed': 0, 'failed': 0, 'cycles': 0}
    t0 = time.time()
    fold_queue = [s.strip() for s in args.fold_states.split(',') if s.strip()]

    for cycle in range(1, args.cycles + 1):
        perturb = (args.fold_instance and args.fold_every
                   and cycle % args.fold_every == 0)
        t_c = time.time()
        rec: Dict[str, Any] = {'cycle': cycle, 'ts': datetime.now().isoformat(timespec='seconds'),
                               'perturb': bool(perturb), 'cases': [], 'fold': None}

        # ---- 折叠扰动（先切走，周期末切回 open）
        if perturb:
            state = fold_queue[(cycle // max(1, args.fold_every) - 1)
                               % len(fold_queue)]
            ok = _fold(args.target, args.fold_instance, state)
            rec['fold'] = {'state': state, 'cmd_ok': ok}
            _log('周期 %d：折叠扰动 → %s' % (cycle, state))
            time.sleep(6)                    # 系统重绘
        else:
            _fold(args.target, args.fold_instance, 'open') if args.fold_instance \
                else None                     # 基准周期：确保回到 open（幂等）

        # ---- replay 冻结套件
        cycle_steps = cycle_pass = 0
        case_results = []
        for cf in cases:
            try:
                d = Driver(bundle=args.bundle, ability='',
                           hdc=Hdc(target=args.target), verbose=False)
                c = load_case(cf)
                res = runner.run_case(d, c)
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
            d = Driver(bundle=args.bundle, ability='', hdc=Hdc(target=args.target),
                       artifact_dir=os.path.join(args.out, 'burst'), verbose=False)
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
        _log('周期 %d/%d 完成: replay %d/%d 步 | 累计 %d 步 | %.1f 分钟'
             % (cycle, args.cycles, cycle_pass, cycle_steps,
                totals['steps'], rec['elapsed_min']))

    _log('全部周期完成: %d 周期 / %d 步（失败 %d）' %
         (totals['cycles'], totals['steps'], totals['failed']))
    return 0 if totals['failed'] == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
