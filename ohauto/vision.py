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


# ---------------------------------------------------------------- 数据结构

@dataclass
class VisualTarget:
    """视觉通道给出的定位结果。"""
    label: str
    rect: Rect
    confidence: float = 0.0
    source: str = 'vision'
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
                 timeout: int = 60, max_hints: int = 40):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_hints = max_hints

    def _build_context(self, hints: Optional[List[Dict[str, Any]]]) -> str:
        if not hints:
            return '（无控件树信息）'
        lines = ['已从控件树解析出以下可交互元素，可作参考（坐标同样为截图像素）：']
        for h in hints[:self.max_hints]:
            lines.append('  - type=%s id=%s text=%r bounds=%s' % (
                h.get('type', ''), h.get('id', ''), h.get('text', ''), h.get('bounds', '')))
        return '\n'.join(lines)

    def locate(self, image_path, instruction, screen_w, screen_h,
               control_hints=None) -> List[VisualTarget]:
        import urllib.request

        with open(image_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')

        prompt = self.PROMPT.format(instruction=instruction, w=screen_w, h=screen_h,
                                    control_context=self._build_context(control_hints))
        payload = {
            'model': self.model,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': prompt},
                    {'type': 'image_url',
                     'image_url': {'url': f'data:image/png;base64,{b64}'}},
                ],
            }],
            'temperature': 0,
        }
        req = urllib.request.Request(
            f'{self.base_url}/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {self.api_key}'},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.loads(r.read().decode('utf-8'))

        text = body['choices'][0]['message']['content']
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> List[VisualTarget]:
        """从模型输出里抠出 JSON 数组。容忍 markdown 代码块包裹。"""
        m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
        if m:
            text = m.group(1)
        m = re.search(r'\[.*\]', text, re.S)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        out = []
        for item in arr if isinstance(arr, list) else []:
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

class HybridLocator:
    """控件树 + 视觉 双通道融合定位。"""

    def __init__(self, provider: Optional[VisionProvider] = None,
                 iou_threshold: float = 0.3,
                 min_confidence: float = 0.4,
                 verbose: bool = True):
        self.provider = provider or MockProvider()
        self.iou_threshold = iou_threshold
        self.min_confidence = min_confidence
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f'[vision] {msg}')

    def _hints(self, root: LayoutNode) -> List[Dict[str, Any]]:
        out = []
        for n in flatten(root, only_visible=True, only_interactive=True):
            out.append({'type': n.type, 'id': n.id, 'text': n.text,
                        'descr': n.descr, 'hint': n.hint,
                        'bounds': n.rect.to_dict()})
        return out

    def locate(self, root: LayoutNode, image_path: str, instruction: str,
               screen_w: int, screen_h: int) -> Optional[VisualTarget]:
        """融合定位，返回最可信的目标；完全找不到返回 None。"""
        hints = self._hints(root)

        # 通道一：控件树按描述模糊匹配
        kw = instruction.strip().lower()
        tree_hits = []
        for n in flatten(root, only_visible=True):
            blob = ' '.join([n.type, n.id, n.text, n.descr, n.hint] +
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
            for n in tree_hits:
                iou = v.rect.overlap_ratio(n.rect)
                if iou >= self.iou_threshold:
                    self._log(f'交叉校验通过 IoU={iou:.2f} -> {n.label}')
                    cand = VisualTarget(label=n.label, rect=n.rect,
                                        confidence=min(1.0, v.confidence + 0.3),
                                        source='hybrid', raw=v.raw)
                    if best is None or cand.confidence > best.confidence:
                        best = cand
                    break

        if best is not None:
            return best

        # 无交叉验证：控件树优先（可解释性强），否则用视觉
        if tree_hits:
            n = max(tree_hits, key=lambda x: x.rect.area)
            self._log(f'仅控件树命中 -> {n.label}')
            return VisualTarget(label=n.label, rect=n.rect,
                                confidence=0.6, source='tree')

        if vis:
            v = max(vis, key=lambda x: x.confidence)
            if not v.raw:
                self._log(f'仅视觉命中 -> {v.label} (conf={v.confidence:.2f})')
                return v
            self._log(f'仅视觉命中 -> {v.label} (conf={v.confidence:.2f})')
            return v

        self._log('两通道均无命中')
        return None


# ---------------------------------------------------------------- 工厂

def build_provider(kind: str = 'mock', **kw) -> VisionProvider:
    """按配置创建 Provider。

    kind: 'mock' | 'openai'
    """
    if kind == 'mock':
        return MockProvider()
    if kind in ('openai', 'openai-compatible'):
        return OpenAICompatibleProvider(
            base_url=kw.get('base_url') or os.environ.get('OH_LLM_BASE_URL', ''),
            api_key=kw.get('api_key') or os.environ.get('OH_LLM_API_KEY', ''),
            model=kw.get('model') or os.environ.get('OH_LLM_MODEL', ''),
            timeout=int(kw.get('timeout', 60)),
        )
    raise ValueError(f'未知的 Provider 类型: {kind}')
