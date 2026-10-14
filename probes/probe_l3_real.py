# -*- coding: utf-8 -*-
"""L3（层级路径 + 类型）在「无标识」可交互控件上的命中率。

用途：换应用做横向对比时，量化「用例定位只能靠层级路径」那一级到底顶不顶用。

    python probes/probe_l3_real.py
    python probes/probe_l3_real.py --samples tools/_out/multiapp_survey

口径与判据见 `_common`：命中 = `_lv_path_type` 返回的节点就是目标节点本身
（同 type 候选里打分必须**唯一最高**，两个同分即落空 —— 这是定位器的既有纪律）。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (DEFAULT_SAMPLES, LocatorManager, blind_controls,  # noqa: E402
                     iter_samples, pct, print_table, spec_of)


def probe(samples_dir: str) -> int:
    lm = LocatorManager()      # 无视觉通道，与「L4 未配 key」的真机情形一致
    rows, tot = [], {'inter': 0, 'wid': 0, 'wtxt': 0, 'blind': 0, 'hit': 0}
    levels: dict = {}

    for name, page in iter_samples(samples_dir):
        inter = [n for n in page.walk() if n.is_interactive()]
        wid = [n for n in inter if (n.id or '').strip()]
        wtxt = [n for n in inter if (n.text or '').strip()]
        blind = blind_controls(page)

        hit = 0
        for node in blind:
            got, level = lm._lv_path_type(spec_of(node), page)
            if got is node:
                hit += 1
                levels[level or '(空)'] = levels.get(level or '(空)', 0) + 1

        rows.append([name, len(inter), len(wid), len(wtxt), len(blind),
                     f'{hit}/{len(blind)}', pct(hit, len(blind))])
        tot['inter'] += len(inter)
        tot['wid'] += len(wid)
        tot['wtxt'] += len(wtxt)
        tot['blind'] += len(blind)
        tot['hit'] += hit

    print(f'样本目录：{samples_dir}')
    print_table(['样本', '可交互', '有id', '有文案', '无标识', 'L3命中', '命中率'],
                rows, aligns=['<', '>', '>', '>', '>', '>', '>'])
    print(f'合计：可交互 {tot["inter"]} ｜ 无标识 {tot["blind"]} ｜ '
          f'L3 命中 {tot["hit"]}/{tot["blind"]} = {pct(tot["hit"], tot["blind"])}')
    if levels:
        print('命中级别分布：', ', '.join(f'{k} x{v}' for k, v in
                                          sorted(levels.items(), key=lambda kv: -kv[1])))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--samples', default=DEFAULT_SAMPLES,
                    help='控件树样本目录（默认 datasets/gallery_13app）')
    args = ap.parse_args()
    if not os.path.isdir(args.samples):
        print(f'样本目录不存在：{args.samples}')
        return 2
    return probe(args.samples)


if __name__ == '__main__':
    raise SystemExit(main())
