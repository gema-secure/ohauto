"""
L4 应用层 —— 结果信号采集（信号采集）
================================

把「设备上发生了什么」**客观**地采集下来，交给下游归因引擎判断
「为什么失败」。本模块只输出：

    客观证据  +  客观异常识别  +  置信度

它**不下结论**。不会说「这是应用缺陷」——那是归因的事。一旦 信号采集 自己下结论，
归因的规则就没有输入可用了。

对外契约（签名冻结）::

    collect_signals(hdc, bundle) -> Signals

三条硬约束
----------
1. **降级优先**：采集是旁路行为，它自己失败绝不能把正在跑的测试搞崩。
   `collect_signals()` 除参数错误外**不抛异常**，一律返回空值 + `warnings`。
2. **采集窗口**：`faultlogger/` 是累积目录，里面有历史崩溃，甚至别的应用的。
   只采纳采集窗口内新增的文件，否则会把几小时前的崩溃当成「刚才发生的」。
3. **按 bundle 过滤**：同一个目录里所有应用的崩溃都在一起，必须用
   `Module name` 匹配被测应用，否则会把系统里任何一个应用的崩溃算到我们头上。

采集窗口为什么用「设备时钟域」而不是直接比本机时间
--------------------------------------------------
设备 RTC 实测不准（曾停在 2017-08-05），所以设备上的 mtime / 文件名时间戳
与本机时间**不能直接比较**。但设备时钟相对本机时钟通常只是一个常量偏移，
因此本模块的做法是：

  * 窗口的**长度**用本机时钟度量（调用方给的 `since` / `lookback_s`）；
  * 窗口的**锚点**落在设备时钟域（采集时刻读一次设备 `date +%s`，算出偏移量）；
  * 两边都换算到设备时钟域后再比较。

这样即使 RTC 整体偏了若干年，只要偏移是常量，窗口判断依然正确。
"""

from __future__ import annotations

import json
import os
import re
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .layout import LayoutNode, parse_layout
from .perf import analyze_samples, take_sample


# ================================================================ 设备侧路径
# 全部在润和 DAYU200 / OpenHarmony 5.0.3.135 上实测确认（见 信号采集的设计说明）。

FAULTLOG_ROOT = '/data/log/faultlog'
FAULTLOGGER_DIR = '/data/log/faultlog/faultlogger'   # ← 正式归档，只读这个
FREEZE_DIR = '/data/log/faultlog/freeze'             # ← ANR（格式未验证）
TEMP_DIR = '/data/log/faultlog/temp'                 # ← 写入中，不读

# 崩溃日志里的 11 个 key:value 字段都是独立成行的，可直接正则提取。
# 这里列出的是本模块**显式认识**的键；其余键仍会被收进 CrashRecord.extra
# （宽松模式 —— jscrash / 未来版本的字段未验证，不能硬编码字段表）。
FIELD_PID = 'Pid'
FIELD_UID = 'Uid'
FIELD_MODULE = 'Module name'
FIELD_TIMESTAMP = 'Timestamp'
FIELD_REASON = 'Reason'
FIELD_FOREGROUND = 'Foreground'
FIELD_LIFETIME = 'Process life time'
FIELD_PROCESS_NAME = 'Process name'

_STACK_MARKER = 'Fault thread info:'

# 栈顶留几帧：`#00`–`#05` 够归因判断「崩在系统库还是应用自己的代码」。
CRASH_STACK_FRAMES = 6

# 日志里的 key:value 行。键允许出现空格（`Module name` / `Process life time`）。
_KV_RE = re.compile(r'^([A-Za-z][A-Za-z0-9 _\-/]*):(.*)$')

# `Reason:Signal:SIGSEGV(SI_USER)@0x000025d9 from:9689:0`
_SIGNAL_RE = re.compile(r'Signal:\s*([A-Za-z0-9_]+)')

# 崩溃日志文件名：
#   faultlogger/  cppcrash-<bundle>-<uid>-<YYYYMMDDHHMMSS>     ← 实测
#   temp/         cppcrash-<pid>-<毫秒时间戳>                   ← 实测
#   freeze/       appfreeze-<bundle>-<uid>-<时间戳>             ← **未验证**，按推测
# bundle 段用贪婪匹配（包名本身可能含 '-'），所以 uid / 时间戳必须是行尾锚定的。
_FAULT_NAME_RE = re.compile(
    r'^(?P<kind>[a-z]+crash|appfreeze|freeze)'
    r'-(?P<bundle>.+?)'
    r'-(?P<uid>\d+)'
    r'-(?P<ts>\d{14})$')
_TEMP_NAME_RE = re.compile(
    r'^(?P<kind>[a-z]+crash)-(?P<pid>\d+)-(?P<ts>\d{10,13})$')

# hilog 里与「崩溃/冻结」相关的关键字。**这是启发式，不是实测确认的清单**
# （freeze 的关键字尚未实测验证），所以它只作为
# 「中等置信度」证据参与叠加，绝不单信号硬判。
HILOG_FILTER = (
    r'crash|SIGSEGV|SIGABRT|SIGBUS|SIGILL|SIGFPE|faultlog|faultlogger|'
    r'freeze|Freeze|FREEZE|ANR|NotResponding|not responding|THREAD_BLOCK|'
    r'ThreadBlock|Watchdog|ArkCompiler|DfxSignalHandler|died|'
    r'[Ee]xception|FATAL'
)

# 白屏判据：
#   真机实测 720×1280 正常页面截图 70 KB – 931 KB，纯色页约 2–5 KB。
#   换成「每像素字节数」以便适配其它分辨率：正常 0.076 – 1.01，纯色 0.0022 – 0.0054。
_PNG_BPP_SOLID = 0.006        # ≤ 这个值：强烈怀疑纯色页
_PNG_BPP_NORMAL = 0.06        # ≥ 这个值：体积完全正常，不报白屏
_DOMINANT_COLOR_MIN = 0.95    # 单一颜色占比 ≥ 此值：判定为空白页
# 交叉验证：控件树节点数超过此值 → 与「空白页」矛盾，判据降级为疑似。
# 为什么是 5：真机的真空白页控件树是空的（实测 377 字节 / 0 节点），
# 而「内容少但有结构」的页面（如纯文本页）通常在 10 个节点以上，5 是安全的分界。
WHITE_SCREEN_NODE_CONTRADICT = 5
#: 与控件树矛盾时给的置信度 —— 不判死，只提示「需人工确认」。
WHITE_SCREEN_CONTRADICTED_CONF = 0.5


# ================================================================ 结果模型

@dataclass
class CrashRecord:
    """一次崩溃的客观现场。

    字段全部来自崩溃日志原文，**不含任何推测**。取不到的字段留空 / None，
    不要用「合理默认值」填 —— 例如 `Foreground` 缺失时是 None 而不是 False，
    否则会把「没采到」误报成「后台崩溃」。
    """

    module_name: str = ''                 # 'Module name' —— 用来过滤是不是我们的应用
    pid: int = 0                          # 'Pid'
    uid: int = 0                          # 'Uid'
    timestamp: str = ''                   # 'Timestamp'，设备时钟，如 '2026-09-16 15:14:46.000'
    reason: str = ''                      # 'Reason'，如 'Signal:SIGSEGV(SI_USER)@0x...'
    foreground: Optional[bool] = None     # 'Foreground'；None = 日志里没有这个字段
    process_life_time: str = ''           # 'Process life time'，如 '6s'
    stack_head: List[str] = field(default_factory=list)   # 'Fault thread info:' 之后的前几帧
    raw_path: str = ''                    # 崩溃日志原文落盘后的**本地**路径
    source_file: str = ''                 # 设备侧原文件名（含 bundle/uid/时间戳）
    kind: str = 'cppcrash'                # cppcrash / jscrash / appfreeze ...
    signal: str = ''                      # 从 reason 抽出的信号名，如 'SIGSEGV'
    timestamp_epoch: Optional[float] = None   # timestamp 换算成 epoch（**设备时钟域**）
    extra: Dict[str, str] = field(default_factory=dict)   # 未显式识别的 key:value

    @property
    def in_foreground(self) -> bool:
        """UI 测试只关心前台崩溃；取不到该字段时按「前台」处理并保留 None 语义。"""
        return self.foreground is not False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'module_name': self.module_name,
            'pid': self.pid,
            'uid': self.uid,
            'timestamp': self.timestamp,
            'timestamp_epoch': self.timestamp_epoch,
            'reason': self.reason,
            'signal': self.signal,
            'foreground': self.foreground,
            'process_life_time': self.process_life_time,
            'stack_head': list(self.stack_head),
            'raw_path': self.raw_path,
            'source_file': self.source_file,
            'kind': self.kind,
            'extra': dict(self.extra),
        }


