# -*- coding: utf-8 -*-
"""缺陷最小复现包 —— 把跨形态差异的根因接到「可重放的操作路径」上。

★ 为什么要有这一步
------------------

`crossform_cluster.py` 回答了「哪里坏了」（根因候选 + 树中方位 +
可交互损失），但开发者拿到的仍然是一份静态快照 —— 要复现，还得自己
猜「这个页面是怎么进去的、折叠态是怎么切的」。本工具把最后一段接上：

    探索状态图（graph.json）  ──页面指纹匹配──▶  缺陷所在页面
            │                                      │
            └─ BFS 出语义操作路径（id/text，无坐标） ┘
                                                   ▼
        复现包 = 启动应用 → 逐步点击（语义定位）→ 切折叠态 → 缺陷 manifests

匹配原理：探索器给每个页面算过双签名（`build_page_signature`，见
ohauto/explorer.py），crossform 采集的基准树用同一函数重算，
`structural_key` 精确相等才算匹配 —— **匹配不上就如实说匹配不上**，
绝不猜一条不可靠的路径出去（红线：产物里绝不出现坐标，宁缺毋滥）。

用法
----

    python tools/defect_repro.py --run-dir _out/crossform/Mate_X7_settings \\
        --graph tools/_out/explore_emulator/open/graph.json \\
        --explore-json tools/_out/explore_emulator/explore_emulator.json

产物：`<run-dir>/repro.md` + `repro.json`。
退出码：0 = 匹配成功；1 = 页面指纹匹配不上；2 = 输入文件不齐。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.explorer import build_page_signature, _digest        # noqa: E402
from ohauto.layout import flatten, parse_layout                  # noqa: E402


# ================================================================ 输入装载

def _load_json(path: str) -> Dict:
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _baseline_dir(run_dir: str) -> Optional[str]:
    for d in sorted(os.listdir(run_dir)):
        p = os.path.join(run_dir, d)
        if d.startswith('baseline_') and os.path.isfile(os.path.join(p, 'layout.json')):
            return p
    return None


def match_state(baseline_root, bundle: str, ability: str,
                graph: Dict) -> Tuple[Optional[Dict], str]:
    """把 crossform 基准树匹配到状态图页面。返回 (state, 匹配方式)。

    两级匹配，都不猜：
    1. 树自带 pagePath/bundle 属性时按节点上下文精确匹配；
    2. 否则用**探索记录里显式存在的上下文**重建签名 —— bundle/ability
       来自 explore_emulator.json，page_path 取状态图各页面一致记录的
       page_path（模拟器的 dumpLayout 不带 pagePath 属性，而探索签名
       是带上下文算的，这一步就是把两边对齐到同一口径）。
    """
    sig = build_page_signature(baseline_root, bundle=bundle, ability=ability)
    by_struct = {s.get('structural_key'): s for s in graph.get('states', [])}
    by_content = {s.get('content_key'): s for s in graph.get('states', [])}
    if sig.structural_key in by_struct:
        return by_struct[sig.structural_key], 'structural_key 精确匹配'
    if sig.content_key in by_content:
        return by_content[sig.content_key], 'content_key 匹配（结构有微差）'

    # 上下文重建：探索记录里有、而 crossform 树里没有的上下文
    paths = {s.get('page_path') for s in graph.get('states', []) if s.get('page_path')}
    if len(paths) == 1 and bundle:
        parts = [n.type for n in flatten(baseline_root, only_visible=True)
                 if n.rect.area > 0]
        key = _digest(f'{bundle}|{ability}|{paths.pop()}|{",".join(parts)}')
        if key in by_struct:
            return by_struct[key], 'structural_key 匹配（上下文取自探索记录）'
    return None, ''


def bfs_path(graph: Dict, dst_id: str) -> List[Dict]:
    """从入口页 BFS 到目标页，返回边序列（全部取 ok=True 的边）。"""
    edges = [e for e in graph.get('transitions', []) if e.get('ok', True)]
    adj: Dict[str, List[Dict]] = {}
    for e in edges:
        adj.setdefault(e['src'], []).append(e)
    start = graph.get('states', [{}])[0].get('id')
    if start is None:
        return []
    prev: Dict[str, Tuple[str, Dict]] = {start: ('', {})}
    q = deque([start])
    while q:
        cur = q.popleft()
        if cur == dst_id:
            break
        for e in adj.get(cur, []):
            if e['dst'] not in prev:
                prev[e['dst']] = (cur, e)
                q.append(e['dst'])
    if dst_id not in prev:
        return []
    path, cur = [], dst_id
    while cur != start:
        src, e = prev[cur]
        path.append(e)
        cur = src
    return list(reversed(path))


def edge_to_step(e: Dict) -> Dict:
    """边 → 可重放步骤。只吐语义定位（spec.id / control 文案），绝不吐坐标。"""
    spec = e.get('spec') or {}
    step: Dict[str, any] = {'action': 'tap', 'dst': e.get('dst')}
    if spec.get('id'):
        step['locator'] = {'kind': 'id', 'value': spec['id']}
    else:
        # 探索边上的 control 文案是「类型:文案」形态，抽文案部分做 text 定位
        ctrl = (e.get('control') or '').split(':', 1)[-1].strip()
        step['locator'] = {'kind': 'text', 'value': ctrl[:40]}
    step['describe'] = ctrl if (ctrl := (e.get('control') or '')[:60]) else step['locator']['value']
    return step


# ================================================================ 主流程

def run(run_dir: str, graph_path: str, explore_json: Optional[str],
        target_state: str) -> int:
    cluster_path = os.path.join(run_dir, 'cluster.json')
    for p, what in ((cluster_path, 'cluster.json（先跑 crossform_cluster.py）'),
                    (graph_path, 'graph.json')):
        if not os.path.isfile(p):
            print(f'✗ 缺 {what}: {p}')
            return 2
    base_dir = _baseline_dir(run_dir)
    if base_dir is None:
        print(f'✗ {run_dir} 下没有 baseline_*/layout.json')
        return 2

    bundle = ability = ''
    if explore_json and os.path.isfile(explore_json):
        ej = _load_json(explore_json)
        bundle, ability = ej.get('bundle', ''), ej.get('ability', '')

    graph = _load_json(graph_path)
    root = parse_layout(os.path.join(base_dir, 'layout.json'))
    state, how = match_state(root, bundle, ability, graph)
    if state is None:
        print('✗ 页面指纹匹配不上 —— crossform 采集的页面不在状态图里。')
        print('  可能原因：探索与 crossform 不是同一次会话/同一页面；')
        print('  请对同一页面先 explore 再 crossform_run，再跑本工具。')
        return 1
    print(f'✓ 页面匹配：{state.get("id")}「{state.get("title")}」（{how}）')

    path = bfs_path(graph, state['id'])
    steps = [edge_to_step(e) for e in path]
    print(f'  从入口到缺陷页共 {len(steps)} 步语义操作')

    clusters = _load_json(cluster_path).get('clusters', [])
    bundle_line = f'{bundle}/{ability}' if bundle else '（应用启动方式见探索记录）'

    repro = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'run_dir': run_dir,
        'page': {'id': state.get('id'), 'title': state.get('title'),
                 'match': how, 'page_path': state.get('page_path')},
        'launch': {'bundle': bundle, 'ability': ability},
        'navigate_steps': steps,
        'fold_switch': {'to': target_state,
                        'command': f'Emulator.exe -instance <name> -foldedState {target_state}'},
        'clusters': clusters,
    }
    with open(os.path.join(run_dir, 'repro.json'), 'w', encoding='utf-8') as f:
        json.dump(repro, f, ensure_ascii=False, indent=1)

    lines = [
        '# 缺陷最小复现包', '',
        f'> 生成时间：{repro["generated_at"]}；页面：'
        f'{state.get("id")}「{state.get("title")}」（{how}）', '',
        '## 复现步骤（全部语义定位，无坐标；定位不到就如实标 unresolved）', '',
        f'1. 启动应用：`{bundle_line}`',
    ]
    for i, s in enumerate(steps, 2):
        loc = s['locator']
        lines.append(f'{i}. 点击 [{loc["kind"]}: {loc["value"]}]（{s["describe"]}）')
    lines += [
        f'{len(steps) + 2}. 切折叠态：`{repro["fold_switch"]["command"]}`，等待 ≥3s 重绘',
        f'{len(steps) + 3}. 预期现象：下述区块在目标形态中缺失/异常', '',
        '## 预期缺陷（来自根因聚类）', '',
        '| # | 定级 | 根 | 位置 | 可交互损失 | bounds |',
        '|---|---|---|---|---:|---|',
    ]
    for i, c in enumerate(clusters, 1):
        r = c.get('root', {})
        lines.append(f'| {i} | {c.get("severity")} | {r.get("type")}'
                     f'{"#" + r.get("id") if r.get("id") else ""} | '
                     f'{r.get("where")} | {c.get("interactive")} | '
                     f'{c.get("bounds")} |')
    lines += ['', '## 关联产物', '',
              f'- 根因聚类：`{os.path.join(run_dir, "cluster.md")}`',
              f'- 差异报告：`{os.path.join(run_dir, "crossform.html")}`',
              f'- 机器可读：`{os.path.join(run_dir, "repro.json")}`', '']
    out_md = os.path.join(run_dir, 'repro.md')
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'  产物: {out_md}')
    print(f'        {os.path.join(run_dir, "repro.json")}')
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--run-dir', required=True, help='crossform 运行目录')
    ap.add_argument('--graph', required=True, help='探索器状态图 graph.json')
    ap.add_argument('--explore-json', default=None,
                    help='explore_emulator.json（取 bundle/ability）')
    ap.add_argument('--target-state', default='close', help='缺陷显现的折叠态')
    a = ap.parse_args()
    sys.exit(run(a.run_dir, a.graph, a.explore_json, a.target_state))


if __name__ == '__main__':
    main()
