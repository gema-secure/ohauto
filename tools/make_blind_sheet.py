# -*- coding: utf-8 -*-
"""盲区标注工装 —— 把「人话文案不可用」的可交互控件做成可点选的标注表。

为什么需要它
------------
`eval_vision_offline.py` 的评测集**只能测「视觉与控件树的一致性」**，测不了
视觉的**绝对准确率** —— 因为标注本身就来自控件树（开卷考试）。

真机数据显示：73 个可交互控件里 **27 个（37%）人话文案不可用**
（`sample_calc` 20 个可交互控件**全部**如此：19 个只有 id、1 个连 id 都没有）。
控件树对这批控件彻底哑火，因此**只有它们能测出视觉通道的真实水平**。
但它们的标注买不到、算不出 —— 只能人看。

这个工装把那次「人看」的成本压到最低：
在整张真机截图上把每个盲区控件画成**编号红框**，下面配一张表，
标注员只需照着编号填「这是什么控件」——不用猜坐标、不用换算、不用对图。

零依赖：不裁图、不引 Pillow，直接用 HTML/CSS 把框叠在整图上
（坐标按 720×1280 换算成百分比定位）。产出是一个自包含的 .html，
双击即用，标完点「导出 JSON」拿结果。

用法::

    python tools/make_blind_sheet.py
    python tools/make_blind_sheet.py --samples datasets/gallery_13app
    python tools/make_blind_sheet.py --out _out/vision_eval/blind_sheet.html
"""
from __future__ import annotations

import argparse
import html
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect, flatten      # noqa: E402

#: 与 eval_vision_offline 保持一致：判定「人话文案不可用」的口径。
#: 刻意重复定义而不 import —— 本工装要能单独分发（标注员那边不需要整包依赖），
#: 这个口径短到不值得为它建耦合。
_DARK = 150


@dataclass
class BlindItem:
    """一个待标注的盲区控件。"""
    idx: int
    sample: str
    png: str
    screen: Tuple[int, int]
    node_type: str
    node_id: str
    rect: Rect
    clickable: bool
    scrollable: bool
    path: str

    @property
    def kind(self) -> str:
        if (self.node_id or '').strip():
            return '仅 id'
        return '无任何标识'


def readable(node: LayoutNode) -> str:
    """取人话文案（与 `eval_vision_offline.readable_text` 同口径）。"""
    for v in (node.text, node.descr, node.hint):
        if (v or '').strip():
            return v.strip()
    for k in node.walk():
        if (k.text or '').strip():
            return k.text.strip()
    return ''


def collect(samples_dir: str) -> List[BlindItem]:
    """扫样本目录，收集所有「可交互 + 人话文案不可用 + 面积有效」的控件。"""
    from eval_vision_offline import load_samples      # 复用装载逻辑，避免重写
    out: List[BlindItem] = []
    for s in load_samples(samples_dir):
        assert s.root is not None
        sw, sh = s.screen
        screen_area = max(1, sw * sh)
        idx = 0
        for n in flatten(s.root, only_visible=True):
            if not n.is_interactive() or readable(n):
                continue
            r = n.rect
            # 零面积/退化节点标不了（真机状态栏里就有一批零宽节点）；
            # 覆盖大半屏的容器也不是「一个控件」。两者都排除，
            # 否则标注员会看到一堆没法填的框。
            if r.area <= 0 or r.width < 8 or r.height < 8:
                continue
            if r.area / screen_area > 0.5:
                continue
            idx += 1
            out.append(BlindItem(
                idx=len(out) + 1, sample=s.name, png=s.png, screen=s.screen,
                node_type=n.type, node_id=n.id, rect=r,
                clickable=n.clickable, scrollable=n.scrollable, path=n.path))
    return out


def _pct(v: int, total: int) -> str:
    return f'{100.0 * v / max(1, total):.3f}%'