@dataclass
class Anomaly:
    """一条**客观**异常信号。

    一个异常现象可以由多条独立证据支撑，因此这里一条 Anomaly = 一条证据，
    每条都自带 `source` 与 `confidence`。想要「合并成一个分数」的消费方
    可以调 `Signals.anomaly_score(kind)`。
    """

    kind: str            # 'CRASH' / 'WHITE_SCREEN' / 'NO_RESPONSE' / 'NO_WINDOW'
                         # / 'LAYOUT_ANOMALY'（新增，见 _judge_layout_anomaly）
    evidence: str        # 客观描述，如 '进程 com.ohos.note 已不在 pidof 输出中'
    source: str          # 'faultlog' / 'hilog' / 'screenshot' / 'layout' / 'hdc'
    confidence: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {'kind': self.kind, 'evidence': self.evidence,
                'source': self.source, 'confidence': round(self.confidence, 4)}


@dataclass
class Signals:
    """一次采集的全部结果 —— 喂给归因引擎的输入。"""

    bundle: str = ''
    captured_at: str = ''                 # 本机时间（ISO 8601）
    device_time: str = ''                 # 设备时间（设备 RTC 可能不准，两个都记）
    hilog_tail: List[str] = field(default_factory=list)   # 按关键词过滤后的日志行
    crashes: List[CrashRecord] = field(default_factory=list)
    anomalies: List[Anomaly] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)  # 本地文件路径
    layout_path: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    # ---- 额外客观证据（字段清单是「建议」，这几条是补充）
    window_start: str = ''                # 采集窗口起点（本机时间 ISO 8601）
    process_alive: Optional[bool] = None  # pidof 是否还有该进程；None = 没测到
    layout_nodes: Optional[int] = None    # 采集到的控件树节点数
    faults_in_window: List[str] = field(default_factory=list)   # 窗口内该目录的全部文件名
    # 性能采样（collect_perf=True 或 PerfChannel.attach 时才有）。
    # None = **没开启采集** —— 与「采到了但缺样本」（perf['pss_kb'] is None）
    # 必须能区分，见 ohauto/perf.py 的「缺样本 ≠ 正常」约定。
    perf: Optional[Dict[str, Any]] = None

    # ---------------------------------------------------------- 便捷查询

    def of_kind(self, kind: str) -> List[Anomaly]:
        return [a for a in self.anomalies if a.kind == kind]

    def has(self, kind: str) -> bool:
        return any(a.kind == kind for a in self.anomalies)

    def anomaly_score(self, kind: str) -> float:
        """把同一类异常的多条独立证据合成为一个置信度。

        用 `1 - Π(1 - cᵢ)`（独立证据的概率合成），而不是取最大值 ——
        三条各自 0.5 的中等证据叠起来应当比单条 0.5 更可信。
        上限 0.99：采集永远给不出「绝对确定」。
        """
        keep = [max(0.0, min(1.0, a.confidence)) for a in self.of_kind(kind)]
        if not keep:
            return 0.0
        rest = 1.0
        for c in keep:
            rest *= (1.0 - c)
        return round(min(0.99, 1.0 - rest), 4)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            'bundle': self.bundle,
            'captured_at': self.captured_at,
            'device_time': self.device_time,
            'window_start': self.window_start,
            'hilog_tail': list(self.hilog_tail),
            'crashes': [c.to_dict() for c in self.crashes],
            'anomalies': [a.to_dict() for a in self.anomalies],
            'screenshots': list(self.screenshots),
            'layout_path': self.layout_path,
            'warnings': list(self.warnings),
            'process_alive': self.process_alive,
            'layout_nodes': self.layout_nodes,
            'faults_in_window': list(self.faults_in_window),
        }
        # 没开性能采集就不出这个键 —— 消费方可以靠键的存在与否区分「没采」
        # 与「采了但缺样本」，序列化口径与字段语义一致。
        if self.perf is not None:
            d['perf'] = dict(self.perf)
        return d

    def to_json(self, path: Optional[str] = None, indent: int = 2) -> str:
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(text)
        return text


# ================================================================ 性能 / 资源通道

class PerfChannel:
    """性能 / 资源采样通道 —— hidumper PSS + loadavg 负载 + 进程存活的周期采样。

    采样与分析原语单源在 `ohauto/perf.py`（L1）；本类是**面向编排的通道**：
    持有一串采样点、随时能出内存趋势曲线（验收口径：note_stability 类
    用例产出 PSS 曲线并入报告）。

    与 faultlog 采集同一条降级纪律：采样是旁路，失败记 None + 警告，
    绝不把正在跑的测试搞崩 —— `sample()` 不抛异常。
    """

    def __init__(self, hdc: Any, bundle: str, host_pid: Optional[int] = None):
        self.hdc = hdc
        self.bundle = bundle
        self.host_pid = host_pid
        self.samples: List[Dict[str, Any]] = []

    def sample(self, rounds_done: Optional[int] = None) -> Dict[str, Any]:
        """采一轮并记入本通道。失败在返回值的 `warn` 里，不抛异常。"""
        s = take_sample(self.hdc, self.bundle, host_pid=self.host_pid,
                        rounds_done=rounds_done)
        self.samples.append(s)
        return s

    def curve(self) -> Dict[str, Any]:
        """内存趋势曲线：PSS / 负载序列 + `perf.analyze_samples` 斜率分析。"""
        return {
            'bundle': self.bundle,
            'series': [{'ts': s.get('ts'), 'pss_kb': s.get('pss_kb'),
                        'load1': s.get('load1')} for s in self.samples],
            'analysis': analyze_samples(self.samples),
        }

    def flush(self, path: str, clear: bool = False) -> int:
        """把已采样本追加写入 jsonl（每行一个采样点）。返回本次写出行数。

        默认**不清空**内存样本 —— 曲线要能随时重出；长跑边采边落盘防丢时
        传 clear=True（落盘节奏由调用方定，工具壳 tools/stability_telemetry.py
        是逐轮即时落盘的写法）。
        """
        if not self.samples:
            return 0
        with open(path, 'a', encoding='utf-8') as f:
            for s in self.samples:
                f.write(json.dumps(s, ensure_ascii=False) + '\n')
        n = len(self.samples)
        if clear:
            self.samples = []
        return n

    def attach(self, sig: 'Signals', rounds_done: Optional[int] = None) -> None:
        """采一轮并把结果挂到 `Signals.perf` —— 诊断时顺带看资源水位。"""
        sig.perf = self.sample(rounds_done)


# ================================================================ 崩溃日志解析

def parse_fault_filename(name: str) -> Dict[str, Any]:
    """解析崩溃日志文件名，返回可用的客观信息（解析不出来就返回 {}）。

    实测命名规律::

        faultlogger/  cppcrash-<bundleName>-<uid>-<YYYYMMDDHHMMSS>
        temp/         cppcrash-<pid>-<毫秒时间戳>

    `freeze/` 的命名**未验证**，这里按 `appfreeze-<bundle>-<uid>-<时间戳>` 推测，
    因此解析失败必须返回 {} 而不是抛异常。
    """
    base = os.path.basename(name or '').strip()
    if not base:
        return {}

    m = _FAULT_NAME_RE.match(base)
    if m:
        ts = m.group('ts')
        try:
            epoch: Optional[float] = datetime.strptime(ts, '%Y%m%d%H%M%S').timestamp()
        except ValueError:
            epoch = None
        return {'file': base, 'kind': m.group('kind'), 'bundle': m.group('bundle'),
                'uid': int(m.group('uid')), 'timestamp_epoch': epoch,
                'timestamp': ts, 'source': 'filename'}

    m = _TEMP_NAME_RE.match(base)
    if m:
        raw = int(m.group('ts'))
        epoch = raw / 1000.0 if raw > 1e11 else float(raw)
        return {'file': base, 'kind': m.group('kind'), 'pid': int(m.group('pid')),
                'timestamp_epoch': epoch, 'timestamp': m.group('ts'),
                'source': 'filename'}
    return {}


