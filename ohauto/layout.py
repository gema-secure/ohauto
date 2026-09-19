"""
L2 定位层 —— 控件树解析
=======================

把 `uitest dumpLayout` 导出的 JSON 解析成 LayoutNode 树对象。

注意：鸿蒙各版本 dumpLayout 的 JSON 结构存在差异，因此本模块采取**宽进**策略：
字段名做多路兜底，bounds 兼容四种形态 —— 真机的 `"[l,t][r,b]"`、JSON 对象串
`"{\"left\":..}"`、纯数字串 `"l,t,r,b"`、以及嵌套 dict。
首次在真机跑通后，建议用 `LayoutNode.describe()` 核对真实结构并微调
ATTR_ALIASES / CHILD_KEYS 即可。

真机基线（2026-09-16 实测，润和 DAYU200 / OpenHarmony 5.0.3.135 / API 15 /
uitest 5.0.1.2）：
  - 顶层键 `attributes` + `children`，节点共 32 种字段
  - 12 个语义**全部命中**：type / id / text / bounds / description / clickable /
    visible / enabled / focused / scrollable / checked / hint
  - 另有 18 个未收录字段，其中对定位器自愈有价值的是：
    `hashcode`（节点稳定哈希）、`hierarchy`（层级路径）、
    `bundleName` / `abilityName` / `pagePath`（**节点自带页面归属**，
    可直接作为页面签名来源，不必自己拼）
  - 屏幕 720×1280（MIPI，9:16 竖屏），devicetype = `default`
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple


# ---------------------------------------------------------------- 字段别名

# 同一语义在不同版本里的可能字段名
ATTR_ALIASES: Dict[str, Tuple[str, ...]] = {
    'type':      ('type', 'Type', 'componentType', 'cls', 'class'),
    'id':        ('id', 'Id', 'ID', 'componentId'),
    'text':      ('text', 'Text', 'content', 'Content', 'label'),
    'bounds':    ('bounds', 'Bounds', 'bound', 'rect', 'frame'),
    'descr':     ('description', 'descr', 'Description', 'accessibilityText'),
    'clickable': ('clickable', 'Clickable'),
    'visible':   ('visible', 'Visible', 'shown'),
    'enabled':   ('enabled', 'Enabled'),
    'focused':   ('focused', 'Focused'),
    'scrollable': ('scrollable', 'Scrollable'),
    'checked':   ('checked', 'Checked'),
    'hint':      ('hint', 'placeholder', 'Placeholder'),
}

CHILD_KEYS = ('children', 'Children', 'child', 'nodes', 'subNodes')
ATTR_KEYS  = ('attributes', 'Attributes', 'attrs', 'attribute', 'props')


def _pick(d: Dict[str, Any], semantic: str, default: Any = None) -> Any:
    """按别名表从 dict 中取第一个存在的值。"""
    for k in ATTR_ALIASES.get(semantic, (semantic,)):
        if k in d and d[k] not in (None, ''):
            return d[k]
    return default


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ('true', '1', 'yes')
    return False


# ---------------------------------------------------------------- 节点

@dataclass
class Rect:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> Tuple[int, int]:
        """控件中心点 —— uiInput 全部基于坐标，这是最后一步的关键换算。"""
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    @property
    def area(self) -> int:
        return self.width * self.height

    def to_dict(self) -> Dict[str, int]:
        return {'left': self.left, 'top': self.top,
                'right': self.right, 'bottom': self.bottom}

    # 真机 `uitest dumpLayout` 的实际写法是 "[left,top][right,bottom]"，例如
    # "[0,0][720,1280]"。两个方括号之间**没有任何分隔符**，所以不能简单地
    # 去掉方括号再 split —— "0,0][720,1280" 会变成 "0,0720,1280"，
    # 把 0 和 720 粘成一个数，最后只得 3 个数字而解析失败（真机实测踩到过，
    # 后果是所有控件坐标都退化成 0，点击全部落到左上角）。
    # 因此这里用正则显式抠两组坐标。
    _BOUNDS_PAIR_RE = re.compile(
        r'\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]'
        r'\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]')
    _NUM_RE = re.compile(r'-?\d+(?:\.\d+)?')

    @classmethod
    def parse(cls, raw: Any) -> 'Rect':
        """兼容 "[l,t][r,b]" / JSON 字符串 / dict / 列表 四种形态。"""
        if raw is None:
            return cls()
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return cls()
            m = cls._BOUNDS_PAIR_RE.search(s)
            if m:
                return cls(*(int(float(g)) for g in m.groups()))
            try:
                raw = json.loads(s)
            except (json.JSONDecodeError, TypeError):
                # 形如 "37,280,118,361" / "37 280 118 361" / "[37,280,118,361]"
                nums = cls._NUM_RE.findall(s)
                if len(nums) == 4:
                    return cls(*(int(float(n)) for n in nums))
                return cls()          # 完全无法解析：退化为空矩形而非抛异常
        if isinstance(raw, dict):
            return cls(
                int(raw.get('left', raw.get('x', 0))),
                int(raw.get('top', raw.get('y', 0))),
                int(raw.get('right', raw.get('x2', 0))),
                int(raw.get('bottom', raw.get('y2', 0))),
            )
        if isinstance(raw, (list, tuple)) and len(raw) == 4:
            return cls(int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]))
        return cls()

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x <= self.right and self.top <= y <= self.bottom

    def overlap_ratio(self, other: 'Rect') -> float:
        """与另一矩形的交并比（IoU），用于视觉通道与控件树通道的一致性校验。"""
        ix = max(0, min(self.right, other.right) - max(self.left, other.left))
        iy = max(0, min(self.bottom, other.bottom) - max(self.top, other.top))
        inter = ix * iy
        union = self.area + other.area - inter
        return inter / union if union else 0.0


@dataclass
class LayoutNode:
    """控件树节点。"""
    type: str = ''
    id: str = ''
    text: str = ''
    descr: str = ''
    hint: str = ''
    rect: Rect = field(default_factory=Rect)
    clickable: bool = False
    visible: bool = True
    enabled: bool = True
    focused: bool = False
    scrollable: bool = False
    checked: bool = False
    attributes: Dict[str, Any] = field(default_factory=dict)
    children: List['LayoutNode'] = field(default_factory=list)
    parent: Optional['LayoutNode'] = field(default=None, repr=False)

    # ---------------------------------------------------------- 属性

    @property
    def center(self) -> Tuple[int, int]:
        return self.rect.center

    @property
    def label(self) -> str:
        """最贴近「人能看懂」的名称，用于日志与视觉提示。

        优先级：text > descr > hint > id > type。
        把 descr / hint 排在 id 之前，是因为它们是给人看的无障碍文案，
        而 id 是代码标识（如 'btn_login'），对阅读日志和喂给视觉模型都不友好。
        """
        return self.text or self.descr or self.hint or self.id or self.type

    @property
    def depth(self) -> int:
        d, p = 0, self.parent
        while p is not None:
            d += 1
            p = p.parent
        return d

    @property
    def path(self) -> str:
        """层级路径，形如 Column > Row > Button#btn_login —— 便于日志定位。"""
        seg = self.type or '?'
        if self.id:
            seg += f'#{self.id}'
        chain, p = [seg], self.parent
        while p is not None:
            s = p.type or '?'
            if p.id:
                s += f'#{p.id}'
            chain.append(s)
            p = p.parent
        return ' > '.join(reversed(chain))

    def is_interactive(self) -> bool:
        """是否可能可交互 —— 用于自动探索时筛选候选控件。

        ⚠️⚠️ **这是方法，不是 property —— 必须带括号调用！**

        同一个类里 `clickable` / `scrollable` / `visible` / `path` 都是
        property，**只有这个是普通方法**，风格不一致。因此：

        ```python
        if n.is_interactive:      # ❌ 拿到方法对象本身
        if n.is_interactive():    # ✅ 真正调用
        ```

        漏括号**不会报错** —— 方法对象恒为真值，于是所有节点都被当成可交互。
        实测踩坑（2026-09-19）：采集统计报「169 个节点全部可交互」，
        而原始 JSON 里只有 11 个 `clickable=true`。
        这类错误是**静默**的，只有交叉核对原始数据才能发现。

        > 若将来要把它改成 property 以统一风格，**必须同步修改全部调用点**
        > （`crossform.py` / `explorer.py` / `matcher.py` / 内部调用），
        > 属于对外接口变更，要先通知 A、B。
        """
        return self.clickable or self.scrollable or self.type in (
            'Button', 'TextInput', 'Checkbox', 'Radio', 'Switch', 'Slider',
            'Toggle', 'Search', 'MenuItem', 'TabContent', 'ListItem')

    # ---------------------------------------------------------- 遍历

    def walk(self) -> Iterator['LayoutNode']:
        """先序遍历自身与所有后代。"""
        yield self
        for c in self.children:
            yield from c.walk()

    def find_all(self, pred) -> List['LayoutNode']:
        return [n for n in self.walk() if pred(n)]

    def describe(self, indent: int = 0) -> str:
        lines = ['  ' * indent + f'- {self.type}'
                 f'{" id=" + self.id if self.id else ""}'
                 f'{" text=" + repr(self.text) if self.text else ""}'
                 f' {self.rect.to_dict()}'
                 f'{" [clickable]" if self.clickable else ""}'
                 f'{" [invisible]" if not self.visible else ""}']
        for c in self.children:
            lines.append(c.describe(indent + 1))
        return '\n'.join(lines)


