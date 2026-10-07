# -*- coding: utf-8 -*-
"""跨形态差异的根因聚类 —— 把「N 条缺失明细」收拢成「少数几个真问题」。

★ 为什么要有这一步
------------------

`crossform_run.py` 的产出是**逐元素**差异：Mate X7 设置页折叠后报
缺失 77 条。但逐条看就知道，其中 DOCK_RESIDENT_BG / DOCK_recent_BG /
两个 recent 图标……大概率是**同一个根因**（外屏不渲染整个 dock 区块）
的十几个叶子。开发者拿到 77 这个数字既不知道先修哪个、也不知道哪些
是本来就该消失的 —— 数字很大，可行动性为零。

本工具做三件事：

1. **子树归并**：缺失元素 X 的祖先链上还有缺失元素 Y，则 X 并入 Y
   ——「整个分支没渲染」是一条根因，不是 N 条缺陷；
2. **共父分组**：若干缺失根各自独立、但共享同一个（未缺失的）父容器，
   归成一组「父容器 P 的子节点整体缺失」—— 响应式布局断层的典型形态；
3. **影响定级**：簇内有可交互控件（`LayoutNode.is_interactive()`）→ HIGH；
   有 id/文本语义 → MEDIUM；纯装饰容器 → LOW。把「树变了」翻译成
   「用户少了什么」。

口径纪律
--------

* 聚类是**启发式归并**，产物定位是「候选问题清单」，最终判断需人工确认；
* 解析不回身份的缺失条目**如实单列**（collect_elements 对重复身份保留
  首个，这类条目本来就不适合做身份比对），不假装聚上了；
* OVERFLOW 不做子树归并（溢出本来就是逐元素判的），只按父容器分组。

用法
----

    python tools/crossform_cluster.py --run-dir _out/crossform/Mate_X7_settings
    python tools/crossform_cluster.py --run-dir <dir> --out <dir>/cluster.md

产物：`<run-dir>/cluster.md` + `cluster.json`（缺省都落在 run 目录里）。
退出码：0 = 成功；2 = 目录/文件不齐。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.crossform import IdentityPolicy, collect_elements   # noqa: E402
from ohauto.layout import LayoutNode, parse_layout              # noqa: E402


# ================================================================ 数据结构

@dataclass
class Cluster:
    """一条根因候选：一个缺失子树的根 + 挂在它下面的全部缺失成员。"""

    root: LayoutNode
    members: List[LayoutNode] = field(default_factory=list)
    kind: str = 'SUBTREE'            # SUBTREE=分支整体缺失 | SIBLINGS=共父成组缺失

    @property
    def interactive(self) -> List[LayoutNode]:
        return [m for m in self.members if m.is_interactive()]

    @property
    def semantic(self) -> List[LayoutNode]:
        """有语义标识（id / 文案）的成员 —— 修复时能对得上代码的。"""
        return [m for m in self.members if (m.id or m.label.strip())]

    @property
    def severity(self) -> str:
        if self.interactive:
            return 'HIGH'
        if self.semantic:
            return 'MEDIUM'
        return 'LOW'

    def where(self) -> str:
        """根在树里的位置（往上三代容器的类型链），给开发者一个方位感。"""
        chain, p, guard = [], self.root.parent, 0
        while p is not None and guard < 3:
            tag = p.type or '?'
            if p.id:
                tag += f'#{p.id}'
            chain.append(tag)
            p = p.parent
            guard += 1
        return ' > '.join(chain) if chain else '(根节点)'

    def bounds(self) -> Tuple[int, int, int, int]:
        rs = [m.rect for m in self.members]
        return (min(r.left for r in rs), min(r.top for r in rs),
                max(r.right for r in rs), max(r.bottom for r in rs))

    def samples(self, n: int = 3) -> List[str]:
        """成员的代表性身份——优先可交互，其次有语义的。"""
        pool = self.interactive + self.semantic + self.members
        out, seen = [], set()
        for m in pool:
            key = m.id or f'{m.type}|{m.label.strip()[:18]}'
            if key in seen:
                continue
            seen.add(key)
            out.append(key)
            if len(out) >= n:
                break
        return out


# ================================================================ 聚类

def cluster_missing(missing_nodes: List[LayoutNode],
                    unresolved: List[Dict]) -> List[Cluster]:
    """把缺失节点归并成根因候选。

    每个缺失节点向上找**最顶端的缺失祖先**（路径上最高的缺失节点）：
    找到 → 并入它所在的簇；没找到 → 自己是一条根因。
    这样「A 缺失、A 的孩子 B 缺失、B 的孩子 C 缺失」整条链必然归到 A，
    不会出现挂在中间节点上的成员被丢掉。
    剩余的独立根再按「共享同一个未缺失父容器」做共父分组。
    """
    missing_ids = set(id(n) for n in missing_nodes)

    def top_missing_ancestor(n: LayoutNode) -> Optional[LayoutNode]:
        top, p = None, n.parent
        while p is not None:
            if id(p) in missing_ids:
                top = p                     # 记最高的，一路爬到树根
            p = p.parent
        return top

    grouped: Dict[int, List[LayoutNode]] = {}   # 根 id() -> 成员
    roots: List[LayoutNode] = []
    for n in missing_nodes:
        top = top_missing_ancestor(n)
        if top is None:
            roots.append(n)
        else:
            grouped.setdefault(id(top), []).append(n)

    clusters: List[Cluster] = []
    for r in roots:
        clusters.append(Cluster(root=r,
                                members=[r] + grouped.get(id(r), []),
                                kind='SUBTREE'))

    # ---- 第二层：共父分组。≥2 个独立根共享同一个未缺失父容器 → 合并
    by_parent: Dict[int, List[Cluster]] = {}
    for c in clusters:
        if c.root.parent is not None:
            by_parent.setdefault(id(c.root.parent), []).append(c)
    for parent_id, group in by_parent.items():
        if len(group) < 2:
            continue
        keep = group[0]
        for other in group[1:]:
            keep.members.extend(other.members)
            clusters.remove(other)
        keep.kind = 'SIBLINGS'
        keep.members = sorted({id(m): m for m in keep.members}.values(),
                              key=lambda m: (m.rect.top, m.rect.left))

    # 安全网：理论上每个缺失节点都恰好落进一个簇
    covered = {id(m) for c in clusters for m in c.members}
    for n in missing_nodes:
        if id(n) not in covered:
            unresolved.append({'identity': f'{n.type}|{(n.id or n.label)[:20]}',
                               'reason': '未落入任何簇（不应出现，请反馈）'})
    return sorted(clusters, key=lambda c: (-len(c.interactive), -len(c.members)))


def group_overflow(diffs: List[Dict]) -> List[Dict]:
    """OVERFLOW 按父容器分组（evidence/detail 里的父容器 bounds 相同 = 同一处）。"""
    groups: Dict[str, List[Dict]] = {}
    for d in diffs:
        text = json.dumps(d.get('evidence') or {}, ensure_ascii=False) + d.get('detail') or ''
        rects = re.findall(r'\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]', text)
        key = rects[-1] if len(rects) >= 2 else 'unknown'
        groups.setdefault(f'父容器[{",".join(key)}]', []).append(d)
    return [{'parent': k, 'items': v} for k, v in
            sorted(groups.items(), key=lambda kv: -len(kv[1]))]


# ================================================================ 主流程

def run(run_dir: str, out_md: Optional[str] = None) -> int:
    cross = os.path.join(run_dir, 'crossform.json')
    if not os.path.isfile(cross):
        print(f'✗ 找不到 {cross} —— run 目录里应有 crossform_run.py 的产物')
        return 2
    data = json.load(open(cross, encoding='utf-8'))

    base_dir = next((os.path.join(run_dir, d) for d in os.listdir(run_dir)
                     if d.startswith('baseline_')
                     and os.path.isfile(os.path.join(run_dir, d, 'layout.json'))), None)
    if not base_dir:
        print('✗ 找不到 baseline_*/layout.json —— 没有基准树就无法定位缺失元素的祖先')
        return 2
    root = parse_layout(os.path.join(base_dir, 'layout.json'))
    elements = collect_elements(root, IdentityPolicy())

    diffs = data.get('differences', [])
    missing = [d for d in diffs if d.get('kind') == 'MISSING']
    overflow = [d for d in diffs if d.get('kind') == 'OVERFLOW']

    nodes, unresolved = [], []
    for d in missing:
        n = elements.get(d['identity'])
        if n is not None:
            nodes.append(n.node)
        else:
            unresolved.append({'identity': d['identity'], 'reason': '基准元素表中无此身份'
                               '（重复身份被 collect_elements 丢弃，或 only_visible 过滤）'})

    clusters = cluster_missing(nodes, unresolved) if nodes else []
    overflow_groups = group_overflow(overflow) if overflow else []

    # ---- 汇总数字
    n_missing = len(missing)
    n_inter = sum(len(c.interactive) for c in clusters)
    print('=' * 62)
    print(f'  根因聚类：{os.path.basename(run_dir.rstrip("/\\"))}')
    print(f'  缺失 {n_missing} 条 → {len(clusters)} 个根因候选'
          f'（可交互损失 {n_inter} 个）；未归并 {len(unresolved)} 条')
    print(f'  溢出 {len(overflow)} 条 → {len(overflow_groups)} 组')
    print('=' * 62)
    for i, c in enumerate(clusters, 1):
        b = c.bounds()
        tag = {'HIGH': '🔴', 'MEDIUM': '🟡', 'LOW': '⚪'}[c.severity]
        print(f'{tag} [{i}] {c.kind} {c.severity}  {c.root.type}'
              f'{"#" + c.root.id if c.root.id else ""}'
              f'  成员 {len(c.members)}（可交互 {len(c.interactive)}）'
              f'  bounds={list(b)}  位置: {c.where()}')
        for s in c.samples():
            print(f'      · {s}')

    # ---- 落盘
    out_md = out_md or os.path.join(run_dir, 'cluster.md')
    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'run_dir': run_dir,
        'caveat': '根因聚类是启发式归并，产物为候选问题清单，最终判断需人工确认',
        'summary': {'missing_raw': n_missing, 'clusters': len(clusters),
                    'interactive_lost': n_inter, 'unresolved': len(unresolved),
                    'overflow_raw': len(overflow), 'overflow_groups': len(overflow_groups)},
        'clusters': [{
            'kind': c.kind, 'severity': c.severity,
            'root': {'type': c.root.type, 'id': c.root.id,
                     'bounds': list(c.root.rect.to_dict().values()) if c.root.rect else None,
                     'where': c.where()},
            'members': len(c.members), 'interactive': len(c.interactive),
            'semantic': len(c.semantic), 'bounds': list(c.bounds()),
            'samples': c.samples(5),
        } for c in clusters],
        'unresolved': unresolved,
        'overflow_groups': overflow_groups,
    }
    with open(os.path.join(run_dir, 'cluster.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    sev_cn = {'HIGH': '高', 'MEDIUM': '中', 'LOW': '低'}
    lines = [
        '# 跨形态差异 · 根因聚类', '',
        f'> 生成时间：{payload["generated_at"]}；原始缺失 **{n_missing}** 条 → '
        f'根因候选 **{len(clusters)}** 个（可交互损失 {n_inter}，未归并 {len(unresolved)}）',
        f'> ⚠️ {payload["caveat"]}', '',
        '| # | 定级 | 类型 | 根（类型#id） | 位置（向上三代） | 成员 | 可交互 | 典型成员 |',
        '|---|---|---|---|---|---:|---:|---|',
    ]
    for i, c in enumerate(clusters, 1):
        lines.append(f'| {i} | {sev_cn[c.severity]} | {c.kind} | '
                     f'{c.root.type}{"#" + c.root.id if c.root.id else ""} | '
                     f'{c.where()} | {len(c.members)} | {len(c.interactive)} | '
                     f'{"、".join(c.samples(2))} |')
    lines += ['', '## 未归并条目（如实保留，不硬凑）', '']
    if unresolved:
        for u in unresolved[:10]:
            lines.append(f'- `{u["identity"]}` —— {u["reason"]}')
        if len(unresolved) > 10:
            lines.append(f'- …共 {len(unresolved)} 条')
    else:
        lines.append('（无）')
    lines += ['', '## 溢出（OVERFLOW）按父容器分组', '']
    if overflow_groups:
        for g in overflow_groups:
            lines.append(f'- **{g["parent"]}**：{len(g["items"])} 条 —— '
                         + '；'.join(d['label'] for d in g['items'][:4]))
    else:
        lines.append('（无）')
    lines.append('')
    with open(out_md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'  产物: {out_md}')
    print(f'        {os.path.join(run_dir, "cluster.json")}')
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--run-dir', required=True,
                    help='crossform_run.py 的运行目录（内含 crossform.json 与 baseline_*/）')
    ap.add_argument('--out', default=None, help='聚类报告 md 路径（缺省落在 run 目录）')
    a = ap.parse_args()
    sys.exit(run(a.run_dir, a.out))


if __name__ == '__main__':
    main()
