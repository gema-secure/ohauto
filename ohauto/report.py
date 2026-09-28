"""
L4 应用层 —— 报告生成
=====================

把一次执行（Driver 留痕 或 DSL RunReport）渲染成结构化报告。
三种格式各有用途：

    JSON  —— 机器消费，接入 CI、做趋势分析
    MD    —— 人读，进 PR 评论或提交给评审
    HTML  —— 带截图的图文报告，演示与归档最直观
"""

from __future__ import annotations

import html
import json
import os
import time
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------- 采集

def collect(driver, run_report=None, extra: Optional[Dict[str, Any]] = None
            ) -> Dict[str, Any]:
    """汇总一次执行的完整信息。"""
    data: Dict[str, Any] = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'bundle': getattr(driver, 'bundle', ''),
        'ability': getattr(driver, 'ability', ''),
        'summary': driver.summary() if hasattr(driver, 'summary') else {},
        'steps': driver.steps_to_dict() if hasattr(driver, 'steps_to_dict') else [],
    }
    if run_report is not None:
        data['case'] = run_report.to_dict() if hasattr(run_report, 'to_dict') else run_report
    if extra:
        data.update(extra)
    return data


# ---------------------------------------------------------------- JSON

def _json_fallback(o: Any) -> Any:
    """非原生类型的兜底序列化。

    runner 的 `StepResult.trees` 设计上允许两种形态（文件路径 str 或
    内存态 `LayoutNode`——真机 pull 偶发竞态时快照落不了盘就退内存），
    JSON 层必须兜得住，否则**执行跑完了、报告写崩了**，100 步的现场
    全丢（B5 真机首跑实测踩到）。
    """
    if hasattr(o, 'to_dict'):
        return o.to_dict()
    if hasattr(o, 'describe'):
        return o.describe()
    return str(o)


def to_json(data: Dict[str, Any], path: str, indent: int = 2) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent,
                  default=_json_fallback)
    return path


# ---------------------------------------------------------------- Markdown

def to_markdown(data: Dict[str, Any], path: str,
                image_rel_prefix: str = '') -> str:
    s = data.get('summary', {})
    case = data.get('case', {})
    L: List[str] = []

    L.append('# UI 自动化执行报告')
    L.append('')
    L.append(f"- **应用**：`{data.get('bundle', '-')}`")
    L.append(f"- **生成时间**：{data.get('generated_at', '-')}")
    if case:
        L.append(f"- **用例**：{case.get('name') or '(未命名)'}")
    L.append('')

    L.append('## 执行概况')
    L.append('')
    L.append('| 指标 | 数值 |')
    L.append('|---|---|')
    L.append(f"| 步骤总数 | {s.get('steps_total', 0)} |")
    L.append(f"| 失败步骤 | {s.get('steps_failed', 0)} |")
    rate = s.get('success_rate', 0.0)
    L.append(f"| 成功率 | {rate * 100:.1f}% |")
    L.append(f"| 总耗时 | {s.get('total_elapsed_ms', 0)} ms |")
    L.append('')

    if case:
        L.append('## 用例判定')
        L.append('')
        L.append(f"- 通过 {case.get('passed', 0)} / 共 {case.get('total', 0)} 步")
        L.append(f"- 结论：**{'通过' if case.get('ok') else '未通过'}**")
        L.append('')

    # ---- 失败步归因（挑战 #5 闭环，2026-09-23）
    # 数据来自 case.steps[].verdict（runner 在失败分支上挂的 diagnose 结论）。
    # 为什么单列一节而不是塞进步骤表：归因的价值在**证据**与**建议**，
    # 表格里一行放不下；而且「没做归因」与「归因为空」必须看得出区别 ——
    # 这里没 verdict 就不输出这一节，不制造空白标题。
    _diag_steps = [st for st in (case.get('steps') or [])
                   if isinstance(st, dict) and st.get('verdict')]
    if _diag_steps:
        L.append('## 失败步归因')
        L.append('')
        for st in _diag_steps:
            v = st.get('verdict') or {}
            L.append(f"### 步骤 {st.get('index')} — "
                     f"{v.get('category_cn', v.get('category', '?'))}"
                     f"（置信度 {v.get('confidence', '-')}）")
            L.append('')
            if v.get('suggestion'):
                L.append(f"- **建议**：{v['suggestion']}")
            if v.get('locator_id'):
                L.append(f"- 定位器：`{v['locator_id']}`")
            if v.get('signals_used'):
                L.append(f"- 用到的信号：{', '.join(v['signals_used'])}")
            for e in (v.get('evidence') or []):
                L.append(f"- 证据：{e}")
            L.append('')

    failed = s.get('failed_steps') or []
    if failed:
        L.append('## 失败明细')
        L.append('')
        for f_ in failed:
            L.append(f"### 步骤 {f_.get('index')} — {f_.get('kind')}")
            L.append('')
            L.append(f"- 目标：`{f_.get('target', '-')}`")
            if f_.get('error'):
                L.append(f"- 错误：{f_['error']}")
            if f_.get('screenshot'):
                img = _rel(f_['screenshot'], image_rel_prefix)
                L.append(f"- 截图：![]({img})")
            # 失败时留的控件树快照（runner._capture_trees 采集、回填到 driver.Step）。
            # 排查定位问题时这是**最有用**的一份证据：截图看得出「界面长什么样」，
            # 控件树才看得出「那个控件到底在不在、id/type/层级是什么」。
            #
            # 字段名注意：driver.Step 上是 `layout_json`（失败瞬间）
            # + `layout_after`（等 200ms 后那张，用来区分「控件自始不存在」与
            # 「界面还没稳定」）。
            snaps = [p for p in (f_.get('layout_json'), f_.get('layout_after')) if p]
            if snaps:
                L.append(f'- 控件树快照（{len(snaps)} 张，失败瞬间 + 稳定后）：')
                for p in snaps:
                    L.append(f'  - `{_rel(str(p), image_rel_prefix)}`')
            L.append('')

    L.append('## 步骤明细')
    L.append('')
    L.append('| # | 动作 | 目标 | 结果 | 耗时(ms) | 坐标 |')
    L.append('|---|---|---|---|---|---|')
    for st in data.get('steps', []):
        L.append('| {i} | {k} | {t} | {r} | {e} | {c} |'.format(
            i=st.get('index', ''),
            k=st.get('kind', ''),
            t=_md_escape(str(st.get('target', ''))[:48]),
            r='通过' if st.get('ok') else '**失败**',
            e=st.get('elapsed_ms', ''),
            c=st.get('coords', '-'),
        ))
    L.append('')

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    return path