# ---------------------------------------------------------------- 构建

def _build(raw: Any, parent: Optional[LayoutNode] = None) -> Optional[LayoutNode]:
    if not isinstance(raw, dict):
        return None

    # 属性可能在 'attributes' 子对象里，也可能平铺在节点上
    attrs: Dict[str, Any] = {}
    for ak in ATTR_KEYS:
        if isinstance(raw.get(ak), dict):
            attrs = raw[ak]
            break
    # 组装扁平属性表时，必须剔除 attributes / children 这类「容器键」，
    # 否则它们会把整棵子树/整个属性字典也带进来 —— 会让按属性模糊搜索
    # （如 label_contains）误命中所有节点。
    merged = {k: v for k, v in raw.items()
              if k not in ATTR_KEYS and k not in CHILD_KEYS}
    merged.update(attrs)

    node = LayoutNode(
        type=str(_pick(merged, 'type', '') or ''),
        id=str(_pick(merged, 'id', '') or ''),
        text=str(_pick(merged, 'text', '') or ''),
        descr=str(_pick(merged, 'descr', '') or ''),
        hint=str(_pick(merged, 'hint', '') or ''),
        rect=Rect.parse(_pick(merged, 'bounds')),
        clickable=_as_bool(_pick(merged, 'clickable', False)),
        visible=_as_bool(_pick(merged, 'visible', True)),
        enabled=_as_bool(_pick(merged, 'enabled', True)),
        focused=_as_bool(_pick(merged, 'focused', False)),
        scrollable=_as_bool(_pick(merged, 'scrollable', False)),
        checked=_as_bool(_pick(merged, 'checked', False)),
        attributes=merged,
        parent=parent,
    )

    for ck in CHILD_KEYS:
        kids = raw.get(ck)
        if isinstance(kids, list):
            for k in kids:
                child = _build(k, node)
                if child is not None:
                    node.children.append(child)
            break

    return node


