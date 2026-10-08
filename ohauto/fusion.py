"""通用 N 源融合 —— **契约驱动**的交叉验证内核。

为什么要把「三源」写成「N 源」
------------------------------
早先的原型硬编码了「静态 + 运行时 + 视觉」三条路，于是：

* 换个没有源码的应用（设备上大多数应用都没源码）就没法用；
* 想加第四个源（OCR / 无障碍树 / 设计稿 / 接口契约）就得改融合器本身。

这不是通用做法。**通用性来自契约，而不是来自把三条路写得再细一点。**

核心抽象只有两个
----------------
1. **`Claim`** —— 一个源对「某处应该/确实存在什么」的一条主张：
   `(kind, value, scope)` 三元组 + 它自己的置信度与出处。
   任何源只要能产出 Claim，就能接进来。
2. **`role`** —— 源只分两类，融合规则**只认 role，不认源的名字**：

   | role | 含义 | 典型源 |
   |---|---|---|
   | `declaration`（声明） | 「**应该**有什么」 | 源码、用例、设计稿、接口契约 |
   | `observation`（观察） | 「**实际**有什么」 | 运行时控件树、视觉识别、OCR |

于是融合规则可以写成**与源无关**的三条：

| 声明 | 观察 | 状态 | 含义 |
|---|---|---|---|
| ✅ | ✅ | `confirmed` | 声明的确实在 |
| ✅ | ❌ | **`missing`** | 🔴 **声明了但没出现**（差异告警） |
| ❌ | ✅ | `undeclared` | 界面上有、但没有任何声明提过（信息级） |

**这个表就是通用性的全部秘密** —— 加源不用改它。

降级是一等公民
--------------
某个源不可用（没有源码 / 没有模型 key / 没有用例）时，**必须显式返回
`ok=False` + 原因**，而不是静默给空列表 —— 否则「这个源没查到」和
「这个源根本没运行」就分不清了，报告会骗人。

    python -c "from ohauto import fusion"   # 依赖极少，可直接当库用
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

#: 支持的标识维度 —— 新维度直接加这里，融合逻辑不用动
KINDS = ('id', 'text', 'type', 'descr', 'hint', 'page', 'route')

#: 源权重（按**源名**给，缺省 1.0）。role 决定规则，权重决定置信度。
DEFAULT_WEIGHTS: Dict[str, float] = {
    'runtime': 1.0,     # 观察：当下事实
    'vision': 0.8,      # 观察：存在误识别
    'ocr': 0.7,         # 观察：文本识别
    'static': 0.9,      # 声明：权威但可能过时
    'case': 0.6,        # 声明：可能已失效
    'design': 0.7,      # 声明：设计稿
}


@dataclass
class Claim:
    """一条主张：「在 `scope` 里，应该/确实存在 kind=value 这个东西」。"""
    kind: str
    value: str
    scope: str = ''
    confidence: float = 1.0
    evidence: str = ''
    ondemand: bool = False         # 按需渲染（弹窗/条件分支）：缺失不算缺陷

    @property
    def key(self) -> tuple:
        return (self.kind, self.value, self.scope)


@dataclass
class SourceResult:
    """一个源的产出 —— **含它到底有没有跑起来**。

    `ok=False` 时 `reason` 必须写清为什么没跑（没源码 / 没 key / 文件不存在），
    这是「降级」与「查不到」的分界线。
    """
    name: str
    role: str                      # 'declaration' | 'observation'
    ok: bool = True
    reason: str = ''
    claims: List[Claim] = field(default_factory=list)
    note: str = ''                 # 补充说明（如"只覆盖当前页面"）

    def to_dict(self) -> Dict[str, Any]:
        return {'name': self.name, 'role': self.role, 'ok': self.ok,
                'reason': self.reason, 'claims': len(self.claims), 'note': self.note}


@dataclass
class Target:
    """融合后的一个目标。"""
    kind: str
    value: str
    scope: str = ''
    declared_by: List[str] = field(default_factory=list)
    seen_by: List[str] = field(default_factory=list)
    confidence: float = 0.0
    evidence: List[str] = field(default_factory=list)
    ondemand: bool = False         # 任一声明源标记了按需渲染 → 缺失不算缺陷

    @property
    def status(self) -> str:
        if self.declared_by and self.seen_by:
            return 'confirmed'
        if self.declared_by and not self.seen_by:
            return 'missing'
        return 'undeclared'


@dataclass
class FusionReport:
    sources: List[SourceResult] = field(default_factory=list)
    targets: List[Target] = field(default_factory=list)
    scope: str = ''

    def by_status(self, status: str) -> List[Target]:
        return [t for t in self.targets if t.status == status]

    @property
    def summary(self) -> Dict[str, int]:
        d = {'confirmed': 0, 'missing': 0, 'undeclared': 0}
        for t in self.targets:
            d[t.status] += 1
        d['declared'] = sum(len(t.declared_by) > 0 for t in self.targets)
        return d

    def usable(self) -> List[SourceResult]:
        """真正跑起来的源（用于说明「本次结论建立在哪几个源上」）。"""
        return [s for s in self.sources if s.ok]


def fuse(sources: Sequence[SourceResult], *, scope: str = '',
         weights: Optional[Dict[str, float]] = None) -> FusionReport:
    """把任意多个源融合 —— **规则只看 role，不看源是谁**。

    置信度 = 命中的观察源权重 + 声明源权重的 0.5 倍（声明是"应该说"，
    观察是"确实在"，后者更硬）。
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    buckets: Dict[tuple, Target] = {}
    for s in sources:
        if not s.ok:
            continue
        for c in s.claims:
            key = (c.kind, c.value, (c.scope or scope))
            t = buckets.get(key)
            if t is None:
                t = Target(kind=c.kind, value=c.value, scope=c.scope or scope)
                buckets[key] = t
            # ★ 去重：同一个源可能对同一目标发多条相同主张（实测：正则级静态
            #   分析会把 `if` 分支里的 `.id()` 读两次），不去重会把「声明方」
            #   渲染成 `static、static`，还会把声明权重算两遍。
            if s.role == 'declaration':
                if s.name not in t.declared_by:
                    t.declared_by.append(s.name)
            else:
                if s.name not in t.seen_by:
                    t.seen_by.append(s.name)
            if getattr(c, 'ondemand', False):
                t.ondemand = True
            if c.evidence:
                ev = '%s: %s' % (s.name, c.evidence)
                if ev not in t.evidence:
                    t.evidence.append(ev)

    for t in buckets.values():
        obs = sum(w.get(n, 1.0) for n in t.seen_by)
        dec = sum(w.get(n, 1.0) for n in t.declared_by) * 0.5
        t.confidence = round(min(1.0, obs + dec), 3)

    order = {'missing': 0, 'confirmed': 1, 'undeclared': 2}
    return FusionReport(sources=list(sources),
                        targets=sorted(buckets.values(),
                                       key=lambda t: (order[t.status], t.kind, t.value)),
                        scope=scope)