def _md_escape(s: str) -> str:
    return s.replace('|', '\\|').replace('\n', ' ')


def _rel(p: str, prefix: str) -> str:
    if not prefix:
        return p
    try:
        return os.path.relpath(p, prefix).replace('\\', '/')
    except ValueError:
        return p


# ---------------------------------------------------------------- HTML

_HTML_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>UI 自动化执行报告</title>
<style>
:root {{
  --bg:#f7f7f5; --card:#fff; --line:#e3e2dd; --tx:#22221f;
  --tx2:#6b6a64; --ok:#1a7f4b; --bad:#c0392b; --accent:#2f6fd0;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:28px; background:var(--bg); color:var(--tx);
  font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
h1 {{ font-size:20px; font-weight:500; margin:0 0 4px; }}
h2 {{ font-size:15px; font-weight:500; margin:28px 0 10px;
  padding-bottom:6px; border-bottom:1px solid var(--line); }}
.sub {{ color:var(--tx2); font-size:13px; margin-bottom:20px; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:8px; }}
.card {{ background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:14px 18px; min-width:120px; }}
.card .k {{ color:var(--tx2); font-size:12px; }}
.card .v {{ font-size:22px; font-weight:500; margin-top:2px; }}
.ok {{ color:var(--ok); }} .bad {{ color:var(--bad); }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
  border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
th,td {{ padding:9px 12px; text-align:left; border-bottom:1px solid var(--line);
  font-size:13px; vertical-align:top; }}
th {{ background:#fafaf8; color:var(--tx2); font-weight:500; }}
tr:last-child td {{ border-bottom:none; }}
tr.fail {{ background:#fdf3f2; }}
code {{ background:#f2f1ed; padding:1px 5px; border-radius:4px;
  font-family:ui-monospace,Consolas,monospace; font-size:12px; }}
.err {{ color:var(--bad); font-size:12px; }}
.shot {{ max-width:220px; border:1px solid var(--line); border-radius:6px;
  margin-top:6px; display:block; }}
details {{ margin:10px 0; }}
summary {{ cursor:pointer; color:var(--accent); font-size:13px; }}
.pill {{ display:inline-block; padding:2px 9px; border-radius:99px;
  font-size:12px; border:1px solid currentColor; }}
</style>
</head>
<body>
<h1>UI 自动化执行报告</h1>
<div class="sub">应用 <code>{bundle}</code> · 生成于 {generated_at}{case_line}</div>
{cards}
{verdict}
<h2>步骤明细</h2>
{table}
{failures}
</body>
</html>
"""


def to_html(data: Dict[str, Any], path: str) -> str:
    s = data.get('summary', {})
    case = data.get('case', {})

    rate = s.get('success_rate', 0.0) * 100
    rate_cls = 'ok' if rate >= 100 else ('bad' if rate < 80 else '')
    cards = '<div class="cards">' + ''.join([
        _card('步骤总数', s.get('steps_total', 0)),
        _card('失败步骤', s.get('steps_failed', 0),
              'bad' if s.get('steps_failed') else 'ok'),
        _card('成功率', f'{rate:.1f}%', rate_cls),
        _card('总耗时', f"{s.get('total_elapsed_ms', 0)} ms"),
    ]) + '</div>'

    verdict = ''
    if case:
        ok = case.get('ok')
        verdict = (f'<h2>用例判定</h2><p><span class="pill {"" if ok else "bad"}">'
                   f'{"通过" if ok else "未通过"}</span> '
                   f'通过 {case.get("passed", 0)} / 共 {case.get("total", 0)} 步</p>')
        # ---- 失败步归因（挑战 #5 闭环，2026-09-23）—— 证据与建议都要看得见
        _diag = [st for st in (case.get('steps') or [])
                 if isinstance(st, dict) and st.get('verdict')]
        if _diag:
            blocks = []
            for st in _diag:
                v = st.get('verdict') or {}
                ev = ''.join(f'<li>{html.escape(str(e))}</li>'
                             for e in (v.get('evidence') or []))
                blocks.append(
                    f'<details open><summary>步骤 {st.get("index")} — '
                    f'{html.escape(str(v.get("category_cn", v.get("category", "?"))))}'
                    f'（置信度 {html.escape(str(v.get("confidence", "-")))}）'
                    f'</summary>'
                    f'<p>建议：{html.escape(str(v.get("suggestion", "")))}</p>'
                    + (f'<ul class="trees">{ev}</ul>' if ev else '')
                    + '</details>')
            verdict += '<h2>失败步归因</h2>' + ''.join(blocks)

    rows = []
    for st in data.get('steps', []):
        cls = '' if st.get('ok') else ' class="fail"'
        rows.append(
            f'<tr{cls}><td>{st.get("index", "")}</td>'
            f'<td>{html.escape(str(st.get("kind", "")))}</td>'
            f'<td><code>{html.escape(str(st.get("target", ""))[:70])}</code></td>'
            f'<td>{st.get("elapsed_ms", "")}</td>'
            f'<td>{html.escape(str(st.get("coords", "-")))}</td>'
            f'<td>{"通过" if st.get("ok") else "失败"}</td></tr>')
    table = ('<table><thead><tr><th>#</th><th>动作</th><th>目标</th>'
             '<th>耗时(ms)</th><th>坐标</th><th>结果</th></tr></thead><tbody>'
             + ''.join(rows) + '</tbody></table>')

    fails = []
    for f_ in (s.get('failed_steps') or []):
        img = ''
        if f_.get('screenshot') and os.path.exists(f_.get('screenshot', '')):
            img = f'<img class="shot" src="{_img_src(f_["screenshot"])}">'
        # 控件树快照 —— 排查定位失败时比截图更直接（截图看「长什么样」，
        # 树看「控件在不在、id/type/层级是什么」）
        trees_html = ''
        snaps = [p for p in (f_.get('layout_json'), f_.get('layout_after')) if p]
        if snaps:
            links = ''.join(
                f'<li><a href="{_img_src(str(t))}">'
                f'{html.escape(os.path.basename(str(t)))}</a></li>'
                for t in snaps)
            trees_html = (f'<p>控件树快照（{len(snaps)} 张）：</p>'
                          f'<ul class="trees">{links}</ul>')
        fails.append(
            f'<details open><summary>步骤 {f_.get("index")} — '
            f'{html.escape(str(f_.get("kind", "")))} 失败</summary>'
            f'<p>目标：<code>{html.escape(str(f_.get("target", "")))}</code></p>'
            f'<p class="err">{html.escape(str(f_.get("error", "")))}</p>'
            f'{img}{trees_html}</details>')
    failures = ('<h2>失败明细</h2>' + ''.join(fails)) if fails else ''

    case_line = f' · 用例 {html.escape(str(case.get("name") or "(未命名)"))}' if case else ''
    doc = _HTML_TMPL.format(
        bundle=html.escape(str(data.get('bundle', '-'))),
        generated_at=data.get('generated_at', '-'),
        case_line=case_line, cards=cards, verdict=verdict,
        table=table, failures=failures)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(doc)
    return path


def _card(k: str, v: Any, cls: str = '') -> str:
    return (f'<div class="card"><div class="k">{html.escape(str(k))}</div>'
            f'<div class="v {cls}">{html.escape(str(v))}</div></div>')


def _img_src(p: str) -> str:
    """转成 file:// URL，保证 HTML 单独打开也能显示截图。"""
    ap = os.path.abspath(p).replace('\\', '/')
    return 'file:///' + ap.lstrip('/')


# ---------------------------------------------------------------- 一体化出口

def write_all(data: Dict[str, Any], out_dir: str, stem: str = 'report'
              ) -> Dict[str, str]:
    """同时产出 JSON / Markdown / HTML 三份报告，返回路径字典。"""
    os.makedirs(out_dir, exist_ok=True)
    return {
        'json': to_json(data, os.path.join(out_dir, f'{stem}.json')),
        'markdown': to_markdown(data, os.path.join(out_dir, f'{stem}.md'),
                                image_rel_prefix=out_dir),
        'html': to_html(data, os.path.join(out_dir, f'{stem}.html')),
    }