def parse_crash_log(text: str, raw_path: str = '',
                    source_file: str = '') -> CrashRecord:
    """从崩溃日志原文解析出 CrashRecord。

    **宽松模式**：只认识确定存在的字段，其余全部原样收进 `extra`；
    任何字段缺失都不报错（`temp/` 那半截日志就没有 `Module name`，
    jscrash 的字段也还没验证过）。
    """
    rec = CrashRecord(raw_path=raw_path, source_file=source_file)

    if source_file:
        rec.kind = parse_fault_filename(source_file).get('kind', 'cppcrash')

    fields: Dict[str, str] = {}
    in_stack = False
    for line in (text or '').splitlines():
        if not in_stack and line.startswith(_STACK_MARKER):
            in_stack = True
            continue
        if in_stack:
            # 栈区：收集 `#00 ...` 帧。中间可能夹着 `Tid:/Name:` 行，跳过即可；
            # 一旦出现非栈非 Tid 的行（`Registers:` 等）说明栈结束。
            if line.startswith('#'):
                if len(rec.stack_head) < CRASH_STACK_FRAMES:
                    rec.stack_head.append(line.strip())
            elif rec.stack_head and not line.startswith('Tid:'):
                break
            continue
        m = _KV_RE.match(line)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            # 同名键以**首次出现**为准（后面 HiLog 段里可能有同名文本）
            fields.setdefault(key, val)

    rec.module_name = fields.get(FIELD_MODULE, '')
    rec.reason = fields.get(FIELD_REASON, '')
    rec.timestamp = fields.get(FIELD_TIMESTAMP, '')
    rec.process_life_time = fields.get(FIELD_LIFETIME, '')
    rec.pid = _as_int(fields.get(FIELD_PID))
    rec.uid = _as_int(fields.get(FIELD_UID))

    fg = fields.get(FIELD_FOREGROUND)
    if fg is not None:
        rec.foreground = fg.strip().lower() in ('yes', 'true', '1')

    if not rec.module_name:
        # temp/ 那份没有 `Module name`，退回文件名里的 bundle 段（客观来源）
        rec.module_name = parse_fault_filename(source_file).get('bundle', '')

    m = _SIGNAL_RE.search(rec.reason)
    if m:
        rec.signal = m.group(1)

    if rec.timestamp:
        rec.timestamp_epoch = _parse_device_timestamp(rec.timestamp)
    if rec.timestamp_epoch is None:
        rec.timestamp_epoch = parse_fault_filename(source_file).get('timestamp_epoch')

    known = {FIELD_MODULE, FIELD_REASON, FIELD_TIMESTAMP, FIELD_LIFETIME,
             FIELD_PID, FIELD_UID, FIELD_FOREGROUND, FIELD_PROCESS_NAME}
    rec.extra = {k: v for k, v in fields.items() if k not in known}
    return rec


def _as_int(v: Any) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _parse_device_timestamp(text: str) -> Optional[float]:
    """把崩溃日志里的 `Timestamp:2026-09-16 15:14:46.000` 转成 epoch。

    结果是**设备时钟域**的 epoch（可能因 RTC 不准而整体偏移），
    只能和设备上的其它时间比，不能和本机时间直接比。
    """
    s = (text or '').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


# ================================================================ 截图分析

