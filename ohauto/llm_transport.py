"""LLM 传输与解析的共用底座 —— vision 与 generator 的 Provider 各自薄封装引用。

为什么单成一个模块
------------------
`vision.OpenAICompatibleProvider` 和 `generator.OpenAICompatibleProvider` 原本是
两个独立实现：HTTP POST、环境变量读取、JSON 围栏解析各写一遍，修一处坑得改两处
（结构债 S9）。本模块抽出**真正重复且语义一致**的部分：

* `post_chat`           —— urllib 的 chat/completions 传输（带可选重试 + transient 判定）
* `extract_json_array`  —— 从模型输出里抠 JSON 数组（容忍 markdown 围栏）
* `env_triple`          —— 逐变量多前缀回退读 (base_url, api_key, model)

**不抽的部分**（各自留在 Provider 里，因为口径有意的差异）：
* vision 的 `_parse` 还要把数组项翻译成 `VisualTarget`（带 Rect/confidence）；
* generator 的 `parse_json_payload` 要解析任意 JSON（对象/数组）+ 多级降级
  （单引号、ast.literal_eval），口径比「数组」宽，失败要抛 SchemaError。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ['post_chat', 'extract_json_array', 'env_triple']

#: 围栏正则。认 json 与 JSON 两种语言标记 —— 比原 vision 只认 json 更宽松，
#: 不会破坏既有测试（测试用的围栏都是 json 或无围栏）。
_FENCE_RE = re.compile(r'```(?:json|JSON)?\s*(.*?)```', re.S)

#: 重试时按 transient 判定的 HTTP 状态码（5xx 服务端错 + 429 限流）。
_TRANSIENT_CODES = frozenset({429, 500, 502, 503, 504})


def post_chat(base_url: str, api_key: str, payload: Dict[str, Any],
              *, timeout: float = 60.0, max_retries: int = 0) -> Dict[str, Any]:
    """POST `{base_url}/chat/completions`，返回解析后的 body dict。

    * `max_retries=0` —— 不重试，异常原样上抛（vision 口径：上层包成 VisionError）。
    * `max_retries>0` —— 对 transient 错误（5xx/429/超时/网络）退避重试，
      non-transient（如 4xx）直接 raise；用尽后抛最后一次的原始异常
      （generator 口径：上层 catch 后包成 ProviderError）。

    只用标准库 `urllib` —— 项目零第三方依赖的纪律不能破。
    """
    import urllib.error
    import urllib.request

    url = f'{base_url.rstrip("/")}/chat/completions'
    data = json.dumps(payload).encode('utf-8')
    headers = {'Content-Type': 'application/json',
               'Authorization': f'Bearer {api_key}'}

    last: Optional[BaseException] = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=data, method='POST', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8', 'replace'))
        except Exception as e:                        # 超时 / 网络 / 非 2xx
            last = e
            transient = True
            if isinstance(e, urllib.error.HTTPError):
                transient = e.code in _TRANSIENT_CODES
            if not transient or attempt >= max_retries:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise last  # 逻辑上到不了这里


def extract_json_array(text: str) -> List[Dict[str, Any]]:
    """从模型输出里抠 JSON 数组。容忍 markdown 围栏包裹。找不到返回 `[]`。

    与 generator 的 `parse_json_payload` 区分：那个要**任意** JSON（对象/数组）
    + 多级降级，失败抛 SchemaError；本函数只认数组、找不到就空 —— 这正是
    vision「图中无目标 → 返回 []」的语义。
    """
    if not text:
        return []
    s = text.strip()
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    m = re.search(r'\[.*\]', s, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return arr if isinstance(arr, list) else []


def env_triple(url_names: Sequence[str], key_names: Sequence[str],
               model_names: Sequence[str], *,
               overrides: Dict[str, Any] = None) -> Tuple[str, str, str, List[str]]:
    """逐变量多前缀回退读 (base_url, api_key, model)。

    每个 kind 各自从其候选环境变量里取**第一个非空值** —— 这是「DeepSeek 生成用例 +
    Qwen-VL 看图」能分开配的前提（vision 的混搭口径）。`overrides` 里的非空值优先于
    环境变量（支持 `from_env(base_url=...)` 显式传参）。

    返回 `(base_url, api_key, model, missing)` —— `missing` 是空值对应的 kind 名列表，
    供调用方按自己的口径决定怎么报错（vision 抛 VisionConfigError，
    generator 抛 ProviderError/ProviderNotConfigured）。
    """
    overrides = overrides or {}

    def pick(names: Sequence[str], override: str) -> str:
        if override:
            return override
        for n in names:
            v = os.environ.get(n, '')
            if v:
                return v
        return ''

    base_url = pick(url_names, overrides.get('base_url') or '')
    api_key = pick(key_names, overrides.get('api_key') or '')
    model = pick(model_names, overrides.get('model') or '')
    missing = [n for n, v in (('base_url', base_url), ('api_key', api_key),
                              ('model', model)) if not v]
    return base_url, api_key, model, missing
