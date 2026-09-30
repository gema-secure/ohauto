# -*- coding: utf-8 -*-
"""应用长稳遥测采样器（B15）—— 与跑测进程并行，每 60 秒采一轮设备侧资源。

用法::

    # 采样（与 run_suite 并行跑，直到 Ctrl+C 或 --duration 到期）
    python tools/stability_telemetry.py --target 127.0.0.1:5555 \\
        --bundle com.huawei.hmos.settings --out tools/_out/b15/telemetry.jsonl

    # 采样一轮立即退出（自检用）
    python tools/stability_telemetry.py --once --target ...

    # 分析：PSS 斜率 / 负载趋势 / 缺样本统计
    python tools/stability_telemetry.py --analyze tools/_out/b15/telemetry.jsonl --strict

设计约定（见 docs/ 下的「B15 应用长稳执行设计」）：
- 采样失败**记 None + 警告字段**，绝不编造、绝不中断——「缺样本 ≠ 正常」；
- 单次采样 < 2 秒（60 秒间隔下占空比 ~3%，不影响被测跑测的延迟口径）；
- 零依赖：hdc 交互复用 `ohauto.hdc`，宿主内存解析走 tasklist CSV。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc                                    # noqa: E402

DEFAULT_INTERVAL = 60
#: PSS 斜率判据：后半程均值 / 前半程均值，涨幅超过它 → 报泄漏嫌疑。
#: 首轮校准前是**暂行值**，首轮长稳跑完按实测分布冻结（先测再定）。
PSS_SLOPE_THRESHOLD = 30.0


# ---------------------------------------------------------------- 采集

def _device_sample(hdc: Hdc, bundle: str) -> Dict[str, Any]:
    """一次设备侧采样：被测应用 PSS / 负载 / 存活。失败记 None + 警告。"""
    s: Dict[str, Any] = {'pss_kb': None, 'load1': None, 'alive': None,
                         'pid': None, 'warn': []}
    try:
        r = hdc.shell('pidof %s' % bundle, timeout=15)
        pids = (r.stdout or '').split()
        if not pids:
            s['alive'] = False
            s['warn'].append('被测应用进程不在运行')
            return s
        s['alive'] = True
        s['pid'] = pids[0]
    except Exception as e:
        s['warn'].append('pidof 失败: %s: %s' % (type(e).__name__, e))
        return s

    try:
        r = hdc.shell('hidumper --mem %s' % s['pid'], timeout=30)
        # 解析格式（真机 DAYU200 实测定稿）：表头行含 `Pss  Shared ...`，
        # **数值在紧随其后的 `Total 41322 ...` 行**——表头行本身没有数字。
        # 规则：见到 Pss 表头后，扫其后首个 Total 行，取第一个数字 = PSS 合计。
        pss = None
        seen_header = False
        for line in (r.stdout or '').splitlines():
            up = line.upper()
            if 'PSS' in up:
                seen_header = True
                continue
            # 表格有**两行** Total 开头：先是单位行（Total Clean Dirty...，无数字），
            # 后才是数值行（Total 41322 ...）——只在拿到数字时才停。
            if seen_header and re.match(r'\s*Total\b', line, re.IGNORECASE):
                nums = re.findall(r'(\d+)', line)
                if nums:
                    pss = int(nums[0])
                    break
        if pss is None:
            s['warn'].append('hidumper 输出里没解析到 PSS（表头+Total 结构缺失）')
        else:
            s['pss_kb'] = pss
    except Exception as e:
        s['warn'].append('hidumper 失败: %s: %s' % (type(e).__name__, e))

    try:
        r = hdc.shell('cat /proc/loadavg', timeout=10)
        m = re.match(r'\s*([0-9.]+)', r.stdout or '')
        if m:
            s['load1'] = float(m.group(1))
    except Exception as e:
        s['warn'].append('loadavg 失败: %s: %s' % (type(e).__name__, e))
    return s


def _host_sample(host_pid: Optional[int]) -> Dict[str, Any]:
    """宿主跑测进程的内存（tasklist CSV，Windows）。host_pid 空则跳过。"""
    s: Dict[str, Any] = {'host_mem_kb': None}
    if not host_pid:
        return s
    try:
        r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % host_pid,
                            '/FO', 'CSV', '/NH'],
                           capture_output=True, text=True, timeout=20)
        m = re.search(r'"\s?([\d,\s]+)\s?K"', r.stdout or '')
        if m:
            s['host_mem_kb'] = int(m.group(1).replace(',', ''))
    except Exception as e:
        s['warn'] = ['tasklist 失败: %s' % e]
    return s


def take_sample(hdc: Hdc, bundle: str, host_pid: Optional[int],
                rounds_done: Optional[int] = None) -> Dict[str, Any]:
    s = {'ts': datetime.now().isoformat(timespec='seconds')}
    s.update(_device_sample(hdc, bundle))
    s.update(_host_sample(host_pid))
    if rounds_done is not None:
        s['rounds_done'] = rounds_done
    return s


# ---------------------------------------------------------------- 分析

def analyze(path: str, strict: bool = False) -> int:
    samples: List[Dict[str, Any]] = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    samples.append(json.loads(line))
                except ValueError:
                    continue          # 坏行跳过（写侧是 jsonl，正常不会坏）
    pss = [s['pss_kb'] for s in samples if s.get('pss_kb') is not None]
    missing = sum(1 for s in samples if s.get('pss_kb') is None)
    loads = [s['load1'] for s in samples if s.get('load1') is not None]

    print('=' * 60)
    print('  遥测分析（%d 个采样点）' % len(samples))
    if len(pss) < 4:
        print('  ❌ 有效 PSS 样本不足 4 个（缺失 %d）——无法判斜率' % missing)
        return 1
    half = len(pss) // 2
    m1 = sum(pss[:half]) / half
    m2 = sum(pss[half:]) / (len(pss) - half)
    pct = (m2 - m1) / m1 * 100 if m1 else 0.0
    print('  被测应用 PSS: 前半均值 %.0f KB → 后半均值 %.0f KB'
          % (m1, m2))
    print('  斜率: %+.2f%%（暂行阈值 ±%.0f%%，首轮跑完校准冻结）'
          % (pct, PSS_SLOPE_THRESHOLD))
    print('  PSS 范围: %d ~ %d KB ｜ 缺失采样: %d' % (min(pss), max(pss), missing))
    if loads:
        print('  设备负载1: 均值 %.2f ｜ 峰值 %.2f' % (sum(loads) / len(loads),
                                                     max(loads)))
    leak = pct > PSS_SLOPE_THRESHOLD
    if leak:
        print('  ❌ PSS 持续上扬超阈值 —— 报泄漏嫌疑，按采样点定位')
    else:
        print('  ✅ PSS 无持续上扬 —— 泄漏判据通过')
    if strict and leak:
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
            except Exception:
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
