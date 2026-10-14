# -*- coding: utf-8 -*-
"""探针共用件：样本加载、控件口径、LocatorSpec 重建。

口径（与本仓库权威定义一致）
----------------------------
* 「可交互」= `LayoutNode.is_interactive()`
  （`ohauto/layout.py`：clickable or scrollable or type 属于一组容器类型）
* 「无标识」= 无 id 且无自身文案（text）

本目录下所有探针**只做测量**，不修改仓库行为。需要做前后对照时，
用子类覆盖对应方法（见 `probe_l3_with_deep.py`），不动物理文件。
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ohauto.identity import control_key, type_fingerprint      # noqa: E402
from ohauto.layout import flatten, parse_layout                # noqa: E402
from ohauto.locator import LocatorManager, LocatorSpec         # noqa: E402

__all__ = ['ROOT', 'DEFAULT_SAMPLES', 'LocatorManager', 'LocatorSpec',
           'control_key', 'type_fingerprint', 'flatten', 'parse_layout',
           'iter_samples', 'load_page', 'ancestors', 'blind_controls',
           'spec_of', 'print_table', 'pct']

#: 仓库内唯一的真机样本集（多应用控件树 + 截图）
DEFAULT_SAMPLES = os.path.join(ROOT, 'datasets', 'gallery_13app')


def iter_samples(samples_dir: str):
    """逐个产出 `(名称, 控件树根节点)`；跳过 `*.meta.json`。"""
    for fn in sorted(os.listdir(samples_dir)):
        if not fn.endswith('.json') or fn.endswith('.meta.json'):
            continue
        yield fn[:-5], load_page(os.path.join(samples_dir, fn))


def load_page(path: str):
    """读一个控件树 JSON 并解析成 `LayoutNode`。"""
    with open(path, encoding='utf-8') as f:
        return parse_layout(json.load(f))


def ancestors(node):
    """自底向上的祖先链。"""
    out, p = [], node.parent
    while p is not None:
        out.append(p)
        p = p.parent
    return out


def blind_controls(page):
    """可交互、但既无 id 也无自身文案的控件 —— 定位只能落到层级路径那一级。"""
    return [n for n in flatten(page, only_visible=True)
            if n.is_interactive()
            and not (getattr(n, 'id', '') or '').strip()
            and not (getattr(n, 'text', '') or '').strip()]


def spec_of(node, locator_id: str = 'probe') -> LocatorSpec:
    """按 `LocatorManager.register()` 的写法重建定位规格。

    字段取值与 `ohauto/locator.py::register` 逐项对应，这样探针测得的就是
    真实定位路径会拿到的输入。
    """
    spec = LocatorSpec(locator_id=locator_id, description='probe',
                       target_id='', text='', descr='',
                       target_type=node.type)
    spec.type_fp = type_fingerprint(node)
    spec.control_key = control_key(node)
    spec.text_deep = (getattr(node, 'text_deep', '') or '').strip()
    spec.parent_types = tuple(p.type for p in ancestors(node))
    return spec


def pct(hit: int, total: int) -> str:
    return f'{hit / total * 100:.1f}%' if total else '-'


def print_table(headers, rows, aligns=None) -> None:
    """等宽表格输出：中文字符按两格算宽（Python 的格式化按字符数补白，不够用）。"""
    aligns = aligns or ['<'] * len(headers)

    def width(text: str) -> int:
        return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))

    cols = len(headers)
    widths = [width(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], width(row[i]))

    def cell(text, i) -> str:
        text = str(text)
        gap = ' ' * (widths[i] - width(text))
        return gap + text if aligns[i] == '>' else text + gap

    line = '-' * (sum(widths) + 2 * (cols - 1))
    print('  '.join(cell(h, i) for i, h in enumerate(headers)))
    print(line)
    for row in rows:
        print('  '.join(cell(v, i) for i, v in enumerate(row)))
    print(line)
