"""用例健康度批量检查 —— 三源融合思想用在「用例复用」上（用例沉淀）。

要解决的真实痛点
----------------
用例沉淀与复用要解决的核心问题是用例会悄悄失效：
应用改了控件名、删了控件，用例照旧躺在仓库里，直到某天真机跑挂了才发现。

本工具把**用例引用的控件**当「声明」，把**运行时控件树**当「实际」，
逐条对齐 —— 于是「失效用例」从"某天跑挂了才知道"变成"**扫一遍就列出来**"。

    python tools/case_health.py                 # 扫 examples/cases × datasets 样本
    python tools/case_health.py --cases <dir> --samples <dir>

配对规则：用例的 `bundle` ↔ 样本 `meta.json` 里的 `bundle`。
**没配到样本的用例**会单独列出来 —— 那是一个**覆盖缺口**，
不是"健康"，不能混在一起（这是最容易自欺的地方）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from ohauto import fusion                                 # noqa: E402
from ohauto.layout import parse_layout                    # noqa: E402

DEFAULT_CASES = os.path.join(ROOT, 'examples', 'cases')
DEFAULT_SAMPLES = os.path.join(ROOT, 'datasets', 'gallery_13app')


def load_case_bundle(path: str) -> str:
    """从用例 YAML 里取 bundle —— 只用它做配对，所以正则足够。"""
    import re
    with open(path, encoding='utf-8') as f:
        m = re.search(r'^\s*bundle\s*:\s*["\']?([\w./-]+)', f.read(), re.M)
    return m.group(1) if m else ''


def load_samples(samples_dir: str) -> List[Dict[str, Any]]:
    """扫样本目录：每个 .json + .meta.json 组成一份运行时数据。"""
    out = []
    for jp in sorted(glob.glob(os.path.join(samples_dir, '*.json'))):
        if jp.endswith('.meta.json'):
            continue
        base = jp[:-5]
        meta_p = base + '.meta.json'
        meta = {}
        if os.path.isfile(meta_p):
            with open(meta_p, encoding='utf-8') as f:
                meta = json.load(f)
        try:
            with open(jp, encoding='utf-8') as f:
                root = parse_layout(f.read())
        except Exception as e:
            print('  ⚠️ 样本解析失败 %s: %s' % (os.path.basename(jp), e))
            continue
        out.append({'name': os.path.basename(base), 'bundle': str(meta.get('bundle') or ''),
                    'root': root, 'meta': meta})
    return out


def ids_of(root) -> set:
    return {n.id for n in root.walk() if n.id}


def texts_of(root) -> set:
    out = set()
    for n in root.walk():
        for t in (n.text, n.text_deep):
            if t:
                out.add(t)
    return out


def check(case_path: str, sample: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """单条用例的健康度 —— 声明源统一走 `ohauto.fusion` 的适配器。

    这里刻意不用 `fusion.fuse()` 的完整报告：批量场景只要「缺了几个、缺哪些」，
    自己算比解析报告更直接。**但声明怎么提取，必须和融合器用同一份实现** ——
    否则两条路的"声明"口径会分叉（这个坑踩过：重写 trifusion 时
    把 case_health 的旧导入打断了）。
    """
    src = fusion.source_case(case_path)
    name = os.path.basename(case_path)
    items = [{'kind': c.kind, 'value': c.value} for c in src.claims]

    if not src.ok:
        return {'case': name, 'ok': False, 'reason': src.reason,
                'declared': 0, 'confirmed': 0, 'missing': []}
    if sample is None:
        return {'case': name, 'ok': False,
                'reason': '**没有对应样本**（覆盖缺口，不代表健康）',
                'declared': len(items), 'confirmed': 0,
                'missing': [i['value'] for i in items]}

    ids, texts = ids_of(sample['root']), texts_of(sample['root'])
    missing = []
    for it in items:
        v = it['value']
        hit = (v in ids) if it['kind'] == 'id' else bool(texts & {v}) or any(
            v in t for t in texts)
        if not hit:
            missing.append('%s=%s' % (it['kind'], v))
    return {'case': name, 'ok': True, 'reason': '',
            'sample': sample['name'], 'bundle': sample['bundle'],
            'declared': len(items),
            'confirmed': len(items) - len(missing),
            'missing': missing}


def render_md(rows: List[Dict[str, Any]]) -> str:
    L = ['# 用例健康度报告', '',
         '把「用例引用的控件」（声明）与「运行时控件树」（实际）逐条对齐 ——',
         '**失效用例不用等真机跑挂，扫一遍就列出来**。', '',
         '| 用例 | 配到的样本 | 声明 | 确认 | 缺失 | 健康度 |',
         '|---|---|---|---|---|---|']
    for r in rows:
        rate = ('%.0f%%' % (r['confirmed'] / r['declared'] * 100)
                if r.get('declared') else '-')
        L.append('| `%s` | %s | %d | %d | %d | %s |' % (
            r['case'], r.get('sample') or '**（无样本）**',
            r.get('declared', 0), r.get('confirmed', 0), len(r.get('missing') or []),
            rate if r.get('ok') else '—'))
    L.append('')
    gaps = [r for r in rows if not r.get('ok')]
    bad = [r for r in rows if r.get('ok') and r.get('missing')]
    if gaps:
        L += ['## ⚠️ 覆盖缺口（**没有配到样本，不代表健康**）', '']
        for r in gaps:
            L.append('- `%s` —— %s' % (r['case'], r['reason']))
        L.append('')
    if bad:
        L += ['## 🔴 失效控件（**用例引用的控件在运行时找不到**）', '']
        for r in bad:
            L += ['- `%s`（样本 %s）缺失 %d 个：' % (r['case'], r['sample'], len(r['missing'])),
                  '  - ' + '、'.join('`%s`' % m for m in r['missing'])]
        L.append('')
    if not gaps and not bad:
        L += ['## 结论', '', '全部用例与样本对齐，无失效控件。', '']
    return '\n'.join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--cases', default=DEFAULT_CASES)
    ap.add_argument('--samples', default=DEFAULT_SAMPLES)
    ap.add_argument('--out', help='报告输出路径（Markdown）')
    args = ap.parse_args()

    samples = load_samples(args.samples)
    by_bundle = {}
    for s in samples:
        if s['bundle']:
            by_bundle.setdefault(s['bundle'], s)
    print('样本 %d 份，覆盖 %d 个 bundle' % (len(samples), len(by_bundle)))
    for b in sorted(by_bundle):
        print('  %-42s <- %s' % (b, by_bundle[b]['name']))
    print()

    rows = []
    for cp in sorted(glob.glob(os.path.join(args.cases, '*.yaml'))):
        b = load_case_bundle(cp)
        rows.append(check(cp, by_bundle.get(b)))
        print('  %-24s bundle=%-38s -> %s'
              % (os.path.basename(cp), b or '(未声明)',
                 '已配对' if b in by_bundle else '无样本'))

    md = render_md(rows)
    print()
    print(md)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(md)
        print('报告已写入 %s' % args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