# ================================================================ 适配器
# 每个适配器只做一件事：把某个来源翻译成 Claim 列表。
# 加新源 = 加一个函数，**融合器不用改**。


def source_static_project(root: str, *, page: str = '',
                          mapper: Optional[Any] = None) -> SourceResult:
    """声明源：ArkTS 工程源码。

    `mapper(file_path, page)` 决定「这个文件属于哪个页面」——
    **默认实现是"文件名匹配"的权宜之计**（早先版本就这么干的）。
    不同工程结构（多 module / 多 entry / 非标准目录）应该传自己的 mapper，
    而不是去改这个函数 —— 这正是把映射策略做成参数的原因。

    没有工程目录时返回 `ok=False`（不是空列表）—— 「没源码」和
    「源码里没这个控件」是两件完全不同的事。
    """
    if not root or not os.path.isdir(root):
        return SourceResult('static', 'declaration', ok=False,
                            reason='未提供 ArkTS 工程目录（该应用可能没有源码）')
    try:
        import sys
        _tools_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'tools', 'static_arkts')
        sys.path.insert(0, _tools_dir)
        try:
            from bridge import analyze_project, control_hints, last_error
        finally:
            # bridge 已进 sys.modules，路径使命完成就恢复 ——
            # 不恢复会把 tools/static_arkts 永久泄漏进调用方的 import 搜索路径（评审 P2）
            try:
                sys.path.remove(_tools_dir)
            except ValueError:
                pass
    except ImportError as e:
        return SourceResult('static', 'declaration', ok=False,
                            reason='静态分析模块不可用: %s' % e)

    info = analyze_project(root)
    if info is None:
        return SourceResult('static', 'declaration', ok=False,
                            reason=last_error() or '静态分析未执行')

    def default_mapper(f: str, pg: str) -> bool:
        if not pg:
            return True
        stem = pg.strip('/').split('/')[-1].lower()
        return stem in (f or '').lower()

    key = page or ''
    claims: List[Claim] = []
    for c in control_hints(info):
        f = str(c.get('file') or '')
        if not (mapper or default_mapper)(f, key):
            continue
        od = bool(c.get('dialog'))      # 弹窗内容：按需渲染，缺失不算缺陷
        if c.get('id'):
            claims.append(Claim('id', c['id'], scope=key, evidence=f, ondemand=od))
        if c.get('text'):
            claims.append(Claim('text', c['text'], scope=key, evidence=f, ondemand=od))
    pages = info.get('pages') or []
    for p in pages:
        claims.append(Claim('page', p, scope=p, evidence='main_pages.json'))
    # 桌面卡片**不算应用内页面**（09-24 踩过：会被误当页面去探索）
    note = '页面 %d 个；已排除桌面卡片 %d 个' % (
        len(pages), len(info.get('widget_pages') or []))
    return SourceResult('static', 'declaration', ok=True, claims=claims,
                        note=note)


