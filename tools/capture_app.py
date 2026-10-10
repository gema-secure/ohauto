"""真机页面采集器 —— 一条命令采一个应用页面。

★ 为什么要有这个脚本
--------------------

真机采集看着就是「dumpLayout + screenCap + 拉回来」三步，但
实测踩了四个坑，每个都**静默出错**（采集成功、不报错、数据是错的）：

1. **息屏锁屏** → 锁屏不进无障碍树，控件树只剩 377 字节，
   看起来像「应用没有元素」
2. **USB 连接对话框** → 插 USB 就弹，盖住被测应用；
   采集拿到的是对话框（103 节点），而且**关掉还会再弹**
3. **`hdc file recv` 不接受盘符路径**（Git Bash 下）→
   报 `no such file or directory`。本脚本走 Python subprocess，避开 MSYS 改写
4. **示例应用的 ability 名不是 `EntryAbility`** → 写错报 `resolve ability err`

本脚本把这四步的处置全部内化，并在采集后**做有效性校验**，
不合格时明确告知原因，而不是把坏数据当成好数据交出去。

用法
----

    # 采一个应用（自动启动）
    python tools/capture_app.py --bundle ohos.samples.etsclock \\
        --ability MainAbility --name etsclock

    # 采当前页面（不启动应用，适合手动导航后的页面）
    python tools/capture_app.py --name settings_bluetooth --no-launch

    # 自定义输出目录与等待时间
    python tools/capture_app.py --bundle X --ability Y --name Z \\
        --out D:/foo/bar --settle 8
"""
import argparse
import datetime
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc, HdcError                          # noqa: E402
from ohauto.layout import LayoutNode, parse_layout            # noqa: E402


#: 系统弹窗的特征文案 —— 采集前检测到就先关掉。
#: 这些弹窗会**盖住被测应用**，让 dumpLayout 拿到对话框而不是应用页面。
SYSTEM_DIALOG_MARKERS: Tuple[str, ...] = (
    'USB 连接方式', 'USB连接方式', '仅充电', 'USB 调试',
)

#: 弹窗里用于关闭的按钮文案（按优先级）
DISMISS_LABELS: Tuple[str, ...] = ('确定', '知道了', '允许', '同意', '好的')

#: 判定「采集未就绪」的最小节点数。真实应用界面不可能只有 1~2 个节点，
#: 只有根节点 = 空树（通常是锁屏或还没重绘完）。
MIN_EXPECTED_NODES = 3


# ---------------------------------------------------------------- 屏幕状态


def wake_and_pin(hdc: Hdc, timeout_ms: int = 1_800_000) -> None:
    """唤醒屏幕、钉住息屏超时、上滑解锁。

    ⚠️ 少做任何一步都会让采集**静默拿到空树**：
    息屏后回到锁屏，而锁屏界面不进无障碍树。
    """
    hdc.shell('power-shell wakeup')
    time.sleep(1.0)
    hdc.shell(f'power-shell timeout -o {timeout_ms}')
    time.sleep(0.5)
    # 上滑解锁（720×1280 实测有效；其它分辨率按比例换算）
    w, h = _screen_size(hdc)
    cx = w // 2
    hdc.swipe(cx, int(h * 0.86), cx, int(h * 0.23), 800)
    time.sleep(1.5)


def _screen_size(hdc: Hdc) -> Tuple[int, int]:
    """从 RenderService 取真实分辨率；失败直接报错 —— 拒绝按猜测的几何解锁。

    旧版失败时回落 720×1280（DAYU200 专属值），换设备后会把解锁滑动
    打到错误的坐标上，静默采到空树 —— 那比崩溃更难查，所以宁可失败。
    """
    try:
        r = hdc.shell('hidumper -s RenderService -a screen')
        for line in r.stdout.replace('\r', '').splitlines():
            if 'render size' in line or 'activeMode' in line:
                import re
                m = re.search(r'(\d{3,4})\s*x\s*(\d{3,4})', line)
                if m:
                    return int(m.group(1)), int(m.group(2))
    except Exception:                                          # noqa: BLE001
        pass
    raise RuntimeError(
        '无法实测屏幕尺寸（hidumper RenderService 无输出），'
        '拒绝按写死的 720×1280 计算解锁滑动 —— 请手动解锁设备后重试')


