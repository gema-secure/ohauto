"""探索覆盖率检查 —— 把「静态声明的页面」当分母，看探索探到了几个。

为什么需要它
------------
探索器跑完只会告诉你「发现了 N 个页面状态」，**但说不出"该有几个"** ——
没有分母，就没有覆盖率，"探够了没有"只能靠感觉。
静态源码分析恰好能给这个分母：`main_pages.json` 声明的应用内页面清单。

于是「探索够不够」从主观判断变成一个**可计算的数字**：

    覆盖率 = 探索到的页面状态数 / 静态声明的页面数

⚠️ 两处口径必须说清楚，否则数字会骗人
--------------------------------------
1. **主指标是「页面覆盖率」**（page_path 口径，分子分母同源）：
   `PageState.page_path` 已落地（explorer 双签名），探索到的状态按
   设备自报的 `pagePath` 归并到页，与静态声明页面求交集。
   **页面状态数只是辅助** —— 同一页面在不同状态下会算多个，
   「状态数/页面数」可能 >100%，那不是超额完成，别拿它当覆盖率。
2. **分母只含应用内页面** —— 桌面卡片（`form_config.json` 的 src）不算，
   它不在应用路由里，探索器本来就进不去（这个坑 09-24 踩过）。
3. pagePath 归并用**双向后缀匹配**：设备侧可能是 `pages/Index`，
   源码声明可能是 `entry/src/main/ets/pages/Index`，取能对上的那层。

用法::

    python tools/explore_coverage.py --static-project D:/project/ohauto-hypium-test \\
        --bundle com.example.myapplication --ability EntryAbility
    # tarpit 误伤对照：加 --tarpit-off 关闭防粘滞再跑一遍比页面覆盖
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.runner import DeviceGuard                   # noqa: E402
from preflight import require_device, default_target                    # noqa: E402

DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
DEFAULT_PROJECT = r'D:\project\ohauto-hypium-test'
DEFAULT_BUNDLE = 'com.example.myapplication'


def declared_pages(project: str) -> list:
    """静态源：应用内页面清单（已排除桌面卡片）。"""
    from ohauto.static_arkts.bridge import analyze_project, last_error
    info = analyze_project(project)
    if info is None:
        print('⚠️ 静态分析未执行：%s' % last_error())
        return []
    w = info.get('widget_pages') or []
    if w:
        print('  （已排除桌面卡片 %d 个：%s）' % (len(w), w))
    return list(info.get('pages') or [])


def match_page(declared: str, visited: set) -> bool:
    """声明页 ↔ 设备 pagePath 双向后缀匹配（容器路径前缀可能不同）。"""
    d = (declared or '').strip().strip('/')
    for v in visited:
        vv = (v or '').strip().strip('/')
        if not vv:
            continue
        if d == vv or d.endswith('/' + vv) or vv.endswith('/' + d):
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--static-project', default=DEFAULT_PROJECT)
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default='EntryAbility')
    ap.add_argument('--max-pages', type=int, default=8)
    ap.add_argument('--actions-per-page', type=int, default=4)
    ap.add_argument('--tarpit-off', action='store_true',
                    help='关闭防粘滞（对照 B7 的「tarpit 是否误伤」）')
    args = ap.parse_args()

    print('=' * 70)
    print('  探索覆盖率检查')
    print('=' * 70)

    pages = declared_pages(args.static_project)
    print('静态声明的应用内页面 (%d)：%s' % (len(pages), pages))
    if not pages:
        print('⚠️ 没有静态声明，覆盖率无从计算（分母为 0）')
        return 1

    from ohauto.driver import Driver
    from ohauto.explorer import Budget, Explorer, TarpitPolicy

    # 设备预检：不在场时如实报「设备连接：失败」退出，绝不带着空树往下跑
    hdc = require_device(target=args.target)
    DeviceGuard(hdc, verbose=False).ensure_awake()
    hdc.shell('aa force-stop ' + args.bundle)
    hdc.shell('aa force-stop com.usb.right')      # 系统 USB 弹窗会霸占顶层窗口
    hdc.shell('uitest uiInput keyEvent Home')
    hdc.start_ability(args.bundle, args.ability)

    d = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
               artifact_dir=os.path.join(HERE, '_out', 'explore_coverage'),
               verbose=False)

    ex = Explorer(d, artifact_dir=os.path.join(HERE, '_out', 'explore_coverage'),
                  verbose=True,
                  tarpit_policy=TarpitPolicy(enabled=not args.tarpit_off))
    print('\n开始探索（max_pages=%d，tarpit=%s）…'
          % (args.max_pages, '关' if args.tarpit_off else '开'))
    graph = ex.explore(args.max_pages,
                       Budget(max_pages=args.max_pages,
                              max_actions_per_page=args.actions_per_page),
                       return_back=False)

    states = len(getattr(graph, 'states', {}) or {})
    trans = len(getattr(graph, 'transitions', []) or [])
    # page_path 口径：状态按设备自报 pagePath 归并到页
    visited_paths = {str(getattr(st, 'page_path', '') or '')
                     for st in (getattr(graph, 'states', {}) or {}).values()}
    visited_paths.discard('')
    covered = [p for p in pages if match_page(p, visited_paths)]
    missing = [p for p in pages if p not in covered]
    page_rate = (len(covered) / len(pages)) if pages else 0.0

    print()
    print('=' * 70)
    print('  结果（page_path 口径）')
    print('=' * 70)
    print('  静态声明页面数（分母）: %d  %s' % (len(pages), pages))
    print('  探索到达的页面        : %d  %s' % (len(covered), covered))
    print('  页面覆盖率            : %.0f%%' % (page_rate * 100))
    if missing:
        print('  ❌ 漏了的页面          : %s（可能是预算不够，也可能不可达）'
              % missing)
    print('  探索到页面状态数（辅助）: %d   跳转数: %d' % (states, trans))
    if states > len(pages):
        print('  （状态数 > 页面数 = 一页多态，属正常，别把状态数当覆盖率）')
    print('  tarpit 拦截           : %d 次%s'
          % (len(getattr(ex, 'tarpit_hits', []) or []),
             '（--tarpit-off 可对照）' if not args.tarpit_off else '（已关）'))
    if page_rate < 0.9:
        print('  ❌ 页面覆盖率 < 90%%（KPI 未达标）')
        return 1
    print('  ✅ 页面覆盖率 ≥ 90%（KPI 达标）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
