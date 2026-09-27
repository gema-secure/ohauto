"""演示材料 · 一页纸速览（可打印 A4 / 可转 PDF / 可分发的自包含 HTML）。

定位：答辩开场或邮件附件用的「一页看懂」。三块演示材料里的第一块 ——
  1. **本页**：一页纸速览（全局 + 关键数字）
  2. `tools/demo_gallery.py`：真机截图 + 控件树叠加画廊（"系统看到了什么"）
  3. `tools/demo_full_chain.py`：可现场跑的一键整链演示

⚠️ **数字口径**：本页所有数字的权威出处是 `docs/指标汇总-2026-09-24.md`
（那里每条都带复现命令；09-23 版**已过期**，顶部有红色指针，别引用）。
改数字时**文件与脚本两处一起改**，否则会出现两个版本。

2026-09-25 同步：单元测试 1035→**1092**（+24 项融合钉子、+12 项沉淀钉子）、
新增 L2/L3 用例生成数字、挑战 5 补「归因结论带 locator_id」。

用法::

    python tools/demo_onepager.py
    python tools/demo_onepager.py -o _out/demo_material/onepager.html
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
#: 同 demo_gallery：产物落仓库内的 docs/demo/，随仓库提交
OUT = os.path.join(ROOT, 'docs', 'demo', 'onepager.html')

#: 关键数字（与 docs/指标汇总-2026-09-24.md 同步，09-25 复跑核定）
KPI = [
    ('1092', '单元测试 OK'),
    ('88%', '覆盖率'),
    ('94.7%', '视觉一致性'),
    ('83.3%', 'NL→可执行用例'),
    ('77.8%', '真机步骤通过'),
    ('7', '真机应用样本'),
    ('13', '命题条目'),
]

#: 真机证据（实采，见 datasets/real_samples_20260919/）
EVIDENCE = [
    ('744', '个可见节点', '7 个应用样本合计，截图与控件树同坐标系'),
    ('12 / 0', '可交互 / id', 'com.ohos.settings：12 个可交互控件，**id 数为 0** '
                              '—— 纯控件树无法稳定定位，必须靠视觉与文案通道'),
    ('0', '个可交互节点', 'etsclock：纯 Canvas 应用，控件树里**一个可交互控件都没有** '
                          '—— 视觉通道是唯一出路'),
    ('1.0 / 0.95', '崩溃 / 无窗口 判据置信度', 'kill -11 受控注入与息屏注入，真机实测'),
    ('21/27', 'L3 真机步骤通过', '生成的用例在真机上**真跑一遍**：步骤级 77.8%；'
                                 '整条全过的 2 条**都是空壳用例**，真引用控件的 3 条 0/3 '
                                 '—— 主动暴露「可执行 ≠ 做了事」，并已派活收口'),
]

HIGHLIGHTS = [
    ('双通道定位', '截图（视觉）+ 控件树（布局）融合；'
                   '控件树拿不到的靠视觉补，视觉拿不准的用控件树交叉验证。'),
    ('差异即缺陷', '真实样本**三类成因全齐**：条件渲染 / 显式隐藏 / 越出屏幕，'
                   '装机后 4 次采集确定性复现，融合报告全部落 🔴 missing；'
                   '正常对照全部 confirmed（置信度 1.00）。'),
    ('失败归因闭环', '失败步自动归因（类别 + 置信度 + 证据 + 建议），归因结论**带 '
                     'locator_id**，回写定位器健康度，驱动自愈换代。'),
    ('可信用例生成', '自然语言 → 用例六阶段：模型只写草案，'
                     '红线（硬编码坐标 / 固定等待）由确定性校验拦下并自动修复，'
                     '再经干跑把关可执行性。'),
    ('零部署', '宿主侧 Python + hdc 直连，**不需要为被测应用打包测试 HAP**。'),
]

COVERAGE = [
    ('基础 1~5', '✅', '截图+布局识别 · ≥3 类控件 · 脚本+断言 · ≥2 场景 · 覆盖 4/4 与 5/3 双口径'),
    ('基础 6', '⚠️', '材料齐备（架构/执行结果/验证报告/用例生成说明）；**演示材料本页即其一**'),
    ('挑战 1~4', '✅', '多模态理解 · NL 生成脚本 · 自动探索+状态图 · 压测脚本'),
    ('挑战 5', '⚠️', '五类异常判据齐备 + 闭环已接（归因结论带 id）+ 两类真机正例；'
                     '自愈记账等 A 收口（派活已发，方案与验收已定）'),
    ('挑战 6', '⚠️→✅推进中', '沉淀工装已建并**真机跑通**（trace_to_case.py：'
                             '采集→沉淀→冷启动重放 4/4，产物零坐标、id 精准命中）；'
                             '探索轨迹入图（StateGraph 存 page_path）等 B 收口'),
    ('挑战 7', '✅', '已孵化为可复用工具链（tools/ + 一键整链演示）'),
]

REPRO = [
    ('全量门禁', 'python -m unittest discover -s tests -t tests -q && python tools/static_check.py'),
    ('一键整链', 'python tools/demo_full_chain.py          # 离线；--real 跑真机'),
    ('真机画廊', 'python tools/demo_gallery.py'),
    ('视觉评测', 'python tools/eval_vision_offline.py --dry'),
    ('异常注入', 'python tools/verify_signals_injection_real.py'),
    ('用例生成评测', 'python tools/eval_nl_generation_real.py --execute   # 需 key + 真机'),
]


def build() -> str:
    kpi = ''.join(f'<div class="k"><b>{v}</b><span>{t}</span></div>' for v, t in KPI)
    ev = ''.join(f'<tr><td class="n">{n}</td><td class="l">{l}</td>'
                 f'<td>{d.replace("**", "")}</td></tr>' for n, l, d in EVIDENCE)
    hi = ''.join(f'<li><b>{t}</b><span>{d}</span></li>' for t, d in HIGHLIGHTS)
    cv = ''.join(f'<tr><td>{a}</td><td class="s">{b}</td><td>{c.replace("**", "")}</td></tr>'
                 for a, b, c in COVERAGE)
    rp = ''.join(f'<tr><td>{k}</td><td><code>{v}</code></td></tr>' for k, v in REPRO)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>ohauto 一页纸速览</title>
<style>
@page{{size:A4;margin:14mm}}
body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0 auto;
     max-width:820px;padding:20px;color:#1f2328;background:#fff;line-height:1.55;font-size:13px}}
h1{{font-size:21px;margin:0 0 2px}}
.lead{{color:#57606a;font-size:13px;margin-bottom:14px}}
.kpi{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}}
.k{{border:1px solid #d0d7de;border-radius:8px;padding:6px 12px;min-width:92px}}
.k b{{font-size:17px;display:block;line-height:1.25}}
.k span{{color:#57606a;font-size:11.5px}}
h2{{font-size:14px;margin:18px 0 8px;font-weight:500;border-bottom:1px solid #d0d7de;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}}
td,th{{border-bottom:1px solid #eaeef2;padding:5px 8px 5px 0;vertical-align:top;text-align:left}}
td.n{{font-weight:500;white-space:nowrap;font-size:14px}}
td.l{{color:#57606a;white-space:nowrap}}
td.s{{width:22px;font-size:14px}}
ul{{margin:0;padding-left:18px}} li{{margin:5px 0}}
li b{{display:inline-block;min-width:104px}}
li span{{color:#24292f}}
code{{background:#f6f8fa;border:1px solid #eaeef2;border-radius:4px;padding:0 4px;font-size:11.5px}}
.foot{{margin-top:16px;color:#8c959f;font-size:11.5px;border-top:1px solid #eaeef2;padding-top:8px}}
</style></head><body>
<h1>ohauto —— 面向 OpenHarmony 的多模态智能测试系统</h1>
<div class="lead">截图（视觉）+ 控件树（布局）<b>双通道驱动</b>的 UI 自动化能力层：
自然语言直通用例 → 执行 → 失败归因 → 自愈的完整闭环，<b>设备侧零部署</b>。</div>
<div class="kpi">{kpi}</div>

<h2>真机证据（DAYU200 / OpenHarmony 5.0.3.135 / 720×1280）</h2>
<table>{ev}</table>

<h2>技术亮点</h2>
<ul>{hi}</ul>

<h2>命题覆盖</h2>
<table>{cv}</table>

<h2>复现入口</h2>
<table>{rp}</table>

<div class="foot">数字权威出处：<code>docs/指标汇总-2026-09-24.md</code>（每条带复现命令；09-23 版已过期）｜
真机样本：<code>datasets/real_samples_20260919/</code>｜
上方所有数字均可在本机当场复跑，未使用任何模拟数据冒充实测。</div>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('-o', '--out', default=OUT)
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(build())
    print('已生成一页纸: %s（%.0f KB）'
          % (args.out, os.path.getsize(args.out) / 1024.0))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
