"""跨形态差异报告渲染
========================

把 `crossform.CompareReport` 渲染成 Markdown / JSON / HTML 三种格式。

设计要点
--------
**① 渲染是纯函数。** 输入一个 `CompareReport`，输出字符串/文件路径，
不碰设备、不重算差异。这样报告内容可以被单测精确断言。

**② 四类差异固定成表，** 即使某类为 0 也保留行 —— 因为
「这一类**查过了**且为 0」和「这一类**没查**」在验收语境下是两回事。
0 值行是「已覆盖」的证据。

**③ 每条差异都给出可复核的证据列。** 报告里出现「元素越界」却看不到
坐标，读的人无法判断是真问题还是解析错误 —— 所以坐标、判据阈值、
命中的挖孔矩形都要落进表里。

用法
----
    from ohauto.crossform_report import write_all
    write_all(report, out_dir='_out/crossform', stem='mate_x7')
"""

from __future__ import annotations

import html as _html
import json
import os
from typing import Any, Dict, List, Optional

from .crossform import CompareReport, DiffKind, Severity

__all__ = ['to_json', 'to_markdown', 'to_html', 'write_all']


#: 四类的展示元数据：中文名 + 判据说明。报告里显式写出判据，
#: 是为了让读的人能验证「引擎是不是按我以为的规则判的」。
_KIND_META: Dict[DiffKind, Dict[str, str]] = {
    DiffKind.MISSING: {
        'label': '元素缺失',
        'criteria': '同一身份的元素在目标形态控件树中**找不到**',
    },
    DiffKind.OUT_OF_SCREEN: {
        'label': '越界',
        'criteria': '元素 bounds **超出屏幕边界**（按裁切像素分级）；'
                    '与屏幕完全无交集时为高严重度',
    },
    DiffKind.UNREACHABLE: {
        'label': '不可达',
        'criteria': '元素**中心点**落在挖孔/异形区，或元素被裁切后'
                    '**可见区不足原面积 1%** → 看得见但点不中',
    },
    DiffKind.OVERFLOW: {
        'label': '溢出',
        'criteria': '元素 bounds 超出**父容器** bounds（containment 破坏）',
    },
}

_SEV_MARK = {
    Severity.HIGH: '高',
    Severity.MEDIUM: '中',
    Severity.LOW: '低',
}


def _esc(s: Any) -> str:
    """Markdown 表格单元格转义。"""
    return str(s).replace('|', '\\|').replace('\n', ' ')


def _fmt_bounds(b: Optional[List[int]]) -> str:
    if not b or len(b) != 4:
        return '-'
    return f'[{b[0]},{b[1]},{b[2]},{b[3]}]'


def _fmt_size(sz) -> str:
    return f'{sz[0]}×{sz[1]}'


# ================================================================ JSON


def to_json(report: CompareReport, path: str, indent: int = 2) -> str:
    """落盘 JSON。结构 = `CompareReport.to_dict()` 原样，不额外包装。"""
    _ensure_dir(path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=indent)
    return path


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)


# ================================================================ Markdown


