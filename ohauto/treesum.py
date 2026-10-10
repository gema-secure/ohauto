"""控件树摘要器
=====================

把 `dumpLayout` 导出的几百节点控件树压成**人工可读的骨架**，供全组使用：

- B 调试探索器时，看骨架比看原始 JSON 快得多；
- C 出报告时，摘要可以直接贴进 Markdown；
- A（我自己）做定位器自愈时，用它肉眼核对「降级链最后落在哪个控件上」。

保留规则：
    只保留「有 text / 有 contentDescription(即 descr) / 有 id」或
    「可交互（clickable / longClickable / scrollable / focusable / enabled）」
    的节点，其余节点不占行、子节点缩进不上移；限制最大深度防爆栈。

实现上有一条与卡片字面不同、但必须说明的取舍：
    **`enabled` 单独为真不构成保留条件。** 真机控件树上几乎全部节点
    enabled="true"（见 real_dayu200_usb_dialog.json），若按字面实现，
    摘要器会把整棵树原样输出，压缩率永远 = 0。
    因此这里把「可交互」实现为：clickable / longClickable / scrollable /
    focusable 任一为真。`enabled` 仍然采集，但只作为行内状态展示，
    不触发保留。如需严格按字面行为，传 keep_enabled=True。

真机适配（B 的设计输入，2026-09-23 @A）：
    真机上可交互容器**自身文案与 id 双双为空**（app_settings 0 个 id、
    标签在子 Text 上；sample_calc 19/20 只有 id）—— 所以「按 id 认控件」
    和「按 text 认控件」单用哪一个都会废掉一边，必须**双向覆盖**，并允许
    **借子节点文案**（LayoutNode.text_deep）：
    - 摘要行 `_node_line`：自身无 id/text/descr/hint 的可交互容器，
      行尾追加 `deep="…"`（借来的子树文案，截断展示）；
    - 新增 `control_catalog()`：给 `Generator(catalog_fn=…)` 的正式版
      控件清单，签名与 B 的最小实现一致（page, limit=60) -> str，换一行
      即可替换 —— 区别正是上面两条：双向产出 + 借文案（截断）而不是丢弃。
"""

from __future__ import annotations

from typing import Any, List, Optional, TextIO

from .layout import LayoutNode, flatten, parse_layout

__all__ = ['summarize_tree', 'summary_lines', 'count_nodes',
           'is_summary_worthy', 'control_catalog']

# LayoutNode 没有单独的 longClickable / focusable 字段，这两个语义
# 存在 attributes 里（真机字段名如下）。
_ATTR_TRUE = ('true', '1', 'yes')


def _attr_true(node: LayoutNode, key: str) -> bool:
    v = node.attributes.get(key)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _ATTR_TRUE if v is not None else False


def is_interactive_for_summary(node: LayoutNode,
                               keep_enabled: bool = False) -> bool:
    """摘要口径的「可交互」判定（与 layout.LayoutNode.is_interactive 互补）。

    LayoutNode.is_interactive 面向探索候选筛选（含类型白名单）；
    这里面向**信息压缩**——任何暗示「用户能按/能滑/能聚焦」的信号都保留，
    不看类型白名单，否则自定义类型会被误丢。
    """
    if node.clickable or node.scrollable:
        return True
    if _attr_true(node, 'longClickable') or _attr_true(node, 'focusable'):
        return True
    if keep_enabled and node.enabled:
        return True
    return False


def is_summary_worthy(node: LayoutNode, keep_enabled: bool = False) -> bool:
    """节点是否值得占一行。"""
    if node.text or node.descr or node.hint or node.id:
        return True
    return is_interactive_for_summary(node, keep_enabled=keep_enabled)


def count_nodes(root: LayoutNode) -> int:
    """整棵树的节点总数（含不可见节点）—— 作为压缩率的分母。"""
    return sum(1 for _ in root.walk())


def _node_line(node: LayoutNode) -> str:
    """一行摘要。坐标一定要带 —— 定位问题肉眼排查时没有坐标等于没写。

    借子节点文案（真机适配）：自身无 id/text/descr/hint 的可交互容器，
    追加 `deep="…"`（text_deep，截断到 40 字符）—— 否则真机上一整行
    只剩类型和坐标，摘要器在真机上等于没有（B 2026-09-23）。
    """
    indent = '  ' * node.depth
    flags = []
    if node.clickable:
        flags.append('clickable')
    if node.scrollable:
        flags.append('scrollable')
    if _attr_true(node, 'longClickable'):
        flags.append('longClickable')
    if _attr_true(node, 'focusable'):
        flags.append('focusable')
    if not node.enabled:
        flags.append('disabled')
    if not node.visible:
        flags.append('invisible')
    flag_s = (' [' + ','.join(flags) + ']') if flags else ''
    r = node.rect
    line = (f'{indent}- {node.type or "?"}'
            f'{" id=" + node.id if node.id else ""}'
            f'{" text=" + repr(node.text) if node.text else ""}'
            f'{" descr=" + repr(node.descr) if node.descr else ""}'
            f'{" hint=" + repr(node.hint) if node.hint else ""}'
            f' [{r.left},{r.top}][{r.right},{r.bottom}]'
            f'{flag_s}')
    if not (node.text or node.descr or node.hint or node.id):
        deep = node.text_deep
        if deep:
            if len(deep) > 40:
                deep = deep[:39] + '…'
            line += f' deep={deep!r}'
    return line


