"""把设备形态档案导出成 跨形态 可用的清单（JSON + Markdown）。

用法：
    python tools/export_form_profiles.py
    python tools/export_form_profiles.py --out ohauto/devices

产出：
    form_profiles.json     机器读（跨形态 的 switch_form 直接吃这个）
    form_profiles.md       人读（周会/评审用）
"""
import argparse
import io
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.devices import (                                    # noqa: E402
    find_product_config, load_profiles,
)

#: 推荐形态对的**规格**（而不是硬编码设备名 —— 硬编码的设备名一旦
#: 华为改配置就会静默失效，而规格可以自动去档案库里挑）。
#: 每项：(用途, 选择函数A, 选择函数B, 说明)
def _pick(profiles, *, kinds=None, width=None, height=None,
          width_gt=None, width_lt=None, exclude_device=None):
    """按规格挑一条档案，挑不到返回 None（不造数据）。"""
    cands = profiles
    if kinds:
        cands = [p for p in cands if p.kind in kinds]
    if width is not None:
        cands = [p for p in cands if p.width == width]
    if height is not None:
        cands = [p for p in cands if p.height == height]
    if width_gt is not None:
        cands = [p for p in cands if p.width > width_gt]
    if width_lt is not None:
        cands = [p for p in cands if p.width < width_lt]
    if exclude_device:
        cands = [p for p in cands if p.device not in exclude_device]
    return cands[0] if cands else None


def build_recommended_pairs(profiles):
    """按规格从档案库里挑出推荐形态对。挑不到的跳过并说明。"""
    pairs = []

    # ---- 1. 同宽不同高：真机 720x1280 vs 配置里 720 宽的机型
    narrow = _pick(profiles, kinds=['phone'], width=720)
    if narrow:
        pairs.append((
            '同宽不同高', '真机 720×1280',
            f'{narrow.name} ({narrow.width}×{narrow.height})',
            f'宽度同为 720 → 横向布局不该变；'
            f'高度差 {narrow.height - 1280}px → 专测纵向滚动与底部元素'))

    # ---- 2. 折叠屏展开/折叠（挑尺寸差最大的那款横折）
    unfolds = [p for p in profiles if p.kind == 'foldable']
    best = None
    for uf in unfolds:
        fd = next((p for p in profiles
                   if p.kind == 'foldable_folded' and p.device == uf.device), None)
        if fd and (best is None or uf.width - fd.width > best[0].width - best[1].width):
            best = (uf, fd)
    if best:
        uf, fd = best
        pairs.append((
            '折叠展开/折叠', f'{uf.name} ({uf.width}×{uf.height})',
            f'{fd.name} ({fd.width}×{fd.height})',
            f'宽度 {uf.width}→{fd.width}（{fd.width / uf.width:.0%}）→ '
            f'专测栅格列数变化、导航栏形态切换'))

    # ---- 3. 三折叠三态（挑宽度落差最大的两个相邻态）
    xt = [p for p in profiles if p.kind.startswith('triple_fold')]
    if len(xt) >= 2:
        xt.sort(key=lambda p: -p.width)
        a, b = xt[0], xt[-1]
        pairs.append((
            '三折叠极值', f'{a.name} ({a.width}×{a.height})',
            f'{b.name} ({b.width}×{b.height})',
            f'宽度 {a.width}→{b.width}（{b.width / a.width:.0%}）→ '
            f'专测超宽屏布局退化为窄屏时元素丢失'))

    # ---- 4. 跨品类：最窄手机 vs 最宽的平板/PC
    phones = [p for p in profiles if p.kind == 'phone']
    bigs = [p for p in profiles if p.kind in ('tablet', 'pc')]
    if phones and bigs:
        ph = min(phones, key=lambda p: p.width)
        bg = max(bigs, key=lambda p: p.width)
        pairs.append((
            '手机↔大屏', f'{ph.name} ({ph.width}×{ph.height})',
            f'{bg.name} ({bg.width}×{bg.height})',
            f'宽度 {ph.width}→{bg.width}（{bg.width / ph.width:.1f}×）→ '
            f'专测最大宽度下的布局溢出与超长留白'))

    return pairs


def _by_kind(profiles):
    g = defaultdict(list)
    for p in profiles:
        g[p.kind].append(p)
    return g


def write_json(profiles, path):
    _src = find_product_config()
    payload = {
        # 只记文件名，不记绝对路径 —— 本机目录结构属于隐私项，不入库。
        'generated_from': (os.path.basename(_src) if _src
                           else 'productConfig.json (fixture)'),
        'total': len(profiles),
        'counts': {k: len(v) for k, v in sorted(_by_kind(profiles).items())},
        'profiles': [p.to_dict() for p in profiles],
    }
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


