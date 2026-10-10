"""ArkTS 工程静态分析（Node 读源码 + Python 桥接）。

包内子模块，随 ohauto 一起分发 —— 此前放在 tools/ 下，
导致 pip 安装后的 ohauto 找不到 analyze.mjs / bridge.py。

对外入口是 :mod:`ohauto.static_arkts.bridge`：
    from ohauto.static_arkts.bridge import analyze_project, control_hints
"""
from .bridge import analyze_project, control_hints, last_error

__all__ = ['analyze_project', 'control_hints', 'last_error']
