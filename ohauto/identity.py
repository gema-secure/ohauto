"""控件身份指纹 —— **跨页面与页面内**两级稳定标识。

为什么单成一个模块
------------------
原来 `type_fingerprint` / `control_key` 长在 `explorer.py`（L4 探索层）里，
而 `locator.py`（L3 定位层）为了复用它们不得不 **反向 import explorer**
—— 这就是结构债 S7：分层违例。

把身份指纹下沉到本模块（L0 基元层），explorer 与 locator 都从它导入，
依赖方向就正回来了：L3/L4 都只往下依赖，不互相横跳。

两个指纹的分工
--------------
* `type_fingerprint` —— **跨页面**找同类控件的锚点（只看 type）。
* `control_key` —— **页面内**可交互控件的稳定标识（id → text → descr → 几何兜底），
  刻意不含坐标：滚动/折叠/旋转都会让坐标失效（红线第 5 条）。

同一个控件在覆盖度统计（explorer）和自愈（locator）里必须是同一个身份，
所以这两个函数必须只有一份实现 —— 本模块就是那份单源。
"""
from __future__ import annotations

import hashlib

from .layout import LayoutNode

__all__ = ['type_fingerprint', 'control_key']


def _digest(text: str) -> str:
    """签名的哈希口径。sha1 而非 md5，避免有人对「指纹」二字有意见。"""
    return hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()


def type_fingerprint(node: LayoutNode) -> str:
    """控件的 **type 级稳定 ID**（`sha1(type)`）。

    用途与 `control_key` 不同：这个是**跨页面**找同类控件的锚点，
    是定位降级链里「层级路径 + 类型」那一级的实现基础。
    """
    return _digest(f'type:{node.type}')[:16]


def control_key(node: LayoutNode) -> str:
    """页面内可交互控件的**稳定标识**，用于覆盖度统计。

    刻意**不含坐标**：滚动、折叠、旋转都会让坐标失效（红线第 5 条），
    拿坐标当身份会让同一控件每动一下就变成一个「新控件」，覆盖率直接虚高。
    取值优先级：id → text → descr → 类型+尺寸（最后的兜底，稳定性最差）。
    """
    if node.id:
        base = f'id:{node.id}'
    elif node.text:
        base = f'text:{node.text}'
    elif node.descr:
        base = f'descr:{node.descr}'
    else:
        base = f'geo:{node.type}:{node.rect.width}x{node.rect.height}'
    return _digest(base)[:16]
