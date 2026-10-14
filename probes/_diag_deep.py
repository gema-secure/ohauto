# -*- coding: utf-8 -*-
"""诊断：同分的兄弟能不能用子树文案（text_deep）区分开。

`_diag_l3.py` 把落空定位到「同分候选 > 1」。本脚本进一步回答：那些同分组
的 `text_deep` 到底是不是互不相同 —— 如果是，说明线索本来就在手上。

与 `_diag_l3.py` 一致，这里的打分公式**刻意保留未计入子树文案的版本**，
否则同分现象已被主实现消掉，就无从诊断这条线索本身。

    python probes/_diag_deep.py
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (DEFAULT_SAMPLES, ancestors, blind_controls,   # noqa: E402
                     control_key, flatten, iter_samples, pct,
                     print_table, spec_of, type_fingerprint)


def _score(spec, node) -> float:
    s = 0.0
    if spec.control_key and control_key(node) == spec.control_key:
        s += 2.0
    parent_types = tuple(p.type for p in ancestors(node))
    if spec.parent_types and parent_types == spec.parent_types:
        s += 1.5
    elif spec.parent_types:
        tail = parent_types[-len(spec.parent_types):]
        if tail == spec.parent_types:
            s += 0.8
    if spec.text and node.text == spec.text:
        s += 0.5
    if spec.descr and node.descr == spec.descr:
        s += 0.5
    return s


def _candidates(spec, page):
    cands = flatten(page, only_visible=True)
    if spec.type_fp:
        return [n for n in cands if type_fingerprint(n) == spec.type_fp]
    if spec.target_type:
        return [n for n in cands if n.type == spec.target_type]
    return []


def _deep(node) -> str:
    return (getattr(node, 'text_deep', '') or '').strip()


def probe(samples_dir: str, examples: int) -> int:
    ties = 0            # 同分 > 1 的控件数
    resolvable = 0      # 其中「子树文案非空且组内唯一」的
    empty = 0           # 子树文案本身为空的（确实不可区分）
    shown = 0

    for name, page in iter_samples(samples_dir):
        for node in blind_controls(page):
            spec = spec_of(node)
            cands = _candidates(spec, page)
            if len(cands) < 2:
                continue
            scores = [_score(spec, n) for n in cands]
            top = max(scores)
            group = [n for n, v in zip(cands, scores) if v == top]
            if len(group) < 2:
                continue

            ties += 1
            labels = [_deep(n) for n in group]
            mine = _deep(node)
            ok = bool(mine) and labels.count(mine) == 1
            resolvable += int(ok)
            empty += int(not mine)

            if examples and shown < examples:
                shown += 1
                distinct = len(set(labels))
                preview = ' / '.join((x or '(空)')[:10] for x in labels[:4])
                print(f'  {name:<14} {node.type:<12} 同分组 {len(group):>2} 个，'
                      f'子树文案 {distinct} 种不同：{preview}')

    print()
    print_table(['同分组', '总数', '子树文案可区分', '其中本组文案为空'],
                [['同分 > 1', ties, f'{resolvable} ({pct(resolvable, ties)})',
                  empty]],
                aligns=['<', '>', '>', '>'])
    if ties:
        print(f'结论：{pct(resolvable, ties)} 的同分组可靠子树文案区分开；'
              f'剩下 {ties - resolvable} 个（多为文案本身为空）继续落到下一级，'
              f'行为正确。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--samples', default=DEFAULT_SAMPLES)
    ap.add_argument('--examples', type=int, default=5)
    args = ap.parse_args()
    if not os.path.isdir(args.samples):
        print(f'样本目录不存在：{args.samples}')
        return 2
    return probe(args.samples, args.examples)


if __name__ == '__main__':
    raise SystemExit(main())
