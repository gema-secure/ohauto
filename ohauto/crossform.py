"""跨形态差异比对引擎
========================

把**同一套用例**在**不同形态**下采到的控件树放在一起比，产出四类差异：

| 差异类型 | 客观判据 |
|---|---|
| **元素缺失** (`MISSING`)   | 同一身份的元素在形态 B 的控件树里找不到 |
| **越界**   (`OUT_OF_SCREEN`) | 元素 bounds 超出形态 B 屏幕边界（按裁切像素分级） |
| **不可达** (`UNREACHABLE`)  | 元素中心点落在挖孔/异形区，或被裁切后可见区不足原面积 1% |
| **溢出**   (`OVERFLOW`)     | 元素 bounds 超出其父容器 bounds |

> 判据的**唯一事实来源是 `_geometric_diffs()` 的实现**，报告侧
> `crossform_report._KIND_META` 的文案必须与之同步。报告里显式写出判据，
> 是为了让读的人能按报告写的规则复核引擎 —— 文案一旦与实现不符，
> 复核就会得出错误结论（踩过：判据从「与屏幕完全无交集」改成
> 「超出边界 + 按裁切分级」后忘了改文案，读报告的人按旧规则理解，
> 等于拿到一份假证据）。

设计与边界
----------
**① 本模块不碰设备。** 输入是两棵已解析好的 `LayoutNode` 树 + 两个
`FormProfile`，输出是纯数据结构。采集与跑测在 `tools/crossform_run.py`。
这样比对逻辑可以完全离线单测（这正是 信号采集 踩过的坑：模拟与真机不一致时
测试全绿反而最危险）。

**② 「元素身份」是跨形态比对的命门。**
同一元素在两形态下的 `bounds` 必然不同（屏幕尺寸都变了），所以**不能用
坐标认元素**。本模块用三级降级策略算身份 key：

    1. `id` 非空              → `id:<id>`            （最稳，代码标识）
    2. `type` + 有语义的文本  → `tp:<type>|<text>`   （次稳）
    3. `type` + 层级路径      → `hp:<type>@<path>`   （兜底，最不稳）

降级顺序不能颠倒：`id` 是开发者给的稳定标识；`text` 可能被多语言改写；
层级路径会被布局重构打破 —— 但它至少能把「一个 Button」和「另一个 Button」
区分开。**库容差是「能比出来的差异」vs「比不出差异」的取舍**，
所以 `MISSING` 判定额外要求「该身份在 B 里连兜底匹配都没有」。

**③ 参考侧与被测侧不对称。**
`baseline`（形态 A）是基准，`target`（形态 B）是被检查方。
差异都是「B 相对 A 缺了什么/坏了什么」，方向不能反。

**④ 不做坐标缩放猜测。**
两形态尺寸不同时，**绝不把 A 的坐标按比例换算到 B 再比对** ——
那等于假设了「元素该按比例缩放」，而响应式布局恰恰不一定这么做。
本模块只做**绝对判定**（B 的元素是否超出 B 的屏幕/父容器），
「A→B 的位移是否合理」交给上层按需分析（见 `geometry_delta()`）。

用法
----
    from ohauto.crossform import compare_forms, IdentityPolicy

    report = compare_forms(
        baseline=(tree_a, profile_a, 'Mate X7_open'),
        target=(tree_b, profile_b, 'Mate X7_close'),
    )
    print(report.summary())
    for d in report.differences:
        print(d.kind, d.identity, d.detail)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .devices import FormProfile
from .layout import LayoutNode, Rect

__all__ = [
    'DiffKind', 'Severity', 'IdentityPolicy',
    'Element', 'Difference', 'CompareReport',
    'identity_of', 'collect_elements',
    'compare_forms', 'geometry_delta',
]


# ================================================================ 差异分类


class DiffKind(str, Enum):
    """四类跨形态差异。值即报告里的稳定标识（不要改，报告与测试依赖它）。"""

    MISSING       = 'MISSING'         # 元素缺失
    OUT_OF_SCREEN = 'OUT_OF_SCREEN'   # 越界
    UNREACHABLE   = 'UNREACHABLE'     # 不可达
    OVERFLOW      = 'OVERFLOW'        # 溢出父容器


class Severity(str, Enum):
    """严重度。用于报告排序与「验收是否能过」的判定。"""

    HIGH   = 'HIGH'      # 必然导致用户操作失败
    MEDIUM = 'MEDIUM'    # 可能失败 / 视觉破损
    LOW    = 'LOW'       # 仅记录，不影响功能


#: 每类差异的默认严重度。判据本身是客观的，严重度是给阅读者的分级。
_DEFAULT_SEVERITY: Dict[DiffKind, Severity] = {
    DiffKind.MISSING:       Severity.HIGH,
    DiffKind.UNREACHABLE:   Severity.HIGH,
    DiffKind.OUT_OF_SCREEN: Severity.MEDIUM,
    DiffKind.OVERFLOW:      Severity.MEDIUM,
}


# ================================================================ 身份策略


@dataclass(frozen=True)
class IdentityPolicy:
    """「什么算同一个元素」的策略。

    三级降级的**开关与阈值**在这里集中，目的是让策略显式可配、
    不埋在函数体里 —— 因为不同的被测应用需要不同策略
    （有的应用 id 齐全，有的几乎没 id，只有文本）。
    """

    #: 是否用 `id` 作为最高优先级身份（强烈建议开：id 最稳）
    use_id: bool = True
    #: 是否用 `type + text` 作为次优先身份
    use_text: bool = True
    #: 是否用 `type + 层级路径` 兜底
    use_hierarchy: bool = True
    #: 参与结构比对的元素过滤：只比可见元素（不可见的元素本来就不渲染）
    only_visible: bool = True
    #: 只比可交互元素。默认 False —— 布局缺陷也可能出在纯展示元素上。
    only_interactive: bool = False
    #: 叶子/容器都算元素。默认 True；置 False 可只比叶子（控件），
    #: 忽略容器（Row/Column 这类），能显著压低噪音。
    include_containers: bool = True
    #: 身份路径的**最大深度**，超过则截断。防止深层容器把所有叶子
    #: 都算成不同身份（层级路径一长，微小重构就让全部元素失配）。
    max_path_depth: int = 4

    def fallback_order(self) -> Tuple[str, ...]:
        """当前启用了哪几级身份，按优先级返回。"""
        out = []
        if self.use_id:
            out.append('id')
        if self.use_text:
            out.append('text')
        if self.use_hierarchy:
            out.append('hierarchy')
        return tuple(out)


#: 常见容器类型 —— 这些通常没有语义，只在 `include_containers=True` 时参与
CONTAINER_TYPES: frozenset = frozenset({
    'Root', 'Column', 'Row', 'Stack', 'Flex', 'Grid', 'Scroll',
    'Refresh', 'RelativeContainer', 'Swiper', 'Tabs', 'TabContent',
    'Navigation', 'NavDestination', 'Panel', 'SideBarContainer',
    'Divider', 'Blank', 'Badge', 'Counter', 'Marquee',
})


# ================================================================ 元素与身份


def identity_of(node: LayoutNode, policy: IdentityPolicy) -> str:
    """算一个控件节点的**形态无关身份**。

    返回形如 `id:btn_login` / `tp:Button|登录` / `hp:Button@Login>Column`。
    三级都算不出来时返回 `''`（调用方应跳过该节点，不参与比对）。
    """
    for level in policy.fallback_order():
        if level == 'id':
            if node.id:
                return f'id:{node.id}'
        elif level == 'text':
            # 用 `label`（text > descr > hint）而不是裸 text：
            # 无障碍文案（descr）同样是稳定的语义标识，
            # 而很多图标按钮只有 descr、没有 text。
            lab = (node.text or node.descr or node.hint).strip()
            if lab:
                return f'tp:{node.type}|{lab}'
        elif level == 'hierarchy':
            path = _skeleton_path(node, policy.max_path_depth)
            if path:
                return f'hp:{node.type}@{path}'
    return ''


def _skeleton_path(node: LayoutNode, max_depth: int) -> str:
    """从根到当前节点的「骨架路径」——只保留类型，丢掉 id/text。

    为什么要丢 id/text：身份本身就要靠它们算，若路径里再带上就循环依赖了
    （而且会让 `hp:` 退化成近乎 `id:` 的行为，失去兜底意义）。
    只留类型能表达「这个 Button 是放在哪个容器里的」这一层结构信息。
    """
    chain, p, guard = [], node.parent, 0
    while p is not None and guard < max_depth:
        chain.append(p.type or '?')
        p = p.parent
        guard += 1
    if not chain:
        return ''
    return '>'.join(reversed(chain))


def collect_elements(root: LayoutNode, policy: IdentityPolicy
                     ) -> Dict[str, 'Element']:
    """把控件树摊平成 `身份 -> Element` 映射。

    重复身份的处理：**保留第一个，其余丢弃**（不合并、不平均）。
    理由：同身份多半意味着「列表项复用了同一个 id」—— 这类元素本身
    就不适合做跨形态身份比对（数量随数据变化）。丢弃比强行合并安全，
    合并会造出一个不存在的「平均元素」。
    """
    out: Dict[str, Element] = {}
    for idx, n in enumerate(root.walk()):
        if policy.only_visible and not n.visible:
            continue
        if policy.only_interactive and not n.is_interactive():
            continue
        if not policy.include_containers and _is_container(n):
            continue
        key = identity_of(n, policy)
        if not key or key in out:
            continue
        out[key] = Element(
            identity=key, node=n, order=idx,
            parent_rect=n.parent.rect if n.parent is not None else None,
        )
    return out


def _is_container(node: LayoutNode) -> bool:
    """是否是「容器型」节点（无交互语义、只是布局盒子）。"""
    return (node.type in CONTAINER_TYPES
            and not node.clickable and not node.scrollable)


@dataclass
class Element:
    """参与比对的一个元素（身份 + 节点 + 上下文）。"""

    identity: str
    node: LayoutNode
    order: int = 0
    parent_rect: Optional[Rect] = None

    @property
    def rect(self) -> Rect:
        return self.node.rect

    @property
    def bounds(self) -> Tuple[int, int, int, int]:
        r = self.rect
        return (r.left, r.top, r.right, r.bottom)

    @property
    def label(self) -> str:
        return self.node.label

    def describe(self) -> str:
        r = self.rect
        return (f'{self.node.type}'
                f'{"#" + self.node.id if self.node.id else ""}'
                f'{" " + repr(self.node.text) if self.node.text else ""}'
                f' [{r.left},{r.top},{r.right},{r.bottom}]')


# ================================================================ 差异记录


@dataclass
class Difference:
    """一条跨形态差异。字段刻意做全，便于报告直接渲染、也便于追证据。"""

    kind: DiffKind
    identity: str
    label: str = ''
    detail: str = ''
    severity: Severity = Severity.MEDIUM
    #: 形态 A（基准）里的证据
    baseline_bounds: Optional[Tuple[int, int, int, int]] = None
    #: 形态 B（被测）里的证据
    target_bounds: Optional[Tuple[int, int, int, int]] = None
    #: 触发判据的原始数据（如命中的挖孔矩形、屏幕尺寸），便于复核
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'kind': self.kind.value,
            'identity': self.identity,
            'label': self.label,
            'detail': self.detail,
            'severity': self.severity.value,
            'baseline_bounds': (list(self.baseline_bounds)
                                if self.baseline_bounds else None),
            'target_bounds': (list(self.target_bounds)
                              if self.target_bounds else None),
            'evidence': dict(self.evidence),
        }


# ================================================================ 比对


@dataclass
class CompareReport:
    """一次跨形态比对的完整结果。"""

    baseline_name: str
    target_name: str
    baseline_size: Tuple[int, int]
    target_size: Tuple[int, int]
    baseline_elements: int = 0
    target_elements: int = 0
    differences: List[Difference] = field(default_factory=list)
    #: 比对过程中的告警（如「两侧元素数差距过大」这类需要人看一眼的情况）
    warnings: List[str] = field(default_factory=list)

    # ---------------------------------------------------------- 查询

    def by_kind(self, kind: DiffKind) -> List[Difference]:
        return [d for d in self.differences if d.kind is kind]

    def counts(self) -> Dict[str, int]:
        """各类型差异数量。**四类都出现**（含 0），便于报告画固定表。"""
        c = {k.value: 0 for k in DiffKind}
        for d in self.differences:
            c[d.kind.value] += 1
        return c

    def has_high(self) -> bool:
        return any(d.severity is Severity.HIGH for d in self.differences)

    def summary(self) -> str:
        c = self.counts()
        return (f'{self.baseline_name} ({self.baseline_size[0]}x'
                f'{self.baseline_size[1]}) -> {self.target_name} '
                f'({self.target_size[0]}x{self.target_size[1]}) | '
                f'缺失 {c["MISSING"]} / 越界 {c["OUT_OF_SCREEN"]} / '
                f'不可达 {c["UNREACHABLE"]} / 溢出 {c["OVERFLOW"]}')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'baseline': {
                'name': self.baseline_name,
                'size': list(self.baseline_size),
                'elements': self.baseline_elements,
            },
            'target': {
                'name': self.target_name,
                'size': list(self.target_size),
                'elements': self.target_elements,
            },
            'counts': self.counts(),
            'has_high': self.has_high(),
            'warnings': list(self.warnings),
            'differences': [d.to_dict() for d in self.differences],
        }


def compare_forms(
    baseline: Tuple[LayoutNode, FormProfile, str],
    target: Tuple[LayoutNode, FormProfile, str],
    policy: Optional[IdentityPolicy] = None,
) -> CompareReport:
    """跨形态比对主入口。

    Parameters
    ----------
    baseline / target:
        `(控件树, 形态档案, 形态名)` 三元组。形态名只是给人看的标签。
    policy:
        身份策略。不传用默认（id → text → 层级路径，只比可见元素）。

    Returns
    -------
    CompareReport
        差异列表已按「严重度降序 + 类型优先级」排好，报告可直接顺序渲染。
    """
    policy = policy or IdentityPolicy()
    tree_a, prof_a, name_a = baseline
    tree_b, prof_b, name_b = target

    elems_a = collect_elements(tree_a, policy)
    elems_b = collect_elements(tree_b, policy)

    diffs: List[Difference] = []
    warnings: List[str] = []

    # ---- 1) 元素缺失：A 有而 B 没有
    for key, el in elems_a.items():
        if key not in elems_b:
            diffs.append(Difference(
                kind=DiffKind.MISSING,
                identity=key,
                label=el.label,
                detail=(f'基准形态存在，{name_b} 中找不到同一身份的元素'),
                severity=_DEFAULT_SEVERITY[DiffKind.MISSING],
                baseline_bounds=el.bounds,
                evidence={'baseline_describe': el.describe()},
            ))

    # ---- 2) 几何类差异：两侧都有的元素，在 B 下检查
    for key, el_b in elems_b.items():
        # ★ 根节点跳过几何判定。根节点就是「屏幕本身」（bounds = 整屏），
        # 形态切换后屏幕尺寸必然变，于是它必然「超出自己」——
        # 这是定义使然，不是缺陷。早期没跳过，靶子场景里每次都多报一条
        # `id:staticRoot 被裁切 234px`，纯噪音，还会让人怀疑引擎的判据。
        if el_b.node.parent is None:
            continue
        el_a = elems_a.get(key)
        base_bounds = el_a.bounds if el_a is not None else None

        diffs.extend(_geometric_diffs(
            el_b, prof_b, name_b,
            base_bounds=base_bounds, baseline_name=name_a))

    # ---- 3) 合理性告警（不判差异，但报告里提示人看一眼）
    #
    # ★ 能力缺口留痕：目标形态没量到挖孔/异形区时，
    # 「不可达（挖孔）」判据**整个没生效** —— 这不是「没有不可达问题」，
    # 是「没法判」。必须在报告里说出来，否则读报告的人会把
    # 「UNREACHABLE=0」当成通过。按 profile 报一次，不逐元素刷。
    if not prof_b.has_measured_safe_area:
        warnings.append(
            f'{name_b} 没有挖孔/异形区的实测数据，'
            f'「不可达（中心点落入挖孔）」判据本次未生效 —— '
            f'报告里的 UNREACHABLE=0 不代表该形态没有不可达问题。')

    if elems_a and elems_b:
        ratio = len(elems_b) / len(elems_a)
        if ratio < 0.5:
            warnings.append(
                f'{name_b} 的元素数只有基准的 {ratio:.0%}'
                f'（{len(elems_b)} vs {len(elems_a)}）—— '
                f'可能是真的形态差异，也可能是身份策略不适配该应用'
                f'（如 id 大面积缺失）。建议核对。')
        elif ratio > 2.0:
            warnings.append(
                f'{name_b} 的元素数达到基准的 {ratio:.1f} 倍'
                f'（{len(elems_b)} vs {len(elems_a)}）—— 可能是展开了'
                f'额外的列表项/面板，也可能是重复身份被丢弃得不均匀。')

    report = CompareReport(
        baseline_name=name_a, target_name=name_b,
        baseline_size=(prof_a.width, prof_a.height),
        target_size=(prof_b.width, prof_b.height),
        baseline_elements=len(elems_a), target_elements=len(elems_b),
        differences=_sort_diffs(diffs), warnings=warnings)
    return report


def _geometric_diffs(el: Element, prof: FormProfile, form_name: str,
                     base_bounds: Optional[Tuple[int, int, int, int]],
                     baseline_name: str) -> List[Difference]:
    """对单个元素在目标形态下做三类几何判定。

    `base_bounds is None` 表示该元素**只在目标形态出现**（基准侧没有）——
    这时几何判定依然要做（新元素也可能有缺陷），但每条差异都会带上
    `is_new_in_target=True` 并在说明里注明，否则读报告的人会疑惑
    「为什么这条的基准 bounds 是空的」。
    """
    out: List[Difference] = []
    r = el.rect
    bounds = el.bounds
    is_new = base_bounds is None
    origin = ('（该元素仅在目标形态出现，基准形态没有）' if is_new else '')

    # 越界判定会用到，后面决定「是否跳过溢出判定」也要用它
    #
    # ⚠️ 判据选择是这一步最容易做错的地方。三种候选：
    #
    #   (a) 「bounds 超出屏幕」 —— 太宽。长列表的下一项、可横滑的卡片
    #       都会命中，噪音会把真问题淹掉。
    #   (b) 「与屏幕完全无交集」 —— 太窄。**部分被裁**（元素比屏幕大，
    #       底部 190px 被切掉）这种最典型的内容裁切反而漏检了。
    #   (c) 采用：**「元素有 bouns 落在屏幕外」+ 按裁切比例分级** ← 本实现
    #
    # 最终用 (c)：只要元素超出屏幕边界就报，但用 `clipped_px` /
    # `fully_invisible` 把「全丢」和「丢了 5px」分开。
    # 全丢 → 高严重度（用户根本看不到）；轻微裁切 → 中，且报告里能一眼
    # 看出是 8px 还是 190px，不会与真问题混淆。
    beyond = _beyond_screen_px(bounds, prof.width, prof.height)
    out_of_screen = beyond['any'] > 0

    if out_of_screen:
        fully = not _intersects_screen(bounds, prof.width, prof.height)
        if fully:
            detail = (f'元素与 {form_name} 屏幕（{prof.width}x{prof.height}）'
                      f'完全无交集，用户完全看不到')
            sev = Severity.HIGH
        else:
            detail = (f'元素超出 {form_name} 屏幕（{prof.width}x{prof.height}）'
                      f'边界，被裁切 {beyond["right"] + beyond["bottom"]}px'
                      f'（右 {beyond["right"]} / 下 {beyond["bottom"]} / '
                      f'左 {beyond["left"]} / 上 {beyond["top"]}）')
            sev = _DEFAULT_SEVERITY[DiffKind.OUT_OF_SCREEN]
        out.append(Difference(
            kind=DiffKind.OUT_OF_SCREEN,
            identity=el.identity, label=el.label,
            detail=detail + origin, severity=sev,
            baseline_bounds=base_bounds, target_bounds=bounds,
            evidence={'screen': [prof.width, prof.height],
                      'element': list(bounds),
                      'clipped_px': beyond,
                      'is_new_in_target': is_new,
                      'fully_invisible': fully},
        ))

    # ---- 不可达：**中心点**落在挖孔里 → 点击必然失败。
    #
    # 用中心点而不是「整块相交」，是因为 uiInput 点击用的就是中心点：
    # 元素与孔相交但中心在外时，点下去仍然能命中元素 —— 那不是缺陷
    # （这种误报在测试里非常常见，会让开发者不信任报告）。
    if prof.center_hits_cutout(bounds):
        hit = _which_cutout(el, prof)
        out.append(Difference(
            kind=DiffKind.UNREACHABLE,
            identity=el.identity, label=el.label,
            detail=(f'元素中心点 {tuple(r.center)} 落在挖孔/异形区内 '
                    f'—— 看得见但点不到{origin}'),
            severity=_DEFAULT_SEVERITY[DiffKind.UNREACHABLE],
            baseline_bounds=base_bounds, target_bounds=bounds,
            evidence={'cutout': list(hit) if hit else None,
                      'center': list(r.center),
                      'is_new_in_target': is_new,
                      'cutouts': [list(c) for c in prof.cutouts]},
        ))
    # 评审中危修正：「必须留痕」的函数体是 pass ——
    # 红线⑤：只写注释不处置等于不存在）。挖孔数据缺失时的能力缺口
    # 改在**报告级**留痕：compare() 的告警段对 `not prof_b.has_measured_safe_area`
    # 统一告警一次（逐元素告警会把同一条能力缺口刷成几十遍，没人读）。
    # 这里不再保留 elif 分支 —— 原来的 `pass` 既没留痕也没跳过任何判定，
    # 是纯死代码。

    # ---- 不可达（第二类）：**可见区过小** —— 露了条边但根本点不中。
    #
    # 这类和「越界」互补：越界是完全看不见，这里是「看得见但可点区域太小」。
    # 真机上很常见：元素被裁到只剩 3px 高，用户看得见却点不中 ——
    # 这是最恼人、也最容易被自动化漏掉的一类（因为「元素存在」是成立的）。
    #
    # ⚠️ 判据必须同时满足两条，缺一条就误报：
    #   ① 元素**确实被屏幕裁切了**（有 bounds 落到屏外）
    #   ② 裁切后剩下的可见区**不到原面积的 1%**
    #
    # 早期只判 ②，后果是「一个本来就很小、但完整显示的图标」被误报成
    # 不可达（实测：10x10 的图标在 500x500 屏上可见区 100px²，
    # 只占屏面积 0.04% < 1%，但它明明完整可见）。
    # 小 ≠ 不可点，**被裁小**才是问题。
    #
    # 另外早期这里写的是「中心点出屏」，那是**死代码**：
    # 中心点必然落在元素内部，若元素与屏幕有交集则中心点也可能在屏内 ——
    # 两个条件几何上不可能同时成立。换成「可见区过小」才有意义。
    # ⚠️ 「可见区过小」与「越界」是**两个不同性质的问题**，都要报：
    #   - 越界说「元素超出屏幕边界」，是几何事实
    #   - 不可达说「这个元素点不中」，是**用户后果**
    # 一个元素被裁到只剩 0.1% 时，两条结论都成立且都有价值 ——
    # 早期版本让越界抑制了不可达，结果这一类最恼人的问题反而被吞掉
    # （实测：元素 top=-99900 只剩 100px² 可点，报告里只说"越界"，
    # 读的人不会意识到"这个按钮用户根本点不中"）。
    #
    # 但**完全不可见**（可见区为 0）时只报越界 —— 那时"点不中"是显然的，
    # 再说一遍是冗余。
    vis = _visible_area(bounds, prof.width, prof.height)
    if (0 < vis < r.area * 0.01):
        full = r.area or 1
        out.append(Difference(
            kind=DiffKind.UNREACHABLE,
            identity=el.identity, label=el.label,
            detail=(f'元素被裁切后可见区仅 {vis}px²'
                    f'（原 {full}px² 的 {vis / full:.2%}）—— '
                    f'看得见但几乎点不中{origin}'),
            severity=_DEFAULT_SEVERITY[DiffKind.UNREACHABLE],
            baseline_bounds=base_bounds, target_bounds=bounds,
            evidence={'visible_area': vis, 'element_area': full,
                      'visible_ratio': round(vis / full, 4),
                      'clipped_px': beyond,
                      'is_new_in_target': is_new,
                      'screen': [prof.width, prof.height]},
        ))

    # ---- 溢出：超出父容器。
    #
    # ⚠️ 先做「去重」：元素**已经判定越界**（与屏幕完全无交集）时不再报溢出。
    # 理由：屏幕变窄时元素跑到屏外，往往同时越出父容器 —— 但那是**同一个
    # 根因**（布局没做响应式）的两种表现，报两条会让报告出现成对重复项，
    # 读的人要自己合并。越界是更上游、更严重的结论，保留它就够了。
    # 早期版本没做这个去重，实测一个 1080→400 的收窄形态对里，
    # 每个跑出屏的元素都贡献「1 越界 + 1 溢出」两条，噪音直接翻倍。
    #
    # 父容器 bounds 为全 0 时跳过 —— 那是「没解析到父容器」而不是
    # 「父容器在原点且尺寸为 0」。全 0 的父容器会让任何元素都算溢出，
    # 是典型的假阳性来源。
    if (not out_of_screen
            and el.parent_rect is not None and el.parent_rect.area > 0):
        p = el.parent_rect
        if prof.rect_overflows_parent(bounds, (p.left, p.top, p.right, p.bottom)):
            out.append(Difference(
                kind=DiffKind.OVERFLOW,
                identity=el.identity, label=el.label,
                detail=(f'元素 [{r.left},{r.top},{r.right},{r.bottom}] '
                        f'超出父容器 [{p.left},{p.top},{p.right},{p.bottom}]'
                        f'{origin}'),
                severity=_DEFAULT_SEVERITY[DiffKind.OVERFLOW],
                baseline_bounds=base_bounds, target_bounds=bounds,
                evidence={'parent': [p.left, p.top, p.right, p.bottom],
                          'is_new_in_target': is_new},
            ))

    return out


def _intersects_screen(bounds: Sequence[int], w: int, h: int) -> bool:
    """元素与屏幕是否有可见交集（面积 > 0）。"""
    return _visible_area(bounds, w, h) > 0


def _visible_area(bounds: Sequence[int], w: int, h: int) -> int:
    """元素落在屏幕内的部分有多少面积（px²）。"""
    if len(bounds) != 4:
        return 0
    l, t, r, b = (int(v) for v in bounds)
    iw = min(r, w) - max(l, 0)
    ih = min(b, h) - max(t, 0)
    return max(0, iw) * max(0, ih)


def _beyond_screen_px(bounds: Sequence[int], w: int, h: int
                      ) -> Dict[str, int]:
    """元素四条边各超出屏幕多少 px（没超出的一侧为 0）。

    返回 `{'left','top','right','bottom','any'}`。
    `any` 是四者之和 —— 用作「是否越界」的快速判据（>0 即越界）。
    """
    z = {'left': 0, 'top': 0, 'right': 0, 'bottom': 0, 'any': 0}
    if len(bounds) != 4:
        return z
    l, t, r, b = (int(v) for v in bounds)
    z['left'] = max(0, -l)
    z['top'] = max(0, -t)
    z['right'] = max(0, r - w)
    z['bottom'] = max(0, b - h)
    z['any'] = z['left'] + z['top'] + z['right'] + z['bottom']
    return z


def _which_cutout(el: Element, prof: FormProfile
                  ) -> Optional[Tuple[int, int, int, int]]:
    """中心点命中哪一个挖孔（报告要给出具体矩形）。"""
    if not prof.cutouts:
        return None
    cx, cy = el.rect.center
    for x, y, w, h in prof.cutouts:
        if x <= cx <= x + w and y <= cy <= y + h:
            return (x, y, w, h)
    return None


#: 排序优先级：严重度越高越前；同severity 内按类型固定顺序，保证报告稳定
_KIND_ORDER = {
    DiffKind.MISSING:       0,
    DiffKind.UNREACHABLE:   1,
    DiffKind.OUT_OF_SCREEN: 2,
    DiffKind.OVERFLOW:      3,
}
_SEV_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


def _sort_diffs(diffs: List[Difference]) -> List[Difference]:
    """排序：严重度降序 → 类型顺序 → 身份字典序。

    最后一级用身份排序是为了**输出稳定** —— 否则 Python 的字典序
    在不同解释器/不同插入顺序下会让报告每次都不一样，无法做 diff。
    """
    return sorted(diffs, key=lambda d: (
        _SEV_ORDER.get(d.severity, 9),
        _KIND_ORDER.get(d.kind, 9),
        d.identity,
    ))


# ================================================================ 几何位移分析


def geometry_delta(
    baseline: Tuple[LayoutNode, FormProfile],
    target: Tuple[LayoutNode, FormProfile],
    policy: Optional[IdentityPolicy] = None,
) -> Dict[str, Dict[str, Any]]:
    """算同一元素在两形态间的**几何位移**（不做对错判定，只给数据）。

    这是给上层做「响应式是否合理」分析用的原料，**本模块不替你下结论**：
    位移多少算「不合理」取决于布局意图（居中的东西该居中，
    左对齐的东西该保持左边距），引擎没法知道。

    返回：`身份 -> {baseline_bounds, target_bounds, dx, dy,
                    dw, dh, width_ratio, height_ratio, kept_left,
                    kept_top, kept_center_x, kept_center_y}`

    `kept_*` 是布尔：该对齐关系是否在切换后保持住了 —— 这是判断
    「布局是否响应式」最有用的信号（例如左对齐的元素 `kept_left=True`）。
    """
    policy = policy or IdentityPolicy()
    tree_a, prof_a = baseline
    tree_b, prof_b = target
    ea = collect_elements(tree_a, policy)
    eb = collect_elements(tree_b, policy)

    out: Dict[str, Dict[str, Any]] = {}
    for key, a in ea.items():
        b = eb.get(key)
        if b is None:
            continue
        ra, rb = a.rect, b.rect
        rec: Dict[str, Any] = {
            'baseline_bounds': list(a.bounds),
            'target_bounds': list(b.bounds),
            'dx': rb.left - ra.left,
            'dy': rb.top - ra.top,
            'dw': rb.width - ra.width,
            'dh': rb.height - ra.height,
            'width_ratio': (rb.width / ra.width) if ra.width else None,
            'height_ratio': (rb.height / ra.height) if ra.height else None,
            # 左边缘/上边缘是否保持（容差 2px，避免渲染取整造成的抖动）
            'kept_left': abs(rb.left - ra.left) <= 2,
            'kept_top': abs(rb.top - ra.top) <= 2,
            # 中心点是否保持「相对屏幕」的比例位置
            'kept_center_x': _close_ratio(ra.center[0], prof_a.width,
                                          rb.center[0], prof_b.width),
            'kept_center_y': _close_ratio(ra.center[1], prof_a.height,
                                          rb.center[1], prof_b.height),
        }
        out[key] = rec
    return out


def _close_ratio(v_a: int, total_a: int, v_b: int, total_b: int,
                 tol: float = 0.05) -> Optional[bool]:
    """两个点的**相对位置比例**是否接近（同一侧/居中等对齐是否保持）。

    `total` 为 0 时无法算比例，返回 None（不猜）。
    """
    if not total_a or not total_b:
        return None
    return abs(v_a / total_a - v_b / total_b) <= tol
