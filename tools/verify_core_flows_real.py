# -*- coding: utf-8 -*-
"""核心流程覆盖验证（真机）—— 基础目标 #5 的证据工装。

为什么需要它
------------
验收要求：「至少基于一个 OpenHarmony 示例应用完成验证，**覆盖不少于 3 个页面
或核心流程**」。

2026-09-23 真机核实：**设备上只有 3 个 `ohos.samples.*`，没有一个有 ≥3 页面** ——

| 示例应用 | 页面数 | 硬证据 |
|---|---|---|
| `ohos.samples.distributedcalc` | 1 | 单页 |
| `ohos.samples.distributedmusicplayer` | **1** | 整棵树只有一个 `pagePath=pages/Index` |
| `ohos.samples.etsclock` | 0 可交互 | 纯 Canvas |

所以只能走**「或核心流程」**这条读法，而不是硬凑页面数。音乐播放器恰好有
4 个播放控制按钮 + 1 个进度条，足以覆盖 ≥3 个核心流程。

这个脚本做的事
--------------
对每条核心流程：唤醒确认 → 记录 before（控件树指纹 + 截图）→ 执行动作 →
记录 after → 判定「状态是否真的变了」。**判定依据是控件树差异，不是「没报错」** ——
「动作发出去了」和「界面响应了」是两回事，后者才是证据。

坐标**全部从当前控件树动态取**（4 个按钮按 x 排序、进度条取 Slider 的 rect），
不写死像素 —— 换分辨率/换皮肤都不会失效。

用法::

    python tools/verify_core_flows_real.py
    python tools/verify_core_flows_real.py --bundle ohos.samples.distributedmusicplayer \\
        --ability ohos.samples.distributedmusicplayer.MainAbility
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc                                    # noqa: E402
from ohauto.layout import LayoutNode, Rect, flatten           # noqa: E402
from ohauto.runner import DeviceGuard                         # noqa: E402
from preflight import require_device                          # noqa: E402

DEFAULT_BUNDLE = 'ohos.samples.distributedmusicplayer'
DEFAULT_ABILITY = 'ohos.samples.distributedmusicplayer.MainAbility'
# None = 走 Hdc 自动定位（env HDC_PATH → hdc.config.json → PATH → 常见位置）。
# 本机自定 hdc 路径是隐私项，写进本地的 hdc.config.json，不写进源码。
DEFAULT_HDC = None


# ---------------------------------------------------------------- 数据结构

@dataclass
class Flow:
    """一条核心流程及其执行结果。"""
    name: str
    action: str                     # 人话描述这一步干了什么
    ok: bool = False
    detail: str = ''
    before_sig: str = ''
    after_sig: str = ''
    before_png: str = ''
    after_png: str = ''
    changed: bool = False
    tree_nodes: int = 0


def _dump(hdc: Hdc, out_dir: str, tag: str,
          shot: bool = True) -> Tuple[LayoutNode, str, str]:
    """取一次控件树（可选截图）。返回 (树, 树指纹, 本地截图路径)。

    注意两个接口约定（踩过）：
      · `Hdc.screen_cap()` 收的是**设备侧路径**并返回设备侧路径 ——
        要拿到本地文件还得再 `pull` 一次，别把本地路径传进去；
      · `Hdc.start_ability(bundle, ability)` 是 bundle 在前。

    `shot=False` 用于「只为取坐标、不需要留痕」的中间调用 ——
    否则会出现一堆没人看的 `_tmp.png` 混在产物里。
    """
    path = hdc.dump_layout()
    txt = hdc.shell(f'cat {path}', timeout=60).stdout
    from ohauto.layout import parse_layout
    try:
        root = parse_layout(txt)
    except Exception as e:      # 典型：dumpLayout 返回 [Fail]... 文本 = 设备掉线/锁屏
        raise RuntimeError(
            '控件树解析失败（dumpLayout 返回了非 JSON 文本，前 80 字符: %r）'
            '—— 多半是设备掉线或锁屏，不是代码有 bug。可先跑: python -m ohauto.doctor'
            % txt[:80]) from e
    sig = hashlib.sha256(txt.encode('utf-8', 'replace')).hexdigest()[:16]
    if not shot:
        return root, sig, ''
    png = os.path.join(out_dir, f'{tag}.png')
    try:
        dev = hdc.screen_cap(f'/data/local/tmp/ohauto_flow_{tag}.png')
        hdc.pull(dev, png, binary=True)
    except Exception as e:
        png = ''
        print(f'    [warn] 截图失败（不影响判定）: {type(e).__name__}: {e}')
    return root, sig, png


def _tree_brief(root: LayoutNode, limit: int = 6) -> str:
    """取树上「有文案的节点」做摘要 —— 比整树指纹更好读，也足以判变化。"""
    parts = [(n.text or '').strip() for n in root.walk()
             if (n.text or '').strip()]
    seen, out = set(), []
    for t in parts:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return ' | '.join(out)


def _play_buttons(root: LayoutNode) -> List[LayoutNode]:
    """找播放控制按钮：可点击的 Image，按 x 排序。

    为什么不按 id 找：真机上这 4 个按钮**一个 id 都没有**（实测依据），
    只能靠「type=Image + clickable + 位于屏幕下半部」这三个客观特征圈定。
    """
    h = max((n.rect.bottom for n in root.walk()), default=1280)
    cands = [n for n in flatten(root, only_visible=True, only_interactive=True)
             if n.type == 'Image' and n.rect.top > h * 0.6]
    return sorted(cands, key=lambda n: n.rect.left)


def _slider(root: LayoutNode) -> Optional[LayoutNode]:
    for n in flatten(root, only_visible=True, only_interactive=True):
        if n.type == 'Slider':
            return n
    return None


# ---------------------------------------------------------------- 主流程

def run_flow(hdc: Hdc, guard: DeviceGuard, out_dir: str, idx: int,
             name: str, action: str, do: Callable[[], None]) -> Flow:
    """跑一条流程：记录 before → 动作 → 记录 after → 判定变化。"""
    f = Flow(name=name, action=action)
    guard.ensure_awake()                       # 每条前都确认屏幕可用
    before, f.before_sig, f.before_png = _dump(hdc, out_dir, f'{idx:02d}a_before')
    f.tree_nodes = len(list(before.walk()))
    try:
        do()
        time.sleep(2.0)                        # 等界面响应，别急着判
    except Exception as e:
        f.detail = f'动作失败: {type(e).__name__}: {e}'
        return f
    after, f.after_sig, f.after_png = _dump(hdc, out_dir, f'{idx:02d}b_after')
    f.changed = before_sig_changed(f.before_sig, f.after_sig)
    b_brief, a_brief = _tree_brief(before), _tree_brief(after)
    f.ok = f.changed
    f.detail = (f'界面有响应' if f.changed else f'界面**无变化**')
    f.detail += f'　[{b_brief[:60]} → {a_brief[:60]}]'
    return f


def before_sig_changed(a: str, b: str) -> bool:
    return a != b


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='核心流程覆盖验证（真机，基础 #5）')
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--hdc', default=DEFAULT_HDC)
    ap.add_argument('--target', default=None)
    ap.add_argument('--out', default=os.path.join(ROOT, '_out', 'core_flows'))
    args = ap.parse_args(argv)

    stamp = time.strftime('%m%d-%H%M%S')
    out_dir = os.path.join(args.out, stamp)
    os.makedirs(out_dir, exist_ok=True)

    hdc = require_device(target=args.target, hdc_path=args.hdc)
    guard = DeviceGuard(hdc, verbose=False)

    print('=' * 72)
    print('  核心流程覆盖验证 · 基础目标 #5')
    print(f'  被测应用: {args.bundle}')
    print('=' * 72)
    print(f'  屏幕可用: {guard.ensure_awake()}')
    hdc.start_ability(args.bundle, args.ability)
    time.sleep(3)

    root, sig0, png0 = _dump(hdc, out_dir, '00_launch')
    pages = sorted({str(n.attributes.get('pagePath'))
                    for n in root.walk() if n.attributes.get('pagePath')})
    print(f'  节点数: {len(list(root.walk()))}   页面归属: {pages or "（无 pagePath）"}')
    btns = _play_buttons(root)
    sld = _slider(root)
    print(f'  播放控制按钮: {len(btns)} 个   进度条: {"有" if sld else "无"}')
    for i, b in enumerate(btns, 1):
        print(f'    · 按钮{i} 中心={b.rect.center}')
    if not btns:
        print('  ❌ 找不到播放控制按钮 —— 应用可能不在预期页面，停止')
        return 1

    flows: List[Flow] = []
    # 每条流程的坐标都从**当次**的树上重新取，不用开跑前的快照 ——
    # 因为上一条流程可能已经改变了布局。
    def tap_btn(i: int) -> Callable[[], None]:
        def _do():
            cur, _, _ = _dump(hdc, out_dir, '_tmp', shot=False)
            bs = _play_buttons(cur)
            hdc.click(*bs[i].rect.center)
        return _do

    plan = [
        ('播放/暂停', f'点击第 2 个播放控制按钮', tap_btn(1)),
        ('上一首',   f'点击第 1 个播放控制按钮', tap_btn(0)),
        ('下一首',   f'点击第 3 个播放控制按钮', tap_btn(2)),
    ]
    if sld is not None:
        def drag() -> None:
            cur, _, _ = _dump(hdc, out_dir, '_tmp', shot=False)
            s = _slider(cur)
            r = s.rect
            y = r.center[1]
            hdc.swipe(r.left + r.width // 5, y, r.left + r.width * 4 // 5, y, 500)
        plan.append(('拖动播放进度', '在进度条上从中段滑到后段', drag))

    for i, (name, desc, do) in enumerate(plan, 1):
        print(f'\n  [{i}/{len(plan)}] {name} —— {desc}')
        f = run_flow(hdc, guard, out_dir, i, name, desc, do)
        print(f'      {"✅" if f.ok else "❌"} {f.detail}')
        flows.append(f)

    ok_n = sum(1 for f in flows if f.ok)
    print('\n' + '=' * 72)
    print(f'  核心流程覆盖: {ok_n}/{len(flows)} 个流程界面有响应')
    print(f'  验收要求: 不少于 3 个页面或核心流程 → '
          f'{"✅ 满足（核心流程口径）" if ok_n >= 3 else "❌ 未满足"}')
    print(f'  产物目录: {out_dir}')

    lines = [f'# 核心流程覆盖验证 —— `{args.bundle}`\n',
             f'> 设备 `{args.target or "（默认）"}` ｜ '
             f'{time.strftime("%Y-%m-%d %H:%M")} ｜ 产物 `{out_dir}`\n',
             f'- 节点数 {len(list(root.walk()))}',
             f'- 页面归属 `pagePath`: {pages or "（无）"}',
             f'- 播放控制按钮 {len(btns)} 个，进度条 {"有" if sld else "无"}',
             '',
             '## 口径说明\n',
             '验收要求：「覆盖不少于 3 个**页面或核心流程**」。',
             '本机核实：设备上只有 3 个 `ohos.samples.*`，',
             '**没有一个有 ≥3 页面**（本应用的整棵树只有一个 `pages/Index`）。',
             '因此本验证走**「核心流程」**口径 —— 这是验收要求允许的读法，',
             '**不是降低标准**。同时另用系统应用（设置/备忘录）提供',
             '「≥3 页面」的结构证据，两者互补。\n',
             '## 结果\n',
             '| # | 核心流程 | 动作 | 界面有响应 | 说明 |',
             '|---|---|---|---|---|']
    for i, f in enumerate(flows, 1):
        lines.append(f'| {i} | {f.name} | {f.action} | '
                     f'{"✅" if f.ok else "❌"} | {f.detail} |')
    lines.append(f'\n**合计：{ok_n}/{len(flows)} 条流程界面有响应；'
                 f'验收要求 ≥3 条 → '
                 f'{"✅ 满足" if ok_n >= 3 else "❌ 未满足"}**\n')
    lines.append('> 判定依据是**控件树指纹是否变化**，不是「动作没报错」。'
                 '「动作发出去了」和「界面响应了」是两回事，后者才是证据。\n')
    lines.append('## 每一步的留痕\n')
    for i, f in enumerate(flows, 1):
        lines.append(f'### {i}. {f.name}\n')
        lines.append(f'- 动作：{f.action}')
        lines.append(f'- 前：`{f.before_sig}` ｜ 后：`{f.after_sig}`')
        if f.before_png:
            lines.append(f'- 截图：`{os.path.basename(f.before_png)}` → '
                         f'`{os.path.basename(f.after_png)}`')
        lines.append('')
    dst = os.path.join(out_dir, 'core_flows.md')
    with open(dst, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    print(f'[报告] {dst}')
    return 0 if ok_n >= 3 else 1


if __name__ == '__main__':
    raise SystemExit(main())
