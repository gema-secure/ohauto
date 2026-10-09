# -*- coding: utf-8 -*-
"""应用长稳遥测采样器（B15）—— CLI 壳。

采样与分析原语已收编进 `ohauto/perf.py`（docs/发展规划与改进建议.md §2 2C），
本文件只保留命令行入口：参数解析、采样循环、结论打印。周期采样与趋势曲线
要在进程内消费（并入报告）时直接用 `ohauto.signals.PerfChannel`。

用法::

    # 采样（与 run_suite 并行跑，直到 Ctrl+C 或 --duration 到期）
    python tools/stability_telemetry.py --target 127.0.0.1:5555 \\
        --bundle com.huawei.hmos.settings --out tools/_out/b15/telemetry.jsonl

    # 采样一轮立即退出（自检用）
    python tools/stability_telemetry.py --once --target ...

    # 分析：PSS 斜率 / 负载趋势 / 缺样本统计
    python tools/stability_telemetry.py --analyze tools/_out/b15/telemetry.jsonl --strict

设计约定（继承自 ohauto/perf.py，即原「B15 应用长稳执行设计」）：
- 采样失败**记 None + 警告字段**，绝不编造、绝不中断——「缺样本 ≠ 正常」；
- 单次采样 < 2 秒（60 秒间隔下占空比 ~3%，不影响被测跑测的延迟口径）；
- 零依赖：hdc 交互复用 `ohauto.hdc`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc                                    # noqa: E402
from ohauto.perf import (analyze_samples, take_sample,        # noqa: E402
                         MIN_PSS_SAMPLES, PSS_SLOPE_THRESHOLD)

DEFAULT_INTERVAL = 60


# ---------------------------------------------------------------- 分析

def analyze(path: str, strict: bool = False) -> int:
    """读 jsonl、出斜率结论。判据计算在 `ohauto.perf.analyze_samples`。"""
    samples: List[dict] = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    samples.append(json.loads(line))
                except ValueError:
                    continue          # 坏行跳过（写侧是 jsonl，正常不会坏）
    a = analyze_samples(samples)

    print('=' * 60)
    print('  遥测分析（%d 个采样点）' % a['samples'])
    if a['insufficient']:
        print('  ❌ 有效 PSS 样本不足 %d 个（缺失 %d）——无法判斜率'
              % (MIN_PSS_SAMPLES, a['pss_missing']))
        return 1
    print('  被测应用 PSS: 前半均值 %.0f KB → 后半均值 %.0f KB'
          % (a['pss_mean_first'], a['pss_mean_second']))
    print('  斜率: %+.2f%%（暂行阈值 ±%.0f%%，首轮跑完校准冻结）'
          % (a['slope_pct'], PSS_SLOPE_THRESHOLD))
    print('  PSS 范围: %d ~ %d KB ｜ 缺失采样: %d'
          % (a['pss_min'], a['pss_max'], a['pss_missing']))
    if a['load_mean'] is not None:
        print('  设备负载1: 均值 %.2f ｜ 峰值 %.2f'
              % (a['load_mean'], a['load_peak']))
    if a['leak_suspect']:
        print('  ❌ PSS 持续上扬超阈值 —— 报泄漏嫌疑，按采样点定位')
    else:
        print('  ✅ PSS 无持续上扬 —— 泄漏判据通过')
    if strict and a['leak_suspect']:
        return 1
    return 0


# ---------------------------------------------------------------- 驱动

def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description='应用长稳遥测采样器（B15）')
    # target/bundle 只在**采样模式**必填（分析模式不需要设备）——
    # 不能用 argparse 的 required=True：它会在解析阶段强制校验，
    # 把 `--analyze` 单独调用也拦下来（自检踩过）。
    ap.add_argument('--target', default=None, help='hdc 目标（真机串号或模拟器 host:port）')
    ap.add_argument('--bundle', default=None, help='被测应用包名')
    ap.add_argument('--out', default='tools/_out/b15_telemetry.jsonl',
                    help='输出 jsonl（追加写）')
    ap.add_argument('--interval', type=int, default=DEFAULT_INTERVAL)
    ap.add_argument('--duration', type=int, default=0,
                    help='采样时长秒数；0 = 一直跑到 Ctrl+C')
    ap.add_argument('--host-pid', type=int, default=None,
                    help='跑测进程 pid（可选，采宿主内存）')
    ap.add_argument('--partial', default=None,
                    help='run_suite 的 partial 报告路径（可选，联动 rounds_done）')
    ap.add_argument('--once', action='store_true', help='只采一轮（自检）')
    ap.add_argument('--analyze', metavar='JSONL', default=None,
                    help='分析模式：对已有 jsonl 出斜率结论')
    args = ap.parse_args(argv)

    if args.analyze:
        return analyze(args.analyze, strict=False)

    if not args.target or not args.bundle:
        ap.error('采样模式需要 --target 与 --bundle（--analyze 分析模式不需要）')

    hdc = Hdc(target=args.target)
    print('遥测启动: target=%s bundle=%s interval=%ds → %s'
          % (args.target, args.bundle, args.interval, args.out))
    n = 0
    t0 = time.time()
    while True:
        rounds = None
        if args.partial and os.path.isfile(args.partial):
            try:
                # cycles 文件是 **JSONL**（每周期一行）——整文件 json.load 会炸，
                # 只读最后一行的 totals.cycles（B15 首跑踩过）。
                with open(args.partial, encoding='utf-8') as f:
                    last = ''
                    for line in f:
                        if line.strip():
                            last = line
                if last:
                    d = json.loads(last)
                    totals = d.get('totals') or {}
                    rounds = totals.get('cycles')
            except (OSError, ValueError):
                pass
        s = take_sample(hdc, args.bundle, args.host_pid, rounds)
        with open(args.out, 'a', encoding='utf-8') as f:
            f.write(json.dumps(s, ensure_ascii=False) + '\n')
        n += 1
        warn = ('  ⚠️ ' + '; '.join(s.get('warn', []))) if s.get('warn') else ''
        print('[%s] #%d PSS=%s load=%s%s'
              % (s['ts'], n, s.get('pss_kb'), s.get('load1'), warn),
              flush=True)
        if args.once:
            return 0
        if args.duration and (time.time() - t0) >= args.duration:
            print('采样时长到期（%ds，共 %d 轮）' % (args.duration, n))
            return 0
        time.sleep(max(1, args.interval))


if __name__ == '__main__':
    sys.exit(main())
