# -*- coding: utf-8 -*-
"""定位全链延迟的**分段**实测 —— 把「一次定位慢了」拆到该负责的那一段。

为什么要分段：KPI 只写「P50 1157ms > 200ms 未达标」会让人以为**引擎慢**，
而实测分解是「设备侧 `uitest dumpLayout` 1091ms（94%）+ cat 67ms + 宿主解析与匹配 1ms」——
宿主侧连 200ms 的 1% 都不到，瓶颈在设备的实现里，优化方向是**少 dump**
（页级缓存 / 事件驱动），不是把宿主代码改快。一句话讲不清，就分段实测给它看。

两段职责分开：

* `--rounds N`（**需真机**）：逐轮测三段耗时，落 JSONL；
* `--analyze <jsonl>`（**全离线**）：读 JSONL 出 p50/p95 与占比，可复算可归档。

三段为什么这么切：`dump_layout` 是设备侧导出（含 hdc 往返 + 设备内部遍历），
`cat` 是把导出内容拉回宿主，`host` 才是我们自己跑的解析 + 匹配。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_TREE = '/data/local/tmp/ohauto_layout.json'
SEGMENTS = ('device', 'cat', 'host')


# ---------------------------------------------------------------- 统计（离线）

def percentile(values: Sequence[float], q: float) -> float:
    """最近秩百分位（q 取 0~100）。样本量小的时候比插值法好讲：
    它只会给出**真实出现过的**那个值。"""
    if not values:
        return 0.0
    if not 0 < q <= 100:
        raise ValueError(f'百分位必须在 (0, 100] 内，收到 {q}')
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(q / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def share(part: float, whole: float) -> float:
    """占比（0~1）。**分母为 0 时返回 0 而不是抛** —— 一次没跑成也要能出报告。"""
    return part / whole if whole else 0.0


def summarize(samples: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐轮样本压成一张表的数字。

    `samples` 每项形如 `{'device_ms': .., 'cat_ms': .., 'host_ms': ..}`；
    缺段按 0 计（某一段没测到不该让整份报告报废）。
    """
    rows = [s for s in samples if isinstance(s, dict)]
    cols = {seg: [float(s.get(f'{seg}_ms') or 0.0) for s in rows] for seg in SEGMENTS}
    totals = [sum(c[i] for c in cols.values()) for i in range(len(rows))]
    out: Dict[str, Any] = {'n': len(rows)}
    for seg in SEGMENTS:
        out[f'{seg}_p50'] = round(percentile(cols[seg], 50), 1)
        out[f'{seg}_p95'] = round(percentile(cols[seg], 95), 1)
    out['total_p50'] = round(percentile(totals, 50), 1)
    out['total_p95'] = round(percentile(totals, 95), 1)
    out['total_mean'] = round(statistics.fmean(totals), 1) if totals else 0.0
    out['device_share'] = round(share(out['device_p50'], out['total_p50']), 4)
    out['host_share'] = round(share(out['host_p50'], out['total_p50']), 4)
    return out


def load_jsonl(path: str) -> List[Dict[str, Any]]:
    """读逐轮 JSONL。空行与坏行跳过（现场采集中断过要能继续分析）。"""
    rows: List[Dict[str, Any]] = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


TARGET_MS = 200.0


def render(summary: Dict[str, Any]) -> str:
    """出人话报告 —— 结论先行，数字带口径。"""
    if not summary.get('n'):
        return '没有样本，跑 `--rounds N` 采集，或换一份 JSONL。'
    lines = [
        f'样本 {summary["n"]} 轮（真机实测，非模拟）',
        '',
        f'端到端 P50 {summary["total_p50"]:.0f}ms / P95 {summary["total_p95"]:.0f}ms'
        f'  —— KPI 阈值 {TARGET_MS:.0f}ms，'
        f'{"达标" if summary["total_p50"] <= TARGET_MS else "未达标"}',
        '',
        '| 段 | 它在干什么 | P50 | P95 | 占端到端 |',
        '|---|---|---|---|---|',
        f'| device | 设备侧导出控件树（含 hdc 往返） | {summary["device_p50"]:.0f}ms | '
        f'{summary["device_p95"]:.0f}ms | {summary["device_share"] * 100:.0f}% |',
        f'| cat | 把导出内容拉回宿主 | {summary["cat_p50"]:.0f}ms | '
        f'{summary["cat_p95"]:.0f}ms | '
        f'{share(summary["cat_p50"], summary["total_p50"]) * 100:.0f}% |',
        f'| host | 宿主解析 + 匹配（**我们自己的代码**） | {summary["host_p50"]:.0f}ms | '
        f'{summary["host_p95"]:.0f}ms | {summary["host_share"] * 100:.1f}% |',
        '',
        '口径：',
        '1. 阈值 200ms 是对**端到端**定的；宿主侧单独看是另一个量级，'
        '两者不能混着讲；',
        '2. device 段是设备实现里的固有开销 —— 优化方向是**少 dump**'
        '（页级缓存 / 事件驱动），不是把宿主代码改快；',
        '3. 数字必须带 `--rounds` 与设备标识，模拟器与真机不混用。',
    ]
    return '\n'.join(lines)


# ---------------------------------------------------------------- 采集（需真机）

def measure_round(hdc: Any, tree: str, matcher: Any = None) -> Dict[str, Any]:
    """测一轮三段耗时。需要真机在线，本函数不在此做设备预检（由调用方负责）。"""
    from ohauto.layout import flatten, parse_layout

    t0 = time.perf_counter()
    hdc.dump_layout(tree)
    t1 = time.perf_counter()
    text = hdc.shell(f'cat {tree}', check=True).stdout
    t2 = time.perf_counter()
    root = parse_layout(text)
    if matcher is not None:
        matcher.filter(flatten(root))
    t3 = time.perf_counter()
    return {'device_ms': round((t1 - t0) * 1000, 1),
            'cat_ms': round((t2 - t1) * 1000, 1),
            'host_ms': round((t3 - t2) * 1000, 1)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='定位全链延迟分段实测')
    ap.add_argument('--rounds', type=int, default=20, help='真机采集轮数（需设备在线）')
    ap.add_argument('--tree', default=DEFAULT_TREE, help='设备侧控件树路径')
    ap.add_argument('--jsonl', default='', help='采集输出（默认 _out/locate_latency.jsonl）')
    ap.add_argument('--analyze', default='', help='只分析既有 JSONL（全离线）')
    ap.add_argument('--target', default='', help='设备序列号（多设备时必填）')
    args = ap.parse_args(argv)

    if args.analyze:
        out = os.path.join(ROOT, '_out', 'locate_latency_report.json')
        summary = summarize(load_jsonl(args.analyze))
        print(render(summary))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f'\n汇总已写入 {out}')
        return 0

    from ohauto.hdc import Hdc
    hdc = Hdc(target=args.target or None)
    if not hdc.list_targets():
        print('设备连接：失败 —— 本工具需要真机/模拟器在线（见 doctor.py）', file=sys.stderr)
        return 2

    out = args.jsonl or os.path.join(ROOT, '_out', 'locate_latency.jsonl')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    samples: List[Dict[str, Any]] = []
    with open(out, 'w', encoding='utf-8') as f:
        for i in range(1, args.rounds + 1):
            row = measure_round(hdc, args.tree)
            samples.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()                                   # 中断也留已采到的
            print(f'  [{i}/{args.rounds}] device={row["device_ms"]}ms '
                  f'cat={row["cat_ms"]}ms host={row["host_ms"]}ms', flush=True)
    print()
    print(render(summarize(samples)))
    print(f'\n逐轮样本：{out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