def read_png_meta(path: str) -> Optional[Tuple[int, int, int, int]]:
    """只读 IHDR，返回 (width, height, bit_depth, color_type)。读不了返回 None。

    体积启发式需要知道像素数 —— 720×1280 的绝对阈值换个分辨率就不成立了。
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(33)
    except OSError:
        return None
    if len(head) < 29 or head[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    try:
        w, h, depth, ctype = struct.unpack('>IIBB', head[16:26])
    except struct.error:
        return None
    return w, h, depth, ctype


def decode_png(path: str) -> Optional[Tuple[int, int, int, bytes]]:
    """零依赖 PNG 解码：返回 (width, height, channels, pixels)。

    是 `sim.py::_write_png()` 的逆运算 —— 项目里没有 PIL / numpy，
    PNG 读取只需要 `zlib` + `struct` 就能手写。

    支持灰度/RGB/调色板/RGBA、8 位深、非隔行；其余形态（16 位、隔行）
    返回 None 让调用方降级，**绝不抛异常**。
    """
    try:
        with open(path, 'rb') as f:
            blob = f.read()
    except OSError:
        return None
    if blob[:8] != b'\x89PNG\r\n\x1a\n':
        return None

    idat = bytearray()
    palette: Optional[bytes] = None
    width = height = depth = ctype = 0
    pos = 8
    try:
        while pos + 8 <= len(blob):
            ln = struct.unpack('>I', blob[pos:pos + 4])[0]
            tag = blob[pos + 4:pos + 8]
            data = blob[pos + 8:pos + 8 + ln]
            if tag == b'IHDR':
                width, height, depth, ctype, _comp, _filt, interlace = \
                    struct.unpack('>IIBBBBB', data[:13])
                if depth != 8 or interlace != 0:
                    return None           # 16 位 / 隔行：不支持，降级
            elif tag == b'PLTE':
                palette = bytes(data)
            elif tag == b'IDAT':
                idat += data
            elif tag == b'IEND':
                break
            pos += 12 + ln
    except (struct.error, IndexError):
        return None

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or width <= 0 or height <= 0:
        return None
    if ctype == 3 and not palette:
        return None

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None

    stride = width * channels
    if len(raw) < (stride + 1) * height:
        return None                       # 文件被截断（例如读到了写入中的文件）

    out = bytearray(stride * height)
    prev = bytes(stride)
    p = 0
    for y in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 1:                      # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:                    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:                    # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:                    # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                line[i] = (line[i] + pr) & 0xFF
        elif ftype != 0:
            return None                     # 未知 filter：降级
        out[y * stride:(y + 1) * stride] = line
        prev = bytes(line)

    if ctype == 3:
        # 调色板 → RGB
        idx = bytes(out)
        rgb = bytearray(width * height * 3)
        for i, v in enumerate(idx):
            o = v * 3
            if o + 2 >= len(palette):
                return None
            rgb[i * 3:i * 3 + 3] = palette[o:o + 3]
        return width, height, 3, bytes(rgb)
    return width, height, channels, bytes(out)


def analyze_screenshot(path: str, deep: bool = True,
                       max_pixels: int = 4_000_000) -> Dict[str, Any]:
    """对一张截图做客观统计，供白屏判据使用。

    返回的键：
        size_bytes / width / height / pixels / bytes_per_pixel
        dominant_ratio   单一颜色占比（`deep=False` 或解码失败时为 None）
        dominant_rgb     占比最高的颜色
        decoded          是否做过像素级分析

    两条思路都实现：A 是体积启发式（永远跑，零成本），
    B 是像素直方图（`deep=True` 时跑，实测 720×1280 约 0.4 s）。
    B 只在 A 已经可疑时才需要 —— 见 is_white_screen() 的调用约定。
    """
    info: Dict[str, Any] = {'path': path, 'size_bytes': 0, 'width': 0, 'height': 0,
                            'pixels': 0, 'bytes_per_pixel': None,
                            'dominant_ratio': None, 'dominant_rgb': None,
                            'decoded': False}
    try:
        info['size_bytes'] = os.path.getsize(path)
    except OSError:
        return info

    meta = read_png_meta(path)
    if meta:
        w, h, _depth, _ctype = meta
        info.update(width=w, height=h, pixels=w * h)
        if w * h > 0:
            info['bytes_per_pixel'] = info['size_bytes'] / float(w * h)

    if not deep or not info['pixels'] or info['pixels'] > max_pixels:
        return info

    decoded = decode_png(path)
    if not decoded:
        return info
    w, h, ch, px = decoded
    info['decoded'] = True
    info['width'], info['height'] = w, h

    counts: Dict[bytes, int] = {}
    for i in range(0, len(px), ch):
        key = px[i:i + ch]
        counts[key] = counts.get(key, 0) + 1
    if counts:
        top, n = max(counts.items(), key=lambda kv: kv[1])
        info['dominant_ratio'] = n / float(w * h)
        info['dominant_rgb'] = list(top)
    return info


def is_white_screen(info: Dict[str, Any]) -> Tuple[bool, float, str]:
    """根据 analyze_screenshot() 的统计给出「是不是白屏/纯色页」判据。

    返回 `(是否可疑, 置信度, 客观描述)`。**输出的是置信度而不是布尔结论** ——
    复杂但内容少的页面体积也可能偏小，这是启发式，不是定理。
    """
    bpp = info.get('bytes_per_pixel')
    ratio = info.get('dominant_ratio')
    size = info.get('size_bytes') or 0
    w, h = info.get('width') or 0, info.get('height') or 0

    # ---- 思路 B：像素直方图（更准，能给出「整个屏就是一个颜色」这种硬证据）
    if ratio is not None:
        if ratio >= _DOMINANT_COLOR_MIN:
            rgb = info.get('dominant_rgb') or []
            shape = '.'.join(str(v) for v in rgb[:3]) if rgb else '?'
            return True, min(0.99, float(ratio)), (
                f'截图像素 {ratio * 100:.1f}% 为同一颜色 ({shape})，'
                f'疑似纯色/空白页（{w}x{h}, {size} 字节）')
        # 颜色分散 → 明确**不是**白屏，直接用低置信度判据否掉体积启发式的误报
        return False, 0.0, (
            f'截图像素分散（最高占比仅 {ratio * 100:.1f}%），不是纯色页'
            f'（{w}x{h}, {size} 字节）')

    # ---- 思路 A：体积启发式（解码不可用时兜底）
    if bpp is None:
        return False, 0.0, f'截图 {path_name(info)} 无法解析 PNG 头，跳过白屏判据'
    if bpp >= _PNG_BPP_NORMAL:
        return False, 0.0, f'截图体积正常（{bpp:.3f} 字节/像素），无白屏信号'
    if bpp <= _PNG_BPP_SOLID:
        return True, 0.8, (f'截图体积异常小（{size} 字节, {bpp:.4f} 字节/像素，'
                           f'{w}x{h}），疑似纯色/空白页')
    span = max(1e-9, _PNG_BPP_NORMAL - _PNG_BPP_SOLID)
    conf = 0.8 * (_PNG_BPP_NORMAL - bpp) / span
    return True, round(conf, 4), (f'截图体积偏小（{size} 字节, {bpp:.4f} 字节/像素，'
                                  f'{w}x{h}），疑似内容稀少')


def path_name(info: Dict[str, Any]) -> str:
    return os.path.basename(info.get('path') or '?')


# ================================================================ 采集窗口

def _device_clock_offset(hdc) -> Tuple[float, str, Optional[str]]:
    """读设备时钟，返回 `(设备与本机的偏移秒数, 设备时间文本, warning)`。

    偏移量用于把采集窗口从本机时钟域换算到设备时钟域 —— `faultlogger/` 里
    的文件时间戳全是设备时钟域的，两者不换算就无法比较（RTC 实测曾停在
    2017-08-05，直接比会把所有崩溃都判成「窗口外」）。

    读不到就返回偏移 0 并记一条 warning：此时窗口判断退化为「按本机时间」，
    可能不准，但**不会把采集流程搞崩**。
    """
    text = ''
    epoch: Optional[float] = None
    try:
        res = hdc.shell('date +%s', timeout=15)
        out = (getattr(res, 'stdout', '') or '').strip()
        if re.fullmatch(r'\d{9,13}', out):
            epoch = float(out) / (1000.0 if len(out) > 10 else 1.0)
    except Exception as e:                                   # noqa: BLE001
        return 0.0, text, f'读取设备时钟失败（窗口判断退化为本机时间）: {e}'

    try:
        res = hdc.shell('date +%Y-%m-%dT%H:%M:%S', timeout=15)
        text = (getattr(res, 'stdout', '') or '').strip()
    except Exception:                                        # noqa: BLE001
        text = ''

    if epoch is None:
        return 0.0, text, ('设备时钟不是 epoch 格式（窗口判断退化为本机时间）：'
                           f'{text or "<空>"}')
    return epoch - time.time(), text, None


def _to_iso(epoch: float) -> str:
    """本机时间 → 带本地时区偏移的 ISO 8601。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat(
        timespec='seconds')


def _window_start_epoch(since: Any, lookback_s: float,
                        offset_s: float) -> float:
    """把调用方给的窗口起点换算到**设备时钟域**。

    窗口的**长度**用本机时钟度量（不要用设备时间做窗口判断），
    只把锚点平移到设备时钟域，以便和文件时间戳比较。
    """
    now = time.time()
    if since is None:
        start_local = now - max(0.0, float(lookback_s))
    elif isinstance(since, datetime):
        start_local = since.timestamp()
    elif isinstance(since, (int, float)):
        start_local = float(since)
    else:
        start_local = _parse_iso_like(str(since), default=now)
    return start_local + offset_s


def _parse_iso_like(text: str, default: float) -> float:
    s = (text or '').strip()
    if not s:
        return default
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return default


# ================================================================ ls -l 解析

_LS_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_LS_SHORT_DATE_RE = re.compile(r'^\d{2}-\d{2}$')   # toybox 近期文件的 MM-DD
_LS_TIME_RE = re.compile(r'^\d{2}:\d{2}(:\d{2})?$')


def parse_ls_line(line: str) -> Optional[Dict[str, Any]]:
    """解析 `ls -l` 的一行，返回 {'name', 'size', 'mtime'}；不是文件行返回 None。

    设备侧 mtime 是**设备时钟域**的。toybox 的 `ls -l` 对近期文件只给
    `MM-DD HH:MM`（无年份）：此时同样定位日期列、解析 size，年份按
    **本机当前年**补齐；若补出的时刻在未来（跨年读旧文件），回退一年。
    精度到分钟足够，而且文件名里通常有更精确的时间戳，会优先采用
    （见 _file_time_epoch）。
    """
    s = (line or '').rstrip()
    if not s or s.startswith('total') or s.startswith('ls:') :
        return None
    parts = s.split()
    if len(parts) < 4:
        return None
    name = parts[-1]

    # 先定位日期列，再从它**往前**找最后一个纯数字列 —— 那才是文件大小。
    # 不能从前往后找：`rw-r 1 root log 388096 ...` 里的硬链接数 `1` 也是数字，
    # 从前往后取会把它当成文件大小。
    date_at = None
    short_date = False
    for i, tok in enumerate(parts[:-1]):
        if _LS_DATE_RE.match(tok):
            date_at = i
            break
        if _LS_SHORT_DATE_RE.match(tok):
            date_at = i
            short_date = True
            break
    if date_at is None:
        return {'name': name, 'size': 0, 'mtime': None}

    size = 0
    for tok in reversed(parts[:date_at]):
        if tok.isdigit():
            size = int(tok)
            break

    stamp = None
    if date_at + 1 < len(parts) - 1 and _LS_TIME_RE.match(parts[date_at + 1]):
        stamp = f'{parts[date_at]} {parts[date_at + 1]}'
    elif date_at + 2 < len(parts) - 1 and _LS_TIME_RE.match(parts[date_at + 1]) \
            and parts[date_at + 2].isdigit():
        stamp = f'{parts[date_at]} {parts[date_at + 1]}'
    if stamp and short_date:
        # MM-DD 无年份：补本机当前年；跨年读旧文件时补出的时刻会落在未来，
        # 回退一年（slack 1 天，容忍设备时钟小偏差）。
        year = time.localtime().tm_year
        mtime = _parse_iso_like(f'{year}-{stamp}', default=-1.0)
        if mtime > time.time() + 86400:
            mtime = _parse_iso_like(f'{year - 1}-{stamp}', default=-1.0)
        return {'name': name, 'size': size,
                'mtime': mtime if mtime and mtime > 0 else None}
    mtime = _parse_iso_like(stamp, default=-1.0) if stamp else None
    if mtime is not None and mtime < 0:
        mtime = None
    return {'name': name, 'size': size, 'mtime': mtime}