def to_markdown(report: CompareReport, path: str,
                generated_at: Optional[str] = None) -> str:
    """渲染 Markdown 报告。"""
    L: List[str] = []
    counts = report.counts()

    L.append('# 跨形态适配差异报告')
    L.append('')
    L.append(f'- **基准形态**：`{report.baseline_name}` '
             f'（{_fmt_size(report.baseline_size)}，'
             f'{report.baseline_elements} 个元素）')
    L.append(f'- **对比形态**：`{report.target_name}` '
             f'（{_fmt_size(report.target_size)}，'
             f'{report.target_elements} 个元素）')
    if generated_at:
        L.append(f'- **生成时间**：{generated_at}')
    L.append('')

    # ---- 结论先行
    total = sum(counts.values())
    L.append('## 结论')
    L.append('')
    if total == 0:
        L.append('两种形态下**未检出适配差异**。')
        L.append('')
        L.append('> ⚠️ 「未检出」不等于「没问题」。请先确认：'
                 '① 被测应用确实做了响应式布局；'
                 '② 身份策略适配该应用（若应用大面积缺 `id`，'
                 '身份会退化到文本/层级，可能掩盖真实差异）。')
    else:
        high = sum(1 for d in report.differences
                   if d.severity is Severity.HIGH)
        L.append(f'共检出 **{total}** 处差异，其中高严重度 **{high}** 处。')
    L.append('')

    # ---- 四类汇总（0 也保留，作为「已覆盖」的证据）
    L.append('## 差异汇总')
    L.append('')
    L.append('| 差异类型 | 数量 | 判据 |')
    L.append('|---|---:|---|')
    for kind in DiffKind:
        meta = _KIND_META[kind]
        L.append(f'| {meta["label"]} | {counts[kind.value]} | '
                 f'{meta["criteria"]} |')
    L.append('')

    if report.warnings:
        L.append('## 需要人工确认的告警')
        L.append('')
        for w in report.warnings:
            L.append(f'- {w}')
        L.append('')

    # ---- 明细（按类型分节，读的人一次只看一类）
    if total:
        L.append('## 差异明细')
        L.append('')
        for kind in DiffKind:
            items = report.by_kind(kind)
            if not items:
                continue
            meta = _KIND_META[kind]
            L.append(f'### {meta["label"]}（{len(items)} 处）')
            L.append('')
            L.append('| 元素身份 | 名称 | 严重度 | 基准 bounds | '
                     '目标 bounds | 说明 |')
            L.append('|---|---|---|---|---|---|')
            for d in items:
                L.append('| `{i}` | {n} | {s} | {b} | {t} | {d} |'.format(
                    i=_esc(d.identity),
                    n=_esc(d.label)[:24] or '-',
                    s=_SEV_MARK.get(d.severity, '?'),
                    b=_fmt_bounds(d.baseline_bounds),
                    t=_fmt_bounds(d.target_bounds),
                    d=_esc(d.detail),
                ))
            L.append('')
            # 判据出处：让读的人能复核引擎的判定规则
            L.append(f'> 判据：{meta["criteria"]}')
            L.append('')

    L.append('---')
    L.append('')
    L.append('*由 `ohauto.crossform` 生成。'
             '身份策略：id → type+文本 → type+层级路径（三级降级）。*')

    _ensure_dir(path)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    return path


# ================================================================ HTML


_CSS = """
:root{--bg:#f7f8fa;--fg:#1f2329;--muted:#646a73;--line:#e5e6eb;
--card:#fff;--high:#d4380d;--mid:#d48806;--low:#8c8c8c;
--missing:#d4380d;--oos:#d48806;--unreach:#722ed1;--overflow:#096dd9;}
*{box-sizing:border-box}
body{margin:0;padding:28px;font:14px/1.7 -apple-system,"Segoe UI",
"Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:26px 0 10px;padding-left:9px;
border-left:3px solid var(--overflow)}
h3{font-size:15px;margin:20px 0 8px}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px 16px;min-width:132px}
.card .k{color:var(--muted);font-size:12px}
.card .v{font-size:20px;font-weight:600;margin-top:2px}
.card.high .v{color:var(--high)}
.card.missing{border-left:3px solid var(--missing)}
.card.oos{border-left:3px solid var(--oos)}
.card.unreach{border-left:3px solid var(--unreach)}
.card.overflow{border-left:3px solid var(--overflow)}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden;
font-size:13px;margin:8px 0}
th{background:#fafafa;text-align:left;font-weight:600;color:var(--muted);
padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
code{background:#f2f3f5;padding:1px 5px;border-radius:3px;
font-family:Consolas,Monaco,monospace;font-size:12px}
.sev{font-weight:600}
.sev.HIGH{color:var(--high)}.sev.MEDIUM{color:var(--mid)}
.sev.LOW{color:var(--low)}
.warn{background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;
padding:12px 16px;margin:12px 0;font-size:13px}
.warn ul{margin:6px 0 0;padding-left:20px}
.crit{color:var(--muted);font-size:12px;margin:6px 0 0}
.ok{background:#f6ffed;border:1px solid #b7eb8f;border-radius:8px;
padding:14px 16px;margin:12px 0}
"""


