# -*- coding: utf-8 -*-
"""诊断 L3 落空的原因：同类型候选里有没有「同分」的兄弟。

`_lv_path_type` 的纪律是：同 type 候选按打分取唯一最高分，两个同分即落空
（线索不够，宁可交给下一级）。本脚本把每个无标识控件的那组数打出来，
用来看落空到底是「线索真的不够」还是「线索没用上」。

    python probes/_diag_l3.py
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (DEFAULT_SAMPLES, ancestors, blind_controls,  # noqa: E402
                     control_key, flatten, iter_samples, pct,
                     print_table, spec_of, type_fingerprint)


def score(spec, node) -> float:
    """基线打分（与 `locator.py::_lv_path_type` 的 score() 一字对应）。"""
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


def candidates(spec, page):
    cands = flatten(page, only_visible=True)
    if spec.type_fp:
        return [n for n in cands if type_fingerprint(n) == spec.type_fp]
    if spec.target_type:
        return [n for n in cands if n.type == spec.target_type]
    return []


def probe(samples_dir: str, examples: int) -> int:
    buckets = {1: [0, 0], '>1': [0, 0]}          # 同分候选数 -> [总数, 命中数]
    keys = {}

    for name, page in iter_samples(samples_dir):
        shown = 0
        for node in blind_controls(page):
            spec = spec_of(node)
            cands = candidates(spec, page)
            if not cands:
                continue
            scores = [score(spec, n) for n in cands]
            top = max(scores)
            tied = sum(1 for v in scores if v == top)
            # 命中条件同 locate：唯一最高分，且分数必须大于 0
            hit = (tied == 1 and top > 0 and cands[scores.index(top)] is node)

            key = 1 if tied == 1 else '>1'
            buckets[key][0] += 1
            buckets[key][1] += int(hit)
            keys[spec.control_key] = keys.get(spec.control_key, 0) + 1

            if examples and shown < examples and tied > 1:
                shown += 1
                print(f'  {name:<14} type={node.type:<12} '
                      f'同型 {len(cands):>3} 个 / 最高分 {top:.1f} / 同分 {tied:>3} 个'
                      f' -> 落空')

    print()
    print_table(['同分候选数', '样本数', 'L3命中', '命中率'],
                [['同分 = 1', buckets[1][0], buckets[1][1],
                  pct(buckets[1][1], buckets[1][0])],
                 ['同分 > 1', buckets['>1'][0], buckets['>1'][1],
                  pct(buckets['>1'][1], buckets['>1'][0])]],
                aligns=['<', '>', '>', '>'])
    print(f'control_key 去重后仅 {len(keys)} 个不同值 —— 同类型同尺寸的无标识'
          f'兄弟会算出同一个 key（该 key 刻意不含坐标）。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--samples', default=DEFAULT_SAMPLES)
    ap.add_argument('--examples', type=int, default=4,
                    help='每个样本打印几条落空明细（0 = 不打印）')
    args = ap.parse_args()
    if not os.path.isdir(args.samples):
        print(f'样本目录不存在：{args.samples}')
        return 2
    return probe(args.samples, args.examples)


if __name__ == '__main__':
    raise SystemExit(main())
