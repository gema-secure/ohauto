"""
L2 定位层 —— 多属性匹配器
=========================

对齐鸿蒙 UiTest 的 ON 语义，但比它更强：支持文本模糊匹配、正则、
坐标区域约束，以及 within 相对位置限定。

    ON.text('登录')                         精确文本
    ON.text_contains('确定')                 包含
    ON.text_matches(r'^第\\d+页$')            正则
    ON.id('btn_login')                     控件 id
    ON.type('Button')                      控件类型
    ON.text('提交').within(ON.type('Scroll')) 限定在某个容器内查找
    ON.type('Image').clickable(True)         组合属性
    ON.all(ON.type('ListItem').text_contains('订单'))
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Sequence

from .layout import LayoutNode, Rect


class Matcher:
    """控件匹配器。所有条件为 AND 关系；链式调用返回新的 Matcher（不可变）。"""

    def __init__(self,
                 preds: Optional[Sequence[Callable[[LayoutNode], bool]]] = None,
                 desc: Optional[Sequence[str]] = None,
                 within: Optional['Matcher'] = None,
                 index: Optional[int] = None):
        self._preds: List[Callable[[LayoutNode], bool]] = list(preds or [])
        self._desc: List[str] = list(desc or [])
        self._within = within
        self._index = index

    # -------------------------------------------------------- 内部

    def _add(self, pred: Callable[[LayoutNode], bool], desc: str) -> 'Matcher':
        return Matcher(self._preds + [pred], self._desc + [desc],
                       self._within, self._index)

    def _copy(self, **kw) -> 'Matcher':
        base = dict(preds=self._preds, desc=self._desc,
                    within=self._within, index=self._index)
        base.update(kw)
        return Matcher(**base)

    # -------------------------------------------------------- 条件

    def type(self, t: str, exact: bool = True) -> 'Matcher':
        if exact:
            return self._add(lambda n: n.type == t, f'type={t}')
        key = t.lower()
        return self._add(lambda n: key in n.type.lower(), f'type~{t}')

    def id(self, i: str, exact: bool = True) -> 'Matcher':
        if exact:
            return self._add(lambda n: n.id == i, f'id={i}')
        key = i.lower()
        return self._add(lambda n: key in n.id.lower(), f'id~{i}')

    def text(self, t: str) -> 'Matcher':
        return self._add(lambda n: n.text == t, f'text={t!r}')

    def text_contains(self, sub: str) -> 'Matcher':
        return self._add(lambda n: sub in n.text, f'text~{sub!r}')

    def text_matches(self, pattern: str, flags: int = 0) -> 'Matcher':
        rx = re.compile(pattern, flags)
        return self._add(lambda n: bool(rx.search(n.text)),
                         f'text/{pattern}/')

    def text_in(self, options: Sequence[str]) -> 'Matcher':
        s = set(options)
        return self._add(lambda n: n.text in s, f'text in {list(options)}')

    def descr_contains(self, sub: str) -> 'Matcher':
        return self._add(lambda n: sub in n.descr, f'descr~{sub!r}')

    def label_contains(self, sub: str) -> 'Matcher':
        """在 text / id / descr / hint 任一字段中查找子串。

        注意：这里必须逐字段做**子串**判断，不能写成
        `sub in (n.text, n.id, ...)` —— 那是元组的精确相等比较，
        语义完全不对。
        """
        def pred(n: LayoutNode) -> bool:
            return any(sub in f for f in (n.text, n.id, n.descr, n.hint))
        return self._add(pred, f'label~{sub!r}')

    def clickable(self, v: bool = True) -> 'Matcher':
        return self._add(lambda n: n.clickable == v, f'clickable={v}')

    def visible(self, v: bool = True) -> 'Matcher':
        return self._add(lambda n: n.visible == v, f'visible={v}')

    def enabled(self, v: bool = True) -> 'Matcher':
        return self._add(lambda n: n.enabled == v, f'enabled={v}')

    def scrollable(self, v: bool = True) -> 'Matcher':
        return self._add(lambda n: n.scrollable == v, f'scrollable={v}')

    def interactive(self) -> 'Matcher':
        return self._add(lambda n: n.is_interactive(), 'interactive')

    def size_at_least(self, w: int = 0, h: int = 0) -> 'Matcher':
        """触控热区不小于给定尺寸 —— 用于筛掉过小目标。"""
        return self._add(lambda n: n.rect.width >= w and n.rect.height >= h,
                         f'size>={w}x{h}')

    def area_in(self, rect: Rect) -> 'Matcher':
        """控件中心点落在指定屏幕区域内。"""
        return self._add(lambda n: rect.contains(*n.rect.center),
                         f'center in {rect.to_dict()}')

    def custom(self, pred: Callable[[LayoutNode], bool], name: str = 'custom') -> 'Matcher':
        return self._add(pred, name)

    # -------------------------------------------------------- 限定与选择

    def within(self, other: 'Matcher') -> 'Matcher':
        """限定在 other 匹配到的节点子树内查找。"""
        return self._copy(within=other)

    def nth(self, index: int) -> 'Matcher':
        """取第 n 个命中项（0 基）。负数表示从后往前。"""
        return self._copy(index=index)

    # -------------------------------------------------------- 匹配

    def match(self, node: LayoutNode) -> bool:
        return all(p(node) for p in self._preds)

    def filter(self, nodes: Sequence[LayoutNode]) -> List[LayoutNode]:
        """在给定节点集合上应用匹配条件（含 within 子树限定）。"""
        pool = list(nodes)

        if self._within is not None:
            containers = [n for n in pool if self._within.match(n)]
            scoped: List[LayoutNode] = []
            for c in containers:
                scoped.extend(c.walk())
            pool = scoped or []

        hits = [n for n in pool if self.match(n)]

        if self._index is not None:
            try:
                pick = hits[self._index]
                return [pick]
            except IndexError:
                return []
        return hits

    def __str__(self) -> str:
        s = ' & '.join(self._desc) if self._desc else '<empty>'
        if self._within is not None:
            s += f' within({self._within})'
        if self._index is not None:
            s += f'.nth({self._index})'
        return s

    __repr__ = __str__


# ---------------------------------------------------------------- 入口

class _ONFactory:
    """匹配器工厂，提供 ON.text(...) 这类简洁写法。"""

    @staticmethod
    def text(t: str) -> Matcher:
        return Matcher().text(t)

    @staticmethod
    def text_contains(sub: str) -> Matcher:
        return Matcher().text_contains(sub)

    @staticmethod
    def text_matches(pattern: str, flags: int = 0) -> Matcher:
        return Matcher().text_matches(pattern, flags)

    @staticmethod
    def text_in(options: Sequence[str]) -> Matcher:
        return Matcher().text_in(options)

    @staticmethod
    def id(i: str, exact: bool = True) -> Matcher:
        return Matcher().id(i, exact)

    @staticmethod
    def type(t: str, exact: bool = True) -> Matcher:
        return Matcher().type(t, exact)

    @staticmethod
    def descr(sub: str) -> Matcher:
        return Matcher().descr_contains(sub)

    @staticmethod
    def label(sub: str) -> Matcher:
        return Matcher().label_contains(sub)

    @staticmethod
    def clickable(v: bool = True) -> Matcher:
        return Matcher().clickable(v)

    @staticmethod
    def scrollable(v: bool = True) -> Matcher:
        return Matcher().scrollable(v)

    @staticmethod
    def visible(v: bool = True) -> Matcher:
        return Matcher().visible(v)

    @staticmethod
    def interactive() -> Matcher:
        return Matcher().interactive()

    @staticmethod
    def size_at_least(w: int = 0, h: int = 0) -> Matcher:
        return Matcher().size_at_least(w, h)

    @staticmethod
    def all(m: Matcher) -> Matcher:
        """语义别名：强调会返回全部命中项（默认行为）。"""
        return m


ON = _ONFactory()
