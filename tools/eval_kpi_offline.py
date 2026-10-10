# -*- coding: utf-8 -*-
"""离线 KPI 评测 —— 不依赖真机，把「能离线测的 KPI」先测出来、固化成报告。

为什么要有这个工具
------------------
KPI 表（项目规划 §三）里有一批指标不需要真机：**归因准确率**与**自愈成功率**
的验收本来就是「注入式」的（FakeHdc 造故障、人工改 id），单测里已经各有验收
（`test_diagnose.TestInjectedSamples20`、`test_a_locator.TestAcceptance5Ids`）。
但单测只给 PASS/FAIL，**KPI 汇报要的是数字** —— 这个工具把验收跑一遍，
把数字、明细与口径写成结构化报告（JSON + Markdown），真机到位前就能进
指标汇总；真机到位后由 `verify_*_real.py` / `explore_coverage.py` 出真值。

三段内容
--------
1. 归因准确率   四类故障各 10 例（复用 test_diagnose 的 40 条注入样例，
                2026-09-27 A6 扩容），硬门槛 ≥ 80%
2. 自愈成功率   10 个控件 id 全改 + 结构突变触发换代（复用 test_a_locator
                的验收场景，A6 扩容 5→10），硬门槛 ≥ 80%
3. 探索冒烟     模拟设备上跑 Explorer（含 tarpit 防粘滞），出覆盖度/页数/步数
                （信息项，不做硬门槛 —— 覆盖率 KPI 的口径是「对照静态声明页面」，
                那要 `explore_coverage.py` + 真机应用）

用法::

    python tools/eval_kpi_offline.py                 # 报告写 _out/kpi_offline/
    python tools/eval_kpi_offline.py --out DIR       # 指定输出目录

退出码：两道硬门槛全过 → 0；任一不过 → 1（可直接挂进 CI 质量门禁）。
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
TESTS = os.path.join(ROOT, 'tests')
sys.path.insert(0, TESTS)

from ohauto.diagnose import diagnose, CATEGORY_CN                    # noqa: E402
from ohauto.locator import LocatorManager                            # noqa: E402

GATE = 0.80          # 项目规划 §三：归因准确率 ≥80%、定位器自愈成功率 ≥80%


# ================================================================ 1. 归因准确率

def eval_diagnose_accuracy() -> Tuple[Dict[str, Any], List[str]]:
    """跑 40 条注入样例（扩容后），返回 (结果摘要, 逐条明细行)。

    样例来源：直接调 `test_diagnose.TestInjectedSamples20` 的
    `_samples() + _extra_samples()` —— 那份注入集就是归因模块验收
    （4×5）+ 扩容（4×5）的本体，在这里重新造一份必然漂移。
    若内部接口变了（`_extra_samples` 拿不到），退回只用原 20 条，
    并在摘要里如实注明。
    """
    import test_diagnose as td

    names = unittest.TestLoader().getTestCaseNames(td.TestInjectedSamples20)
    if not names:
        raise RuntimeError('TestInjectedSamples20 里没有任何测试方法')
    inst = td.TestInjectedSamples20(names[0])
    inst.tmp = tempfile.mkdtemp(prefix='ohauto_kpi_diag_')
    try:
        samples = list(inst._samples())
        if hasattr(inst, '_extra_samples'):
            samples += list(inst._extra_samples())
    finally:
        inst.doCleanups()
    note = ('' if len(samples) >= 40 else
            '（⚠️ 只取到原 20 条 —— test_diagnose 的扩容接口变了，请同步本工具）')

    per: Dict[str, Dict[str, int]] = {}
    detail: List[str] = []
    hits = 0
    for name, record, expected in samples:
        verdict = diagnose(record)
        ok = verdict.category == expected
        hits += ok
        bucket = per.setdefault(str(expected.value), {'total': 0, 'hit': 0})
        bucket['total'] += 1
        bucket['hit'] += ok
        mark = '✓' if ok else '✗'
        detail.append(f'  [{mark}] {name}: 判为「{CATEGORY_CN[verdict.category]}」'
                      f'期望「{CATEGORY_CN[expected]}」'
                      f'（置信度 {verdict.confidence:.2f}）')
    rate = hits / len(samples) if samples else 0.0
    summary = {'samples': len(samples), 'hit': hits, 'accuracy': round(rate, 4),
               'gate': GATE, 'pass': rate >= GATE, 'note': note,
               'per_category': {k: dict(v) for k, v in per.items()}}
    return summary, detail


# ================================================================ 2. 自愈成功率

def eval_selfheal_rate() -> Tuple[Dict[str, Any], List[str]]:
    """两个验收场景（复用 test_a_locator 的舞台与判据）：

    A. 10 个控件 id 全改、结构不动 → 降级链接住（A6 扩容自 5 例；验收原文
       5 例版「成功率 ≥80%」在 TestAcceptance5Ids 原样保留）；
    B. 结构突变（降级链全灭）→ 连续失败到阈值**就地自愈换代**，新一代直连命中。
    """
    import test_a_locator as tal

    detail: List[str] = []

    # ---- 场景 A：10 个 id 改名（A6 扩容）
    m = LocatorManager()
    page = tal.ten_widget_page([f'w{i}' for i in range(1, 11)])
    specs = [m.register(c, page, page_signature='P1') for c in tal.CLUES10]
    new_page = tal.ten_widget_page([f'x{i}' for i in range(1, 11)])
    ok_a = 0
    for clue, sp in zip(tal.CLUES10, specs):
        r = m.locate(clue, new_page, page_signature='P1')
        good = bool(r) and r.level in ('id_exact', 'id_fuzzy_text',
                                       'path_type', 'repaired')
        ok_a += good
        health = m.health(sp.locator_id) if r else None
        detail.append(
            f'  [{"✓" if good else "✗"}] {clue["description"]}({clue["type"]}): '
            f'level={getattr(r, "level", "-")} '
            f'通道={getattr(r, "channel", "-")} '
            f'降级次数={getattr(health, "degrade_count", "-")}')
    rate_a = ok_a / len(tal.CLUES10)

    # ---- 场景 B：结构突变 → 阈值触发就地自愈
    root = tal.node('root', bounds=(0, 0, 720, 1280))
    col = tal.node('Column', parent=root)
    tal.node('Button', id='pay_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=col)
    m2 = LocatorManager(failure_threshold=3)
    spec_b = m2.register({'id': 'pay_btn', 'type': 'Button',
                          'description': '支付按钮'}, root, page_signature='P1')
    stack = tal.node('root2', bounds=(0, 0, 720, 1280))
    row = tal.node('Row', parent=stack)
    tal.node('Button', id='confirm_btn', bounds=(10, 10, 110, 50),
             clickable=True, parent=row)
    levels = []
    for _ in range(3):
        r = m2.locate('支付按钮', stack, page_signature='P1')
        levels.append(getattr(r, 'level', '-'))
    ok_b = levels[2] == 'repaired'
    r4 = m2.locate('支付按钮', stack, page_signature='P1')
    ok_b = ok_b and getattr(r4, 'level', '') == 'id_exact'
    detail.append(f'  [{"✓" if ok_b else "✗"}] 结构突变自愈换代: 三连击落点='
                  f'{levels} → 第 4 次直连={getattr(r4, "level", "-")} '
                  f'（第 {spec_b.generation} 代定位器）')

    rate = (ok_a + ok_b) / (len(tal.CLUES10) + 1)
    summary = {'scenario_a_10ids': {'hit': ok_a, 'total': len(tal.CLUES10),
                                    'rate': round(rate_a, 4)},
               'scenario_b_restructure': {'ok': ok_b},
               'overall_rate': round(rate, 4), 'gate': GATE,
               'pass': rate >= GATE}
    return summary, detail


# ================================================================ 3. 探索冒烟

def eval_explore_smoke(out_dir: str) -> Tuple[Dict[str, Any], List[str]]:
    """模拟设备上跑一次完整探索（含 tarpit 防粘滞）。

    信息项，不做硬门槛：覆盖率 KPI 的口径是「对照静态声明的页面数」，
    要真机应用 + `explore_coverage.py` 才算数；这里证明的是
    「探索链路离线可跑、预算守得住、tarpit 台账在工作」。
    """
    from ohauto.driver import Driver
    from ohauto.explorer import Budget, Explorer
    from ohauto.sim import FakeHdc

    sim = FakeHdc(start_page='login')
    d = Driver(bundle='com.demo.app', hdc=sim,
               artifact_dir=os.path.join(out_dir, 'artifacts'),
               verbose=False, sleep_fn=lambda _s: None)
    ex = Explorer(d, artifact_dir=d.artifact_dir, verbose=False)
    budget = Budget(max_pages=8, max_actions_per_page=6, max_seconds=60)
    ex.explore(8, budget, return_back=False)
    cov = ex.coverage
    graph_path = os.path.join(out_dir, 'graph.json')
    ex.save_graph(graph_path)

    lines = [
        f'  页面状态 {cov.pages} 个（结构归并后 {cov.pages_structural} 页），'
        f'跳转 {len(ex.graph.transitions)} 条',
        f'  覆盖度 {cov.ratio:.1%}（{cov.interactive_visited}/{cov.interactive_total}，'
        f'其中 {cov.blocked} 个危险控件被拦）',
        f'  tarpit 拦截 {len(ex.tarpit_hits)} 次（同页小变体不入队）',
        f'  预算使用：{ex.last_budget.to_dict() if ex.last_budget else "-"}',
        f'  状态图已存 {graph_path}',
    ]
    summary = {'pages': cov.pages, 'pages_structural': cov.pages_structural,
               'coverage': cov.to_dict(), 'transitions': len(ex.graph.transitions),
               'tarpit_hits': len(ex.tarpit_hits), 'budget_respected': True,
               'graph': graph_path}
    return summary, lines


# ================================================================ 主流程

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--out', default=os.path.join(HERE, '_out', 'kpi_offline'))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)

    print('=' * 70)
    print('  离线 KPI 评测（无真机） —— 归因 / 自愈 / 探索冒烟')
    print('=' * 70)

    results: Dict[str, Any] = {
        'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'gate': GATE,
        'caveat': '全部指标产自模拟设备与注入样例；真机到位后由 '
                  'verify_*_real.py / explore_coverage.py 出真值',
    }
    diag_detail: List[str] = []
    heal_detail: List[str] = []
    expl_lines: List[str] = []
    gates_ok = True

    # ---- 1 归因
    print('\n【1】失败归因准确率（40 条注入样例 · A6 扩容，硬门槛 ≥80%）')
    try:
        diag, diag_detail = eval_diagnose_accuracy()
        results['diagnose'] = diag
        for line in diag_detail:
            print(line)
        print(f"  → 准确率 {diag['accuracy']:.0%}（{diag['hit']}/{diag['samples']}），"
              f"门槛{'通过' if diag['pass'] else '未通过'}")
        gates_ok &= diag['pass']
    except Exception as e:                                   # noqa: BLE001
        results['diagnose'] = {'error': f'{type(e).__name__}: {e}'}
        print(f'  [错误] 归因评测失败：{e} —— 请同步 test_diagnose 的样例接口')
        gates_ok = False

    # ---- 2 自愈
    print('\n【2】定位器自愈成功率（10-id 改名 · A6 扩容 + 结构突变，硬门槛 ≥80%）')
    try:
        heal, heal_detail = eval_selfheal_rate()
        results['selfheal'] = heal
        for line in heal_detail:
            print(line)
        print(f"  → 综合成功率 {heal['overall_rate']:.0%}，"
              f"门槛{'通过' if heal['pass'] else '未通过'}")
        gates_ok &= heal['pass']
    except Exception as e:                                   # noqa: BLE001
        results['selfheal'] = {'error': f'{type(e).__name__}: {e}'}
        print(f'  [错误] 自愈评测失败：{e}')
        gates_ok = False

    # ---- 3 探索冒烟
    print('\n【3】探索冒烟（模拟设备，信息项）')
    try:
        expl, expl_lines = eval_explore_smoke(args.out)
        results['explore_smoke'] = expl
        for line in expl_lines:
            print(line)
    except Exception as e:                                   # noqa: BLE001
        results['explore_smoke'] = {'error': f'{type(e).__name__}: {e}'}
        print(f'  [错误] 探索冒烟失败：{e}')
        gates_ok = False

    # ---- 报告落盘
    json_path = os.path.join(args.out, 'kpi_offline.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    md_path = os.path.join(args.out, 'kpi_offline.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(_to_markdown(results, diag_detail, heal_detail, expl_lines))

    print('\n' + '=' * 70)
    print(f'  报告：{json_path}')
    print(f'        {md_path}')
    print(f'  总体：{"通过" if gates_ok else "未通过"}（两道硬门槛：归因、自愈）')
    print('=' * 70)
    return 0 if gates_ok else 1


def _to_markdown(results: Dict[str, Any], diag_detail: List[str],
                 heal_detail: List[str], expl_lines: List[str]) -> str:
    """把结果写成可以直接贴进指标汇总的 Markdown。"""
    d = results.get('diagnose', {})
    h = results.get('selfheal', {})
    e = results.get('explore_smoke', {})
    out = ['# 离线 KPI 评测报告（无真机）', '',
           f'> 生成时间：{results.get("generated_at", "-")}；'
           f'硬门槛：归因 / 自愈 ≥ {results.get("gate", 0.8):.0%}',
           f'> ⚠️ {results.get("caveat", "")}', '']
    if 'accuracy' in d:
        out += ['## 失败归因准确率', '',
                f'**{d["accuracy"]:.0%}**（{d["hit"]}/{d["samples"]}），'
                f'门槛{"✅ 通过" if d["pass"] else "❌ 未通过"}', '']
        for cat, v in d.get('per_category', {}).items():
            out.append(f'- {cat}：{v["hit"]}/{v["total"]}')
        out += ['', '<details><summary>逐条明细</summary>', '', '```']
        out += diag_detail
        out += ['```', '</details>', '']
    if 'overall_rate' in h:
        a = h['scenario_a_10ids']
        out += ['## 定位器自愈成功率', '',
                f'**{h["overall_rate"]:.0%}**，门槛'
                f'{"✅ 通过" if h["pass"] else "❌ 未通过"}',
                f'- 场景 A（10 个 id 改名，降级链接住；A6 扩容自 5 例）：'
                f'{a["hit"]}/{a["total"]}',
                f'- 场景 B（结构突变，就地换代）：'
                f'{"✅" if h["scenario_b_restructure"]["ok"] else "❌"}', '',
                '<details><summary>逐条明细</summary>', '', '```']
        out += heal_detail
        out += ['```', '</details>', '']
    if e and 'error' not in e:
        out += ['## 探索冒烟（信息项）', ''] + expl_lines + ['']
    for key in ('diagnose', 'selfheal', 'explore_smoke'):
        if isinstance(results.get(key), dict) and 'error' in results[key]:
            out += [f'## {key}', '', f'❌ {results[key]["error"]}', '']
    return '\n'.join(out)


if __name__ == '__main__':
    raise SystemExit(main())
