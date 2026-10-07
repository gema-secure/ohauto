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
    # A6（任务卡）：不同鸿蒙版本 / 不同导出工具里，「这个控件在屏幕上的矩形」
    # 可能叫五种名字。越具体的名字放越后面——_pick 按顺序取第一个存在的键，
    # 'bounds' 是真机实测的标准键，必须保持第一优先。
    'bounds':    ('bounds', 'Bounds', 'bound', 'rect', 'frame',
                  'rectInScreen', 'visibleBounds', 'region'),
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
            # A6 加固：字典形态要做「缺角补全」而不是静默取 0。
            # 旧实现 raw.get('right', raw.get('x2', 0)) 在只给 left+width 时
            # 会把 right 当成 0，得到一个负宽矩形且无任何告警 —— 这类
            # 「测试全绿但结论是假的」事故分工卡第一章第（六）节点名过两次。
            # 规则：
            #   left  ← left / x / startX
            #   top   ← top / y / startY
            #   right ← right / x2 / endX；缺省时用 left+width 推导
            #   bottom← bottom / y2 / endY；缺省时用 top+height 推导
            # 四个角仍凑不齐才算解析失败（退化为空矩形）。
            def _num(*keys: str) -> Optional[float]:
                for k in keys:
                    v = raw.get(k)
                    if v not in (None, ''):
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            continue
                return None

            left = _num('left', 'x', 'startX')
            top = _num('top', 'y', 'startY')
            right = _num('right', 'x2', 'endX')
            bottom = _num('bottom', 'y2', 'endY')
            width = _num('width', 'w')
            height = _num('height', 'h')
            if right is None and left is not None and width is not None:
                right = left + width
            if bottom is None and top is not None and height is not None:
                bottom = top + height
            if None in (left, top, right, bottom):
                return cls()          # 仍是宽进：凑不齐就空矩形，不抛异常
            return cls(int(left), int(top), int(right), int(bottom))
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
    def text_deep(self) -> str:
        """控件**自身及其子树**里的文案，拼成一个串。

        ⚠️ 这条是为真机结构加的 —— **可交互性在容器上、文案在子节点上**。
        实测（2026-09-22，设备自带「设置」页）12 个可交互控件里：

            自身带文案           0 个
            自身无文案、子节点有  12 个   ← 100%

        真实形态：

            Flex (clickable=true, text='')
             ├ Text (text='蓝牙')
             └ Text (text='已关闭')

        所以任何「按文案判断这个控件是干什么的」逻辑 ——
        安全策略的「删除/支付」拦截、探索器的关键词加权、压测目标挑选 ——
        **只看 `node.text` 在真机上会全部落空**（模拟夹具里 clickable 和
        text 常在同一节点，所以自测发现不了）。要文案请用这个属性。

        自身有文案时直接返回自身（更精确）；否则按子树顺序收集、去重。
        """
        own = (self.text or '').strip()
        if own:
            return own
        parts: List[str] = []
        for k in self.walk():
            t = (k.text or '').strip()
            if t and t not in parts:
                parts.append(t)
        return ' '.join(parts)

    @property
    def label(self) -> str:
        """最贴近「人能看懂」的名称，用于日志与视觉提示。

        优先级：text > descr > hint > **text_deep** > id > type。

        为什么 descr / hint / text_deep 都排在 id 之前：这一组是**给人看的**
        无障碍文案，id 是代码标识（如 `btn_login`、`item_bluetooth`）——
        对读日志的人和喂给视觉模型都不友好。

        ⚠️ `text_deep` 必须排在 id 之前（2026-09-22 实测踩到）：
        真机上可交互容器的形态是 `Flex(id='item_reset', text='')` +
        子节点 `Text('恢复出厂设置')`。若 id 优先，日志里这个危险按钮就叫
        `item_reset`，而安全策略按文案匹配「恢复出厂」也照样找不到它。
        排到 id 前面之后，同一个控件就叫「恢复出厂设置」了。

        反方向也验过：计算器按键这种**既没文案也没子节点文案**的，
        `text_deep` 为空，自然退到 id（`'7'`）—— 不会退化成 `'Button'`。
        """
        return (self.text or self.descr or self.hint
                or self.text_deep or self.id or self.type)

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
        """是否可能可交互 —— 用于自动探索时筛选候选控件。"""
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

#: 控件树的最大解析深度。真机树实测 30 层上下，留 6 倍余量。
#: 超过这个数的只可能是敌意输入或坏数据 —— 再往下递归会撞 Python 的栈上限，
#: 裸崩成 `RecursionError`（看起来像代码 bug，实际是输入问题）。
MAX_TREE_DEPTH = 200

