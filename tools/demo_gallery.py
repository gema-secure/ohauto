"""演示材料：真机截图 + 控件树叠加的交互式画廊（**自包含 HTML**）。

为什么采用截图 + 叠加标注
--------------------------
本设备的 `uitest screenCap` 单次约 1.26 秒（约 0.8 fps），无法构成录屏；
OpenHarmony 开源版不含 screenrecorder 系统应用。因此演示材料采用
真实截图叠加标注的形式。

为什么这条比单纯放截图强得多
----------------------------
截图和控件树**是同一坐标系**（都是 720×1280 的设备像素），所以能把
「系统看到的东西」直接画在截图上 —— 这是「多模态」最直观的呈现方式：

    * 蓝框 = 可交互控件（对应「布局通道」）
    * 绿框 = 带文案的控件（对应「视觉通道」能对上的部分）
    * 框上标 id / 文案

而且它顺带把**多模态的必要性**讲清楚了：`app_settings` 有 183 个节点、
28 个可交互控件，**但 id 数为 0** —— 只靠控件树，这些控件无法被稳定定位，
必须靠视觉和文案通道补上。这个数字是真实采集的，不是渲染出来的。

产物是**单文件 HTML**（截图 base64 内嵌，无外链），可直接转发给评委。

用法::

    python tools/demo_gallery.py                       # 用真机样本集
    python tools/demo_gallery.py --src datasets/real_samples_20260919 -o out.html
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import parse_layout                    # noqa: E402

DEFAULT_SRC = os.path.join(ROOT, 'datasets', 'real_samples_20260919')
#: 产物落在**仓库内**的 docs/demo/，演示材料要随仓库提交，
#: 不能躺在 _out/（临时产物目录）里
OUT = os.path.join(ROOT, 'docs', 'demo', 'gallery.html')

#: 截图按这个宽度显示（CSS 里再等比缩放；控件框用百分比定位，不受影响）
SHOT_W = 300


def load_sample(json_path: str, meta_path: str):
    with open(json_path, encoding='utf-8') as f:
        root = parse_layout(f.read())
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, encoding='utf-8') as f:
            meta = json.load(f)
    return root, meta


def nodes_payload(root, screen=(720, 1280)) -> list:
    """把控件树压成前端要用的最小结构（只保留可见且有面积的节点）。"""
    sw, sh = screen
    out = []
    for n in root.walk():
        if not n.visible or n.rect.area <= 0:
            continue
        out.append({
            't': n.type or '',
            'id': n.id or '',
            'x': n.text or '',
            'd': (n.text_deep or '')[:60] if not n.text else '',
            'c': bool(n.clickable),
            'r': [round(n.rect.left / sw * 100, 3), round(n.rect.top / sh * 100, 3),
                  round((n.rect.right - n.rect.left) / sw * 100, 3),
                  round((n.rect.bottom - n.rect.top) / sh * 100, 3)],
        })
    return out


def b64_png(path: str) -> str:
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('ascii')


def build(samples: list) -> str:
    """samples: [(name, meta, nodes, b64 或 '')]"""
    cards = []
    for name, meta, nodes, png in samples:
        inter = sum(1 for n in nodes if n['c'])
        withid = sum(1 for n in nodes if n['id'])
        withtext = sum(1 for n in nodes if n['x'])
        img = (f'<img src="data:image/png;base64,{png}" alt="{html.escape(name)}">'
               if png else '<div class="noshot">无截图</div>')
        boxes = ''.join(
            '<i class="bx{p}" style="left:{l}%;top:{t}%;width:{w}%;height:{h}%" '
            'data-t="{t_}" data-id="{i}" data-x="{x}" data-d="{d}"></i>'.format(
                p=(' c' if n['c'] else '') + (' x' if n['x'] else ''),
                l=n['r'][0], t=n['r'][1], w=n['r'][2], h=n['r'][3],
                t_=html.escape(n['t']), i=html.escape(n['id']),
                x=html.escape(n['x']), d=html.escape(n['d']))
            for n in nodes)
        cards.append(f"""
<section class="card" data-count="{len(nodes)}">
  <h3>{html.escape(name)}<span class="bundle">{html.escape(str(meta.get('bundle') or ''))}</span></h3>
  <div class="wrap">
    <div class="shot">{img}{boxes}</div>
    <div class="side">
      <table>
        <tr><td>可见节点</td><td><b>{len(nodes)}</b></td></tr>
        <tr><td>可交互</td><td><b>{inter}</b></td></tr>
        <tr><td>带文案</td><td><b>{withtext}</b></td></tr>
        <tr class="{'bad' if withid == 0 else ''}"><td>带 id</td><td><b>{withid}</b></td></tr>
      </table>
      {('<p class="warn"><b>id 数为 0</b>：纯控件树无法稳定定位这 '
        f'{inter} 个可交互控件，需要视觉与文案通道补位 —— 多模态定位的必要性所在。</p>')
       if (withid == 0 and inter > 0) else ''}
      <div class="detail">点控件框看属性</div>
    </div>
  </div>
