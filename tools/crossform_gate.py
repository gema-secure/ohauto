# -*- coding: utf-8 -*-
"""跨形态差异回归门禁 —— 改动前后各跑一次 crossform，门禁判「真的修好了没有」。

★ 闭环的最后一环
----------------

检出（crossform_run）→ 聚类（crossform_cluster）→ 复现（defect_repro）
之后，开发者修完代码，重跑一次 crossform_run，然后本工具回答唯一的问题：

    高危差异清零了吗？有没有修出新的问题？

判定规则（就三条，刻意简单）
--------------------------

1. **页面指纹必须一致**：前后两次采集的基准页 `structural_key` 不一致
   → 直接拒绝比较（比较两个不同页面等于谎报「修复见效」）。
   `--force` 可以跳过，但门禁结论会打上「指纹不一致」的标记；
2. **通过**：after 的 MISSING / OVERFLOW 计数都 ≤ before，
   且高危差异（cluster.json 里 severity=HIGH 的簇）≤ `--max-high`（默认 0）；
3. **不通过**：列出「恶化项」（某类计数上升）与「残留高危簇」，
   每条都指向 cluster/复现包，不输出一个光秃秃的 FAIL。

用法
----

    python tools/crossform_gate.py --before <run_dir> --after <run_dir>
    python tools/crossform_gate.py --before A --after B --max-high 2

退出码：0 = 通过；1 = 不通过；2 = 输入不齐 / 指纹不一致被拒绝。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.explorer import build_page_signature                # noqa: E402
from ohauto.layout import parse_layout                          # noqa: E402

KINDS = ('MISSING', 'OVERFLOW', 'OUT_OF_SCREEN', 'UNREACHABLE')


def _load(run_dir: str) -> Optional[Dict]:
    p = os.path.join(run_dir, 'crossform.json')
    if not os.path.isfile(p):
        return None
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def _sig(run_dir: str) -> Optional[str]:
    for d in sorted(os.listdir(run_dir)):
        p = os.path.join(run_dir, d)
        if d.startswith('baseline_') and os.path.isfile(os.path.join(p, 'layout.json')):
            root = parse_layout(os.path.join(p, 'layout.json'))
            return build_page_signature(root).structural_key
    return None


def _high_clusters(run_dir: str) -> int:
    p = os.path.join(run_dir, 'cluster.json')
    if not os.path.isfile(p):
        return -1                                               # -1 = 没跑过聚类
    data = json.load(open(p, encoding='utf-8'))
    return sum(1 for c in data.get('clusters', []) if c.get('severity') == 'HIGH')


def run(before: str, after: str, max_high: int, force: bool) -> int:
    b, a = _load(before), _load(after)
    if b is None or a is None:
        print('✗ 两个目录都要有 crossform_run.py 的产物（crossform.json）')
        return 2

    sb, sa = _sig(before), _sig(after)
    fingerprint_ok = (sb is not None and sb == sa)
    if not fingerprint_ok and not force:
        print('✗ 前后两次采集的页面指纹不一致 —— 比较两个不同页面等于谎报修复见效。')
        print('  请确认修复后重跑的是同一页面；确要比较请加 --force。')
        return 2

    rows, regressions = [], []
    for k in KINDS:
        cb, ca = b['counts'].get(k, 0), a['counts'].get(k, 0)
        rows.append((k, cb, ca, '⚠️ 恶化' if ca > cb else '✓'))
        if ca > cb:
            regressions.append(f'{k}: {cb} → {ca}')
    hb = _high_clusters(before)
    ha = _high_clusters(after)

    high_ok = (ha <= max_high) if ha >= 0 else None
    passed = (not regressions) and (high_ok is not False)

    print('=' * 60)
    print('  跨形态差异 · 回归门禁')
    print('=' * 60)
    print(f'  页面指纹一致: {"✓" if fingerprint_ok else "✗（--force 跳过）"}')
    print(f'  {"类型":<14}{"before":>8}{"after":>8}')
    for k, cb, ca, mark in rows:
        print(f'  {k:<14}{cb:>8}{ca:>8}  {mark}')
    if hb >= 0 and ha >= 0:
        print(f'  高危根因簇    {hb:>8}{ha:>8}   （阈值 ≤{max_high}）')
    elif ha < 0:
        print('  ⚠️ after 目录没有 cluster.json —— 先跑 crossform_cluster.py 再过门禁')
    print('-' * 60)
    verdict = ('✅ 通过：无恶化项，高危簇未超阈值'
               if passed else '❌ 不通过')
    if passed and ha > 0:
        verdict += f'（残留 {ha} 个，阈值 ≤{max_high}）'
    print(f'  结论：{verdict}')
    if regressions:
        print('  恶化项：' + '；'.join(regressions))
    if ha > 0:
        print(f'  残留高危簇 {ha} 个 —— 复现步骤见 after 目录 repro.md，'
              '根因见 cluster.md')
    print('=' * 60)

    out = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'before': before, 'after': after,
        'fingerprint_ok': fingerprint_ok, 'forced': force and not fingerprint_ok,
        'counts': {k: {'before': b['counts'].get(k, 0), 'after': a['counts'].get(k, 0)}
                   for k in KINDS},
        'high_clusters': {'before': hb, 'after': ha, 'threshold': max_high},
        'regressions': regressions, 'passed': passed,
    }
    for d in (after,):
        with open(os.path.join(d, 'gate.json'), 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return 0 if passed else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--before', required=True, help='修复前的 crossform 运行目录')
    ap.add_argument('--after', required=True, help='修复后的 crossform 运行目录')
    ap.add_argument('--max-high', type=int, default=0, help='高危根因簇阈值（默认 0）')
    ap.add_argument('--force', action='store_true',
                    help='页面指纹不一致时仍强行比较（结论会带标记）')
    a = ap.parse_args()
    sys.exit(run(a.before, a.after, a.max_high, a.force))


if __name__ == '__main__':
    main()