# ---------------------------------------------------------------- 启动应用


def launch(hdc: Hdc, ability: str, bundle: str) -> bool:
    """启动应用。返回是否启动成功。"""
    r = hdc.shell(f'aa start -a {ability} -b {bundle}')
    out = (r.stdout + r.stderr).lower()
    return 'successfully' in out


# ---------------------------------------------------------------- 弹窗处置


def _texts_of(root: LayoutNode) -> List[str]:
    return [(n.text or '').strip() for n in root.walk()]


def looks_like_system_dialog(root: LayoutNode) -> Optional[str]:
    """控件树像不像系统弹窗？是则返回命中的关键词。"""
    for t in _texts_of(root):
        for marker in SYSTEM_DIALOG_MARKERS:
            if marker in t:
                return marker
    return None


def find_dismiss_button(root: LayoutNode) -> Optional[Tuple[int, int]]:
    """找弹窗的关闭按钮中心坐标。"""
    for n in root.walk():
        if (n.text or '').strip() in DISMISS_LABELS:
            return n.center
    return None


# ---------------------------------------------------------------- 采集


def _dump(hdc: Hdc, tag: str) -> Tuple[str, str]:
    """在设备上生成控件树与截图，返回两个设备路径。"""
    dev_json = f'{hdc.tmp_dir}/capture_{tag}.json'
    dev_png = f'{hdc.tmp_dir}/capture_{tag}.png'
    hdc.dump_layout(dev_json)
    hdc.screen_cap(dev_png)
    return dev_json, dev_png


