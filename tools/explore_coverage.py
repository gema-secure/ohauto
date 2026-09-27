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
1. **分子是「页面状态数」不是「页面数」** —— `explorer.StateGraph.states` 按内容签名去重，
   同一页面在不同状态下会算多个（比如点过"加一"之后）。所以分子**可能大于**分母，
   那不是"超额完成"，而是"一页多态"。**覆盖率 > 100% 时不该庆祝，该去看分母对不对。**
2. **分母只含应用内页面** —— 桌面卡片（`form_config.json` 的 src）不算，
   它不在应用路由里，探索器本来就进不去（这个坑 09-24 踩过）。

另外如实说明：`StateGraph.add_state` **只存内容签名的 hash、不保留 `page_path`**，
所以本工具**没法列出"具体漏了哪一页"**，只能给数量对比。
要精确到页，需要 `explorer.PageState` 增加 `page_path` 字段（归 B）。

用法::

    python tools/explore_coverage.py --static-project D:/project/ohauto-hypium-test \\
        --bundle com.example.myapplication --ability EntryAbility
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(HERE, 'static_arkts'))

from ohauto.runner import DeviceGuard                   # noqa: E402
from preflight import require_device, default_target                    # noqa: E402

DEFAULT_TARGET = default_target()   # 串号是隐私项：env OHAUTO_TARGET_SERIAL → hdc.config.json → 空时自动钉第一台
DEFAULT_PROJECT = r'D:\project\ohauto-hypium-test'
DEFAULT_BUNDLE = 'com.example.myapplication'


def declared_pages(project: str) -> list:
    """静态源：应用内页面清单（已排除桌面卡片）。"""
    from bridge import analyze_project, last_error
    info = analyze_project(project)
    if info is None:
        print('⚠️ 静态分析未执行：%s' % last_error())
        return []
    w = info.get('widget_pages') or []
    if w:
        print('  （已排除桌面卡片 %d 个：%s）' % (len(w), w))
    return list(info.get('pages') or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', default=DEFAULT_TARGET)
    ap.add_argument('--static-project', default=DEFAULT_PROJECT)
    ap.add_argument('--bundle', default=DEFAULT_BUNDLE)
    ap.add_argument('--ability', default='EntryAbility')
    ap.add_argument('--max-pages', type=int, default=8)
    ap.add_argument('--actions-per-page', type=int, default=4)
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
    from ohauto.explorer import Budget, Explorer

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
                  verbose=True)
    print('\n开始探索（max_pages=%d）…' % args.max_pages)
    graph = ex.explore(args.max_pages,
                       Budget(max_pages=args.max_pages,
                              max_actions_per_page=args.actions_per_page),
                       return_back=False)

    states = len(getattr(graph, 'states', {}) or {})
    trans = len(getattr(graph, 'transitions', []) or [])
    rate = states / len(pages) if pages else 0.0
    print()
    print('=' * 70)
    print('  结果')
    print('=' * 70)
    print('  静态声明页面数（分母）: %d  %s' % (len(pages), pages))
    print('  探索到页面状态数（分子）: %d' % states)
    print('  跳转数                : %d' % trans)
    print('  覆盖率                : %.0f%%' % (rate * 100))
    if rate > 1.0:
        print('  ⚠️ 覆盖率 >100% —— 分子是「页面状态」不是「页面」，'
              '同一页面多状态会重复计数，别当成超额完成')
    elif rate < 1.0:
        print('  ⚠️ 有缺口：静态声明了 %d 个页面，探索只到 %d 个状态'
              % (len(pages), states))
        print('     （注意：缺口可能来自探索预算不够，也可能来自页面确实不可达）')
    print()
    print('  ⚠️ 本工具给的是**数量对比**，列不出"具体漏了哪一页" ——')
    print('     `StateGraph.add_state` 只存签名 hash、不保留 page_path（归 B）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