def summary_lines(root: LayoutNode, max_depth: Optional[int] = None,
                  keep_enabled: bool = False,
                  keep_invisible: bool = False) -> List[str]:
    """生成摘要行列表。

    Parameters
    ----------
    max_depth:
        超过该深度的节点直接剪掉（不占行、其后代也剪掉）。
        这是防爆栈的硬限制，也防止摘要比原文还长。
        None 表示不限制深度（树的递归由 walk 顺序驱动，迭代实现不炸栈）。

    keep_enabled:
        严格按字面要求，把 enabled 当作可交互信号。默认 False，理由见模块 docstring。

    keep_invisible:
        是否保留不可见节点。默认 False —— 摘要服务于「人看当前界面」，
        不可见节点是噪音；要审计整棵树时再打开。

    Notes
    -----
    「子节点缩进不上移」的实现方式：每个保留节点按它在**原树中的绝对
    深度**（node.depth）缩进。被跳过的中间节点不留行，也不改变其子孙
    的缩进 —— 这样摘要的缩进始终与 dumpLayout 原始层级对齐，
    对照原 JSON 时不用重新数层。
    """
    lines: List[str] = []
    # 显式栈迭代，避免超深控件树把解释器递归打爆（真机上 hierarchy 已见 10+ 层）
    stack: List[LayoutNode] = [root]
    while stack:
        n = stack.pop()
        if max_depth is not None and n.depth > max_depth:
            continue          # 剪枝：其后代不再入栈
        if not keep_invisible and not n.visible:
            continue
        if n is not root and is_summary_worthy(n, keep_enabled=keep_enabled):
            lines.append(_node_line(n))
        # 先压右侧子节点再压左侧，保证输出顺序与先序遍历一致
        for c in reversed(n.children):
            stack.append(c)
    return lines


def summarize_tree(root: LayoutNode, max_depth: Optional[int] = None,
                   keep_enabled: bool = False, keep_invisible: bool = False,
                   header: bool = True, out: Optional[TextIO] = None) -> str:
    """把控件树压成人工可读骨架，返回多行字符串。

    header=True 时首行带上总节点数与压缩率，方便验收时一眼核对指标。
    传 out= 则同时写入文件对象（报告生成用）。
    """
    lines = summary_lines(root, max_depth=max_depth,
                          keep_enabled=keep_enabled,
                          keep_invisible=keep_invisible)
    total = count_nodes(root)
    head = ''
    if header:
        ratio = (len(lines) / total * 100) if total else 0.0
        head = (f'# 控件树摘要：{len(lines)} 行 / 全树 {total} 节点'
                f'（{ratio:.1f}%）\n')
    text = head + '\n'.join(lines)
    if text and not text.endswith('\n'):
        text += '\n'
    if out is not None:
        out.write(text)
    return text


def control_catalog(page: Any, limit: int = 60,
                    text_limit: int = 30) -> str:
    """控件清单 —— `Generator(catalog_fn=…)` 的替换件。

    签名与 B 的最小实现完全一致（``control_catalog(page, limit=60) -> str``），
    B 侧 ``Generator(catalog_fn=ohauto.treesum.control_catalog)`` 换一行即可。
    与最小实现的两点差异（B 的真机设计输入，2026-09-23 @A）：

    - **双向覆盖**：id 维度与文案维度都产出 —— sample_calc 19/20 只有 id、
      app_settings 0 个 id 且标签在子 Text 上，按单键筛必废一边；
    - **借子节点文案**：自身无 text/descr 的可交互容器，输出
      ``deep="…"`（text_deep，截断到 text_limit）—— 借文案而不是
      整行丢弃，否则清单在真机上等于没有。

    自身 text 超过 text_limit 时**截断保留**（B 的最小实现是整行跳过）：
    长文案行该防的是撑爆 prompt，不是连人带 id 一起扔。
    """
    if page is None:
        return '(未提供控件树)'
    root = page if isinstance(page, LayoutNode) else parse_layout(page)
    lines: List[str] = []
    for n in flatten(root, only_visible=True):
        if n.rect.area <= 0:
            continue
        if not (n.clickable or n.scrollable or n.id or n.text
                or n.descr or is_interactive_for_summary(n)):
            continue
        head = n.type or '?'
        if n.id:
            head += f' id={n.id}'
        if n.text:
            text = n.text if len(n.text) <= text_limit else n.text[:text_limit] + '…'
            head += f' text="{text}"'
        elif n.descr:
            descr = n.descr if len(n.descr) <= text_limit else n.descr[:text_limit] + '…'
            head += f' descr="{descr}"'
        deep = n.text_deep
        if deep and deep != n.text:
            if len(deep) > text_limit:
                deep = deep[:text_limit] + '…'
            head += f' deep="{deep}"'
        lines.append(head)
        if len(lines) >= limit:
            break
    return '\n'.join(lines) or '(控件树里没有可用控件)'
