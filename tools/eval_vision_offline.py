# -*- coding: utf-8 -*-
"""离线视觉评测工装 —— 不用真机就能给多模态通道出数字。

为什么有这个东西
----------------
命题挑战目标第一条是「支持多模态大模型理解应用截图和布局结构」，而我们的
现状是：`ohauto/vision.py` 的 Provider 抽象、HybridLocator、TieredVisionLocator
全都写完了，**一次真实推理都没跑过** —— 没有 key，也没有评测口径。

更麻烦的是「评测要等设备」这个假设是错的。2026-09-19 的真机采集里已经躺着一批
**截图 + 控件树配对样本**（8 个应用，7 组有 png），设备在不在都能跑。

⭐ 必须说清楚的口径问题（否则报出来的「准确率」站不住）
--------------------------------------------------------
本工装的标注**来自控件树自身的文案**（`LayoutNode.label`，真机上可交互容器的
文案在子节点里，所以实际取的是 `text_deep`）。这意味着：

    控件树通道在这个评测集上是「开卷考试」—— 它天然能命中绝大多数标注项。

所以本工装报告的指标**不是视觉通道的绝对准确率**，而是：

    视觉通道与控件树通道的「一致性」 —— 即 IoU 交叉校验能不能过（A2 的判据）。

视觉通道真正的**增量价值**只体现在控件树盲区（可交互但人话文案不可用的控件），
那部分没有自动标注、只能人评。本工装把盲区清单原样列出来，供后续人工标注，
**不拿它充数当准确率**。

用法
----
    python tools/eval_vision_offline.py --list          # 只看评测集，不跑模型
    python tools/eval_vision_offline.py --dry           # 零成本基线（纯控件树）
    python tools/eval_vision_offline.py                 # 用 mock provider 通管道
    python tools/eval_vision_offline.py --probe         # 体检 key 兼不兼容
    python tools/eval_vision_offline.py --provider openai   # 真模型出数字

环境变量（key 绝不写进代码，见 A1 红线）::

    OHAUTO_VISION_BASE_URL   OHAUTO_VISION_API_KEY   OHAUTO_VISION_MODEL

推荐配置（2026-09-22 核实 DeepSeek 官方文档）::

    export OHAUTO_VISION_BASE_URL=https://api.deepseek.com
    export OHAUTO_VISION_API_KEY=sk-...
    export OHAUTO_VISION_MODEL=deepseek-flash

选它的三个理由：
1. `deepseek-flash`（= DeepSeek-V4.1-Flash）**原生支持图像理解**，
   且是 OpenAI 兼容的 `image_url` + base64 data URL 形态 —— 与
   `OpenAICompatibleProvider` 零改动对接；
2. **同一个 key 通吃文本与视觉**，正好落在 `vision.py` 的回退链设计上：
   只配 `OHAUTO_LLM_*` 一套，B 的 NL→用例生成与 A 的视觉通道一起跑起来，
   挑战 #1 与 #2 一次解决，不用申请两个账号；
3. 便宜：单图 token 上限 1024，Flash 输入空闲时段 1 元/百万 token。
   本工装全量 20 条一轮 **不到一毛钱**。

⚠️ 三个坑（踩过或官方明文）::

    · 别用 `deepseek-v4-pro` —— 官方价格表里「图像理解」一栏为**不支持**，
      带图请求直接 400。
    · 旧模型名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 已下线，
      会被路由到 V4.1-Flash。能跑，但**别写死在代码里**。
    · `available()` 陷阱：`vision.py` 的环境变量回退链会读 `OHAUTO_LLM_*`，
      若那边配的是纯文本模型名，`available()` 返回 True 却带图 400 ——
      「看起来配好了、其实不能看图」。`--probe` 的第 3 关专抓这个。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    from ohauto.layout import LayoutNode, Rect, flatten, parse_layout   # noqa: E402
    from ohauto.vision import (MockProvider, OpenAICompatibleProvider,  # noqa: E402
                               TieredVisionLocator, VisionError,
                               VisionProvider, build_provider)
except ModuleNotFoundError as exc:                      # pragma: no cover
    # 交付包里只带了 tools/ 和 datasets/，没带 ohauto/ ——
    # 独立解压会在这里炸成一句看不懂的 ModuleNotFoundError。
    # 说清楚怎么用，比让人去猜 sys.path 强。
    raise SystemExit(
        f'找不到 ohauto 包（{exc}）。\n'
        '本工具依赖仓库里的 ohauto/，用法是把 tools/ 放到 ohauto-system 仓库根目录下，'
        '然后在仓库根执行：\n'
        '    python tools/eval_vision_offline.py --dry\n'
        '（它会自己把仓库根加进 sys.path；放到别处运行会找不到 ohauto。）') from exc

#: 样本目录（按顺序取第一个存在的），仓库内自包含副本。
DEFAULT_SAMPLES = (
    os.path.join(ROOT, 'datasets', 'real_samples_20260919'),
)

DEFAULT_OUT = os.path.join(ROOT, '_out', 'vision_eval')

#: 判定「命中」的 IoU 阈值。0.3 与 HybridLocator 的交叉校验阈值保持一致，
#: 0.5 是通用的严格口径；中心命中是「有没有指对东西」的宽松判据 ——
#: 容器节点与文字节点天然不同尺寸，只报 IoU 会低估视觉通道。
IOU_STRICT = 0.5
IOU_LOOSE = 0.3


# ---------------------------------------------------------------- 数据结构

@dataclass
class Sample:
    """一组真机「截图 + 控件树」配对样本。"""
    name: str
    png: str
    tree: str
    bundle: str = ''
    ability: str = ''
    screen: Tuple[int, int] = (0, 0)
    png_size: Optional[Tuple[int, int]] = None
    root: Optional[LayoutNode] = None
    #: 可交互节点的标注可用性统计
    n_visible: int = 0
    n_interactive: int = 0
    n_readable: int = 0
    n_id_only: int = 0
    n_bare: int = 0
    #: 控件树自洽性统计（真机 dumpLayout 的坐标可能不可信，见 _audit_tree）
    n_oob: int = 0
    n_oob_side: int = 0
    n_off_screen: int = 0
    n_zero_area: int = 0
    n_dup_bounds: int = 0
    max_oob_px: int = 0
    oob_examples: List[str] = field(default_factory=list)


@dataclass
class Item:
    """一条评测项。"""
    sample: str
    instruction: str
    gt: Rect
    page: str = ''
    node_path: str = ''
    ambiguous: bool = False


@dataclass
class Result:
    item: Item
    pred_rect: Optional[Rect] = None
    pred_label: str = ''
    iou: float = 0.0
    center_hit: bool = False
    source: str = '-'
    lat_ms: float = 0.0
    n_model_calls: int = 0
    uncertain: bool = False
    #: 该条最后一次模型调用的原话（L1 静态命中时为空）——
    #: 诊断 MISS 成因全靠它：`[]` / 低置信度被滤 / 指错地方，三种修法不同。
    raw_text: str = ''


@dataclass
class Report:
    provider: str
    samples: List[Sample] = field(default_factory=list)
    items: List[Item] = field(default_factory=list)
    results: List[Result] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)
    ambiguous: int = 0
    stats: Dict[str, Any] = field(default_factory=dict)
    error: str = ''


# ---------------------------------------------------------------- 图像工具

def png_size(path: str) -> Optional[Tuple[int, int]]:
    """只读 PNG 头拿尺寸 —— 纯标准库，不引 Pillow。

    这一步不是为了好看：截图尺寸决定了模型的输出坐标系。若截图是 720×1280
    而控件树 bounds 也是 720×1280，则两通道坐标**同系**，可以直接比 IoU。
    一旦两者不一致（有人传了缩放过的图），模型返回的 bbox 会整体偏移，
    而 IoU 全灭的表象会被误读成「模型不行」。所以这个检查必须显式报出来。
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(26)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    w, h = struct.unpack('>II', head[16:24])
    return int(w), int(h)


