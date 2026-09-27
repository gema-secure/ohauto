"""
B4 —— 失败归因引擎（四分类）
============================

对外契约：`diagnose(record) -> Verdict`（category + evidence + suggestion）。

这是本项目的**三个真空白之一**（另外两个是 C3 跨形态、A3 定位器自愈）：
调研过的同类项目都只报「失败」，没有一个做归因。所以这个模块的价值不在于
「跑得出结果」，而在于**每一步结论都能拿出客观证据、并且能被证伪**。

四分类（任务卡第三章 B4）
------------------------
    LOCATOR      定位失败      目标控件不在树、但页面指纹符合预期
    TIMING       时序问题      控件存在，等一等/重试就能过
    APP_DEFECT   应用缺陷      崩溃 / 白屏 / 无响应 / 日志有异常栈
    CASE_DEFECT  用例缺陷      前置条件不满足、步骤或期望值本身写错

另有**两类非归因结论**（不是第五、第六类归因，而是「这次失败不指责任何一方」）：

    ENVIRONMENT  环境问题      设备掉线 / hdc 断链 / 应用拉不起来
    UNKNOWN      证据不足      **四类之外**：宁可说不知道，也不硬扣帽子

`FOUR_CATEGORIES` 就是上面那四类，**归因准确率只在这四类上统计**。

与 C4 的分工（这条边界不能越）
------------------------------
`collect_signals()` 只输出「客观证据 + 异常识别 + 置信度」，**不做归因判断**；
本模块才下结论。所以这里绝不能反过来要求 C4 告诉我们「这是应用缺陷」。

判定顺序（先看严重的、排除性的，再看解释性的）
----------------------------------------------
    1. 应用缺陷（崩溃类证据是一票否决：应用都崩了，讨论「控件没找到」没有意义）
    2. 环境问题（设备都没连上，讨论「定位失败还是用例缺陷」同样没有意义，
       而且会把结论交给错误的人去修）
    3. 步骤成功后仍带失败尝试 → 时序（已经证伪了「定位失败」）
    4. 失败步 → 在 用例缺陷 / 时序 / 定位失败 三条里按置信度取最高
       （同分时按 用例缺陷 > 时序 > 定位失败 的顺序，见 _TIE_ORDER）

一条重要纪律：**弱证据不参与定案**
------------------------------------------------
C4 明确标注过几类低置信度证据（进程不在但无崩溃日志 0.35、hilog 关键词 0.45、
无响应的多轮探测 0.3 等），并特意说明「别把它单独当结论用」。
因此应用缺陷的判定有一条 `APP_CONFIDENCE_FLOOR` 门槛：低于它的异常证据
只会进 `evidence` 作为参考，**不会**单独把一次失败判成应用缺陷。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .explorer import MIN_INTERACTIVE_OPACITY, build_page_signature
from .layout import LayoutNode, flatten, parse_layout
from .matcher import Matcher
from .signals import collect_signals


# ================================================================ 分类

class Category(str, Enum):
    """归因输出：**四分类**（对被测应用/用例的归因）+**两类非归因**。

    四分类是任务卡的验收口径，也是答辩差异表的那一条：

        LOCATOR / TIMING / APP_DEFECT / CASE_DEFECT

    另外两类**不是「第五类归因」，而是「这次失败不归被测应用也不归用例」**：

        ENVIRONMENT  环境/链路问题（设备掉线、hdc 超时、应用拉不起来）
        UNKNOWN      证据不足（不硬扣帽子）

    为什么要有 ENVIRONMENT（C 复核提出的缺陷，2026-09-23 补）：
    原来只有四类 + UNKNOWN，于是「设备掉线」这种**根本没到被测应用**的失败
    全部落到 `UNKNOWN @ 0.00`，还被建议「请补做结果信号采集」——
    **设备都没连上，采不到任何东西**，建议方向是错的。
    归因的价值一半在结论、一半在「把结论交给对的人」：
    环境问题该给运维/重跑，不该给应用开发者。
    """

    LOCATOR = 'LOCATOR'            # 定位失败
    TIMING = 'TIMING'              # 时序问题
    APP_DEFECT = 'APP_DEFECT'      # 应用缺陷
    CASE_DEFECT = 'CASE_DEFECT'    # 用例缺陷
    ENVIRONMENT = 'ENVIRONMENT'    # 环境/链路问题（非归因：不指责任何一方）
    UNKNOWN = 'UNKNOWN'            # 证据不足（不硬扣帽子）


#: 任务卡口径的「四分类」—— 归因准确率只在这四类上统计，
#: ENVIRONMENT / UNKNOWN 是「非归因结论」，不计入归因命中率的分母。
FOUR_CATEGORIES: Tuple[Category, ...] = (
    Category.LOCATOR, Category.TIMING, Category.APP_DEFECT, Category.CASE_DEFECT)

CATEGORY_CN: Dict[Category, str] = {
    Category.LOCATOR: '定位失败',
    Category.TIMING: '时序问题',
    Category.APP_DEFECT: '应用缺陷',
    Category.CASE_DEFECT: '用例缺陷',
    Category.ENVIRONMENT: '环境问题',
    Category.UNKNOWN: '证据不足',
}

# 定案门槛与置信度档位。数字写在这里，改它的人一眼能看到影响面。
APP_CONFIDENCE_FLOOR = 0.5      # 低于此值的异常证据不足以单独定「应用缺陷」
CRASH_WINDOW_TOLERANCE_S = 120  # 崩溃时间戳与步骤开始时间的容差（设备时钟域）

CONF_STRONG = 0.95
CONF_HIGH = 0.9
CONF_GOOD = 0.85
CONF_MID = 0.8
CONF_FAIR = 0.75
CONF_WEAK = 0.7
CONF_HINT = 0.6

# 同分时的定案顺序（越靠前越优先）。放在这里是为了「同分怎么办」有据可查，
# 而不是取决于代码里 if 的书写顺序。
_TIE_ORDER: Tuple[Category, ...] = (
    Category.CASE_DEFECT, Category.TIMING, Category.LOCATOR)

# hilog 里能说明「应用自己炸了」的痕迹
_STACK_PATTERNS = (
    r'Fault thread', r'cppcrash', r'jscrash', r'appfreeze', r'SIGSEGV', r'SIGABRT',
    r'SIGBUS', r'SIGILL', r'SIGFPE', r'abort', r'backtrace', r'stacktrace',
    r'FATAL EXCEPTION', r'Out of memory', r'\boom\b', r'ANR',
)
# ⚠️ `oom` 必须带词边界（C 复核）：这些模式是 `re.I` 下 `re.search` 的，
#    裸 `r'oom'` 会把 **Zoom / room / boom / zoomIn** 全部当成 OOM 崩溃痕迹
#    —— 而「缩放失败」「进入房间」在真机应用里到处都是，等于给应用缺陷塞假证据。

# 目标控件「在树里但用不了」的判据（C1 报告建议纳入的字段）
_OCCLUSION_KEYS = ('zIndex', 'opacity', 'hitTestBehavior')

#: `opacity` 低于此值才谈得上「点不着」。半透明（0.9）照样可点，判死是过度归因。
#: 数字**不在这里拍** —— 由 `explorer.MIN_INTERACTIVE_OPACITY` 统一给出，
#: 同一件事全项目只留一个数字（`detect_dialog` 的遮挡线也用同一个）。
OPACITY_MIN_USABLE = MIN_INTERACTIVE_OPACITY

# 可用性判定的三态 —— 故意不用 bool：
# 「确定不可用」和「有点可疑」是两回事，混成一个 False 会把应用的渲染属性
# 说成用例写错了（C 复核的原始缺陷）。
USABLE = 'usable'
SUSPECT = 'suspect'
BLOCKED = 'blocked'


# ================================================================ 结论

@dataclass
class Verdict:
    """归因结论。

    `evidence` 是**结论的一部分**，不是日志：每条都要写清楚「凭什么这么判」，
    归因引擎最怕的就是给一个结论却说不出理由。
    """
    category: Category = Category.UNKNOWN
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    suggestion: str = ''
    step_index: Optional[int] = None
    signals_used: List[str] = field(default_factory=list)
    locator_id: str = ''            # 仅定位失败才有：回写给 A 的定位器自愈
    scores: Dict[str, float] = field(default_factory=dict)   # 各类别的置信度快照

    @property
    def category_cn(self) -> str:
        return CATEGORY_CN.get(self.category, str(self.category))

    def to_dict(self) -> Dict[str, Any]:
        d = {'category': self.category.value, 'category_cn': self.category_cn,
             'confidence': round(self.confidence, 4),
             'evidence': list(self.evidence),
             'suggestion': self.suggestion,
             'scores': {k: round(v, 4) for k, v in self.scores.items()}}
        if self.step_index is not None:
            d['step_index'] = self.step_index
        if self.signals_used:
            d['signals_used'] = list(self.signals_used)
        if self.locator_id:
            d['locator_id'] = self.locator_id
        return d

    def __str__(self) -> str:
        return (f'[{self.category_cn}] 置信度 {self.confidence:.2f}'
                f'{"（步骤 " + str(self.step_index) + "）" if self.step_index is not None else ""}'
                f' — {self.suggestion}')


# ================================================================ 输入

@dataclass
class ExecutionRecord:
    """一次失败执行的全部可归因材料。

    刻意**兼容 C 的 runner**：`step` / `steps` 直接收 `runner.StepResult`
    （它已经带了 kind / attempts / rescued_by_retry / cascade），
    这里只补齐 runner 不产出、而归因需要的三类东西：

      * 控件树快照（失败前后的 `dumpLayout` 结果，dict / JSON 文本 / LayoutNode 均可）
      * 结果信号（C4 的 `collect_signals()` 产物）
      * 用例的预期（目标控件规格、期望页面、前置条件）

    `started_at` 是**设备时钟域**的 epoch（与 `CrashRecord.timestamp_epoch` 同一时基），
    用来剔除「上一轮遗留的旧崩溃日志」。取不到就留 None —— 那会放宽核对但会记在
    evidence 里，不会假装已经核对过。
    """

    bundle: str = ''
    ability: str = ''
    case_name: str = ''
    step: Any = None                      # runner.StepResult
    steps: List[Any] = field(default_factory=list)
    signals: Any = None                   # signals.Signals
    trees: List[Any] = field(default_factory=list)
    tree_labels: List[str] = field(default_factory=list)
    expected_target: Optional[Any] = None  # 匹配器规格（dict / str / Matcher）
    expected_page: str = ''                # 期望页面：PageSignature 的任一 key，或 pagePath
    precondition: Optional[Any] = None     # 前置条件控件的匹配器规格
    # ★ A 的 LocateResult.locator_id —— 由 A 的 LocatorManager 统一生成（形如 `L3_登录按钮`），
    #   **归因侧不自己拼**。执行时 locate 返回什么，这里就填什么；执行失败时原样回写。
    locator_id: str = ''
    hilog: Any = None                      # 文本或行列表
    started_at: Optional[float] = None     # 设备时钟域 epoch
    artifacts: Dict[str, str] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------- 便捷构造

    @classmethod
    def from_step(cls, step: Any, **kw) -> 'ExecutionRecord':
        """从一个 `runner.StepResult` 建记录（最常见的入口）。"""
        kw.setdefault('steps', [step])
        return cls(step=step, **kw)

    @classmethod
    def from_case_result(cls, res: Any, **kw) -> 'ExecutionRecord':
        """从 `runner.CaseResult` 建记录：自动挑出**第一个独立失败步**。

        挑「独立失败」而不是「第一个失败」：级联失败（cascade=True）是前面某步
        带崩的，拿它做归因只会得到重复结论 —— 归因要打在病灶上。

        ★ 2026-09-23 修（C 在集成时抓到的缺陷，见 `docs/给B-回执-2026-09-23.md` §2.2）：
        **必须继承失败步的快照**，否则走这条路的归因永远「没有快照」，
        定位失败只能给 0.6（置信度阶梯的最低档）。

        ⚠️ 一个坑：`StepResult.trees` 存的是**文件路径**，而
        `ExecutionRecord.trees` 的消费方 `_parse_trees()` 只会 `parse_layout(t)` ——
        **接不了路径**（路径会被解析失败变成 None）。所以这里必须**读出文件内容**
        再放进去，直接塞路径等于把「恒空」换成「恒解析失败」，那更坏。
        读不出来的（文件被清理 / 路径无效）**跳过**，不制造 None 占位。
        """
        steps = list(getattr(res, 'steps', []) or [])
        chosen = None
        for s in steps:
            if not getattr(s, 'ok', True) and not getattr(s, 'cascade', False):
                chosen = s
                break
        if chosen is None:
            for s in steps:
                if not getattr(s, 'ok', True):
                    chosen = s
                    break
        kw.setdefault('steps', steps)
        kw.setdefault('bundle', getattr(res, 'bundle', '') or '')
        kw.setdefault('case_name', getattr(res, 'name', '') or '')
        if not kw.get('trees'):
            kw['trees'] = _load_step_trees(chosen)
        return cls(step=chosen, **kw)


_FIELDS = set(ExecutionRecord.__dataclass_fields__)


def _load_step_trees(step: Any) -> List[Any]:
    """把 `StepResult.trees` 里的**快照路径**读成控件树文本。

    为什么需要单独一个函数：`StepResult.trees` 是**路径列表**（运行器抓快照时
    落盘存路径），而 `ExecutionRecord.trees` 期望**能解析成控件树的东西**。
    两者类型对不上时直接赋值会静默产出 `[None]`（`_parse_trees` 解析失败），
    那比空列表更坏 —— 空列表至少是诚实的「没有证据」。

    容错原则：**读不出来就跳过**，不抛、不占位。归因侧的立场一直是
    「证据不足就说不确定」，不是「缺文件就崩」。
    """
    out: List[Any] = []
    for t in getattr(step, 'trees', None) or []:
        if isinstance(t, LayoutNode):
            out.append(t)
            continue
        if not isinstance(t, str):
            continue
        # 已经是控件树文本（JSON 串）就直接用，否则当路径读文件
        head = t.lstrip()[:1]
        if head in ('{', '['):
            out.append(t)
            continue
        try:
            with open(t, 'r', encoding='utf-8', errors='replace') as f:
                text = f.read()
        except OSError:
            continue                       # 文件被清理/路径无效 → 跳过
        if text.strip():
            out.append(text)
    return out


def coerce_record(record: Any) -> ExecutionRecord:
    """把各种形态的输入统一成 `ExecutionRecord`。

    支持：`ExecutionRecord` / 普通 dict / `runner.StepResult` / `runner.CaseResult`。
    这样 C 在报告里顺手丢一个 StepResult 过来也能用，不必先包装。
    """
    if isinstance(record, ExecutionRecord):
        return record
    if isinstance(record, dict):
        kw = {k: v for k, v in record.items() if k in _FIELDS}
        return ExecutionRecord(**kw)
    if hasattr(record, 'steps') and hasattr(record, 'bundle') and not hasattr(record, 'action'):
        return ExecutionRecord.from_case_result(record)
    if hasattr(record, 'action') and hasattr(record, 'ok'):
        return ExecutionRecord.from_step(record)
    raise TypeError(f'diagnose() 不认识的输入类型: {type(record).__name__}')


# ================================================================ 工具

def _kind_of(step: Any) -> str:
    """取失败类别文本（`FailureKind` 是 str Enum，取值可能两种形态都有）。"""
    k = getattr(step, 'kind', None)
    if k is None:
        return ''
    return getattr(k, 'value', None) or str(k)


def _err_text(step: Any, limit: int = 200) -> str:
    """取「首次失败尝试」的错误文本。

    `runner.StepResult` 自己不带 error —— 错误文本挂在 `attempts[*].error` 上，
    所以这里统一从尝试记录里捞第一条失败的。
    """
    for a in getattr(step, 'attempts', None) or []:
        if not getattr(a, 'ok', True) and getattr(a, 'error', ''):
            return str(a.error)[:limit]
    return str(getattr(step, 'error', '') or '')[:limit]


def _parse_trees(rec: ExecutionRecord) -> List[Optional[LayoutNode]]:
    out: List[Optional[LayoutNode]] = []
    for t in rec.trees or []:
        if t is None:
            out.append(None)
            continue
        if isinstance(t, LayoutNode):
            out.append(t)
            continue
        try:
            out.append(parse_layout(t))
        except Exception:
            out.append(None)
    return out


def _as_matcher(spec: Any) -> Optional[Matcher]:
    if spec is None:
        return None
    if isinstance(spec, Matcher):
        return spec
    from .action import spec_to_matcher
    try:
        return spec_to_matcher(spec)
    except Exception:
        return None


def _spec_text(spec: Any) -> str:
    if isinstance(spec, Matcher):
        return str(spec)
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict):
        return ' '.join(f'{k}={v}' for k, v in spec.items())
    return str(spec)


def _find_in_tree(m: Optional[Matcher], root: Optional[LayoutNode]) -> List[LayoutNode]:
    if m is None or root is None:
        return []
    return m.filter(flatten(root, only_visible=False))


def _contains_point(rect: Any, pt: Tuple[int, int]) -> bool:
    """点是否落在矩形内（左闭右开，避免相邻控件在边界上互相「压住」）。"""
    x, y = pt
    return rect.left <= x < rect.right and rect.top <= y < rect.bottom


def _find_occluder(node: LayoutNode, root: Optional[LayoutNode],
                   z: Optional[float]) -> Optional[Tuple[float, LayoutNode]]:
    """找「确实挡住了这个控件点击落点」的上层节点，没有则 None。

    判据用**点击落点是否被更高 zIndex 的节点接走**，而不是「矩形有重叠」：
    uiInput 是打坐标的，落点就是控件中心 —— 中心被上层节点覆盖，这一击才真的
    会打到别人身上。只算交集会把「父容器/相邻卡片」这类正常重叠误判成遮挡。
    """
    if root is None:
        return None
    base = z if z is not None else 0.0
    center = node.rect.center
    best: Optional[Tuple[float, LayoutNode]] = None
    for other in flatten(root, only_visible=True):
        if other is node:
            continue
        oz = _num(other.attributes.get('zIndex'))
        if oz is None or oz <= base:
            continue
        if _contains_point(other.rect, center):
            if best is None or oz > best[0]:
                best = (oz, other)
    return best


def _usability(node: LayoutNode, root: Optional[LayoutNode] = None
               ) -> Tuple[str, str]:
    """控件「在树里但点不动」的判据。返回 (`USABLE`/`SUSPECT`/`BLOCKED`, 原因)。

    ★ zIndex / opacity 的语义方向（C 复核，2026-09-23 修）：

    原来写的是「`zIndex > 0` → 被上层覆盖」——**方向是反的**。
    zIndex 越大表示这个控件**在上层**，它恰恰说明控件没被盖住。
    于是真机上一个 `zIndex=5` 的按钮被判成「被覆盖」，再顺着推到
    `CASE_DEFECT @ 0.7` —— **把应用的渲染属性说成了用例写错**，归因方向偏了。

    正确判据：存在**另一个**可见节点，zIndex 更高，且**盖住了本控件的点击落点**。

    `opacity` 同理：「< 1.0 就判不可点」太急 —— 0.9 的半透明照样能点。
    阈值下放到 `OPACITY_MIN_USABLE`，中间区间只记「疑似」不判死。

    三态而不是 bool：**「确定不可用」和「有点可疑」不是一回事**。
    混成一个 False，就成了同一种过度归因，只是换了个方向。
    """
    if not node.visible:
        return BLOCKED, '控件 visible=false'
    if not node.enabled:
        return BLOCKED, '控件 enabled=false（不可点）'

    hit = str(node.attributes.get('hitTestBehavior') or '')
    if hit and 'Default' not in hit and 'Transparent' not in hit:
        return BLOCKED, f'hitTestBehavior={hit}（不接收命中）'

    suspect: List[str] = []
    z = _num(node.attributes.get('zIndex'))
    cover = _find_occluder(node, root, z)
    if cover is not None:
        return BLOCKED, (f'控件点击落点被上层节点接走：'
                         f'zIndex {cover[0]:g} > {z if z is not None else 0:g}，'
                         f'且该节点覆盖控件中心')
    if z is not None and z > 0 and root is None:
        # 没有整树可比对时判不出遮挡，但至少别把「在上层」写成「被覆盖」
        suspect.append(f'控件 zIndex={z:g}（在上层；无整树可比对，未判遮挡）')

    op = _num(node.attributes.get('opacity'), 1.0)
    if op is not None:
        if op < OPACITY_MIN_USABLE:
            return BLOCKED, (f'控件 opacity={op:g} < {OPACITY_MIN_USABLE:g}'
                             f'（近乎不可见，命中不了）')
        if op < 1.0:
            suspect.append(f'控件 opacity={op:g}（半透明，疑似——'
                           f'0.9 的不透明度照样可点，不足以判定不可用）')
    if suspect:
        return SUSPECT, '；'.join(suspect)
    return USABLE, ''


def _num(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _anomaly_score(sig: Any, kind: str) -> float:
    fn = getattr(sig, 'anomaly_score', None)
    if callable(fn):
        try:
            return float(fn(kind))
        except Exception:
            return 0.0
    return 0.0


def _anomalies(sig: Any, kind: str) -> List[Any]:
    fn = getattr(sig, 'of_kind', None)
    if callable(fn):
        try:
            return list(fn(kind))
        except Exception:
            return []
    return []


def _hilog_lines(rec: ExecutionRecord) -> List[str]:
    """把 `hilog` 或 `signals.hilog_tail` 统一成行列表。"""
    src = rec.hilog
    if src in (None, ''):
        src = getattr(rec.signals, 'hilog_tail', None)
    if src in (None, ''):
        return []
    if isinstance(src, str):
        return [ln for ln in src.splitlines() if ln.strip()]
    return [str(ln) for ln in src]


def _stack_traces(lines: Sequence[str]) -> List[str]:
    hits = []
    for ln in lines:
        for p in _STACK_PATTERNS:
            if re.search(p, ln, re.I):
                hits.append(ln.strip()[:200])
                break
    return hits


def _page_expectation(rec: ExecutionRecord,
                      trees: Sequence[Optional[LayoutNode]]) -> str:
    """落点是否在用例声明的期望页面上：'match' / 'mismatch' / 'unknown'。

    ★ 2026-09-22 真机缺陷修正（C 在 DAYU200 上抓到）：
    **没有可用快照时必须返回 'unknown'，不能返回 'mismatch'。**

    原实现是「for 遍历所有快照，没命中就 return 'mismatch'」——
    快照列表为空时这个循环一次都不执行，直接掉到 `return 'mismatch'`。
    于是「落点与 expected_page 不符 → 用例缺陷」这条判据被凭空触发，
    把本该是 `LOCATOR` 的失败判成了 `CASE_DEFECT @ 0.80`。

    后果比"置信度低"严重得多：**是类别判错**，不是判得不确定。
    真机实测对照：

        有快照 → LOCATOR      0.85   ✓
        无快照 → CASE_DEFECT  0.80   ✗

    修正后：没有快照 → 'unknown'（判不了就是判不了），
    定位失败仍给出 0.6 的弱结论，但**类别不再跑偏**。
    """
    if not rec.expected_page:
        return 'unknown'
    usable = [r for r in trees if r is not None]
    if not usable:
        return 'unknown'          # ← 一行之差：核不了落点，就别假装核过了
    want = rec.expected_page
    # ★ `ability` 两边填得不一致时不能就此判「落点不符」（2026-09-23 发现）：
    #   页面签名里带了 ability，而记录侧默认 `EntryAbility`、签名侧可能是空串
    #   （`build_page_signature` 的默认值就是 ''）。这种「一边填一边空」的
    #   不一致会把**定位失败**推成**用例缺陷** —— 又是方向错。
    #   所以先用记录里的 ability 核，核不上再用空串核一遍：页面身份不该被
    #   一个可选字符串卡死。
    abilities = [rec.ability]
    if rec.ability:
        abilities.append('')
    for ability in abilities:
        for root in usable:
            sig = build_page_signature(root, bundle=rec.bundle, ability=ability)
            if want in (sig.content_key, sig.structural_key, sig.page_path):
                return 'match'
    return 'mismatch'


def locator_id_note(rec: ExecutionRecord) -> str:
    """定位失败但没有 locator_id 时，说明为什么回写不了。

    ⚠️ 口径已与 A 对齐（2026-09-22）：`locator_id` **由 A 的 LocatorManager 统一生成**，
    形如 `L3_登录按钮`，归因侧**不自己拼**。所以这里只做一件事：
    记录里带了 `locate()` 返回的 `locator_id` 就原样用它回写；没带就说明缺什么。

    （早先我临时拼过 `bundle::目标规格`，A 已明确否掉，代码已删。）
    """
    if rec.locator_id:
        return ''
    return ('记录里没有 locator_id（应由 locate() 的 LocateResult.locator_id 带过来），'
            '无法回写定位器自愈；执行器把 locate 的结果一起放进 ExecutionRecord 即可')


# ================================================================ 判据：应用缺陷

def _crash_matches(rec: ExecutionRecord, crash: Any) -> Tuple[bool, str]:
    """这条崩溃日志能不能解释本次失败。返回 (是否采纳, 不采纳的原因)。"""
    mod = str(getattr(crash, 'module_name', '') or '').strip()
    # C4 明确说过：module_name 可能是空串（文件名和正文都没给），**不能**当成
    # 「不是这个应用」—— 丢证据比多一条可疑证据更糟。
    if mod and rec.bundle and mod != rec.bundle:
        return False, f'崩溃的应用是 {mod}，不是被测应用 {rec.bundle}'

    if getattr(crash, 'in_foreground', True) is False:
        return False, '该崩溃是后台崩溃（Foreground=false），不能解释前台失败'

    ts = getattr(crash, 'timestamp_epoch', None)
    if rec.started_at is not None and ts is not None:
        if ts < rec.started_at - CRASH_WINDOW_TOLERANCE_S:
            return False, (f'崩溃时间 {ts:.0f} 早于本步开始时间 '
                           f'{rec.started_at:.0f} 超过 {CRASH_WINDOW_TOLERANCE_S}s，'
                           f'属于上一轮遗留的旧日志')
    return True, ''


def _app_defect(rec: ExecutionRecord) -> Tuple[float, List[str], List[str]]:
    """崩溃 / 白屏 / 无响应 / 日志异常栈 → 应用缺陷。返回 (置信度, 证据, 用到的信号)。"""
    ev: List[str] = []
    used: List[str] = []
    sig = rec.signals
    conf = 0.0

    # ---- ① 崩溃日志：最硬的证据，一票否决
    crashes = list(getattr(sig, 'crashes', []) or [])
    if crashes:
        kept, rejected = [], []
        for c in crashes:
            ok, why = _crash_matches(rec, c)
            if ok:
                kept.append(c)
            else:
                rejected.append(why)
        if kept:
            used.append('CRASH')
            conf = max(conf, _anomaly_score(sig, 'CRASH'), CONF_HIGH)
            for c in kept:
                ev.append(
                    f'崩溃日志：module={getattr(c, "module_name", "") or "(未标注)"} '
                    f'signal={getattr(c, "signal", "") or "?"} '
                    f'时间={getattr(c, "timestamp", "") or "?"} '
                    f'pid={getattr(c, "pid", 0)}'
                    + (f' 栈顶={c.stack_head[0][:80]}' if getattr(c, 'stack_head', None) else ''))
            if _anomaly_score(sig, 'CRASH'):
                ev.append(f'崩溃信号合成置信度 {_anomaly_score(sig, "CRASH"):.2f}'
                          f'（faultlog + 进程存活两条独立证据）')
        for why in rejected:
            ev.append(f'（已排除一条崩溃日志：{why}）')

    # ---- ② 其余异常：按 C4 的置信度门槛过滤，弱证据只记不判
    for kind in ('WHITE_SCREEN', 'NO_RESPONSE', 'NO_WINDOW'):
        score = _anomaly_score(sig, kind)
        if score >= APP_CONFIDENCE_FLOOR:
            used.append(kind)
            conf = max(conf, score)
            for a in _anomalies(sig, kind):
                if a.confidence >= APP_CONFIDENCE_FLOOR:
                    ev.append(f'{kind}（{a.source}，置信度 {a.confidence:.2f}）：{a.evidence}')
        elif score > 0:
            ev.append(f'（{kind} 证据置信度仅 {score:.2f}，低于定案门槛 '
                      f'{APP_CONFIDENCE_FLOOR}，按要求不单独作为应用缺陷判定）')

    # ---- ③ 进程不在，但没有崩溃日志：C4 给 0.35，只作参考
    if getattr(sig, 'process_alive', None) is False and not crashes:
        ev.append('（进程已不在 pidof 输出中，但**没有**崩溃日志：也可能是被 '
                  'force-stop / 系统回收，置信度不足，不作为应用缺陷判定）')

    # ---- ④ hilog 异常栈
    hits = _stack_traces(_hilog_lines(rec))
    if hits:
        used.append('hilog')
        mentions = bool(rec.bundle) and any(rec.bundle in h for h in hits)
        conf = max(conf, CONF_MID if mentions else CONF_WEAK)
        ev.append(f'hilog 出现异常栈痕迹（{len(hits)} 行）'
                  + (f'，且其中提到被测应用 {rec.bundle}' if mentions
                     else '，但没直接提到被测应用（阈值按下限取）')
                  + f'，首行：{hits[0]}')

    return conf, ev, used


# ================================================================ 判据：用例缺陷

def _case_defect(rec: ExecutionRecord, trees: Sequence[Optional[LayoutNode]],
                 target: Optional[Matcher], pre: Optional[Matcher],
                 page_expect: str) -> Tuple[float, List[str]]:
    ev: List[str] = []
    conf = 0.0
    kind = _kind_of(rec.step)
    err = _err_text(rec.step)

    # ---- 步骤本身非法
    if kind == 'DSL':
        conf = max(conf, CONF_STRONG)
        ev.append(f'运行器判定为 DSL 错误（用例本身写错，重试无意义）：{err or "(无错误详情)"}')

    # ---- 断言不成立
    elif kind == 'ASSERT':
        if target is not None:
            present_any = any(_find_in_tree(target, r) for r in trees)
            if present_any:
                conf = max(conf, CONF_GOOD)
                ev.append('断言失败，但被断言的控件**存在于控件树中** '
                          '→ 期望值或断言逻辑写错，不是应用行为异常')
            else:
                conf = max(conf, CONF_MID)
                ev.append('断言失败，且被断言的控件不在任何控件树快照中 '
                          '→ 前置条件没满足就断言了')
        else:
            conf = max(conf, CONF_FAIR)
            ev.append(f'断言失败（运行器判定 ASSERT），未提供被断言的控件规格：{err or ""}')

    # ---- 前置条件控件不存在
    if pre is not None:
        if not any(_find_in_tree(pre, r) for r in trees):
            conf = max(conf, CONF_MID)
            ev.append(f'用例声明的前置条件控件 {_spec_text(pre)} 不在任何控件树快照中')
        else:
            ev.append(f'前置条件控件 {_spec_text(pre)} 在树中（前置条件已满足）')

    # ---- 落点页面与用例声明不符：说明用例的流程没走到目标页
    if page_expect == 'mismatch':
        conf = max(conf, CONF_MID)
        ev.append(f'落点页面与用例声明的 expected_page({rec.expected_page[:16]}…) 不符 '
                  f'→ 用例缺少「先走到目标页」的步骤')

    # ---- 目标控件在树里但不可用：前置状态没满足
    if target is not None:
        for r in trees:
            found = _find_in_tree(target, r)
            if not found:
                continue
            verdict, why = _usability(found[0], r)
            if verdict == BLOCKED:
                conf = max(conf, CONF_WEAK)
                ev.append(f'目标控件 {_spec_text(target)} 存在但不可用：{why} '
                          f'→ 用例应先准备好前置状态')
            elif verdict == SUSPECT:
                # 疑似项只记证据、只给提示级置信度：**不把渲染属性当成用例写错**
                conf = max(conf, CONF_HINT)
                ev.append(f'目标控件 {_spec_text(target)} 存在，但有可疑属性：{why} '
                          f'（不足以判定不可用，仅记录）')
            break

    return conf, ev


# ================================================================ 判据：时序

def _timing(rec: ExecutionRecord, trees: Sequence[Optional[LayoutNode]],
            target: Optional[Matcher]) -> Tuple[float, List[str]]:
    ev: List[str] = []
    conf = 0.0
    step = rec.step
    if step is None:
        return 0.0, ev

    attempts = list(getattr(step, 'attempts', []) or [])
    ok = bool(getattr(step, 'ok', True))
    kind = _kind_of(step)

    # ---- ① 重试救回：时序问题的教科书形态
    if ok and getattr(step, 'rescued_by_retry', False):
        conf = CONF_HIGH
        detail = '；'.join(
            f'第{a.attempt}次 {"成功" if a.ok else f"失败({_kind_of(a)})"}'
            f'{f" {a.elapsed_ms}ms" if a.elapsed_ms else ""}'
            for a in attempts)
        ev.append(f'该步首次失败、重试后成功 —— 控件最终是能用的，说明只是等得不够：{detail}')
        ev.append(f'该步累计耗时 {getattr(step, "elapsed_ms", 0)}ms，'
                  f'共 {len(attempts)} 次尝试')
        return conf, ev

    if ok:
        return 0.0, ev

    # ---- ② 超时，但目标控件在超时之后的快照里出现了
    if target is not None and trees:
        present_late = _find_in_tree(target, trees[-1])
        present_early = any(_find_in_tree(target, r) for r in trees[:-1])
        if present_late and not present_early:
            conf = CONF_FAIR
            ev.append(f'目标控件 {_spec_text(target)} 在失败后的快照里才出现 '
                      f'（失败前 {len(trees) - 1} 张快照里都没有）→ 界面还没稳定')
            return conf, ev

    if kind == 'TIMEOUT':
        if target is not None and any(_find_in_tree(target, r) for r in trees):
            conf = CONF_MID
            ev.append(f'运行器判定为超时，且目标控件 {_spec_text(target)} 确实在控件树里 '
                      f'→ 等待时长/条件不足，不是控件找不到')
        else:
            conf = CONF_HINT
            ev.append('运行器判定为超时，但控件树里没看到目标控件 '
                      '→ 尚不能确定是等待不足，也可能根本没到那一页')
    return conf, ev


# ================================================================ 判据：定位失败

def _near_miss(rec: ExecutionRecord, target: Optional[Matcher],
               trees: Sequence[Optional[LayoutNode]]) -> str:
    """给定位失败补一条「树里有长得像的控件」的线索 —— 便于人工判断是不是改过 id。"""
    if target is None or not trees:
        return ''
    spec = rec.expected_target
    want_type = want_id = ''
    if isinstance(spec, dict):
        want_type = str(spec.get('type', '') or '')
        want_id = str(spec.get('id', '') or '')
    elif isinstance(spec, str):
        want_id = spec
    for r in trees:
        if r is None:
            continue
        for n in flatten(r, only_visible=False):
            if want_type and n.type == want_type:
                return f'同类型控件 {n.type}(id={n.id or "-"}) 在树里'
            if want_id and want_id[:4] and want_id[:4] in (n.id or ''):
                return f'同前缀控件 id={n.id} 在树里'
    return ''


def _locator(rec: ExecutionRecord, trees: Sequence[Optional[LayoutNode]],
             target: Optional[Matcher], page_expect: str
             ) -> Tuple[float, List[str]]:
    ev: List[str] = []
    step = rec.step
    if step is None or getattr(step, 'ok', True):
        return 0.0, ev

    kind = _kind_of(step)
    if target is None:
        if kind == 'LOCATE':
            ev.append('运行器判定为定位失败，但未提供目标控件规格 '
                      '→ 无法核对控件树，只能按运行器分类给出弱结论')
            return CONF_HINT, ev
        return 0.0, ev

    # 没有控件树快照，就说不了「控件不在树里」—— 除了运行器明确判为定位失败，
    # 那种情况下运行器的分类本身算一条（弱）证据。
    if not trees:
        if kind == 'LOCATE':
            ev.append('运行器判定为定位失败，但没有控件树快照可供核对 '
                      '（无法排除「根本没到那一页」，置信度下调）')
            return CONF_HINT, ev
        return 0.0, ev

    # 控件在树里 → 不是「找不到」，这条判据直接把自己否掉
    if any(_find_in_tree(target, r) for r in trees):
        return 0.0, ev

    if page_expect == 'mismatch':
        # 落点都不对，说「定位失败」是错的归因 —— 交给用例缺陷
        return 0.0, ev

    conf = CONF_GOOD if page_expect == 'match' else CONF_WEAK
    ins = f'{len(trees)} 张控件树快照' if trees else '（无控件树快照，仅凭运行器分类）'
    ev.append(f'目标控件 {_spec_text(target)} 在 {ins} 里都匹配不到')
    if page_expect == 'match':
        ev.append(f'落点页面与用例声明的 expected_page 一致 '
                  f'→ 页面是对的，控件不在，属于定位失败')
    else:
        ev.append('用例未声明 expected_page，无法核对落点是否为目标页（置信度下调）')
    if kind:
        ev.append(f'运行器一级分类为 {kind}')
    near = _near_miss(rec, target, trees)
    if near:
        ev.append(f'线索：{near}（可能是 id 被改/层级变动，可交给定位器自愈）')
    return conf, ev


# ================================================================ 建议

# ================================================================ 判据：环境问题

# 「根本没到被测应用」的链接层线索。注意这里**故意不收裸 `timeout`/`超时`** ——
# 那是时序问题的领地，收进来会把大量真实的时序失败误判成环境问题。
_ENV_PATTERNS = (
    r'device\s+not\s+found', r'no\s+devices?\b', r'设备未连接', r'未检测到设备',
    r'offline', r'disconnected', r'设备已断开', r'设备离线', r'连接已断开',
    r'hdc\s*server', r'hdc\s*超时', r'hdc\s*连接',
    r'unable\s+to\s+connect', r'connect(ion)?\s+(failed|timeout|refused|reset)',
    r'broken\s+pipe', r'transport\s+error', r'socket\s+(error|closed)',
    r'failed\s+to\s+start\s+ability', r'resolve\s+ability',
    r'应用未启动', r'应用已退出', r'启动应用失败',
)


def _environment(rec: ExecutionRecord) -> Tuple[float, List[str]]:
    """设备掉线 / hdc 断链 / 应用拉不起来 → **环境问题**（非归因结论）。

    C 复核的缺陷（2026-09-23 补）：没有这一类时，上面几种失败全部落到
    `UNKNOWN @ 0.00`，还被建议「请补做结果信号采集」——
    **设备都没连上，采不到任何东西**。归因的价值一半在结论、
    一半在「把结论交给对的人」；环境问题该去重跑，不该去应用开发者那里。

    门槛放在 `CONF_MID`（0.8）是刻意的：**只有足够硬的链接层信号才提前定案**，
    证据弱时回去走四分类，免得抢走本该属于「时序」的判断。
    """
    # 崩溃证据已在调用方（`diagnose` 第①步）优先拦掉，这里只判「没到应用」。
    kind = _kind_of(rec.step)
    err = _err_text(rec.step)
    ev: List[str] = []
    hit = ''
    for p in _ENV_PATTERNS:
        if re.search(p, err, re.I):
            hit = p
            break

    if kind == 'DEVICE':
        ev.append(f'运行器判定为设备/hdc 异常（FailureKind.DEVICE）：{err or "(无错误详情)"}')
        return CONF_GOOD, ev
    if kind == 'APP' and hit:
        ev.append(f'应用未能拉起（FailureKind.APP），且错误文本是链接层原因「{hit}」：{err}')
        ev.append('没有崩溃证据 —— 这是「没到应用」而不是「应用行为异常」')
        return CONF_MID, ev
    if hit and kind in ('TIMEOUT', 'UNKNOWN', ''):
        ev.append(f'失败文本命中链接层线索「{hit}」：{err}')
        return CONF_MID, ev
    if hit:
        ev.append(f'（核对过：失败文本像链接层问题「{hit}」，但失败类别是 {kind or "未知"}，'
                  f'不据此定案）')
    return 0.0, ev


def _suggest(cat: Category, rec: ExecutionRecord, verdict_locator_id: str) -> str:
    if cat is Category.APP_DEFECT:
        return ('按**应用缺陷**处理：保留崩溃日志原文与截图，提缺陷单；'
                '不要通过加等待或改用例把它绕过去 —— 那会把真缺陷藏起来。')
    if cat is Category.TIMING:
        return ('按**时序问题**处理：给该步补具名等待（waitFor / 条件等待），'
                '不要加固定 sleep 时长（红线第 5 条禁止硬编码等待）。')
    if cat is Category.LOCATOR:
        if verdict_locator_id:
            return ('按**定位失败**处理：回写 `record_locator_failure(locator_id, reason)` '
                    f'给 A 的定位器自愈（locator_id={verdict_locator_id}，'
                    '由 A 的 LocatorManager 生成，我们原样回写）；'
                    '同时检查该控件的定位策略是否过于依赖 id。')
        return ('按**定位失败**处理：检查该控件的定位策略是否过于依赖 id。'
                '⚠️ 这次**没能回写**定位器自愈 —— 记录里缺 locator_id，'
                '请让执行器把 `locate()` 的 LocateResult.locator_id 一并放进记录。')
    if cat is Category.CASE_DEFECT:
        return ('按**用例缺陷**处理：修用例（前置条件 / 期望值 / 步骤顺序），'
                '不要动应用代码。')
    if cat is Category.ENVIRONMENT:
        return ('按**环境问题**处理：先查设备连接（`hdc list targets`）、'
                'hdc server 与取景器状态，确认设备在线后原地重跑这条用例；'
                '不要据此提应用缺陷单，也不必去补采集信号 —— '
                '设备没连上时采不到任何东西，重跑才是第一步。')
    return ('证据不足：补做结果信号采集（collect_signals）并保留失败前后的控件树快照，'
            '再重新归因；在此之前不要直接判给某一方。')


# ================================================================ 接入辅助（给 C 的闭环用）

#: 产物目录里优先当成控件树快照的文件名特征
_SNAPSHOT_HINTS = ('layout', 'dump', 'tree', 'snapshot')

#: 明确**不是**控件树的产物（避免把报告/用例当成快照解析）
_SNAPSHOT_EXCLUDE = ('report', 'graph', 'summary', 'suite', 'case',
                     'signals', 'steps', 'manifest', 'index')


def load_snapshots(artifact_dir: Any, *, step_index: Optional[int] = None,
                   limit: int = 8) -> List[LayoutNode]:
    """从产物目录里捞控件树快照，**按文件名排序**返回（最早的在前）。

    这是为 C 的「闭环接入」补的：`ExecutionRecord.trees` 需要失败步前后的
    `dumpLayout` 快照，而产物目录里混着报告 / 用例 / 图等一堆 json，
    **不能盲读** —— 所以这里逐个尝试解析，只有能解析出带 bounds 的控件树才采用，
    其余静默跳过（坏文件不该让归因失败）。

    C 那边已实现「失败步自动留 2 张快照」；本函数对命名**不挑食**：
    带 `layout / dump / tree / snapshot` 字样的排前面，其余按名字顺序兜底，
    这样他的命名调整不会让我这边失效。

    Parameters
    ----------
    step_index: 若给出，只优先取文件名里带该步号（`3` / `03` / `step3`）的快照，
                取不到就退回全部 —— **宁可多给几张，也不要给空**。
    limit:      最多返回几张（默认 8）。
    """
    import json as _json

    if not artifact_dir or not os.path.isdir(str(artifact_dir)):
        return []

    names = []
    for fn in sorted(os.listdir(str(artifact_dir))):
        low = fn.lower()
        if not low.endswith('.json'):
            continue
        if any(bad in low for bad in _SNAPSHOT_EXCLUDE):
            continue
        names.append(fn)

    if step_index is not None:
        want = (f'{step_index}', f'{step_index:02d}', f'step{step_index}',
                f'step_{step_index}', f'step-{step_index}')
        prefer = [n for n in names if any(w in n.lower() for w in want)]
        if prefer:
            names = prefer

    names.sort(key=lambda n: (0 if any(h in n.lower() for h in _SNAPSHOT_HINTS)
                              else 1, n))

    out: List[LayoutNode] = []
    for fn in names:
        if len(out) >= limit:
            break
        path = os.path.join(str(artifact_dir), fn)
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                data = _json.load(fh)
        except (OSError, ValueError):
            continue
        root = _as_tree(data)
        if root is not None:
            out.append(root)
    return out


def _as_tree(data: Any) -> Optional[LayoutNode]:
    """把一份 json 尝试解析成控件树；不像控件树就返回 None。"""
    if not isinstance(data, dict):
        return None
    try:
        root = parse_layout(data)
    except Exception:
        return None
    try:
        nodes = flatten(root, only_visible=False)
    except Exception:
        return None
    if not nodes:
        return None
    # 至少要有一个带非零 bounds 的节点，否则多半是别的 json 恰好长这样
    if not any(n.rect.width > 0 and n.rect.height > 0 for n in nodes):
        return None
    return root


def diagnose_failed_step(step: Any, *, bundle: str = '', ability: str = 'EntryAbility',
                         artifact_dir: Any = None, trees: Optional[Sequence[Any]] = None,
                         signals: Any = None, hdc: Any = None, out_dir: Any = None,
                         expected_target: Any = None, expected_page: str = '',
                         precondition: Any = None, locator_id: str = '',
                         hilog: Any = None, started_at: Optional[float] = None,
                         locator_sink: Optional[Callable[[str, str], None]] = None,
                         ) -> Verdict:
    """**给执行器用的一步到位入口**：一个失败步结果 → 一个 `Verdict`。

    C 的复核说得对：「闭环没闭上」和「三个自证式 KPI」是同一件事的两面 ——
    `diagnose()` 一旦接进失败分支，就同时得到真实失败快照上的分类数字。
    他要的落点在 `runner.py`（**那是 C 的文件**，我不动），所以我把
    「构造记录」这段最容易写错、也最容易漏参的部分做成了这个函数：

        报告 / 执行器侧只需要三行
        ------------------------------------------------
        if not sr.ok:
            v = diagnose_failed_step(sr, bundle=self.bundle,
                                     artifact_dir=adir, signals=sig)
            res.verdicts.append(v)          # 报告里渲染 v.to_dict() 即可
        ------------------------------------------------

    它会替你做完这些事：从产物目录捞控件树快照、按需调 `collect_signals()`
    采集信号（只有传了 `hdc` 才采）、把 `locator_id` 原样带上、最后调 `diagnose()`。
    任何一步拿不到都不报错 —— 归因引擎的立场是**证据不足就说不确定**，
    而不是因为缺一个入参就崩掉整轮执行。
    """
    probed_trees: List[Optional[LayoutNode]] = []
    if trees is not None:
        probed_trees = [t if isinstance(t, LayoutNode) else _as_tree(t) for t in trees]
    if not any(probed_trees):
        probed_trees = load_snapshots(artifact_dir,
                                     step_index=getattr(step, 'index', None))

    sig = signals
    if sig is None and hdc is not None and bundle:
        try:
            sig = collect_signals(hdc, bundle,
                                  out_dir=str(out_dir or artifact_dir or '.'))
        except Exception:
            sig = None      # 采不到信号不是错误：归因会按「证据不足」处理

    lid = locator_id or str(getattr(step, 'locator_id', '') or '')
    if not lid:
        extra_lid = getattr(step, 'extra', None)
        if isinstance(extra_lid, dict):
            lid = str(extra_lid.get('locator_id', '') or '')

    rec = ExecutionRecord(
        bundle=bundle, ability=ability, step=step, trees=probed_trees,
        signals=sig, expected_target=expected_target, expected_page=expected_page,
        precondition=precondition, locator_id=lid, hilog=hilog,
        started_at=started_at)
    return diagnose(rec, locator_sink=locator_sink)


# ================================================================ 主入口

def diagnose(record: Any, *, locator_sink: Optional[Callable[[str, str], None]] = None,
             ) -> Verdict:
    """把一个失败执行记录归到四类之一。

    Parameters
    ----------
    record:
        `ExecutionRecord` / dict / `runner.StepResult` / `runner.CaseResult`。
    locator_sink:
        可选。判定为**定位失败**时用它回写 A 的定位器自愈，签名为
        `sink(locator_id, reason)`，与 A 的 `record_locator_failure(locator_id, reason)` 对齐。

        口径已与 A 对齐（2026-09-22）：**`locator_id` 由 A 的 LocatorManager 统一生成**
        （形如 `L3_登录按钮`），归因侧不自己拼，只把 `LocateResult.locator_id`
        原样带回去。记录里没带这个 id 时**不会调用 sink**（免得在 A 的健康度台账里
        塞进一条空 id 的假账），并在 evidence 里写明缺什么。

    Returns
    -------
    Verdict（category + confidence + evidence + suggestion）
    """
    rec = coerce_record(record)
    trees = _parse_trees(rec)
    target = _as_matcher(rec.expected_target)
    pre = _as_matcher(rec.precondition)
    page_expect = _page_expectation(rec, trees)

    scores: Dict[str, float] = {}
    ev_all: List[str] = []

    # ---- ① 应用缺陷优先：崩溃类证据一票否决
    app_conf, app_ev, used = _app_defect(rec)
    scores[Category.APP_DEFECT.value] = app_conf
    if app_conf >= APP_CONFIDENCE_FLOOR:
        v = Verdict(category=Category.APP_DEFECT, confidence=app_conf,
                    evidence=app_ev, step_index=_step_index(rec),
                    signals_used=used, scores=scores)
        v.suggestion = _suggest(Category.APP_DEFECT, rec, '')
        return v

    # 不足以定案的应用类证据**不能丢**：它们记录的是「核对过、已排除」，
    # 报告里看不到这一段，读的人就不知道引擎到底查没查过崩溃日志。
    ruled_out = _ruled_out(app_ev)

    # ---- ② 环境/链路问题：与崩溃判据同样提前定案
    #     顺序很关键：放在「崩溃一票否决」之后（真崩溃仍然是应用缺陷），
    #     但放在四分类之前 —— 设备都没连上时，讨论「定位失败还是用例缺陷」
    #     是没有意义的，而且会把结论交给错误的人去修。
    env_conf, env_ev = _environment(rec)
    scores[Category.ENVIRONMENT.value] = env_conf
    if env_conf >= CONF_MID:
        v = Verdict(category=Category.ENVIRONMENT, confidence=env_conf,
                    evidence=env_ev + ruled_out, step_index=_step_index(rec),
                    scores=scores)
        v.suggestion = _suggest(Category.ENVIRONMENT, rec, '')
        return v
    if env_conf > 0:                    # 有链接层迹象但不够硬 → 只留痕，不定案
        ruled_out = ruled_out + env_ev

    # ---- ③ 成功但被重试救回 → 时序
    if rec.step is not None and getattr(rec.step, 'ok', False) \
            and getattr(rec.step, 'rescued_by_retry', False):
        t_conf, t_ev = _timing(rec, trees, target)
        scores[Category.TIMING.value] = t_conf
        v = Verdict(category=Category.TIMING, confidence=t_conf,
                    evidence=t_ev + ruled_out, step_index=_step_index(rec),
                    scores=scores)
        v.suggestion = _suggest(Category.TIMING, rec, '')
        return v

    # ---- ④ 失败步：三条解释性判据取最高分
    c_conf, c_ev = _case_defect(rec, trees, target, pre, page_expect)
    t_conf, t_ev = _timing(rec, trees, target)
    l_conf, l_ev = _locator(rec, trees, target, page_expect)
    scores.update({Category.CASE_DEFECT.value: c_conf,
                   Category.TIMING.value: t_conf,
                   Category.LOCATOR.value: l_conf})

    cands = [(c_conf, Category.CASE_DEFECT, c_ev),
             (t_conf, Category.TIMING, t_ev),
             (l_conf, Category.LOCATOR, l_ev)]
    # 同分按 _TIE_ORDER 定序，让「同分怎么办」有据可查
    cands.sort(key=lambda it: (-it[0], _TIE_ORDER.index(it[1])))
    best_conf, best_cat, best_ev = cands[0]

    if best_conf <= 0.0:
        v = Verdict(category=Category.UNKNOWN, confidence=0.0,
                    evidence=['四类判据都没有拿到可用证据：'
                              '没有结果信号、没有控件树快照，也没有可核对的失败类别']
                             + ruled_out,
                    step_index=_step_index(rec), scores=scores)
        v.suggestion = _suggest(Category.UNKNOWN, rec, '')
        return v

    # 附上落选判据里分数最高的那些证据，便于人工复核（不改变结论）
    for conf, cat, ev in cands[1:]:
        if conf > 0:
            ev_all.append(f'—— 另一条候选判据「{CATEGORY_CN[cat]}」'
                          f'置信度 {conf:.2f}（证据供复核）：' + '；'.join(ev[:2]))

    lid = rec.locator_id if best_cat is Category.LOCATOR else ''
    v = Verdict(category=best_cat, confidence=best_conf,
                evidence=list(best_ev) + ev_all + ruled_out,
                step_index=_step_index(rec), scores=scores, locator_id=lid)
    if best_cat is Category.LOCATOR:
        note = locator_id_note(rec)
        if note:
            v.evidence.append(f'（{note}）')
    v.suggestion = _suggest(best_cat, rec, lid)

    if best_cat is Category.LOCATOR and locator_sink is not None and lid:
        reason = f'{rec.case_name or "(未命名用例)"}：' + '；'.join(best_ev[:2])
        try:
            locator_sink(lid, reason)
        except Exception:
            pass
    return v


def _ruled_out(app_ev: List[str]) -> List[str]:
    """把「核对过、但不足以定案」的应用类证据整理成一段，附在结论后面。

    这段的读者是**人**：它回答「引擎到底查没查崩溃日志、为什么没算数」。
    没有它，一个「定位失败」的结论看起来就像引擎压根没看信号。
    """
    if not app_ev:
        return []
    return ['—— 已核对但不足以定案为应用缺陷的信号（供复核）：'
            + '；'.join(app_ev)]


def _step_index(rec: ExecutionRecord) -> Optional[int]:
    idx = getattr(rec.step, 'index', None)
    return int(idx) if isinstance(idx, int) else None


def diagnose_all(records: Iterable[Any],
                 locator_sink: Optional[Callable[[str, str], None]] = None
                 ) -> List[Verdict]:
    """批量归因（KPI 表要的是「一批故障注入的准确率」）。"""
    return [diagnose(r, locator_sink=locator_sink) for r in records]


def summarize(verdicts: Sequence[Verdict]) -> Dict[str, Any]:
    """归因结果的分布统计，直接进报告。"""
    dist: Dict[str, int] = {}
    for v in verdicts:
        dist[v.category.value] = dist.get(v.category.value, 0) + 1
    return {'total': len(verdicts), 'by_category': dist,
            'by_category_cn': {CATEGORY_CN[Category(k)]: n for k, n in dist.items()},
            'avg_confidence': (round(sum(v.confidence for v in verdicts)
                                     / len(verdicts), 4) if verdicts else 0.0)}
