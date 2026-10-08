"""演示材料 · 一页纸速览（可打印 A4 / 可转 PDF / 可分发的自包含 HTML）。

定位：答辩开场或邮件附件用的「一页看懂」。三块演示材料里的第一块 ——
  1. **本页**：一页纸速览（全局 + 关键数字）
  2. `tools/demo_gallery.py`：真机截图 + 控件树叠加画廊（"系统看到了什么"）
  3. `tools/demo_full_chain.py`：可现场跑的一键整链演示

⚠️ **数字口径**：本页所有数字的权威出处是 `项目内部指标档案（仓库外）`
（那里每条都带复现命令；09-23 版**已过期**，顶部有红色指针，别引用）。
改数字时**文件与脚本两处一起改**，否则会出现两个版本。

2026-09-25 同步：单元测试 1035→**1092**（+24 项融合钉子、+12 项沉淀钉子）、
新增 L2/L3 用例生成数字、挑战 5 补「归因结论带 locator_id」。

2026-10-08 同步（**本页多处数字与表述此前已过期，这次一并订正**）：
  * 单元测试 1092→**1383**（含本轮新增的取树记账 / 样本量 / 描述文件钉子共 22 项）、
    覆盖率 88%→**91%**（`coverage report` TOTAL：7958 语句 / 747 未覆盖）；
  * 视觉一致性 94.7%→**95.0%**（B13 真机复测 19/20；扩充批 100%，与 09-24 基线同量级）；
  * 删掉已过期的「L3 步骤级 77.8%（21/27）」与两处「**等 A/B 收口**」——
    B8 与自愈**都已收口**，换成 10-08 的真机结论（自愈换通道 A/B/C/D 全 PASS）；
  * 新增 10-08 三项硬能力：**取树提效（−26%）**、**自愈视觉换通道**、**按页控件清单修 B8 断言目标**；
  * **B8 补到规划要求的 20 条样本口径**：24 条正样本 + 2 条固定负样本，
    L2 可执行 **24/26 = 92.3%**（真引用控件/断言 24/24，**空壳 0 条**），
    L3 真机**用例级 23/24 = 95.8%**、**步骤级 176/177 = 99.4%**
    —— 详见 `项目内部评测档案（仓库外）`（含唯一失败步的根因：控件树读不到「0」）。

⚠️ 再次强调口径：本页「视觉一致性 / 定位」类数字都是**与控件树标注的一致性**，
**不是绝对准确率**（项目没有人工真值标注集）—— 对外必须按这个口径讲。

用法::

    python tools/demo_onepager.py
    python tools/demo_onepager.py -o _out/demo_material/onepager.html
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
#: 同 demo_gallery：产物落仓库内的 docs/demo/，随仓库提交
OUT = os.path.join(ROOT, 'docs', 'demo', 'onepager.html')

#: 关键数字（与 项目内部指标档案（仓库外） 同步，10-08 复核）
#: ⚠️ 每个数字都要能在指标汇总里找到对应行；改这里必须同时改那边。
KPI = [
    ('1383', '单元测试 OK'),
    ('91%', '覆盖率'),
    ('95.0%', '视觉一致性'),
    ('92.3%', 'NL→用例可执行'),
    ('220 轮', '长稳 0 失败'),
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
    ('92.3%', 'NL→用例 L2 可执行（26 条）', '规划要求的 **20 条样本口径**已补齐：'
                                          '24 条正样本 + 2 条固定负样本；可执行的 24 条'
                                          '**全部真引用控件/断言（空壳 0 条）**，'
                                          '两条负样本被两道闸正确拦下'),
    ('23/24 · 22/24', 'NL→用例 L3 真机用例级（两轮）', '步骤级 **176/177 与 170/172**；'
                                                  '两轮挂掉的两条**同属「断言/期望落在控件树之外」**：'
                                                  '① 计算器**没把「0」写进控件树**（视觉是 0、树里是空串，'
                                                  '应用侧可访问性缺口）② 模型自己加了「前导运算符进表达式」'
                                                  '的断言（计算器本就忽略）—— 都不是机制问题'),
    ('A/B/C/D', '自愈降级链真机 PASS', '五档线索全灭时**自动换视觉通道定人**'
                                       '（非 uncertain / conf≥0.4 / 唯一映射 IoU≥0.3 三道闸），'
                                       '自愈后直连命中 id_exact'),
    ('−26%', '同一用例墙钟（取树提效）', 'dumpLayout 与 cat **合并成一次设备往返** + '
                                        '只读步骤间复用同一张树（动作步骤前仍必重取）：'
                                        '21488→15913ms，取树 12→9 次'),
]

HIGHLIGHTS = [
    ('双通道定位', '截图（视觉）+ 控件树（布局）融合；'
                   '控件树拿不到的靠视觉补，视觉拿不准的用控件树交叉验证。'),
    ('差异即缺陷', '真实样本**三类成因全齐**：条件渲染 / 显式隐藏 / 越出屏幕，'
                   '装机后 4 次采集确定性复现，融合报告全部落 🔴 missing；'
                   '正常对照全部 confirmed（置信度 1.00）。'),
    ('失败归因闭环', '失败步自动归因（类别 + 置信度 + 证据 + 建议），归因结论**带 '
                     'locator_id**，回写定位器健康度，驱动自愈换代。'),
    ('自愈会自己变好', '连续失败触发重探索换代，五档线索（control_key / 子树文案 / '
                       '自带文案 / 池剩一 / **视觉换通道**）逐级尝试，'
                       '验证不过则**真回滚**（快照含全部线索字段）。'),
    ('可信用例生成', '自然语言 → 用例两阶段生成：模型只写草案，'
                     '红线（硬编码坐标 / 固定等待）由确定性校验拦下并自动修复，'
                     '控件存在性按**页并集**校验，再经干跑把关可执行性。'),
    ('树复用提效', '只读步骤之间复用同一张控件树（动作步骤前必重取，红线不破），'
                   '并把 dumpLayout+cat 合并为一次往返 —— 同一条 10 步用例墙钟 **−26%**。'),
    ('懂鸿蒙多形态', '折叠屏 / 平板 / 2in1 形态档案（68 台设备、11 类型），'
                     '双态四形态差异比对 + 四类判据（缺失/溢出/…）+ 根因聚类。'),
    ('零部署', '宿主侧 Python + hdc 直连，**不需要为被测应用打包测试 HAP**。'),
]

COVERAGE = [
    ('基础 1~5', '✅', '截图+布局识别 · ≥3 类控件 · 脚本+断言 · ≥2 场景 · 覆盖 4/4 与 5/3 双口径'),
    ('基础 6', '⚠️', '材料齐备（架构/执行结果/验证报告/用例生成说明）；**演示材料本页即其一**'),
    ('挑战 1~4', '✅', '多模态理解 · NL 生成脚本 · 自动探索+状态图 · 压测脚本'),
    ('挑战 5', '✅', '五类异常判据齐备 + 归因闭环（结论带 locator_id）+ 两类真机正例；'
                     '**自愈已收口**：降级链真机树场景 A/B/C/D 全 PASS（含视觉换通道）'),
    ('挑战 6', '✅', '沉淀工装真机跑通（trace_to_case.py：采集→沉淀→冷启动重放 4/4，'
                     '产物零坐标、id 精准命中）；探索轨迹入图已按 **page_path 新口径**'
                     '验证（页面覆盖 3/3=100%）'),
    ('挑战 7', '✅', '已孵化为可复用工具链（tools/ + 一键整链演示）'),
]

REPRO = [
    ('全量门禁', 'python -m unittest discover -s tests -t tests -q && python tools/static_check.py'),
    ('一键整链', 'python tools/demo_full_chain.py          # 离线；--real 跑真机'),
    ('真机画廊', 'python tools/demo_gallery.py'),
    ('视觉评测', 'python tools/eval_vision_offline.py --dry'),
    ('异常注入', 'python tools/verify_signals_injection_real.py'),
    ('降级链验证', 'python tools/verify_locator_degrade.py          # 真机树 A/B/C/D'),
    ('用例生成评测', 'python tools/eval_nl_generation_real.py --bundle ohos.samples.distributedcalc '
                     '--max 20 --require-samples 20 --execute --prompts tools/b8_prompts_calc.txt'
                     '   # 需 key + 真机；本轮 24 正 + 2 负 = 92.3%'),
]


def _em(s: str) -> str:
    """把正文里的 `**强调**` 转成 `<b>`。

    原先两处是直接 `.replace('**', '')`（把强调**丢掉**），而技术亮点那一列
    根本没处理 —— 页面上会**原样显示星号**。这里统一：强调要看得见，星号不出现。
    """
    return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)


def build() -> str:
    kpi = ''.join(f'<div class="k"><b>{v}</b><span>{t}</span></div>' for v, t in KPI)
    ev = ''.join(f'<tr><td class="n">{n}</td><td class="l">{l}</td>'
                 f'<td>{_em(d)}</td></tr>' for n, l, d in EVIDENCE)
    hi = ''.join(f'<li><b>{t}</b><span>{_em(d)}</span></li>' for t, d in HIGHLIGHTS)
    cv = ''.join(f'<tr><td>{a}</td><td class="s">{b}</td><td>{_em(c)}</td></tr>'
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
<div class="lead">截图（视觉）+ 控件树（布局）<b>双通道驱动</b>的 UI 自动化能力层
（静态源码 / OCR 作为可插拔补充源，按 <code>Claim</code> 契约融合，源不可用必须显式降级）：
自然语言直通用例 → 执行 → 失败归因 → 定位器自愈 → 跨形态比对，<b>设备侧零部署</b>。</div>
<div class="kpi">{kpi}</div>

<h2>真机证据（DAYU200 / OpenHarmony 5.0.3.135 / 720×1280）</h2>
<table>{ev}</table>

<h2>技术亮点</h2>
<ul>{hi}</ul>

<h2>命题覆盖</h2>
<table>{cv}</table>

<h2>复现入口</h2>
<table>{rp}</table>

<div class="foot">数字权威出处：<code>项目内部指标档案（仓库外）</code>（每条带复现命令，10-08 更新；09-23 版已过期）｜
真机样本：<code>datasets/real_samples_20260919/</code>｜
⚠️ <b>口径声明</b>：本页「视觉一致性」是<b>与控件树标注的一致性</b>（非绝对准确率，项目无人工真值标注集）；
「取树提效」是<b>宿主侧调用口径</b>（dumpLayout+cat 已合并为一次设备往返）；
端到端单次取树约 1s 是<b>平台常数</b>，故 KPI 只对宿主侧提阈值；
B8 的 92.3% <b>分母是 26</b>（24 条正样本 + 2 条固定负样本，负样本被拦下才算对），
且正样本 = <b>19 条真机树自动派生 + 5 条人工补充</b>（多步描述，出处见
<code>tools/b8_prompts_calc.txt</code>）；L2 <b>两轮一致</b>、L3 两轮并列（23/24 与 22/24），
单应用单设备、未做更多轮取稳定值。｜
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