def make_png(w: int, h: int, rgb: Tuple[int, int, int] = (200, 60, 60)) -> bytes:
    """生成纯色 PNG（体检用的最小图）。手写而非硬编码 base64 —— 硬编码的
    blob 错了不会报错，只会让体检结论假阴性。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return (struct.pack('>I', len(data)) + payload
                + struct.pack('>I', zlib.crc32(payload) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    row = b'\x00' + bytes(rgb) * w
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
            + chunk(b'IDAT', zlib.compress(row * h)) + chunk(b'IEND', b''))


# ---------------------------------------------------------------- 样本装载

def find_samples_dir(explicit: str = '') -> str:
    if explicit:
        return explicit
    for c in DEFAULT_SAMPLES:
        if os.path.isdir(c):
            return c
    return ''


def load_samples(src: str) -> List[Sample]:
    """扫目录，把同名 png/json 配成对。配不上对的不算数（宁可少也不编）。"""
    if not os.path.isdir(src):
        raise SystemExit(f'样本目录不存在: {src}')
    out: List[Sample] = []
    for fn in sorted(os.listdir(src)):
        if not fn.endswith('.json') or fn.endswith('.meta.json'):
            continue
        stem = fn[:-5]
        tree = os.path.join(src, fn)
        png = os.path.join(src, stem + '.png')
        if not os.path.isfile(png):
            continue
        meta_path = os.path.join(src, stem + '.meta.json')
        bundle = ability = ''
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, encoding='utf-8') as f:
                    meta = json.load(f)
                bundle = str(meta.get('bundle', ''))
                ability = str(meta.get('ability', ''))
            except (OSError, json.JSONDecodeError):
                pass
        s = Sample(name=stem, png=png, tree=tree, bundle=bundle, ability=ability)
        s.png_size = png_size(png)
        try:
            s.root = parse_layout(tree)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f'[warn] {stem} 控件树解析失败，跳过: {type(e).__name__}: {e}')
            continue
        top = s.root.rect
        s.screen = (top.right, top.bottom)
        _audit_tree(s)
        out.append(s)
    return out


#: 判定「子节点跑出父容器」的容差。真机布局存在 1~2px 的取整误差，
#: 容差太小会把正常的像素抖动报成缺陷。
OOB_TOL = 8

#: 明确的占位/脏文案。真机样本里出现过 `Text='nan'`（应用自身没填内容的
#: 兜底字符串），拿它当指令去问模型是无效题。
JUNK_LABELS = {'nan', 'null', 'undefined', 'none', 'nil', '-', '--', '...'}


def readable_text(node: LayoutNode) -> str:
    """取「人话指令」用的文案。

    刻意**不用** `node.label` —— label 在没文案时会退到 `text_deep`（把整棵
    子树文案拼一串），真机上会得到
    `'蓝牙 已关闭 移动网络 显示与亮度 声音 生物识别和密码 应用 存储 隐私'`
    这种没人口语的指令，拿去问模型属于**刁难题**，不是评测。

    取值顺序：自身 text → 自身 descr → 自身 hint → 子树里第一个有文案的
    （真机上可交互容器的文案在子节点，第一个通常是行标题，如「蓝牙」）。
    """
    for v in (node.text, node.descr, node.hint):
        if (v or '').strip():
            return v.strip()
    for k in node.walk():
        if (k.text or '').strip():
            return k.text.strip()
    return ''


def containment_gap(node: LayoutNode) -> Tuple[int, int]:
    """节点相对**最紧的祖先容器**的越界像素数。

    返回 `(向上越界, 任意方向越界)`。为什么要单独看「向上」：
    列表滚动时子项跑到可视区**下方**是正常的裁剪现象，但跑到容器
    **上方**往往意味着坐标偏移没叠加（2026-09-22 在设备自带「设置」页
    实测到：内层 List `[42,156][678,207]` 的子项报 `[42,72][678,122]`，
    越界 84px ≈ 一个行高，且与页面标题「设置」的 `[36,72][684,156]`
    区域重叠 —— 子节点不在父节点内，是纯粹的逻辑矛盾）。
    """
    up = any_gap = 0
    p = node.parent
    r = node.rect
    while p is not None:
        pr = p.rect
        if pr.area > 0:
            up = max(up, pr.top - r.top)
            any_gap = max(any_gap, pr.top - r.top, r.bottom - pr.bottom,
                          pr.left - r.left, r.right - pr.right)
        p = p.parent
    return up, any_gap


def _audit_tree(s: Sample) -> None:
    """标注可用性 + 坐标自洽性。

    前半段就是「视觉通道不可绕过」那张表的来源；三档分法（真机形态决定了
    这个分法才准）：

        人话文案可用  text / descr / hint / 子树文案 任一非空 —— 控件树能给语义
        仅 id         只有 id（计算器按键 '7'）—— 机器可读，但不是「描述性指令」
        无任何标识    连 id 都没有（纯 Canvas 图标）—— 控件树彻底哑火

    后半段回答另一个问题：**控件树的坐标本身可不可信**。这是评测的前置条件
    —— 拿不可信的坐标当 ground truth，量出来的准确率是假的。
    """
    if s.root is None:
        return
    nodes = [n for n in s.root.walk() if n.visible]
    s.n_visible = len(nodes)
    seen_bounds: Dict[Tuple[int, int, int, int], int] = {}
    sw, sh = s.screen
    for n in nodes:
        r = n.rect
        key = (r.left, r.top, r.right, r.bottom)
        seen_bounds[key] = seen_bounds.get(key, 0) + 1
        if r.area <= 0:
            s.n_zero_area += 1
        if r.right > sw + OOB_TOL or r.bottom > sh + OOB_TOL or r.left < -OOB_TOL:
            s.n_off_screen += 1
        # ⚠️ 只统计**正面积**节点。真机状态栏里有一批零宽退化节点
        # （如 `Row [218,0][218,72]`，宽=0），它们的 top 是 0 而父容器 top 是 32，
        # 会被算成「向上越界 32px」—— 实测 7 个样本里每个都稳定出现 8 个、
        # 每个都正好 32px。这个整齐的 32 不是缺陷，是噪声：不做这个过滤的话
        # 「越界数」会变成每样本恒定 8，把真缺陷（app_settings 的 84px 嵌套
        # List 偏移）淹没掉。
        if r.area <= 0:
            continue
        up, any_gap = containment_gap(n)
        if up > OOB_TOL:
            s.n_oob += 1
            s.max_oob_px = max(s.max_oob_px, up)
            if len(s.oob_examples) < 4 and n.is_interactive():
                s.oob_examples.append(
                    f'`{n.type}` {r.to_dict()} 向上越界 {up}px '
                    f'文案={readable_text(n)[:12]!r}')
        elif any_gap > OOB_TOL:
            s.n_oob_side += 1
        if not n.is_interactive():
            continue
        s.n_interactive += 1
        if readable_text(n):
            s.n_readable += 1
        elif (n.id or '').strip():
            s.n_id_only += 1
        else:
            s.n_bare += 1
    s.n_dup_bounds = sum(v - 1 for v in seen_bounds.values() if v > 1)


def build_items(samples: Sequence[Sample]) -> Tuple[List[Item], Dict[str, int]]:
    """从控件树造评测项。

    标注源 = `readable_text(node)`（人话文案），只取「可交互 + 有文案 + 坐标自洽」
    的节点。只带 id 的节点（计算器按键 `'7'`）不进来 —— 那部分进盲区清单走人评。

    两道必要的清洗：

    1. **同标签合并**：真机上同一个目标会被父子两级重复收录
       （`ListItem` 与内层 `Flex` 同 bounds、同文案）。它们不是「歧义」，
       是同一个东西 —— 取**面积最小的那层**（最紧的 bbox 才是可用目标）。
       真正同名但**不重叠**的才标 `ambiguous` 并从分母剔除。
    2. **坐标可疑剔除**：`containment_gap` 报向上越界的节点，其坐标不可信，
       不能当 ground truth。
    """
    items: List[Item] = []
    skipped = {'不可交互': 0, '无人话文案': 0, '区域退化': 0,
               '整屏容器': 0, '坐标可疑': 0, '占位文案': 0,
               '重复收录(已合并)': 0}
    for s in samples:
        if s.root is None:
            continue
        screen_area = max(1, s.screen[0] * s.screen[1])
        cands: List[Tuple[str, LayoutNode]] = []
        for n in flatten(s.root, only_visible=True):
            if not n.is_interactive():
                skipped['不可交互'] += 1
                continue
            label = readable_text(n)
            if not label:
                skipped['无人话文案'] += 1
                continue
            if label.lower() in JUNK_LABELS:
                skipped['占位文案'] += 1
                continue
            r = n.rect
            if r.area <= 0 or r.width < 4 or r.height < 4:
                skipped['区域退化'] += 1
                continue
            if r.area / screen_area > 0.5:
                # 覆盖半个屏幕以上的可交互容器不是「定位目标」，
                # 拿它当 GT 会让中心命中判据失去意义。
                skipped['整屏容器'] += 1
                continue
            up, _ = containment_gap(n)
            if up > OOB_TOL:
                skipped['坐标可疑'] += 1
                continue
            cands.append((label, n))

        # --- 同标签合并：重叠的取最紧的那层 ---
        groups: Dict[str, List[LayoutNode]] = {}
        for label, n in cands:
            groups.setdefault(label, []).append(n)
        for label, ns in groups.items():
            if len(ns) == 1:
                keep = ns
            else:
                keep = []
                for n in sorted(ns, key=lambda x: x.rect.area):
                    if any(n.rect.overlap_ratio(k.rect) > 0 for k in keep):
                        skipped['重复收录(已合并)'] += 1
                        continue
                    keep.append(n)
            ambiguous = len(keep) > 1
            for n in keep:
                items.append(Item(
                    sample=s.name, instruction=label, gt=n.rect,
                    page=str(n.attributes.get('pagePath', '') or ''),
                    node_path=n.path, ambiguous=ambiguous))
    return items, skipped


# ---------------------------------------------------------------- 评测

def evaluate(items: Sequence[Item], samples: Sequence[Sample],
             provider_name: str, limit: int = 0, verbose: bool = False,
             delay: float = 0.0, no_thinking: bool = False) -> Report:
    by_name = {s.name: s for s in samples}
    if provider_name in ('openai', 'openai-compatible'):
        # 用 RecordingProvider 而不是裸的 build_provider：全量跑时 MISS 的
        # 成因必须能从报告里读出来（见 RecordingProvider 的 docstring）。
        provider: VisionProvider = RecordingProvider.from_env()
        if no_thinking:
            provider = NoThinkingProvider.from_env()
    else:
        provider = build_provider(provider_name)
    loc = TieredVisionLocator(provider=provider, verbose=verbose)
    rep = Report(provider=getattr(provider, 'name', provider_name))
    rep.samples = list(samples)
    rep.items = list(items)

    todo = [it for it in items if not it.ambiguous]
    rep.ambiguous = len(items) - len(todo)
    if limit:
        todo = todo[:limit]

    for idx, it in enumerate(todo, 1):
        s = by_name[it.sample]
        w, h = s.screen
        calls0 = loc.model_calls
        if hasattr(provider, 'last_text'):
            provider.last_text = ''          # 防止读到上一条的残留
        t0 = time.perf_counter()
        pred = None
        err = ''
        try:
            pred = loc.locate(s.root, s.png, it.instruction, w, h,
                              page_fingerprint=f'{it.sample}:{it.page}')
        except VisionError as e:
            err = f'{type(e).__name__}: {e}'
        lat = (time.perf_counter() - t0) * 1000.0

        res = Result(
            item=it,
            pred_rect=pred.rect if pred else None,
            pred_label=pred.label if pred else '',
            source=getattr(pred, 'source', '-') if pred else '-',
            lat_ms=lat,
            n_model_calls=loc.model_calls - calls0,
            uncertain=bool(getattr(pred, 'uncertain', False)),
            raw_text=str(getattr(provider, 'last_text', '') or ''),
        )
        if pred is not None:
            res.iou = pred.rect.overlap_ratio(it.gt)
            res.center_hit = (it.gt.contains(*pred.rect.center)
                              or pred.rect.contains(*it.gt.center))
        if err:
            res.pred_label = f'(错误) {err}'
        rep.results.append(res)
        if verbose or idx % 10 == 0:
            mark = 'OK ' if res.iou >= IOU_STRICT else (
                '~  ' if res.center_hit else 'MISS')
            print(f'[{idx}/{len(todo)}] {mark} {it.sample:14s} '
                  f'{it.instruction[:16]:16s} IoU={res.iou:.2f} '
                  f'[{res.source}] {lat:.0f}ms')
            # MISS 就地打印模型原话 —— 否则要翻回 JSON 才知道是「没找到」
            # 还是「找到了但被阈值滤掉」，排查一次要多跑一轮。
            if mark == 'MISS':
                why = (f'原话={res.raw_text[:200]!r}' if res.raw_text
                       else '未调模型（L1 已裁决）')
                print(f'           ↳ {why}')
        if delay:
            time.sleep(delay)

    rep.stats = loc.stats()
    rep.stats['n_evaluated'] = len(rep.results)
    return rep


def tree_baselines(items: Sequence[Item],
                   samples: Sequence[Sample]) -> Dict[str, Any]:
    """控件树单通道的命中率 —— 三条口径，各自含义明确。

    **视觉通道值不值得上，取决于控件树单通道能到多少**，所以基线必须和视觉
    跑在同一批评测项、同一套判据上。三条分开报，是因为「控件树不行」这个
    结论太粗 —— 真正要区分的是下面三种情况：

    | key | 口径 | 回答的问题 |
    |---|---|---|
    | `l1_now`  | 只看**可交互**节点，blob 只有自身字段 | 现状接线能免掉多少模型调用（成本） |
    | `l1_deep` | 只看可交互节点，blob 补 `text_deep` | 修好线索后能免掉多少 |
    | `tree_all`| **全树**节点（含不可交互的 `Text`），补 `text_deep` | 控件树的信息上限 |

    `l1_now` 与 `l1_deep` 之差 = 「线索漏拼子树文案」的代价；
    `l1_deep` 与 `tree_all` 之差 = 「只查可交互节点」的代价。
    两者都不是视觉通道能补的，只有 `tree_all` 之下的残余才是视觉的用武之地。
    """
    by = {s.name: s for s in samples}
    modes = {
        'l1_now': (True, False),
        'l1_deep': (True, True),
        'tree_all': (False, True),
    }
    acc = {k: {'hit': 0, 'n': 0} for k in modes}
    misses: Dict[str, List[str]] = {k: [] for k in modes}
    for it in items:
        if it.ambiguous:
            continue
        s = by.get(it.sample)
        if s is None or s.root is None:
            continue
        kw = it.instruction.strip().lower()
        if not kw:
            continue
        for mode, (only_interactive, with_deep) in modes.items():
            found = False
            for n in flatten(s.root, only_visible=True,
                             only_interactive=only_interactive):
                vals = [n.type, n.id, n.text, n.descr, n.hint]
                if with_deep:
                    vals.append(n.text_deep)
                if kw not in ' '.join(v for v in vals if v).lower():
                    continue
                if (n.rect.overlap_ratio(it.gt) >= IOU_LOOSE
                        or it.gt.contains(*n.rect.center)
                        or n.rect.contains(*it.gt.center)):
                    found = True
                    break
            acc[mode]['n'] += 1
            if found:
                acc[mode]['hit'] += 1
            elif len(misses[mode]) < 5:
                misses[mode].append(f'{it.sample}/{it.instruction}')
    out: Dict[str, Any] = {}
    for k, v in acc.items():
        out[k] = round(v['hit'] / v['n'], 4) if v['n'] else 0.0
        out[k + '_miss'] = misses[k]
    return out


def read_page(provider: OpenAICompatibleProvider, image: str,
              screen: Tuple[int, int]) -> List[Dict[str, Any]]:
    """让模型把整页可见文字**念一遍**（带位置），用于诊断定位失败。

    为什么需要这个：定位失败时，`[]` 有两种截然不同的含义 ——
    ① 模型没认出那个目标；② 那个位置上**根本没有**该文字（截图与控件树
    不一致、界面已变、目标被遮挡）。两者的修法完全相反，而定位接口
    只回一个空数组，分不出来。

    实测（2026-09-22）：`app_settings` 的「蓝牙」在三次运行里稳定返回 `[]`，
    而控件树说它就在 `[36,156][684,213]`。到底谁对？——问模型「这页有哪些字」
    就能定论：若念出来的清单里有「蓝牙」，说明是定位 prompt 的问题；
    若没有，说明那一行在画面上不是「蓝牙」，是**控件树与界面不一致**。

    副产品：这份「模型念的页面文字 + 位置」可以作为**控件树盲区**的人工标注
    起点（见报告第五节那 27 个盲区控件 —— 它们没有任何自动标注可用）。
    """
    prompt = ('请逐行列出这张截图里**所有可见的文字**及其位置。'
              f'截图尺寸 {screen[0]}×{screen[1]} 像素。'
              '请只输出 JSON 数组，不要其他内容，每个元素形如：'
              '{"text": "该处文字", "bbox": [left, top, right, bottom]}。'
              'bbox 使用截图像素坐标，原点在左上角。')
    with open(image, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('ascii')
    payload = {
        'model': provider.model, 'temperature': 0,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url',
             'image_url': {'url': f'data:image/png;base64,{b64}'}},
        ]}],
    }
    body = provider._post(payload)                       # noqa: SLF001
    try:
        text = str(body['choices'][0]['message']['content'])
    except (KeyError, IndexError, TypeError):
        return []
    m = re.search(r'\[.*\]', text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: List[Dict[str, Any]] = []
    for it in arr if isinstance(arr, list) else []:
        if isinstance(it, dict) and it.get('bbox'):
            out.append({'text': str(it.get('text', '')),
                        'rect': Rect.parse(it['bbox'])})
    return out


def align_rows(model_items: Sequence[Dict[str, Any]],
               rows: Sequence[Tuple[str, Rect]]
               ) -> Tuple[List[Tuple[str, Rect, Optional[str], float]],
                          List[Tuple[str, Rect]]]:
    """逐行如实对齐：每个控件树行，找出它 rect **里面**的画面文字。

    返回 `(逐行结果, 没被任何行覆盖的画面文字)`。
    逐行结果每项是 `(控件树文案, 控件树 rect, 画面文字或 None, 重合度)`。

    为什么不做「一对一匹配」：真机上一个文字框可能落在**多个**控件树行里
    （容器行 + 行内控件），强行一对一反而会掩盖真相 —— 2026-09-22 第一版
    用贪心一对一，结果「蓝牙」抢走了「设置」、把「应用」挤成"对不上"，
    输出的误配比真相还难读。**这里是诊断工具，不是求解器 —— 如实并列，
    让人自己看出问题更可靠。**

    判定规则（看那一列就够）：
        控件树文案 == 画面文字 → ✅ 一致
        两者都在但不等        → ⚠️ **位置对得上、文案不一致**
        （这一栏一出现，就说明控件树与截图不一致 —— 不是模型的问题）
        画面文字为空          → ❌ 该 rect 里没有任何文字
    """
    out: List[Tuple[str, Rect, Optional[str], float]] = []
    covered = set()
    for label, rect in rows:
        best: Optional[Tuple[float, int]] = None
        cx, cy = rect.center
        for i, g in enumerate(model_items):
            if not (rect.contains(*g['rect'].center)
                    or rect.overlap_ratio(g['rect']) >= 0.3):
                continue
            gx, gy = g['rect'].center
            dist = ((gx - cx) ** 2 + (gy - cy) ** 2) ** 0.5
            if best is None or dist < best[0]:
                best = (dist, i)
        if best is None:
            out.append((label, rect, None, 0.0))
            continue
        g = model_items[best[1]]
        covered.add(best[1])
        out.append((label, rect, str(g['text']),
                    rect.overlap_ratio(g['rect'])))
    uncovered = [(str(g['text']), g['rect'])
                 for i, g in enumerate(model_items) if i not in covered]
    return out, uncovered


def dedupe_rows(rows: Sequence[Tuple[str, Rect]],
                screen: Tuple[int, int] = (0, 0)) -> List[Tuple[str, Rect]]:
    """同一文案的多层节点只留**最紧的那个**；再剔掉覆盖大半屏的容器行。

    真机上父子两级同文案很常见（`ListItem` 与内层 `Flex`），容器那个 rect
    大得多，留着会把对齐全搅乱。容器行（如整块 `List`）本身也不是
    「一行」，同样要剔。
    """
    screen_area = max(1, screen[0] * screen[1])
    groups: Dict[str, List[Rect]] = {}
    for t, r in rows:
        if r.area <= 0:
            continue
        if screen[0] and r.area / screen_area > 0.5:
            continue
        groups.setdefault(t, []).append(r)
    out: List[Tuple[str, Rect]] = []
    for t, rs in groups.items():
        for r in sorted(rs, key=lambda x: x.area):
            if any(r.overlap_ratio(k) > 0.3 for k in
                   [o for u, o in out if u == t]):
                continue
            out.append((t, r))
    return out


def page_intent(provider: OpenAICompatibleProvider, image: str,
                screen: Tuple[int, int]) -> Dict[str, Any]:
    """让模型输出**页面意图 / 核心操作入口 / 潜在测试路径**。

    这是命题挑战目标第一条的后半句：「识别页面意图、核心操作入口、潜在测试路径」。
    前半句「理解截图和布局结构」已由 bbox 定位落地（见本工装主流程），
    但后半句是**另一个问题** —— 不是「某某控件在哪」，而是「这个页面上
    值得测什么、从哪进去」。两者的答案结构不同，所以用独立的提问。

    为什么要它：探索式测试的**入口选择**目前靠控件树启发式（标题长度、
    关键词加权），在控件树哑火的页面上（真机 37% 的可交互控件无人话文案）
    就基本失去依据。让多模态模型给一份「核心入口 + 测试路径」的候选，
    正好补上这一段 —— 而且它是**可人工复核的**（每条都带理由和风险）。

    返回 `{}` 表示模型没按格式回；调用方应据此降级，而不是编造。
    """
    prompt = (
        '你是移动应用测试专家。分析这张应用截图，只输出一个 JSON 对象'
        '（不要 markdown 代码块、不要解释文字），结构如下：\n'
        '{\n'
        '  "page_intent": "一句话说明这个页面是做什么的",\n'
        '  "core_entries": [{"label": "操作入口名称", "bbox": [left, top, right, bottom],'
        ' "why": "为什么它是核心入口（一句话）"}],\n'
        '  "test_paths": [{"name": "测试路径名称", "steps": ["步骤1", "步骤2"],'
        ' "risk": "这条路径可能暴露什么问题"}]\n'
        '}\n'
        f'截图尺寸 {screen[0]}×{screen[1]} 像素；bbox 使用截图像素坐标，'
        '原点在左上角。core_entries 与 test_paths 各给 3~5 条，'
        '按重要性排序。')
    with open(image, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode('ascii')
    payload = {
        'model': provider.model, 'temperature': 0,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url',
             'image_url': {'url': f'data:image/png;base64,{b64}'}},
        ]}],
    }
    body = provider._post(payload)                       # noqa: SLF001
    try:
        text = str(body['choices'][0]['message']['content'])
    except (KeyError, IndexError, TypeError):
        return {}
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def compare_reports(paths: Sequence[str], out_dir: str) -> str:
    """读两份（或多份）`vision_eval.json`，出逐条对照报告。

    为什么单独做一个模式：本项目要反复做「两组配置跑同一批项」的实验
    （思考开/关、改 `_hints()` 前后、换模型前后），而肉眼比两份 20 行表格
    极易漏项。这个模式还会**自动分辨系统性差异与随机性差异**：

        两次都未命中 → 系统性（换配置也救不了，得改 prompt/指令/标注）
        只有一次未命中 → 随机性（说明那份报告的数字不可复现）

    后者尤其重要：`--no-thinking` 下我们传的 `temperature: 0` 才真正生效，
    理论上应当可复现。若仍出现「同一项一次中一次不中」，
    那就说明**即使关掉思考，准确率也谈不上可复现** —— 这条必须写进验证报告。
    """
    datas: List[Dict[str, Any]] = []
    for p in paths:
        with open(p, encoding='utf-8') as f:
            datas.append(json.load(f))
    if len(datas) < 2:
        raise SystemExit('对照至少需要两份报告')

    names = [d.get('provider', f'#{i}') for i, d in enumerate(datas)]
    # 以第一份的评测项顺序为基准，后面按 (sample, instruction) 对齐
    def key_of(r: Dict[str, Any]) -> str:
        return f'{r["sample"]}::{r["instruction"]}'

    by_run = [{key_of(r): r for r in d.get('results', [])} for d in datas]
    order = [key_of(r) for r in datas[0].get('results', [])]

    L: List[str] = ['# 视觉通道分组对照\n']
    L.append('| 组 | Provider |')
    L.append('|---|---|')
    for i, n in enumerate(names):
        L.append(f'| #{i + 1} | `{n}` |')
    L.append('')

    L.append('## 一、总体\n')
    heads = ['指标'] + [f'#{i + 1}' for i in range(len(datas))]
    L.append('| ' + ' | '.join(heads) + ' |')
    L.append('|' + '---|' * len(heads))
    ov = [d.get('summary', {}).get('overall', {}) for d in datas]
    for label, k, fmt in (
            ('n', 'n', '{}'), ('IoU@0.5', 'iou@0.5', '{:.1%}'),
            ('中心命中', 'center_hit', '{:.1%}'), ('平均 IoU', 'mean_iou', '{:.3f}'),
            ('L1 免调模型', 'l1_free_ratio', '{:.1%}'),
            ('模型调用数', 'model_calls', '{}'),
            ('p50 延迟', 'lat_p50_ms', '{:.0f}ms'),
            ('p95 延迟', 'lat_p95_ms', '{:.0f}ms')):
        cells = [fmt.format(o.get(k, 0)) for o in ov]
        L.append(f'| {label} | ' + ' | '.join(cells) + f' |')
    tot = [sum(r['lat_ms'] for r in d.get('results', [])) / 1000.0
           for d in datas]
    L.append('| 总耗时 | ' + ' | '.join(f'{t:.1f}s' for t in tot) + ' |')
    L.append('')

    L.append('## 二、逐条对照\n')
    L.append('| 指令 | ' + ' | '.join(
        f'#{i + 1} 命中/IoU/延迟' for i in range(len(datas))) + ' |')
    L.append('|' + '---|' * (len(datas) + 1))
    decisive: Dict[str, List[bool]] = {}
    for k in order:
        instr = k.split('::', 1)[1]
        cells = []
        hits: List[bool] = []
        for run in by_run:
            r = run.get(k)
            if r is None:
                cells.append('（缺）')
                hits.append(False)
                continue
            ok = bool(r.get('center_hit'))
            hits.append(ok)
            cells.append(f'{"✅" if ok else "❌"} {r.get("iou", 0):.2f} / '
                         f'{r.get("lat_ms", 0):.0f}ms')
        decisive[k] = hits
        L.append(f'| {instr} | ' + ' | '.join(cells) + ' |')
    L.append('')

    n_run = len(datas)
    both = [k for k, h in decisive.items() if all(h)]
    never = [k for k, h in decisive.items() if not any(h)]
    flaky = [k for k, h in decisive.items()
             if any(h) and not all(h)]
    L.append('## 三、差异归因\n')
    L.append(f'- 各组**都命中**：{len(both)} 条')
    L.append(f'- 各组**都未命中**（**系统性** —— 换配置救不了，'
             f'要改 prompt / 指令 / 标注）：{len(never)} 条')
    if never:
        for k in never:
            L.append(f'  - `{k.split("::", 1)[1]}`（{k.split("::")[0]}）')
    L.append(f'- **时中时不中**（**随机性** —— 该组数字不可复现）：'
             f'{len(flaky)} 条')
    if flaky:
        for k in flaky:
            marks = '、'.join(
                f'#{i + 1}{"中" if decisive[k][i] else "不中"}'
                for i in range(n_run))
            L.append(f'  - `{k.split("::", 1)[1]}`（{k.split("::")[0]}）：{marks}')
        L.append('')
        L.append('⚠️ 出现随机性差异时，**不要**把某一组的准确率当成稳定指标报出去；'
                 '要么多跑几轮取区间，要么先查为什么没锁住随机性'
                 '（模型侧采样、服务端批处理都可能是来源，'
                 '`temperature: 0` 不等于绝对确定）。')
    L.append('')

    L.append('## 四、失败项的模型原话（诊断用）\n')
    for i, d in enumerate(datas):
        rows = [r for r in d.get('results', []) if not r.get('center_hit')]
        L.append(f'### #{i + 1} `{names[i]}` —— {len(rows)} 条未命中\n')
        if not rows:
            L.append('（无）\n')
            continue
        L.append('| 指令 | 预测 bbox | 模型原话 |')
        L.append('|---|---|---|')
        for r in rows:
            raw = (r.get('raw_text', '') or '（未调模型）')
            raw = raw[:200].replace('|', '\\|').replace('\n', ' ')
            L.append(f'| {r["instruction"]} | `{r.get("pred")}` | `{raw}` |')
        L.append('')

    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, 'vision_compare.md')
    with open(dst, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    return dst


def find_box_reuse(results: Sequence[Result],
                   iou_threshold: float = 0.9) -> List[Tuple[str, str, str,
                                                             float]]:
    """找「同一轮里两个不同指令被回了同一个框」。

    这是视觉定位最隐蔽的一类错误，因为它**不影响单条判分以外的任何指标**：
    每条都有框、格式合法、位置也不是乱指（往往是邻行的真实位置），
    所以单看一行的 IoU 会以为是「模型定位不准」。

    实测（2026-09-22，app_settings）：
        指令「应用」        → 框 [42,465][678,549]
        指令「生物识别和密码」 → 框 [42,465][678,549]   ← 同一个框
    模型还给这个框贴了不同的 `label`，即 **bbox 与 label 自相矛盾**。

    判据：同一 `sample` 内、指令不同、预测框 IoU ≥ 阈值 → 判为复用。
    跨样本不算（不同截图恰巧同坐标是正常的，例如底部导航栏）。
    """
    out: List[Tuple[str, str, str, float]] = []
    rs = [r for r in results if r.pred_rect is not None]
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            a, b = rs[i], rs[j]
            if a.item.sample != b.item.sample:
                continue
            if a.item.instruction == b.item.instruction:
                continue
            iou = a.pred_rect.overlap_ratio(b.pred_rect)
            if iou >= iou_threshold:
                out.append((a.item.sample, a.item.instruction,
                            b.item.instruction, iou))
    return out


def summarize(rep: Report) -> Dict[str, Any]:
    """算指标。分样本 + 合计，另给延迟分位。"""
    def agg(rs: Sequence[Result]) -> Dict[str, Any]:
        n = len(rs)
        if not n:
            return {'n': 0}
        strict = sum(1 for r in rs if r.iou >= IOU_STRICT)
        loose = sum(1 for r in rs if r.iou >= IOU_LOOSE)
        center = sum(1 for r in rs if r.center_hit)
        ious = sorted(r.iou for r in rs)
        lats = sorted(r.lat_ms for r in rs)
        by_tree = sum(1 for r in rs if r.source == 'tree' and r.n_model_calls == 0)
        return {
            'n': n,
            'iou@0.5': round(strict / n, 4),
            'iou@0.3': round(loose / n, 4),
            'center_hit': round(center / n, 4),
            'mean_iou': round(sum(ious) / n, 4),
            'l1_free_ratio': round(by_tree / n, 4),
            'lat_p50_ms': round(lats[n // 2], 1),
            'lat_p95_ms': round(lats[min(n - 1, int(n * 0.95))], 1),
            'model_calls': sum(r.n_model_calls for r in rs),
            'uncertain': sum(1 for r in rs if r.uncertain),
        }

    out: Dict[str, Any] = {'overall': agg(rep.results), 'per_sample': {}}
    names = []
    for r in rep.results:
        if r.item.sample not in names:
            names.append(r.item.sample)
    for nm in names:
        out['per_sample'][nm] = agg([r for r in rep.results
                                     if r.item.sample == nm])
    return out


# ---------------------------------------------------------------- 报告

def write_report(rep: Report, summ: Dict[str, Any], out_dir: str,
                 samples_dir: str,
                 skipped: Optional[Dict[str, int]] = None) -> Tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    md = os.path.join(out_dir, 'vision_eval.md')
    js = os.path.join(out_dir, 'vision_eval.json')

    L: List[str] = []
    L.append('# 视觉通道离线评测报告（多模态 a24）\n')
    L.append(f'> Provider: `{rep.provider}` ｜ 样本目录: `{samples_dir}`\n')
    L.append(f'> 评测项 {summ["overall"].get("n", 0)} 条'
             f'（歧义剔除 {rep.ambiguous} 条）\n')

    L.append('## 一、⚠️ 口径声明（先读这段，再看数字）\n')
    L.append('本表的标注**来自控件树自身的文案**（`readable_text()` 取 '
             '`text`/`descr`/`hint`，真机上退到子树里第一个文案）。'
             '这意味着控件树通道在这个评测集上是**开卷考试**。\n')
    L.append('因此下面这些数字的含义是：\n')
    L.append('- `IoU@0.5 / IoU@0.3`：**视觉通道与控件树通道的一致性**'
             '—— 交叉校验（`HybridLocator.iou_threshold=0.3`）能不能过；\n')
    L.append('- `中心命中`：视觉指的点和控件树指的控件是否同一处'
             '（容器 vs 文字节点天然不同尺寸，只报 IoU 会低估）；\n')
    L.append('- **不是**视觉通道的绝对准确率。绝对准确率必须在控件树盲区上测，'
             '那部分没有自动标注 —— 见**第五节**，本工装只列清单，'
             '**不拿它充数**。\n')

    L.append('## 二、样本与坐标系\n')
    L.append('| 样本 | bundle | 控件树尺寸 | 截图尺寸 | 同系 | 可见节点 | 可交互 | '
             '人话文案可用 | 仅 id | 无标识(盲区) |')
    L.append('|---|---|---|---|---|---|---|---|---|---|')
    for s in rep.samples:
        same = '✅' if (s.screen == s.png_size) else '❌'
        L.append(f'| `{s.name}` | `{s.bundle or "-"}` | '
                 f'{s.screen[0]}×{s.screen[1]} | '
                 f'{s.png_size[0] if s.png_size else "?"}×'
                 f'{s.png_size[1] if s.png_size else "?"} | {same} | '
                 f'{s.n_visible} | {s.n_interactive} | {s.n_readable} | '
                 f'{s.n_id_only} | {s.n_bare} |')
    tot_i = sum(s.n_interactive for s in rep.samples)
    tot_r = sum(s.n_readable for s in rep.samples)
    tot_b = sum(s.n_bare for s in rep.samples)
    tot_d = sum(s.n_id_only for s in rep.samples)
    L.append(f'| **合计** | | | | | '
             f'{sum(s.n_visible for s in rep.samples)} | {tot_i} | {tot_r} | '
             f'{tot_d} | {tot_b} |')
    L.append('')
    L.append('**坐标系同系**那一列是关键：截图与控件树同为 720×1280 时两通道坐标'
             '可以直接比 IoU；若不同系，模型返回的 bbox 会整体偏移，'
             '而 IoU 全灭的表象会被误读成「模型不行」。\n')
    blind_ratio = (tot_b + tot_d) / tot_i if tot_i else 0.0
    if tot_i:
        blind = tot_b + tot_d
        L.append(f'**盲区占比：{blind}/{tot_i} = {blind / tot_i:.1%}** —— '
                 f'这些控件人话文案不可用，控件树通道给不出描述性定位，'
                 f'只能靠视觉语义。**这就是「多模态不可绕过」的真机证据。**\n')

    # ---- 前置条件：控件树坐标本身可不可信 ----
    L.append('## 三、控件树坐标自洽性（评测的前置条件）\n')
    L.append('拿不可信的坐标当 ground truth，量出来的准确率是假的。'
             '所以先查一遍控件树自己是否自洽：子节点的矩形应当落在父容器内。\n')
    L.append('| 样本 | 零面积节点 | 越界节点 | 最大越界 | 屏幕外 | 重复 bounds |')
    L.append('|---|---|---|---|---|---|')
    for s in rep.samples:
        L.append(f'| `{s.name}` | {s.n_zero_area} | {s.n_oob} | '
                 f'{s.max_oob_px}px | {s.n_off_screen} | {s.n_dup_bounds} |')
    L.append('')
    bad = [s for s in rep.samples if s.n_oob and s.max_oob_px > OOB_TOL]
    if bad:
        L.append('**⚠️ 发现真实缺陷（不是评测口径问题，是控件树/坐标本身的问题）：**\n')
        for s in bad:
            L.append(f'- `{s.name}`：{s.n_oob} 个节点越出父容器，'
                     f'最大 {s.max_oob_px}px')
            for ex in s.oob_examples:
                L.append(f'  - {ex}')
        L.append('')
        L.append('根因（2026-09-22 手工核对 `app_settings.json` 原始 JSON 确认，'
                 '不是解析问题）：嵌套 `List` 的内层子项坐标**没有叠加外层偏移**。'
                 '原始 bounds 链条为\n')
        L.append('```\n'
                 'List       [36,156][684,1208]   外层列表\n'
                 ' ListItem  [36,156][684,213]    蓝牙行（坐标正确）\n'
                 '  List     [42,156][678,207]    内层列表（坐标正确）\n'
                 '   ListItem[42,72][678,122]     ← 越界 84px ≈ 一个行高\n'
                 '    Flex   [42,72][678,122] clickable=true  ← 实际会被点的目标\n'
                 '```\n')
        L.append('两个独立的逻辑矛盾（都不依赖任何主观判断）：\n')
        L.append('1. 子节点 `[42,72][678,122]` 不在父容器 `[42,156][678,207]` 内；\n')
        L.append('2. 该 bbox 与页面标题「设置」的 `[36,72][684,156]` **重叠** '
                 '—— 两个不同控件占据了同一块屏幕区域。\n')
        L.append('后果：按控件树坐标点「蓝牙」会落到 y≈97（标题区），'
                 '而真实行中心在 y≈184 —— **点错控件**。\n')
        L.append(f'处理：本工装把向上越界 > {OOB_TOL}px 的节点从评测集中剔除'
                 f'（计入 `坐标可疑`），避免用脏标注量准确率。\n')
    else:
        L.append('✅ 全部样本坐标自洽，ground truth 可用。\n')

    o = summ['overall']
    L.append('## 四、指标（仅标注集）\n')
    L.append('| 范围 | n | IoU@0.5 | IoU@0.3 | 中心命中 | 平均 IoU | '
             'L1 免调模型 | 模型调用 | p50 | p95 |')
    L.append('|---|---|---|---|---|---|---|---|---|---|')
    L.append(f'| **合计** | {o.get("n", 0)} | {o.get("iou@0.5", 0):.1%} | '
             f'{o.get("iou@0.3", 0):.1%} | {o.get("center_hit", 0):.1%} | '
             f'{o.get("mean_iou", 0):.3f} | {o.get("l1_free_ratio", 0):.1%} | '
             f'{o.get("model_calls", 0)} | {o.get("lat_p50_ms", 0)}ms | '
             f'{o.get("lat_p95_ms", 0)}ms |')
    for nm, a in summ['per_sample'].items():
        L.append(f'| `{nm}` | {a["n"]} | {a["iou@0.5"]:.1%} | {a["iou@0.3"]:.1%} | '
                 f'{a["center_hit"]:.1%} | {a["mean_iou"]:.3f} | '
                 f'{a["l1_free_ratio"]:.1%} | {a["model_calls"]} | '
                 f'{a["lat_p50_ms"]}ms | {a["lat_p95_ms"]}ms |')
    L.append('')
    st = rep.stats
    L.append(f'缓存：命中 {st.get("cache_hits", 0)} / 未命中 '
             f'{st.get("cache_misses", 0)} ｜ 缓存命中率 '
             f'{st.get("cache_hit_rate", 0):.1%} ｜ 平均模型耗时 '
             f'{st.get("avg_model_ms", 0)}ms（A4 验收线：≤2s / ≥60%）\n')

    # ---- 对照组：控件树单通道 ----
    bl = tree_baselines(rep.items, rep.samples)
    L.append('### 对照组：控件树**单通道**在同一批项上的命中率\n')
    L.append('| 口径 | 命中率 | 说明 |')
    L.append('|---|---|---|')
    L.append(f'| `l1_now` 现状接线 | {bl.get("l1_now", 0):.1%} | 只看**可交互**节点，'
             f'blob 只拼 `type/id/text/descr/hint` |')
    L.append(f'| `l1_deep` 补齐子树文案 | {bl.get("l1_deep", 0):.1%} | '
             f'同上但追加 `text_deep` |')
    L.append(f'| `tree_all` 全树文案 | {bl.get("tree_all", 0):.1%} | '
             f'含不可交互的 `Text` 节点 —— 控件树的信息上限 |')
    L.append('')
    L.append(f'- `l1_deep − l1_now = ` **'
             f'{bl.get("l1_deep", 0) - bl.get("l1_now", 0):.1%}** '
             f'← 线索漏拼子树文案的代价；\n')
    L.append(f'- `tree_all − l1_deep = ` **'
             f'{bl.get("tree_all", 0) - bl.get("l1_deep", 0):.1%}** '
             f'← 只查可交互节点的代价；\n')
    L.append(f'- 剩下的 **{1 - bl.get("tree_all", 0):.1%}** 是控件树在'
             f'**标注集**上够不着的部分。\n')
    L.append('⚠️ 别把这个数字当「视觉的价值」读 —— 标注集是控件树自己出题，'
             '所以残余天然接近 0（本轮就是 0）。视觉的用武之地在**盲区**'
             f'（第二节的 {blind_ratio:.1%}），以及被剔除的坐标可疑项；'
             '那部分本工装只列清单，**要人评**。\n')
    if bl.get('l1_now_miss'):
        L.append(f'- `l1_now` 未命中的项（前 5）：'
                 f'`{"`, `".join(bl["l1_now_miss"])}`\n')
    L.append('⚠️ **已定位到具体代码位置**（`ohauto/vision.py`）：\n')
    L.append('- `TieredVisionLocator._hints()` / `HybridLocator._hints()` 只送 '
             '`n.text`（自身）；真机上可交互容器自身文案恒为空，所以送进模型的'
             '线索**文本全空**（实测 28 条线索无一带文案）；\n')
    L.append('- 连带 `TieredVisionLocator._static_candidates()` 命中 0 条 → '
             '**L1 永远免不了调模型**，每条都要打到 L2/L3，A4 的「三层成本递增」'
             '实际退化成单层；\n')
    L.append('- `HybridLocator` 树通道构造的 blob 同样不含 `text_deep`'
             '（实测对「蓝牙」判定为 `False`）。\n')
    L.append('建议修法：线索里补 `\'deep\': n.text_deep`，匹配键列表加上 `deep`；'
             '`HybridLocator` 的 blob 追加 `n.text_deep`。'
             '**这是 A 的模块（任务卡 A1/A2/A4），本工装只报不改。**\n')
    L.append('## 五、清洗与盲区清单\n')
    if skipped:
        L.append('评测集构建时被剔除/合并的项：\n')
        for k, v in skipped.items():
            L.append(f'- {k}：{v}')
        L.append('')
    L.append('**盲区（人话文案不可用，需人工标注后才能真正评测视觉的增量价值）：**\n')
    for s in rep.samples:
        n = s.n_id_only + s.n_bare
        if not n:
            continue
        L.append(f'- `{s.name}`：{n} 个（仅 id {s.n_id_only} / 无标识 {s.n_bare}）')
    L.append('')
    L.append('## 六、失败明细（中心未命中）\n')
    miss = [r for r in rep.results if not r.center_hit]
    if not miss:
        L.append('（无）\n')
    else:
        L.append('| 样本 | 指令 | 标注 bbox | 预测 bbox | IoU | 通道 | 模型原话 |')
        L.append('|---|---|---|---|---|---|---|')
        for r in miss[:40]:
            pr = r.pred_rect.to_dict() if r.pred_rect else 'None'
            raw = (r.raw_text[:160].replace('|', '\\|').replace('\n', ' ')
                   if r.raw_text else '（未调模型）')
            L.append(f'| `{r.item.sample}` | {r.item.instruction} | '
                     f'`{r.item.gt.to_dict()}` | `{pr}` | {r.iou:.2f} | '
                     f'{r.source} | `{raw}` |')
        L.append('')
        L.append('**「模型原话」那一列是诊断的关键**（本工装 2026-09-22 补）：\n')
        L.append('- 原话是 `[]` → 模型**真的没找到**该目标（图里没有、'
                 '或描述与画面不符、或被遮挡）→ 改 prompt / 改指令；\n')
        L.append('- 原话里有框但结果 `None` → 候选被 '
                 '`min_confidence`（0.4）**静默滤掉** → 调阈值，不是模型的问题；\n')
        L.append('- 原话里有框且被采纳 → 是**指错了地方** → 看是不是行错位'
                 '（本项目 app_settings「应用」就是偏了一整行）。\n')

    reuse = find_box_reuse(rep.results)
    L.append('### 跨项框复用检查\n')
    if not reuse:
        L.append('✅ 未发现「两个不同指令回同一个框」。\n')
    else:
        L.append(f'⚠️ 发现 **{len(reuse)} 组**：同一轮里不同指令被回了同一个框。'
                 '这类错误单看每行的 IoU 看不出来（位置往往是邻行的真实位置），'
                 '但说明模型的 bbox 与它自己给的 label **自相矛盾**。\n')
        L.append('| 样本 | 指令 A | 指令 B | 框重合度 |')
        L.append('|---|---|---|---|')
        for s, a, b, iou in reuse[:20]:
            L.append(f'| `{s}` | {a} | {b} | {iou:.2f} |')
        L.append('')
        L.append('处理建议：这类错误**不能靠调阈值解决**。可做的有三条 —— '
                 '① 让 prompt 要求一次只回一个目标并给出理由；'
                 '② 拿到多个候选时用控件树做交叉校验（`HybridLocator` 已有 '
                 'IoU 校验，但需要先修好线索漏拼 `text_deep` 的问题）；'
                 '③ 在上层加「两条指令回同框则都判低可信」的守门。\n')
    L.append('## 七、复现\n')
    L.append('```bash\n'
             '# 零成本基线（纯控件树，不调模型）\n'
             'python tools/eval_vision_offline.py --dry\n'
             '# 体检 key\n'
             'python tools/eval_vision_offline.py --probe\n'
             '# 真模型评测\n'
             'python tools/eval_vision_offline.py --provider openai\n'
             '```\n')

    with open(md, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    with open(js, 'w', encoding='utf-8') as f:
        json.dump({
            'provider': rep.provider,
            'samples_dir': samples_dir,
            'ambiguous': rep.ambiguous,
            'skipped': skipped or {},
            'baselines_tree_only': bl,
            'summary': summ,
            'stats': rep.stats,
            'coverage': [{'sample': s.name, 'bundle': s.bundle,
                          'screen': s.screen, 'png_size': s.png_size,
                          'visible': s.n_visible, 'interactive': s.n_interactive,
                          'readable': s.n_readable, 'id_only': s.n_id_only,
                          'bare': s.n_bare, 'oob': s.n_oob,
                          'max_oob_px': s.max_oob_px,
                          'off_screen': s.n_off_screen,
                          'zero_area': s.n_zero_area,
                          'dup_bounds': s.n_dup_bounds} for s in rep.samples],
            'results': [{
                'sample': r.item.sample, 'instruction': r.item.instruction,
                'page': r.item.page, 'gt': r.item.gt.to_dict(),
                'pred': r.pred_rect.to_dict() if r.pred_rect else None,
                'pred_label': r.pred_label, 'iou': round(r.iou, 4),
                'raw_text': r.raw_text[:400],
                'center_hit': r.center_hit, 'source': r.source,
                'lat_ms': round(r.lat_ms, 1),
                'model_calls': r.n_model_calls, 'uncertain': r.uncertain,
            } for r in rep.results],
        }, f, ensure_ascii=False, indent=2)
    return md, js


# ---------------------------------------------------------------- 兼容性体检

class RecordingProvider(OpenAICompatibleProvider):
    """记录模型**原话**，并可选关掉思考模式。

    为什么必须记原话：只看解析后的结果，分不清一次 MISS 的成因 ——
    是模型返回了 `[]`（真的没找到），还是给了框却被置信度阈值滤掉了，
    还是给了个错框？三者的修法完全不同，而报告里它们长得一模一样
    （都是 `预测 bbox = None`）。2026-09-22 全量跑出 2 个 MISS 时正卡在这里。

    刻意继承生产类而不是另写一套请求：必须跑真实生产路径，否则测通了也不算数。
    """
    _NAME = 'openai-compatible'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)      # disable_thinking 由生产类托管
        self.last_text: str = ''
        self.n_http = 0

    # 不再覆写 `from_env`：`disable_thinking` 的转发已经在生产类里做掉，
    # 这里那份覆写是它缺席时的临时补丁 —— 同一件事两处实现必然分叉。

    @property
    def name(self) -> str:                       # type: ignore[override]
        return self._NAME + ('(thinking=disabled)' if self.disable_thinking
                             else '')

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # `thinking` 开关由生产类在 payload 里注入，这里只负责记原话与计数。
        self.n_http += 1
        body = super()._post(payload)
        try:
            self.last_text = str(body['choices'][0]['message']['content'])
        except (KeyError, IndexError, TypeError):
            self.last_text = ''
        return body


class NoThinkingProvider(RecordingProvider):
    """关掉思考模式（**工装侧注入**，不改生产代码）。

    DeepSeek 官方文档明写「**思考模式默认打开**，且 effort 默认 `high`」。
    这在本项目上是有害的：

    1. **延迟**：目标定位是「看图点一个控件」，不需要长推理链。每条请求都先输出
       一段思维链 —— 2026-09-22 全量实测量到 p50 从 1059ms 涨到 3238ms（3.06×）；
    2. **计费**：思维链 token 按**输出**计价（Flash 空闲时段 4 元/百万，
       是输入的 4 倍）；
    3. **口径失效**：思考模式下 `temperature` **不生效**（官方原文：
       「设置参数不会报错，但也不会生效」）。而 `OpenAICompatibleProvider`
       恰好传了 `temperature: 0` —— 也就是说**我们以为自己在求确定性，
       其实没有**。同一张图跑两次可能给出不同 bbox，报告里的「准确率」不可复现。

    注入的字段就是官方 OpenAI 格式的开关：`{"thinking": {"type": "disabled"}}`，
    与 `temperature` 同级（用 openai SDK 时要放 `extra_body`，我们直接发原始
    JSON，所以放顶层）。

    这个开关现在由**生产类** `OpenAICompatibleProvider` 落地（`disable_thinking`
    参数 + payload 注入）；本类只是把它固定打开的一个薄壳。
    """
    _NAME = 'openai-compatible'

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs['disable_thinking'] = True
        super().__init__(*args, **kwargs)


def _norm_base(url: str) -> Tuple[str, str]:
    """归一化 base_url，并说明改了什么。

    最常见的配错有两种：写成 `.../v1/chat/completions`（多写了路径），
    以及只写到域名不写 `/v1`。两种都会 404，但错误信息长得一样，
    所以这里主动纠偏并把纠偏动作报出来。
    """
    u = (url or '').strip().rstrip('/')
    note = ''
    for tail in ('/chat/completions', '/completions'):
        if u.endswith(tail):
            u = u[:-len(tail)]
            note = f'去掉了多余的 {tail}'
            break
    return u, note


def probe(base_url: str, api_key: str, model: str, image: str = '',
          screen: Tuple[int, int] = (720, 1280), timeout: int = 45,
          progress: bool = True, instruction: str = '') -> str:
    """体检「这个 key 兼不兼容」。输出人话结论，不抛异常。

    `progress=True` 时**边跑边打印**。这不是装饰：三关各有一条最长 `timeout`
    秒的网络等待，若把输出攒到最后才打，用户看到的是一个光标停在那里、
    分不清「在跑」还是「卡死」—— 2026-09-22 实测就发生过一次误判。

    `instruction` 必须**是该截图里真实存在的目标**。踩过的坑：本函数曾把指令
    硬编码成 `'设置'`，而样本截图是「备忘录」—— 模型老实返回 `[]`，
    报告里却显示「解析出 0 个候选」，看起来像模型/格式有问题。
    实际上模型是**对的**：备忘录页面上确实没有「设置」。体检用例自己错了，
    却记在模型头上。所以调用方必须传入与截图匹配的指令。
    """
    def _p(msg: str) -> None:
        if progress:
            print(f'[体检] {msg}', flush=True)

    L: List[str] = []
    L.append('# 视觉 Provider 兼容性体检\n')
    base, note = _norm_base(base_url)
    L.append(f'- base_url 归一化 → `{base}`' + (f'（{note}）' if note else ''))
    L.append(f'- model: `{model}`')
    if api_key:
        # 只露**末 4 位**，且不报长度 —— 末 4 位足够让人确认「加载的是哪把 key」，
        # 而前缀+长度+后缀拼起来的信息量没必要给出。报告可能被贴进群里/文档里，
        # 默认按最小暴露处理（2026-09-22 用户主动问过 key 落盘问题后收紧）。
        L.append(f'- api_key: `...{api_key[-4:]}`（已掩码）')
    else:
        L.append('- api_key: **空**')
    # 代理是「key 明明对、却连不上」的头号嫌疑：urllib 默认吃系统代理，
    # 而国内模型端点经代理出去常常 502 / 证书错。实测本机就出现过
    # `Tunnel connection failed: 502 Bad Gateway`。所以显式报出来。
    prox = []
    for k, v in os.environ.items():
        if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy'):
            continue
        if not v:
            continue
        # 代理串里可能带账号密码，报告中只留主机部分
        prox.append(f'`{k}={v.split("@")[-1] if "@" in v else v}`')
    L.append(f'- 代理：{"，".join(prox) if prox else "未设置"}'
             + ('　⚠️ 请求会经代理出去；国内端点请设 `no_proxy` 或临时清空'
                if prox else ''))
    L.append('')

    ok: List[str] = []
    bad: List[str] = []
    _p(f'开始：base_url={base} model={model}（共 4 关，前三关每关最长 {timeout}s）')

    # --- 第 1 关：/models 是否可达（用于分辨「路径错」还是「鉴权错」）---
    L.append('## 第 1 关：GET /models（探路径与鉴权）\n')
    _p('第 1 关：GET /models …')
    _t0 = time.perf_counter()
    models: List[str] = []
    try:
        req = urllib.request.Request(f'{base}/models',
                                     headers={'Authorization': f'Bearer {api_key}'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode('utf-8', 'replace'))
        models = [str(m.get('id', '')) for m in body.get('data', [])
                  if isinstance(m, dict)]
        L.append(f'- HTTP 200，返回 {len(models)} 个模型')
        _p(f'  第 1 关 通过（{time.perf_counter() - _t0:.1f}s，{len(models)} 个模型）')
        if models:
            hit = model in models
            L.append(f'- 目标 model 在列表里：{"是" if hit else "**否** ⚠️"}')
            if not hit:
                L.append(f'- 可用的前 20 个：`{"`, `".join(models[:20])}`')
                bad.append('模型名不在服务端列表里，换一个 model 名')
        ok.append('/models 可达')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:300]
        L.append(f'- HTTP {e.code} —— {detail}')
        _p(f'  第 1 关 失败：HTTP {e.code}（{time.perf_counter() - _t0:.1f}s）')
        if e.code in (401, 403):
            bad.append('鉴权失败（401/403）：key 不对、或该 key 无权调用此模型')
        elif e.code == 404:
            bad.append('404：base_url 路径不对。多数服务要写成 '
                       '`https://host/v1`（**不带** /chat/completions）')
        else:
            bad.append(f'/models 返回 {e.code}，需人工确认')
    except Exception as e:                              # noqa: BLE001
        L.append(f'- 请求异常：{type(e).__name__}: {e}')
        bad.append('网络层就不通：检查 base_url、代理、出网是否被拦')
        _p(f'  第 1 关 异常：{type(e).__name__}（{time.perf_counter() - _t0:.1f}s）')
    L.append('')

    # --- 第 2 关：纯文本 chat（确认对话接口形态）---
    L.append('## 第 2 关：POST /chat/completions（纯文本）\n')
    _p('第 2 关：POST /chat/completions（纯文本）…')
    _t0 = time.perf_counter()
    txt_payload = {
        'model': model, 'temperature': 0,
        'messages': [{'role': 'user',
                      'content': '只输出 JSON 数组 []，不要任何其他字符。'}],
    }
    def _post(payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            f'{base}/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {api_key}'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))

    try:
        body = _post(txt_payload)
        content = str(body['choices'][0]['message']['content'])
        L.append(f'- HTTP 200，模型回答：`{content[:120]}`')
        L.append(f'- 耗时 {time.perf_counter() - _t0:.1f}s'
                 + ('（偏慢，多半是思考模式在跑）'
                    if time.perf_counter() - _t0 > 5 else ''))
        ok.append('chat/completions 可用')
        _p(f'  第 2 关 通过（{time.perf_counter() - _t0:.1f}s）')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:300]
        L.append(f'- HTTP {e.code} —— {detail}')
        bad.append('chat/completions 不可用，见上（若 /models 通而这关 404，'
                   '说明该服务不提供 OpenAI 对话接口）')
        content = ''
        _p(f'  第 2 关 失败：HTTP {e.code}（{time.perf_counter() - _t0:.1f}s）')
    except Exception as e:                              # noqa: BLE001
        L.append(f'- 请求异常：{type(e).__name__}: {e}')
        bad.append('对话接口网络层失败')
        content = ''
        _p(f'  第 2 关 异常：{type(e).__name__}（{time.perf_counter() - _t0:.1f}s）')
    L.append('')

    # --- 第 3 关：带图（这才是决定「能不能做多模态」的一关）---
    L.append('## 第 3 关：POST /chat/completions（带图 base64）\n')
    if image and os.path.isfile(image):
        L.append(f'- 用真机截图：`{os.path.basename(image)}`'
                 f'（{os.path.getsize(image) // 1024} KB）')
    else:
        L.append('- 未找到真机截图，改用合成的 64×64 纯色图')
    L.append(f'- 指令：`{instruction or "（未指定 —— 结果不可解读）"}`'
             f'　⚠️ 指令必须是**这张截图里真实存在**的目标，否则模型返回 `[]` '
             f'是正确行为，不是故障')
    img_text = ''
    n_boxes = 0
    _d3 = 0.0
    _p('第 3 关：POST /chat/completions（带图 base64，走生产 prompt）…')
    # 刻意用生产类 + 生产 prompt，而不是另编一句「这张图里有什么」——
    # 体检要回答的是「**我们的**定位请求能不能跑通」，不是「模型能不能看图」。
    prov = RecordingProvider(base_url=base, api_key=api_key, model=model,
                             timeout=timeout)
    img_path = image
    tmp_path = ''
    if not (image and os.path.isfile(image)):
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix='.png')
        with os.fdopen(fd, 'wb') as f:
            f.write(make_png(64, 64))
        img_path = tmp_path
    try:
        _t3 = time.perf_counter()
        targets = prov.locate(img_path, instruction or '设置',
                              screen[0], screen[1], None)
        _d3 = time.perf_counter() - _t3
        img_text = prov.last_text
        n_boxes = len(targets)
        L.append(f'- HTTP 200，耗时 **{_d3:.1f}s**')
        L.append(f'- 模型原话（前 300 字符）：`{img_text[:300]}`')
        L.append(f'- 走生产解析器 `_parse()` 得到 {n_boxes} 个候选'
                 + (f'，首个={targets[0].rect.to_dict()}'
                    if targets else ''))
        # 坐标系校验：这是最阴的一类失败 —— 模型答对了、格式也对，
        # 但 bbox 用的是 0~1 归一化坐标而不是像素坐标，于是每个框都缩在
        # 屏幕左上角几个像素里，IoU 全 0，表象却是「模型定位不准」。
        # 2026-09-22 实测见过：那次体检 prompt 里给的示例是 `bbox:[0,0,1,1]`，
        # 模型就照抄了示例的坐标约定。**给视觉模型的示例会决定它输出的坐标系。**
        if targets and screen[0] > 100 and screen[1] > 100 \
                and all(t.rect.right <= 2 and t.rect.bottom <= 2 for t in targets):
            L.append('- ❌ **坐标系不符**：bbox 全部 ≤2，是 0~1 归一化坐标，'
                     '而生产 prompt 要求的是像素坐标（本屏 '
                     f'{screen[0]}×{screen[1]}）。调用方拿到这些框会全部失效。')
            bad.append('模型返回归一化坐标而非像素坐标 —— 检查 prompt 里是否'
                       '带了 `[0,0,1,1]` 之类的示例（示例会被照抄）')
        if n_boxes:
            ok.append('多模态输入被接受，且输出能被生产解析器消费')
        else:
            L.append('- ⚠️ 没解析出 bbox。若原话不是 JSON 数组，说明模型没守格式；'
                     '若原话为空、内容在 `reasoning_content` 里，'
                     '说明思考模式把答案吃掉了 —— 加 `--no-thinking` 再试。')
            ok.append('多模态输入被接受（输出格式待确认）')
        _p(f'  第 3 关 通过（{_d3:.1f}s，解析出 {n_boxes} 个候选）')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', 'replace')[:400]
        L.append(f'- HTTP {e.code} —— {detail}')
        low = detail.lower()
        if any(k in low for k in ('image', 'vision', 'modal', 'unsupported')):
            bad.append('**这个模型不支持图片输入**（服务端明确提到 image/vision）。'
                       '换一个多模态模型，例如 `deepseek-flash` / qwen-vl / glm-4v')
        else:
            bad.append(f'带图请求返回 {e.code}，见原始报错')
        _p(f'  第 3 关 失败：HTTP {e.code}')
    except Exception as e:                              # noqa: BLE001
        L.append(f'- 请求异常：{type(e).__name__}: {e}')
        bad.append('带图请求网络层失败')
        _p(f'  第 3 关 异常：{type(e).__name__}')
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    L.append('')

    # --- 第 4 关：思考模式开/关的耗时对比 ---
    # DeepSeek 官方文档明写「思考模式默认打开，effort 默认 high」，而目标定位
    # 不需要长推理链。这一关把代价量出来，而不是靠猜。
    L.append('## 第 4 关：思考模式开 vs 关（延迟/结果对比）\n')
    _p('第 4 关：同一个请求关掉思考模式，对比耗时 …')
    L.append('- 官方文档：思考模式**默认打开**，effort 默认 `high`；'
             '思考模式下 `temperature` **不生效**（设了不报错也不生效）。\n')
    try:
        nt = NoThinkingProvider(base_url=base, api_key=api_key, model=model,
                                timeout=timeout)
        _t4 = time.perf_counter()
        nt_targets = nt.locate(img_path, instruction or '设置',
                               screen[0], screen[1], None)
        _d4 = time.perf_counter() - _t4
        L.append(f'- 关思考：耗时 **{_d4:.1f}s**，解析出 {len(nt_targets)} 个候选'
                 + (f'，首个={nt_targets[0].rect.to_dict()}'
                    if nt_targets else ''))
        if _d3 > 0:
            L.append(f'- 开关对比：**{_d3:.1f}s → {_d4:.1f}s**'
                     f'（{"省了 " + format(_d3 - _d4, ".1f") + "s" if _d4 < _d3 else "没省，反而更慢"}）\n')
    except Exception as e:                              # noqa: BLE001
        L.append(f'- 对比失败（不影响前面结论）：{type(e).__name__}: {e}')
    L.append('')
    if n_boxes:
        L.append('> 注：思维链 token 按**输出**计价（Flash 空闲时段 4 元/百万，'
                 '是输入的 4 倍），所以关掉思考模式同时省时间和钱。'
                 '详见 `--no-thinking`。\n')

    # --- 结论 ---
    L.append('## 结论\n')
    if not bad:
        L.append('✅ **可以用。** 直接跑：\n')
        L.append('```bash\n'
                 'export OHAUTO_VISION_BASE_URL=...\n'
                 'export OHAUTO_VISION_API_KEY=...\n'
                 'export OHAUTO_VISION_MODEL=...\n'
                 'python tools/eval_vision_offline.py --provider openai\n'
                 '```')
    else:
        L.append('❌ **还不能直接用**，需要处理：\n')
        for b in bad:
            L.append(f'- {b}')
    L.append('')
    if ok:
        L.append('已通过的关卡：' + '、'.join(ok))
    return '\n'.join(L)


# ---------------------------------------------------------------- CLI

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description='多模态视觉通道离线评测（不需要真机）')
    ap.add_argument('--samples', default='', help='样本目录（含同名 png/json）')
    ap.add_argument('--out', default=DEFAULT_OUT, help='报告输出目录')
    ap.add_argument('--provider', default='mock', choices=['mock', 'openai'],
                    help='mock=通管道；openai=真模型（读 OHAUTO_VISION_*）')
    ap.add_argument('--list', action='store_true', help='只打印评测集，不跑模型')
    ap.add_argument('--dry', action='store_true',
                    help='零成本基线：不调模型，只看纯控件树（mock + L1）')
    ap.add_argument('--probe', action='store_true',
                    help='兼容性体检：检查 key 能不能用、模型支不支持图片')
    ap.add_argument('--compare', nargs=2, metavar=('DIR_A', 'DIR_B'),
                    help='对照两份已跑完的报告目录（各含 vision_eval.json），'
                         '输出逐条对照 + 随机性归因')
    ap.add_argument('--read-page', metavar='SAMPLE', default='',
                    help='让模型把某样本截图的可见文字全部念一遍（带位置），'
                         '用于诊断定位失败是「模型没认出」还是「画面里没有」')
    ap.add_argument('--page-intent', metavar='SAMPLE', default='',
                    help='让模型输出该页面的意图、核心操作入口、潜在测试路径'
                         '（对应命题挑战目标第一条的后半句）')
    ap.add_argument('--no-thinking', action='store_true',
                    help='关掉模型思考模式（\u4ec5 openai 生效）。DeepSeek 默认'
                         '开着思考，定位任务不需要，关掉省时间省钱且让 '
                         'temperature 恢复生效（可复现）')
    ap.add_argument('--limit', type=int, default=0, help='只跑前 N 条（省钱）')
    ap.add_argument('--delay', type=float, default=0.0,
                    help='每条之间 sleep 秒数（避限流）')
    ap.add_argument('--v', action='store_true', help='逐条打印')
    args = ap.parse_args(argv)

    if args.compare:
        paths = [os.path.join(d, 'vision_eval.json') for d in args.compare]
        miss_f = [p for p in paths if not os.path.isfile(p)]
        if miss_f:
            print('找不到报告文件：' + '，'.join(miss_f))
            return 1
        dst = compare_reports(paths, args.out)
        with open(dst, encoding='utf-8') as f:
            print(f.read())
        print(f'[写入] {dst}')
        return 0

    src = find_samples_dir(args.samples)
    if not src:
        print('找不到样本目录。用 --samples 指定，或把 09-19 真机采集的 '
              'samples/ 拷到 datasets/real_samples_20260919/')
        return 1               # 前置缺（非设备类）→ 未达标，2 专留给设备不在场

    samples = load_samples(src)
    if not samples:
        print(f'目录里没有可用的配对样本: {src}')
        return 1
    items, skipped = build_items(samples)
    print(f'样本 {len(samples)} 组 ｜ 评测项 {len(items)} 条 ｜ '
          f'跳过 {skipped}')

    if args.probe:
        base = os.environ.get('OHAUTO_VISION_BASE_URL',
                              os.environ.get('OHAUTO_LLM_BASE_URL', '')).strip()
        key = os.environ.get('OHAUTO_VISION_API_KEY',
                             os.environ.get('OHAUTO_LLM_API_KEY', '')).strip()
        model = os.environ.get('OHAUTO_VISION_MODEL',
                               os.environ.get('OHAUTO_LLM_MODEL', '')).strip()
        missing = [n for n, v in (('OHAUTO_VISION_BASE_URL', base),
                                  ('OHAUTO_VISION_API_KEY', key),
                                  ('OHAUTO_VISION_MODEL', model)) if not v]
        if missing:
            print('体检前先配环境变量：' + ', '.join(missing))
            print('（key 走环境变量，不要写进代码/报告 —— A1 红线）')
            return 1
        # 体检指令必须取自**同一张截图**上真实存在的评测项，
        # 否则模型返回 `[]` 是正确行为却会被读成故障（踩过）。
        probe_img = samples[0].png
        probe_instr = ''
        for it in items:
            if it.sample == samples[0].name and not it.ambiguous:
                probe_instr = it.instruction
                break
        report = probe(base, key, model, image=probe_img,
                       screen=samples[0].screen, instruction=probe_instr)
        os.makedirs(args.out, exist_ok=True)
        dst = os.path.join(args.out, 'vision_probe.md')
        with open(dst, 'w', encoding='utf-8') as f:
            f.write(report)
        print(report)
        print(f'\n[写入] {dst}')
        return 0

    if args.read_page:
        name = args.read_page
        if name not in {s.name for s in samples}:
            print('没有这个样本：' + name)
            print('可选：' + '、'.join(s.name for s in samples))
            return 1
        smp = next(s for s in samples if s.name == name)
        try:
            prov = RecordingProvider.from_env(disable_thinking=True)
        except VisionError as e:
            print(f'装配 Provider 失败: {type(e).__name__}: {e}')
            print('提示：先跑 --probe 体检环境变量。')
            return 1
        print(f'[念页面] {name}.png  {smp.screen[0]}×{smp.screen[1]} …',
              flush=True)
        got = read_page(prov, smp.png, smp.screen)
        print(f'[念页面] 模型念出 {len(got)} 处文字\n')
        rows = dedupe_rows([(readable_text(n), n.rect)
                            for n in flatten(smp.root, only_visible=True)
                            if n.is_interactive() and readable_text(n)],
                           smp.screen)
        aligned, uncovered = align_rows(got, rows)

        lines: List[str] = [f'# 念页面一致性检查 —— `{name}`\n']
        lines.append(f'> 截图 `{name}.png` {smp.screen[0]}×{smp.screen[1]}'
                     f' ｜ 模型念出 {len(got)} 处文字'
                     f' ｜ 控件树可交互行（去重去容器后）{len(rows)} 个\n')
        lines.append('判定：`✅一致` = 控件树文案与画面文字相同；'
                     '`⚠️不一致` = **位置对得上但文案不同**（控件树与截图不符，'
                     '不是模型的问题）；`❌空` = 该 rect 里没有任何文字。\n')
        lines.append('| 控件树文案 | 控件树 rect | 该位置画面上的文字 | 判定 |')
        lines.append('|---|---|---|---|')
        n_ok = n_bad = n_empty = 0
        print(f'{"控件树文案":16s} {"控件树 rect":26s} '
              f'{"该位置画面上的文字":20s} 判定')
        print('-' * 78)
        for label, rect, txt, ov in aligned:
            d = f'({rect.left},{rect.top},{rect.right},{rect.bottom})'
            if txt is None:
                n_empty += 1
                mark = '❌空'
            elif txt == label:
                n_ok += 1
                mark = '✅一致'
            else:
                n_bad += 1
                mark = '⚠️不一致'
            print(f'{label[:14]:16s} {d:26s} {str(txt or "—")[:18]:20s} {mark}')
            lines.append(f'| {label} | `{d}` | {txt or "—"} | {mark} |')
        lines.append(f'\n**汇总**：一致 {n_ok} ｜ '
                     f'⚠️ 不一致 {n_bad} ｜ ❌ 空 {n_empty}\n')
        print(f'\n汇总：一致 {n_ok} ｜ ⚠️ 不一致 {n_bad} ｜ ❌ 空 {n_empty}')

        if uncovered:
            lines.append('## 没被任何控件树行覆盖的画面文字\n')
            lines.append('含状态栏等非可交互元素属正常；'
                         '若出现**可交互按钮的文案**，说明控件树漏采。\n')
            lines.append('| 画面文字 | 位置 |')
            lines.append('|---|---|')
            print('\n没被任何控件树行覆盖的画面文字：')
            for t, r in uncovered:
                d = (r.left, r.top, r.right, r.bottom)
                print(f'  {t[:20]:22s} {d}')
                lines.append(f'| {t} | `{d}` |')

        if n_bad:
            lines.append('\n## ⚠️ 有「位置对得上但文案不一致」的行\n')
            lines.append('这说明**控件树与截图不是同一时刻/同一滚动状态**。'
                         '这类行的 ground truth 不可用，'
                         '在评测里必须剔除，否则会冤枉模型。\n')

        os.makedirs(args.out, exist_ok=True)
        dst = os.path.join(args.out, f'read_page_{name}.md')
        with open(dst, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        print(f'\n[写入] {dst}')
        return 0

    if args.page_intent:
        name = args.page_intent
        if name not in {s.name for s in samples}:
            print('没有这个样本：' + name)
            print('可选：' + '、'.join(s.name for s in samples))
            return 1
        smp = next(s for s in samples if s.name == name)
        try:
            prov = RecordingProvider.from_env(disable_thinking=True)
        except VisionError as e:
            print(f'装配 Provider 失败: {type(e).__name__}: {e}')
            return 1
        print(f'[页面意图] {name}.png  {smp.screen[0]}×{smp.screen[1]} …',
              flush=True)
        t0 = time.perf_counter()
        obj = page_intent(prov, smp.png, smp.screen)
        dt = time.perf_counter() - t0
        if not obj:
            print(f'[{dt:.1f}s] 模型没按格式回。原话：')
            print(prov.last_text[:600])
            return 1
        L = [f'# 页面意图与测试路径 —— `{name}`\n',
             f'> 模型：`{prov.model}`'
             f'（thinking={"disabled" if getattr(prov, "disable_thinking", False) else "默认"}）'
             f'｜ 耗时 {dt:.1f}s ｜ '
             f'截图 `{name}.png` {smp.screen[0]}×{smp.screen[1]}\n',
             '## 一、页面意图\n', f'{obj.get("page_intent", "（未给出）")}\n',
             '## 二、核心操作入口\n',
             '| # | 入口 | 位置 | 为什么是核心入口 |',
             '|---|---|---|---|']
        print(f'[{dt:.1f}s] 页面意图：{obj.get("page_intent", "（未给出）")}\n')
        ents = obj.get('core_entries') or []
        for i, e in enumerate(ents, 1):
            if not isinstance(e, dict):
                continue
            bbox = e.get('bbox')
            pos = ('`%s`' % bbox) if bbox else '—'
            L.append(f'| {i} | {e.get("label", "")} | {pos} | '
                     f'{e.get("why", "")} |')
            print(f'  入口 {i}: {e.get("label", "")}  {bbox}')
        L.append('\n## 三、潜在测试路径\n')
        print()
        paths = obj.get('test_paths') or []
        for i, p in enumerate(paths, 1):
            if not isinstance(p, dict):
                continue
            L.append(f'### {i}. {p.get("name", "")}\n')
            for j, st in enumerate(p.get('steps') or [], 1):
                L.append(f'{j}. {st}')
            L.append(f'\n**可能暴露的问题**：{p.get("risk", "（未给出）")}\n')
            print(f'  路径 {i}: {p.get("name", "")}'
                  f'（{len(p.get("steps") or [])} 步）')
        L.append('---\n')
        L.append('> ⚠️ 本文件是**模型生成、未经人工复核**的候选，'
                 '用途是给探索测试提供入口和路径假设，不能直接当结论。'
                 '核验方式：把 `core_entries` 的 bbox 交给视觉定位通道'
                 '（`--provider openai`）看能否命中，'
                 '命中率即「入口识别可用性」。\n')
        os.makedirs(args.out, exist_ok=True)
        dst = os.path.join(args.out, f'page_intent_{name}.md')
        with open(dst, 'w', encoding='utf-8') as f:
            f.write('\n'.join(L) + '\n')
        print(f'\n[写入] {dst}')
        return 0

    if args.list:
        print(f'{"样本":16s} {"尺寸":10s} {"可交互":>6s} {"人话文案":>8s} '
              f'{"仅id":>5s} {"盲区":>5s} {"越界":>5s} {"最大越界":>8s}')
        for s in samples:
            print(f'{s.name:16s} {s.screen[0]}×{s.screen[1]:<6d} '
                  f'{s.n_interactive:6d} {s.n_readable:8d} '
                  f'{s.n_id_only:5d} {s.n_bare:5d} {s.n_oob:5d} '
                  f'{s.max_oob_px:7d}px')
        print(f'\n评测项（前 30）:')
        for it in items[:30]:
            flag = ' [歧义]' if it.ambiguous else ''
            print(f'  {it.sample:14s} {it.instruction[:24]:24s} '
                  f'{it.gt.to_dict()}{flag}')
        return 0

    provider = 'mock' if args.dry else args.provider
    try:
        rep = evaluate(items, samples, provider, limit=args.limit,
                       verbose=args.v, delay=args.delay,
                       no_thinking=args.no_thinking)
    except VisionError as e:
        print(f'Provider 装配/调用失败: {type(e).__name__}: {e}')
        print('提示：先跑 --probe 体检环境变量。')
        return 1

    summ = summarize(rep)
    md, js = write_report(rep, summ, args.out, src, skipped)
    o = summ['overall']
    print(f'\n合计 n={o.get("n", 0)}  IoU@0.5={o.get("iou@0.5", 0):.1%}  '
          f'IoU@0.3={o.get("iou@0.3", 0):.1%}  '
          f'中心命中={o.get("center_hit", 0):.1%}  '
          f'L1免调模型={o.get("l1_free_ratio", 0):.1%}  '
          f'p50={o.get("lat_p50_ms", 0)}ms')
    bl = tree_baselines(rep.items, rep.samples)
    print(f'对照组（控件树单通道）：现状 {bl.get("l1_now", 0):.1%} ｜ '
          f'补齐子树文案 {bl.get("l1_deep", 0):.1%} ｜ '
          f'全树上限 {bl.get("tree_all", 0):.1%}')
    print(f'[报告] {md}')
    print(f'[数据] {js}')
    print('\n⚠️ 口径：标注源自控件树文案，本表衡量的是「视觉与控件树的一致性」，'
          '不是视觉的绝对准确率。盲区清单见报告第五节。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