def source_case(case_path: str) -> SourceResult:
    """声明源：已沉淀的用例 —— 「用例引用了哪些控件」。

    与源码源同样是 declaration：它说的是"按用例的设计，这些控件应该在"。
    用例失效正是靠它被发现的。
    """
    if not case_path or not os.path.isfile(case_path):
        return SourceResult('case', 'declaration', ok=False,
                            reason='未提供用例文件')
    with open(case_path, encoding='utf-8') as f:
        text = f.read()
    base = os.path.basename(case_path)
    claims: List[Claim] = []
    seen = set()

    def add(kind: str, value: str) -> None:
        if value and (kind, value) not in seen:
            seen.add((kind, value))
            claims.append(Claim(kind, value, confidence=0.6, evidence=base))

    try:
        import yaml
        data = yaml.safe_load(text) or {}

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ('id', 'text') and isinstance(v, str):
                        add(k, v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(data.get('steps') or [])
    except Exception:
        import re
        for m in re.finditer(r'\b(id|text)\s*:\s*["\']([^"\']+)["\']', text):
            add(m.group(1), m.group(2))
    return SourceResult('case', 'declaration', ok=True, claims=claims,
                        note='用例 %s' % base)


def source_runtime_tree(root: Any, *, name: str = 'runtime',
                        scope: str = '') -> SourceResult:
    """观察源：运行时控件树（`LayoutNode` 或可 dumpLayout 的 JSON 文本）。"""
    if root is None:
        return SourceResult(name, 'observation', ok=False,
                            reason='未提供运行时控件树')
    try:
        from .layout import parse_layout
        node = parse_layout(root) if isinstance(root, (str, bytes, dict)) else root
    except Exception as e:
        return SourceResult(name, 'observation', ok=False,
                            reason='控件树解析失败: %s' % e)

    claims: List[Claim] = []
    n_vis = 0
    paths = set()
    for n in node.walk():
        # 页面路由：应用自报的 `pagePath` 是最硬的页面标识。
        # ⚠️ 必须让**观察源也产出 page 主张** —— 否则「声明了 pages/Second」
        # 永远找不到对手源，会被一律判成「缺失」，那是假告警。
        p = (n.attributes or {}).get('pagePath')
        if p and str(p) not in paths:
            paths.add(str(p))
            # scope 留空：页面清单是**应用级**概念，不属于某一个页面，
            # 留空让它走统一的作用域兜底，两边才对得上。
            claims.append(Claim('page', str(p), evidence='pagePath'))
        if not n.visible:
            continue
        n_vis += 1
        if n.id:
            claims.append(Claim('id', n.id, scope=scope,
                                evidence='[%d,%d]' % (n.rect.left, n.rect.top)))
        if n.text:
            claims.append(Claim('text', n.text, scope=scope))
        elif n.text_deep:
            # 真机可点容器自身无文案，文案在子节点上（identity 兜底）
            claims.append(Claim('text', n.text_deep, scope=scope,
                                evidence='text_deep'))
    note = '可见节点 %d 个' % n_vis + ('；页面路由 %s' % sorted(paths) if paths else '')
    return SourceResult(name, 'observation', ok=True, claims=claims, note=note)


#: venv 解释器（本项目「包只装 venv」的隔离约定下的 OCR 后端宿主）
_VENV_PY = os.path.expandvars(
    r'%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe')


def _ocr_via_venv(image_path: str, name: str) -> Optional[SourceResult]:
    """回退：用 **venv 解释器**跑 `tools/ocr_worker.py`。

    为什么要有这条：本项目的 Python 包只装进 venv（不污染托管运行时），
    而脚本平时用托管解释器跑 —— 直接 `import winsdk` 必然失败。
    走子进程桥一下，两边约定都不用破。

    返回 None 表示「这条路也走不通」，交给上层继续降级。

    ⚠️ 子进程 stdout 的编码必须在**子进程这一侧**固定下来（下面 `env` 那几行）。
    Windows 上 venv 解释器默认按 locale（**cp936**）写 stdout，而父进程按 utf-8 解码
    → 中文识别结果直接 `UnicodeDecodeError`。

    更隐蔽的是失败形态 —— 该异常发生在 subprocess 的 `_readerthread` 里：
    `run()` **不抛异常**、`returncode` **仍是 0**、`proc.stdout` **变成 None**。
    看起来跟「worker 跑通了但没读到字」一模一样，比抛异常难查得多。
    所以三层都钉：子进程固定 UTF-8 + 父进程 `errors='replace'` + `stdout is None` 检测。
    """
    if not os.path.isfile(_VENV_PY):
        return None
    worker = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'tools', 'ocr_worker.py')
    if not os.path.isfile(worker):
        return None
    try:
        import subprocess

        _env = dict(os.environ)
        _env['PYTHONIOENCODING'] = 'utf-8'
        _env['PYTHONUTF8'] = '1'
        proc = subprocess.run([_VENV_PY, worker, os.path.abspath(image_path)],
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=90, env=_env)
    except Exception as e:                                    # noqa: BLE001
        _OCR_VENV_ERR.append('venv 子进程异常: %s: %s' % (type(e).__name__, e))
        return None
    if proc.stdout is None:
        # 「解码失败」的静默形态：上面根本不进 except，rc 还是 0
        _OCR_VENV_ERR.append('worker stdout 解码失败（子进程编码不受控）—— '
                             '这跟「没读到字」不是一回事，不许混为一谈')
        return None
    if proc.returncode != 0:
        err = (proc.stderr or '').strip().splitlines()
        _OCR_VENV_ERR.append(err[-1] if err else 'worker 返回 %d' % proc.returncode)
        return None
    lines = [t.strip() for t in (proc.stdout or '').splitlines() if t.strip()]
    if not lines:
        _OCR_VENV_ERR.append('worker 跑通但没读到文字')
        return None
    # 后端名由 worker 写在 stderr（stdout 只放识别结果）。
    # **必须如实标注是谁读的** —— 写死成 winsdk 而实际用 rapidocr，就是假信息。
    import re as _re
    m = _re.search(r'backend=(\w+)', proc.stderr or '')
    be = m.group(1) if m else 'unknown'
    claims = [Claim('text', t, confidence=0.7,
                    evidence='OCR(%s via venv) %s' % (be, os.path.basename(image_path)))
              for t in lines]
    return SourceResult(name, 'observation', ok=True, claims=claims,
                        note='%s（经 venv 解释器）读到 %d 行' % (be, len(lines)))


