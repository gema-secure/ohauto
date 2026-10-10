"""系统权限弹窗的识别与处置。

与 `explorer.detect_dialog()` 的分工
------------------------------------
`detect_dialog()` 回答的是「当前页面是不是弹窗压在应用上」，用于决定点击优先级；
本模块回答的是「这个弹窗是不是**系统权限门**、该不该替用户作答」。
前者是页面结构问题，后者是策略与设备状态问题 —— 混在一起两边都难改。

识别依据（真机实测，夹具见 `tests/fixtures/permission/`）
--------------------------------------------------------
① 顶层窗口的 `bundleName` 属于系统权限 UI 进程（`com.ohos.permissionmanager`）；
② 该窗口子树里存在可见且可点的「允许类」或「禁止类」按钮。

两条都命中才判为权限弹窗。只看 ① 会把权限管理器的普通设置页当成弹窗；
只看 ② 会把应用自己画的「允许/取消」对话框当成系统权限门 —— 后者是业务操作，
不该由本模块代答。

为什么必须替它作答
------------------
真机实测（`ohos.samples.distributedcalc` 冷启动）：权限弹窗盖住整个内容区，
`dumpLayout` 返回的是**弹窗的树**。此时定位全部落空、探索把弹窗当页面来回走、
压测分片整段失败 —— 整条链路卡在一个人工点一下就能过的地方。

策略
----
`record` 只记录不动（保持「观察到」语义，不替用户作答）；`allow` / `deny`
按文案点掉对应按钮。默认 `deny`：**不在一台陌生设备上新增授权**，同时把弹窗
消掉让链路继续。取值可用环境变量 `OHAUTO_PERMISSION_POLICY` 覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .layout import LayoutNode, flatten

__all__ = ['PermissionDialogVerdict', 'detect_permission_dialog', 'resolve_policy',
           'POLICY_RECORD', 'POLICY_ALLOW', 'POLICY_DENY', 'POLICIES',
           'DEFAULT_POLICY', 'ENV_POLICY', 'PERMISSION_UI_BUNDLES',
           'ALLOW_LABELS', 'DENY_LABELS']

#: 系统权限 UI 进程，按前缀匹配以兼容厂商变体。
PERMISSION_UI_BUNDLES: Tuple[str, ...] = (
    'com.ohos.permissionmanager',
    'com.android.permissioncontroller',
)

#: 作答类按钮文案。匹配用**精确相等**（不是包含）：「允许“计算器”使用多设备
#: 协同？」这种标题里也含「允许」，包含匹配会直接把标题当成按钮。
ALLOW_LABELS: Tuple[str, ...] = (
    '允许', '仅本次', '仅在使用时允许', '始终允许', '允许一次', '同意', '确定',
    'allow', 'ok', 'while using the app', 'only this time',
)
DENY_LABELS: Tuple[str, ...] = (
    '禁止', '不允许', '拒绝', '取消', '以后再说', 'deny', "don't allow", 'cancel',
)

POLICY_RECORD = 'record'
POLICY_ALLOW = 'allow'
POLICY_DENY = 'deny'
POLICIES: Tuple[str, ...] = (POLICY_RECORD, POLICY_ALLOW, POLICY_DENY)

#: 默认策略：点「禁止」。不替用户新增授权，但把弹窗消掉让链路继续。
DEFAULT_POLICY = POLICY_DENY

#: 覆盖策略取值的环境变量（仅当前进程，不写任何配置文件）。
ENV_POLICY = 'OHAUTO_PERMISSION_POLICY'


def resolve_policy(value: Optional[str] = None) -> str:
    """定策略：显式参数 > 环境变量 > 默认值。

    非法取值**当场报错**，不静默退回默认 —— 策略写错会直接改变设备状态，
    必须让人看见，而不是让人以为「deny 生效了」。
    """
    chosen = (value or os.environ.get(ENV_POLICY) or DEFAULT_POLICY)
    chosen = chosen.strip().lower()
    if chosen not in POLICIES:
        raise ValueError('未知的权限弹窗策略 %r，可选：%s（可用环境变量 %s 指定）'
                         % (chosen, ' / '.join(POLICIES), ENV_POLICY))
    return chosen


@dataclass
class PermissionDialogVerdict:
    """当前页面是否存在系统权限弹窗，以及允许/禁止按钮各是哪个节点。"""
    is_permission_dialog: bool = False
    owner: str = ''
    title: str = ''
    allow: Optional[LayoutNode] = None
    deny: Optional[LayoutNode] = None
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {'is_permission_dialog': self.is_permission_dialog,
                'owner': self.owner, 'title': self.title,
                'allow': self.allow.text.strip() if self.allow else '',
                'deny': self.deny.text.strip() if self.deny else '',
                'evidence': list(self.evidence)}


def _window_owner(node: LayoutNode) -> str:
    """取窗口属主 bundle —— 真机上只有顶层窗口根节点带 `bundleName`。"""
    return str(node.attributes.get('bundleName') or '').strip()


def _label_of(text: str, labels: Sequence[str]) -> Optional[str]:
    """精确相等匹配按钮文案，命中则返回标签原文。"""
    t = text.strip()
    low = t.lower()
    for label in labels:
        if t == label or low == label:
            return label
    return None


def _dialog_title(nodes: List[LayoutNode], skip: Sequence[str]) -> str:
    """标题启发式：第一个够长、且不是作答按钮文案的文本节点。"""
    for n in nodes:
        t = n.text.strip()
        if len(t) >= 2 and t not in skip:
            return t
    return ''


def detect_permission_dialog(
        root: Optional[LayoutNode],
        bundles: Sequence[str] = PERMISSION_UI_BUNDLES
) -> PermissionDialogVerdict:
    """判断当前控件树是否是「系统权限弹窗」，并给出可点的作答按钮。

    只认**可见且可点**的按钮：不可见的按钮点了等于没点，那不该算「认出来了」。
    """
    if root is None:
        return PermissionDialogVerdict()
    for win in root.children:
        owner = _window_owner(win)
        if not owner or not any(owner.startswith(b) for b in bundles):
            continue
        nodes = list(flatten(win, only_visible=False))
        tappable = [n for n in nodes
                    if (n.clickable or n.is_interactive()) and n.visible and n.enabled]
        allow = deny = None
        for n in tappable:
            if allow is None and _label_of(n.text, ALLOW_LABELS) is not None:
                allow = n
            elif deny is None and _label_of(n.text, DENY_LABELS) is not None:
                deny = n
        if allow is None and deny is None:
            # 属主是权限 UI 但没有任何作答按钮：多半是权限管理器的普通页面
            continue
        skip = [n.text.strip() for n in (allow, deny) if n is not None]
        # 标题在 Text 节点上（不可点），所以标题要在**全部节点**里找，
        # 不能只在可点集合里找 —— 否则认得出弹窗却报不出它在问什么。
        title = _dialog_title(nodes, skip)
        evidence = ['窗口属主 %s' % owner]
        if allow is not None:
            evidence.append('允许类按钮「%s」' % allow.text.strip())
        if deny is not None:
            evidence.append('禁止类按钮「%s」' % deny.text.strip())
        if title:
            evidence.append('标题「%s」' % title)
        return PermissionDialogVerdict(True, owner, title, allow, deny, evidence)
    return PermissionDialogVerdict()
