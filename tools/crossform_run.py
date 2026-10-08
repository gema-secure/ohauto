"""跨形态跑测驱动
====================

对一台设备在**多个形态**下跑同一套用例，采集控件树 + 屏幕信息，
然后调用 `crossform` 比对并出报告。

这个工具的职责边界
------------------
- **采集**：切形态 → 等稳定 → dumpLayout → 采 hidumper → 落盘
- **比对**：交给 `ohauto.crossform`（纯计算，可离线测）
- **渲染**：交给 `ohauto.crossform_report`

采集与计算**分开**，是为了让比对逻辑能完全离线单测 ——
这是 信号采集 踩过的坑：模拟与真机不一致时，跑出来的「全绿」比失败更危险。

两种运行模式
------------
1. **在线模式**（默认）：`--device "Mate X7"` 通过 hdc 采集 + `Emulator CLI` 切折叠态。
   ⚠️ 这里的 device **是模拟器实例名**（`tools/emulator_cli.py` 管理的实例），
   不是物理真机 —— 切形态走的是 `Emulator.exe -foldedState`。
   物理真机（DAYU200）没有折叠态可切，用它跑本模式的 `--baseline/--target` 没有意义。
2. **离线模式**（`--offline`）：用 `ohauto.sim.FakeHdc` 模拟，**不需要设备**

离线模式的用途：CI 里跑回归、以及开发比对逻辑时快速迭代。

落盘布局
--------
    <out>/<stem>/
      ├── baseline_<形态名>/
      │     ├── layout.json     控件树原文
      │     ├── screens.txt     hidumper RenderService 原文
      │     └── meta.json       屏幕尺寸/元素数/采集时间
      ├── target_<形态名>/
      │     └── ...
      ├── crossform.json / .md / .html    差异报告
      └── run.json              本次跑测的完整记录（含两个形态的元数据）

用例
----
    # 离线跑（无需设备）
    python tools/crossform_run.py --offline

    # 真机/模拟器跑 Mate X7 的展开态 vs 折叠态
    python tools/crossform_run.py --device "Mate X7" \\
        --baseline open --target close
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ohauto.crossform import (          # noqa: E402
    CompareReport, IdentityPolicy, compare_forms)
from ohauto.crossform_report import write_all      # noqa: E402
from ohauto.devices import FormProfile             # noqa: E402
from ohauto.layout import parse_layout             # noqa: E402
from preflight import require_device, default_target   # noqa: E402


def _emulator_target(real_serial: str) -> Optional[str]:
    """多设备在线时解析出**模拟器自己的 hdc 目标**。

    真机与模拟器同时在线时，折叠态切换（Emulator.exe -foldedState）只作用于
    模拟器——若采集目标被自动钉到真机，两轮采集就是一模一样的树，
    B12 会静默产出 0 差异的废报告（09-29 双设备实测场景）。
    解析规则：排除配置的真机串号后，优先形如 `host:port` 的 TCP 目标。
    """
    from ohauto.hdc import Hdc
    h = Hdc()
    try:
        ts = h.list_targets() or []
    except Exception:
        return None
    ts = [t for t in ts if t]
    if not ts:
        return None
    if len(ts) == 1:
        return ts[0]
    others = [t for t in ts if t != real_serial]
    tcp = [t for t in others if ':' in t]
    return (tcp or others or [None])[0]

# 复用上一步做好的纯函数与模拟器
from tools.emulator_cli import parse_screen_info   # noqa: E402


# ================================================================ 形态采集


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _safe(name: str) -> str:
    """形态名转成安全的目录名。"""
    return ''.join(c if (c.isalnum() or c in '-_.') else '_'
                   for c in name) or 'form'


class FormCapture:
    """一次形态采集的结果。"""

    def __init__(self, name: str, layout_text: str, screens_text: str,
                 profile: FormProfile):
        self.name = name
        self.layout_text = layout_text
        self.screens_text = screens_text
        self.profile = profile
        self.captured_at = _now()
        self.root = parse_layout(layout_text)
        self.element_count = sum(1 for _ in self.root.walk())

    def to_meta(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'captured_at': self.captured_at,
            'size': [self.profile.width, self.profile.height],
            'density': self.profile.density,
            'kind': self.profile.kind,
            'element_count': self.element_count,
            'screens_raw_lines': len(self.screens_text.splitlines()),
        }

    def dump(self, out_dir: str) -> Dict[str, str]:
        """把三份原始数据落盘，返回文件名映射。"""
        os.makedirs(out_dir, exist_ok=True)
        paths = {
            'layout': os.path.join(out_dir, 'layout.json'),
            'screens': os.path.join(out_dir, 'screens.txt'),
            'meta': os.path.join(out_dir, 'meta.json'),
        }
        with open(paths['layout'], 'w', encoding='utf-8') as f:
            f.write(self.layout_text)
        with open(paths['screens'], 'w', encoding='utf-8') as f:
            f.write(self.screens_text)
        with open(paths['meta'], 'w', encoding='utf-8') as f:
            json.dump(self.to_meta(), f, ensure_ascii=False, indent=2)
        return paths


def profile_from_screens(screens_text: str, fallback: Optional[Tuple[int, int]],
                         name: str, device: str = '',
                         kind: str = 'phone') -> FormProfile:
    """从 hidumper 原文构造 `FormProfile`。

    ★ **必须用实测值**，不能拿配置里的静态值 —— 静态配置没有安全区、
    且折叠态的分辨率可能与配置不一致。这里只填实测拿得到的东西，
    拿不到的字段留默认（语义是「没有」而不是「猜一个」）。
    `fallback` 允许为 None（Driver.screen_size() 实测不到时）——
    此时分辨率字段留默认，绝不退回猜测值。
    """
    screens = parse_screen_info(screens_text)
    active = None
    for s in screens:
        if 'ON' in str(s.get('power_status', '')).upper():
            active = s
            break
    if active is None and screens:
        active = screens[0]

    fb_w = fallback[0] if fallback else None
    fb_h = fallback[1] if fallback else None
    if active:
        raw_w = active.get('active_width') or active.get('width') or fb_w
        raw_h = active.get('active_height') or active.get('height') or fb_h
    else:
        raw_w, raw_h = fb_w, fb_h
    if not raw_w or not raw_h:
        # FormProfile.width/height 是必填 int，没有「猜一个」的余地；
        # 抛错让 capture_online 的重试循环接住，重试耗尽则如实报采集失败。
        raise ValueError(
            f'{name}: 屏幕分辨率实测不到（hidumper 与控件树均不可用），'
            '拒绝构造猜测几何的 FormProfile')
    w, h = int(raw_w), int(raw_h)

    return FormProfile(
        name=name, device=device or name, kind=kind,
        width=w, height=h,
        source='hiddenumper_measured',
    )


# ================================================================ 在线采集


def capture_online(device: str, form_state: Optional[str],
                   name: str, kind: str,
                   folded_state: Optional[str] = None,
                   min_elements: int = 3,
                   retries: int = 4) -> FormCapture:
    """在真实设备/模拟器上采一次形态。

    `folded_state` 走模拟器的 `Emulator.exe -foldedState`（如 open / close）。
    采到的控件树与屏幕信息都来自设备原生接口，与 `capture_offline` 的
    两条通道**完全对应**（dumpLayout 文件 + hidumper RenderService）。

    ★ **重试与有效性校验是必须的，不是保险**。
    实测（Mate X7 模拟器，2026-09-18）：折叠态切换后立刻 dump，
    大约每 3 次里会有 1 次拿回**只有根节点的空树** —— 桌面还没重绘完。
    不校验的话，那份空树会被当成「目标形态真有 89 个元素缺失」，
    报告里刷出 89 条高严重度假差异，而且看起来非常像真问题。
    这类「采集失败伪装成被测系统缺陷」是自动化测试最危险的失效模式。
    """
    # 延迟导入：离线模式不该依赖 hdc 相关模块可用
    from ohauto.driver import Driver
    from ohauto.hdc import Hdc
    from tools import emulator_cli

    if folded_state:
        r = emulator_cli.set_folded_state(device, folded_state)
        print(f'  [fold] {device} -> {folded_state} :: '
              f'{str(r.get("stdout", "")).strip()[:80]}')

    # ★ 多设备在线时必须**显式选模拟器**：真机也在 targets 里，而折叠态
    #   切换只作用于模拟器——采错目标就是两轮一模一样的废报告。
    tgt = _emulator_target(default_target())
    hdc = require_device(target=tgt)
    last_err = ''
    for attempt in range(1, retries + 1):
        # 折叠态切换后系统要重新完成布局，等不够会拿到旧树或空树。
        # 首次等 `_FOLD_SETTLE_SEC`，之后逐次加长（系统重绘时间不定）。
        time.sleep(_FOLD_SETTLE_SEC if attempt == 1
                   else _FOLD_SETTLE_SEC * attempt)
        try:
            drv = Driver(bundle='', hdc=hdc, verbose=False)
            drv.refresh()
            layout_text = json.dumps(_tree_to_dict(drv.root),
                                     ensure_ascii=False)
            screens_text = hdc.shell(
                'hidumper -s RenderService -a screen').stdout
            fallback = drv.screen_size()
        except Exception as e:                       # noqa: BLE001
            last_err = f'{type(e).__name__}: {e}'
            print(f'  [retry {attempt}/{retries}] {name} 采集异常：{last_err}')
            continue
        finally:
            try:
                drv.close()
            except Exception:
                pass

        root = parse_layout(layout_text)
        n = sum(1 for _ in root.walk())
        if n >= min_elements:
            prof = profile_from_screens(screens_text, fallback, name,
                                        device=device, kind=kind)
            print(f'  [capture] {name}: {prof.width}x{prof.height}, '
                  f'{n} 个元素（第 {attempt} 次尝试）')
            return FormCapture(name, layout_text, screens_text, prof)

        # 元素太少 → 判定为采集失败（多半是切换后还没重绘完），重试
        last_err = f'控件树只有 {n} 个元素（阈值 {min_elements}）'
        print(f'  [retry {attempt}/{retries}] {name} {last_err}，'
              f'判定为采集未就绪')

    raise RuntimeError(
        f'{name} 采集失败：连续 {retries} 次都拿不到有效控件树。'
        f'最后一次原因：{last_err}。'
        f'这**不是**被测应用的缺陷，是采集侧没就绪 —— '
        f'请检查设备是否锁屏/息屏，或加大重试次数。')


#: 折叠态切换后的稳定等待（秒）。实测值，别随手改小。
_FOLD_SETTLE_SEC = 3.0


def _tree_to_dict(node) -> Dict[str, Any]:
    """把 `LayoutNode` 还原成 `dumpLayout` 风格的 dict（供落盘与复用解析器）。

    为什么要还原而不是直接存对象：落盘的夹具要能被 `parse_layout()`
    重新读回来，也必须与真机 dumpLayout 的 JSON 格式同构 ——
    否则「采集时解析一次、报告时再解析一次」两条路径会不一致。
    """
    return {
        'attributes': {
            'type': node.type, 'id': node.id, 'text': node.text,
            'descr': node.descr, 'hint': node.hint,
            'bounds': f'[{node.rect.left},{node.rect.top}]'
                      f'[{node.rect.right},{node.rect.bottom}]',
            'clickable': str(node.clickable).lower(),
            'visible': str(node.visible).lower(),
            'enabled': str(node.enabled).lower(),
            'scrollable': str(node.scrollable).lower(),
        },
        'children': [_tree_to_dict(c) for c in node.children],
    }


# ================================================================ 离线采集


def capture_offline(folded_state: str, name: str,
                    responsive: bool = True,
                    page: str = 'login') -> FormCapture:
    """用 `FakeHdc` 在无设备环境下模拟一次形态采集。

    模拟 Mate X7 的双屏折叠：内屏 `open`、外屏 `close`。
    屏幕数据格式与真机一致（`_screen_dump()` 就是照真机夹具写的）。

    Parameters
    ----------
    page:
        采哪个页面。`login`/`home`/`order` 是**响应式**页面（跨形态通常
        0 差异）；`static_fixed` 是**写死坐标的靶子应用**，必然产出
        越界/溢出/不可达差异 —— 用它验证「差异报告确实能检出问题」。
    """
    from ohauto.sim import FakeHdc

    # Mate X7 实测双屏参数（见 docs/API手册.md §六）
    screens = [
        {'index': 0, 'power_status': 'POWER_STATUS_OFF', 'backlight': 1,
         'width': 2416, 'height': 2210},       # 内屏
        {'index': 1, 'power_status': 'POWER_STATUS_OFF', 'backlight': 4,
         'width': 1080, 'height': 2444},       # 外屏
    ]
    hdc = FakeHdc(start_page=page, screen=(1080, 2340),
                  screens=screens, responsive=responsive)
    hdc.set_folded_state(folded_state)

    # 走与真机完全相同的两条通道
    dev_path = hdc.dump_layout()
    layout_text = hdc.run(['shell', 'cat', dev_path]).stdout
    screens_text = hdc.shell('hidumper -s RenderService -a screen').stdout

    prof = profile_from_screens(screens_text, hdc.screen_size, name,
                                device='Mate X7', kind='foldable')
    print(f'  [offline] {name}: {prof.width}x{prof.height} '
          f'（页面 {page}）')
    return FormCapture(name, layout_text, screens_text, prof)


# ================================================================ 主流程


def build_report(cap_a: FormCapture, cap_b: FormCapture,
                 policy: Optional[IdentityPolicy] = None) -> CompareReport:
    return compare_forms(
        (cap_a.root, cap_a.profile, cap_a.name),
        (cap_b.root, cap_b.profile, cap_b.name),
        policy=policy,
    )


def run(out_dir: str, stem: str,
        baseline: FormCapture, target: FormCapture,
        policy: Optional[IdentityPolicy] = None) -> Dict[str, Any]:
    """采集已完成 → 落盘 → 比对 → 出报告。返回本次运行的记录。"""
    root = os.path.join(out_dir, stem)
    os.makedirs(root, exist_ok=True)

    a_dir = os.path.join(root, f'baseline_{_safe(baseline.name)}')
    b_dir = os.path.join(root, f'target_{_safe(target.name)}')
    a_paths = baseline.dump(a_dir)
    b_paths = target.dump(b_dir)

    rep = build_report(baseline, target, policy=policy)

    stamp = _now()
    reports = write_all(rep, root, stem='crossform', generated_at=stamp)

    record: Dict[str, Any] = {
        'ran_at': stamp,
        'mode': 'offline' if getattr(baseline, '_offline', False)
                else 'online',
        'baseline': {**baseline.to_meta(), 'files': a_paths},
        'target': {**target.to_meta(), 'files': b_paths},
        'report_files': reports,
        'summary': rep.summary(),
        'counts': rep.counts(),
        'has_high': rep.has_high(),
        'warnings': rep.warnings,
    }
    with open(os.path.join(root, 'run.json'), 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return record


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description='跨形态跑测 + 差异报告',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('--device', '-d', default=None,
                    help='设备/模拟器实例名（如 "Mate X7"）。'
                         '缺省或配合 --offline 时走模拟模式')
    ap.add_argument('--baseline', default='open',
                    help='基准形态的折叠态（默认 open）')
    ap.add_argument('--target', default='close',
                    help='对比形态的折叠态（默认 close）')
    ap.add_argument('--out', default=os.path.join(_ROOT, '_out', 'crossform'),
                    help='输出根目录')
    ap.add_argument('--stem', default=None,
                    help='本次运行的目录名（默认用设备名+时间）')
    ap.add_argument('--offline', action='store_true',
                    help='离线模式：用 sim.FakeHdc，不需要设备')
    ap.add_argument('--no-responsive', action='store_true',
                    help='离线模式下关闭控件树响应式重排（用于对照实验）')
    ap.add_argument('--offline-page', default='login',
                    choices=['login', 'home', 'order', 'static_fixed'],
                    help='离线模式下采哪个页面。static_fixed 是**写死坐标的'
                         '靶子应用**，必然检出适配缺陷；其余三者是响应式页面')
    ap.add_argument('--only-id', action='store_true',
                    help='身份策略：只用 id 匹配（最严格，误报最少）')
    args = ap.parse_args(argv)

    policy = None
    if args.only_id:
        policy = IdentityPolicy(use_id=True, use_text=False,
                                use_hierarchy=False)

    dev = args.device or ('<offline>' if args.offline else 'Mate X7')
    stem = args.stem or (_safe(dev) + '_'
                         + datetime.now().strftime('%Y%m%d_%H%M%S'))

    print(f'=== 跨形态跑测：{dev} ===')
    print(f'  基准形态 : {args.baseline}')
    print(f'  对比形态 : {args.target}')
    print(f'  模式     : {"离线（模拟）" if args.offline else "在线（设备）"}')
    print()

    if args.offline:
        cap_a = capture_offline(args.baseline, f'{dev}_{args.baseline}',
                                responsive=not args.no_responsive,
                                page=args.offline_page)
        cap_b = capture_offline(args.target, f'{dev}_{args.target}',
                                responsive=not args.no_responsive,
                                page=args.offline_page)
        cap_a._offline = cap_b._offline = True
    else:
        cap_a = capture_online(dev, None, f'{dev}_{args.baseline}',
                              kind='foldable', folded_state=args.baseline)
        cap_b = capture_online(dev, None, f'{dev}_{args.target}',
                              kind='foldable', folded_state=args.target)

    rec = run(args.out, stem, cap_a, cap_b, policy=policy)

    print()
    print('=== 结果 ===')
    print(f'  {rec["summary"]}')
    if rec['warnings']:
        print('  告警：')
        for w in rec['warnings']:
            print(f'    - {w}')
    print()
    print('  报告：')
    for fmt, p in rec['report_files'].items():
        print(f'    {fmt:9s} {p}')
    print(f'    {"record":9s} {os.path.join(args.out, stem, "run.json")}')

    # 有高严重度差异时返回非 0，便于接进 CI 当门禁。
    #
    # ⚠️ 判据必须直接用 `has_high`（引擎定义的高严重度），
    # **不要**在这里另写一份「哪些类型算高危」的清单 ——
    # 两处定义一旦漂移，CI 门禁就会与报告结论矛盾。
    # 踩过：早期这里写的是 `MISSING or UNREACHABLE`，
    # 而「完全不可见的元素」被引擎判为 HIGH 的 OUT_OF_SCREEN，
    # 于是报告标红、退出码却是 0 —— 门禁直接失效。修好后以引擎为准。
    #
    # 退出码用 **1**（未达标），不是 2 —— 2 是「设备不在场」专用
    # （见 docs/API手册.md §三），CI 拿到 2 会按「跳过」处理而不是门禁红。
    return 1 if rec['has_high'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