_OCR_VENV_ERR: List[str] = []


def source_ocr(image_path: str, *, name: str = 'ocr',
               backend: str = 'auto') -> SourceResult:
    """观察源：OCR —— 从**截图**里读文字。

    为什么它是独立的第四源，而不是重复控件树
    ------------------------------------------
    控件树里能读到的文字，是**系统帮我们读出来的、有语义绑定的**文字；
    OCR 读的是**画面上真实画出来的像素**。两者会分叉，而分叉有意义：

    | 场景 | 控件树 | OCR | 说明 |
    |---|---|---|---|
    | 常规页面 | ✅ 读得到 | ✅ 读得到 | 两个观察源交叉印证 |
    | **纯 Canvas 应用** | ❌ **一个字都没有** | ✅ 读得到 | **OCR 是唯一通道**（实测 `etsclock`：0 个可交互节点） |
    | 图片里的字 / 图标下的文字 | ❌ 看不到 | ✅ 读得到 | 同上 |
    | 控件树文字与画面不一致 | 说的是一套 | 显的是另一套 | **冲突信号**（渲染没更新/被遮挡） |

    后端可换（`backend`），自动探测顺序：`winsdk`（Windows 系统 OCR，无需模型）
    → `pytesseract`（需另装 tesseract）。**一个都没有就如实降级**，
    并给出安装提示，不假装做过 OCR。

    ⚠️ 注意它与其他观察源**同 role**：融合规则不需要为 OCR 改一行。
    """
    if not image_path or not os.path.isfile(image_path):
        return SourceResult(name, 'observation', ok=False,
                            reason='未提供截图文件')

    # 每次调用先清空：这是「**本次**调用」的回退轨迹容器。
    # 不清会把上一次调用的失败原因串到这一次的报告里 —— 假归因，比没归因更糟。
    _OCR_VENV_ERR.clear()

    if backend in ('auto', 'winsdk'):
        try:
            import asyncio

            import winsdk.windows.media.ocr as _ocr
            import winsdk.windows.globalization as _glob
            import winsdk.windows.graphics.imaging as _img
            import winsdk.windows.storage as _st

            async def _run() -> List[str]:
                f = await _st.StorageFile.get_file_from_path_async(
                    os.path.abspath(image_path))
                stream = await f.open_async(_st.FileAccessMode.READ)
                decoder = await _img.BitmapDecoder.create_async(stream)
                bitmap = await decoder.get_software_bitmap_async()
                lang = _glob.Language('zh-Hans-CN')
                eng = _ocr.OcrEngine.try_create_from_language(lang) or \
                    _ocr.OcrEngine.try_create_from_user_profile_languages()
                if eng is None:
                    raise RuntimeError('系统没有可用的 OCR 语言包')
                res = await eng.recognize_async(bitmap)
                return [line.text for line in res.lines]

            lines = asyncio.run(_run())
            claims = [Claim('text', t.strip(), confidence=0.7,
                            evidence='OCR(%s)' % os.path.basename(image_path))
                      for t in lines if t and t.strip()]
            return SourceResult(name, 'observation', ok=True, claims=claims,
                                note='winsdk 系统 OCR，读到 %d 行' % len(claims))
        except ImportError:
            if backend == 'winsdk':
                return SourceResult(name, 'observation', ok=False,
                                    reason='未安装 winsdk（pip install winsdk）')
            # auto：当前解释器没有后端，退回 venv 解释器（见 _ocr_via_venv）
            via_venv = _ocr_via_venv(image_path, name)
            if via_venv is not None:
                return via_venv
        except Exception as e:
            if backend == 'winsdk':
                return SourceResult(name, 'observation', ok=False,
                                    reason='winsdk OCR 失败: %s' % e)
            _OCR_VENV_ERR.append('本解释器 winsdk 失败: %s' % e)
            via_venv = _ocr_via_venv(image_path, name)
            if via_venv is not None:
                return via_venv

    if backend in ('auto', 'tesseract'):
        try:
            import pytesseract
            from PIL import Image
            txt = pytesseract.image_to_string(Image.open(image_path), lang='chi_sim+eng')
            claims = [Claim('text', t.strip(), confidence=0.6, evidence='OCR')
                      for t in txt.splitlines() if t and t.strip()]
            return SourceResult(name, 'observation', ok=True, claims=claims,
                                note='pytesseract，读到 %d 行' % len(claims))
        except ImportError:
            pass
        except Exception as e:
            return SourceResult(name, 'observation', ok=False,
                                reason='pytesseract 失败: %s' % e)

    # 降级要把**试过什么、卡在哪**说清楚 —— 只说"没装后端"没法排查
    detail = ('；venv 回退也失败：%s' % _OCR_VENV_ERR[-1]) if _OCR_VENV_ERR else ''
    return SourceResult(
        name, 'observation', ok=False,
        reason=('未装任何 OCR 后端（试过 winsdk / pytesseract / venv 回退）%s'
                ' → 本次融合不含 OCR 源。装一个即可启用：'
                'pip install winsdk（用 Windows 自带 OCR，无需模型）') % detail)