def write_md(profiles, path, payload):
    L = []
    L.append('# 设备形态档案清单（跨形态测试用）\n')
    L.append('> **自动生成，勿手工编辑。** 重新生成：'
             '`python tools/export_form_profiles.py`\n')
    L.append(f'数据源：`{payload["generated_from"]}`\n')
    L.append(f'共 **{payload["total"]}** 条形态档案。\n')

    # ---- 数量总览
    L.append('\n## 一、形态总览\n')
    L.append('| 类别 | 形态数 | 说明 |')
    L.append('|---|---:|---|')
    desc = {
        'phone': '直板手机', 'tablet': '平板', 'pc': '2in1 笔记本',
        'pc_foldable': '折叠笔记本（展开）', 'pc_foldable_folded': '折叠笔记本（折叠）',
        'foldable': '横折折叠屏（展开）', 'foldable_folded': '横折折叠屏（折叠）',
        'wide_fold': '竖折/阔折（展开）', 'wide_fold_folded': '竖折/阔折（折叠）',
        'triple_fold': '三折叠（全展开）', 'triple_fold_double': '三折叠（双屏中间态）',
        'triple_fold_folded': '三折叠（折叠态）',
        'tv': '智慧屏', 'wearable': '穿戴', 'car': '车机',
    }
    for kind, n in sorted(payload['counts'].items(), key=lambda x: -x[1]):
        L.append(f'| `{kind}` | {n} | {desc.get(kind, "—")} |')

    # ---- 折叠屏详解
    L.append('\n## 二、折叠屏多形态对照（跨形态 的核心资产）\n')
    L.append('折叠屏在配置里是**一台设备多组分辨率**，加载时展开成多条档案。'
             '下表是同一台设备各形态的对照 —— 跨形态差异就该在这些行之间比。\n')
    folds = [p for p in profiles
             if p.device and p.fold_status is not None]
    by_dev = defaultdict(list)
    for p in folds:
        by_dev[p.device].append(p)
    L.append('| 设备 | 形态 | 分辨率 | DPI | 对角线 | 圆角 | 挖孔 (x,y,w,h) |')
    L.append('|---|---|---|---:|---:|---|---|')
    for dev in sorted(by_dev):
        for p in sorted(by_dev[dev], key=lambda x: -x.width):
            cut = '—' if not p.cutouts else ' / '.join(
                f'({x},{y},{w},{h})' for x, y, w, h in p.cutouts)
            rad = ','.join(f'{r:g}' for r in p.corner_radius) or '—'
            L.append(f'| {dev} | {p.fold_status} | {p.width}×{p.height} | '
                     f'{p.density} | {p.diagonal:g}" | {rad} | {cut} |')

    # ---- 推荐形态对
    L.append('\n## 三、推荐形态对（比单点形态更有测试价值）\n')
    L.append('按**规格**（同宽 / 宽高比反转 / 宽度腰斩 / 跨品类）自动从档案库挑选，'
             '不是硬编码设备名 —— 华为改了配置这张表会跟着变，不会静默失效。\n')
    pairs = build_recommended_pairs(profiles)
    if pairs:
        L.append('| 用途 | 形态 A | 形态 B | 测什么 |')
        L.append('|---|---|---|---|')
        for name, a, b, why in pairs:
            L.append(f'| {name} | {a} | {b} | {why} |')
    else:
        L.append('（档案库为空，无法挑选）\n')

    # ---- 数据可信度
    L.append('\n## 四、需要实测复核的档案\n')
    L.append('下列档案的静态数据有已知的不确定性，`caveats` 字段（JSON 里）'
             '已显式标注。**在这些形态上做出的结论必须先实测验证。**\n')
    flagged = [p for p in profiles if p.caveats]
    if not flagged:
        L.append('（无）\n')
    else:
        L.append('| 档案 | 分辨率 | 不确定性 |')
        L.append('|---|---|---|')
        for p in flagged:
            for c in p.caveats:
                L.append(f'| {p.name} | {p.width}×{p.height} | {c} |')

    # ---- 安全区说明
    L.append('\n## 五、⚠️ 安全区不在静态配置里\n')
    L.append('`productConfig.json` **没有任何安全区（状态栏/导航栏高度）字段**，'
             '所以 JSON 里每条的 `status_bar_h` / `nav_bar_h` 都是 0、'
             '`has_measured_safe_area` 都是 `false`，此时 `safe_area` 等于整屏。\n')
    L.append('真实值只能从真机 hidumper 实测：\n')
    L.append('```bash\nhidumper -s WindowManagerService -a \'-a\'\n```\n')
    L.append('实测样本见 `ohauto/tests/fixtures/crossform/'
             'displaymanager_20260917_172127.txt`：\n')
    L.append('```\nSystemUi_StatusBar    [ 0    0    720  72  ]\n'
             'SystemUi_NavigationB [ 0    1208 720  72  ]\n```\n')

    with io.open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', default=os.path.join(ROOT, 'ohauto', 'devices'),
                    help='输出目录')
    ap.add_argument('--config', default=None, help='指定 productConfig.json')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    profiles = load_profiles(args.config)
    if not profiles:
        print('ERROR: 读不到任何形态档案。请确认 DevEco 模拟器已安装、'
              'productConfig.json 存在。')
        return 1

    jp = os.path.join(args.out, 'form_profiles.json')
    mp = os.path.join(args.out, 'form_profiles.md')
    payload = write_json(profiles, jp)
    write_md(profiles, mp, payload)

    print(f'OK  {len(profiles)} 条形态档案')
    for k, n in sorted(payload['counts'].items(), key=lambda x: -x[1]):
        print(f'    {k:24s} {n}')
    print(f'\n    {jp}')
    print(f'    {mp}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