def list_fault_dir(hdc, device_dir: str,
                   warnings: List[str]) -> List[Dict[str, Any]]:
    """列目录，返回 [{'name','size','mtime'}]。失败 → 空列表 + warning。

    降级是硬要求：`faultlogger/` 权限是 `drwxr-x--- hiview log`，非 root 设备
    读不到（该字段尚未实测验证），此时必须返回空列表而不是抛异常。
    """
    try:
        res = hdc.shell(f'ls -l {device_dir}', timeout=30)
    except Exception as e:                                   # noqa: BLE001
        warnings.append(f'无法列出 {device_dir}（已降级为空列表）: {e}')
        return []
    out = getattr(res, 'stdout', '') or ''
    err = (getattr(res, 'stderr', '') or '').strip()
    rc = getattr(res, 'returncode', 0)

    entries = []
    for line in out.splitlines():
        item = parse_ls_line(line)
        if item:
            entries.append(item)

    # 「目录是空的」和「读不了」必须分开：前者是最常见的情况（没崩溃就没人写），
    # 不该每次都报一条 warning；后者才是需要让人看见的降级。
    if not entries and (rc != 0 or err):
        warnings.append(f'无法列出 {device_dir}（已降级为空列表）: '
                        f'{(err or f"rc={rc}")[:200]}')
    return entries


