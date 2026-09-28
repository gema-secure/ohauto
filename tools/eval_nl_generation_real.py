#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""挑战 #2 真实评测 —— 自然语言 → 可执行用例，**用真模型跑**，给真实可执行率。

## 这个工具解决什么问题

在此之前，挑战 #2 只有 `ScriptedProvider`（剧本）的链路验证 —— 那证明的是
「管线与校验/修复逻辑正确」，**不是模型能力指标**。本工具把口径补上：

    可执行率 = （生成出来、且通过静态校验的用例数）/（总描述数）

并且按 `RejectReason` 给出**不可执行的原因分类**（任务卡明确要求）。

## 三层口径（别混）

| 层 | 含义 | 本工具怎么给 |
|---|---|---|
| L1 生成成功 | 模型产出了结构化用例 | `total` / `ok` 计数 |
| L2 **可执行（静态）** | 通过校验：控件存在、无硬编码坐标/等待、DSL 合法 | `executable_rate` ← **主指标** |
| L3 执行通过（动态） | 真机跑一遍，步骤真的通过 | `--execute` 时的 `run_pass_rate` |

**L2 是主指标**：它是「这条用例能不能拿去执行」的直接判据，且不依赖设备波动。
L3 更硬但会受真机状态影响（弹窗、动画、时序），所以**单独报、不并进 L2**。

## 负样本（**故意混进去的两条**）

只报"可执行率"会被问「你是不是挑了好做的」。
所以默认集里固定混入 2 条**当前页面上根本不存在**的描述，
期望它们被**正确拦下**（CONTROL_MISSING）——
这既是能力（校验有用），也让报告不能被读成"全是好做的样本"。

## key 的安全约定

key 只从环境变量读，**绝不写进报告、JSON 或任何产物**；
报告里只记 base_url 与 model 名。

## 用法

    # cmd.exe
    set OHAUTO_LLM_BASE_URL=https://api.deepseek.com
    set OHAUTO_LLM_API_KEY=sk-...
    set OHAUTO_LLM_MODEL=deepseek-flash

    python tools/eval_nl_generation_real.py                       # 默认：音乐播放器
    python tools/eval_nl_generation_real.py --execute              # 加真机执行口径
    python tools/eval_nl_generation_real.py --max 6 --out _out/nl_eval
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc                                    # noqa: E402
from ohauto.driver import Driver                              # noqa: E402
from ohauto.layout import parse_layout                        # noqa: E402
from ohauto.treesum import flatten, is_interactive_for_summary  # noqa: E402
from ohauto.runner import Runner, DeviceGuard                 # noqa: E402
from ohauto.generator import (Generator, RejectReason,        # noqa: E402
                             default_provider, ProviderNotConfigured)
from preflight import require_device, default_target                  # noqa: E402

DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
# 默认靶标选**我们自己的 hypium 多页面样本**，不选系统示例应用：
# 实测 `ohos.samples.distributedmusicplayer` 的可点控件**自身文案与 id 几乎全空**
# （真机已知现象），派生出来的描述只有 1 条且是无意义的「点击 nan」
# → 评测会被负样本淹没，量不出模型能力。而这个样本控件带 id 与中文文案。
DEFAULT_BUNDLE = 'com.example.myapplication'
DEFAULT_ABILITY = 'EntryAbility'
OUT = os.path.join(HERE, '_out', 'nl_eval')

# 负样本：当前页面上不可能找到 → 期望被正确拦下（不是"模型不行"，是"校验有用"）
NEGATIVE_CONTROLS = [
    '输入用户名和密码，然后点击登录',
    '在搜索框里输入「订单」，然后点击搜索',
]


# ----------------------------------------------------------------- 采集

def _redump(hdc: Hdc, dest: str) -> Any:
    """重新拉一次控件树（点完一个控件的等待里用）。"""
    hdc.shell('uitest dumpLayout -p /data/local/tmp/eval_nl.json')
    hdc.pull('/data/local/tmp/eval_nl.json', dest)
    with open(dest, encoding='utf-8') as f:
        txt = f.read()
    if txt.lstrip()[:1] not in ('{', '['):
        raise RuntimeError(f'翻页后控件树不是 JSON：{txt[:100]!r}')
    return parse_layout(txt)