def to_html(report: CompareReport, path: str,
            generated_at: Optional[str] = None) -> str:
    """渲染自带样式的单文件 HTML 报告（离线可看，无外部依赖）。"""
    counts = report.counts()
    total = sum(counts.values())
    high = sum(1 for d in report.differences if d.severity is Severity.HIGH)
    e = _html.escape

    P: List[str] = []
    P.append('<!DOCTYPE html><html lang="zh-CN"><head>'
             '<meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>跨形态适配差异报告 - {e(report.baseline_name)} '
             f'vs {e(report.target_name)}</title>'
             f'<style>{_CSS}</style></head><body>')

    P.append('<h1>跨形态适配差异报告</h1>')
    P.append(f'<div class="sub">基准 <b>{e(report.baseline_name)}</b> '
             f'({_fmt_size(report.baseline_size)}) → 对比 '
             f'<b>{e(report.target_name)}</b> '
             f'({_fmt_size(report.target_size)})'
             + (f' ｜ 生成于 {e(generated_at)}' if generated_at else '')
             + '</div>')

    # ---- 指标卡
    P.append('<div class="cards">')
    P.append(f'<div class="card{" high" if high else ""}">'
             f'<div class="k">差异总数</div><div class="v">{total}</div></div>')
    for kind in DiffKind:
        cls = {DiffKind.MISSING: 'missing',
               DiffKind.OUT_OF_SCREEN: 'oos',
               DiffKind.UNREACHABLE: 'unreach',
               DiffKind.OVERFLOW: 'overflow'}[kind]
        P.append(f'<div class="card {cls}">'
                 f'<div class="k">{_KIND_META[kind]["label"]}</div>'
                 f'<div class="v">{counts[kind.value]}</div></div>')
    P.append(f'<div class="card"><div class="k">基准元素数</div>'
             f'<div class="v">{report.baseline_elements}</div></div>')
    P.append(f'<div class="card"><div class="k">对比元素数</div>'
             f'<div class="v">{report.target_elements}</div></div>')
    P.append('</div>')

    if total == 0:
        P.append('<div class="ok"><b>未检出适配差异。</b><br>'
                 '「未检出」不等于「没问题」——请确认被测应用确实做了'
                 '响应式布局，且身份策略适配该应用'
                 '（若应用大面积缺 <code>id</code>，身份会退化到文本/层级，'
                 '可能掩盖真实差异）。</div>')
    else:
        P.append(f'<h2>结论</h2><p>共检出 <b>{total}</b> 处差异，'
                 f'其中高严重度 <b>{high}</b> 处。</p>')

    # ---- 汇总表
    P.append('<h2>差异汇总</h2><table>'
             '<tr><th>差异类型</th><th>数量</th><th>判据</th></tr>')
    for kind in DiffKind:
        m = _KIND_META[kind]
        P.append(f'<tr><td>{m["label"]}</td>'
                 f'<td>{counts[kind.value]}</td>'
                 f'<td>{_criteria_html(m["criteria"])}</td></tr>')
    P.append('</table>')

    if report.warnings:
        P.append('<h2>需要人工确认的告警</h2><div class="warn"><ul>')
        for w in report.warnings:
            P.append(f'<li>{e(w)}</li>')
        P.append('</ul></div>')

    # ---- 明细
    if total:
        P.append('<h2>差异明细</h2>')
        for kind in DiffKind:
            items = report.by_kind(kind)
            if not items:
                continue
            m = _KIND_META[kind]
            P.append(f'<h3>{m["label"]}（{len(items)} 处）</h3>')
            P.append('<table><tr><th>元素身份</th><th>名称</th>'
                     '<th>严重度</th><th>基准 bounds</th>'
                     '<th>目标 bounds</th><th>说明</th></tr>')
            for d in items:
                P.append(
                    f'<tr><td><code>{e(d.identity)}</code></td>'
                    f'<td>{e(d.label) or "-"}</td>'
                    f'<td class="sev {e(d.severity.value)}">'
                    f'{_SEV_MARK.get(d.severity, "?")}</td>'
                    f'<td><code>{_fmt_bounds(d.baseline_bounds)}</code></td>'
                    f'<td><code>{_fmt_bounds(d.target_bounds)}</code></td>'
                    f'<td>{e(d.detail)}</td></tr>')
            P.append('</table>')
            P.append(f'<p class="crit">判据：{_criteria_html(m["criteria"])}'
                     f'</p>')

    P.append('<hr><p class="crit">由 <code>ohauto.crossform</code> 生成。'
             '身份策略：id → type+文本 → type+层级路径（三级降级）。</p>')
    P.append('</body></html>')

    _ensure_dir(path)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(P))
    return path


def _criteria_html(s: str) -> str:
    """判据文本里的 `**强调**` 转成 HTML（判据表里用了 Markdown 写法）。"""
    import re as _re
    out = _html.escape(s)
    out = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', out)
    out = _re.sub(r'`(.+?)`', r'<code>\1</code>', out)
    return out


# ================================================================ 一次出三份


def write_all(report: CompareReport, out_dir: str, stem: str = 'crossform',
              generated_at: Optional[str] = None) -> Dict[str, str]:
    """一次写出 json / md / html 三份，返回 `格式 -> 路径`。"""
    os.makedirs(out_dir, exist_ok=True)
    return {
        'json': to_json(report, os.path.join(out_dir, f'{stem}.json')),
        'markdown': to_markdown(report,
                                os.path.join(out_dir, f'{stem}.md'),
                                generated_at=generated_at),
        'html': to_html(report, os.path.join(out_dir, f'{stem}.html'),
                        generated_at=generated_at),
    }
