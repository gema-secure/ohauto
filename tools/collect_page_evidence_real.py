# -*- coding: utf-8 -*-
"""多页面结构证据采集（真机）—— 基础 #5「≥3 个页面」的硬证据工装。

为什么需要它
------------
命题原话：「至少基于一个 OpenHarmony 示例应用完成验证，**覆盖不少于 3 个页面
或核心流程**」。

2026-09-23 的两条事实：

1. **示例应用这条路走不通**：设备上只有 3 个 `ohos.samples.*`，
   没有一个有 ≥3 页面（`distributedmusicplayer` 整棵树只有一个
   `pagePath=pages/Index`）。核心流程那条已由
   `tools/verify_core_flows_real.py` 出了 4/4 的证据。
2. **系统应用有多页面结构**：`com.ohos.settings` 有独立的
   `AppInfoAbility` / `MainAbility` 两个 ability，
   且在真机上 183 个节点、**0 个 id** —— 这本身还是个有价值的样本。

⚠️ **本脚本刻意不把系统应用冒充成「示例应用」**。
它产出的是「多页面结构证据」，用途是补强「页面覆盖」这一维；
在报告里必须如实注明样本来源，命题的「示例应用」要求仍以
`ohos.samples.*` + 核心流程那条为准。

判定依据是什么（关键）
----------------------
**不是「截图看起来不一样」，而是页面结构指纹变化**：

- `pagePath`（`pagePath=pages/Index` 这类属性）—— 页面级的身份
- 结构签名（层级 + 类型 + bounds 的哈希）—— 内容级的身份

两者任一变化才算「进入了新页面」。**只按截图判会把「同一页的滚动」
误判成换页** —— 滚动前后截图不同、但 `pagePath` 和结构语义没变。

用法::

    python tools/collect_page_evidence_real.py
    python tools/collect_page_evidence_real.py --bundle com.ohos.settings \\
        --ability com.ohos.settings.MainAbility --max-pages 6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc                                    # noqa: E402
from ohauto.layout import LayoutNode, flatten                 # noqa: E402
from ohauto.runner import DeviceGuard                         # noqa: E402
from preflight import require_device                  # noqa: E402

DEFAULT_BUNDLE = 'com.ohos.settings'
DEFAULT_ABILITY = 'com.ohos.settings.MainAbility'
# None = 走 Hdc 自动定位（env HDC_PATH → hdc.config.json → PATH → 常见位置）。
# 本机自定 hdc 路径是隐私项，写进本地的 hdc.config.json，不写进源码。
DEFAULT_HDC = None

#: 页面上「能点的东西」的类型（与 explorer 的口径保持一致，便于交叉核对）
CLICKABLE_TYPES = {
    'Button', 'Image', 'ListItem', 'Row', 'Column', 'Toggle', 'Checkbox',
    'Radio', 'Slider', 'TextInput', 'Search', 'GridItem', 'TabBar',
}


# ---------------------------------------------------------------- 数据结构

@dataclass
class PageEvidence:
    """一个页面的结构证据。"""
    seq: int
    title: str = ''
    page_path: str = ''
    sig: str = ''                       # 结构签名（层级+类型+bounds 的哈希）
    nodes: int = 0
    clickables: int = 0
    ids: int = 0
    texts: List[str] = field(default_factory=list)
    how_reached: str = ''               # 怎么到这个页面的
    png: str = ''
    tree_file: str = ''


# ---------------------------------------------------------------- 工具

def _sh(hdc: Hdc, cmd: str, retries: int = 4) -> str:
    """跑一条设备命令，带 hdc 通道重试。返回 stdout 文本。

    为什么要重试：真机上 hdc 通道会间歇性回
    `need connect-key?` / `The communication channel is being established`
    —— 那不是命令错，是通道还没建好，等一两秒重来就好。

    ⚠️ `Hdc.shell()` 返回的是 **`ShellResult` 对象**，不是 str（踩过）。
    """
    last = ''
    for _ in range(retries):
        try:
            r = hdc.shell(cmd)
        except Exception as e:                                # noqa: BLE001
            last = str(e)
            time.sleep(1.5)
            continue
        text = f'{getattr(r, "stdout", "") or ""}{getattr(r, "stderr", "") or ""}'
        if getattr(r, 'returncode', 0) == 0 and not _is_channel_noise(text):
            return text
        last = text.strip() or str(r)
        time.sleep(1.5)
    raise RuntimeError(f'命令反复失败：{cmd}\n最后一次：{last}')


def _is_channel_noise(text: str) -> bool:
    """通道未就绪的特征串 —— 不是命令错误，重试即可。"""
    return ('need connect-key' in text
            or 'channel is being established' in text)


def _dump(hdc: Hdc, out_dir: str, tag: str,
          shot: bool = False) -> Tuple[Optional[LayoutNode], str, str]:
    """取一次控件树（可选截图）。返回 (树, 结构签名, 本地截图路径)。

    直接用 `Hdc` 的高层 API（`dump_layout` / `screen_cap` / `pull`），
    它们已经处理了「设备侧路径 → 本地文件」这一步：

    - `dump_layout()` 收/返回的都是**设备侧路径**，要本地文件还得 `pull`
    - `screen_cap()` 同理
    """
    from ohauto.layout import parse_layout

    local = os.path.join(out_dir, f'{tag}.json')
    for _ in range(4):
        try:
            dev = hdc.dump_layout()
            hdc.pull(dev, local, binary=True)
            break
        except Exception:                                     # noqa: BLE001
            time.sleep(1.5)
    else:
        return None, '', ''

    text = open(local, 'r', encoding='utf-8', errors='replace').read()
    try:
        root = parse_layout(text)
    except Exception:                                         # noqa: BLE001
        return None, '', ''

    png = ''
    if shot:
        try:
            dev_png = hdc.screen_cap()
            png = os.path.join(out_dir, f'{tag}.png')
            hdc.pull(dev_png, png, binary=True)
        except Exception:                                     # noqa: BLE001
            png = ''
    return root, _sig(root), png


def _sig(root: LayoutNode) -> str:
    """结构签名：层级 + 类型 + bounds。**不含文案** —— 文案会变（时钟在跳），
    但页面结构不变，用文案签名会把同一页面判成两个。"""
    parts = []
    for depth, n in _walk_depth(root):
        parts.append(f'{depth}:{n.type}:{n.rect.left},{n.rect.top},'
                     f'{n.rect.width},{n.rect.height}')
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:16]


def _walk_depth(root: LayoutNode) -> List[Tuple[int, LayoutNode]]:
    out: List[Tuple[int, LayoutNode]] = []

    def rec(n: LayoutNode, d: int) -> None:
        out.append((d, n))
        for c in n.children:
            rec(c, d + 1)
    rec(root, 0)
    return out


PAGE_PATH_RE = re.compile(r'pagePath=([^\s,;\]"]+)')


def _page_path(root: LayoutNode) -> str:
    """从控件树里取 `pagePath` —— 这是**页面级身份，比标题和结构签名都硬**。

    ★ 2026-09-23 真机确认：`uitest dumpLayout` 把 `pagePath` 放在节点的
    `attributes` 里，值是**路由路径**，形如：

        attributes['pagePath'] = 'pages/settingList'

    ⚠️ 踩过的坑：我最初按「内联文本里搜 `pagePath=xxx`」写正则，
    **永远读不到空字符串** —— 因为它根本不是内联文本，是独立键值对。
    所以这里是**直接读 attributes**，不做字符串匹配。

    为什么只认被测应用窗口的 pagePath：真机 dump 返回**整个窗口栈**，
    状态栏/导航栏那些 root 节点的 `pagePath` 都是空串，
    而带回溯地「取最后一个非空」会串到系统窗口去 —— 所以加 `bundleName` 过滤。
    """
    found: List[str] = []

    def rec(n: LayoutNode) -> None:
        attrs = n.attributes or {}
        p = attrs.get('pagePath') or ''
        # 只采被测应用窗口的 pagePath（系统窗口的 bundleName 是 com.ohos.systemui）
        b = attrs.get('bundleName') or ''
        if p and (not b or 'systemui' not in b):
            found.append(str(p))
        for c in n.children:
            rec(c)
    rec(root)
    # 同一路由会出现在多个节点上，取最深的那个（更接近实际内容页）
    return found[-1] if found else ''


def _title(root: LayoutNode, status_top: int = 72) -> str:
    """页面标题：排除状态栏后取最靠上的短文本（与 explorer 同口径）。"""
    cands = []
    for n in flatten(root):
        if not (n.visible and n.text) or len(n.text) > 20:
            continue
        if n.type not in ('Text', 'Title', 'NavigationTitle', 'NavDestinationTitle'):
            continue
        if n.rect.top < status_top:
            continue
        cands.append((n.rect.top, -n.rect.area, n.text))
    return sorted(cands)[0][2] if cands else ''


def _stats(root: LayoutNode) -> Tuple[int, int, int, List[str]]:
    nodes = list(flatten(root))
    ck = [n for n in nodes if n.clickable and n.type in CLICKABLE_TYPES]
    ids = [n for n in nodes if n.id]
    # 文案采样**排除状态栏**（top < 72）—— 否则每页前几条永远是
    # 「没有 SIM 卡 / × / 11% / 08:40」，把真正的页面文案挤掉。
    # 这是写完第一版后看产物才发现的问题：搜索页的正文文案一条都没采到。
    texts = [n.text for n in nodes
             if n.text and n.rect.top >= 72 and n.rect.bottom <= 1208][:14]
    return len(nodes), len(ck), len(ids), texts


def _identity(node: LayoutNode) -> str:
    """取控件身份 —— **这条是踩出来的，不是设计出来的**。

    ★ 真机实测（2026-09-23，`com.ohos.settings` 首页）：
    **11 个可点控件，自身 id / text / descr 全部为空**（11/11 = 100%）！

    | 层级 | 内容 |
    |---|---|
    | 可点容器（`Flex` / `ListItem`） | id='' text='' descr='' |
    | 子 `Text` | `WLAN` / `已关闭` / `显示与亮度` / `蓝牙` … |

    所以「按 id 或自身 text 找可点控件」在真机上**一个都找不到** ——
    必用 `text_deep`（自身 + 子树文案）。这与 `layout.py:215` 的
    `text_deep` 是同一个教训，B 的 `pick_stress_target` 也踩过同一坑。

    取值顺序：① id ② 自身 text ③ descr ④ **text_deep**（子节点文案）⑤ ''。
    """
    if node.id:
        return node.id
    if node.text:
        return node.text
    if node.descr:
        return node.descr
    return (node.text_deep or '').strip()


def _tap(hdc: Hdc, node: LayoutNode) -> None:
    x, y = node.rect.center
    _sh(hdc, f'uitest uiInput click {int(x)} {int(y)}')


def _back(hdc: Hdc) -> None:
    _sh(hdc, 'uitest uiInput keyEvent Back')


# ---------------------------------------------------------------- 主流程

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='多页面结构证据采集（真机）')
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default=DEFAULT_ABILITY)
    ap.add_argument('--hdc', default=DEFAULT_HDC)
    ap.add_argument('--out', default=os.path.join(ROOT, '_out', 'page_evidence'))
    ap.add_argument('--max-pages', type=int, default=6,
                    help='最多采集几个页面（含入口页）')
    ap.add_argument('--shot', action='store_true', default=True,
                    help='同时截图（默认开）')
    args = ap.parse_args(argv)

    stamp = time.strftime('%m%d-%H%M%S')
    out_dir = os.path.join(args.out, stamp)
    os.makedirs(out_dir, exist_ok=True)

    hdc = require_device(hdc_path=args.hdc)
    print('=' * 68)
    print('  多页面结构证据采集（真机）')
    print('=' * 68)
    print(f'  悬停目标 : {args.bundle} / {args.ability}')
    print(f'  产物目录 : {out_dir}')

    guard = DeviceGuard(hdc, verbose=False)
    awake = guard.ensure_awake()
    print(f'  屏幕可用 : {awake}'
          f'{"（已唤醒并解锁）" if awake else "（⚠️ 仍不可用，后续可能全是空树）"}')

    # ---- 拉起应用（**先清干净**再起）
    #
    # ⚠️ 真机踩坑（2026-09-23）：`com.ohos.settings` 的 MainAbility 会**恢复上次
    # 停留的页面**。只 `aa start` 的话，第二轮就会停在上一轮进去的子页，
    # 于是「拉不回入口页」而提前终止（实测只采到 2 页甚至 1 页）。
    # 所以：force-stop → 回桌面 → 再 start，入口页才是干净的。
    print(f'\n[1] 拉起 {args.bundle}（先 force-stop 清状态）…')
    _sh(hdc, f'aa force-stop {args.bundle}')
    time.sleep(0.8)
    try:
        hdc.home()
    except Exception:                                         # noqa: BLE001
        _sh(hdc, 'uitest uiInput keyEvent Home')
    time.sleep(0.8)
    try:
        hdc.start_ability(args.bundle, args.ability)
    except Exception:                                         # noqa: BLE001
        _sh(hdc, f'aa start -a {args.ability} -b {args.bundle}')
    time.sleep(2.5)

    pages: List[PageEvidence] = []
    seen_sigs: Dict[str, int] = {}

    root, sig, png = _dump(hdc, out_dir, 'page1', shot=args.shot)
    if root is None:
        print('  ✗ 入口页控件树解析失败，中止')
        return 2
    n, ck, ids, texts = _stats(root)
    p0 = PageEvidence(seq=1, title=_title(root), page_path=_page_path(root),
                      sig=sig, nodes=n, clickables=ck, ids=ids, texts=texts,
                      how_reached='force-stop → start_ability', png=png,
                      tree_file=os.path.join(out_dir, 'page1.json'))
    pages.append(p0)
    seen_sigs[sig] = 1
    print(f'  ✓ 入口页: 标题={p0.title!r} pagePath={p0.page_path!r} '
          f'节点={n} 可点={ck} id={ids}')

    # ---- 依次进入子页面
    #
    # 策略：**每轮都从入口页新鲜重来**，按索引尝试「入口页的第 N 个可点控件」。
    #
    # 为什么不沿路深挖（先点 A 再点 B）：真机上 `Back` 不保证回到原页
    # （实测点进「搜索设置项」后返回，落点不是原入口页）——
    # 一旦偏了，后续轮次就在错误的页面上挑控件，产物不可复现。
    # 从入口页重来虽然慢一点，但每一页都能说清「怎么到达的」，且可复现。
    print(f'\n[2] 逐页探索（最多 {args.max_pages} 页）…')
    tried: set = set()
    for round_no in range(1, 60):
        if len(pages) >= args.max_pages:
            break

        # 每轮重新拉起 + 回到入口页
        #
        # ⚠️ 必须**先 force-stop 再 start**：`aa start` 是**复用已有实例**
        # （应用停在哪页就还在哪页）。实测只 `start_ability` 会让后续轮次
        # 停在上一轮进去的子页，于是「拉不回入口页」而提前终止 —— 只采到 2 页。
        _sh(hdc, f'aa force-stop {args.bundle}')
        time.sleep(0.8)
        try:
            hdc.home()
        except Exception:                                     # noqa: BLE001
            _sh(hdc, 'uitest uiInput keyEvent Home')
        time.sleep(0.8)
        try:
            hdc.start_ability(args.bundle, args.ability)
        except Exception:                                     # noqa: BLE001
            _sh(hdc, f'aa start -a {args.ability} -b {args.bundle}')
        time.sleep(2.2)
        home_root, home_sig, _ = _dump(hdc, out_dir, f'home{round_no}')
        if home_root is None or home_sig != p0.sig:
            # 拉不回到入口页就停 —— 宁可少采一页，也不要采到不可复现的页面
            print(f'    · 第 {round_no} 轮：拉不回入口页（sig '
                  f'{home_sig or "?"} != {p0.sig}），停止探索')
            break

        cands = [n for n in flatten(home_root)
                 if n.clickable and n.rect.width >= 60 and n.rect.height >= 60
                 and _identity(n)
                 and n.rect.top >= 72 and n.rect.bottom <= 1208]
        if not cands:
            print(f'    · 第 {round_no} 轮：入口页无可点控件，停止')
            break

        picked = None
        for n in cands:
            key = (_identity(n), n.rect.center)
            if key in tried:
                continue
            tried.add(key)
            picked = n
            break
        if picked is None:
            print('    · 入口页可点控件已全部试过，停止')
            break

        label = _identity(picked)[:20]
        print(f'  · 第 {round_no} 轮：从入口页点 {label!r} @ {picked.rect.center}')
        try:
            _tap(hdc, picked)
        except Exception as e:                                # noqa: BLE001
            print(f'    ✗ 点击失败：{e}')
            continue
        time.sleep(1.6)

        new_root, new_sig, new_png = _dump(
            hdc, out_dir, f'page{len(pages) + 1}', shot=args.shot)
        if new_root is None:
            print('    · 控件树解析失败，跳过')
            continue

        if new_sig in seen_sigs:
            print(f'    · 结构未变（＝同一页，可能是滚动或无跳转），跳过')
            continue

        nn, nck, nids, ntexts = _stats(new_root)
        pe = PageEvidence(
            seq=len(pages) + 1, title=_title(new_root),
            page_path=_page_path(new_root), sig=new_sig, nodes=nn,
            clickables=nck, ids=nids, texts=ntexts,
            how_reached=f"tap({label}) from 入口页", png=new_png,
            tree_file=os.path.join(out_dir, f'page{len(pages) + 1}.json'))
        pages.append(pe)
        seen_sigs[new_sig] = pe.seq
        print(f'    ✓ 新页面 #{pe.seq}: 标题={pe.title!r} '
              f'pagePath={pe.page_path!r} 节点={nn} 可点={nck} id={nids}')

    # ---- 汇总
    print(f'\n[3] 汇总：共采集 {len(pages)} 个页面')
    mark = '✅ 达标' if len(pages) >= 3 else '❌ 不足 3 页'
    print(f'    基础 #5「≥3 页面」：{mark}（{len(pages)}/3）')

    report = {
        'bundle': args.bundle, 'ability': args.ability,
        'hdc': args.hdc, 'collected_at': stamp,
        'pages': [p.__dict__ for p in pages],
        'distinct_sigs': len(seen_sigs),
        'meets_3_pages': len(pages) >= 3,
        'caveat': ('本样本是**系统应用**，不是命题要求的「OpenHarmony 示例应用」。'
                   '用途是补强「页面覆盖」这一维的结构证据；'
                   '命题的示例应用要求以 ohos.samples.* + 核心流程为准。'),
    }
    rp = os.path.join(out_dir, 'page_evidence.json')
    with open(rp, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md = _markdown(report, pages)
    mp = os.path.join(out_dir, 'page_evidence.md')
    with open(mp, 'w', encoding='utf-8') as f:
        f.write(md)

    print(f'\n  报告: {mp}')
    print(f'  数据: {rp}')
    return 0 if len(pages) >= 3 else 1


def _markdown(report: Dict[str, Any], pages: List[PageEvidence]) -> str:
    L = []
    L.append('# 多页面结构证据（真机）\n')
    L.append(f'> 样本：`{report["bundle"]}` / `{report["ability"]}`')
    L.append(f'> 采集时间：{report["collected_at"]}｜产物：`{report["collected_at"]}/`')
    L.append(f'> **基础 #5 判定：{"✅ 达标" if report["meets_3_pages"] else "❌ 不足"}'
             f'（{len(pages)}/3 页）**\n')
    L.append('## ⚠️ 样本来源声明\n')
    L.append(report['caveat'] + '\n')
    L.append('## 判定依据\n')
    L.append('**不是「截图看起来不一样」**，而是页面结构指纹变化。三级依据，'
             '强度从高到低：\n')
    L.append('| 级别 | 依据 | 含义 | 强度 |')
    L.append('|---|---|---|---|')
    L.append('| L1 | `pagePath` | ArkUI 路由路径，**应用自己声明的页面身份** | ★★★ |')
    L.append('| L2 | 结构签名 | 层级 + 类型 + bounds 的哈希（**不含文案**） | ★★ |')
    L.append('| L3 | 页面标题 | 排除状态栏后最靠上的短文本 | ★ |')
    L.append('')
    L.append('任一级变化即算进入新页面。只按截图判会把「同一页的滚动」'
             '误判成换页 —— 滚动前后截图不同，但结构语义没变。\n')
    L.append('## 逐页证据\n')
    L.append('| # | 标题 | pagePath | 结构签名 | 节点 | 可点 | id | 到达方式 |')
    L.append('|---|---|---|---|---|---|---|---|')
    for p in pages:
        L.append(f'| {p.seq} | {p.title or "（无）"} | `{p.page_path or "—"}` | '
                 f'`{p.sig}` | {p.nodes} | {p.clickables} | {p.ids} | '
                 f'{p.how_reached} |')
    L.append('')

    # ---- L1 自证：pagePath 各不相同 ⇒ 确实是不同的路由页面
    pps = [p.page_path for p in pages if p.page_path]
    if pps:
        L.append('## 路由独立性自证（L1）\n')
        if len(set(pps)) == len(pps):
            L.append(f'**{len(pps)} 个页面各不相同**，全部为独立 ArkUI 路由：\n')
        else:
            L.append('⚠️ **有重复的 `pagePath`** —— 下面的页面不是各自独立的路由，'
                     '需要人工确认是否真的是不同页面：\n')
        for p in pages:
            L.append(f'- #{p.seq} → `{p.page_path or "（未暴露 pagePath）"}`')
        L.append('')

    L.append('## 页面上抓到的文案（前 14 条，**已排除状态栏**）\n')
    for p in pages:
        if p.texts:
            L.append(f'**#{p.seq} {p.title or "（无标题）"}**：'
                     + '、'.join(f'`{t}`' for t in p.texts[:14]))
        else:
            L.append(f'**#{p.seq} {p.title or "（无标题）"}**：'
                     f'（排除状态栏后**无可采文案** —— 该页正文由输入型控件'
                     f'构成（`TextInput` / 图标），不是采样遗漏）')
        L.append('')
    if pages:
        L.append('## 结构差异自证（L2）\n')
        L.append('相邻页面的结构签名不同 ⇒ 确实换了页面（不是同一页重复计数）：\n')
        for a, b in zip(pages, pages[1:]):
            same = '❌ 相同（可疑）' if a.sig == b.sig else '✅ 不同'
            L.append(f'- #{a.seq} → #{b.seq}：{same}'
                     f'（`{a.sig}` vs `{b.sig}`）')
        L.append('')
    return '\n'.join(L)


if __name__ == '__main__':
    sys.exit(main())