def parse_layout(source: Any) -> LayoutNode:
    """从 JSON 文本 / dict / 文件路径 解析出控件树根节点。

    Parameters
    ----------
    source:
        - str：若像是文件路径且存在，则读文件；否则当作 JSON 文本
        - dict：直接解析
    """
    if isinstance(source, dict):
        data = source
    elif isinstance(source, (bytes, bytearray)):
        data = json.loads(source.decode('utf-8', 'replace'))
    elif isinstance(source, str):
        s = source.strip()
        if not s.startswith(('{', '[')) and os.path.exists(s):
            with open(s, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
        else:
            data = json.loads(s)
    else:
        raise TypeError(f'不支持的输入类型: {type(source)}')

    # 可能是 {"root": {...}} 或直接就是根节点
    if isinstance(data, list):
        data = data[0] if data else {}
    for rk in ('root', 'Root', 'node', 'data'):
        if isinstance(data.get(rk), dict):
            data = data[rk]
            break

    root = _build(data)
    if root is None:
        raise ValueError('无法从输入中解析出控件树根节点')
    return root


def flatten(root: LayoutNode, only_visible: bool = True,
            only_interactive: bool = False) -> List[LayoutNode]:
    """把控件树摊平成候选列表，供匹配器检索。"""
    out = []
    for n in root.walk():
        if only_visible and not n.visible:
            continue
        if only_interactive and not n.is_interactive():
            continue
        out.append(n)
    return out
