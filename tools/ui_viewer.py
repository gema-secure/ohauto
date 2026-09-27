"""控件树交互式查看器 —— 借鉴 HapTest 的 `ui-viewer`，**零依赖前端**。

为什么要有它
------------
排查定位问题时，截图能看出「界面长什么样」，**控件树才看得出控件在不在、
id/type/层级是什么**。但 dumps 出来的 JSON 有几百个节点，用眼睛翻 JSON
是折磨。HapTest 给了一个 Web 查看器（Express 服务 + 前端），我们照这个思路
做一个**不依赖任何前端框架、不联网、单文件**的版本 —— 双击就能打开，
也方便直接放进演示材料。

技术栈上这是第三块：**Python 出数据 + HTML/JS 做交互**（前两块是 Python 主控、
Node 做静态分析）。

用法::

    # 从真机抓一份当前界面
    python tools/ui_viewer.py --dump -o view.html

    # 用已有的控件树 JSON
    python tools/ui_viewer.py --json tests/fixtures/real_20260919/app_settings.json -o view.html

产物是一个**自包含**的 HTML（数据内嵌，无 CDN、无外链），可离线打开、可转发。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto.layout import parse_layout                    # noqa: E402
from preflight import require_device, default_target                  # noqa: E402

DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
OUT = os.path.join(HERE, '_out', 'ui_viewer')


def node_to_dict(n) -> dict:
    return {
        'type': n.type or '',
        'id': n.id or '',
        'text': n.text or '',
        'descr': n.descr or '',
        'hint': n.hint or '',
        'label': n.label or '',
        'text_deep': (n.text_deep or '')[:120],
        'clickable': bool(n.clickable),
        'visible': bool(n.visible),
        'enabled': bool(n.enabled),
        'scrollable': bool(n.scrollable),
        'rect': [n.rect.left, n.rect.top, n.rect.right, n.rect.bottom],
        'children': [node_to_dict(c) for c in n.children],
    }


def build_html(root_dict: dict, title: str, note: str) -> str:
    """生成自包含 HTML —— 数据与脚本全部内嵌。"""
    data = json.dumps(root_dict, ensure_ascii=False)
    # ⚠️ 内嵌 JSON 到 <script> 时必须转义 `</script>`，否则控件文案里
    #    出现这个串会提前闭合脚本块（界面文案是不可信输入）。
    data = data.replace('</', '<\\/')
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%(title)s</title>
<style>
body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:16px;background:#fafafa;color:#1f2328}
h1{font-size:18px;margin:0 0 4px}
.note{color:#57606a;font-size:12px;margin-bottom:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
input{padding:6px 8px;border:1px solid #d0d7de;border-radius:6px;width:240px}
label{font-size:13px;display:flex;align-items:center;gap:4px}
#tree{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:10px;
      max-height:70vh;overflow:auto;font-size:13px;line-height:1.7}
ul{list-style:none;margin:0;padding-left:16px}
li{margin:1px 0}
.sel{background:#fff8c5;border-radius:4px}
.hit{background:#ddf4ff;border-radius:4px}
.tw{cursor:pointer;user-select:none}
.tw:hover{background:#f3f4f6;border-radius:4px}
.tag{font-size:11px;padding:1px 5px;border-radius:9px;margin-left:4px}
.t-click{background:#dafbe1;color:#1a7f37}
.t-danger{background:#ffebe9;color:#cf222e}
.t-invis{background:#f0f0f0;color:#6e7781}
.mut{color:#8c959f}
#detail{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:10px;
        margin-top:12px;font-size:13px;white-space:pre-wrap}
</style></head><body>
<h1>%(title)s</h1>
<div class="note">%(note)s</div>
<div class="bar">
  <input id="q" placeholder="搜索 id / text / type / label">
  <label><input type="checkbox" id="onlyInter">只看可交互</label>
  <label><input type="checkbox" id="onlyDanger">只看疑似危险</label>
</div>
<div id="tree"></div>
<div id="detail">点任意节点看它的完整属性</div>
<script>
const DATA = %(data)s;
const DANGER = /删除|移除|清空|注销|重置|恢复出厂|支付|付款|转账|下单|提交订单|确认支付/;
const el = (t,c)=>{const e=document.createElement(t); if(c) e.className=c; return e;};

function isDanger(n){ return DANGER.test((n.text||'')+' '+(n.descr||'')+' '+(n.label||'')); }

function render(n, depth){
  const li = el('li');
  const tw = el('span','tw');
  const name = n.type || '?';
  tw.innerHTML = '<span class="mut">' + '· '.repeat(Math.min(depth,12)) + '</span>'
    + esc(name)
    + (n.id ? ' <span class="mut">#' + esc(n.id) + '</span>' : '')
    + (n.text ? ' “' + esc(n.text) + '”' : (n.text_deep ? ' <span class="mut">deep:“' + esc(n.text_deep) + '”</span>' : ''))
    + (n.clickable ? '<span class="tag t-click">可点击</span>' : '')
    + (isDanger(n) ? '<span class="tag t-danger">危险</span>' : '')
    + (!n.visible ? '<span class="tag t-invis">不可见</span>' : '');
  const ul = el('ul');
  if(n.children && n.children.length){
    let open = depth < 3;
    tw.onclick = ()=>{ open = !open; ul.style.display = open ? '' : 'none'; };
    ul.style.display = open ? '' : 'none';
    for(const c of n.children) ul.appendChild(render(c, depth+1));
  }
  tw.onmouseenter = ()=>{ tw.classList.add('sel'); };
  tw.onmouseleave = ()=>{ tw.classList.remove('sel'); };
  tw.addEventListener('click', ()=>show(n));
  li.appendChild(tw); li.appendChild(ul);
  li._n = n; li._tw = tw;
  return li;
}
function esc(s){ return String(s).replace(/[&<>"]/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m])); }
function show(n){
  document.getElementById('detail').textContent =
    'type      : ' + (n.type||'-') + '\\n' +
    'id        : ' + (n.id||'-') + '\\n' +
    'text      : ' + (n.text||'-') + '\\n' +
    'text_deep : ' + (n.text_deep||'-') + '\\n' +
    'label     : ' + (n.label||'-') + '\\n' +
    'descr/hint: ' + (n.descr||'-') + ' / ' + (n.hint||'-') + '\\n' +
    'bounds    : [' + n.rect.join(',') + ']  (' + (n.rect[2]-n.rect[0]) + 'x' + (n.rect[3]-n.rect[1]) + ')\\n' +
    'clickable : ' + n.clickable + '  visible: ' + n.visible + '  scrollable: ' + n.scrollable;
}
const tree = document.getElementById('tree');
const rootUl = el('ul'); rootUl.appendChild(render(DATA,0)); tree.appendChild(rootUl);

function apply(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const oi = document.getElementById('onlyInter').checked;
  const od = document.getElementById('onlyDanger').checked;
  document.querySelectorAll('#tree li').forEach(li=>{
    const n = li._n; if(!n) return;
    let ok = true;
    if(q){ const s = ((n.id||'')+' '+(n.text||'')+' '+(n.type||'')+' '+(n.label||'')+' '+(n.text_deep||'')).toLowerCase();
           ok = s.includes(q); }
    if(ok && oi) ok = n.clickable;
    if(ok && od) ok = isDanger(n);
    li._tw.classList.toggle('hit', !!q && ok);
    li.style.display = ok ? '' : 'none';
    // 命中项把父链展开，否则筛完看不见
    if(ok && q){ let p = li.parentElement; while(p && p.tagName==='UL'){ p.style.display=''; p = p.parentElement ? p.parentElement.closest('ul') : null; } }
  });
}
document.getElementById('q').addEventListener('input', apply);
document.getElementById('onlyInter').addEventListener('change', apply);
document.getElementById('onlyDanger').addEventListener('change', apply);
</script></body></html>
""" % {'title': html.escape(title), 'note': html.escape(note), 'data': data}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', help='控件树 JSON 文件')
    ap.add_argument('--dump', action='store_true', help='从真机抓当前界面')
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('-o', '--out', default=os.path.join(OUT, 'view.html'))
    args = ap.parse_args()

    if args.dump:
        from ohauto.hdc import Hdc
        hdc = require_device(target=args.target)
        path = hdc.dump_layout()
        local = os.path.join(OUT, 'layout.json')
        try:
            # pull 成功返回本地路径，失败会抛 —— 别用返回值真假来判断
            hdc.pull(path, local)
        except Exception as e:
            print('拉取控件树失败: %s' % e)
            return 1
        src, title, note = local, '真机控件树 %s' % args.target, \
            '来源：真机 `uitest dumpLayout`（设备 %s）' % args.target
    elif args.json:
        src, title, note = args.json, '控件树 %s' % os.path.basename(args.json), \
            '来源：本地文件 %s' % args.json
    else:
        print('需要 --json 或 --dump')
        return 1               # 用法错误，不是设备不在场

    with open(src, encoding='utf-8') as f:
        root = parse_layout(f.read())
    out_html = build_html(node_to_dict(root), title, note)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(out_html)
    print('节点数: %d' % sum(1 for _ in root.walk()))
    print('已生成: %s' % args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