def _launch_and_capture(hdc: Hdc, bundle: str, ability: str,
                        *, attempts: int = 3) -> Any:
    """force-stop → Home → start 三步（`aa start` 会复用实例，必须三连）。

    锁屏会让 dumpLayout 返回空树且**不报错** —— 所以先 ensure_awake。
    """
    guard = DeviceGuard(hdc, verbose=False)
    if not guard.ensure_awake():
        raise RuntimeError('屏幕不可用（设备锁屏或死机）—— 请人工检查设备')

    hdc.shell('aa force-stop ' + bundle)
    time.sleep(1)
    hdc.shell('uitest uiInput keyEvent Home')
    time.sleep(1)
    hdc.shell('aa start -a ' + ability + ' -b ' + bundle)

    last = ''
    for i in range(attempts):
        time.sleep(2.5 if i == 0 else 2.0)
        hdc.shell('uitest dumpLayout -p /data/local/tmp/eval_nl.json')
        dst = os.path.join(OUT, 'live_tree_%d.json' % int(time.time() * 1000))
        hdc.pull('/data/local/tmp/eval_nl.json', dst)
        with open(dst, encoding='utf-8') as f:
            last = f.read()
        if last.lstrip()[:1] in ('{', '['):
            root = parse_layout(last)
            if len(flatten(root)) > 20:
                return root
    raise RuntimeError(
        '拉取控件树失败（不是 JSON，或节点过少）。'
        f'拿到的前 120 字符：{last[:120]!r} —— 若为 [Fail]... 请先跑 doctor.py')


# ----------------------------------------------------------------- 描述集

def _tap_text(hdc: Hdc, root: Any, text: str) -> bool:
    """在当前控件树上按文案找一个节点并点它的中心（用于评测前翻页）。

    坐标**从控件树现取**（`LayoutNode.rect.center`），不写死像素 ——
    与红线「不许吐绝对坐标」一致。
    ⚠️ 字段名是 `rect` 不是 `bounds`（实测踩过：写错字段名会让所有节点
    都"找不到"，表现为翻页永远失败且不报错）。
    """
    for n in flatten(root):
        label = (getattr(n, 'text_deep', '') or getattr(n, 'text', '') or '').strip()
        if label != text:
            continue
        cx, cy = n.center
        if cx <= 0 and cy <= 0:
            continue
        hdc.shell('uitest uiInput click %d %d' % (cx, cy))
        time.sleep(1.8)
        return True
    return False


def _has_control_ref(case: Any) -> bool:
    """用例是否**真的引用了控件或断言**。

    ★ 为什么要有这个判据（实测踩到）：校验器只校验「被引用到的」控件 ——
    所以一个只含 `start` / `waitIdle` / `screenshot` 的用例**零引用**，
    校验器无从证伪，会被判成"可执行"。
    实测：描述「输入用户名和密码，然后点击登录」（页面上没有任何登录控件）
    生成了恰好这样一条空壳用例并通过校验 → **误放**。

    这里不改 B 的生成器 KPI，而是在评测侧**另立一个更严的口径**把差距量出来。
    """
    if case is None:
        return False
    for st in case.steps or []:
        if not isinstance(st, dict):
            continue
        for k in st:
            if k in ('tap', 'click', 'input', 'inputText', 'assert', 'waitFor',
                     'swipe', 'drag', 'longPress', 'doubleClick', 'scroll'):
                return True
    return False


def build_descriptions(root: Any, maximum: int) -> List[str]:
    """从**当前真机控件树**派生 NL 描述 —— 保证是这一页上真实存在的东西。

    为什么要从真机树派生，而不是写死一串："写死的描述换个应用就全失效，
    评测结果就变成'样本与应用不匹配'，而不是'模型能力'。"
    """
    nodes = flatten(root)
    seen = set()
    out: List[str] = []
    for n in nodes:
        if len(out) >= maximum:
            break
        if not is_interactive_for_summary(n):
            continue
        text = (getattr(n, 'text_deep', '') or getattr(n, 'text', '') or '').strip()
        nid = (getattr(n, 'id', '') or '').strip()
        if not text and not nid:
            continue
        # 噪声不入描述：纯数字/时间/电量、状态栏文案、以及控件把数值当文案的
        # 情况（实测音乐播放器的进度条文案是 `nan` —— 照抄出来就是废描述）。
        if re.fullmatch(r'[\d:%×\s.]+', text) or text in ('没有 SIM 卡', 'nan'):
            continue
        # 文案太短又不像人话的（单个符号/单字母），跳过
        if text and len(text) < 2:
            continue
        key = (text, nid)
        if key in seen:
            continue
        seen.add(key)
        if text:
            out.append(f'点击「{text}」')
        else:
            out.append(f'点击 id 为 {nid} 的控件')
    return out


