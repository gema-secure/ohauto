# -*- coding: utf-8 -*-
"""逐应用：语义标识分布 + L3 命中率。

回答的是「换一个应用会怎样」：真机上大量可交互控件没有 id、也没有自身文案，
定位只能落到层级路径那一级。本脚本把每个应用的这组数摊开。

    # 离线：直接分析仓库内的样本
    python probes/probe_apps_real.py

    # 真机：先采集（force-stop → 拉起 → 控件树 + 截图），再分析
    python probes/probe_apps_real.py --capture tools/_out/multiapp_survey

注意：本脚本**不做探索**。「探索页数 / 覆盖度」需要真机真实点击，
沿用既有工具 `tools/e2e_smoke_real.py`（其阶段 2 会真点击并给覆盖度），
这里不重复实现。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import (DEFAULT_SAMPLES, ROOT, LocatorManager,   # noqa: E402
                     blind_controls, iter_samples, pct,
                     print_table, spec_of)


def capture(out_dir: str) -> int:
    """调既有采集工具抓一批控件树到 out_dir（需要真机在线）。"""
    script = os.path.join(ROOT, 'tools', 'multiapp_survey.py')
    if not os.path.isfile(script):
        print(f'找不到采集工具：{script}')
        return 2
    cmd = [sys.executable, script, '--out', out_dir]
    print('采集：', ' '.join(cmd))
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        print('采集失败 —— 确认设备在线（hdc list targets）后重试。')
    return rc


def probe(samples_dir: str) -> int:
    lm = LocatorManager()
    rows, tot = [], {'node': 0, 'inter': 0, 'wid': 0, 'wtxt': 0, 'blind': 0, 'hit': 0}

    for name, page in iter_samples(samples_dir):
        nodes = list(page.walk())
        inter = [n for n in nodes if n.is_interactive()]
        wid = [n for n in inter if (n.id or '').strip()]
        wtxt = [n for n in inter if (n.text or '').strip()]
        blind = blind_controls(page)
        hit = sum(1 for node in blind
                  if lm._lv_path_type(spec_of(node), page)[0] is node)
        rows.append([name, len(nodes), len(inter), len(wid), len(wtxt), len(blind),
                     pct(len(blind), len(inter)), pct(hit, len(blind))])
        tot['node'] += len(nodes)
        tot['inter'] += len(inter)
        tot['wid'] += len(wid)
        tot['wtxt'] += len(wtxt)
        tot['blind'] += len(blind)
        tot['hit'] += hit

    print(f'样本目录：{samples_dir}')
    print_table(['样本', '节点', '可交互', '有id', '有文案', '无标识', '无标识占比', 'L3命中'],
                rows, aligns=['<', '>', '>', '>', '>', '>', '>', '>'])
    print(f'合计：节点 {tot["node"]} ｜ 可交互 {tot["inter"]} ｜ 带 id {tot["wid"]} '
          f'({pct(tot["wid"], tot["inter"])}) ｜ 无标识 {tot["blind"]} '
          f'({pct(tot["blind"], tot["inter"])}) ｜ L3 命中 {pct(tot["hit"], tot["blind"])}')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--samples', default=DEFAULT_SAMPLES,
                    help='控件树样本目录（默认 datasets/gallery_13app）')
    ap.add_argument('--capture', metavar='DIR',
                    help='先用 tools/multiapp_survey.py 采集到 DIR，再分析 DIR')
    args = ap.parse_args()

    samples = args.capture or args.samples
    if args.capture:
        rc = capture(args.capture)
        if rc != 0:
            return rc
    if not os.path.isdir(samples):
        print(f'样本目录不存在：{samples}')
        return 2
    return probe(samples)


if __name__ == '__main__':
    raise SystemExit(main())
