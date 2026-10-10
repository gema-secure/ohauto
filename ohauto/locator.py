"""定位器健康度与自愈
==========================================

同类项目只有「换策略重试」，**没有健康度模型 +
自动替换**——这是三大差异点之一，验收标准是「人为改 5 个控件 id，
不人工干预自动恢复，成功率 ≥ 80%」。

对外接口（对外接口，签名不可改）::

    locate(target, page) -> LocateResult
    record_locator_failure(locator_id, reason) -> None
    repair_locators(page_signature) -> RepairReport

核心模型
--------

**降级链**（每往下一级记一次降级）::

    L1  id 精确          Matcher.id(exact)         快、可解释、最脆
    L2  id 模糊 + text   id 包含匹配，或 text 命中   版本升级改前缀后还能活
    L3  层级路径 + 类型  type_fingerprint + control_key（B 已交付，
        from ohauto.explorer import）——id 全改掉也能按「树里的位置和类型」找回
    L4  视觉语义         HybridLocator（需要截图与已配置的 Provider）
    L5  坐标兜底         记录时的 rect 中心，**必记告警**——
        坐标是红线第 5 条明令不信任的东西，只作为最后的一搏

**健康度**：每个定位器独立记账 —— 尝试数 / 成功率 / 最近失败原因 /
平均耗时 / 降级次数 / 连续失败数。连续失败达阈值 → 触发自愈。

**自愈**（repair_locators）：拿新鲜的控件树，按 type_fingerprint
（跨页面同类锚点）+ control_key（页面内稳定标识）重新圈定候选，
唯一候选即替换定位器并验证。全程不人工干预。

设计约定（与全组对齐）：
- 不缓存坐标（红线第 5 条）：L5 的兜底坐标只是 spec 里的一次性记录，
  每次定位都基于**当次**传入的新鲜控件树；
- 失败自动记账（幂等口径，2026-09-23 定稿 / 2026-09-27 A-0 修正）：
  locate 全链路落空时内部也会 record_locator_failure，执行器按契约
  回写是第二重保险。去重键 = **(来源, 代次, locator_id)** ——
  内部 locate 路径用 `_locate_seq`，执行器回写路径用 runner 传进来的
  `attempt`（A-0 之后新增的可选参数）。2026-09-23 那版只认 `_locate_seq`，
  而执行器**从不调 locate()**，代次恒为 0 → 回写被全量误判成重复记账 →
  连续失败停在 1 → 自愈在生产链路上永不触发（归因链路断点）。
  调用方签名不变（新参数可选、有缺省），A3 阈值不会被翻倍触发；
- 自愈换代必须**先快照、验证通过才提交**：验证失败时恢复快照、
  generation 不自增、consecutive_failures 不清零 —— 账本与真实状态
  必须一致（修复前是「报已回滚但没回滚」）；
- 复用 type_fingerprint / control_key，不另造指纹——
  同一个控件在覆盖度统计（B）和自愈（A）里必须是同一个身份。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .layout import LayoutNode, Rect, flatten
from .matcher import ON, Matcher
from .identity import type_fingerprint, control_key  # S7：身份指纹下沉到 identity，不再反向 import explorer
from .vision import CONF_MIN_DEFAULT, IOU_MIN_DEFAULT

__all__ = ['LocateResult', 'LocatorSpec', 'LocatorHealth', 'RepairReport',
           'LocatorManager', 'LocatorMissError']

_DEGRADE_NAMES = ('id_exact', 'id_fuzzy_text', 'path_type', 'vision', 'coordinate')

#: 自愈换通道（第 5 档）的两个门槛。视觉框要**映射回一个**控件树节点，
#: 且映射必须够唯一、置信度够高 —— 否则宁可如实失败：
#: 自愈猜错等于把一条好定位器换成坏的，比不换更糟。
#: 取值直接来自视觉通道的默认口径（单一出处），不在这里另写一份字面量。
VISION_IOU_MIN = IOU_MIN_DEFAULT
VISION_CONF_MIN = CONF_MIN_DEFAULT


class LocatorMissError(Exception):
    """降级链五级全部落空。携带 locator_id 方便上层回写。"""

    def __init__(self, locator_id: str, message: str):
        super().__init__(message)
        self.locator_id = locator_id


# ---------------------------------------------------------------- 数据结构

@dataclass
class LocatorSpec:
    """一个定位器的完整描述（记录「这个控件长什么样」的全部线索）。"""
    locator_id: str
    description: str                 # 人类可读描述，L4 视觉通道的输入
    page_signature: str = ''         # 页面归属（B1 内容签名或等价物）
    target_id: str = ''
    text: str = ''
    #: **子树文案聚合**（`LayoutNode.text_deep`）。真机上「可交互容器自身
    #: text/id 全空、文案在子节点」是常态（id 覆盖仅 5.62%），
    #: 自愈重探索时这一条往往是唯一能区分同类型兄弟的线索 ——
    #: 见 `_pick_repair_candidate` 的第 2 档。
    text_deep: str = ''
    descr: str = ''
    target_type: str = ''
    type_fp: str = ''                # explorer.type_fingerprint：跨页面同类锚点
    control_key: str = ''            # explorer.control_key：页面内稳定标识
    parent_types: Tuple[str, ...] = ()   # 父链类型序列（L3 的层级路径证据）
    fallback_center: Optional[Tuple[int, int]] = None   # L5 兜底（记告警）
    generation: int = 0              # 第几代定位器（自愈一次 +1）

    def describe_chain(self) -> str:
        return (f'id={self.target_id!r} text={self.text!r} '
                f'type={self.target_type!r} type_fp={self.type_fp[:8]}')


@dataclass
class LocatorHealth:
    """单个定位器的健康账本。"""
    attempts: int = 0
    successes: int = 0
    consecutive_failures: int = 0
    last_failure_reason: str = ''
    last_failure_at: float = 0.0
    total_time_ms: float = 0.0
    degrade_count: int = 0           # 降级总次数（每一级落空记一次）
    coordinate_fallbacks: int = 0    # L5 兜底次数（告警口径）
    repairs: int = 0                 # 自愈成功次数

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0

    @property
    def avg_time_ms(self) -> float:
        return self.total_time_ms / self.attempts if self.attempts else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {'attempts': self.attempts, 'successes': self.successes,
                'success_rate': round(self.success_rate, 4),
                'consecutive_failures': self.consecutive_failures,
                'last_failure_reason': self.last_failure_reason,
                'avg_time_ms': round(self.avg_time_ms, 1),
                'degrade_count': self.degrade_count,
                'coordinate_fallbacks': self.coordinate_fallbacks,
                'repairs': self.repairs}


@dataclass
class LocateResult:
    """契约类型：rect / channel / confidence / health 四个字段必须有。"""
    rect: Rect
    channel: str                     # 'tree' | 'vision' | 'hybrid' | 'coordinate'
    confidence: float
    health: LocatorHealth
    locator_id: str = ''
    level: str = ''                  # 命中的降级链级别（_DEGRADE_NAMES 之一）
    node: Optional[LayoutNode] = None
    uncertain: bool = False          # 视觉命中且无控件树背书
    warning: str = ''
    elapsed_ms: float = 0.0


@dataclass
class RepairReport:
    """repair_locators 的产出，C 集成日手动触发时也看这个。"""
    page_signature: str
    repaired: List[str] = field(default_factory=list)   # 成功换代的 locator_id
    failed: Dict[str, str] = field(default_factory=dict)  # id -> 失败原因
    #: id -> 并列候选（`type#id` 或 `type«子树文案»`）。自愈失败时**必须**留下它 ——
    #: 「有 3 个同分候选」比「线索全失效」可操作得多（人能一眼看出还差什么线索）。
    ambiguous: Dict[str, List[str]] = field(default_factory=dict)
    details: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.repaired) or not self.failed


# ---------------------------------------------------------------- 主类

class LocatorManager:
    """定位器注册表 + 健康度账本 + 自愈引擎。

    Parameters
    ----------
    vision:
        可选的 HybridLocator（vision.py）。不传则 L4 自动跳过。
    failure_threshold:
        连续失败达到该值即触发自愈（验收场景：改 id 后连续落空
        到阈值，自动换新一代定位器，不人工干预）。
    clock:
        时间源，测试可注入假时钟。
    """

    def __init__(self, vision: Optional[Any] = None,
                 failure_threshold: int = 3,
                 clock: Callable[[], float] = time.time,
                 verbose: bool = False):
        self.vision = vision
        self.failure_threshold = failure_threshold
        self.clock = clock
        self.verbose = verbose
        self._specs: Dict[str, LocatorSpec] = {}
        self._health: Dict[str, LocatorHealth] = {}
        # 定位器的业务键 = page_signature + 目标描述（同一描述同页只一个定位器）
        self._by_key: Dict[Tuple[str, str], str] = {}
        self._last_root: Optional[LayoutNode] = None
        self._counter = 0
        # 失败记账幂等闸（已扩执行器来源）：
        # _locate_seq 每次 locate() 自增；_fail_seq 记录每个 locator 最近一次
        # 「真正计入」的失败属于哪个 (来源, 代次) —— 来源 0 = 内部 locate，
        # 1 = 执行器回写（带 attempt）。两条路径各自单调、互不干扰。
        self._locate_seq = 0
        self._fail_seq: Dict[str, Tuple[int, int]] = {}

    def _health_of(self, locator_id: str) -> LocatorHealth:
        """`_health` 的唯一访问器 —— 缺失就地补建，杜绝裸索引。

        为什么要有它（A-2，C 2026-09-26 高危）：`self._health[lid]` 这种
        裸索引只要有一处漏补账本，就会在运行时炸成 KeyError
        （`_resolve_spec` 当初就只补了 `_specs` 没补 `_health`，
        于是「传未注册 spec 进 locate()」第一行必崩）。收敛到这一个
        访问器之后，**账本缺失只会就地补建、不再升级成崩溃**；
        而 spec 的缺失仍然照旧抛错 —— 那是调用方给错了 id，本该炸。

        注意：`_count_failure` 刻意**不走**这里，仍然用 `.get()` 判空后
        直接返回 False（未知 id 的失败回写必须是无副作用的 no-op，
        不能顺手把账本建出来，否则 「缺 id 不调」的契约就形同虚设）。
        """
        h = self._health.get(locator_id)
        if h is None:
            h = LocatorHealth()
            self._health[locator_id] = h
        return h

    # ------------------------------------------------------ 日志
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f'[locator] {msg}')

    # ------------------------------------------------------ 注册
    def register(self, target: Any, page: LayoutNode,
                 page_signature: str = '') -> LocatorSpec:
        """从目标描述 + 当前控件树生成定位器。

        target 可以是：
        - str：人类描述（'登录按钮'），线索从树上按描述模糊圈定；
        - dict：{'id':..., 'text':..., 'type':...} 显式线索；
        - LayoutNode：以某个实际节点为模板。
        """
        if isinstance(target, dict):
            clue = target
            desc = str(clue.get('description') or clue.get('id')
                       or clue.get('text') or clue.get('type') or '?')
        elif isinstance(target, LayoutNode):
            clue = {'id': target.id, 'text': target.text,
                    'descr': target.descr, 'type': target.type}
            desc = target.label
        else:
            clue = {}
            desc = str(target)
        key = (page_signature, desc)
        if key in self._by_key:
            return self._specs[self._by_key[key]]

        # 按线索在当前树上找模板节点（找不到模板就只记线索本身）
        node = self._find_template(clue, desc, page)
        spec = LocatorSpec(
            locator_id=self._new_id(desc),
            description=desc,
            page_signature=page_signature,
            target_id=str(clue.get('id', '') or ''),
            text=str(clue.get('text', '') or ''),
            descr=str(clue.get('descr', '') or clue.get('description', '') or ''),
            target_type=str(clue.get('type', '') or ''),
        )
        if node is not None:
            spec.type_fp = type_fingerprint(node)
            spec.control_key = control_key(node)
            spec.text_deep = (getattr(node, 'text_deep', '') or '').strip()
            spec.parent_types = tuple(p.type for p in self._ancestors(node))
            spec.fallback_center = node.rect.center
        self._specs[spec.locator_id] = spec
        self._health_of(spec.locator_id)      # 注册表与账本同生共死（A-2 口径）
        self._by_key[key] = spec.locator_id
        self._log(f'注册定位器 {spec.locator_id} ({spec.describe_chain()})')
        return spec

    def _new_id(self, desc: str) -> str:
        self._counter += 1
        safe = ''.join(c if c.isalnum() else '_' for c in desc)[:24]
        return f'L{self._counter:03d}_{safe}'

    @staticmethod
    def _ancestors(node: LayoutNode) -> List[LayoutNode]:
        out, p = [], node.parent
        while p is not None:
            out.append(p)
            p = p.parent
        return out

    def _find_template(self, clue: Dict[str, str], desc: str,
                       page: LayoutNode) -> Optional[LayoutNode]:
        """按线索找模板节点，圈不定唯一就放弃（宁缺毋滥）。"""
        m = ON.interactive()
        if clue.get('id'):
            m = ON.id(clue['id'])
        elif clue.get('text'):
            m = ON.text_contains(clue['text'])
        elif clue.get('descr'):
            m = ON.descr(clue['descr'])
        elif desc:
            m = ON.label(desc)
        cands = m.filter(flatten(page, only_visible=True))
        return cands[0] if len(cands) == 1 else None

    # ------------------------------------------------------ 降级链
    def locate(self, target: Any, page: LayoutNode,
               page_signature: str = '',
               image_path: Optional[str] = None,
               screen_size: Tuple[int, int] = (0, 0)
               ) -> Optional[LocateResult]:
        """契约接口：五级降链定位。

        全链路落空时返回 None（同时内部自动记一次失败——自愈的触发
        不依赖调用方是否按契约回写；与执行器回写之间按代次幂等，
        见 record_locator_failure）。
        """
        self._locate_seq += 1          # 幂等闸的代次（一次 locate = 一次定位尝试）
        self._last_root = page
        spec = self._resolve_spec(target, page, page_signature)
        # A-2：这里原来是对 _health 的裸索引 —— 传未注册 LocatorSpec 时
        # _resolve_spec 只补了 _specs，账本还没有，于是第一行就 KeyError。
        health = self._health_of(spec.locator_id)
        t0 = time.perf_counter()

        # ---- L1 id 精确
        node = self._lv_id_exact(spec, page)
        level = 'id_exact'
        # ---- L2 id 模糊 + text
        if node is None:
            self._degrade(spec)
            node, level = self._lv_id_fuzzy_text(spec, page)
        # ---- L3 层级路径 + 类型
        if node is None:
            self._degrade(spec)
            node, level = self._lv_path_type(spec, page)
        # ---- L4 视觉语义
        vision_target = None
        if node is None:
            self._degrade(spec)
            vision_target = self._lv_vision(spec, image_path, screen_size)
        # ---- L5 坐标兜底
        coordinate_fallback = False
        if node is None and vision_target is None:
            self._degrade(spec)
            coordinate_fallback = spec.fallback_center is not None

        elapsed = (time.perf_counter() - t0) * 1000.0

        # ---- 全部五级落空（连兜底坐标都没记录过）
        if node is None and vision_target is None and not coordinate_fallback:
            self.record_locator_failure(spec.locator_id, '全链路落空(5级)')

        # ---- 坐标兜底：先按失败记账（它是降级链落空的告警，不是成功），
        #      连续失败到阈值就就地自愈 —— 「不人工干预自动恢复」的主路径。
        #      不这样记账的后果：坐标兜底永远「成功」，健康度全绿，
        #      自愈永远不触发 —— 又一次「测试全绿但结论是假的」。
        if coordinate_fallback:
            self.record_locator_failure(spec.locator_id,
                                        '降级链落空，坐标兜底（告警）')
            if health.consecutive_failures >= self.failure_threshold:
                node = self._try_self_heal(spec, page)
                if node is not None:
                    level = 'repaired'
                    coordinate_fallback = False

        # ---- 仍然全部落空（连兜底坐标都没有）：自愈一次（若已到阈值），
        #      救不回来就认输。注意：坐标兜底不是「落空」，不能进这个分支。
        if (node is None and vision_target is None
                and not coordinate_fallback):
            if health.consecutive_failures >= self.failure_threshold:
                node = self._try_self_heal(spec, page)
                if node is not None:
                    level = 'repaired'
            if node is None and vision_target is None:
                return None

        if node is not None:
            # 自愈成功的那次命中已在 repair_locators 里记过账（验证通过），
            # 这里再记会把同一次命中算两次 —— 只在非自愈路径记账。
            if level != 'repaired':
                self._record_success(spec.locator_id, elapsed)
            warning = ''
            channel = 'tree'
            if level in ('id_fuzzy_text', 'path_type'):
                warning = f'命中于降级级别 {level}'
            if level == 'repaired':
                warning = (f'自愈后命中（第 '
                           f'{self._specs[spec.locator_id].generation} 代定位器）')
            return LocateResult(
                rect=node.rect, channel=channel,
                confidence={'id_exact': 0.99, 'id_fuzzy_text': 0.85,
                            'path_type': 0.7, 'repaired': 0.8}.get(level, 0.5),
                health=health, locator_id=spec.locator_id, level=level,
                node=node, warning=warning, elapsed_ms=elapsed)

        if vision_target is not None:
            # 视觉命中
            self._record_success(spec.locator_id, elapsed)
            return LocateResult(
                rect=vision_target.rect,
                channel='hybrid' if not vision_target.uncertain else 'vision',
                confidence=vision_target.confidence, health=health,
                locator_id=spec.locator_id, level='vision',
                node=None, uncertain=vision_target.uncertain,
                warning='视觉命中' + ('（无控件树背书）' if vision_target.uncertain else ''),
                elapsed_ms=elapsed)

        # 走到这里 = 坐标兜底成立、且自愈没救回来：按告警口径返回兜底点
        assert coordinate_fallback
        health.coordinate_fallbacks += 1
        self._log(f'{spec.locator_id} 坐标兜底 ({spec.fallback_center[0]},'
                  f'{spec.fallback_center[1]}) —— 已告警')
        return LocateResult(
            rect=Rect(spec.fallback_center[0], spec.fallback_center[1],
                      spec.fallback_center[0], spec.fallback_center[1]),
            channel='coordinate', confidence=0.2, health=health,
            locator_id=spec.locator_id, level='coordinate',
            uncertain=True, warning='坐标兜底：不可信，仅救急',
            elapsed_ms=elapsed)

    def _try_self_heal(self, spec: LocatorSpec,
                       page: LayoutNode) -> Optional[LayoutNode]:
        """到阈值后的自愈 + 重试。成功返回命中节点，失败返回 None。"""
        health = self._health_of(spec.locator_id)
        self._log(f'{spec.locator_id} 连续失败 '
                  f'{health.consecutive_failures} 次，触发自愈')
        rep = self.repair_locators(spec.page_signature, page=page)
        self._log(f'自愈结果: repaired={rep.repaired} failed={rep.failed}')
        if spec.locator_id in rep.repaired:
            return self._best_after_repair(self._specs[spec.locator_id], page)
        return None

    def _resolve_spec(self, target: Any, page: LayoutNode,
                      page_signature: str) -> LocatorSpec:
        """按 target 找到（或注册）对应的 LocatorSpec。

        A-2（C 2026-09-26 高危）：`locate(target, ...)` 的 `target` 是
        `Any`，且显式支持传 `LocatorSpec` —— 这是**受支持的输入形态**，
        不是误用。修复前这条旁路只 `setdefault` 了 `_specs`，没补
        `_health`，于是 locate() 第一行的账本索引直接 KeyError。
        现在两个字典一起补，与 `register()` 的记账口径对齐。
        """
        if isinstance(target, LocatorSpec):
            lid = target.locator_id
            if lid not in self._specs:
                self._specs[lid] = target
            self._health_of(lid)          # 账本与注册表必须同生共死
            return self._specs[lid]
        desc = (target if isinstance(target, str)
                else str(target.get('id') or target.get('text') or target.get('type') or '?')
                if isinstance(target, dict) else getattr(target, 'label', '?'))
        key = (page_signature, desc)
        lid = self._by_key.get(key)
        if lid:
            return self._specs[lid]
        return self.register(target, page, page_signature)

    def _degrade(self, spec: LocatorSpec) -> None:
        h = self._health_of(spec.locator_id)
        h.degrade_count += 1
        self._log(f'{spec.locator_id} 降级 -> 已 {h.degrade_count} 次')

    # ---- 各级实现
    def _lv_id_exact(self, spec: LocatorSpec, page: LayoutNode) -> Optional[LayoutNode]:
        if not spec.target_id:
            return None
        cands = ON.id(spec.target_id).filter(flatten(page, only_visible=True))
        # 同 id 多候选（列表项重复 id 在真机树里见过）取第一个可见命中，
        # 不算失败 —— 精确 id 唯一性由上游 register 的模板圈定保证
        return cands[0] if cands else None

    def _lv_id_fuzzy_text(self, spec: LocatorSpec,
                          page: LayoutNode) -> Tuple[Optional[LayoutNode], str]:
        cands = flatten(page, only_visible=True)
        if spec.target_id:
            loose = [n for n in cands if n.id and spec.target_id in n.id]
            if len(loose) == 1:
                return loose[0], 'id_fuzzy_text'
        if spec.text:
            hits = ON.text_contains(spec.text).filter(cands)
            if len(hits) == 1:
                return hits[0], 'id_fuzzy_text'
        if spec.descr:
            hits = ON.descr(spec.descr).filter(cands)
            if len(hits) == 1:
                return hits[0], 'id_fuzzy_text'
        return None, ''

    def _lv_path_type(self, spec: LocatorSpec,
                      page: LayoutNode) -> Tuple[Optional[LayoutNode], str]:
        """层级路径 + 类型：id 被全改掉时的主力。

        双证据打分：type_fp（跨页面同类锚点）必须有；control_key
        （不含坐标的稳定标识）或父链类型序列作第二证据，按得分取唯一最高。
        """
        cands = flatten(page, only_visible=True)
        if spec.type_fp:
            same_type = [n for n in cands if type_fingerprint(n) == spec.type_fp]
        elif spec.target_type:
            same_type = [n for n in cands if n.type == spec.target_type]
        else:
            return None, ''
        if not same_type:
            return None, ''

        def score(n: LayoutNode) -> float:
            s = 0.0
            if spec.control_key and control_key(n) == spec.control_key:
                s += 2.0
            parent_types = tuple(p.type for p in self._ancestors(n))
            if spec.parent_types and parent_types == spec.parent_types:
                s += 1.5
            elif spec.parent_types:
                # 部分匹配：树局部重构（插了一层容器）也该算相似
                tail = parent_types[-len(spec.parent_types):]
                if tail == spec.parent_types:
                    s += 0.8
            if spec.text and n.text == spec.text:
                s += 0.5
            if spec.descr and n.descr == spec.descr:
                s += 0.5
            return s

        scored = sorted(same_type, key=score, reverse=True)
        if scored and score(scored[0]) > 0:
            # 唯一最高分才认——两个同分候选说明线索不够，宁可落空走 L4
            if len(scored) == 1 or score(scored[0]) > score(scored[1]):
                return scored[0], 'path_type'
        return None, ''

    def _lv_vision(self, spec: LocatorSpec, image_path: Optional[str],
                   screen_size: Tuple[int, int]):
        if self.vision is None or not image_path:
            return None
        root = self._last_root
        if root is None:
            return None
        try:
            return self.vision.locate(root, image_path, spec.description,
                                      screen_size[0], screen_size[1])
        except Exception as e:
            self._log(f'视觉通道失败: {type(e).__name__}: {e}')
            return None

    def _lv_coordinate(self, spec: LocatorSpec, health: LocatorHealth,
                       elapsed_ms: float) -> Optional[LocateResult]:
        """坐标兜底已并入 locate 主流程记账（记失败+告警，不记成功）。
        保留本方法供外部需要「只拿兜底点」的场景使用。"""
        if spec.fallback_center is None:
            return None
        cx, cy = spec.fallback_center
        health.coordinate_fallbacks += 1
        self._log(f'{spec.locator_id} 坐标兜底 ({cx},{cy}) —— 已告警')
        return LocateResult(
            rect=Rect(cx, cy, cx, cy), channel='coordinate', confidence=0.2,
            health=health, locator_id=spec.locator_id, level='coordinate',
            uncertain=True, warning='坐标兜底：不可信，仅救急', elapsed_ms=elapsed_ms)

    def _best_after_repair(self, spec: LocatorSpec,
                           page: LayoutNode) -> Optional[LayoutNode]:
        """自愈后用新定位器重试一次（只走到 L3，L4/L5 不是自愈验证口径）。"""
        node = self._lv_id_exact(spec, page)
        if node is not None:
            return node
        node, _ = self._lv_id_fuzzy_text(spec, page)
        if node is not None:
            return node
        node, _ = self._lv_path_type(spec, page)
        return node

    # ------------------------------------------------------ 记账
    def _record_success(self, locator_id: str, elapsed_ms: float) -> None:
        h = self._health_of(locator_id)
        h.attempts += 1
        h.successes += 1
        h.consecutive_failures = 0
        h.total_time_ms += elapsed_ms

    def record_locator_failure(self, locator_id: str, reason: str,
                               attempt: Optional[int] = None) -> None:
        """契约接口：执行器在定位失败时回写。

        幂等口径：以 **(定位尝试代次,
        locator_id)** 为去重键 —— 同一次定位尝试内，内部记账与执行器
        回写谁先到谁生效，重复调用不累加；下一次代次推进后照常记账。
        签名与调用方式不变（调用方只调一次、缺 id 不调；
        即便重复回写也不会重复计数）。

        Parameters
        ----------
        attempt:
            **可选**（A-0，C 2026-09-25 P0）。执行器侧的执行尝试序号，
            由 runner 维护、单调递增，每次回写自带一个新值。
            不传（内部 locate 路径以及旧式两参调用）时行为与修复前
            **完全一致**，用 `_locate_seq` 当代次 —— 契约签名向后兼容，
            所以分工卡的 W2 冻结签名不受影响。
        """
        self._count_failure(locator_id, reason, attempt)

    def _count_failure(self, locator_id: str, reason: str,
                       attempt: Optional[int] = None) -> bool:
        """失败记账的唯一入口（带幂等闸）。返回是否真的计入。

        幂等键 = 本次失败所属的「定位尝试代次」，来源二选一：

        - `attempt`（执行器回写路径）：一次执行尝试 = 一次记账；
        - `self._locate_seq`（内部 locate 路径）：一次 locate() = 一次记账。

        A-0 根因（修复前）：只用 `_locate_seq` 当代次，而执行器走
        matcher/layout 定位、**从不调 `locate()`** → 代次恒为 0 →
        第 1 次回写后 `_fail_seq[lid] = 0`，第 2 次起 `0 == 0` 恒成立 →
        回写全部被当重复记账丢掉 → `consecutive_failures` 永远停在 1 →
        阈值够不着 → **自愈在生产链路上一次都不会触发**。

        为什么键要带命名空间（`_fail_seq` 存 `(来源, 序号)` 而非裸序号）：
        两个来源各自独立单调，但取值空间相同（都是"第几次"）。若直接
        混在一个整数空间里，就可能出现「内部代次恰好等于执行器 attempt」
        的假去重窗口 —— 同一次真实失败被吞掉一次，自愈被推迟。加上
        来源位后，两条路径的记账**互不干扰**，幂等语义不变（同来源
        同序号仍然去重）。
        """
        h = self._health.get(locator_id)
        if h is None:
            return False
        key = (0, self._locate_seq) if attempt is None else (1, attempt)
        if self._fail_seq.get(locator_id) == key:
            self._log(f'{locator_id} 同一次定位失败的重复记账，已去重: {reason}')
            return False
        self._fail_seq[locator_id] = key
        h.attempts += 1
        h.consecutive_failures += 1
        h.last_failure_reason = reason
        h.last_failure_at = self.clock()
        self._log(f'{locator_id} 失败 #{h.consecutive_failures}: {reason}')
        return True

    # ------------------------------------------------------ 自愈
    def repair_locators(self, page_signature: str = '',
                        page: Optional[LayoutNode] = None,
                        force: bool = False,
                        image_path: Optional[str] = None,
                        screen_size: Optional[Tuple[int, int]] = None,
                        ) -> RepairReport:
        """契约接口：重探索当前页面，为连续失败的定位器生成下一代。

        page 不传时用最近一次 locate 看到的控件树（探索器重 dump 后
        调用 locate，天然就是新鲜的）。

        force=True（C 集成日手动触发的口径）：不看失败阈值，
        对该页面的所有定位器一律尝试重探索换代。

        `image_path` / `screen_size`（可选）：线索四档全灭时用来**换通道** ——
        见 `_repair_by_vision`。不传则行为与原来完全一致（纯树内线索）。
        """
        root = page or self._last_root
        rep = RepairReport(page_signature=page_signature)
        if root is None:
            rep.failed['*'] = '没有可用的控件树（从未 locate 过，也未显式传 page）'
            return rep

        for lid, spec in list(self._specs.items()):
            if page_signature and spec.page_signature != page_signature:
                continue
            h = self._health_of(lid)
            if not force and h.consecutive_failures < self.failure_threshold:
                continue

            # 重探索：在新鲜树上按双指纹重新圈定候选
            cands = flatten(root, only_visible=True)
            pool = [n for n in cands
                    if (spec.type_fp and type_fingerprint(n) == spec.type_fp)
                    or (spec.target_type and n.type == spec.target_type)]
            new_node = self._pick_repair_candidate(spec, pool)
            via_vision = False
            if new_node is None:
                new_node = self._repair_by_vision(spec, root, image_path,
                                                  screen_size)
                via_vision = new_node is not None
                if via_vision:
                    self._log(f'{lid} 树内线索全灭 → 换通道：视觉给出候选')
            if new_node is None:
                ties = [f'{n.type}#{n.id}' if n.id else
                        f'{n.type}«{(getattr(n, "text_deep", "") or n.text or "")[:12]}»'
                        for n in (self._last_ties or [])[:5]]
                rep.failed[lid] = ('重探索后无唯一匹配候选'
                                   '（control_key / 子树文案 / text 三档都没唯一命中）')
                if ties:
                    rep.ambiguous[lid] = ties
                    rep.failed[lid] += f'；并列候选 {len(self._last_ties)} 个：' + \
                                       '、'.join(ties)
                continue

            # 生成下一代定位器：以实况节点回填线索。
            # 先留快照，验证通过才提交 —— 修复前的顺序是
            # 「先改写 spec + generation+1 + 清零失败计数，验证失败只报告
            # 不回滚」，后果是 spec 被静默污染、账本多出没通过的一代、
            # 自愈被推迟（阈值被清零）。
            snapshot = (spec.target_id, spec.text, spec.text_deep, spec.descr,
                        spec.target_type, spec.type_fp, spec.control_key,
                        spec.parent_types, spec.fallback_center)
            next_generation = spec.generation + 1
            spec.target_id = new_node.id
            spec.text = new_node.text
            spec.text_deep = (getattr(new_node, 'text_deep', '') or '').strip()
            spec.descr = new_node.descr
            spec.target_type = new_node.type
            spec.type_fp = type_fingerprint(new_node)
            spec.control_key = control_key(new_node)
            spec.parent_types = tuple(p.type for p in self._ancestors(new_node))
            spec.fallback_center = new_node.rect.center

            # 验证：新定位器必须在树上可唯一命中（L1 或 L3 口径）
            verify = (self._lv_id_exact(spec, root)
                      or self._lv_path_type(spec, root)[0])
            if verify is None:
                # 真回滚：8 个线索字段逐项恢复快照；generation 不自增、
                # consecutive_failures 不清零、h.repairs 不计 —— 让
                # 「报告说什么」和「状态是什么」重新变回同一件事。
                (spec.target_id, spec.text, spec.text_deep, spec.descr,
                 spec.target_type, spec.type_fp, spec.control_key,
                 spec.parent_types, spec.fallback_center) = snapshot
                rep.failed[lid] = (f'第 {next_generation} 代候选验证未通过，'
                                   f'已回滚至第 {spec.generation} 代')
                self._log(f'{lid} 自愈验证失败，已回滚至第 {spec.generation} 代')
                continue
            spec.generation = next_generation
            h.repairs += 1
            h.consecutive_failures = 0
            rep.repaired.append(lid)
            rep.details.append(
                f'{lid}: 第{spec.generation}代'
                f'{"（via=vision）" if via_vision else ""} id={spec.target_id!r} '
                f'text={spec.text!r} type_fp={spec.type_fp[:8]}')
            self._log(f'{lid} 自愈成功 -> 第 {spec.generation} 代')

        # 自愈成功的定位器，成功账要补一笔（本次修复自身就是一次成功定位）
        for lid in rep.repaired:
            h = self._health_of(lid)
            h.attempts += 1
            h.successes += 1
        return rep

    def _repair_by_vision(self, spec: LocatorSpec, root: LayoutNode,
                          image_path: Optional[str],
                          screen_size: Optional[Tuple[int, int]],
                          ) -> Optional[LayoutNode]:
        """第 5 档：树内线索全灭时**换通道**（第 4 档是「视觉得分」的降级链位置）。

        为什么需要它 —— 真机实测（`tools/verify_locator_degrade.py` 场景 C）：
        靶子是计算器键盘的 `L001_7`，而计算器按键**在控件树里连文案都没有**
        （`text_deep` 为空）、同类型兄弟 18 个。此时 id / 子树文案 / text 三档
        全都没法唯一定人，**任何"猜一个"都是错的**。但换成视觉通道，「7」这个
        键在截图上是有字面图形的 —— 这正是多模态兜底该上场的地方。

        三道闸，缺一不可（自愈猜错比不换更糟）：
        1. 视觉**不是** uncertain（它自己也不确定就不要）；
        2. 置信度 ≥ `VISION_CONF_MIN`；
        3. 视觉框能**唯一**映射回一个控件树节点，且 IoU ≥ `VISION_IOU_MIN`。
        """
        if self.vision is None or not image_path:
            return None
        size = screen_size or (root.rect.width, root.rect.height)
        try:
            vt = self.vision.locate(root, image_path, spec.description,
                                    int(size[0]), int(size[1]))
        except Exception as e:                      # 视觉是增强项，失败不影响树内结论
            self._log(f'{spec.locator_id} 换通道失败: {type(e).__name__}: {e}')
            return None
        if vt is None or getattr(vt, 'uncertain', False):
            return None
        if float(getattr(vt, 'confidence', 0.0)) < VISION_CONF_MIN:
            return None

        best_set: List[LayoutNode] = []
        best_iou = 0.0
        for n in flatten(root, only_visible=True):
            r = n.rect.overlap_ratio(vt.rect)
            if r <= 0:
                continue
            if r > best_iou + 1e-6:
                best_set, best_iou = [n], r
            elif abs(r - best_iou) <= 1e-6:
                best_set.append(n)
        if not best_set or best_iou < VISION_IOU_MIN:
            return None
        return self._break_vision_tie(best_set)

    @staticmethod
    def _break_vision_tie(tied: List[LayoutNode]) -> Optional[LayoutNode]:
        """同分候选里挑一个 —— **真机上容器与子节点同 bounds 是常态**。

        实测（计算器「7」键）：视觉框与**两个**节点的 IoU 都是 1.0 ——
        `GridItem`（容器，无 id）与 `Button#…`（真正的按键）。
        这种同分不能按「并列 ⇒ 放弃」处理，因为**它们不是两个候选，
        是同一个区域的两层**。判据：
          · 取**最深**的那层（子节点）；容器只是它的壳；
          · 再同层就看 `clickable` / 有 id 的 —— 那才是可操作的目标；
          · 仍分不出（真·两个并列的可点兄弟）→ 返回 None，
            宁可不修，也不把定位器换成错的。
        """
        if len(tied) == 1:
            return tied[0]

        def depth(n: LayoutNode) -> int:
            d, p = 0, n.parent
            while p is not None:
                d += 1
                p = p.parent
            return d

        deepest = max(depth(n) for n in tied)
        finals = [n for n in tied if depth(n) == deepest]
        if len(finals) == 1:
            return finals[0]
        actionable = [n for n in finals if n.clickable or n.id]
        return actionable[0] if len(actionable) == 1 else None

    def _pick_repair_candidate(self, spec: LocatorSpec,
                               pool: List[LayoutNode]) -> Optional[LayoutNode]:
        """自愈选人：按线索强度分四档，**每档都要求唯一**。

        | 档 | 线索 | 为什么排这个位置 |
        |---|---|---|
        | 1 | `control_key` 精确同款 | 页面内稳定标识，最硬 |
        | 2 | **`text_deep`（子树文案）** | 真机上容器自身 id/text 常为空、文案在子节点 —— **这一档就是为真机加的**：旧实现只有 1/3/4 档，于是「无 id 无自带文案的容器」在真机上直接掉到第 4 档（同类型多候选）→ 报「线索全失效」。 |
        | 3 | 自带 text / descr 同文案 | 离线受控树常用 |
        | 4 | 候选池只剩 1 个 | 没得挑，只能认 |

        四档都没有唯一命中时**返回 None 并留下并列候选**（`_last_ties`），
        让上层报「有 N 个同分候选」而不是含糊的「线索全失效」——
        降级要能说清降在哪一档。
        """
        self._last_ties = []
        if not pool:
            return None
        if spec.control_key:
            exact = [n for n in pool if control_key(n) == spec.control_key]
            if len(exact) == 1:
                return exact[0]
        # ★ 第 2 档：子树文案。真机上这是容器唯一可辨识的信息。
        if spec.text_deep:
            deep = [n for n in pool
                    if (getattr(n, 'text_deep', '') or '').strip() == spec.text_deep]
            if len(deep) == 1:
                return deep[0]
            if len(deep) > 1:
                self._last_ties = deep
        same_text = ([n for n in pool if spec.text and n.text == spec.text]
                     or [n for n in pool if spec.descr and n.descr == spec.descr])
        if len(same_text) == 1:
            return same_text[0]
        # 线索全失效：同类型多候选无法安全定人 —— 放弃，交给上层报失败
        if len(pool) == 1:
            return pool[0]
        if not self._last_ties:
            self._last_ties = list(pool)
        return None

    # ------------------------------------------------------ 查询
    def health(self, locator_id: str) -> LocatorHealth:
        """查账本。**未知 id 照旧抛 KeyError** —— 这是查询接口，
        调用方要的是「这个定位器现在什么状态」，把不存在的 id 悄悄
        变成一份空账本反而会掩盖调用方的 id 拼错（A-2 的教训是
        「注册链路必须补账本」，不是「查错 id 也该给个默认可信对象」）。
        """
        return self._health[locator_id]

    def spec(self, locator_id: str) -> LocatorSpec:
        return self._specs[locator_id]

    def all_health(self) -> Dict[str, Dict[str, Any]]:
        return {lid: h.as_dict() for lid, h in self._health.items()}