def capture(hdc: Hdc, name: str, out_dir: str,
            bundle: Optional[str] = None, ability: Optional[str] = None,
            settle: float = 5.0, max_dismiss: int = 3,
            dry_run: bool = False) -> Dict[str, Any]:
    """采集一个页面。返回 meta 字典（同时落盘 `<name>.meta.json`）。"""
    meta: Dict[str, Any] = {
        'name': name,
        'bundle': bundle,
        'ability': ability,
        'captured_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'warnings': [],
    }

    # ① 屏幕就绪（唤醒 + 钉住 + 解锁）
    wake_and_pin(hdc)

    # ② 启动目标应用
    if bundle and ability:
        meta['launched'] = launch(hdc, ability, bundle)
        if not meta['launched']:
            meta['warnings'].append(
                f'启动失败：{bundle}/{ability} —— 检查 ability 名'
                f'（示例应用常用 MainAbility，不是 EntryAbility）')
        time.sleep(settle)

    dev_json, dev_png = _dump(hdc, name)
    hdc.pull(dev_json, os.path.join(out_dir, f'{name}.json'))
    hdc.pull(dev_png, os.path.join(out_dir, f'{name}.png'), binary=True)

    # ③ 有效性校验 —— 不合格就重采，而不是把坏数据交出去
    for attempt in range(1, max_dismiss + 1):
        with open(os.path.join(out_dir, f'{name}.json'),
                  encoding='utf-8') as f:
            root = parse_layout(f.read())
        nodes = list(root.walk())
        meta['node_count'] = len(nodes)

        # 空树 → 通常是锁屏或未就绪
        if len(nodes) < MIN_EXPECTED_NODES:
            meta['warnings'].append(
                f'第 {attempt} 次采集只有 {len(nodes)} 个节点 —— '
                f'可能是锁屏或界面未就绪')
            wake_and_pin(hdc)
            time.sleep(2.0)
            dev_json, dev_png = _dump(hdc, name)
            hdc.pull(dev_json, os.path.join(out_dir, f'{name}.json'))
            hdc.pull(dev_png, os.path.join(out_dir, f'{name}.png'), binary=True)
            continue

        # 系统弹窗遮挡 → 点掉后立即重采
        marker = looks_like_system_dialog(root)
        if marker:
            btn = find_dismiss_button(root)
            meta['warnings'].append(
                f'第 {attempt} 次采集被系统弹窗遮挡（命中「{marker}」）')
            if btn is None:
                meta['warnings'].append('未找到关闭按钮，无法自动处置')
                break
            hdc.click(*btn)
            time.sleep(1.5)      # ★ 关掉后立即采，等久了它又弹
            dev_json, dev_png = _dump(hdc, name)
            hdc.pull(dev_json, os.path.join(out_dir, f'{name}.json'))
            hdc.pull(dev_png, os.path.join(out_dir, f'{name}.png'), binary=True)
            continue

        break                    # 通过校验

    # ④ 汇总统计（供 report 与人眼快速判断）
    with open(os.path.join(out_dir, f'{name}.json'), encoding='utf-8') as f:
        root = parse_layout(f.read())
    nodes = list(root.walk())
    meta.update({
        'node_count': len(nodes),
        'interactive': sum(1 for n in nodes if n.is_interactive()),
        'with_id': sum(1 for n in nodes if n.id),
        'with_text': sum(1 for n in nodes if (n.text or '').strip()),
        'types': _type_hist(nodes),
        'clean': not meta['warnings'],
    })

    if dry_run:
        return meta

    with open(os.path.join(out_dir, f'{name}.meta.json'), 'w',
              encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return meta


def _type_hist(nodes: List[LayoutNode]) -> Dict[str, int]:
    hist: Dict[str, int] = {}
    for n in nodes:
        hist[n.type or '(空)'] = hist.get(n.type or '(空)', 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------- CLI


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='真机页面采集器（内化息屏/弹窗/路径/ability 四个坑）')
    ap.add_argument('--bundle', help='应用包名（与 --ability 一起用时自动启动）')
    ap.add_argument('--ability', help='ability 名（示例应用常用 MainAbility）')
    ap.add_argument('--name', required=True, help='输出文件名前缀')
    ap.add_argument('--out', default=None, help='输出目录（默认 _out/capture）')
    ap.add_argument('--settle', type=float, default=5.0,
                    help='启动应用后等待秒数（默认 5）')
    ap.add_argument('--no-launch', action='store_true',
                    help='不启动应用，只采当前页面')
    ap.add_argument('--target', default=None, help='设备序列号')
    args = ap.parse_args(argv)

    out_dir = args.out or os.path.join(ROOT, '_out', 'capture')
    os.makedirs(out_dir, exist_ok=True)

    print('=' * 64)
    print(f'  真机采集 · {args.name}')
    print('=' * 64)

    try:
        hdc = Hdc(target=args.target)
        targets = hdc.list_targets()
    except Exception as e:                                     # noqa: BLE001
        print(f'[失败] 无法连接设备: {e}')
        return 2                       # 设备不在场（约定见 docs/API手册.md §三）
    print(f'  设备      : {targets[0] if targets else "?"}')

    bundle = None if args.no_launch else args.bundle
    ability = None if args.no_launch else args.ability
    if bundle and not ability:
        print('[失败] 指定了 --bundle 就必须同时给 --ability')
        return 1                       # 用法错误，不是设备问题
    if bundle:
        print(f'  目标应用  : {bundle} / {ability}')

    try:
        meta = capture(hdc, args.name, out_dir, bundle, ability,
                       settle=args.settle)
    except HdcError as e:
        print(f'[失败] 采集出错: {e}')
        return 1

    print(f'  输出目录  : {out_dir}')
    print()
    print(f'  节点数    : {meta["node_count"]}')
    print(f'  可交互    : {meta["interactive"]}')
    print(f'  有 id     : {meta["with_id"]}')
    print(f'  有 text   : {meta["with_text"]}')
    print(f'  类型分布  : {meta["types"]}')
    if meta['warnings']:
        print()
        print('  ⚠️ 告警：')
        for w in meta['warnings']:
            print(f'    - {w}')
    print('=' * 64)
    print('结论：' + ('采集干净，可直接使用' if meta['clean']
                      else '采集有问题，见上方告警 —— 不要直接拿去用'))
    return 0 if meta['clean'] else 1   # 采集有问题=未达标，不是设备不在场


if __name__ == '__main__':
    sys.exit(main())
