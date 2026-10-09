"""
L4 应用层 —— 自动探索与页面状态图
=================================

让 Driver 自己「点着玩」，把应用的可达页面与跳转关系摸出来，
形成页面状态图，并把探索轨迹沉淀成可重放的回归脚本。

安全设计（真机上非常关键）：
    探索会真的点击 UI，因此内置**危险控件黑名单**。默认拦截含
    「删除 / 支付 / 退出 / 注销 / 卸载 / 格式化」等语义的控件，
    避免自动探索造成不可逆影响。可通过 allow_dangerous=True 放开。

B1/B2 改造（2026-09-21）：
    ★ 页面双签名    content_sig 精确去重 / structural_sig 返回栈回溯
    ★ 覆盖度度量    已访可交互 / 全部可交互，按**结构签名**归并页面
    ★ 四档优先级    弹窗内控件置顶；危险控件默认拦截，放开后置顶
    ★ Budget        一次探索的资源上限，替代散落的三个参数
    ★ Tarpit        防粘滞（2026-09-27，借鉴 HapTest --simk）：
                    「点一下、文案变一点」的页面不再把 BFS 队列灌满

    from ohauto.explorer import Explorer, Budget

    ex = Explorer(driver)
    graph = ex.explore(max_pages=8, budget=Budget(max_pages=8, max_seconds=300))
    ex.save_graph('graph.json')
    print(ex.coverage.ratio)          # 覆盖度
    print(ex.to_mermaid())
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .driver import Driver, DriverError
from .layout import LayoutNode, flatten, Rect  # noqa: E402
from .permission import (POLICY_ALLOW, POLICY_DENY,  # noqa: E402
                         PermissionDialogVerdict, detect_permission_dialog,
                         resolve_policy)


# ---------------------------------------------------------------- 安全策略

DEFAULT_DENY_PATTERNS = [
    r'删除', r'移除', r'清除', r'清空', r'格式化', r'重置',
    r'支付', r'付款', r'购买', r'下单', r'充值', r'转账',
    r'退出登录', r'注销', r'登出', r'解绑',
    r'卸载', r'停止', r'关闭账号', r'取消订单',
    r'delete', r'remove', r'pay', r'purchase', r'logout',
    r'sign\s*out', r'uninstall', r'reset', r'format',
]


def _matches_danger(node: LayoutNode, patterns: Sequence[str]) -> Tuple[bool, str]:
    """「这个控件语义上是不是危险控件」—— 与策略是否放行无关。

    刻意独立成函数：`allow_dangerous=True` 时控件**仍然**是危险控件，
    只是策略允许点它（此时要置顶到 URGENT）。如果把「危险」和「是否放行」
    混在一个判断里，放开之后这些控件就再也识别不出来了。
    """
    blob = f'{node.text} {node.id} {node.descr} {node.hint}'
    for p in patterns:
        if re.search(p, blob, re.I):
            return True, p
    return False, ''


@dataclass
class SafetyPolicy:
    """探索期安全策略。"""
    deny_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_DENY_PATTERNS))
    allow_dangerous: bool = False
    max_text_len: int = 40          # 排除超长文本块（多为正文而非按钮）

    def is_dangerous(self, node: LayoutNode) -> Tuple[bool, str]:
        if self.allow_dangerous:
            return False, ''
        return _matches_danger(node, self.deny_patterns)

    def is_clickable_candidate(self, node: LayoutNode) -> Tuple[bool, str]:
        if not node.visible:
            return False, 'invisible'
        if not node.enabled:
            return False, 'disabled'
        if node.rect.width < 20 or node.rect.height < 20:
            return False, 'too-small'
        if node.type in ('Text', 'Image', 'Divider', 'Blank', 'Stack'):
            if not node.clickable:
                return False, f'passive-{node.type}'
        if len(node.text) > self.max_text_len:
            return False, 'text-too-long'
        bad, pat = self.is_dangerous(node)
        if bad:
            return False, f'dangerous({pat})'
        if not (node.clickable or node.is_interactive()):
            return False, 'not-interactive'
        return True, ''


# ================================================================ ★ 页面双签名
#
# 为什么必须要两个签名（任务卡 B1 补注）：
#   页面里的文案会随数据变化（列表首项、未读计数、时间戳）。只用内容签名，
#   探索器会把同一个页面反复「发现」成新页面 —— 覆盖率统计虚高、还会绕圈。
#   结构签名只看控件类型序列，文案怎么变都不影响。
#
#   反过来也不能只用结构签名：两个结构相同但语义不同的页面会被当成同一页，
#   页面状态图直接塌掉。所以：内容签名管「这页我访问过没有」，
#   结构签名管「返回后是不是回到了原页」。

# 节点自带的页面归属字段（真机实测有，见 C1-真机适配报告）
PAGE_ATTR_KEYS = ('pagePath', 'PagePath', 'page_path', 'page', 'routerPath')
ABILITY_ATTR_KEYS = ('abilityName', 'AbilityName', 'ability', 'abilityInfo')
BUNDLE_ATTR_KEYS = ('bundleName', 'BundleName', 'bundle')


def _digest(text: str) -> str:
    """签名的哈希口径。sha1 而非 md5，避免有人对「指纹」二字有意见。"""
    return hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()


def _first_attr(root: Optional[LayoutNode], keys: Sequence[str]) -> str:
    """在树里找第一个带该属性的节点取值（真机是节点级字段，不一定挂在根上）。"""
    if root is None:
        return ''
    for n in root.walk():
        for k in keys:
            v = n.attributes.get(k)
            if v not in (None, ''):
                return str(v)
    return ''


@dataclass
class PageSignature:
    """一个页面的双签名。

    content    精确签名：类型 / id / 文案 / 尺寸全参与 —— 用于「这页访问过没有」
    structural 宽容签名：只取控件类型序列 —— 用于「返回后是否回到原页」

    bundle / ability / page_path 参与两个签名：三者任一不同就是不同页面，
    哪怕控件树长得一模一样（真机上切换 ability 会出现这种情况）。
    """
    content: str
    structural: str
    bundle: str = ''
    ability: str = ''
    page_path: str = ''
    nodes: int = 0

    @property
    def content_key(self) -> str:
        return _digest(f'{self.bundle}|{self.ability}|{self.page_path}|{self.content}')

    @property
    def structural_key(self) -> str:
        return _digest(f'{self.bundle}|{self.ability}|{self.page_path}|{self.structural}')

    def to_dict(self) -> Dict[str, Any]:
        return {'content_key': self.content_key, 'structural_key': self.structural_key,
                'bundle': self.bundle, 'ability': self.ability,
                'page_path': self.page_path, 'nodes': self.nodes}


def build_page_signature(root: LayoutNode, bundle: str = '',
                         ability: str = '') -> PageSignature:
    """从控件树算双签名。

    真机优先用节点自带的 `pagePath` / `abilityName` / `bundleName`（C1 报告实测存在），
    取不到才回落到 Driver 上配置的 bundle / ability。
    """
    ctx_bundle = _first_attr(root, BUNDLE_ATTR_KEYS) or bundle or ''
    ctx_ability = _first_attr(root, ABILITY_ATTR_KEYS) or ability or ''
    ctx_path = _first_attr(root, PAGE_ATTR_KEYS)

    content_parts: List[str] = []
    struct_parts: List[str] = []
    n_visible = 0
    for n in flatten(root, only_visible=True):
        if n.rect.area <= 0:
            continue
        n_visible += 1
        content_parts.append(f'{n.type}|{n.id}|{n.text}|{n.descr}'
                             f'|{n.rect.width}x{n.rect.height}')
        struct_parts.append(n.type)

    return PageSignature(
        content=';'.join(content_parts),
        structural=','.join(struct_parts),
        bundle=ctx_bundle, ability=ctx_ability, page_path=ctx_path,
        nodes=n_visible,
    )


# 控件身份指纹已下沉到 .identity（结构债 S7：locator L3 不再反向 import 本模块）。
# 这里 re-export 是为兼容 explorer 内部调用与既有 `from ohauto.explorer import ...`。
from .identity import control_key, type_fingerprint


# ================================================================ ★ 优先级
#
# 任务卡给的是固定档位，不是「权重」—— 固定值才好测试、好解释、好回归。
# 卡里叫「四档」但列了 5 个取值，这里按列出的取值实现。

class Priority:
    URGENT = 100    # 危险控件（删除/支付/注销）—— 默认拦截，放开后置顶
    HIGH = 80       # 弹窗内可交互控件；含「登录/搜索/详情/提交」等关键词的控件
    MEDIUM = 40     # 普通可交互控件
    NORMAL = 20     # 其他
    LOW = 10        # 已访问过且无状态变化的控件（兜底）


HIGH_KEYWORDS = ('登录', '登陆', '搜索', '查询', '详情', '提交', '注册',
                 '确认', '确定', '下一步', '开始', '进入', '查看',
                 'login', 'sign in', 'search', 'submit', 'detail')

# 弹窗识别线索。真机实测可用的字段见 C1-真机适配报告：
# hostWindowId（多窗口/弹窗）、zIndex / opacity（遮挡）、hitTestBehavior（点不动）。
DIALOG_TYPE_HINTS = ('dialog', 'popup', 'modal', 'sheet', 'alert', 'picker',
                     'menu', 'overlay', 'toast')
WINDOW_ID_KEYS = ('hostWindowId', 'windowId', 'HostWindowId', 'WindowId')

#: 交互可用判定的不透明度下限。**全项目只留这一个小数** ——
#: `detect_dialog` 的遮挡线引用它，`diagnose._usability` 也引它。
#: 0.9 的半透明照样能点，判死是过度归因（C 复核 `diagnose` 那条时的同源问题）。
MIN_INTERACTIVE_OPACITY = 0.5

#: 「多窗口叠加」判为弹窗时，两窗重叠面积至少要占屏幕的多少。
#:
#: ★ 这个数字不是拍的，是拿真机样本量出来的（2026-09-23，C 给的
#:   `datasets/gallery_13app/`，DAYU200 720×1280）：
#:
#:     状态栏窗口  hostWindowId=7  bbox [0,0][720,72]    52 节点
#:     系统窗口    hostWindowId=9  bbox [0,0][720,32]     3 节点
#:     导航栏窗口  hostWindowId=8  bbox [0,1208][720,1280] 18 节点
#:     被测应用    hostWindowId=<各自>  bbox [0,72][720,1208]
#:
#:   真机 `dumpLayout` 返回的是**整个窗口栈**，所以「≥2 个 hostWindowId」
#:   **恒为真** —— 而状态栏(7) 与系统窗(9) 还是互相包含的，几何上也算「重叠」。
#:   它们重叠 23040 px² = 屏幕的 **2.5%**：那是「把屏幕切开」，不是「压在应用上」。
#:   而真弹窗会盖住应用内容区，重叠 ≥ 9%。
#:   阈值取 5% 正好把系统条放过去、把真弹窗留下来。
DIALOG_WINDOW_OVERLAP_MIN = 0.05


@dataclass
class DialogVerdict:
    """当前页面是不是弹窗 / 有遮罩，以及凭什么这么判。

    `evidence` 不是装饰：B4 的归因要拿它当证据，报告里也要能解释。
    """
    is_dialog: bool = False
    evidence: List[str] = field(default_factory=list)


def detect_dialog(root: Optional[LayoutNode]) -> DialogVerdict:
    """判断当前控件树是不是「弹窗压在页面上」的状态。

    三条独立线索，命中任意一条即判为弹窗（都写进 evidence）：
      ① 控件类型带弹窗语义（Dialog / Popup / Menu / Sheet ...）
      ② 两个窗口**互相重叠**（弹窗压在应用上）—— 不是「存在多个窗口」
      ③ 存在近似全屏的遮挡节点（面积盖住 90% 屏幕，且 zIndex>0 或 opacity 不透明）

    不先处理弹窗，后续所有点击都会打在遮罩上 —— 这是 B2 把它提到最高优先级的理由。

    ★ 线索②的判据在 2026-09-23 被重写（用 C 给的真机样本抓到的**真机缺陷**）：

    原文是「出现 ≥2 个不同的 hostWindowId → 多窗口叠加」。这在模拟设备上成立，
    在真机上**恒为真**：`uitest dumpLayout` 返回的是整个窗口栈 ——
    状态栏、系统窗、导航栏、被测应用各占屏幕一块，本来就有 4 个 hostWindowId。

    后果不是「偶尔误报」而是**每一张真机页面都被判成弹窗**：
    弹窗置顶（HIGH 档）永远生效 → 四档优先级排序整体失效；
    B4 拿这条当证据时也会一直看到「多窗口叠加」。**7/7 真机样本全中。**

    改成几何判据后（重叠面积 ≥ 屏幕 5%，见 `DIALOG_WINDOW_OVERLAP_MIN` 的实测数字），
    7 张真机样本里只有真正的那个弹窗页被判为弹窗。
    """
    ev: List[str] = []
    if root is None:
        return DialogVerdict(False, ev)

    for n in root.walk():
        t = (n.type or '').lower()
        if any(h in t for h in DIALOG_TYPE_HINTS):
            ev.append(f'控件类型含弹窗语义：{n.type}')
            break

    # ---- 线索②：两个窗口**互相重叠**
    boxes: Dict[str, List[int]] = {}
    for n in root.walk():
        wid = ''
        for k in WINDOW_ID_KEYS:
            v = n.attributes.get(k)
            if v not in (None, ''):
                wid = str(v)
                break
        if not wid:
            continue      # 合成根节点（'' 窗口）覆盖全屏，不参与叠加判定
        if not n.visible or n.rect.area <= 0:
            continue
        b = boxes.setdefault(wid, [n.rect.left, n.rect.top,
                                   n.rect.right, n.rect.bottom])
        b[0] = min(b[0], n.rect.left)
        b[1] = min(b[1], n.rect.top)
        b[2] = max(b[2], n.rect.right)
        b[3] = max(b[3], n.rect.bottom)

    screen = root.rect.area or 0
    wids = sorted(boxes)
    found = False
    for i in range(len(wids)):
        for j in range(i + 1, len(wids)):
            a, c = boxes[wids[i]], boxes[wids[j]]
            ow = min(a[2], c[2]) - max(a[0], c[0])
            oh = min(a[3], c[3]) - max(a[1], c[1])
            if ow <= 0 or oh <= 0 or screen <= 0:
                continue
            ratio = (ow * oh) / screen
            if ratio >= DIALOG_WINDOW_OVERLAP_MIN:
                ev.append(f'多窗口叠加：hostWindowId={wids[i]} 与 {wids[j]} '
                          f'重叠 {ow}×{oh}={ow * oh} px²'
                          f'（屏幕的 {ratio:.1%}）')
                found = True
                break
        if found:
            break

    root_area = root.rect.area
    if root_area > 0:
        for n in root.walk():
            if n is root or not n.visible or n.rect.area <= 0:
                continue
            if n.rect.area < root_area * 0.9:
                continue
            z = _as_num(n.attributes.get('zIndex'))
            op = _as_num(n.attributes.get('opacity'), default=1.0)
            if (z is not None and z > 0) or \
                    (op is not None and op < MIN_INTERACTIVE_OPACITY):
                ev.append(f'存在遮挡节点：{n.type} zIndex={z} opacity={op}')
                break

    return DialogVerdict(bool(ev), ev)


def _as_num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def classify_priority(node: LayoutNode, *, in_dialog: bool = False,
                      exhausted: bool = False, dangerous: bool = False,
                      allow_dangerous: bool = False) -> Optional[int]:
    """给一个控件定档。返回 None 表示**拦截**（不参与探索）。"""
    if dangerous:
        return Priority.URGENT if allow_dangerous else None
    if exhausted:
        return Priority.LOW
    if in_dialog:
        return Priority.HIGH
    blob = f'{node.text} {node.id} {node.descr} {node.hint}'.lower()
    if any(k.lower() in blob for k in HIGH_KEYWORDS):
        return Priority.HIGH
    if node.clickable or node.is_interactive():
        return Priority.MEDIUM
    return Priority.NORMAL


# ================================================================ ★ 资源预算

@dataclass
class Budget:
    """一次探索的资源上限。

    这是对外契约 `explore(max_pages, budget)` 里那个 `budget` 的形态。
    四个上限**全部要有**，缺一个探索就可能在某些页面上停不下来：
    页数管广度、每页动作数管深度、秒数管卡死、总步数管「页数少但每页巨大」。
    """
    max_pages: int = 8
    max_actions_per_page: int = 6
    max_seconds: float = 300.0
    max_steps: int = 200

    def __post_init__(self) -> None:
        for name in ('max_pages', 'max_actions_per_page', 'max_steps'):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f'Budget.{name} 必须为正整数')
        if float(self.max_seconds) <= 0:
            raise ValueError('Budget.max_seconds 必须为正数')

    def to_dict(self) -> Dict[str, Any]:
        return {'max_pages': self.max_pages,
                'max_actions_per_page': self.max_actions_per_page,
                'max_seconds': self.max_seconds, 'max_steps': self.max_steps}


# ================================================================ ★ 覆盖度

@dataclass
class CoverageReport:
    """覆盖度报告。

    ⚠️ 口径说明（不然很容易自我感觉良好）：

    **一、分母只含已访问页面**
        本指标的分母是**探索过程中实际访问过的页面**里的全部可交互控件，
        所以它衡量的是「进了这一页，我把它点全了吗」，**不含「页面有没有找全」**。
        页面是否找全要看 `pages_structural` —— 两者必须一起看，
        否则「只进一页、点光了它」也能拿到 100%。

    **二、两个页数字段不是一回事（2026-09-22 订正）**

        pages             探索覆盖了多少种界面**状态**（内容签名去重）
        pages_structural  界面上有多少个**不同的页面**（结构签名归并）

        **KPI「发现页面数」取 `pages_structural`。**

        为什么：本体例的「页面」粒度就是结构签名。用内容签名会把
        「点一下、正文文字变了」算成一个新页面 —— 真机计算器（单页应用）
        点 5 次就被算成 6 个页面，数字虚高。这正是 B1 里警告过的
        「内容签名太敏感」，只不过它体现在报告的页数上而不是探索逻辑上。
        `pages`（状态数）仍然有用，它衡量的是**界面状态**的覆盖面，
        报告里两个都出，别混用。

    **三、分母包含被安全策略拦下的危险控件**，并单独报 `blocked`。
        把拦掉的控件从分母里剔掉会变成「拦得越多、数字越好看」，不干。
    """
    interactive_total: int = 0
    interactive_visited: int = 0
    pages: int = 0                  # 界面**状态**数（内容签名）
    pages_structural: int = 0       # **页面**数（结构签名）← KPI 用这个
    blocked: int = 0        # 因安全策略被拦下、永远不会被点的危险控件数
    per_page: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        if self.interactive_total <= 0:
            return 0.0
        return self.interactive_visited / self.interactive_total

    def to_dict(self) -> Dict[str, Any]:
        return {'interactive_total': self.interactive_total,
                'interactive_visited': self.interactive_visited,
                'ratio': round(self.ratio, 4),
                'pages': self.pages,
                'pages_structural': self.pages_structural,
                'blocked': self.blocked,
                'per_page': self.per_page}


# ================================================================ ★ Tarpit 防粘滞
#
# 借鉴 HapTest 的 `--simk`（UI 相似度阈值跳出，见
# 项目内部调研档案（仓库外）§三）。
#
# 问题的形态：列表翻页、开关切换、「加一」按钮这类操作，每次点击都让文案
# 变一点 —— 内容签名体系下每点一次都是一个「新页面」。B1 的双签名解决了
# 「返回栈回溯被文案变化骗走」的问题，但 BFS 队列仍然会把这些同族小变体
# 逐个入队探索：预算被「同一页的影子」吃光，覆盖率虚高、状态图被灌水。
# 这就是 tarpit（焦油坑）—— 探索器看得见出口，却总在同一段路上打转。
#
# 防线只有一条原则：**只影响「要不要入队探索」，不影响记账。**
# 状态照加、边照记、相似度和原因照存 —— 报告里查得到「为什么没探这一页」，
# 绝不静默丢弃（与 generate_case 的「不许静默丢边」是同一条红线）。

def content_similarity(a: PageSignature, b: PageSignature) -> float:
    """两个页面状态的内容相似度（Jaccard，0~1）。

    粒度取**控件行**（type|id|text|descr|WxH），不取整串哈希 ——
    要度量的正是「大部分控件没变、少数文案变了」这种局部差异。
    两页控件完全一致 → 1.0；完全不同 → 0.0。
    """
    sa = {p for p in a.content.split(';') if p}
    sb = {p for p in b.content.split(';') if p}
    union = sa | sb
    if not union:
        return 1.0        # 两张全空页视为相同（内容键相等时根本走不到这里）
    return len(sa & sb) / len(union)


@dataclass
class TarpitPolicy:
    """tarpit 防粘滞策略。

    sim_threshold       与来源页的相似度 ≥ 此值 → 视为「没走出原页」，不入队。
                        0.90 的量级参考：20 个控件的列表页翻页（改 1~2 行）≈ 0.86~0.95；
                        小页面上的开关切换（3 控件改 1 行）≈ 0.5 —— 不会误伤。
    max_family_states   同一结构签名下最多入队多少个状态。防「每次变化都够大、
                        但始终在同一结构里绕圈」的残余形态；给 4 是给
                        合法的同构兄弟页（如同模板的详情页）留的余量。
    """
    enabled: bool = True
    sim_threshold: float = 0.90
    max_family_states: int = 4

    def __post_init__(self) -> None:
        if not 0.0 <= float(self.sim_threshold) <= 1.0:
            raise ValueError('TarpitPolicy.sim_threshold 必须在 [0, 1]')
        if int(self.max_family_states) < 1:
            raise ValueError('TarpitPolicy.max_family_states 必须为正整数')

    def to_dict(self) -> Dict[str, Any]:
        return {'enabled': self.enabled,
                'sim_threshold': self.sim_threshold,
                'max_family_states': self.max_family_states}


# ---------------------------------------------------------------- 状态图

@dataclass
class PageState:
    """一个页面状态（内容签名去重）。"""
    sid: str
    signature: str
    structural: str = ''
    content_key: str = ''
    structural_key: str = ''
    title: str = ''
    # 设备自报的页面路由（PageSignature.page_path 原样带出）。
    # 签名层早就有它（参与双签名哈希），但状态层之前没带出来 ——
    # 覆盖率工具想做「页级归并」时读不到（B 复核；C 于 09-28 B7 实测补全）。
    page_path: str = ''
    first_seen: float = field(default_factory=time.time)
    visits: int = 0
    screenshot: Optional[str] = None
    # ★ 返回栈回溯：从入口页到达本页的控件序列（元素是匹配器规格）。
    #   探索别的页面时设备会离开这里，要回来就得照着这条路重走一遍 ——
    #   只靠 `back()` 是回不来的：连点几层之后 back 只退一层。
    path: List[Dict[str, Any]] = field(default_factory=list)
    reachable: bool = True

    def to_dict(self) -> Dict[str, Any]:
        d = {'id': self.sid, 'title': self.title, 'visits': self.visits,
             'screenshot': self.screenshot,
             'content_key': self.content_key,
             'structural_key': self.structural_key,
             # 设备自报路由：签名层参与哈希、状态层存了字段，这里必须一并带出 ——
             # 不然落盘的 graph.json 里读不到它，「页级归并」的消费方只能拿
             # structural_key 反推，而那正是 page_path 想替掉的事。
             'page_path': self.page_path,
             'reachable': self.reachable}
        if self.path:
            d['path'] = self.path
        return d


@dataclass
class Transition:
    """一条跳转边：在某页点了某控件，到了另一页。"""
    src: str
    dst: str
    control: str
    control_spec: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    note: str = ''

    def to_dict(self) -> Dict[str, Any]:
        d = {'src': self.src, 'dst': self.dst, 'control': self.control, 'ok': self.ok}
        if self.control_spec:
            d['spec'] = self.control_spec
        if self.note:
            d['note'] = self.note
        return d


class StateGraph:
    """页面状态图。

    `states` 按**内容签名**索引（精确去重：这页访问过没有）；
    `structural_index` 按**结构签名**索引（返回栈回溯、DOM 变体重归并）。
    两份索引缺一不可 —— 只有一份就必然在「同页新发现」或「异页同名」上翻车。
    """

    def __init__(self) -> None:
        self.states: Dict[str, PageState] = {}
        self.structural_index: Dict[str, Set[str]] = {}
        self.transitions: List[Transition] = []
        self._auto = 0

    def add_state(self, signature: Any, title: str = '',
                  screenshot: Optional[str] = None,
                  path: Optional[List[Dict[str, Any]]] = None) -> PageState:
        """加入一个页面状态。

        `signature` 可以是 `PageSignature`，也可以是裸字符串（旧调用方式，
        会被当成内容签名，结构签名留空）—— 保持向后兼容。
        `path` 是从入口页到达本页的控件序列，供返回栈回溯重走。
        """
        if isinstance(signature, PageSignature):
            key = signature.content_key
            struct_key = signature.structural_key
            content_text = signature.content
            struct_text = signature.structural
            page_path = signature.page_path
        else:
            key = _digest(str(signature))
            struct_key = ''
            content_text = str(signature)
            struct_text = ''
            page_path = ''

        if key in self.states:
            st = self.states[key]
            st.visits += 1
            # 路由以**最新的可信观测**为准：空则补写（幂等地填一次），
            # 非空则保留 —— 先到的那次观测不该被无声改写。
            # 触发条件是「同一个 key、先空后有」：真实 PageSignature 把
            # page_path 算进哈希，两条路由必然两个 key，所以生产路径够不着；
            # 这里守的是公开方法的契约（add_state 不是私有方法）。
            if not st.page_path and page_path:
                st.page_path = page_path
            return st

        self._auto += 1
        st = PageState(sid=f'S{self._auto}', signature=content_text,
                       structural=struct_text, content_key=key,
                       structural_key=struct_key,
                       title=title or f'页面{self._auto}', screenshot=screenshot,
                       page_path=page_path,
                       path=list(path or []))
        st.visits = 1
        self.states[key] = st
        if struct_key:
            self.structural_index.setdefault(struct_key, set()).add(st.sid)
        return st

    def find_by_structural(self, structural_key: str) -> List[PageState]:
        """找出所有结构签名相同的页面状态。"""
        sids = self.structural_index.get(structural_key, set())
        return [s for s in self.states.values() if s.sid in sids]

    def add_edge(self, src: str, dst: str, control: str,
                 spec: Optional[Dict[str, Any]] = None, ok: bool = True,
                 note: str = '') -> None:
        for t in self.transitions:
            if t.src == src and t.dst == dst and t.control == control:
                return
        self.transitions.append(Transition(src, dst, control, spec or {}, ok, note))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'states': [s.to_dict() for s in self.states.values()],
            'transitions': [t.to_dict() for t in self.transitions],
        }


def _sig_hash(sig: str) -> str:
    """旧接口保留：早期版本用它算页面 key。"""
    return _digest(sig)


# ---------------------------------------------------------------- 探索器

class Explorer:
    """基于状态图的广度优先自动探索。"""

    # 状态栏剔除阈值的「最后兜底」（历史值：DAYU200 实测 72px）。
    # 运行时优先用 _status_bar_top()：实测 SystemUi_StatusBar 节点底边，
    # 取不到按屏高 5.6% 估算（72/1280 的来历）—— 不写死任何设备的 px。
    STATUS_BAR_MAX_TOP = 72

    #: 半盲页阈值：树内可交互候选少于它 → 视觉定位介入（仅当注入了定位器）
    BLIND_PAGE_THRESHOLD = 3
    #: 视觉定位指令：产出与控件树候选同构（可点击元素名列表），才能进探索队列
    VISION_INSTRUCTION = ('列出当前屏幕上所有可点击的元素（按钮/开关/列表项），'
                          '每个元素给出名称和位置')

    def __init__(self, driver: Driver, policy: Optional[SafetyPolicy] = None,
                 artifact_dir: Optional[str] = None, verbose: bool = True,
                 tarpit_policy: Optional[TarpitPolicy] = None,
                 vision_locator: Optional[Any] = None,
                 permission_policy: Optional[str] = None) -> None:
        self.driver = driver
        self.policy = policy or SafetyPolicy()
        self.tarpit_policy = tarpit_policy or TarpitPolicy()
        # 视觉定位器（可选）：半盲页（树候选过少）时介入，补充控件树看不到的
        # 可点击目标（Canvas/无标识按钮）。缓存命中统计由定位器自带
        # （cache_hit_rate），这里只透传——B14 的 KPI 数字从这里出。
        self.vision_locator = vision_locator
        self.vision_stats: Dict[str, Any] = {'calls': 0, 'cache_hits': 0,
                                             'cache_misses': 0, 'errors': 0,
                                             'suggested': 0, 'tapped_ok': 0}
        # 页级缓存：页面指纹 → 视觉候选（同页重访即命中，B14 的测量口径）
        self._vision_page_cache: Dict[str, List[LayoutNode]] = {}
        self.graph = StateGraph()
        self.artifact_dir = artifact_dir or getattr(driver, 'artifact_dir', None)
        self.verbose = verbose
        self.skipped: List[Dict[str, Any]] = []

        # 覆盖度台账：以「结构签名::控件稳定 ID」为键，文案变化不重复计数
        self._universe: Set[str] = set()
        self._done: Set[str] = set()
        self._per_page: Dict[str, Dict[str, Any]] = {}
        # 已执行过且没有引起页面变化的控件（结构签名::控件 ID）→ 降到 LOW
        self._exhausted: Set[str] = set()
        # 被安全策略拦下的控件（结构签名::控件 ID）—— 计入分母但不计入分子
        self._blocked: Set[str] = set()
        # tarpit 台账：同结构签名的状态计数 + 被拦截不入队的记录（供报告取证）
        self._tarpit_family: Dict[str, int] = {}
        self.tarpit_hits: List[Dict[str, Any]] = []
        # 返回核对的判定流水，供报告与测试查看（exact / content-changed / ...）
        self.back_verdicts: List[str] = []
        self.last_budget: Optional[Budget] = None
        self.dialog: DialogVerdict = DialogVerdict()
        # C3 权限弹窗：策略（record / allow / deny，默认 deny）+ 台账。
        # 台账必须留：替用户作答是**有副作用的动作**，不能只在日志里一闪而过。
        self.permission_policy = resolve_policy(permission_policy)
        self.permission: PermissionDialogVerdict = PermissionDialogVerdict()
        self.permission_events: List[Dict[str, Any]] = []

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f'[explore] {msg}')

    # ------------------------------------------------------------ 页面

    def _page_signatures(self, root: Optional[LayoutNode] = None) -> PageSignature:
        """取当前页面的双签名。

        默认**无条件 refresh**（不是性能疏忽）：页面签名驱动返回核对、
        导航判定、候选生成这些**状态决策**，而 `_tree_dirty` 只看得见
        宿主发起的动作 —— 设备带外自变（晚到的跳转、通知、异步加载）
        只有无条件重取才能兜住。省 dump 的口子开在 wait_idle 的判稳
        基线上，不开在这里。
        """
        root = root if root is not None else self.driver.refresh()
        return build_page_signature(
            root,
            bundle=str(getattr(self.driver, 'bundle', '') or ''),
            ability=str(getattr(self.driver, 'ability', '') or ''))

    def _page_signature(self) -> str:
        """旧接口保留：只返回内容签名。新代码请用 `_page_signatures()`。"""
        return self._page_signatures().content

    @staticmethod
    def _status_bar_top(root: Any) -> int:
        """状态栏剔除阈值（px）—— 不写死任何设备的分辨率。

        优先实测：树里找 id/type 含 statusbar 的节点，取其底边；
        取不到按根屏高 5% 估算 —— 区间依据：必须 > 状态栏文字的 top
        （DAYU200 实测 32px/1280 高）且 < 页面标题的 top（1080×2340 上
        标题在 120，对应 5.1%），5% 在两台样本上都成立；
        连屏高都拿不到才回落历史常量。
        """
        try:
            h = root.rect.height
        except Exception:
            h = 0
        try:
            for n in root.walk():
                tag = ((n.id or '') + (n.type or '')).lower()
                if 'statusbar' in tag:
                    return n.rect.bottom
        except Exception:
            pass
        return int(h * 0.05) if h > 0 else Explorer.STATUS_BAR_MAX_TOP

    def _page_title(self) -> str:
        """猜页面标题：**排除状态栏后**，取最靠上的短文本。

        判据是「最靠上」而不是「字最大」—— 标题栏的位置是稳定的，字号不是。
        （真机上试过按面积排：正文提示「按 = 出结果」的面积是标题「计算器」的
        30 倍，直接把标题挤掉了。）

        ⚠️ 真机缺陷（2026-09-22，C 在 DAYU200 上抓到）：**必须先剔掉状态栏**。
        真机状态栏永远在 `top≈32` 且带文本（`没有 SIM 卡` 在 `[43,32][170,50]`），
        所以「最靠上的文本」永远命中状态栏 —— 六个页面标题全成了同一句。
        模拟设备的状态栏文本为空，自测暴露不出来。

        状态栏阈值运行时实测/估算，见 `_status_bar_top()`。
        """
        try:
            root = self.driver.root
            bar_top = self._status_bar_top(root)
            cands = []
            for n in root.walk():
                if not (n.visible and n.text) or len(n.text) > 20:
                    continue
                if n.type not in ('Text', 'Title', 'NavigationTitle',
                                  'NavDestinationTitle'):
                    continue
                if n.rect.top < bar_top:
                    continue                      # ★ 剔掉状态栏区域
                cands.append((n.rect.top, -n.rect.area, n.text))
            if cands:
                cands.sort()
                return cands[0][2]
        except Exception:
            pass
        return ''

    def _observe(self) -> Tuple[Optional[LayoutNode], PageSignature]:
        """刷新控件树 → 算签名 → 登记覆盖度分母 → 判弹窗。"""
        root = self.driver.refresh()
        sig = self._page_signatures(root)
        self.dialog = detect_dialog(root)
        self._note_page(sig, root)
        return root, sig

    def _clear_permission_dialog(self, root: Optional[LayoutNode]) -> bool:
        """按策略处置系统权限弹窗；返回 True 表示已动作（调用方需重新观察）。

        与「安全策略拦截」不是一回事：拦截是**不点**，这里是**必须点掉** ——
        系统权限弹窗盖住整个内容区时 `dumpLayout` 返回的就是弹窗的树，
        不点掉它，后面的观察、定位、点击全都打在弹窗上。
        """
        verdict = detect_permission_dialog(root)
        self.permission = verdict
        if not verdict.is_permission_dialog:
            return False
        chosen = None
        if self.permission_policy == POLICY_ALLOW:
            chosen = verdict.allow
        elif self.permission_policy == POLICY_DENY:
            chosen = verdict.deny
        label = chosen.text.strip() if chosen is not None else ''
        self.permission_events.append({
            'policy': self.permission_policy, 'owner': verdict.owner,
            'title': verdict.title, 'answered': label,
            'evidence': list(verdict.evidence)})
        if chosen is None:
            self.log('权限弹窗（%s「%s」）策略=%s —— 未作答（无可点按钮）'
                     % (verdict.owner, verdict.title, self.permission_policy))
            return False
        self.log('权限弹窗（%s「%s」）策略=%s → 点「%s」'
                 % (verdict.owner, verdict.title, self.permission_policy, label))
        try:
            self.driver.tap(chosen, post_idle=True)
        except Exception as e:                                     # noqa: BLE001
            self.log('  权限弹窗作答失败: %s' % str(e)[:80])
            return False
        return True

    def _note_page(self, sig: PageSignature, root: Optional[LayoutNode]) -> None:
        """把当前页面的可交互控件登记进覆盖度分母。

        ⚠️ 这里**只登记分母**（`_universe` / `_blocked`）与页面标题。
        `total` / `visited` 不在此时算 —— 刚进这一页、还没点任何控件时，
        这一页的已访数注定是 0，存下来就永远停在 0（见 `coverage` 的说明）。
        """
        if root is None:
            return
        self._per_page.setdefault(
            sig.structural_key, {'title': self._page_title()})
        for n in self._interactive_nodes(root):
            uid = f'{sig.structural_key}::{control_key(n)}'
            self._universe.add(uid)
            dang, _ = _matches_danger(n, self.policy.deny_patterns)
            if dang and not self.policy.allow_dangerous:
                self._blocked.add(uid)

    def _interactive_nodes(self, root: LayoutNode) -> List[LayoutNode]:
        """当前页面上「算得上可交互」的全部控件（覆盖度分母的原始集合）。

        注意与 `_candidates()` 的区别：这里**含**被安全策略拦下的危险控件 ——
        拦截是我们的策略，不代表这个控件不可交互。把拦截掉的控件从分母里
        剔掉，覆盖率就会因为「拦得越多、数字越好看」而虚高。
        """
        out = []
        for n in flatten(root, only_visible=True):
            if n.rect.area <= 0:
                continue
            if not (n.clickable or n.is_interactive()):
                continue
            if n.type in ('Text', 'Image', 'Divider', 'Blank', 'Stack') and not n.clickable:
                continue
            if n.rect.width < 20 or n.rect.height < 20:
                continue
            out.append(n)
        return out

    def _candidates(self, root: Optional[LayoutNode] = None,
                    prioritized: bool = True) -> List[LayoutNode]:
        """当前页面的可点击候选控件，按档位降序排列。

        排序规则（任务卡 B2）：先榨干当前页未执行过的事件（档位降序），
        再跳到最近的未访问页面 —— 这个「局部穷尽 + 全局 BFS」的混合结构
        比纯 BFS 少走回头路，也天然避免页面间来回弹跳。
        """
        root = root if root is not None else self.driver.refresh()
        sig = self._page_signatures(root)
        items: List[Tuple[int, int, LayoutNode]] = []
        seen: Set[Tuple[int, int, str]] = set()

        for idx, n in enumerate(flatten(root, only_visible=True)):
            ok, why = self.policy.is_clickable_candidate(n)
            # 危险判定独立于策略：放开后仍要认出它们并置顶，不能靠 why 里有没有
            # 'dangerous(' 来判断（allow_dangerous=True 时那个标记根本不会出现）。
            dangerous, _pat = _matches_danger(n, self.policy.deny_patterns)
            if not ok and not dangerous:
                if why not in ('invisible', 'passive-Text'):
                    self.skipped.append({'label': n.label, 'reason': why})
                continue
            if not ok and dangerous and not self.policy.allow_dangerous:
                self.skipped.append({'label': n.label, 'reason': why})
                continue

            key = (n.rect.center[0] // 8, n.rect.center[1] // 8, n.type)
            if key in seen:
                continue
            seen.add(key)

            uid = f'{sig.structural_key}::{control_key(n)}'
            pr = classify_priority(
                n, in_dialog=self.dialog.is_dialog,
                exhausted=uid in self._exhausted,
                dangerous=dangerous,
                allow_dangerous=self.policy.allow_dangerous)
            if pr is None:
                self.skipped.append({'label': n.label, 'reason': 'dangerous(blocked)'})
                continue
            items.append((pr, idx, n))

        if prioritized:
            # 档位降序；同档保持控件树顺序（稳定排序），保证可复现
            items.sort(key=lambda it: (-it[0], it[1]))
        return [n for _, _, n in items]

    def priority_of(self, node: LayoutNode) -> Optional[int]:
        """给单个控件定档（供测试与报告用）。"""
        bad, _ = _matches_danger(node, self.policy.deny_patterns)
        return classify_priority(node, in_dialog=self.dialog.is_dialog,
                                 dangerous=bad,
                                 allow_dangerous=self.policy.allow_dangerous)

    def _vision_candidates(self, limit: int, fingerprint: str) -> List[LayoutNode]:
        """半盲页的视觉候选：截图 → Provider 枚举 → 过安全围栏 → **伪控件节点**。

        伪节点带视觉框（rect）与 label，能直接流进既有探索循环
        （tap 走 node.center，产物规格为 {'text': label}——无坐标，红线安全）。
        页级缓存按**页面内容指纹**键控——同页重访即命中（B14 的测量口径），
        命中/未命中计入 vision_stats 供 KPI 出数。"""
        cached = self._vision_page_cache.get(fingerprint)
        if cached is not None:
            self.vision_stats['cache_hits'] += 1
            return cached[:limit]

        self.vision_stats['cache_misses'] += 1
        shot = self.driver.screenshot()
        if not shot:
            return []
        try:
            w, h = self.driver.screen_size()
            targets = self.vision_locator.locate(
                shot, self.VISION_INSTRUCTION, w, h) or []
            self.vision_stats['calls'] += 1
        except Exception as e:                              # noqa: BLE001
            self.vision_stats['errors'] += 1
            self.log(f'  视觉定位失败（不影响探索）: {type(e).__name__}: {str(e)[:80]}')
            return []

        out: List[LayoutNode] = []
        for t in targets:
            label = (getattr(t, 'label', '') or '').strip()
            rect = getattr(t, 'rect', None)
            if not label or rect is None:
                continue
            nums = [int(v) for v in re.findall(r'\d+', str(rect))]
            if len(nums) < 4:
                continue
            fake = LayoutNode(type='Vision', id='', text=label,
                              rect=Rect(nums[0], nums[1], nums[2], nums[3]),
                              clickable=True)
            dangerous, _pat = _matches_danger(fake, self.policy.deny_patterns)
            if dangerous and not self.policy.allow_dangerous:
                self.skipped.append({'label': label,
                                     'reason': 'dangerous(vision)'})
                continue
            out.append(fake)
            if len(out) >= limit:
                break
        self._vision_page_cache[fingerprint] = out
        self.vision_stats['suggested'] += len(out)
        return out

    def _spec_of(self, node: LayoutNode) -> Dict[str, Any]:
        """给控件生成优先稳定的匹配器规格：id 优先，其次 text，再次 label。"""
        if node.id:
            return {'id': node.id}
        if node.text:
            return {'text': node.text}
        if node.descr:
            return {'descr': node.descr}
        return {'type': node.type, 'nth': 0}

    def _tarpit_judge(self, new_sig: PageSignature,
                      cur_sig: PageSignature) -> Optional[str]:
        """新状态要不要入队探索？返回拦截原因；None = 放行。

        只在 **fresh**（内容签名第一次见）的新状态上调用。
        两条判据见 `TarpitPolicy` 的说明；拦截的记录进 `tarpit_hits`。
        """
        if not self.tarpit_policy.enabled:
            return None
        sim = content_similarity(new_sig, cur_sig)
        if sim >= self.tarpit_policy.sim_threshold:
            return (f'与来源页相似度 {sim:.2f} ≥ '
                    f'{self.tarpit_policy.sim_threshold:.2f}（同页小变体）')
        fam = self._tarpit_family.get(new_sig.structural_key, 0)
        if fam >= self.tarpit_policy.max_family_states:
            return (f'同结构状态已入队 {fam} 个 ≥ 上限 '
                    f'{self.tarpit_policy.max_family_states}（同族封顶）')
        return None

    def _tarpit_note(self, sig: PageSignature) -> None:
        """把一个已登记状态计入其结构族的台账（含入口页与被拦截的状态）。"""
        self._tarpit_family[sig.structural_key] = \
            self._tarpit_family.get(sig.structural_key, 0) + 1

    # ------------------------------------------------------------ 主循环

    def explore(self, max_pages: Optional[int] = None,
                budget: Optional[Budget] = None,
                max_actions_per_page: Optional[int] = None,
                max_seconds: Optional[float] = None,
                return_back: bool = True) -> StateGraph:
        """从当前页面开始广度优先探索。

        **对外契约是 `explore(max_pages, budget)`。**

        `max_actions_per_page` / `max_seconds` 这两个**过渡期散参数已弃用**
        （它们已经进了 `Budget`）。保留只是为了让还没迁移的调用方不至于当场断掉，
        用一次会发一条 `DeprecationWarning`。迁移就一行：

            explore(max_pages=8, max_actions_per_page=6, max_seconds=300)
            → explore(8, Budget(max_actions_per_page=6, max_seconds=300))

        `return_back` **不是**同一个层次的参数：它是行为开关（探完一页要不要退回原页），
        不是资源上限，所以留在签名里，并改成 keyword-only 以免位置传参出错。

        max_pages:            最多发现多少个页面状态
        budget:               Budget，一次探索的资源上限
        max_actions_per_page: 【已弃用】每页最多尝试多少个控件 → 用 Budget
        max_seconds:          【已弃用】总时间上限 → 用 Budget
        return_back:          每次跳转后尝试返回，维持 BFS 层次
        """
        if max_actions_per_page is not None or max_seconds is not None:
            import warnings
            warnings.warn(
                'explore() 的 max_actions_per_page / max_seconds 已弃用，'
                '请改用 explore(max_pages, Budget(...))；'
                '散参数将在 W4 前删除。',
                DeprecationWarning, stacklevel=2)

        overrides: Dict[str, Any] = {}
        if max_pages is not None:
            overrides['max_pages'] = int(max_pages)
        if max_actions_per_page is not None:
            overrides['max_actions_per_page'] = int(max_actions_per_page)
        if max_seconds is not None:
            overrides['max_seconds'] = float(max_seconds)
        b = replace(budget, **overrides) if budget is not None else Budget(**overrides)
        self.last_budget = b

        t0 = time.time()
        steps = 0
        root, sig = self._observe()
        # ★ 权限弹窗必须先答掉：它盖住内容区时整棵树都是弹窗的，
        #   不处理的话「起始页面」直接就是弹窗，探索从这里整体跑偏。
        if self._clear_permission_dialog(root):
            root, sig = self._observe()
        start = self.graph.add_state(sig, self._page_title())
        self._tarpit_note(sig)
        self.log(f'起始页面 {start.sid}「{start.title}」 结构签名 {sig.structural_key[:8]}')

        queue: deque = deque([start])
        visited: Set[str] = {sig.content_key}

        while queue and len(self.graph.states) < b.max_pages:
            if time.time() - t0 > b.max_seconds:
                self.log(f'达到时间上限 {b.max_seconds}s，停止探索')
                break

            cur = queue.popleft()
            self.log(f'--- 探索 {cur.sid}「{cur.title}」 (第 {cur.visits} 次访问)')

            # ★ 返回栈回溯：设备早就被上一次探索带走了，先照路径回到这一页。
            #   不做这一步的话，队列里的「待探索页面」和设备的实际页面会脱节 ——
            #   探索器会拿着 A 页的候选控件去点 B 页，页面状态图整个失真。
            if not self._navigate_to(cur):
                cur.reachable = False
                self.log(f'  {cur.sid} 返回栈回溯失败，标记不可达并跳过')
                continue

            # ★ 刷新后的签名必须接住（C 复核发现：原来写成 `root, _ =`，
            #   于是整个内层循环用的都是**入口页**的 `sig`）。
            #   后果不是崩溃而是四层静默偏差：
            #     ① `_done` 按入口页记账 → 跨页同 key 控件被误合并 → 后续页面少探索；
            #     ② 「点了没反应就降档」在非入口页静默失效；
            #     ③ 覆盖度明细里每页 `visited` 恒为 0；
            #     ④ 状态图（挑战 #3）可信度受影响。
            #   它不显眼，是因为现有测试的无效控件恰好落在入口页 —— 见
            #   `tests/test_explorer_b1.py::TestRefreshSignatureRegression`。
            try:
                root, sig = self._observe()
            except DriverError as e:
                self.log(f'刷新控件树失败: {e}')
                continue

            # 每轮重新观察后都要再判一次：权限弹窗可能在任意一次跳转后弹出
            if self._clear_permission_dialog(root):
                root, sig = self._observe()

            cands = self._candidates(root)[:b.max_actions_per_page]
            if (self.vision_locator is not None
                    and len(cands) < self.BLIND_PAGE_THRESHOLD):
                # 缓存键 = 结构指纹（内容变化时稳定——设置页每次回访
                # 内容都有微变，content_key 会让缓存永不命中）
                v = self._vision_candidates(
                    max(0, b.max_actions_per_page - len(cands)),
                    fingerprint=sig.structural_key)
                if v:
                    self.log(f'  半盲页：视觉补充 {len(v)} 个候选')
                    cands = cands + v
            self.log(f'候选控件 {len(cands)} 个'
                     f'{"（弹窗优先）" if self.dialog.is_dialog else ""}')

            for node in cands:
                if len(self.graph.states) >= b.max_pages or steps >= b.max_steps:
                    break
                if time.time() - t0 > b.max_seconds:
                    break
                steps += 1

                spec = self._spec_of(node)
                label = f'{node.type}:{node.label}'
                uid = f'{sig.structural_key}::{control_key(node)}'

                # 截图分级留存（docs/截图分级留存策略.md）—— 判定输入由探索器
                # 给，driver 不猜：
                #   * 新页首达（本页还没登记过页面图）→ 必留，这是页面级证据；
                #   * 降级命中（视觉通道伪节点）→ 必留，走了视觉通道不留图=隐瞒；
                #   * 已留档页面上的普通成功交互 → 可省，省略在 Step 上留痕。
                keep_shot = not cur.screenshot or node.type == 'Vision'

                try:
                    self.driver.tap(node, post_idle=True, keep_shot=keep_shot)
                    if node.type == 'Vision':
                        self.vision_stats['tapped_ok'] += 1
                except Exception as e:
                    self.graph.add_edge(cur.sid, cur.sid, label, spec,
                                        ok=False, note=str(e)[:120])
                    self.log(f'  点击「{node.label}」失败: {str(e)[:80]}')
                    continue

                self._done.add(uid)

                try:
                    new_sig = self._page_signatures()
                except DriverError as e:
                    self.log(f'  跳转后无法读取控件树: {str(e)[:70]}')
                    new_sig = sig

                if new_sig.content_key == cur.content_key:
                    self.graph.add_edge(cur.sid, cur.sid, label, spec,
                                        note='页面未变化')
                    # 点了但页面没变 → 这个控件榨干了，下一轮降到 LOW
                    self._exhausted.add(uid)
                    self.log(f'  点击「{node.label}」-> 页面无变化')
                else:
                    st = self.graph.states.get(new_sig.content_key)
                    fresh = st is None
                    # ★ tarpit 判定必须在登记之前：拦截依据是「现有的」同族数量
                    tarpit_reason = self._tarpit_judge(new_sig, sig) if fresh else None
                    shot = None
                    if fresh and self.artifact_dir:
                        # 新页面首次到达 → 页面图必留（分级矩阵第一行）。
                        # 拿到新签名后再截：wait_idle 已判稳，屏幕内容稳定；
                        # 与旧实现「签名前先截」的错位窗口同量级，不新增风险。
                        shot = os.path.join(self.artifact_dir,
                                            f'explore_{cur.sid}_{len(self.graph.transitions)}.png')
                        try:
                            self.driver.screenshot(shot)
                        except Exception as e:                 # noqa: BLE001
                            # 证据尽力而为：截不到图就登记为无图新页，
                            # 不能让留痕动作打断探索主链路。
                            self.log(f'  新页首图截取失败（不影响探索）: {str(e)[:60]}')
                            shot = None
                    if fresh:
                        st = self.graph.add_state(
                            new_sig, self._page_title(), shot,
                            path=list(cur.path) + [dict(spec)])
                        self._tarpit_note(new_sig)
                        if tarpit_reason:
                            self.tarpit_hits.append(
                                {'src': cur.sid, 'dst': st.sid, 'control': label,
                                 'spec': dict(spec), 'reason': tarpit_reason})
                    note = f'tarpit 不入队：{tarpit_reason}' if tarpit_reason else ''
                    self.graph.add_edge(cur.sid, st.sid, label, spec, note=note)  # type: ignore[union-attr]
                    self.log(f'  点击「{node.label}」-> {st.sid}「{st.title}」'  # type: ignore[union-attr]
                             f'{"（新页面）" if fresh else "（已知页面）"}'
                             + (f'〔{tarpit_reason}，不入队〕' if tarpit_reason else ''))
                    if fresh and not tarpit_reason:
                        queue.append(st)                                   # type: ignore[arg-type]

                if return_back:
                    if self._back_and_verify(cur, label) == 'lost':
                        self.log('  回不去当前页，本页剩余候选放弃')
                        break

        unreachable = [s.sid for s in self.graph.states.values() if not s.reachable]
        self.log(f'探索结束：发现 {len(self.graph.states)} 个页面、'
                 f'{len(self.graph.transitions)} 条跳转、{steps} 步动作，'
                 f'耗时 {time.time() - t0:.1f}s；'
                 f'覆盖度 {self.coverage.ratio:.1%}'
                 f'（{self.coverage.interactive_visited}/{self.coverage.interactive_total}'
                 f'{f"，其中 {self.coverage.blocked} 个被安全策略拦下" if self.coverage.blocked else ""}）'
                 + (f'；tarpit 拦截 {len(self.tarpit_hits)} 次不入队'
                    if self.tarpit_hits else '')
                 + (f'；权限弹窗作答 {len(self.permission_events)} 次'
                    f'（策略 {self.permission_policy}）'
                    if self.permission_events else '')
                 + (f'；不可达页面 {unreachable}' if unreachable else ''))
        return self.graph

    def _navigate_to(self, state: PageState) -> bool:
        """★ 返回栈回溯：把设备带回 `state` 这一页。

        步骤：回到入口页（`start`）→ 照着 `state.path` 里的控件序列依次点进去
        → 用签名核对确实到了这一页。

        为什么不能只 `back()`：连着点进去三层之后，`back()` 只退一层；
        而 BFS 要探索的是队列里任意一个页面，靠退一层永远回不去。
        """
        try:
            now = self._page_signatures()
        except DriverError:
            now = None
        if now is not None and now.content_key == state.content_key:
            return True                                   # 已经在了，别白跑一遍

        if not state.path:
            # 入口页：回到起点即可
            self._reenter()
            return self._verify_landing(state, '入口页')

        from .action import spec_to_matcher              # 延迟导入，避免循环依赖
        self._reenter()
        for i, spec in enumerate(state.path, 1):
            try:
                # 回溯重放 = 已留档页面上的重复交互 → 前置图可省（分级留存）；
                # 若此步失败，driver.tap 的失败补图兜底，证据只多不少。
                self.driver.tap(spec_to_matcher(spec), post_idle=True,
                                keep_shot=False)
            except Exception as e:
                self.log(f'  回溯第 {i} 步失败: {str(e)[:70]}')
                return False
        return self._verify_landing(state, '回溯')

    def _verify_landing(self, state: PageState, how: str) -> bool:
        try:
            now = self._page_signatures()
        except DriverError:
            return False
        if now.content_key == state.content_key:
            return True
        if now.structural_key == state.structural_key:
            # 结构一致、内容不同：列表首项/未读计数变了，同一页，不是走错
            self.log(f'  {how}落点结构一致、内容有变化，按原页处理')
            return True
        self.log(f'  {how}落点与 {state.sid} 不符（{now.structural_key[:8]}'
                 f' != {state.structural_key[:8]}）')
        return False

    def _back_and_verify(self, cur: PageState, label: str = '') -> str:
        """返回并核对是否真的回到了原页，返回判定结果。

        判定用的是**双签名配合**，不是单独一个：
          exact           内容签名一致 —— 精确回到原页
          content-changed 结构签名一致但内容不同 —— 回到原页，只是文案变了
                          （列表首项、未读计数、时间戳都会这样，不算导航失败）
          recovered       没直接回到，但照路径重走成功
          lost            照路径也回不去 —— 本页剩余的候选控件只能放弃
        """
        try:
            self.driver.back()
            back_sig = self._page_signatures()
        except Exception as e:
            self.log(f'  返回失败: {str(e)[:70]}')
            v = 'recovered' if self._navigate_to(cur) else 'lost'
            self.back_verdicts.append(v)
            return v

        if back_sig.content_key == cur.content_key:
            self.back_verdicts.append('exact')
            return 'exact'
        if back_sig.structural_key == cur.structural_key:
            self.log('  返回后回到原页，但内容有变化（结构签名一致）')
            self.back_verdicts.append('content-changed')
            return 'content-changed'

        self.log('  返回后页面不符，照路径重新进入')
        v = 'recovered' if self._navigate_to(cur) else 'lost'
        self.back_verdicts.append(v)
        return v

    def _reenter(self) -> None:
        try:
            self.driver.start(wait=True)
        except Exception:
            pass

    # ------------------------------------------------------------ 指标

    @property
    def coverage(self) -> CoverageReport:
        """覆盖度：已访可交互 / 全部可交互。

        `pages` 是界面**状态**数（内容签名），`pages_structural` 是**页面**数
        （结构签名）—— **KPI「发现页面数」以后者为准**，理由见 CoverageReport 文档串。

        ★ `per_page` 里的 `total` / `visited` 是**读时算出来的**，不是观察页面时
        拍下的快照（C 复核缺陷，2026-09-23 修）。快照那个写法必然恒为 0：
        `_note_page()` 是在**刚进入某页、还没点任何控件**时调用的，
        那一刻这一页当然一个已访控件都没有，而之后再没人回头更新它。
        `visited` 本来就是 `_done` 的派生量，派生的东西不该被存成快照。
        """
        per_page: Dict[str, Dict[str, Any]] = {}
        for key, bucket in self._per_page.items():
            item = dict(bucket)
            prefix = key + '::'
            item['total'] = len([u for u in self._universe if u.startswith(prefix)])
            item['visited'] = len([u for u in self._done if u.startswith(prefix)])
            per_page[key] = item
        return CoverageReport(interactive_total=len(self._universe),
                              interactive_visited=len(self._done),
                              pages=len(self.graph.states),
                              pages_structural=len(self.graph.structural_index),
                              blocked=len(self._blocked - self._done),
                              per_page=per_page)

    # ------------------------------------------------------------ 导出

    def to_mermaid(self) -> str:
        """导出 Mermaid 状态图 —— 可直接贴进文档或 Markdown 渲染。"""
        states = list(self.graph.states.values())
        if not states:
            return 'stateDiagram-v2\n    [*]'

        lines = ['stateDiagram-v2']

        # 状态声明。标题里的冒号会破坏 Mermaid 语法，替换掉。
        for s in states:
            title = (s.title or s.sid).replace(':', '：').replace('\n', ' ')
            lines.append(f'    {s.sid}: {title}')

        # 起始状态
        lines.append(f'    [*] --> {states[0].sid}')

        # 跳转边。自环不画（页面无变化的点击对状态图无信息量）。
        for t in self.graph.transitions:
            if t.src == t.dst:
                continue
            arrow = '-->' if t.ok else '-x->'
            label = t.control.replace(':', '：').replace('\n', ' ')[:24]
            lines.append(f'    {t.src} {arrow} {t.dst}: {label}')

        return '\n'.join(lines)

    def save_graph(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        payload = self.graph.to_dict()
        payload['mermaid'] = self.to_mermaid()
        payload['skipped_controls'] = _dedup_skipped(self.skipped)
        payload['coverage'] = self.coverage.to_dict()
        payload['tarpit'] = {'policy': self.tarpit_policy.to_dict(),
                             'hits': list(self.tarpit_hits)}
        if self.last_budget is not None:
            payload['budget'] = self.last_budget.to_dict()
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        self.log(f'状态图已保存: {path}')
        return path

    def generate_case(self, path: str, name: str = '自动探索轨迹') -> str:
        """把探索轨迹串成一条**可重放**的用例（只串首尾相接的连续边）。

        ★ C 派活 B-1（2026-09-26，高危）：原实现把 `graph.transitions`
        （**边集**）按列表顺序当成一条线性轨迹串。边与边之间没有顺序保证，
        最简反例就是同一个来源页扇出两条边：

            S1 --btn_to_S2--> S2
            S1 --btn_to_S3--> S3

        产物第二步要求「当前页是 S2」才能点 `btn_to_S3`，而它只存在于 S1
        → **必然重放失败**。而 docstring 当时写的是「串成一条可重放用例」，
        又是一次「声称做到了、实际没做」。

        现在的做法：
          ① 只纳入**首尾相接**的连续边（贪心走一条真实路径）；
          ② 走不通的边**一条都不许静默丢** —— 写进产物 `note` 与日志
             （红线：只打印警告不处置等于不存在）；
          ③ 要想覆盖全部边，用 `generate_cases()` 拆成多条用例（各自可重放）。
        """
        from .action import dump_case
        edges = _usable_transitions(self.graph)
        picked, skipped = _chain_transitions(edges, _pick_start(edges, self.graph))

        steps: List[Dict[str, Any]] = [{'start': True}]
        if picked:
            # 链的起点若不是入口页，先照记录下来的路径导航过去 —— 否则第一条
            # tap 就同样「要求当前页不对」。没有路径信息时（如单测桩）退化为
            # 直接从入口开始，并在 note 里说明。
            steps += _nav_steps_to(self.graph, picked[0].src)
        for t in picked:
            steps.append({'tap': t.control_spec})
            steps.append({'waitIdle': True})

        case: Dict[str, Any] = {'name': name, 'bundle': self.driver.bundle,
                                'ability': self.driver.ability, 'steps': steps}
        note = _coverage_note(len(picked), len(edges), skipped)
        if note:
            case['note'] = note
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(dump_case(case))
        self.log(f'用例已生成: {path}（{len(steps)} 步，'
                 f'覆盖 {len(picked)}/{len(edges)} 条跳转边）')
        if skipped:
            self.log('未纳入的跳转边（与前一条不首尾相接，串进来必然重放失败）：'
                     + '、'.join(f'{_edge_label(t)}({t.src}→{t.dst})' for t in skipped))
        return path

    def generate_cases(self, out_dir: str,
                       name_prefix: str = '自动探索轨迹') -> List[str]:
        """把探索图拆成**多条各自可重放**的用例，返回产物路径列表。

        为什么要拆：边集里有分叉（同一个页面扇出多条边）时，
        **一条用例不可能重放所有边** —— 要么静默丢边（这就是原缺陷），
        要么跑不通。拆成「一条路径一条用例」，每条都真能跑，
        总体覆盖率还比硬塞进一条更高。

        每条用例都带 `note`：写清自己覆盖了哪几条边、总共有几条 ——
        读的人一眼知道「不是全都覆盖了」。
        """
        from .action import dump_case
        edges = _usable_transitions(self.graph)
        chains = _all_chains(edges, _pick_start(edges, self.graph))
        chains.sort(key=len, reverse=True)          # 覆盖最多的那条排前面
        os.makedirs(out_dir, exist_ok=True)

        paths: List[str] = []
        for i, chain in enumerate(chains, 1):
            steps: List[Dict[str, Any]] = [{'start': True}]
            steps += _nav_steps_to(self.graph, chain[0].src)
            for t in chain:
                steps.append({'tap': t.control_spec})
                steps.append({'waitIdle': True})
            case: Dict[str, Any] = {
                'name': f'{name_prefix}-{i}',
                'bundle': self.driver.bundle, 'ability': self.driver.ability,
                'steps': steps,
                'note': _chain_note(i, len(chains), chain, edges)}
            path = os.path.join(out_dir, f'{name_prefix}-{i}.yaml')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(dump_case(case))
            paths.append(path)

        covered = sum(len(c) for c in chains)
        self.log(f'已拆出 {len(chains)} 条用例，覆盖 {covered}/{len(edges)} 条跳转边'
                 + ('（全部覆盖）' if covered >= len(edges) else '（有边不可用，见各用例 note）'))
        return paths

    def _dump_case(self, path: str, case: Dict[str, Any]) -> None:
        """写出用例文件（YAML，无 PyYAML 时退 JSON）。

        说明：`generate_case` / `generate_cases` **不调它** —— 那两个函数是
        模块级逻辑，会被单测用 `SimpleNamespace` 桩直接以「未绑定方法」调用，
        因此不能依赖 `self` 上的其它方法。这里保留一份，给持有真实例的调用方用。
        """
        from .action import dump_case
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(dump_case(case))


def _dedup_skipped(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for it in items:
        k = (it.get('label'), it.get('reason'))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


# ================================================================ 探索图 → 用例
#
# C 派活 B-1（2026-09-26）：`transitions` 是**边集**，不是轨迹。
# 下面这组函数负责把边集切成「首尾相接的路径」，并把接不上的边**显式记账**。

def _nav_steps_to(graph: Any, sid: str) -> List[Dict[str, Any]]:
    """返回「从入口页走到 `sid` 这一页」的控件序列步骤。

    用的是 B1 那套返回栈回溯记录下来的 `PageState.path` —— 同一份数据，
    探索时用来回退、生成用例时用来当导航前缀，不必另造一套。

    做成**模块级函数**而不是方法：`generate_case` 会被单测用
    `SimpleNamespace` 桩以「未绑定方法」的方式直接调用（见
    `C交付-给B-2026-09-27/verify_b_fixes.py`），那时 `self` 上没有别的方法。

    拿不到路径（没有 states / 该页就是入口页 / 桩对象）时返回空 list ——
    退化成「直接从入口开始」，不猜。
    """
    states = getattr(graph, 'states', None)
    if not states:
        return []
    for st in states.values():
        if getattr(st, 'sid', '') != sid:
            continue
        out: List[Dict[str, Any]] = []
        for spec in list(getattr(st, 'path', None) or []):
            out.append({'tap': spec})
            out.append({'waitIdle': True})
        return out
    return []


def _pick_start(edges: List[Transition],
                graph: Any = None) -> Optional[str]:
    """挑一个「自然的起点页」，**与输入顺序无关**。

    这一步不能省：`transitions` 是边集，`edges[0].src` 未必是入口页。
    从半路的页面起步，产物第一条 tap 就要求「当前页」是对的 ——
    没有路径信息时补不出来，产物就是不可重放的。

    三级判据（越靠前越可信）：
      ① 有 `graph.states` 时用入口页（`path` 为空的那个，即 S1）；
      ② 没有入边的源点（图的自然起点）—— 纯拓扑，不依赖顺序；
      ③ 环形图没有源点 → 退回第一条边的 src（顺序依赖无法消除，但会在
         `note` 里说明，不假装它是入口）。
    """
    states = getattr(graph, 'states', None)
    if states:
        for st in states.values():
            if not getattr(st, 'path', None):
                return str(getattr(st, 'sid', '') or '')
    dsts = {t.dst for t in edges}
    for t in edges:
        if t.src not in dsts:
            return t.src
    return edges[0].src if edges else None


def _usable_transitions(graph: Any) -> List[Transition]:
    """能拿去当回归步骤的边：成功、不是自环、带控件规格。"""
    out = []
    for t in getattr(graph, 'transitions', None) or []:
        if not getattr(t, 'ok', False):
            continue
        if t.src == t.dst:                       # 自环（点了没跳页）不是一步导航
            continue
        if not getattr(t, 'control_spec', None):
            continue
        out.append(t)
    return out


def _chain_transitions(edges: List[Transition],
                       start: Optional[str] = None
                       ) -> Tuple[List[Transition], List[Transition]]:
    """把边集贪心串成**首尾相接**的一条路径，返回 (纳入的边, 剩下的边)。

    从当前页出发，每次只挑「当前页出发」的边；挑不到就停 ——
    剩下的边**接不上**，串进同一条用例必然重放失败。

    `start=None` 时**不拿 `edges[0].src` 当起点**，而是走 `_pick_start()`
    ——那是「边集没有顺序保证」这条缺陷的根：同一次探索，`transitions`
    的列表顺序可能变，但产出的用例不该跟着变。
    """
    rest = list(edges)
    if not rest:
        return [], []
    if start is None:
        start = _pick_start(edges)
    picked: List[Transition] = []
    cur = start
    while rest:
        t = next((x for x in rest if x.src == cur), None)
        if t is None:
            break                                # 这条链走到头了
        rest.remove(t)
        picked.append(t)
        cur = t.dst
    return picked, rest


def _all_chains(edges: List[Transition],
                start: Optional[str] = None) -> List[List[Transition]]:
    """把边集切成若干条首尾相接的路径（贪心取完为止，不丢边）。

    只有**第一条**链用 `start`（入口页）—— 后面的链是从「上一条链接不上的
    残余边」里起的，本来就没有天然起点。
    """
    chains: List[List[Transition]] = []
    rest = list(edges)
    cur_start = start
    while rest:
        picked, rest = _chain_transitions(rest, cur_start)
        cur_start = None
        if not picked:                           # 防御：不该发生
            break
        chains.append(picked)
    return chains


def _edge_label(t: Transition) -> str:
    """边的人话标识 —— 优先用控件规格里的 text / id（就是产物里点的那个东西）。"""
    spec = t.control_spec if isinstance(t.control_spec, dict) else {}
    for k in ('text', 'id', 'descr'):
        if spec.get(k):
            return str(spec[k])
    return str(getattr(t, 'control', '') or '(未命名控件)')


def _coverage_note(picked_n: int, total_n: int, skipped: List[Transition]) -> str:
    """把「没纳入哪些边」写清楚。**不许静默丢** —— 这是 B-1 的硬要求。"""
    if total_n == 0:
        return '探索图里没有可用的跳转边（成功、非自环、带控件规格），本用例只有启动步骤'
    if not skipped:
        return f'覆盖全部 {total_n} 条跳转边'
    lines = [f'只覆盖 {picked_n}/{total_n} 条跳转边。以下 {len(skipped)} 条**未纳入**：'
             f'它们与前一条边不首尾相接，串进同一条用例必然重放失败'
             f'（第一步就要求「当前页」是错的）——']
    for t in skipped:
        lines.append(f'  - {_edge_label(t)}：{t.src} → {t.dst}')
    lines.append('要覆盖它们，用 Explorer.generate_cases() 拆成多条用例'
                 '（每条路径一条，各自可重放）。')
    return '\n'.join(lines)


def _chain_note(idx: int, total_chains: int, chain: List[Transition],
                edges: List[Transition]) -> str:
    """单条拆出用例的说明：我覆盖了哪几条边、全局一共几条。"""
    covers = '、'.join(f'{_edge_label(t)}({t.src}→{t.dst})' for t in chain)
    return (f'本用例是探索图拆出的第 {idx}/{total_chains} 条路径，'
            f'覆盖 {len(chain)}/{len(edges)} 条跳转边：{covers}\n'
            f'其余边在同目录的其它用例里 —— 总覆盖率看全部，别只看这一条。')
