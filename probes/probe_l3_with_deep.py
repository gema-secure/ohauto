# -*- coding: utf-8 -*-
"""把子树文案（text_deep）计进 L3 打分的前后对照。

背景：真机上可交互容器自身常无 id 也无文案，文案落在子节点上（`text_deep`）。
`control_key` 刻意不含坐标，于是同类型、同尺寸的无标识兄弟会算出同一个 key，
在 `_lv_path_type` 里打成同分，按「唯一最高分才认」的纪律一律落空。
`text_deep` 恰好能区分它们 —— 而这条线索在自愈路径上已经在用，主打分路径没用。

做法：用**子类覆盖** `_lv_path_type`，不修改仓库里的 `locator.py`，
在同一批控件上跑前后两次，差值只来自那一项打分。

    python probes/probe_l3_with_deep.py
    python probes/probe_l3_with_deep.py --subset app_settings,contacts
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (DEFAULT_SAMPLES, LocatorManager, type_fingerprint,  # noqa: E402
                     blind_controls, control_key, flatten, iter_samples,
                     pct, print_table, spec_of)

#: 建议权重：夹在 control_key(2.0) 之下、text/descr(0.5) 之上
DEEP_WEIGHT = 1.0

#: 典型的「无标识重灾区」应用，用于子集汇总
DEFAULT_SUBSET = ('app_settings', 'contacts', 'app_note', 'app_photos',
                  'mms', 'certmanager', 'camera')


class L3WithDeep(LocatorManager):
    """在 `_lv_path_type` 的 score() 里补一项 text_deep 打分。"""

    def _lv_path_type(self, spec, page):
        cands = flatten(page, only_visible=True)
        if spec.type_fp:
            same_type = [n for n in cands if type_fingerprint(n) == spec.type_fp]
        elif spec.target_type:
            same_type = [n for n in cands if n.type == spec.target_type]
        else:
            return None, ''
        if not same_type:
            return None, ''

        def score(n):
            s = 0.0
            if spec.control_key and control_key(n) == spec.control_key:
                s += 2.0
            parent_types = tuple(p.type for p in self._ancestors(n))
            if spec.parent_types and parent_types == spec.parent_types:
                s += 1.5
            elif spec.parent_types:
                tail = parent_types[-len(spec.parent_types):]
                if tail == spec.parent_types:
                    s += 0.8
            if spec.text and n.text == spec.text:
                s += 0.5
            if spec.descr and n.descr == spec.descr:
                s += 0.5
            # 新增：同类型无标识兄弟的唯一区分线索（数据早已在 spec 里）
            if spec.text_deep and \
                    (getattr(n, 'text_deep', '') or '').strip() == spec.text_deep:
                s += DEEP_WEIGHT
            return s

        scored = sorted(same_type, key=score, reverse=True)
        if scored and score(scored[0]) > 0:
            if len(scored) == 1 or score(scored[0]) > score(scored[1]):
                return scored[0], 'path_type'
        return None, ''


def probe(samples_dir: str, subset) -> int:
    base, deep = LocatorManager(), L3WithDeep()
    rows, tot = [], {'blind': 0, 'b': 0, 'd': 0, 'sub_blind': 0, 'sub_b': 0, 'sub_d': 0}

    for name, page in iter_samples(samples_dir):
        blind = blind_controls(page)
        b = d = 0
        for node in blind:
            spec = spec_of(node)
            b += (base._lv_path_type(spec, page)[0] is node)
            d += (deep._lv_path_type(spec, page)[0] is node)
        rows.append([name, len(blind), f'{b}/{len(blind)}', pct(b, len(blind)),
                     f'{d}/{len(blind)}', pct(d, len(blind))])
        tot['blind'] += len(blind)
        tot['b'] += b
        tot['d'] += d
        if subset and name in subset:
            tot['sub_blind'] += len(blind)
            tot['sub_b'] += b
            tot['sub_d'] += d

    print(f'样本目录：{samples_dir}')
    print_table(['样本', '无标识', 'L3现状', '现状率', 'L3+deep', '新率'],
                rows, aligns=['<', '>', '>', '>', '>', '>'])
    print(f'全量：{tot["b"]}/{tot["blind"]} = {pct(tot["b"], tot["blind"])}'
          f'  ->  {tot["d"]}/{tot["blind"]} = {pct(tot["d"], tot["blind"])}'
          f'  （提升 {tot["d"] / tot["b"]:.1f}x）' if tot['b'] else '')
    if tot['sub_blind']:
        print(f'子集（{len(subset)} 个应用）：'
              f'{tot["sub_b"]}/{tot["sub_blind"]} = {pct(tot["sub_b"], tot["sub_blind"])}'
              f'  ->  {tot["sub_d"]}/{tot["sub_blind"]} = '
              f'{pct(tot["sub_d"], tot["sub_blind"])}')
        print(f'  该子集未命中 {tot["sub_blind"] - tot["sub_d"]} 个 —— '
              f'这些兄弟的子树文案本就相同，正确行为是继续落到 L4/L5。')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--samples', default=DEFAULT_SAMPLES)
    ap.add_argument('--subset', default=','.join(DEFAULT_SUBSET),
                    help='子集汇总用（逗号分隔样本名）；传空串关闭')
    args = ap.parse_args()
    if not os.path.isdir(args.samples):
        print(f'样本目录不存在：{args.samples}')
        return 2
    subset = tuple(s for s in args.subset.split(',') if s)
    return probe(args.samples, subset)


if __name__ == '__main__':
    raise SystemExit(main())