#: 视觉源的默认提问。**刻意只要文字**：这样它的产出能与控件树/OCR 对齐；
#: 想要语义理解（"这个图标是干什么的"）就换 instruction —— 这正是把它做成
#: 参数而不是写死的原因。
VISION_READ_TEXT = '列出画面上的所有文字，每行一条，不要解释，不要加编号。'


def _png_size(path: str) -> tuple:
    """读 PNG IHDR 拿宽高。截图即全屏 → 图尺寸=屏尺寸，不写死任何设备的分辨率。"""
    import struct
    with open(path, 'rb') as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        raise ValueError('不是合法 PNG（缺 IHDR 头）')
    w, h = struct.unpack('>II', head[16:24])
    return int(w), int(h)


def source_vision(image_path: str, *, instruction: str = VISION_READ_TEXT,
                  provider: Any = None, screen: Any = None,
                  name: str = 'vision') -> SourceResult:
    """观察源：**视觉理解** —— 复用 `ohauto.vision` 的 Provider，不重写。

    与 OCR 源的分工（这是刻意设计的，不是重复）
    --------------------------------------------
    | | OCR（本地 rapidocr） | 视觉（云 VLM） |
    |---|---|---|
    | 成本 | 0（本地算力） | 按 token（09-24 实测核对：每图上限 **1024** tokens） |
    | 离线 | ✅ | ❌ |
    | 能力 | **只认字** | **认字 + 理解**（图标含义、页面意图、图表） |

    所以**不建议用视觉模型去替代 OCR**（花着钱做本地免费能做的事），
    而是让它干 OCR 干不了的：回答「这个图标是什么」「这一页是干什么的」
    —— 那正是命题挑战 #1「理解页面意图」要的东西。

    换 `instruction` 就换用途；默认只读文字，是为了让产出能与其它源对齐。

    配置走 `OHAUTO_VISION_*` / `OHAUTO_LLM_*` / `OH_LLM_*` 三件套；
    **一个都没配就如实降级**，不假装看过图。
    """
    if not image_path or not os.path.isfile(image_path):
        return SourceResult(name, 'observation', ok=False, reason='未提供截图文件')

    try:
        from .vision import OpenAICompatibleProvider, VisionConfigError
    except ImportError as e:
        return SourceResult(name, 'observation', ok=False,
                            reason='vision 模块不可用: %s' % e)

    if provider is None:
        try:
            provider = OpenAICompatibleProvider.from_env()
        except VisionConfigError as e:
            return SourceResult(
                name, 'observation', ok=False,
                reason='未配置视觉模型（%s）—— 需要 OHAUTO_VISION_BASE_URL / '
                       '_API_KEY / _MODEL 三件套（或 OHAUTO_LLM_*）。'
                       'DeepSeek 侧填 `deepseek-flash`（= V4.1-Flash，原生支持图像；'
                       '旧的 `deepseek-v4-flash-vision-exp` 已于 2026-09-10 下线，'
                       '其名字会路由到 deepseek-flash；`deepseek-v4-pro` 不接受图片）' % e)
        except Exception as e:
            return SourceResult(name, 'observation', ok=False,
                                reason='装配视觉 Provider 失败: %s' % e)

    try:
        if screen is None:
            screen = _png_size(image_path)   # 截图即全屏：图尺寸=屏尺寸
        targets = provider.locate(image_path, instruction, screen[0], screen[1])
    except Exception as e:
        return SourceResult(name, 'observation', ok=False,
                            reason='视觉调用失败: %s: %s' % (type(e).__name__, e))

    claims: List[Claim] = []
    for t in targets or []:
        label = (getattr(t, 'label', '') or '').strip()
        if not label:
            continue
        conf = float(getattr(t, 'confidence', 0.0) or 0.0)
        if getattr(t, 'uncertain', False):
            conf = min(conf, 0.5)      # 未获交叉印证的降权
        # ⚠️ 必须 str() 包一层：`rect` 可能是 tuple，而 `'%s' % (1,2,3,4)`
        # 会被当成「4 个参数给一个占位符」直接抛 TypeError
        claims.append(Claim('text', label, confidence=conf or 0.7,
                            evidence='vision %s' % str(getattr(t, 'rect', '') or '')))
    return SourceResult(name, 'observation', ok=True, claims=claims,
                        note='视觉模型读到 %d 条' % len(claims))