#: JSON 文本的括号嵌套深度上限。**`json.loads` 自己也是递归实现** ——
#: 实测（本机 3.13）控件树形状在 ~1500 层树深处开始 `RecursionError`，
#: 而且崩在标准库里面，拦截点在 `_build` 之前根本轮不到。
#: 取 1000（≈ 500 层树）留一倍余量：既能挡下敌意输入，又比真正的崩点低一半。
MAX_JSON_NESTING = 1000

#: 设备侧 `dumpLayout` 失败时返回的文本前缀。它不是 JSON，形如
#: `[Fail]Not match target founded, check config or confirm the key`
DEVICE_FAIL_PREFIX = '[Fail]'


class LayoutParseError(ValueError):
    """控件树输入无法解析。

    `reason` 是机器可读的短串，调用方据此区分"如实说设备不在场"和"数据坏了"：

        'device_fail'  设备侧返回的是失败文本（多半设备没插 / uitest 未就绪）
        'not_json'     输入不是 JSON
        'too_deep'     嵌套深到会撞栈（拒绝解析）
        'empty'        空输入
        'io'           读文件失败
    """

    def __init__(self, message: str, reason: str = 'not_json') -> None:
        super().__init__(message)
        self.reason = reason


def _json_nesting_depth(text: str) -> int:
    """粗算 JSON 的括号嵌套深度。**迭代实现**（要拦的就是递归），
    并跳过字符串字面量里的括号。"""
    depth = deepest = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in '{[':
            depth += 1
            deepest = max(deepest, depth)
        elif ch in '}]':
            depth -= 1
    return deepest


def _loads_json(text: str) -> Any:
    """解析 JSON 文本；不是 JSON 时给**说得清**的错误。

    裸抛 `JSONDecodeError` 会让"设备没插"看起来像"代码崩了" —— 这是最误导的
    一类失败，所以这里把原因分开标出来。嵌套过深则在 `json.loads` 之前拦下。
    """
    stripped = text.lstrip()
    if stripped.startswith(DEVICE_FAIL_PREFIX):
        raise LayoutParseError(
            f'设备侧返回失败文本（不是控件树）：{text.strip()[:200]}'
            f' —— 多半是设备不在场或 uitest 未就绪', 'device_fail')
    if not stripped.startswith(('{', '[')):
        raise LayoutParseError(
            f'输入不是 JSON 文本（前 80 字符）：{text.strip()[:80]!r}', 'not_json')
    depth = _json_nesting_depth(text)
    if depth > MAX_JSON_NESTING:
        raise LayoutParseError(
            f'JSON 嵌套过深（{depth} 层 > 上限 {MAX_JSON_NESTING}）—— 拒绝解析：'
            f'再深会在 json.loads 内部撞栈', 'too_deep')
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LayoutParseError(f'JSON 解析失败: {e}', 'not_json') from e
    except RecursionError as e:                  # 兜底：扫描判据没覆盖的形状
        raise LayoutParseError('JSON 嵌套过深，解析时撞栈', 'too_deep') from e


def _build(raw: Any, parent: Optional[LayoutNode] = None,
           depth: int = 0) -> Optional[LayoutNode]:
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
            if depth + 1 >= MAX_TREE_DEPTH:
                # 截断：不再下探。**记在节点上**，别静默把子树丢掉 ——
                # 一条"树被截断了"的标记，比一个看起来正常的浅树有用得多。
                node.truncated_children = len(kids)
                break
            for k in kids:
                child = _build(k, node, depth + 1)
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

    Raises
    ------
    LayoutParseError:
        输入不是 JSON、是空串，或**设备侧的失败文本**（`[Fail]...`，多半是
        设备不在场）。带 `reason` 字段，调用方据此如实报告而不是崩栈。
    TypeError: 输入类型不支持。

    Notes
    -----
    敌意/坏数据的超深嵌套在 `MAX_TREE_DEPTH` 处截断，截断点的节点上带
    `truncated_children` 计数 —— 不裸崩 `RecursionError`，也不静默丢子树。
    真机树实测 30 层上下，正常样本永远碰不到这个上限。
    """
    if isinstance(source, dict):
        data = source
    elif isinstance(source, (bytes, bytearray)):
        data = _loads_json(source.decode('utf-8', 'replace'))
    elif isinstance(source, str):
        s = source.strip()
        if not s:
            raise LayoutParseError('输入是空字符串 —— 没有控件树可解析', 'empty')
        if not s.startswith(('{', '[')) and os.path.exists(s):
            try:
                with open(s, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError as e:
                raise LayoutParseError(f'读文件失败（{s}）: {e}', 'io') from e
            data = _loads_json(text)
        else:
            data = _loads_json(s)
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
