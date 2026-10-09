"""
L3 语义层 —— 多模态视觉定位
===========================

控件树是主通道，但有盲区：Canvas 绘制的内容、纯图标按钮、无 id/text 的
自定义控件，在 dumpLayout 里几乎是空的。这时靠视觉通道补位。

双通道融合策略：
    1. 先走控件树（快、准、可解释）
    2. 控件树无命中，或命中项无任何标识时，降级到视觉通道
    3. 两者都给出候选时做 IoU 交叉校验，一致才采信（降低模型幻觉风险）

Provider 是可插拔的：换模型、换供应商、退化成纯规则，都不影响上层。
"""

from __future__ import annotations

import base64
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .layout import LayoutNode, Rect, flatten


# ---------------------------------------------------------------- 通道门槛

#: 双通道交叉校验的默认门槛：IoU 重叠下限 + 视觉置信度下限。
#: **单一出处** —— `HybridLocator` / `TieredVisionLocator` 的构造默认值，
#: 以及 locator 自愈「换视觉通道」时的唯一性判据，都引用这里。
#: 两处各写一份字面量、再靠注释声明「与某某对齐」，是必然漂移的写法。
IOU_MIN_DEFAULT = 0.3
CONF_MIN_DEFAULT = 0.4


# ---------------------------------------------------------------- 异常

class VisionError(Exception):
    """视觉通道运行时错误（网络 / 服务端 / 返回结构异常）。"""


class VisionConfigError(VisionError):
    """视觉 Provider 配置缺失（环境变量没配齐）。"""


# ---------------------------------------------------------------- 数据结构

@dataclass
class VisualTarget:
    """视觉通道给出的定位结果。

    uncertain 的语义（任务卡 A2）：该结果**没有**得到另一个通道的交叉印证。
    - 视觉命中但与控件树候选不重叠（IoU < 阈值）→ uncertain=True，
      仍然返回（「不盲点」），但调用方应视为低可信，只在无其他选择时采用；
    - 与控件树候选重叠 ≥ 阈值 → 采用控件树精确边界，uncertain=False。
    """
    label: str
    rect: Rect
    confidence: float = 0.0
    source: str = 'vision'
    uncertain: bool = False
    raw: Optional[Dict[str, Any]] = None

    @property
    def center(self) -> Tuple[int, int]:
        return self.rect.center


# ---------------------------------------------------------------- Provider

class VisionProvider(ABC):
    """视觉定位能力提供者。"""

    name = 'abstract'

    @abstractmethod
    def locate(self, image_path: str, instruction: str,
               screen_w: int, screen_h: int,
               control_hints: Optional[List[Dict[str, Any]]] = None
               ) -> List[VisualTarget]:
        """在截图中定位符合 instruction 描述的目标，返回候选列表。"""


class MockProvider(VisionProvider):
    """不依赖模型的占位实现 —— 用于单测与无模型环境下的联调。

    它在传入的 control_hints 里做关键词匹配，返回对应控件的区域。
    这样即使没有模型，整条「视觉通道」的管线也能被验证。
    """
    name = 'mock'

    def locate(self, image_path, instruction, screen_w, screen_h,
               control_hints=None) -> List[VisualTarget]:
        out: List[VisualTarget] = []
        kw = instruction.strip().lower()
        for h in (control_hints or []):
            blob = ' '.join(str(h.get(k, '')) for k in
                            ('type', 'id', 'text', 'descr', 'hint')).lower()
            if kw and kw in blob:
                out.append(VisualTarget(label=str(h.get('text') or h.get('id') or h.get('type')),
                                        rect=Rect.parse(h.get('bounds')),
                                        confidence=0.5, source='mock'))
        return out