def build_html(items: Sequence[BlindItem], samples_dir: str) -> str:
    by_sample: Dict[str, List[BlindItem]] = {}
    for it in items:
        by_sample.setdefault(it.sample, []).append(it)

    rows: List[str] = []
    panels: List[str] = []
    filled = 0
    for sname, its in by_sample.items():
        it0 = its[0]
        sw, sh = it0.screen
        rel = os.path.relpath(it0.png, ROOT).replace(os.sep, '/')
        # 图片用相对路径，标注表要能跟着仓库一起发出去
        boxes = []
        for it in its:
            r = it.rect
            filled += 1
            boxes.append(
                f'<div class="box" style="left:{_pct(r.left, sw)};'
                f'top:{_pct(r.top, sh)};width:{_pct(r.width, sw)};'
                f'height:{_pct(r.height, sh)};">'
                f'<span class="tag">{filled}</span></div>')
            rows.append(
                f'<tr data-idx="{filled}">'
                f'<td class="num">{filled}</td>'
                f'<td class="mono">{html.escape(sname)}</td>'
                f'<td class="mono">{html.escape(it.node_type)}'
                f'{"/" + html.escape(it.node_id) if it.node_id else ""}</td>'
                f'<td class="mono small">'
                f'({r.left},{r.top},{r.right},{r.bottom})</td>'
                f'<td class="small">{it.kind}</td>'
                f'<td><input class="lab" data-idx="{filled}" '
                f'placeholder="这是什么控件（人话）"></td>'
                f'<td><select class="act" data-idx="{filled}">'
                f'<option value="">?</option><option value="y">可点</option>'
                f'<option value="n">不可点</option>'
                f'<option value="s">可滑动</option>'
                f'<option value="x">不是控件</option></select></td>'
                f'</tr>')
        panels.append(
            f'<figure><figcaption><b>{html.escape(sname)}</b>'
            f' <span class="small">（{len(its)} 个盲区控件）</span>'
            f'</figcaption><div class="shot" style="aspect-ratio:{sw}/{sh}">'
            f'<img src="{html.escape(rel)}" alt="{html.escape(sname)}">'
            + ''.join(boxes) + '</div></figure>')

    meta = {
        'samples_dir': samples_dir,
        'n_items': len(items),
        'samples': {k: len(v) for k, v in by_sample.items()},
    }

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>视觉盲区标注表 · 多模态 a24</title>
<style>
:root {{ --line:#d8dbe0; --ink:#1f2328; --dim:#6b7280; --acc:#c62828; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:24px 28px 80px; background:#f7f8fa;
  color:var(--ink);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
h2 {{ font-size:15px; margin:28px 0 10px; padding-bottom:6px;
  border-bottom:2px solid var(--line); }}
.lead {{ color:var(--dim); font-size:13px; margin:0 0 18px; }}
.lead b {{ color:var(--ink); }}
.mono {{ font-family:ui-monospace,Consolas,monospace; font-size:12px; }}
.small {{ font-size:12px; color:var(--dim); }}
.gal {{ display:flex; flex-wrap:wrap; gap:20px; }}
figure {{ margin:0; background:#fff; border:1px solid var(--line);
  border-radius:8px; padding:10px; }}
figcaption {{ font-size:13px; margin-bottom:8px; }}
.shot {{ position:relative; width:270px; }}
.shot img {{ width:100%; display:block; border-radius:4px; }}
.box {{ position:absolute; border:2px solid var(--acc);
  background:rgba(198,40,40,.14); }}
.tag {{ position:absolute; top:-2px; left:-2px; background:var(--acc);
  color:#fff; font-size:10px; line-height:15px; padding:0 4px;
  border-radius:0 0 4px 0; font-weight:700; }}
table {{ width:100%; border-collapse:collapse; background:#fff;
  border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
th,td {{ padding:6px 8px; border-bottom:1px solid var(--line);
  text-align:left; vertical-align:middle; }}
th {{ background:#eef0f3; font-size:12px; color:var(--dim);
  position:sticky; top:0; }}
td.num {{ font-weight:700; color:var(--acc); width:36px; }}
input.lab,select.act {{ width:100%; padding:5px 7px;
  border:1px solid var(--line); border-radius:4px; font:13px inherit; }}
input.lab:focus {{ outline:2px solid #90caf9; border-color:#90caf9; }}
.bar {{ position:fixed; left:0; right:0; bottom:0; background:#fff;
  border-top:1px solid var(--line); padding:10px 28px;
  display:flex; gap:12px; align-items:center; }}
button {{ padding:7px 14px; border:1px solid var(--line); border-radius:6px;
  background:#fff; font:13px inherit; cursor:pointer; }}
button.primary {{ background:#1f6feb; color:#fff; border-color:#1f6feb; }}
#out {{ flex:1; color:var(--dim); font-size:12px;
  font-family:ui-monospace,monospace; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; }}
</style></head><body>
<h1>视觉盲区标注表</h1>
<p class="lead">
真机 73 个可交互控件里 <b>27 个（37%）人话文案不可用</b>，控件树定位对它们
完全失效。<b>只有这批控件能测出视觉通道的绝对准确率</b> —— 标注只能人看，
所以这张表把成本压到最低：照着编号填文字即可。
<br>填完点右下「导出 JSON」，把结果贴回给集成方。
</p>

<h2>一、在截图上看这些框在哪</h2>
<div class="gal">{''.join(panels)}</div>

<h2>二、逐条标注（编号与上图红框一致）</h2>
<table><thead><tr>
<th>#</th><th>样本</th><th>控件 type/id</th><th>rect</th><th>标识情况</th>
<th>这是什么控件（人话）</th><th>可操作性</th>
</tr></thead><tbody>{''.join(rows)}</tbody></table>

<div class="bar">
  <button class="primary" onclick="dump()">导出 JSON</button>
  <button onclick="copyOut()">复制</button>
  <span id="out">共 {len(items)} 条待标注</span>
</div>
<script>
const META = {meta!r};
function dump() {{
  const rows = [...document.querySelectorAll('tr[data-idx]')].map(tr => {{
    const i = tr.dataset.idx;
    return {{
      idx: +i,
      sample: tr.children[1].textContent,
      ctrl: tr.children[2].textContent,
      rect: tr.children[3].textContent,
      label: (document.querySelector(`input.lab[data-idx="${{i}}"]`)||{{}}).value||'',
      action: (document.querySelector(`select.act[data-idx="${{i}}"]`)||{{}}).value||''
    }};
  }});
  const filled = rows.filter(r => r.label).length;
  const payload = Object.assign({{}}, META, {{
    annotated_at: new Date().toISOString().slice(0,19),
    filled: filled, total: rows.length, items: rows
  }});
  document.getElementById('out').textContent =
    `已填 ${{filled}}/${{rows.length}} 条`;
  return JSON.stringify(payload, null, 2);
}}
function copyOut() {{
  navigator.clipboard.writeText(dump())
    .then(() => document.getElementById('out').textContent += '（已复制）');
}}
</script></body></html>
'''


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='生成视觉盲区人工标注表（自包含 HTML）')
    ap.add_argument('--samples', default='',
                    help='样本目录；默认取 eval_vision_offline 的默认目录')
    ap.add_argument('--out', default=os.path.join(
        ROOT, '_out', 'vision_eval', 'blind_sheet.html'))
    args = ap.parse_args(argv)

    from eval_vision_offline import find_samples_dir
    src = find_samples_dir(args.samples)
    if not src:
        print('找不到样本目录。用 --samples 指定。')
        return 1

    items = collect(src)
    if not items:
        print('没收集到盲区控件。')
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(build_html(items, src))

    by_sample: Dict[str, int] = {}
    for it in items:
        by_sample[it.sample] = by_sample.get(it.sample, 0) + 1
    print(f'盲区控件 {len(items)} 个（共 {len(by_sample)} 个样本）：')
    for k, v in by_sample.items():
        print(f'  {k:16s} {v:3d} 个')
    print(f'\n[输出] {args.out}')
    print('双击打开 → 照着红框编号填「这是什么控件」→ 点「导出 JSON」')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
