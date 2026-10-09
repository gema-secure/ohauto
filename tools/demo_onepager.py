"""演示材料 · 一页纸速览（自包含 HTML，可打印 A4 / 可转 PDF）。

配套演示材料：
  1. 本页：一页纸速览（全局 + 关键数字）
  2. `tools/demo_gallery.py`：截图 + 控件树叠加画廊（"系统看到了什么"）
  3. `tools/demo_full_chain.py`：可现场执行的一键整链演示

维护约定：本页数字与 README / docs 保持一致；修改数字时同步更新本脚本
并重新生成，不直接手改 HTML。

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
#: 每个数字须与 README 及项目指标档案保持一致；修改时同步更新。
KPI = [
    ('1384', '单元测试 OK'),
    ('91%', '覆盖率'),
    ('95.0%', '视觉一致性'),
    ('92.3%', 'NL→用例可执行'),
    ('220 轮', '长稳 0 失败'),
    ('7', '真机应用样本'),
]

#: 真机证据（实采，见 datasets/gallery_13app/）
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

<h2>复现入口</h2>
<table>{rp}</table>

<div class="foot">口径说明：视觉一致性为与控件树标注的一致性（IoU@0.5），非绝对准确率；
NL→用例可执行率分母为 26（24 条正样本 + 2 条固定负样本，负样本被校验闸拦截），
两轮复跑结果一致；用例生成真机执行两轮并列（用例级 23/24 与 22/24）。
全部数字均可在本机以仓库内命令复现，复现方式见 README。</div>
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