</section>""")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ohauto 真机演示 · 多模态看到了什么</title>
<style>
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:24px auto;
     max-width:1180px;background:#fafafa;color:#1f2328;line-height:1.6}}
h1{{font-size:22px;margin:0 0 6px}} h2{{font-size:16px;margin:26px 0 10px;font-weight:500}}
.sub{{color:#57606a;font-size:13px;margin-bottom:18px}}
.kpi{{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 22px}}
.kpi div{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:8px 14px;font-size:13px}}
.kpi b{{font-size:17px;display:block}}
.card{{background:#fff;border:1px solid #d0d7de;border-radius:10px;padding:14px;margin:14px 0}}
.card h3{{font-size:15px;margin:0 0 10px;font-weight:500}}
.bundle{{color:#8c959f;font-size:12px;font-weight:400;margin-left:8px}}
.wrap{{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}}
.shot{{position:relative;width:{SHOT_W}px;flex:0 0 {SHOT_W}px;border:1px solid #d0d7de;border-radius:6px;overflow:hidden}}
.shot img{{display:block;width:100%}}
.noshot{{height:530px;display:flex;align-items:center;justify-content:center;color:#8c959f}}
.bx{{position:absolute;border:1px solid rgba(55,138,221,.85);background:rgba(55,138,221,.10);
     box-sizing:border-box;cursor:pointer}}
.bx.x{{border-color:rgba(29,158,117,.85);background:rgba(29,158,117,.10)}}
.bx:hover{{background:rgba(255,140,0,.30);border-color:#d85a30}}
.side{{flex:1;min-width:320px}}
table{{border-collapse:collapse;font-size:13px;margin-bottom:10px}}
td{{padding:3px 12px 3px 0}}
tr.bad td{{color:#cf222e}}
.warn{{background:#fff8c5;border-left:3px solid #d4a72c;padding:8px 10px;font-size:12.5px;
       border-radius:4px;margin:8px 0}}
.detail{{background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px;
         font-size:12.5px;white-space:pre-wrap;min-height:76px;font-family:ui-monospace,Consolas,monospace}}
.legend{{font-size:12.5px;color:#57606a;margin-bottom:8px}}
.legend i{{display:inline-block;width:11px;height:11px;border-radius:2px;margin:0 4px 0 12px;
          vertical-align:-1px;border:1px solid rgba(55,138,221,.85);background:rgba(55,138,221,.15)}}
.legend i.g{{border-color:rgba(29,158,117,.85);background:rgba(29,158,117,.15)}}
</style></head><body>
<h1>ohauto · 真机演示 —— 系统「看到」了什么</h1>
<div class="sub">数据来源：真机 <b>DAYU200</b>（OpenHarmony 5.0.3.135 / 720×1280）实采，
截图与控件树<b>同一坐标系</b>，所以可以把布局通道的识别结果直接画在截图上。</div>
<div class="legend"><i></i>可交互控件<i class="g"></i>带文案的控件　（点框看属性；截图与控件树均为真机原始数据）</div>
<div class="kpi">{''.join(
    f'<div><b>{len(s[2])}</b>{html.escape(s[0])} 可见节点</div>' for s in samples)}</div>
{''.join(cards)}
<script>
document.querySelectorAll('.shot').forEach(shot=>{{
  const panel = shot.parentElement.querySelector('.detail');
  shot.querySelectorAll('.bx').forEach(bx=>{{
    bx.addEventListener('click', ()=>{{
      const d = bx.dataset;
      panel.textContent =
        'type : ' + (d.t || '-') + '\\n' +
        'id   : ' + (d.i || '（空）') + '\\n' +
        'text : ' + (d.x || '（空）') + '\\n' +
        (d.d ? 'deep : ' + d.d + '\\n' : '') +
        'bounds: left ' + bx.style.left + '  top ' + bx.style.top +
        '  w ' + bx.style.width + '  h ' + bx.style.height;
    }});
  }});
}});
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=DEFAULT_SRC)
    ap.add_argument('-o', '--out', default=OUT)
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        print('样本目录不存在: %s' % args.src)
        return 1

    names = sorted(f[:-5] for f in os.listdir(args.src) if f.endswith('.json')
                   and not f.endswith('.meta.json'))
    samples = []
    for n in names:
        jp = os.path.join(args.src, n + '.json')
        mp = os.path.join(args.src, n + '.meta.json')
        pp = os.path.join(args.src, n + '.png')
        root, meta = load_sample(jp, mp)
        nodes = nodes_payload(root)
        png = b64_png(pp) if os.path.isfile(pp) else ''
        samples.append((n, meta, nodes, png))
        print('  %-16s 节点 %-4d 可交互 %-3d id %-3d 文案 %-3d 截图 %s'
              % (n, len(nodes), sum(1 for x in nodes if x['c']),
                 sum(1 for x in nodes if x['id']), sum(1 for x in nodes if x['x']),
                 '有' if png else '无'))

    out_html = build(samples)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(out_html)
    size = os.path.getsize(args.out) / 1024.0
    print('\n样本 %d 个 → %s（%.0f KB，自包含）' % (len(samples), args.out, size))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