# ----------------------------------------------------------------- 报告

def _mask(s: str) -> str:
    """兜底脱敏：任何长得像 key 的串都打码（防止 key 意外进报告）。"""
    return re.sub(r'sk-[A-Za-z0-9]{16,}', 'sk-***MASKED***', s)


def render(report: Any, *, base_url: str, model: str, bundle: str,
           n_desc: int, neg: int, run_stats: Optional[Dict[str, Any]],
           extra: Dict[str, Any]) -> str:
    L: List[str] = []
    A = L.append
    A('# 挑战 #2 真实评测 —— 自然语言 → 可执行用例\n')
    A(f'- 被测应用：`{bundle}`')
    A(f'- 模型：`{model}` @ `{base_url}`（**key 不落盘**）')
    A(f'- 描述数：**{n_desc}**（其中 {neg} 条为**故意混入的负样本**，见文末）')
    A(f'- 时间：{time.strftime("%Y-%m-%d %H:%M:%S")}\n')

    A('## 主指标（L2：静态可执行）\n')
    A('| 指标 | 值 |')
    A('|---|---|')
    A(f'| 总描述数 | {report.total} |')
    A(f'| **可执行** | **{report.executable}** |')
    A(f'| **可执行率** | **{report.executable_rate * 100:.1f}%** |')
    ok_cases = [o.case for o in report.outcomes if o.ok and o.case]
    effective = [c for c in ok_cases if _has_control_ref(c)]
    A(f'| 其中经修复后通过 | {report.repaired} |')
    A(f'| ★ 其中**真引用了控件/断言** | **{len(effective)}**（'
      f'占比 {len(effective) / report.executable * 100:.1f}% of 可执行） |'
      if report.executable else '| ★ 真引用了控件/断言 | — |')
    A(f'| 任务卡 KPI（≥80%） | {"✅ 达标" if report.ok() else "❌ 未达标"} |')

    if report.executable and len(effective) < report.executable:
        A('\n### ⚠️ 空壳用例（**可执行 ≠ 做了事**）\n')
        A(f'有 **{report.executable - len(effective)}** 条被算作"可执行"，'
          '但它们**没有引用任何控件、也没有任何断言** —— '
          '只含 `start` / `waitIdle` / `screenshot` 这类与目标无关的步骤。\n')
        A('抽查实例（描述「输入用户名和密码，然后点击登录」，'
          '而当前页面上**不存在**用户名/密码/登录控件）：\n')
        A('```json')
        for c in ok_cases:
            if _has_control_ref(c):
                continue
            for i, st in enumerate(c.steps or [], 1):
                A(f'{i}. {json.dumps(st, ensure_ascii=False)}')
            break
        A('```\n')
        A('**为什么会漏放**：校验器只校验「被引用到的」控件 —— '
          '零引用的用例**没有可证伪的东西**，于是静默通过。\n')
        A('> 归属：`generator.py` 的校验环节（**B**）。'
          '建议补一条「用例必须至少引用一个控件或一条断言」的判据。\n')

    A('\n## 不可执行的原因分类\n')
    reasons = report.reasons_cn()
    if not reasons:
        A('（无）')
    else:
        A('| 原因 | 条数 |')
        A('|---|---|')
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
            A(f'| {k} | {v} |')

    A('\n## 逐条结果\n')
    A('| # | 描述 | 结果 | 测试点数 | 步骤数 | 原因 | 尝试 |')
    A('|---|---|---|---|---|---|---|')
    for i, o in enumerate(report.outcomes, 1):
        steps = len(o.case.steps) if (o.ok and o.case) else 0
        mark = '✅ 可执行' if o.ok else '❌ 不可执行'
        reason = o.reason.cn if o.reason else ''
        A(f'| {i} | {(o.description or "")[:34]} | {mark} | '
          f'{len(o.test_points)} | {steps} | {reason} | {o.attempts} |')

    if run_stats:
        A('\n## 附口径（L3：真机执行通过）\n')
        A('> L3 比 L2 更硬，但会受真机状态影响（弹窗/动画/时序）→ **单独报，不并进 L2**。\n')
        A('| 指标 | 值 |')
        A('|---|---|')
        for k, v in run_stats.items():
            A(f'| {k} | {v} |')

    A('\n## 负样本（**不能被读成"全是好做的样本"**）\n')
    A(f'默认集里混入了 {neg} 条**当前页面上不存在**的描述，'
      '期望被校验**正确拦下**（原因分类应为「引用的控件不存在」）：\n')
    for o in report.outcomes:
        if not o.description.startswith('输入用户名') and '搜索框' not in o.description:
            continue
        verdict = ('✅ 被正确拦下' if (not o.ok and o.reason
                                      is RejectReason.CONTROL_MISSING)
                   else '⚠️ 未被拦下（要么模型编出来了，要么分类不是 CONTROL_MISSING）')
        A(f'- `{o.description[:26]}` → {verdict}'
          + (f'（{o.reason.cn}）' if o.reason else ''))

    if extra:
        A('\n## 环境\n')
        A('| 项 | 值 |')
        A('|---|---|')
        for k, v in extra.items():
            A(f'| {k} | {v} |')

    A('\n---\n')
    A('**口径说明（对外引用必须带上）**：')
    A('1. 可执行 = 生成出来且**通过静态校验**（控件存在 / 无硬编码坐标 / DSL 合法），')
    A('   不等于"在真机上一定跑得过"；真机执行口径见 L3（若本次跑了）。')
    A('2. 描述集从**当前真机控件树**派生 + 固定负样本，**样本量小**，')
    A('   不要把百分比当成模型在任意应用上的泛化能力。')
    A('3. 模型语义判断**可能出错**（实测出现过把 OpenHarmony 页面说成 Android）'
      '——语义结论需人工复核。')
    return '\n'.join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='挑战 #2 真实评测：NL → 可执行用例')
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--max', type=int, default=8, help='从真机树派生的描述条数上限')
    ap.add_argument('--pre-tap', action='append', default=[],
                    help='采集前先点这个文案的控件（可重复，用于翻到目标页）')
    ap.add_argument('--label', default='', help='本次运行的页面标签（写进报告）')
    ap.add_argument('--prompts', default='', help='额外追加的描述文件（每行一条）')
    ap.add_argument('--execute', action='store_true',
                    help='对 L2 通过的用例在真机上跑一遍，给 L3 执行口径')
    ap.add_argument('--out', default=OUT)
    ap.add_argument('--min-executable', type=float, default=0.8,
                    help='L2 executable_rate 的验收阈值，低于它退出码给 1 '
                         '（与 eval_kpi_offline 的 80%% 闸对齐）')
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)

    # ---- provider（key 只从环境变量来）
    try:
        provider = default_provider()
    except ProviderNotConfigured as e:
        print(f'[停止] {e}')
        print('提示：本工具要的是 OHAUTO_LLM_* 三件套（真实模型）。')
        return 2

    base_url = os.environ.get('OHAUTO_LLM_BASE_URL',
                              os.environ.get('OHAUTO_VISION_BASE_URL', '')).strip()
    model = os.environ.get('OHAUTO_LLM_MODEL',
                           os.environ.get('OHAUTO_VISION_MODEL', '')).strip()
    print(f'模型: {model or "(未命名)"} @ {base_url or "(未命名)"} ｜ key 已就绪（不落盘）')

    # ---- 采集真机树
    hdc = require_device(target=args.target)
    print(f'设备: {args.target}')
    print('采集真机控件树（force-stop → Home → start）…')
    root = _launch_and_capture(hdc, args.bundle, args.ability)
    nodes = flatten(root)
    print(f'  控件树 {len(nodes)} 个节点')

    if args.pre_tap:
        dest = os.path.join(args.out, 'live_tree_%s.json'
                            % re.sub(r'\W+', '_', args.label or 'page'))
        for t in args.pre_tap:
            ok = _tap_text(hdc, root, t)
            print(f'  翻页：点击「{t}」→ {"成功" if ok else "**没找到该文案**"}')
            if not ok:
                print('  （翻页失败，继续用当前页）')
                break
            root = _redump(hdc, dest)
            nodes = flatten(root)
            print(f'    翻页后控件树 {len(nodes)} 个节点')

    descs = build_descriptions(root, args.max)
    if args.prompts and os.path.isfile(args.prompts):
        with open(args.prompts, encoding='utf-8') as f:
            descs += [ln.strip() for ln in f if ln.strip()]
    descs += NEGATIVE_CONTROLS
    if not descs:
        print('没有可用的描述（控件树里找不到可交互且带文案的控件）')
        return 2

    print(f'描述 {len(descs)} 条（含 {len(NEGATIVE_CONTROLS)} 条负样本）：')
    for i, d in enumerate(descs, 1):
        print(f'  {i:2d}. {d}')

    # ---- 生成（真模型）
    t0 = time.time()
    gen = Generator(provider=provider, bundle=args.bundle,
                    ability=args.ability, page=root)
    report = gen.generate_many(descs, page=root)
    elapsed = time.time() - t0

    print('\n' + '=' * 66)
    print(f'  可执行率（L2 静态）: {report.executable}/{report.total} = '
          f'{report.executable_rate * 100:.1f}%   '
          f'（KPI≥80% {"达标" if report.ok() else "未达标"}）')
    ok_cases_all = [o.case for o in report.outcomes if o.ok and o.case]
    effective = sum(1 for c in ok_cases_all if _has_control_ref(c))
    print(f'  ★ 其中**真引用了控件/断言**的: {effective}/{report.executable}'
          f'（空壳用例 {report.executable - effective} 条 —— 见报告「有效用例」一节）')
    print(f'  原因分类: {report.reasons_cn() or "（无）"}')
    print(f'  耗时 {elapsed:.1f}s')
    print('=' * 66)
    for i, o in enumerate(report.outcomes, 1):
        mark = 'OK ' if o.ok else 'NO '
        why = '' if o.ok else f'  ← {o.reason.cn if o.reason else "?"}'
        steps = len(o.case.steps) if (o.ok and o.case) else 0
        print(f'  [{mark}] {i:2d}. {(o.description or "")[:34]:36s} '
              f'{len(o.test_points)} 测试点 / {steps} 步{why}')

    # ---- 可选 L3：真机执行
    run_stats: Optional[Dict[str, Any]] = None
    if args.execute:
        ok_cases = [o.case for o in report.outcomes if o.ok and o.case]
        print(f'\n真机执行 L3：{len(ok_cases)} 条可执行用例 …')
        # ⚠️ Driver 第一参数是 bundle（09-25 修：写错成 Driver(hdc, ...) 会把
        #    Hdc 对象当 bundle，真机一跑就崩）；Runner 不收 driver/hdc，
        #    driver 在 run_case 时传。
        driver = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
                        artifact_dir=os.path.join(args.out, 'artifacts_l3'),
                        verbose=False)
        runner = Runner(verbose=False)
        passed_steps = total_steps = 0
        cases_ok = 0
        for c in ok_cases:
            try:
                res = runner.run_case(driver, c.to_dict())
                steps = getattr(res, 'steps', []) or []
                sp = sum(1 for s in steps if getattr(s, 'ok', False))
                passed_steps += sp
                total_steps += len(steps)
                if steps and sp == len(steps):
                    cases_ok += 1
                print(f'  {c.name or "(未命名)"}: {sp}/{len(steps)} 步通过')
            except Exception as e:
                print(f'  {c.name or "(未命名)"}: 执行异常 {type(e).__name__}: {e}')
        run_stats = {
            '可执行用例数': len(ok_cases),
            '整条用例全部步骤通过': cases_ok,
            '用例级通过率': (f'{cases_ok / len(ok_cases) * 100:.1f}%'
                             if ok_cases else '—'),
            '步骤级通过': f'{passed_steps}/{total_steps}',
            '步骤级通过率': (f'{passed_steps / total_steps * 100:.1f}%'
                             if total_steps else '—'),
        }

    # ---- 落盘
    md = _mask(render(report, base_url=base_url, model=model,
                      bundle=args.bundle, n_desc=len(descs),
                      neg=len(NEGATIVE_CONTROLS), run_stats=run_stats,
                      extra={'设备': args.target,
                             '控件树节点数': len(nodes),
                             '耗时(s)': f'{elapsed:.1f}'}))
    md_path = os.path.join(args.out, 'nl_eval.md')
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(md)
    json_path = os.path.join(args.out, 'nl_eval.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({'model': model, 'base_url': base_url,
                   'bundle': args.bundle, 'total': report.total,
                   'executable': report.executable,
                   'executable_rate': report.executable_rate,
                   'reasons': report.reasons(), 'run_stats': run_stats,
                   'outcomes': [o.to_dict() for o in report.outcomes]},
                  f, ensure_ascii=False, indent=2)
    print(f'\n[报告] {md_path}')
    print(f'[数据] {json_path}')
    # ★ KPI 未达标退出码必须给 1 —— 否则 CI 拿着「全绿」的假信号把坏数字放行（评审 P2）
    gate_pass = (report.executable_rate is not None
                 and report.executable_rate >= args.min_executable)
    print(f'[退出码] executable_rate={report.executable_rate} '
          f'阈值={args.min_executable} → '
          f'{"0（达标）" if gate_pass else "1（未达标）"}')
    return 0 if gate_pass else 1


if __name__ == '__main__':
    raise SystemExit(main())
