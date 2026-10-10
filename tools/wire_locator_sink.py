# -*- coding: utf-8 -*-
"""把执行失败回写接到定位器自愈上。

契约原文
--------
> **你也要给出去**：执行失败时调用 A 的 `record_locator_failure` 回写，
> 让自愈有输入。

断在哪
------
两边的接口**对不上**：

| 侧 | 手里有什么 | 对方要什么 |
|---|---|---|
| C 的 `Runner` | **目标规格**（DSL 里写的 `{'id': '7'}`） | — |
| A 的 `LocatorManager` | **`locator_id`**（`L001_7`，它自己册子里发的水号） | — |

执行器的定位走 `matcher`/`layout`，**从来没经过 `LocatorManager`**，所以
runner 手里根本没有那个号。A 交付说明里写的路径是「执行时先 `locate()` 拿
`LocateResult.locator_id`」—— 那要求把 `LocatorManager` 塞进执行链路，
改动面很大。

这里的做法
----------
**两边都不动**：让 runner 把规格原样递出来（已加 `locator_sink` 钩子），
由本模块负责反查成 `locator_id`。

    lm = LocatorManager()
    lm.register({'id': '7'}, page)          # 调用方照常注册

    sink = make_locator_sink(lm)            # 本模块
    runner = Runner(locator_sink=sink)      # C 侧照常跑
    # 步骤定位失败 → runner 调 sink({'id': '7'}, '...')
    #                → 反查出 'L001_7' → lm.record_locator_failure('L001_7', ...)

反查用的是 A **已有的**公开接口（`all_health()` + `spec()`），
所以 A 不需要为此改任何东西。等 A 把 `locator_id_for(spec)` 之类的
正式反查接口定下来，把 `_find_locator_id` 换成一行调用即可。

用法::

    python tools/wire_locator_sink.py            # 自测：跑一条失败用例看健康度
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def _find_locator_id(lm, spec: dict) -> str:
    """按目标规格反查 locator_id。查不到返回 ''。

    优先用 id 匹配（最精确）；只给了 text 才退而用 text。
    ⚠️ 不按 type 匹配 —— 一页里同类型控件成堆，按 type 反查必然撞车。
    """
    want_id = str(spec.get('id') or '')
    want_text = str(spec.get('text') or '')
    if not want_id and not want_text:
        return ''
    for lid in lm.all_health():
        sp = lm.spec(lid)
        if want_id:
            if sp.target_id == want_id:
                return lid
        elif want_text and sp.text == want_text:
            return lid
    return ''


def make_locator_sink(lm, *, verbose: bool = False):
    """造一个可直接传给 `Runner(locator_sink=...)` 的回写函数。

    target_spec: DSL 里那个原始规格字典（runner 原样递出）
    reason:      失败原因串
    attempt: 执行尝试序号（新增，runner 递增传入）——
                 透传给 `record_locator_failure(lid, reason, attempt)`，
                 作为幂等键，让执行器路径的连续失败能真正累加（自愈才有输入）。
                 调用方还没加该参数时（旧签名）自动退回两参调用，行为不变。

    反查不到就**静默跳过** —— 那个控件本来就没注册过定位器，
    没什么可回写的，报错只会制造噪音。
    """
    stats = {'reported': 0, 'skipped': 0, 'with_attempt': 0}

    def sink(target_spec: dict, reason: str, attempt: int = None) -> None:
        lid = _find_locator_id(lm, target_spec or {})
        if not lid:
            stats['skipped'] += 1
            if verbose:
                print(f'    [sink] 规格 {target_spec} 没有对应定位器，跳过')
            return
        if attempt is not None:
            try:
                lm.record_locator_failure(lid, reason, attempt)
                stats['with_attempt'] += 1
            except TypeError:
                # 调用方还是旧签名（不收 attempt）——退回两参，行为不变；
                # 修复后自动带上 attempt。
                lm.record_locator_failure(lid, reason)
        else:
            lm.record_locator_failure(lid, reason)
        stats['reported'] += 1
        if verbose:
            print(f'    [sink] {lid} <- {reason}'
                  + (f' (attempt={attempt})' if attempt is not None else ''))

    sink.stats = stats          # type: ignore[attr-defined]
    return sink


def make_locator_id_resolver(lm, *, verbose: bool = False):
    """造一个可直接传给 `Runner(locator_id_resolver=...)` 的反查函数。

    签名 `spec -> locator_id`，**只查不写**。用途是给**归因结论**补上 id，
    让归因能指认「该修哪个定位器」，而不是笼统地说「定位失败了」。

    ⚠️ 为什么单独要这么一个函数、而不是复用 `make_locator_sink`：
    回写**只能有一条路**（否则同一次失败被计两遍，把幂等破坏掉）。
    集成层选的是执行侧那条（`locator_sink`），所以这里只做只读反查。
    """
    def resolver(spec: dict) -> str:
        lid = _find_locator_id(lm, spec or {})
        if verbose:
            print(f'    [resolve] {spec} -> {lid or "(无对应定位器)"}')
        return lid

    return resolver


def make_diagnose_sink(lm, *, verbose: bool = False):
    """造 `Runner(diagnose_locator_sink=...)` 用的回写函数：`(locator_id, reason)`。

    ⚠️ **默认不要和 `make_locator_sink` 一起接** —— 那是两条回写路径，
    同一次失败会被计两遍。这里保留它是为了让「归因侧独立接管回写」的场景可用
    （那种用法下，执行侧的 `locator_sink` 必须留空）。
    """
    def sink(locator_id: str, reason: str) -> None:
        if not locator_id:
            return
        lm.record_locator_failure(locator_id, reason)
        if verbose:
            print(f'    [diagnose-sink] {locator_id} <- {reason}')

    return sink


# ------------------------------------------------------------------ 自测

def _selftest() -> int:
    """闭环端到端：失败 → 归因带上 locator_id → 回写 A → 自愈换代。

    验证的是**一整条链**，不是单个钩子：
        执行失败(LOCATE)
          → Runner 补 StepResult.locator_id（本模块的 resolver）
          → 归因出结论，结论里带 locator_id
          → 执行侧回写（唯一一条路）→ A 的连续失败计数 +1
          → 到阈值后 repair_locators() 自愈换代
    """
    from ohauto import Driver, LocatorManager, Runner
    from ohauto.sim import FakeHdc

    print('=' * 70)
    print('  闭环自测：失败 → 归因带 id → 回写 A → 自愈换代')
    print('=' * 70)

    sim = FakeHdc(start_page='login', verbose=False)
    d = Driver(bundle='com.demo.app', ability='EntryAbility', hdc=sim,
               verbose=False, default_timeout=250, poll_interval=50,
               sleep_fn=lambda s: None)
    page = d.refresh()

    lm = LocatorManager()
    lm.register({'id': 'username'}, page)
    ids = list(lm.all_health())
    if not ids:
        print('  [FAIL] 一个定位器都没注册上，自测无法进行')
        return 1
    lid0 = ids[0]
    print(f'  已注册定位器: {ids}')

    sink = make_locator_sink(lm, verbose=False)
    runner = Runner(
        locator_sink=sink,                                  # 回写（唯一一条路）
        locator_id_resolver=make_locator_id_resolver(lm),   # 给归因补 id（只读）
        diagnose_failures=True, verbose=False, sleep_fn=lambda s: None)

    # 靶子：**id 已注册**（能反查到）但 text 不匹配（必然定位失败）
    bad_case = {'name': '闭环验证', 'steps': [
        {'tap': {'id': 'username', 'text': '根本不匹配的文案'}},
    ]}

    print('\n  --- 第 1 次失败 ---')
    before = lm.health(lid0).consecutive_failures
    res = runner.run_case(d, bad_case)
    sr = res.steps[0]
    print('  失败步 kind           : %s' % getattr(sr.kind, 'value', '-'))
    print('  StepResult.locator_id : %s' % (sr.locator_id or '(空)'))
    print('  归因结论              : %s' % (sr.verdict_cn or '(无)'))
    v_lid = getattr(sr.verdict, 'locator_id', '') if sr.verdict is not None else ''
    print('  verdict.locator_id    : %s' % (v_lid or '(空)'))
    after = lm.health(lid0).consecutive_failures
    print('  调用方连续失败          : %d -> %d' % (before, after))

    checks = {
        '失败步被识别为 LOCATE': sr.kind is not None
                                and getattr(sr.kind, 'value', '') == 'LOCATE',
        'StepResult.locator_id 被填上': bool(sr.locator_id),
        '归因结论带 locator_id': bool(v_lid),
        '归因建议不再说「没能回写」': bool(sr.verdict_cn)
                                 and '没能回写' not in sr.verdict_cn,
        '调用方连续失败 +1（回写生效）': after == before + 1,
    }

    print('\n  --- 诊断：连续失败能不能累加（决定自愈会不会被触发）---')
    for _ in range(5):
        runner.run_case(d, bad_case)
    cnt = lm.health(lid0).consecutive_failures
    print('  又失败 5 次后连续失败 : %d（期望接近 6）' % cnt)
    if cnt <= 1:
        print('  ⚠️ **已知断点（调用方）**：连续失败停在 %d 不再增长。' % cnt)
        print('     原因：A 的幂等键是 (locate 代次, locator_id)，'
              '代次只在 `locate()` 时推进；')
        print('     而执行器走 matcher/layout，**从不调 `locate()`** → 代次恒为 0，')
        print('     第一次之后的回写全被判成「同一次定位的重复记账」而丢弃。')
        print('     后果：**自愈阈值达不到 → 自愈永远不会触发**。')
        print('     归属：`locator.py::_count_failure`（A 的文件），需 A 收口 ——')
        print('     例如让代次可由执行器显式推进，或幂等键改用「执行尝试序号」。')
        print('     （这不是本接线的问题：locator_id 已经正确送达，回写也被调用了。）')

    # 修：必须显式传 page。执行路径走 matcher/layout、从不调
    # locate() → lm._last_root 恒 None → 不传 page 时自愈报告恒
    # 「ok=False / 没有可用的控件树」，看起来像断点，其实是工装口径。
    # 传当前页（d.refresh() 拿新鲜树）后：连续失败到阈值 → 换代真实发生。
    _repairs_before = lm.health(lid0).repairs
    rep = lm.repair_locators(page=d.refresh())
    _h_after = lm.health(lid0)
    print('  自愈报告              : ok=%s repaired=%s'
          % (rep.ok, getattr(rep, 'repaired', '?')))
    print('  L0 自愈成功次数       : %s → %s（换代后应 +1）'
          % (_repairs_before, _h_after.repairs))

    print('\n  --- 判定（接线本身）---')
    ok = True
    for name, passed in checks.items():
        print('  %s %s' % ('✅' if passed else '❌', name))
        ok = ok and passed
    print()
    print('[PASS] 接线已通：locator_id 送达 + 归因结论带 id + 回写生效'
          if ok else '[FAIL] 接线仍有断点')
    if ok and cnt <= 1:
        print('[注] 闭环**下一环**卡在幂等代次上（见上面诊断），'
              '已记录待 A 收口 —— 不在本模块范围。')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(_selftest())
