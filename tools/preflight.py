# -*- coding: utf-8 -*-
"""真机工装共用的设备预检 —— 设备不在场时「如实说」，绝不崩栈。

背景（2026-09-24 复核）
----------------------
`verify_core_flows_real.py` / `explore_coverage.py` / `demo_full_chain.py --real`
在设备缺席时会以未捕获的 `json.decoder.JSONDecodeError` 崩栈：`dumpLayout`
返回 `[Fail]Not match target founded...` 文本，`parse_layout()` 直接 `json.loads`。

**崩栈看起来像「代码有 bug」，而真相是「设备没插」** —— 这是最误导的一类失败。

修法
----
所有真机工装入口先调 :func:`require_device`：设备不在场时打印
「设备连接：失败」并以退出码 **2** 结束（与「测试未达标」的退出码 1 区分）。
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc                                    # noqa: E402


def default_target() -> str:
    """本机默认设备串号 —— **隐私项，绝不写进源码**。

    优先级：环境变量 `OHAUTO_TARGET_SERIAL` → `hdc.config.json` 的
    `target_serial` 键 → `''`。给空串时 `require_device` 会自动钉住
    第一台在线设备，单设备场景零配置可用。
    """
    v = os.environ.get('OHAUTO_TARGET_SERIAL', '')
    if v:
        return v
    cfg = os.path.join(ROOT, 'hdc.config.json')
    try:
        with open(cfg, encoding='utf-8') as f:
            return (json.load(f) or {}).get('target_serial') or ''
    except Exception:
        return ''


def require_device(target: str = None, hdc_path: str = None) -> Hdc:
    """构造 Hdc 并确认设备在场；不在场时**如实报失败**并退出（码 2）。

    返回就绪的 Hdc 实例（已带 target），调用方直接用，不要自己再 new ——
    否则就绕过了预检。
    """
    hdc = (Hdc(hdc_path=hdc_path, target=target) if hdc_path
           else Hdc(target=target))
    try:
        targets = hdc.list_targets()
    except Exception as e:                                    # hdc 本身跑不起来
        print('[设备连接] 失败：hdc list targets 异常 %s: %s'
              % (type(e).__name__, e))
        print('  真相是「环境不通」，不是代码有 bug。请先跑: python doctor.py')
        sys.exit(2)
    if not targets:
        print('[设备连接] 失败：未检测到任何设备（hdc list targets 为空）')
        print('  真相是「设备没插」，不是代码有 bug。')
        print('  请确认 USB 连接 / 设备授权，或先跑: python doctor.py')
        sys.exit(2)
    shown = [t if t else '<默认设备>' for t in targets]
    print('[设备连接] OK: %s' % ', '.join(shown))
    if target and target not in targets:
        print('[设备连接] 失败：指定的 %s 不在在线列表 %s 里'
              % (target, shown))
        print('  串号了。用 --target 改成上面列表里的一个，或先跑: python doctor.py')
        sys.exit(2)
    if not target:
        hdc.target = targets[0]      # 未指定串号时钉住第一台，后续 -t 保持一致
    return hdc
