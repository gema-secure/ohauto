# -*- coding: utf-8 -*-
"""用例沉淀工装：真机操作留痕 → DSL 回归用例 → 重放跑通。

为什么要有它
------------
验收要求：「把一次探索的 graph.json 转成一份 DSL 回归用例并**跑通**」。
此前卡在两处（剖析）：

  1. 留痕只存 ``node_path``(type+id)，真机 id 覆盖率仅 **5.62%** →
     沉淀规格退化成按 type 歧义匹配。**已修**：``driver.Step`` 现在带
     ``node_spec``（id / text / text_deep，见 driver._fill_node_spec）。
  2. 没有转换工装 → 本文件补上（归 C）。

红线（本工具的自我约束）
------------------------
* 产物里**绝不出现 tap_xy / 任何坐标** —— 只吐语义规格；
* **不猜**：没有 id 也没有文案的步骤标 ``unresolved`` 占位并保留线索，
  宁可如实说「这条重放不了」，也不退化成 type 歧义或编造定位。

用法::

    python tools/trace_to_case.py               # 真机采集 → 沉淀 → 展示
    python tools/trace_to_case.py --execute     # 沉淀后再真机重放，判「跑通」
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from ohauto.driver import Driver, Step               # noqa: E402
from ohauto.hdc import Hdc                            # noqa: E402
from ohauto.layout import flatten                     # noqa: E402
from ohauto.matcher import ON                         # noqa: E402
from ohauto.runner import DeviceGuard, Runner         # noqa: E402
from preflight import require_device, default_target                  # noqa: E402

#: 默认靶标：我们自己的 hypium 多页面样本（控件带 id 与中文文案）
DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
DEFAULT_BUNDLE = 'com.example.myapplication'
DEFAULT_ABILITY = 'EntryAbility'
OUT = os.path.join(HERE, '_out', 'trace_case')

#: 状态栏高度以下才算页面内容（文案采样排除状态栏，真机实测踩过）
_TOP_MIN = 72


# ---------------------------------------------------------------- 采集

def pick_tappable(root, taken) -> Optional[Any]:
    """从当前树上挑一个「带文案的可交互控件」作为采集目标。

    条件：可见 + 可交互 + 文案非空（自身 text 或 text_deep）+
    在状态栏以下。挑不到返回 None（如实说，不硬点）。
    """
    for n in flatten(root, only_visible=True, only_interactive=True):
        text = (n.text or '').strip() or (getattr(n, 'text_deep', '') or '').strip()
        if not text or n.rect.top < _TOP_MIN:
            continue
        key = (n.type, text)
        if key in taken:
            continue
        return n
    return None


def collect(driver: Driver, n_targets: int = 2) -> List[Step]:
    """采集剧本：拉起 → 依次点 n 个带文案控件 → back。留痕留在 driver.steps。"""
    driver.start()
    taken = set()
    for i in range(n_targets):
        page = driver.refresh()
        node = pick_tappable(page, taken)
        if node is None:
            print('  [采集] 第 %d 个目标挑不到（没有带文案的可交互控件），停止采集'
                  % (i + 1))
            break
        text = (node.text or '').strip() or (getattr(node, 'text_deep', '') or '').strip()
        taken.add((node.type, text))
        print('  [采集] tap #%d: %s %r' % (i + 1, node.type, text[:30]))
        driver.tap(node)
        time.sleep(1.0)
    driver.back()
    return [s for s in driver.steps if s.ok]


# ---------------------------------------------------------------- 沉淀

def _spec_of(step: Step) -> Optional[Dict[str, Any]]:
    """从留痕反解匹配器规格：id 优先，其次文案（含 text_deep 兜底）。

    返回 None 表示「解析不出」→ 上层给 unresolved 占位，**不猜 type、不猜坐标**。
    """
    ns = step.node_spec or {}
    if ns.get('id'):
        return {'id': ns['id']}
    if ns.get('text'):
        return {'text': ns['text']}
    return None


def steps_to_case(steps: List[Step], name: str = '') -> Dict[str, Any]:
    """留痕 → DSL 用例。返回 {'name', 'steps', 'stats'}（stats 供报告，不入 DSL）。"""
    out: List[Dict[str, Any]] = []
    stats = {'id': 0, 'text': 0, 'unresolved': 0}
    first = True
    for s in steps:
        k = s.kind
        if first:
            # 用例以 start 开头（状态可控、可重跑），采集的拉起留痕不重复收录
            out.append({'start': True})
            first = False
        if k == 'tap':
            spec = _spec_of(s)
            if spec is None:
                stats['unresolved'] += 1
                out.append({'unresolved': {'action': 'tap', 'reason': '留痕无可定位规格',
                                           'node_path': s.node_path or ''}})
            else:
                stats['id' if 'id' in spec else 'text'] += 1
                out.append({'tap': spec})
        elif k == 'input' and s.value is not None:
            spec = _spec_of(s)
            if spec is None:
                stats['unresolved'] += 1
                out.append({'unresolved': {'action': 'input', 'reason': '留痕无可定位规格',
                                           'node_path': s.node_path or ''}})
            else:
                spec = dict(spec)
                spec['value'] = s.value
                stats['id' if 'id' in spec else 'text'] += 1
                out.append({'input': spec})
        elif k == 'back':
            out.append({'back': True})
        elif k == 'waitFor':
            spec = _spec_of(s)
            out.append({'waitFor': spec} if spec else {'waitFor': s.target})
        elif k.startswith('assert'):
            out.append({k.replace('.', '_'): {'exists': _spec_of(s) or s.target}})
    return {'name': name or '沉淀回归用例', 'steps': out, 'stats': stats}


# ---------------------------------------------------------------- 主流程

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description='用例沉淀：留痕 → DSL 用例 → 重放跑通')
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--targets', type=int, default=2, help='采集时点几个控件')
    ap.add_argument('--execute', action='store_true', help='沉淀后真机重放判「跑通」')
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)
    print('=' * 70)
    print('  用例沉淀：采集 → 沉淀 → %s' % ('真机重放' if args.execute else '落盘'))
    print('=' * 70)

    hdc = require_device(target=args.target)
    DeviceGuard(hdc, verbose=False).ensure_awake()
    # 系统 USB 弹窗会霸占顶层窗口（实测坑）—— 采集前先清掉，
    # 否则「第一个带文案的控件」永远是弹窗自己
    hdc.shell('aa force-stop com.usb.right')

    # ---- 1. 采集
    driver = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
                    artifact_dir=os.path.join(OUT, 'artifacts'), verbose=False)
    steps = collect(driver, n_targets=args.targets)
    print('  留痕 %d 步（全部成功）' % len(steps))

    # ---- 2. 沉淀
    case = steps_to_case(steps)
    stats = case.pop('stats')
    print('\n  沉淀结果：%d 步 ｜ 定位规格命中：id=%d text=%d unresolved=%d'
          % (len(case['steps']), stats['id'], stats['text'], stats['unresolved']))
    for i, st in enumerate(case['steps'], 1):
        print('    %2d. %s' % (i, json.dumps(st, ensure_ascii=False)))
    runnable = stats['unresolved'] == 0

    # ---- 3. 落盘（YAML + JSON 双份）
    stamp = time.strftime('%m%d-%H%M%S')
    yaml_path = os.path.join(OUT, 'case_%s.yaml' % stamp)
    json_path = os.path.join(OUT, 'case_%s.json' % stamp)
    try:
        import yaml
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(case, f, allow_unicode=True, sort_keys=False)
        print('\n  [YAML] %s' % yaml_path)
    except ImportError:
        print('\n  [YAML] 未安装 pyyaml，跳过（DSL 仍有 JSON 版）')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(case, f, ensure_ascii=False, indent=2)
    print('  [JSON] %s' % json_path)

    # ---- 4. 重放（可选）
    if not args.execute:
        print('\n  （未加 --execute，到此为止；加上可真机重放判「跑通」）')
        return 0
    if not runnable:
        print('\n  [停止] 用例含 unresolved 步骤 —— 重放必然跑不通，如实不跑。')
        print('         这是「标识稀缺」的如实呈现，不是工具坏了。')
        return 1

    print('\n' + '=' * 70)
    print('  真机重放（同一设备、冷启动）')
    print('=' * 70)
    runner = Runner(verbose=False)
    res = runner.run_case(driver, {'name': case['name'], 'steps': case['steps']})
    for s in res.steps:
        mark = '✅' if s.ok else '❌'
        err = ''
        # ⚠️ StepResult 没有 error 字段，失败原因在 attempts 里
        # （离线全过时这个分支从不执行 —— 失败分支必须真机跑过才算数）
        for a in reversed(getattr(s, 'attempts', None) or []):
            if not a.ok and getattr(a, 'error', ''):
                err = a.error
                break
        print('  %s 步骤 %d %s %s' % (mark, s.index, s.action,
                                     ('← ' + err) if not s.ok else ''))
    ok = bool(res.steps) and all(s.ok for s in res.steps)
    print('\n  结论：%s（%d/%d 步通过）'
          % ('**跑通**' if ok else '未跑通',
             sum(1 for s in res.steps if s.ok), len(res.steps)))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