class OpenAICompatibleProvider(VisionProvider):
    """任意 OpenAI 兼容的多模态接口。

    只需配置 base_url / api_key / model，即可适配绝大多数云端或自建服务。
    不绑定单一供应商 —— 这正是命题「避免强绑定单一模型」的要求。

    **密钥纪律（任务卡 A1 红线）**：key 一律走环境变量，绝不写进代码。
    环境变量（按组优先级，任一组配齐即可）：
        OHAUTO_VISION_BASE_URL  /  OHAUTO_VISION_API_KEY  /  OHAUTO_VISION_MODEL
        OHAUTO_LLM_BASE_URL     /  OHAUTO_LLM_API_KEY     /  OHAUTO_LLM_MODEL
        OH_LLM_BASE_URL         /  OH_LLM_API_KEY         /  OH_LLM_MODEL   （旧）
    第二组是 B3 定的全组统一口径（2026-09-23 对齐）：视觉与用例生成共用
    一份 key 时，全组只需配一组 OHAUTO_LLM_*；视觉专用配置仍以
    OHAUTO_VISION_* 优先。
    """

    name = 'openai-compatible'

    PROMPT = """你是移动应用 UI 定位助手。请在下述截图中找到符合用户描述的目标控件。

用户描述：{instruction}

截图尺寸：{w} x {h} 像素

{control_context}

请只输出 JSON 数组，不要输出其他内容。每个元素形如：
{{"label": "控件简短名称", "bbox": [left, top, right, bottom], "confidence": 0.0到1.0}}

bbox 使用截图像素坐标，原点在左上角。
若图中不存在该目标，输出空数组 []。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 60, max_hints: int = 40,
                 disable_thinking: bool = False):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_hints = max_hints
        #: 关掉模型的思考模式：payload 里加 `{"thinking": {"type": "disabled"}}`。
        #: 为什么值得做成生产开关而不是工装补丁：
        #:  1. **延迟** —— 目标定位不需要长思维链，实测 p50 1059ms → 3238ms（3.06×）；
        #:  2. **计费** —— 思维链按输出计价；
        #:  3. **口径** —— 思考开着时 `temperature` 不生效，我们传的 0 是空转，
        #:     同一张图两次可能给不同 bbox，「准确率」因此不可复现。
        self.disable_thinking = disable_thinking

    # ------------------------------------------------------ 装配
    @classmethod
    def from_env(cls, **kw) -> 'OpenAICompatibleProvider':
        """从环境变量装配。缺任一项直接抛错 —— 静默降级成 Mock 会让
        「以为接了真模型、其实在跑关键词匹配」这种假绿测试混进 CI。

        `disable_thinking` **只在调用方显式传了**才转发：子类可能在自己的
        `__init__` 里自塞 `True`（见工装的 `NoThinkingProvider`），
        无条件带上默认值会把它的开关反向清掉。

        环境变量读取已下沉到 `llm_transport.env_triple`（S9：逐变量多前缀回退
        只一份实现）；本方法只保留 vision 专有的报错口径（VisionConfigError）。
        """
        from .llm_transport import env_triple
        base_url, api_key, model, missing = env_triple(
            ('OHAUTO_VISION_BASE_URL', 'OHAUTO_LLM_BASE_URL', 'OH_LLM_BASE_URL'),
            ('OHAUTO_VISION_API_KEY', 'OHAUTO_LLM_API_KEY', 'OH_LLM_API_KEY'),
            ('OHAUTO_VISION_MODEL', 'OHAUTO_LLM_MODEL', 'OH_LLM_MODEL'),
            overrides=kw)
        if missing:
            raise VisionConfigError(
                '视觉 Provider 缺少配置: ' + ', '.join(missing) +
                '（请设置环境变量 OHAUTO_VISION_* 或 OHAUTO_LLM_* 三件套，'
                'key 绝不写进代码）')
        extra: Dict[str, Any] = {}
        if 'disable_thinking' in kw:
            extra['disable_thinking'] = bool(kw['disable_thinking'])
        return cls(base_url=base_url, api_key=api_key, model=model,
                   timeout=int(kw.get('timeout', 60)), **extra)

    @staticmethod
    def available() -> bool:
        """环境变量是否齐备（供上层决定是否启用视觉通道，不抛错）。

        判定必须与 `from_env()` 的回退链**逐变量对齐**：base_url / api_key /
        model 三件套各自只要在任一前缀里取到值即可（三个前缀见类 docstring）。

        ⚠️ **不要改成「任一一组三个变量配齐」**（2026-09-23 集成实测）：
        `from_env()` 是**逐变量**回退取值 —— 这正是「DeepSeek 生成用例 +
        Qwen-VL 看图」能分开配的前提，也是 `test_vision_group_has_priority`
        依赖的行为。改成按组会让 available() 比 from_env() **更严格**：
        混搭配置下（如 `OHAUTO_VISION_BASE_URL` + `OHAUTO_LLM_API_KEY` +
        `OHAUTO_LLM_MODEL`）会出现「`from_env()` 装配成功、`available()`
        却说不可用」→ 上层据此**静默跳过视觉通道**，而用户明明配好了。
        复现脚本：`_out/probe_env_consistency.py`（2/5 场景矛盾）；
        回归钉子：`tests/test_c_env_consistency.py`。
        """
        def has(*names: str) -> bool:
            return any(os.environ.get(n, '') for n in names)

        return (has('OHAUTO_VISION_BASE_URL', 'OHAUTO_LLM_BASE_URL', 'OH_LLM_BASE_URL')
                and has('OHAUTO_VISION_API_KEY', 'OHAUTO_LLM_API_KEY', 'OH_LLM_API_KEY')
                and has('OHAUTO_VISION_MODEL', 'OHAUTO_LLM_MODEL', 'OH_LLM_MODEL'))

    # ------------------------------------------------------ 请求
    _MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
             '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp'}

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """传输层：只发请求、只解析 body。异常上抛给 locate 统一包裹 ——
        这样测试替身可以只覆写传输层、复用上层的异常语义。

        实现已下沉到 `llm_transport.post_chat`（结构债 S9：传输层只一份实现）；
        本方法保留为实例方法，是因为测试用 `mock.patch.object(..., '_post', ...)`
        拦截传输层来断言 payload，去掉方法会让那些钉子失去拦截点。
        """
        from .llm_transport import post_chat
        return post_chat(self.base_url, self.api_key, payload,
                         timeout=self.timeout)

    def _build_context(self, hints: Optional[List[Dict[str, Any]]]) -> str:
        if not hints:
            return '（无控件树信息）'
        lines = ['已从控件树解析出以下可交互元素，可作参考（坐标同样为截图像素）：']
        for h in hints[:self.max_hints]:
            # deep 截断到 60 字符（C 专文 ③⑤）：容器节点的 deep 拼接了整组
            # 文案，不截会吃掉 token 预算 —— 这是 L2/L3 的收益点，模型终于
            # 能看到每个候选框里写的是什么，而不是 28 个空壳。
            lines.append('  - type=%s id=%s text=%r deep=%r bounds=%s' % (
                h.get('type', ''), h.get('id', ''), h.get('text', ''),
                str(h.get('deep', ''))[:60], h.get('bounds', '')))
        return '\n'.join(lines)

    def locate(self, image_path, instruction, screen_w, screen_h,
               control_hints=None) -> List[VisualTarget]:
        with open(image_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')

        ext = os.path.splitext(image_path)[1].lower()
        mime = self._MIME.get(ext, 'image/png')

        prompt = self.PROMPT.format(instruction=instruction, w=screen_w, h=screen_h,
                                    control_context=self._build_context(control_hints))
        payload = {
            'model': self.model,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url',
                     'image_url': {'url': f'data:{mime};base64,{b64}'}},
                ],
            }],
            'temperature': 0,
        }
        if self.disable_thinking:
            payload['thinking'] = {'type': 'disabled'}
        body = None
        try:
            body = self._post(payload)
        except VisionError:
            raise
        except Exception as e:      # 网络错/超时/非 2xx 统一包一层，方便上层降级
            raise VisionError(f'视觉服务请求失败: {type(e).__name__}: {e}') from e
        try:
            text = body['choices'][0]['message']['content']
        except (KeyError, IndexError, TypeError) as e:
            raise VisionError(f'视觉服务返回结构异常: {e}') from e
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> List[VisualTarget]:
        """从模型输出里抠出 JSON 数组并翻译成 `VisualTarget`。

        抠数组已下沉到 `llm_transport.extract_json_array`（S9：围栏解析只一份实现）；
        本方法只负责把每个 dict 翻译成带 `Rect`/`confidence` 的 `VisualTarget` ——
        这是 vision 专有的口径，不与 generator 的 `parse_json_payload` 共用。
        """
        from .llm_transport import extract_json_array
        out = []
        for item in extract_json_array(text):
            if not isinstance(item, dict):
                continue
            bbox = item.get('bbox') or item.get('bounds')
            if not bbox:
                continue
            out.append(VisualTarget(
                label=str(item.get('label', '')),
                rect=Rect.parse(bbox),
                confidence=float(item.get('confidence', 0.5)),
                source='vision',
                raw=item,
            ))
        return out


# ---------------------------------------------------------------- 融合定位器

def _collect_hints(root: LayoutNode) -> List[Dict[str, Any]]:
    """把控件树里**可见且可交互**的节点压成给模型的候选清单。

    两个融合定位器（树+视觉的 `HybridLocator`、分层的 `TieredVisionLocator`）
    共用这一份 —— 这段以前是两份逐字符相同的拷贝，且已经漂移过一次；
    合一之后上下文格式只有一处出处，改 prompt 不会漏掉另一半。
    """
    out = []
    for n in flatten(root, only_visible=True, only_interactive=True):
        out.append({'type': n.type, 'id': n.id, 'text': n.text,
                    'deep': n.text_deep,
                    'descr': n.descr, 'hint': n.hint,
                    'bounds': n.rect.to_dict()})
    return out


class HybridLocator:
    """控件树 + 视觉 双通道融合定位。"""

    def __init__(self, provider: Optional[VisionProvider] = None,
                 iou_threshold: float = IOU_MIN_DEFAULT,
                 min_confidence: float = CONF_MIN_DEFAULT,
                 verbose: bool = True):
        self.provider = provider or MockProvider()
        self.iou_threshold = iou_threshold
        self.min_confidence = min_confidence
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f'[vision] {msg}')

    def _hints(self, root: LayoutNode) -> List[Dict[str, Any]]:
        return _collect_hints(root)

    def locate(self, root: LayoutNode, image_path: str, instruction: str,
               screen_w: int, screen_h: int) -> Optional[VisualTarget]:
        """融合定位，返回最可信的目标；完全找不到返回 None。"""
        hints = self._hints(root)

        # 通道一：控件树按描述模糊匹配
        # blob 追加 text_deep（C 专文 ④）：真机可交互容器的文案在子节点，
        # 只拼自身字段会对「蓝牙」恒判 False，与 TieredVisionLocator 的
        # L1 判定口径必须保持一致。
        kw = instruction.strip().lower()
        tree_hits = []
        for n in flatten(root, only_visible=True):
            blob = ' '.join([n.type, n.id, n.text, n.text_deep,
                             n.descr, n.hint] +
                            [str(v) for v in n.attributes.values()]).lower()
            if kw and kw in blob:
                tree_hits.append(n)

        # 通道二：视觉
        try:
            vis = [t for t in self.provider.locate(image_path, instruction,
                                                   screen_w, screen_h, hints)
                   if t.confidence >= self.min_confidence]
        except Exception as e:
            self._log(f'视觉通道失败，仅依赖控件树: {type(e).__name__}: {e}')
            vis = []

        self._log(f'控件树命中 {len(tree_hits)} 项，视觉命中 {len(vis)} 项')

        # 融合：视觉候选若与控件树命中重叠，则提升置信度并采用控件树精确边界
        best: Optional[VisualTarget] = None
        for v in vis:
            matched = False
            for n in tree_hits:
                iou = v.rect.overlap_ratio(n.rect)
                if iou >= self.iou_threshold:
                    self._log(f'交叉校验通过 IoU={iou:.2f} -> {n.label}')
                    raw = dict(v.raw) if v.raw else {}
                    raw['iou'] = round(iou, 4)
                    cand = VisualTarget(label=n.label, rect=n.rect,
                                        confidence=min(1.0, v.confidence + 0.3),
                                        source='hybrid', uncertain=False, raw=raw)
                    if best is None or cand.confidence > best.confidence:
                        best = cand
                    matched = True
                    break
            if not matched:
                # A2：视觉有命中、但控件树不背书 —— 不盲点，照常返回，
                # 但标记 uncertain，调用方可以按置信度决定是否采用。
                # raw 里带上「它没跟谁重叠」，报告里能解释为什么标记。
                raw = dict(v.raw) if v.raw else {}
                raw['iou'] = 0.0
                v.uncertain = True
                v.raw = raw

        if best is not None:
            return best

        # 无交叉验证：控件树优先（可解释性强），否则用视觉
        if tree_hits:
            # 取最紧（面积最小）：blob 补 deep 之后容器也会命中，若按
            # 最大面积取会拿到整页容器 —— 与 L1 的取最紧口径保持一致。
            n = min(tree_hits, key=lambda x: x.rect.area)
            self._log(f'仅控件树命中 -> {n.label}')
            return VisualTarget(label=n.label, rect=n.rect,
                                confidence=0.6, source='tree')

        if vis:
            v = max(vis, key=lambda x: x.confidence)
            self._log(f'仅视觉命中(uncertain={v.uncertain}) -> '
                      f'{v.label} (conf={v.confidence:.2f})')
            return v

        self._log('两通道均无命中')
        return None


# ---------------------------------------------------------------- A4 分层成本控制

class TieredVisionLocator:
    """三层成本递增的视觉定位（任务卡 A4）。

    L1  静态候选筛选   —— 只在控件树 hints 里按关键词匹配，约 50ms，**不调模型**
    L2  候选区域送模型 —— 只把目标附近的候选 + 区域上下文喂给模型（1–2s）
    L3  全屏兜底       —— L2 没命中时，把全部 hints 交给模型（约 3s，最贵）

    缓存：key = 页面指纹 + 目标描述。同一页面同一描述，第二次直接吃缓存。
    页面指纹由调用方传（B1 的 content signature，或任意能区分页面的稳定串）——
    摘要器 treesum / explorer 的 PageSignature.content 都可以。

    「裁剪」的诚实说明：纯标准库没有图像裁剪能力（不引新依赖是分工卡约束）。
    L2 在无 PIL 环境下的实现是**候选区域上下文注入**：只送区域内的 hints，
    并在 prompt 上下文里写明目标区域，引导模型把注意力放在局部；
    如果宿主环境装了 PIL，可通过 crop_fn 钩子注入真正的像素裁剪，
    本类不检测、不依赖 PIL。

    全部层级都有耗时统计（perf 计数），验收指标「视觉平均耗时 ≤ 2s、
    缓存命中率 ≥ 60%」直接读 cache_hit_rate / avg_model_ms。
    """

    def __init__(self, provider: Optional[VisionProvider] = None,
                 region_margin: int = 60,
                 min_confidence: float = CONF_MIN_DEFAULT,
                 verbose: bool = False,
                 crop_fn=None,
                 l1_require_exact: bool = True):
        self.provider = provider or MockProvider()
        self.region_margin = region_margin
        self.min_confidence = min_confidence
        self.verbose = verbose
        self.crop_fn = crop_fn      # Optional[Callable[[str, Rect], str]]
        # L1 保守/激进开关（C 专文 ⑥，2026-09-23 定稿）：
        #   True（默认，保守版 E）—— 最紧匹配的 deep 文案须与指令完全相等
        #     才免调模型。实测 70% 免调、14/14 全对、零误命中；
        #   False（激进版 D）—— 只要取到最紧就返回。实测 100% 免调、
        #     19/19（唯一错例的标注本身无效）。跑一段时间看误命中率再放开。
        self.l1_require_exact = l1_require_exact
        # 缓存与统计（进程内即可；跨进程持久化留给 runner 的报告层）
        self._cache: Dict[Tuple[str, str], Optional[VisualTarget]] = {}
        self.cache_hits = 0
        self.cache_misses = 0
        self.model_ms = 0.0
        self.model_calls = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f'[tiered] {msg}')

    @property
    def cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return self.cache_hits / total if total else 0.0

    @property
    def avg_model_ms(self) -> float:
        return self.model_ms / self.model_calls if self.model_calls else 0.0

    # ------------------------------------------------------ L1
    @staticmethod
    def _static_candidates(hints, instruction) -> List[Dict[str, Any]]:
        kw = instruction.strip().lower()
        if not kw:
            return []
        out = []
        for h in hints:
            # 匹配键加 deep（C 专文 ③）：真机可交互容器自身文案恒为空，
            # 不借 text_deep 的话 28 条线索全空、L1 恒命中 0 条。
            blob = ' '.join(str(h.get(k, '')) for k in
                            ('type', 'id', 'text', 'deep',
                             'descr', 'hint')).lower()
            if kw in blob:
                out.append(h)
        return out

    # ------------------------------------------------------ L2
    def _region_context(self, anchors: List[Dict[str, Any]],
                        hints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """取锚点候选的外包区域（带 margin），筛出区域内的 hints。

        `hints` 由调用方显式传入（过去读实例属性 `_all_hints_cache`，
        那是跨调用存活的隐藏状态）。返回值第一个元素是锚点本身。
        """
        if not anchors:
            return []
        rects = [Rect.parse(a.get('bounds')) for a in anchors]
        l = min(r.left for r in rects) - self.region_margin
        t = min(r.top for r in rects) - self.region_margin
        r_ = max(r.right for r in rects) + self.region_margin
        b = max(r.bottom for r in rects) + self.region_margin
        region = Rect(l, t, r_, b)
        near = [h for h in (hints or [])
                if region.overlap_ratio(Rect.parse(h.get('bounds'))) > 0]
        # 锚点放最前，模型对列表开头的元素注意力更高
        return anchors + [h for h in near if h not in anchors]

    # ------------------------------------------------------ 模型调用
    def _call_model(self, image_path, instruction, screen_w, screen_h,
                    hints, tag: str) -> List[VisualTarget]:
        import time as _t
        t0 = _t.perf_counter()
        try:
            targets = self.provider.locate(image_path, instruction,
                                           screen_w, screen_h, hints)
        finally:
            self.model_ms += (_t.perf_counter() - t0) * 1000.0
            self.model_calls += 1
        self._log(f'{tag}: {len(targets)} 命中, 累计均值 {self.avg_model_ms:.0f}ms')
        return targets

    # ------------------------------------------------------ 主入口
    def locate(self, root: LayoutNode, image_path: str, instruction: str,
               screen_w: int, screen_h: int,
               page_fingerprint: str = '') -> Optional[VisualTarget]:
        """分层定位。page_fingerprint 不传时用控件树 type 序列兜底
        （宽容签名——比完整 content 便宜，且同页面文案变化不会打穿缓存）。"""
        hints = self._hints(root)
        if not page_fingerprint:
            page_fingerprint = ','.join(n.type for n in flatten(root, only_visible=True))

        key = (page_fingerprint, instruction.strip())
        if key in self._cache:
            self.cache_hits += 1
            self._log(f'缓存命中 rate={self.cache_hit_rate:.2f} key={key[1]!r}')
            return self._cache[key]
        self.cache_misses += 1

        # --- L1：静态候选（不调模型） ---
        # C 专文 ⑥（2026-09-23 定稿）：补 deep 之后容器也会命中（deep 拼接
        # 了子项文案），「必须唯一命中」在真机上基本永不成立 —— 实测补了
        # 文案但保留唯一判定，免调模型率仍是 10%。改为取**最紧匹配**：
        # 容器的 rect 一定比它吞掉的行大，面积最小的那个就是真正的目标行。
        static = self._static_candidates(hints, instruction)
        if static:
            h = min(static, key=lambda x: Rect.parse(x.get('bounds')).area)
            # 保守口径的「相等」判定：deep 与指令完全相等即免调模型（C 专文
            # ⑥ 的口径，真机实测 70%/14 对 14）；另纳入 id / 自身 text 的
            # **精确相等** —— 修复前「id 精确唯一命中」就走 L1，只查 deep
            # 会让 id 形态的指令（模拟器页面很常见）退化到 L2，属回归。
            kw = instruction.strip().lower()
            exact = (str(h.get('deep', '')).strip().lower() == kw
                     or str(h.get('id', '')).strip().lower() == kw
                     or str(h.get('text', '')).strip().lower() == kw)
            if exact or not self.l1_require_exact:
                r = Rect.parse(h.get('bounds'))
                self._log(f'L1 取最紧命中（未调模型，exact={exact}，'
                          f'候补 {len(static)} 项）')
                result = VisualTarget(
                    label=str(h.get('text') or h.get('id') or h.get('type')),
                    rect=r, confidence=0.65, source='tree')
                self._cache[key] = result
                return result
            # 保守口径：最紧候选的文案与指令不相等 → 宁可多花一次模型调用
            self._log(f'L1 最紧候选文案与指令不相等（exact=False），'
                      f'保守口径升级 L2（l1_require_exact=True）')

        # --- L2：候选区域送模型 ---
        if static:
            context = self._region_context(static, hints)
            # 有 PIL 钩子时真正裁剪，否则整图 + 局部上下文
            img = image_path
            if self.crop_fn is not None:
                region = Rect.parse(static[0].get('bounds'))
                img = self.crop_fn(image_path, region)
            targets = [t for t in self._call_model(
                img, instruction, screen_w, screen_h, context, 'L2 区域')
                if t.confidence >= self.min_confidence]
            if targets:
                best = max(targets, key=lambda x: x.confidence)
                self._cache[key] = best
                return best

        # --- L3：全屏兜底（最贵，仅 L1/L2 都没结果时） ---
        targets = [t for t in self._call_model(
            image_path, instruction, screen_w, screen_h, hints, 'L3 全屏')
            if t.confidence >= self.min_confidence]
        best = max(targets, key=lambda x: x.confidence) if targets else None
        self._cache[key] = best
        return best

    # ------------------------------------------------------ 工具
    def _hints(self, root: LayoutNode) -> List[Dict[str, Any]]:
        return _collect_hints(root)

    def stats(self) -> Dict[str, Any]:
        return {'cache_hits': self.cache_hits, 'cache_misses': self.cache_misses,
                'cache_hit_rate': round(self.cache_hit_rate, 4),
                'model_calls': self.model_calls,
                'avg_model_ms': round(self.avg_model_ms, 1)}


# ---------------------------------------------------------------- 工厂

def build_provider(kind: str = 'mock', **kw) -> VisionProvider:
    """按配置创建 Provider。

    kind: 'mock' | 'openai'
    openai 模式下若未显式传 base_url/api_key/model，会从环境变量装配
    （见 OpenAICompatibleProvider.from_env），缺配置时抛 VisionConfigError
    而不是静默退回 Mock —— 假装接了真模型的测试比没有测试更危险。
    """
    if kind == 'mock':
        return MockProvider()
    if kind in ('openai', 'openai-compatible'):
        if kw.get('base_url') and kw.get('api_key') and kw.get('model'):
            return OpenAICompatibleProvider(
                base_url=kw['base_url'], api_key=kw['api_key'], model=kw['model'],
                timeout=int(kw.get('timeout', 60)),
                disable_thinking=bool(kw.get('disable_thinking', False)))
        env_kw: Dict[str, Any] = {'timeout': int(kw.get('timeout', 60))}
        if 'disable_thinking' in kw:                 # 显式传了才转发（同 from_env 口径）
            env_kw['disable_thinking'] = kw['disable_thinking']
        return OpenAICompatibleProvider.from_env(**env_kw)
    raise ValueError(f'未知的 Provider 类型: {kind}')
