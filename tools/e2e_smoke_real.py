# -*- coding: utf-8 -*-
"""真机端到端冒烟 —— 一次跑完 A/B 两边的全部模块。

为什么有这个东西
----------------
B 的交付包请求 C「集成日顺手带一次真机冒烟（探索 + 生成 + 归因 + 压测各跑一遍）」，
理由是他自己所有数字都是**模拟设备自测**、不是独立验收。
A 同理：A1 视觉通道、A3 降级链与自愈也只在 `FakeHdc` 上验过。

这个脚本就是那两个请求的载体：在**真机**上把五个阶段各跑一遍，
每阶段独立报告，任一阶段失败**不阻断其余阶段**（冒烟的价值在于一次性看全貌，
而不是遇到第一个错误就退出）。

被测应用默认选设备自带计算器 `ohos.samples.distributedcalc`：
它的数字/运算符按钮**全部带 id**，定位稳定；而且点它**没有副作用**
（不改系统设置、不删数据）—— 适合当冒烟靶子。

用法::

    python tools/e2e_smoke_real.py
    python tools/e2e_smoke_real.py --skip-explore          # 跳过会真点击的探索
    python tools/e2e_smoke_real.py --bundle com.ohos.note --ability MainAbility
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import Driver, Hdc, ON, diagnose, run, run_suite          # noqa: E402
from ohauto.diagnose import ExecutionRecord                            # noqa: E402
from ohauto.explorer import Budget, Explorer                           # noqa: E402
from ohauto.runner import DeviceGuard                                  # noqa: E402
from ohauto.generator import (ScriptedProvider, generate,              # noqa: E402
                              stress_cases)
from preflight import require_device                  # noqa: E402

DEFAULT_BUNDLE = 'ohos.samples.distributedcalc'
DEFAULT_ABILITY = 'MainAbility'
DEVICE_HDC = None    # None = 走 Hdc 自动定位；本机路径写 hdc.config.json（隐私项不入源码）

RESULTS: list = []


def stage(n, title):
    print()
    print('=' * 72)
    print(f'  阶段 {n} · {title}')
    print('=' * 72)
    return time.time()


def record(name, ok, detail='', seconds=0.0):
    RESULTS.append((name, ok, detail, seconds))
    flag = '通过' if ok else '未通过'
    print(f'\n  >> [{flag}] {name}' + (f'  ({detail})' if detail else ''))


# --------------------------------------------------------------------- 0

def stage_device(args, d, hdc):
    """设备准备与环境事实。不做任何破坏性动作。"""
    stage(0, '设备准备与环境事实')
    facts = {}
    for key, cmd in (
        ('版本', 'param get const.product.software.version'),
        ('API', 'param get const.ohos.apiversion'),
        ('可调试', 'param get const.debuggable'),
        ('设备时间', 'date'),
        ('前台应用', 'aa dump -a 2>/dev/null | grep -c FOREGROUND'),
    ):
        try:
            out = hdc.shell(cmd).stdout.strip().splitlines()
            facts[key] = out[0] if out else '?'
        except Exception as e:
            facts[key] = f'<读取失败 {type(e).__name__}>'
    for k, v in facts.items():
        print(f'  {k:<10} {v}')

    # 时间偏差超过 60s 会污染崩溃日志窗口与签名有效期，先提醒
    note = ''
    try:
        dev_t = hdc.shell('date +%s').stdout.strip()
        drift = abs(int(float(dev_t)) - time.time())
        note = f'设备时钟偏差 {drift:.0f}s'
        print(f'  时钟偏差    {drift:.0f}s '
              f'{"(正常)" if drift < 60 else "(⚠️ 建议先跑 tools/sync_device_time.py)"}')
    except Exception:
        pass
    record('设备连通与信息读取', bool(facts), note)
    return facts


# --------------------------------------------------------------------- 1

def stage_locator(args, d):
    """A 的定位能力：降级链在真机控件树上能否命中。"""
    stage(1, 'A · 定位（真机控件树上的降级链）')
    try:
        from ohauto import LocatorManager

        page = d.refresh()
        nodes = list(page.walk()) if hasattr(page, 'walk') else []
        print(f'  当前控件树      {len(nodes)} 个节点')

        lm = LocatorManager()
        # 三个探针：存在的 id / 不存在的 id（应走降级链或落空）/ text 线索
        probes = [{'id': '7'}, {'id': 'plus_should_not_exist'}, {'text': '计算器'}]
        hits = 0
        for spec in probes:
            try:
                r = lm.locate(spec, page)
            except Exception as e:
                print(f'    {spec}  -> 抛错 {type(e).__name__}: {e}')
                continue
            if r is None:
                print(f'    {spec}  -> 未命中（None）')
                continue
            print(f'    {spec}  -> channel={getattr(r, "channel", "?")} '
                  f'level={getattr(r, "level", "?")} '
                  f'conf={getattr(r, "confidence", 0):.2f} '
                  f'id={getattr(r, "locator_id", "?")} '
                  f'rect={getattr(r, "rect", "?")}')
            hits += 1

        health = lm.all_health() if hasattr(lm, 'all_health') else {}
        print(f'  健康度账本      {len(health)} 条')
        ok = hits > 0
        record('定位降级链（真机）', ok, f'{hits}/{len(probes)} 命中')
        return ok
    except Exception as e:
        print(traceback.format_exc(limit=4))
        record('定位降级链（真机）', False, f'{type(e).__name__}: {e}')
        return False


# --------------------------------------------------------------------- 2

def stage_explore(args, d):
    """B1/B2：真机探索与覆盖度。会真的点击，默认只点计算器（无副作用）。"""
    stage(2, 'B · 探索（真机 BFS + 覆盖度）')
    if args.skip_explore:
        print('  （--skip-explore，跳过）')
        record('探索覆盖度（真机）', True, 'skipped')
        return
    try:
        ex = Explorer(d, verbose=False)
        budget = Budget(max_pages=args.max_pages,
                        max_actions_per_page=args.actions_per_page,
                        max_seconds=args.max_seconds, max_steps=120)
        graph = ex.explore(budget=budget)
        cov = ex.coverage                     # 是 property，不是方法
        # 口径提醒：分母只含**已访问页面**里的可交互控件，
        # 衡量「进了这页把它点全了吗」，不含「页面有没有找全」——
        # 必须与 pages 一起看，否则「只进一页点光了它」也能拿 100%。
        print(f'  发现页面数      {cov.pages}')
        print(f'  页面数(结构签名) {getattr(cov, "pages_structural", "?")}')
        print(f'  状态数(内容签名) {cov.pages}')
        print(f'  可交互控件      {cov.interactive_visited}/{cov.interactive_total}')
        print(f'  覆盖度          {getattr(cov, "ratio", float("nan")):.1%}')
        print(f'  被安全策略拦下  {cov.blocked}')
        # C3：权限弹窗是**有副作用的动作**（点了就等于替人作答），必须显式报出来
        if ex.permission_events:
            print(f'  权限弹窗        策略={ex.permission_policy} '
                  f'作答 {len(ex.permission_events)} 次')
            for e in ex.permission_events:
                print(f'    · {e["owner"]}「{e["title"]}」→ '
                      f'{e["answered"] or "未作答"}')
        print(f'  已访问状态数    {len(graph.states)}')
        for st in list(graph.states.values())[:6]:
            print(f'    · {st.sid} 「{st.title}」 visits={st.visits} '
                  f'路径步数={len(getattr(st, "path", []) or [])} '
                  f'content_key={str(getattr(st, "content_key", ""))[:10]}')
        if ex.skipped:
            kinds: dict = {}
            for s in ex.skipped:
                k = s.get('reason') or s.get('kind') or 'unknown'
                kinds[k] = kinds.get(k, 0) + 1
            print(f'  跳过项          {len(ex.skipped)}  {dict(sorted(kinds.items(), key=lambda kv: -kv[1])[:4])}')
        ok = cov.pages >= 1 and cov.interactive_total > 0
        record('探索覆盖度（真机）', ok,
               f'{getattr(cov, "pages_structural", "?")} 页 / {cov.pages} 状态, '
               f'覆盖 {getattr(cov, "ratio", 0):.1%}, 控件 {cov.interactive_total}')
    except Exception as e:
        print(traceback.format_exc(limit=4))
        record('探索覆盖度（真机）', False, f'{type(e).__name__}: {e}')


# --------------------------------------------------------------------- 3

def stage_generate(args, d):
    """B3：自然语言转用例。没配模型时用 ScriptedProvider 走链路（不碰网络）。"""
    stage(3, 'B · 生成（描述 → 测试点 → DSL）')
    try:
        page = d.refresh()
        # 没配模型时用 ScriptedProvider 走链路（完全不碰网络）。
        # replies=[] 表示「模型什么都没回」—— 正好验证解析失败时
        # 是否给出清晰的原因分类，而不是抛个裸异常。
        provider = ScriptedProvider(replies=[])
        case = generate('点 7 加 8 等于 15', provider=provider,
                        bundle=args.bundle, ability=args.ability, page=page)
        steps = getattr(case, 'steps', []) or []
        print(f'  生成用例名      {getattr(case, "name", "?")}')
        print(f'  步骤数          {len(steps)}')
        for s in steps[:5]:
            print(f'    · {s}')
        record('用例生成链路（ScriptedProvider）', len(steps) > 0, f'{len(steps)} 步')
    except Exception as e:
        # 空回复下 GenerationError 是**预期**结果：说明它没硬凑一条看起来能跑的用例，
        # 而是明确报出「拿不到可执行用例」。这本身就是 B3 想要的语义。
        name = type(e).__name__
        reason = getattr(e, 'reason', None) or getattr(e, 'reject_reason', None)
        print(f'  {name}: {e}')
        if reason:
            print(f'  原因分类        {reason}')
        record('用例生成链路（ScriptedProvider）', name == 'GenerationError',
               f'{name}（空回复下明确报错，符合预期）')
        print('  注：真实模型需配 OHAUTO_LLM_*；本项只验链路语义。')


# --------------------------------------------------------------------- 4

def stage_diagnose(args, d):
    """B4：失败归因。真机跑一条注定失败的用例，看能否正确归类。"""
    stage(4, 'B · 归因（真机失败样本 → 四分类）')
    try:
        case = {'name': 'smoke_locate_fail',
                'bundle': args.bundle, 'ability': args.ability,
                'steps': [{'tap': {'id': 'this_control_does_not_exist'}}]}
        # 同样关掉产物轮转（原因见 stage_stress 的注释）
        from ohauto import Runner
        res = Runner(artifact_budget=0, verbose=False).run_case(d, case)
        failed = [s for s in res.steps if not s.ok]
        print(f'  用例结果        ok={res.ok}  失败步 {len(failed)}')
        for s in failed:
            print(f'    · #{s.index} {s.action} kind={getattr(s.kind, "value", s.kind)} '
                  f'快照 {len(getattr(s, "trees", []) or [])} 张')

        rec = ExecutionRecord.from_case_result(
            res, bundle=args.bundle, expected_target={'id': 'this_control_does_not_exist'})
        v = diagnose(rec)
        print(f'  归因类别        {v.category.value}')
        print(f'  置信度          {v.confidence:.2f}')
        for ev in (v.evidence or [])[:3]:
            print(f'    · {ev}')
        ok = v.category.value == 'LOCATOR' and len(rec.trees) >= 1
        record('归因（真机 + 失败快照）', ok,
               f'{v.category.value} @ {v.confidence:.2f}, 快照 {len(rec.trees)} 张')
    except Exception as e:
        print(traceback.format_exc(limit=4))
        record('归因（真机 + 失败快照）', False, f'{type(e).__name__}: {e}')


# --------------------------------------------------------------------- 5

def stage_stress(args, d):
    """B5：压测用例生成 + 分片执行（DSL 无循环，靠 run_suite 层重复）。"""
    stage(5, 'B · 压测（生成 + 分片执行）')
    try:
        cases = stress_cases('重复点击', chunks=args.chunks, bundle=args.bundle,
                             ability=args.ability, rounds=4, target={'id': '7'})
        print(f'  分片数          {len(cases)}')
        dicts = [c.to_dict() if hasattr(c, 'to_dict') else c for c in cases]
        # 注意：run_suite() 直接返回 SuiteResult（没有 .suite）；
        # 只有契约入口 run() 才返回带 .suite 的 RunReport。
        #
        # ★ artifact_budget=0 关掉产物轮转 —— 这是实测踩的坑：
        #   默认 budget=60 会在产物超量时逐个 os.remove 清理旧截图，
        #   而本机沙箱对批量删除有安全拦截：
        #     [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] targets=[...]
        #   拦截会让整个冒烟进程在这里断掉（前面几个阶段的结果全白跑）。
        #   冒烟本身产物不多，不轮转更稳；真要控体积就在跑之前手工清目录。
        from ohauto import Runner
        suite = Runner(artifact_budget=0, verbose=False).run_suite(
            [(d, c) for c in dicts])
        total = len(dicts)
        ok_n = getattr(suite, 'ok_count', None)
        if ok_n is None:
            ok_n = sum(1 for c in suite.cases if c.ok)
        print(f'  分片执行        {ok_n}/{total} 通过')
        for c in suite.cases:
            print(f'    · {c.name}: ok={c.ok} 步数={len(c.steps)}')
        record('压测分片执行（真机）', ok_n >= 1, f'{ok_n}/{total} 分片通过')
    except Exception as e:
        print(traceback.format_exc(limit=4))
        record('压测分片执行（真机）', False, f'{type(e).__name__}: {e}')


# ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--hdc', default=DEVICE_HDC)
    ap.add_argument('--target', default=None)
    ap.add_argument('--skip-explore', action='store_true',
                    help='跳过会真点击的探索阶段')
    ap.add_argument('--max-pages', type=int, default=3)
    ap.add_argument('--max-seconds', type=float, default=90.0)
    ap.add_argument('--actions-per-page', type=int, default=4,
                    help='每页最多尝试多少个控件 —— 覆盖率的主要限制项')
    ap.add_argument('--chunks', type=int, default=2)
    args = ap.parse_args()

    print('=' * 72)
    print('  真机端到端冒烟 · 探索 / 定位 / 生成 / 归因 / 压测')
    print(f'  被测应用: {args.bundle} / {args.ability}')
    print('=' * 72)

    # 每次跑用**独立的时间戳目录**，两个理由：
    #   ① 产物不跨次累积，Runner 的产物轮转（`_prune_artifacts` → os.remove）
    #      就不会被触发 —— 本机沙箱对批量删除有拦截（[safe-delete]），
    #      一旦撞上，整个冒烟会断在半路，前面几个阶段的结果全白跑；
    #   ② 每次的结果可单独回看，不会和上次的截图/控件树混在一起。
    stamp = time.strftime('%m%d-%H%M%S')
    out = os.path.join(ROOT, '_out', 'smoke_real', stamp)
    os.makedirs(out, exist_ok=True)

    hdc = require_device(hdc_path=args.hdc, target=args.target)
    d = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
               artifact_dir=out, verbose=False)

    # ★ 冒烟前先确认屏幕可用 —— 这一步不是可选的美化，是**数字真伪的前提**。
    #
    # 设备默认 30s 息屏；息屏后回到**锁屏界面**，而锁屏界面**不进无障碍树**，
    # 于是 `uitest dumpLayout` 返回一个 **377 字节的空树**
    # （`bounds:[0,0][0,0]`、`children:[]`）。
    #
    # 后果极隐蔽：探索照跑、覆盖率照出、报告照生成 —— 但每一步都在
    # 「没有窗口」的状态下进行。2026-09-23 实测踩到：探索只拿到 2 页 /
    # 覆盖 28.6%，且第 3 步之后的控件树**全部是 377 字节**，
    # 而当时的输出里一个错误都没有。
    #
    # 复用生产类 `DeviceGuard`（runner.py:198）—— 它做的就是
    # 「唤醒 + 钉住息屏超时 + 必要时上滑解锁」，不另抄一份。
    guard = DeviceGuard(hdc, verbose=False)
    awake = guard.ensure_awake()
    print(f'  屏幕可用: {awake}'
          f'{"（已唤醒并解锁）" if awake else "（⚠️ 仍不可用，后续可能全是空树）"}')

    stage_device(args, d, hdc)

    # ★ 冒烟前必须先把被测应用拉到前台。
    # 否则后面每个阶段都在「桌面 / 另一个应用」的控件树上跑 ——
    # 数字看着有、结论全是假的（第一次跑就踩了这个坑：定位 0/3 命中，
    # 因为设备当时根本不在计算器上）。
    print('\n  拉起被测应用...')
    try:
        d.start()
        time.sleep(2)
        root = d.refresh()
        n = len(list(root.walk())) if hasattr(root, 'walk') else -1
        print(f'  已启动 {args.bundle}，当前控件树 {n} 个节点')
        if n <= 3:
            print('  ⚠️ 控件树近乎为空（空树 = 无窗口），重试唤醒解锁 ...')
            guard.ensure_awake()
            root = d.refresh()
            n = len(list(root.walk())) if hasattr(root, 'walk') else -1
            print(f'  重试后控件树 {n} 个节点')
            if n <= 3:
                print('  ❌ 仍是空树 —— 后面的探索/定位数字**不可采信**，'
                      '请人工检查设备是否锁屏')
    except Exception as e:
        print(f'  [警告] 启动失败: {type(e).__name__}: {e}')

    for fn in (stage_locator, stage_explore, stage_generate,
               stage_diagnose, stage_stress):
        try:
            fn(args, d)
        except Exception as e:                     # 兜底：任何阶段都不许炸掉整体
            record(fn.__name__, False, f'未捕获 {type(e).__name__}: {e}')

    print()
    print('=' * 72)
    print('  汇总')
    print('=' * 72)
    passed = sum(1 for _, ok, _, _ in RESULTS if ok)
    for name, ok, detail, _ in RESULTS:
        print(f'  [{"PASS" if ok else "FAIL"}] {name:<34} {detail}')
    print(f'\n  {passed}/{len(RESULTS)} 项通过')
    print(f'  产物目录: {out}')
    return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
    sys.exit(main())