def _file_time_epoch(entry: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """取一个崩溃日志文件的时间戳（**设备时钟域**），优先用文件名里的。

    文件名里的 `YYYYMMDDHHMMSS` 精度到秒且不受 `ls -l` 输出格式影响，
    比 mtime 可靠；mtime 是兜底。
    """
    meta = parse_fault_filename(entry.get('name', ''))
    if meta.get('timestamp_epoch'):
        return float(meta['timestamp_epoch']), 'filename'
    if entry.get('mtime'):
        return float(entry['mtime']), 'mtime'
    return None, ''


# 设备时钟「未来」容差：文件时间戳最多可以比当前设备时间晚这么多秒。
FUTURE_SLACK_S = 120.0

# 设备时钟与本机相差超过这个秒数就在 warnings 里点明（真机 RTC 卡在 2017，
# 偏差约 9 年 —— 不点明的话读者会以为产物里的时间戳写错了）。
CLOCK_SKEW_WARN_S = 86400.0


# ================================================================ 设备文件读取

def _read_device_file(hdc, device_path: str, local_path: str,
                      warnings: List[str],
                      binary: bool = False) -> Optional[str]:
    """把设备文件拉到本地，返回本地路径；失败返回 None 并记 warning。

    先走 `hdc.pull()`（内部是 `file recv`），失败再兜一层 `cat`
    （真机上 `file recv` 可能因权限/路径问题失败）。

    ⚠️ **二进制文件必须传 `binary=True`** —— 它做两件事：
    ① 透传给 `hdc.pull`，让 `file recv` 走字节保真通道；
    ② **禁用 cat 兜底** —— `hdc shell cat` 会对二进制做 CRLF 转换
    （PNG 头 `\\x89PNG\\r\\n` 被写成 `\\x89PNG\\r\\r\\n`），产出**损坏但不报错**
    的文件（`hdc.pull` docstring 里的实测约束，hdc.py「坑 3」）。

    全项目评审发现：截图拉取一直没传这个参数 —— PNG 一旦走了
    cat 兜底，白屏判据读到的是坏文件，**看起来像「应用白屏」，实为工具损坏**。
    """
    try:
        hdc.pull(device_path, local_path, binary=binary)
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            return local_path
    except Exception as e:                                   # noqa: BLE001
        warnings.append(f'拉取 {device_path} 失败: {e}')
    if binary:
        # 宁可如实报失败，也不留一份「看起来正常」的坏 PNG
        warnings.append(f'{device_path} 为二进制，file recv 失败后禁用 cat 兜底'
                        f'（cat 会 CRLF 损坏且不报错）')
        return None
    try:
        res = hdc.shell(f'cat {device_path}', timeout=60)
        text = getattr(res, 'stdout', '') or ''
        if text:
            os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
            with open(local_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(text)
            return local_path
    except Exception as e:                                   # noqa: BLE001
        warnings.append(f'读取 {device_path} 失败（已跳过）: {e}')
    return None


def _read_device_text(hdc, device_path: str) -> Optional[str]:
    """读设备上的文本文件内容；失败返回 None。"""
    try:
        res = hdc.shell(f'cat {device_path}', timeout=60)
        text = getattr(res, 'stdout', '') or ''
        if text.strip():
            return text
    except Exception:                                        # noqa: BLE001
        return None
    return None


# ================================================================ 主入口

def collect_signals(
    hdc,
    bundle: str,
    *,
    out_dir: Optional[str] = None,
    since: Any = None,
    lookback_s: float = 600.0,
    hilog_lines: int = 500,
    hilog_grep: Optional[str] = None,
    deep_screenshot: bool = True,
    max_pixels: int = 4_000_000,
    probe_rounds: int = 1,
    probe_gap_s: float = 0.0,
    collect_layout: bool = True,
    collect_screenshot: bool = True,
    collect_perf: bool = False,
) -> Signals:
    """采集一次「设备上发生了什么」。**除参数错误外不抛异常。**

    Parameters
    ----------
    hdc:
        `Hdc` 或 `FakeHdc`（接口一致）。
    bundle:
        被测应用包名 —— 用来过滤崩溃日志，不匹配的**绝不采纳**。
    out_dir:
        产物落盘目录。为 None 时用系统临时目录（**不写进程当前目录**）。
    since:
        采集窗口起点。接受 epoch(float) / `datetime` / ISO 字符串。
        典型用法：传「这一步开始执行的时刻」。
        None 时退化为 `now - lookback_s`。
    lookback_s:
        `since` 为 None 时的回看秒数。默认 600 s。
    hilog_lines:
        hilog 取缓冲区末尾多少行。
    hilog_grep:
        `hilog_tail` 的过滤正则。None 时用模块默认关键字表 `HILOG_FILTER`。
    probe_rounds / probe_gap_s:
        「无响应」的控件树稳定性探测。>=2 时多采几轮控件树并比对签名。
        默认 1 轮（不额外付出设备往返）。
    collect_layout / collect_screenshot:
        是否采集控件树 / 截图。
    collect_perf:
        是否顺带采一轮性能样本（写入 `Signals.perf`）。**默认 False** ——
        诊断链路不背这两次设备往返；长稳场景显式开启，或直接用
        `PerfChannel` 做周期采样（结果挂 `Signals.perf` 或走 `curve()`）。

    返回的 `Signals` 里：

    * `crashes`      —— 窗口内、且 `Module name` 匹配 bundle 的崩溃
    * `anomalies`    —— 每条独立证据一个 `Anomaly`（CRASH / WHITE_SCREEN /
                        NO_RESPONSE / NO_WINDOW / LAYOUT_ANOMALY），自带 `source`
                        与 `confidence`
    * `warnings`     —— 采集过程中的每一处降级说明

    本函数**不做归因**。它只回答「发生了什么」，不回答「为什么」。
    """
    if not isinstance(bundle, str) or not bundle.strip():
        raise ValueError('bundle 必须是非空字符串')

    started = time.time()
    sig = Signals(bundle=bundle, captured_at=_to_iso(started))

    if out_dir is None:
        # 绝不写进程当前目录 —— 模拟设备曾经就因为写 getcwd() 在项目根
        # 攒了一堆垃圾文件（见 sim.py 的注释）。
        out_dir = os.path.join(tempfile.gettempdir(), 'ohauto_signals',
                               f'{bundle}_{int(started)}')
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        sig.warnings.append(f'无法创建输出目录 {out_dir}（产物只留在内存）: {e}')

    # ---- 0. 采集窗口：先读设备时钟，把窗口锚到设备时钟域
    offset_s, device_time, warn = _device_clock_offset(hdc)
    sig.device_time = device_time
    if warn:
        sig.warnings.append(warn)
    win_start = _window_start_epoch(since, lookback_s, offset_s)
    sig.window_start = _to_iso(win_start - offset_s)

    # 设备时钟偏差本身要让人看见：窗口锚在设备域，但产物里的时间是本机域，
    # 两者对不上时读者得知道为什么（真机 RTC 卡在 2017 就属于这种情况）。
    if abs(offset_s) > CLOCK_SKEW_WARN_S:
        sig.warnings.append(
            f'设备时钟与本机相差约 {offset_s / 86400.0:+.1f} 天'
            f'（设备 {device_time}），采集窗口与文件时间戳一律锚在**设备时钟域**；'
            f'产物中的本机时间不可与设备时间直接比较')

    # ---- 1. 崩溃日志（主证据）
    _collect_crashes(hdc, bundle, sig, out_dir, win_start,
                     device_now=time.time() + offset_s)

    # ---- 2. 进程存活（辅证据）
    sig.process_alive = _probe_process_alive(hdc, bundle, sig)

    # ---- 2.5 性能采样（默认关 —— 见参数说明）
    if collect_perf:
        PerfChannel(hdc, bundle).attach(sig)

    # ---- 3. 截图 + 白屏判据（先出原始判据，**延迟定案**见步骤 6.5）
    if collect_screenshot:
        _collect_screenshot(hdc, sig, out_dir, deep_screenshot, max_pixels)

    # ---- 4. 控件树 + 无窗口 / 无响应判据
    if collect_layout:
        _collect_layout(hdc, sig, out_dir, probe_rounds, probe_gap_s)

    # ---- 5. hilog（缓冲区是环形的，必须在这里立刻取）
    _collect_hilog(hdc, sig, hilog_lines, hilog_grep)

    # ---- 6. 交叉判据：崩溃 / 无响应
    _judge_crash(sig)
    _judge_no_response(hdc, sig, win_start, device_now=time.time() + offset_s)

    # ---- 6.5 白屏判据**交叉验证**（必须在 3/4 都跑完之后）
    # 为什么不能放在步骤 3 就地定案：截图（3）跑在控件树（4）**之前**，
    # 那一刻 `sig.layout_nodes` 必然是 None，想交叉验证也拿不到数 ——
    # 照抄「判据里查 layout_nodes」在调用顺序上根本走不通。
    # 所以做成「3 出原始判据 → 6.5 用节点数复核并调整置信度」两段。
    _judge_white_screen(sig)

    return sig


# ---------------------------------------------------------------- 各步采集

def _collect_crashes(hdc, bundle: str, sig: Signals, out_dir: str,
                     win_start: float,
                     device_now: Optional[float] = None) -> None:
    """采集 `faultlogger/` 里**窗口内 + bundle 匹配**的崩溃。

    只读正式归档目录，不读 `temp/` —— 后者是写入中的文件，可能读到半截。

    `device_now` 是当前设备时间（设备时钟域）。窗口只有下界是不够的：
    设备 RTC 一旦向后跳（真机上就跳回了 2017 年），跳变**之前**写的旧日志
    会带着一个「未来」时间戳留在目录里，于是 `when >= win_start` 会把它们
    全部误采纳。而一个日志文件不可能在设备当前时间之后才被写出来，
    所以 `when > device_now + 容差` 是判定「该文件跨越了一次时钟回跳、
    与本次采集窗口不可比」的硬信号，必须剔除。
    """
    entries = list_fault_dir(hdc, FAULTLOGGER_DIR, sig.warnings)
    if not entries:
        return

    rejected: List[str] = []
    from_future: List[str] = []
    for entry in sorted(entries, key=lambda e: e.get('name', '')):
        name = entry.get('name', '')
        meta = parse_fault_filename(name)

        # ---- 窗口界定：只采纳采集窗口内新增的文件
        when, how = _file_time_epoch(entry)
        if when is None:
            sig.warnings.append(
                f'崩溃日志 {name} 无法获得时间戳（文件名与 ls 都解析失败），'
                f'按「窗口内」保守采纳')
        elif when < win_start:
            continue
        elif device_now is not None and when > device_now + FUTURE_SLACK_S:
            # 时钟回跳前的旧日志：时间戳落在设备当前时间之后，不可能属于本轮
            from_future.append(name)
            continue
        sig.faults_in_window.append(name)

        # ---- bundle 过滤：不匹配的绝不采纳
        if meta.get('bundle') and meta['bundle'] != bundle:
            rejected.append(meta['bundle'])
            continue

        device_path = f'{FAULTLOGGER_DIR}/{name}'
        text = _read_device_text(hdc, device_path)
        if text is None:
            # 读不到原文也不能丢记录 —— 文件名本身已经是客观证据
            sig.warnings.append(f'崩溃日志 {name} 读取失败（权限？），仅保留文件名')
            rec = CrashRecord(source_file=name, raw_path='',
                              module_name=meta.get('bundle', ''),
                              uid=meta.get('uid', 0),
                              timestamp_epoch=meta.get('timestamp_epoch'),
                              kind=meta.get('kind', 'cppcrash'))
            sig.crashes.append(rec)
            continue

        local = os.path.join(out_dir, f'{_safe(name)}.log')
        try:
            os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
            with open(local, 'w', encoding='utf-8', errors='replace') as f:
                f.write(text)
        except OSError as e:
            sig.warnings.append(f'崩溃日志 {name} 落盘失败（保留在内存中）: {e}')
            local = ''
        rec = parse_crash_log(text, raw_path=local, source_file=name)

        # 二次 bundle 校验：文件名段可能缺失（temp/ 形态），但正文里的
        # `Module name` 一定在。两者都没有才放行（宽松模式，不误丢证据）。
        if rec.module_name and rec.module_name != bundle:
            rejected.append(rec.module_name)
            continue
        if not rec.module_name:
            # 文件名和正文都没给出模块名 —— 无法确认是不是我们的应用。
            # 仍然采纳（丢证据比多一条可疑证据更糟），但把不确定**写在明面上**，
            # 让归因知道这条不能当铁证用。module_name 保持空串，不猜。
            sig.warnings.append(
                f'崩溃日志 {name} 的文件名与正文都未给出 Module name，'
                f'无法确认归属（module_name 留空，未做推测）')
        if when is not None:
            rec.timestamp_epoch = rec.timestamp_epoch or when
        sig.crashes.append(rec)

    if from_future:
        sig.warnings.append(
            f'{len(from_future)} 份崩溃日志的时间戳晚于当前设备时间，'
            f'判为设备时钟回跳前的旧日志、与本次采集窗口不可比，已剔除：'
            f'{", ".join(from_future[:3])}{" 等" if len(from_future) > 3 else ""}')

    if rejected:
        uniq = sorted(set(rejected))
        sig.warnings.append(
            f'窗口内有 {len(rejected)} 份其它应用的崩溃日志，已按 bundle 过滤：'
            f'{", ".join(uniq[:5])}{" 等" if len(uniq) > 5 else ""}')


def _probe_process_alive(hdc, bundle: str, sig: Signals) -> Optional[bool]:
    """`pidof <bundle>` 是否还有进程。取不到时返回 None（不猜）。"""
    try:
        res = hdc.shell(f'pidof {bundle}', timeout=15)
        out = (getattr(res, 'stdout', '') or '').strip()
        return bool(out)
    except Exception as e:                                   # noqa: BLE001
        sig.warnings.append(f'pidof {bundle} 探测失败: {e}')
        return None


def _collect_screenshot(hdc, sig: Signals, out_dir: str,
                        deep: bool, max_pixels: int) -> None:
    """截图并做白屏判据。失败只记 warning，不中断采集。

    ⚠️ **只出原始判据，不当场定案** —— 定案在 `_judge_white_screen()`（步骤 6.5），
    因为那一刻 `sig.layout_nodes` 还没采到（控件树在步骤 4），无法交叉验证。
    """
    local = os.path.join(out_dir, 'screen.png')
    try:
        device_path = hdc.screen_cap()
    except Exception as e:                                   # noqa: BLE001
        sig.warnings.append(f'截图失败（已跳过）: {e}')
        return
    if not _read_device_file(hdc, device_path, local, sig.warnings,
                             binary=True):     # PNG 是二进制：禁 cat 兜底
        sig.warnings.append(f'截图 {device_path} 无法取回本地，白屏判据跳过')
        return
    sig.screenshots.append(local)

    info = analyze_screenshot(local, deep=deep, max_pixels=max_pixels)
    suspicious, conf, evidence = is_white_screen(info)
    # 存原始判据供 6.5 交叉验证（不进 anomalies，避免未经复核就一票否决）
    setattr(sig, '_white_screen_raw', (bool(suspicious), float(conf), str(evidence)))
    if not suspicious and not info.get('decoded'):
        sig.warnings.append(f'白屏判据未使用像素级分析：{evidence}')


def _judge_white_screen(sig: Signals) -> None:
    """用控件树节点数**交叉验证**白屏判据，节点多就降级为「疑似」。

    ★ 新增（来源：外部评审 + 复核建议）。

    **缺陷原来的样子**：白屏判据是「单一颜色占比 ≥ 阈值」单一证据，
    触发后以 ≥0.95 的置信度一票否决。但同一次采集里 `layout_nodes` 就在手边 ——
    一个「控件树有 20 个节点」的页面，一眼就能否掉「空白页」。

    **为什么是降级而不是推翻**：像素分析本身没错（整屏同色确实是硬证据），
    错的是**无视矛盾证据**。所以：
      - 节点数 > `WHITE_SCREEN_NODE_CONTRADICT`(5) → 判据降级为「疑似」，
        杠杆从 0.95 压到 0.5，并**把节点数写进 evidence**（读报告的人自己判断）；
      - 节点数 ≤ 5（或没采到） → 维持原判据与置信度。

    真机上的意义：锁屏页、崩溃后白屏这类**真**空白页，控件树本来就是空的
    （实测 377 字节空树），判据不受影响；而被误判的是「内容少但有结构」的页面。
    """
    raw = getattr(sig, '_white_screen_raw', None)
    if not raw:
        return
    suspicious, conf, evidence = raw
    if not suspicious:
        return

    nodes = sig.layout_nodes
    if nodes is not None and nodes > WHITE_SCREEN_NODE_CONTRADICT:
        sig.anomalies.append(Anomaly(
            kind='WHITE_SCREEN',
            evidence=(f'{evidence}；但同一次采集的控件树有 {nodes} 个节点'
                      f'（> {WHITE_SCREEN_NODE_CONTRADICT}），与「空白页」矛盾 —— '
                      f'降级为疑似，需人工确认是不是内容稀少的正常页面'),
            source='screenshot+layout', confidence=WHITE_SCREEN_CONTRADICTED_CONF))
        return

    node_txt = (f'控件树 {nodes} 个节点' if nodes is not None
                else '未能取到控件树（无法交叉验证）')
    sig.anomalies.append(Anomaly(
        kind='WHITE_SCREEN',
        evidence=f'{evidence}；{node_txt}，与空白页一致',
        source='screenshot+layout', confidence=conf))


def _layout_signature(tree_text: Optional[str]) -> Optional[str]:
    """控件树的结构签名（层级 + 类型 + bounds），用于判断界面是否长时间不变。"""
    if not tree_text:
        return None
    try:
        root = parse_layout(tree_text)
    except Exception:                                        # noqa: BLE001
        return None
    parts = [f'{n.type}|{n.rect.to_dict()}|{len(n.children)}' for n in root.walk()]
    return ','.join(parts)


def _collect_layout(hdc, sig: Signals, out_dir: str,
                    probe_rounds: int, probe_gap_s: float) -> None:
    """采控件树，判定「无窗口」；可选多轮探测「界面卡住不动」。

    无窗口的判据**直接复用** `runner.DeviceGuard.has_window()`，
    明确要求不要重写；本模块只在结果之上补一条节点数证据并让两者交叉印证。
    """
    local = os.path.join(out_dir, 'layout.json')
    device_path = None
    try:
        device_path = hdc.dump_layout()
    except Exception as e:                                   # noqa: BLE001
        sig.warnings.append(f'控件树导出失败: {e}')

    text = None
    if device_path:
        if _read_device_file(hdc, device_path, local, sig.warnings):
            sig.layout_path = local
            # 已经从本地读了就别再 `cat` 一遍设备 —— 真机上一次控件树可以到几十 KB
            try:
                with open(local, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                text = _read_device_text(hdc, device_path)

    root = None
    if text is not None:
        try:
            root = parse_layout(text)
            sig.layout_nodes = sum(1 for _ in root.walk())
        except Exception as e:                               # noqa: BLE001
            sig.warnings.append(f'控件树解析失败，无窗口判据降级: {e}')

    # ---- 无窗口判据：复用现成实现 + 自己的节点数，两者一致才给高置信度
    has_window: Optional[bool] = None
    try:
        from .runner import DeviceGuard
        has_window = DeviceGuard(hdc, verbose=False).has_window()
    except Exception as e:                                   # noqa: BLE001
        sig.warnings.append(f'无窗口探测失败: {e}')

    nodes = sig.layout_nodes
    if has_window is False and nodes is not None and nodes <= 1:
        sig.anomalies.append(Anomaly(
            kind='NO_WINDOW',
            evidence=(f'控件树仅 {nodes} 个节点（且 bounds 全为 [0,0][0,0]），'
                      f'DeviceGuard.has_window() 同样判定无窗口 —— '
                      f'锁屏或无前台窗口'),
            source='layout', confidence=0.95))
    elif has_window is False:
        sig.anomalies.append(Anomaly(
            kind='NO_WINDOW',
            evidence='DeviceGuard.has_window() 判定无窗口（未能取到控件树交叉印证）',
            source='layout', confidence=0.4))
    elif has_window is None and nodes is not None and nodes <= 1:
        sig.anomalies.append(Anomaly(
            kind='NO_WINDOW',
            evidence=f'控件树仅 {nodes} 个零尺寸节点',
            source='layout', confidence=0.6))

    # ---- 布局异常：可见控件越出父容器 bounds（布局越界这一类）
    if root is not None:
        _judge_layout_anomaly(sig, root)

    # ---- 多轮稳定性探测（「无响应」的一条客观证据）
    if probe_rounds and probe_rounds > 1 and device_path:
        sigs = [_layout_signature(text)]
        for _ in range(int(probe_rounds) - 1):
            if probe_gap_s:
                time.sleep(max(0.0, float(probe_gap_s)))
            try:
                hdc.dump_layout()
                sigs.append(_layout_signature(_read_device_text(hdc, device_path)))
            except Exception:                                # noqa: BLE001
                sigs.append(None)
        seen = [s for s in sigs if s]
        if len(seen) >= 2 and len(set(seen)) == 1:
            # ⚠️ 这是本模块最弱的一条判据：两次探测之间若**没有注入过操作**，
            # 控件树本来就该是一样的（页面没理由自己变）。所以置信度压到 0.3，
            # 并且只在调用方显式要求多轮探测时才会产生。
            sig.anomalies.append(Anomaly(
                kind='NO_RESPONSE',
                evidence=(f'连续 {len(seen)} 次 dumpLayout 控件树签名完全相同。'
                          f'注意：若两次探测之间没有注入过操作，签名相同属正常现象，'
                          f'该信号仅在「刚发生过应当改变界面的操作」时才有效'),
                source='layout', confidence=0.3))


def _overflow_px(child: Any, parent: Any) -> int:
    """子控件越出父容器的最大像素数（不越界为 0）。

    取四个方向里最大的那个 —— 报告里说「越出 40px」比说「越出」有用得多，
    幅度直接决定了它是取整误差还是真的写坏了布局。
    """
    return max(0,
               int(parent.left) - int(child.left),
               int(parent.top) - int(child.top),
               int(child.right) - int(parent.right),
               int(child.bottom) - int(parent.bottom))


#: 越界多少像素以内不算异常 —— 渲染取整/1px 边框会让相邻 bounds 差一两个像素。
LAYOUT_OVERFLOW_MIN_PX = 2


def _judge_layout_anomaly(sig: Signals, root: LayoutNode) -> None:
    """布局异常：**可见控件越出屏幕边界**（布局越界这一类）。

    ⚠️ 判据为什么选「越出屏幕」而不是更直觉的「越出父容器」——
    这是**真机数据**定的，不是偏好（复现：`tools/verify_layout_anomaly_real.py`
    之后的对比扫描，7 个真机样本）：

    | 判据 | 7 个样本的命中数 | 能不能用 |
    |---|---|---|
    | 越出父容器 | 每个样本都有 **11~13** 个 | ❌ 永远报警 —— 滚动列表的内容坐标与可视区坐标本就不同，而真机 `List` 的 `scrollable` 字段是 `False`，排不掉 |
    | 子比父大 | 每个样本都有 **8~9** 个 | ❌ 同样永远报警 |
    | **越出屏幕** | **全部为 0** | ✅ 有区分度、零误报 |

    一条「在所有正常页面上都报警」的判据等于没有判据 —— 它只会把真正的异常
    淹掉。所以这里用「越出屏幕」：控件跑到屏幕外，意味着用户**看不见也点不到**，
    这是明确的缺陷形态；而前两条在真机上属于常态。

    顺带：屏幕边界取根节点 bounds（uitest 的根就是 [0,0][w,h]），
    不额外查设备 —— 少一次设备往返，也避免两处尺寸对不上。

    产出**一条** Anomaly（不是每个越界控件一条）：一个页面布局写坏会产生多个
    越界节点，逐个报会把 `anomalies` 淹掉。evidence 给总数 + 最严重的 3 个。
    """
    screen = root.rect
    if screen.area <= 0:
        return                      # 空树/无窗口：这里不做判定（那是 NO_WINDOW 的活）

    offenders: List[Tuple[LayoutNode, int]] = []
    for n in root.walk():
        if n is root or not n.visible or n.rect.area <= 0:
            continue
        over = _overflow_px(n.rect, screen)
        if over > LAYOUT_OVERFLOW_MIN_PX:
            offenders.append((n, over))
    if not offenders:
        return

    offenders.sort(key=lambda x: -x[1])
    top = offenders[:3]
    detail = '；'.join(
        f'{n.type or "?"}{" id=" + n.id if n.id else ""} 越出 {over}px'
        f'（[{n.rect.left},{n.rect.top}][{n.rect.right},{n.rect.bottom}]）'
        for n, over in top)
    sig.anomalies.append(Anomaly(
        kind='LAYOUT_ANOMALY',
        evidence=(f'{len(offenders)} 个可见控件越出屏幕 '
                  f'[{screen.left},{screen.top}][{screen.right},{screen.bottom}]'
                  f'（不可见与零尺寸节点已排除）；最严重的 {len(top)} 个：{detail}'),
        source='layout', confidence=0.6))


def _collect_hilog(hdc, sig: Signals, lines: int, grep: Optional[str]) -> None:
    """取 hilog 末尾若干行，按关键词过滤后放进 `hilog_tail`。

    **必须带 `-x`**：不带参数时 hilog 是阻塞读，会把整条自动化流程挂死。
    `hdc.py::Hilog()` 已经用对了，这里直接调它。
    超时/异常一律降级为空列表 + warning。
    """
    try:
        text = hdc.hilog(lines=lines)
    except Exception as e:                                   # noqa: BLE001
        sig.warnings.append(f'hilog 采集失败（已降级为空）: {e}')
        return
    if not text:
        sig.warnings.append('hilog 返回为空（缓冲区可能已被冲掉）')
        return

    pattern = grep or HILOG_FILTER
    try:
        rx = re.compile(pattern)
    except re.error as e:
        sig.warnings.append(f'hilog 过滤正则无效，改用内置关键字表: {e}')
        rx = re.compile(HILOG_FILTER)
    rows = [ln for ln in text.splitlines() if rx.search(ln)]
    sig.hilog_tail = rows[:lines]


def _judge_crash(sig: Signals) -> None:
    """把「窗口内 + bundle 匹配」的崩溃记录转成客观异常信号。"""
    if sig.crashes:
        for rec in sig.crashes:
            fg = {True: '前台', False: '后台', None: '未知'}[rec.foreground]
            sig.anomalies.append(Anomaly(
                kind='CRASH',
                evidence=(f'崩溃日志 {rec.source_file}: module={rec.module_name} '
                          f'pid={rec.pid} reason={rec.reason or "<未解析出>"} '
                          f'timestamp={rec.timestamp or "<未解析出>"} 前台={fg}'),
                source='faultlog', confidence=1.0))

    if sig.process_alive is False:
        # 「进程没了」不是崩溃的充分条件（也可能是被 force-stop 或系统回收），
        # 所以它在有崩溃日志时是辅证，没有崩溃日志时只给较低的置信度。
        conf = 0.9 if sig.crashes else 0.35
        sig.anomalies.append(Anomaly(
            kind='CRASH',
            evidence=f'进程 {sig.bundle} 已不在 pidof 输出中（进程已消失）',
            source='hdc', confidence=conf))


def _judge_no_response(hdc, sig: Signals, win_start: float,
                       device_now: Optional[float] = None) -> None:
    """无响应的多信号叠加 —— 每条信号独立成 Anomaly，各带来源与置信度。

    **不做单信号硬判**。「无响应」（ANR / 卡死）本身没有
    一个可靠的单一判据，`freeze/` 的文件名格式甚至还没实测验证过，
    所以这里只叠加证据，由归因综合。

    窗口判定与 `_collect_crashes` 一致：下界 + 「未来」上界，
    两处任缺一个都会让设备时钟回跳后的旧文件冒充本轮证据。
    """
    # 信号 1：freeze/ 出现窗口内的新文件（可靠性高，但格式未验证）
    for entry in list_fault_dir(hdc, FREEZE_DIR, sig.warnings):
        when, _how = _file_time_epoch(entry)
        if when is not None and when < win_start:
            continue
        if (when is not None and device_now is not None
                and when > device_now + FUTURE_SLACK_S):
            continue
        # ★ bundle 过滤（模块头硬约束 3）：freeze/ 是**全体应用共用**的目录，
        # 别的应用 ANR 也在这里落文件。不滤就给 0.9 置信度，等于把
        # 「别人卡死了」记成「被测应用卡死」—— 违反本模块自己定的规则。
        # 文件名解析不出 bundle 时（appfreeze 命名本就未实测验证），
        # 归属未知 → 降置信度并在证据里写明，不许在「不知道是谁的」时还硬给高分。
        parsed = parse_fault_filename(entry.get('name') or '')
        fb = parsed.get('bundle', '')
        if fb and fb != sig.bundle:
            continue                      # 明确是别人的 ANR，跳过
        owned = (fb == sig.bundle)
        note = '' if owned else '（文件名解析不出 bundle，归属未确认，降置信）'
        sig.anomalies.append(Anomaly(
            kind='NO_RESPONSE',
            evidence=(f'freeze/ 目录出现窗口内文件 {entry.get("name")}{note} —— '
                      f'疑似 ANR（该目录命名格式尚未实测验证）'),
            source='faultlog', confidence=0.9 if owned else 0.5))

    # 信号 2：hilog 里的冻结/超时关键字（启发式，中等置信度）
    hits = [ln for ln in sig.hilog_tail
            if re.search(r'freeze|ANR|NotResponding|not responding|'
                         r'THREAD_BLOCK|ThreadBlock', ln, re.I)]
    if hits:
        sig.anomalies.append(Anomaly(
            kind='NO_RESPONSE',
            evidence=(f'hilog 中出现 {len(hits)} 行冻结/超时关键字，'
                      f'首行: {hits[0][:160]}'),
            source='hilog', confidence=0.45))


def _safe(name: str) -> str:
    """把设备侧文件名变成安全的本地文件名。"""
    return re.sub(r'[^A-Za-z0-9._-]', '_', name or 'fault')[:_MAX_LOCAL_NAME]


_MAX_LOCAL_NAME = 120