#: 向后兼容旧名（早先只占位、没有真实现）
source_vision_stub = source_vision


# ================================================================ 渲染

STATUS_CN = {'confirmed': '确认存在', 'missing': '🔴 声明了但没出现',
             'undeclared': '未声明（仅观察）'}


def render_md(rep: FusionReport, *, title: str = '融合报告',
              limit_undeclared: int = 12) -> str:
    s = rep.summary
    _miss_all = rep.by_status('missing')
    page_missing = sum(1 for t in _miss_all if t.kind == 'page')
    ondemand_missing = sum(1 for t in _miss_all
                           if t.kind != 'page' and t.ondemand)
    real_missing = s['missing'] - page_missing - ondemand_missing
    L = ['# %s' % title, '']
    if rep.scope:
        L.append('- 作用域：`%s`' % rep.scope)
    L += ['', '## 本次用到了哪些源', '',
          '| 源 | 角色 | 状态 | 说明 |', '|---|---|---|---|']
    for src in rep.sources:
        L.append('| `%s` | %s | %s | %s |' % (
            src.name, '声明' if src.role == 'declaration' else '观察',
            '✅ 已运行（%d 条主张）' % len(src.claims) if src.ok else '⛔ 未运行',
            src.reason or src.note or '-'))
    skipped = [x for x in rep.sources if not x.ok]
    if skipped:
        L += ['', '> ⚠️ 有源未运行 —— 结论建立在**剩余 %d 个源**上，'
                  '不要把"缺源"读成"没问题"。' % len(rep.usable())]

    L += ['', '## 汇总', '', '| 指标 | 值 |', '|---|---|',
          '| 被声明的目标 | %d |' % s['declared'],
          '| 确认存在 | %d |' % s['confirmed'],
          '| **声明了但没出现（总）** | %d |' % s['missing'],
          '| 　↳ 其中 **🔴 真缺失**（id/text，非页面） | %d |' % real_missing,
          '| 　↳ 其中 按需渲染控件未出现（弹窗/条件渲染，**不是缺陷**） | %d |' % ondemand_missing,
          '| 　↳ 其中 未观察到的页面（**不是缺陷**） | %d |' % page_missing,
          '| 未声明（仅观察） | %d |' % s['undeclared'], '']

    if page_missing:
        L += ['> ⚠️ 「声明了但没出现（总）」里包含 %d 个**未观察到的页面** —— '
              '页面只是这次没访问到，**不是缺陷**；'
              '对外引用请用「🔴 真缺失」那一行。' % page_missing, '']

    miss = rep.by_status('missing')
    if miss:
        # ⚠️ 不同 kind 的「缺失」含义完全不同，**不能一句"缺陷"了事**：
        #    · page —— 只是**这次没访问到**（未观察到 ≠ 页面不存在）
        #    · id/text 且非按需 —— "声明了但运行时找不到"，可能真缺陷
        #    · id/text 且按需渲染（弹窗/条件分支）—— 没触发而已，不是缺陷
        pages = [t for t in miss if t.kind == 'page']
        ondemand = [t for t in miss if t.kind != 'page' and t.ondemand]
        others = [t for t in miss if t.kind != 'page' and not t.ondemand]
        if pages:
            L += ['## 未观察到的页面（**不是缺陷**）', '',
                  '> 这些页面在源码里声明了，但本次采集的运行时树里没有 —— '
                  '**通常只是没访问到**（或该页面需要特定入口）。', '']
            for t in pages:
                L.append('- `%s`（声明方：%s）'
                         % (t.value, '、'.join('`%s`' % x for x in t.declared_by)))
            L.append('')
        if ondemand:
            L += ['## 按需渲染的控件未出现（**不是缺陷**）', '',
                  '> 这些控件在弹窗/条件分支里声明（builder 引用或 @CustomDialog），'
                  '**只有触发时才进控件树** —— 本次没触发而已。', '']
            for t in ondemand:
                L.append('- `%s`（%s）' % (t.value, t.kind))
            L.append('')
        if others:
            L += ['## 🔴 声明了但没出现', '']
            for t in others:
                L += ['- **`%s`**（%s）' % (t.value, t.kind),
                      '  - 声明方：%s' % '、'.join('`%s`' % x for x in t.declared_by),
                      '  - 没有任何观察源看到它']
                for e in t.evidence[:2]:
                    L.append('  - 出处：%s' % e)
            L.append('')
    else:
        L += ['## 声明了但没出现', '', '无。', '']

    conf = rep.by_status('confirmed')
    if conf:
        L += ['## 确认存在（前 %d 条）' % min(len(conf), 20), '',
              '| 目标 | 类型 | 声明方 | 观察方 | 置信度 |',
              '|---|---|---|---|---|']
        for t in conf[:20]:
            L.append('| `%s` | %s | %s | %s | %.2f |' % (
                t.value, t.kind, '/'.join(t.declared_by),
                '/'.join(t.seen_by), t.confidence))
        L.append('')

    unde = rep.by_status('undeclared')
    if unde:
        # ★ 必须标注「谁看到的」：不同观察源看到的不是一回事 ——
        #   实测纯 Canvas 应用（etsclock）的 `14:54:11` 只有 OCR/视觉能看到，
        #   控件树看不到。不标来源，这条最硬的证据就淹没在合并列表里。
        obs_names = [x.name for x in rep.sources
                     if x.ok and x.role == 'observation']
        L += ['## 未声明（仅观察，前 %d 条 / 共 %d）' % (limit_undeclared, len(unde)),
              '', '> 这是**信息级**，不是问题：界面上本就有大量系统控件、'
                  '容器文案不在任何声明里。', '']
        if obs_names:
            L.append('观察源：%s' % '、'.join('`%s`' % x for x in obs_names))
            L.append('')
        only_one = [t for t in unde if len(t.seen_by) == 1 and len(obs_names) >= 2]
        if only_one:
            L.append('**只有一个观察源看到的目标**（其余观察源都看不到 —— '
                     '这正是「某条通道独有」的证据）：')
            L.append('')
            for t in only_one[:limit_undeclared]:
                L.append('- `%s`　只有 **`%s`** 看到'
                         % (t.value, '、'.join(t.seen_by)))
            L.append('')
            rest = [t for t in unde if t not in only_one]
            if rest:
                L.append('其余（多源都看到）：')
                L.append('　'.join('`%s`（%s）' % (t.value, '/'.join(t.seen_by))
                                   for t in rest[:limit_undeclared]))
                L.append('')
        else:
            L.append('　'.join('`%s`（%s）' % (t.value, '/'.join(t.seen_by))
                               for t in unde[:limit_undeclared]))
            L.append('')
    return '\n'.join(L)
