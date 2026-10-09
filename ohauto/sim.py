"""
模拟设备 —— 无真机也能跑通整条管线
==================================

提供一个接口与 Hdc 完全一致的 FakeHdc，内部用「页面集合 + 跳转表」模拟
一台真实设备的行为。用途：

    1. **CI 里跑端到端测试** —— 没有硬件也能验证 Driver / DSL / 报告
    2. **新人上手** —— 不开真机就能理解整条链路
    3. **回归保护** —— 后续改代码时有个稳定的基准

它模拟了什么：
    - dumpLayout  : 按当前页返回控件树 JSON
    - screenCap   : 生成一张带页面名与点击落点的占位 PNG（真实文件）
    - uiInput click: 反查坐标命中的控件，按跳转表切页；命中的控件会被记录
    - keyEvent Back: 回到上一页（维护页面栈）
    - aa start    : 重置到首页（模拟冷启动）

它不模拟什么：
    真实渲染、动画时序、异步加载。所以它验证的是**编排逻辑**，
    不是**渲染正确性** —— 后者必须上真机。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import struct
import tempfile
import time
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .layout import Rect
# C8：备用输入通路的执行器**直接复用** `hdc` 里那三个 backend 类 ——
# 手写第二份命令拼装迟早会与真机分叉。它们以下划线开头（包内实现细节），
# 但 sim 与 hdc 同属本包、sim 是 L3 可依赖 L2，故此处引用是合规的。
from .hdc import (Hdc, InputUnavailable, _NullBackend, _parse_axis_ranges,
                  _SendeventBackend, _UinputBackend)


# ---------------------------------------------------------------- 页面定义

def _node(type_: str, cid: str, text: str, bounds: str,
          clickable: str = 'true', descr: str = '',
          visible: str = 'true', enabled: str = 'true',
          checked: str = '') -> Dict[str, Any]:
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': visible, 'enabled': enabled}
    if descr:
        a['descr'] = descr
    if checked:
        # 选中状态只在使用方显式给时才输出 —— 缺省节点与既有页面完全不变。
        a['checked'] = checked
    return {'attributes': a}


def _b(l, t, r, bo) -> str:
    """真机 `uitest dumpLayout` 的 bounds 写法：`"[left,top][right,bottom]"`。

    这里刻意与真机保持一致。早期版本返回的是
    `json.dumps({'left': .., 'top': ..})` —— 一种真机根本不会产出的形态，
    结果 113 个单测全绿，真机上却所有坐标退化成 0、点击全落屏幕左上角。
    教训：模拟器一旦和真机格式不一致，就是在替被测系统掩盖缺陷。
    回归护栏见 tests/test_core.py::TestRealDeviceFixture。
    """
    return f'[{l},{t}][{r},{bo}]'


# 三个页面的控件树：登录页 -> 首页 -> 详情页
PAGES: Dict[str, Dict[str, Any]] = {
    'login': {
        'attributes': {'type': 'Root', 'id': 'loginRoot',
                       'bounds': _b(0, 0, 1080, 2340), 'visible': 'true'},
        'children': [
            _node('Text', 'title', '欢迎登录', _b(240, 200, 840, 280), 'false'),
            _node('TextInput', 'username', '', _b(120, 400, 960, 500)),
            _node('TextInput', 'password', '', _b(120, 540, 960, 640)),
            _node('Button', 'btn_login', '登录', _b(120, 720, 960, 820)),
            _node('Button', 'btn_forget', '忘记密码', _b(120, 860, 960, 940)),
            _node('Button', 'btn_danger_delete', '注销账号', _b(120, 980, 960, 1060)),
            _node('Image', 'icon_wechat', '', _b(440, 1200, 520, 1280),
                  descr='微信快捷登录'),
        ],
    },
    'home': {
        'attributes': {'type': 'Root', 'id': 'homeRoot',
                       'bounds': _b(0, 0, 1080, 2340), 'visible': 'true'},
        'children': [
            _node('Text', 'tv_title', '首页', _b(40, 120, 400, 200), 'false'),
            _node('Button', 'tab_msg', '消息', _b(40, 300, 520, 400)),
            _node('Button', 'tab_order', '订单', _b(560, 300, 1040, 400)),
            _node('List', 'feed', '', _b(0, 460, 1080, 2000)),
            _node('Button', 'btn_logout', '退出登录', _b(120, 2100, 960, 2200)),
        ],
    },
    'order': {
        'attributes': {'type': 'Root', 'id': 'orderRoot',
                       'bounds': _b(0, 0, 1080, 2340), 'visible': 'true'},
        'children': [
            _node('Text', 'tv_order_title', '我的订单', _b(40, 120, 500, 200), 'false'),
            _node('ListItem', 'order_1', '订单 20260915001', _b(40, 300, 1040, 480)),
            _node('ListItem', 'order_2', '订单 20260915002', _b(40, 500, 1040, 680)),
            _node('Button', 'btn_back', '返回', _b(40, 2100, 300, 2200)),
        ],
    },
}

# 跳转表：(当前页, 控件 id) -> 目标页
TRANSITIONS: Dict[Tuple[str, str], str] = {
    ('login', 'btn_login'):   'home',
    ('login', 'icon_wechat'): 'home',
    ('home', 'tab_order'):    'order',
    ('order', 'btn_back'):    'home',
    ('home', 'btn_logout'):   'login',
}


#: 「非响应式」靶子页面 —— 模拟一个**布局写死坐标**的劣质适配应用。
#:
#: 为什么需要它：`PAGES` 里的正常页面是「等比响应式」的，跨形态跑出来
#: 永远 0 差异 —— 于是「差异报告有没有用」这件事在离线环境里验证不了。
#: 而 跨形态 的验收要求是「检出不少于 2 类真实适配问题」，必须有一个
#: 必然出问题的靶子才行。
#:
#: ★ 设计基准取**外屏 1080x2444**（折叠态，最常被当作「手机形态」），
#: 这是真实世界里最常见的错误来源：开发者只测了折叠态，展开成大屏后
#: 布局完全不适配。这个页面刻意复现该情形。
#:
#: 复现的三类真实缺陷（均在**展开态 2416x2210** 下暴露）：
#:   1. `panel_content` 写死 2000 高 —— 内屏只有 2210 高，
#:      元素底边 2400 > 2210 → **越界**（底部内容看不到）
#:   2. `btn_dock_bottom` 贴外屏底边（2364~2444）—— 内屏高仅 2210，
#:      整块跑到屏幕外 → **越界**
#:   3. `banner_half` 宽 1080 但内屏宽 2416 —— 只占左半边，
#:      视觉上「右边留白一半」，属**布局未拉伸**
#:
#: 坐标一律以 1080x2444 为设计基准，切形态时**纹丝不动** ——
#: 这正是「没做响应式」的定义。
_STATIC_LAYOUT_PAGE: Dict[str, Any] = {
    'attributes': {'type': 'Root', 'id': 'staticRoot',
                   'bounds': _b(0, 0, 1080, 2444), 'visible': 'true'},
    'children': [
        # ① 顶部横幅：按外屏宽度写死 1080（内屏 2416 下不拉伸）
        _node('Image', 'banner_fixed', '活动横幅',
              _b(0, 0, 1080, 300), 'false'),
        # ② 主内容区：写死高度 2100，底边 2400 —— 内屏(2210)下越界
        _node('List', 'panel_content', '内容列表',
              _b(0, 300, 1080, 2400)),
        # ③ 提交按钮：居中，写死 960 宽
        _node('Button', 'btn_submit_fixed', '提交',
              _b(60, 1000, 1020, 1100)),
        # ④ 底部悬浮操作条：贴外屏底边（2364~2444）——
        #    内屏高 2210 < 2364 → 整块出屏，用户完全看不到
        _node('Button', 'btn_dock_bottom', '底部操作条',
              _b(60, 2364, 1020, 2444)),
        # ⑤ 右上角设置图标（对照组：这个不该报问题）
        _node('Image', 'icon_ok', '', _b(920, 40, 1000, 120),
              'false', descr='设置'),
    ],
}

# 注册进页面表，入口 id 用 `staticRoot` 便于按页面名切换。
# 注意：这里是在 PAGES 定义**之后**追加，不影响上面三个页面的定义顺序。
PAGES['static_fixed'] = _STATIC_LAYOUT_PAGE

#: 哪些页面**不做**响应式重排（模拟「布局写死坐标」的应用）。
#:
#: 这张表就是靶子应用与正常应用的分界：
#: 表内的页面切形态后坐标纹丝不动 → 必然产出越界/溢出/不可达差异；
#: 表外的页面按屏幕等比重排 → 通常 0 差异。
NON_RESPONSIVE_PAGES: frozenset = frozenset({'static_fixed'})

# 密码框里的虚拟文本（用于校验 input 是否真的落到目标控件）
TYPED: Dict[str, str] = {}

# ---- C8 备用输入通路（uinput / sendevent）在模拟侧的常量
#
# `uinput --help` 的探测输出：真机 `Hdc._probe_backend('uinput')` 认的是
# 「rc==0 且输出含 usage」，这里照抄同一判据，否则备用通路在离线环境里
# 永远探成「不可用」（模拟器不保真 = 测试全绿反而危险）。
_UINPUT_USAGE = 'usage: uinput [-T|-K|-M] ...\n'

#: uinput `-K -l <code>` 收的是 **OpenHarmony KeyCode**（真机实测 1=Home、2=Back）。
_UINPUT_OHOS_KEYS = {1: 'Home', 2: 'Back'}


def _is_int(s: Any) -> bool:
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- PNG 生成

def _write_png(path: str, w: int, h: int, rgb=(250, 250, 248)) -> None:
    """写一张纯色 PNG。不依赖 Pillow，用 zlib 手工拼。

    截图内容本身不重要，重要的是**真的产出一个可被后续流程读取的文件**，
    这样才能验证「拉取截图」这一步没有断。
    """
    raw = b''.join(b'\x00' + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
           chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(png)


def _write_content_png(path: str, w: int, h: int) -> None:
    """写一张**有内容**的 PNG（每像素颜色各不相同），体积接近真机截图。

    与 `_write_png()` 相对：那个写纯色页（体积极小，正是白屏的特征），
    这个写「正常页面」，供白屏检测做**负样本**。
    用确定性伪随机，保证同样的输入产出同样的文件（测试可复现）。
    """
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            v = (x * 7 + y * 13 + (x * y) % 251) & 0xFF
            row += bytes(((v * 3) & 0xFF, (v * 5) & 0xFF, (v * 11) & 0xFF))
        rows.append(b'\x00' + bytes(row))
    raw = b''.join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) +
           chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(png)


# ---------------------------------------------------------------- 崩溃日志模拟

# 设备侧目录（与 ohauto/signals.py 的常量一致 —— 真机实测路径）
FAULTLOG_DIRS = {
    'faultlogger': '/data/log/faultlog/faultlogger',
    'freeze': '/data/log/faultlog/freeze',
    'temp': '/data/log/faultlog/temp',
}

# 一份最小的崩溃日志模板。
#
# ★ 注意：**解析器的开发与回归测试用的是真机样本**
#   （tests/fixtures/signals/cppcrash_real.log），不是这个模板。
#   模板只用来在模拟环境里把「注入崩溃 → 采集 → 解析」这条**链路**跑通，
#   字段名严格照抄真机实测格式，避免模拟器与真机格式不一致。
CRASH_TEMPLATE = """Generated by HiviewDFX@OpenHarmony
================================================================
Device info:OpenHarmony 3.2
Build info:OpenHarmony 5.0.3.135
Module name:{bundle}
Version:1.0.0
VersionCode:1000000
PreInstalled:Yes
Foreground:Yes
Timestamp:{timestamp}
Pid:{pid}
Uid:{uid}
Process name:{bundle}
Process life time:6s
Reason:Signal:SIGSEGV(SI_USER)@0x000025d9 from:{tid}:0
Fault thread info:
Tid:{pid}, Name:{bundle}
#00 pc 000912d8 /system/lib/ld-musl-arm.so.1(epoll_wait+28)(90eb79e807337289ff99d3d78741363f)
#01 pc 000111a1 /system/lib/chipset-pub-sdk/libeventhandler.z.so(EventQueue::GetEvent()+140)(40b66d055c355508bfd0e27c2ec9de63)
#02 pc 0001e297 /system/lib/chipset-pub-sdk/libeventhandler.z.so(EventRunnerImpl::Run()+586)(40b66d055c355508bfd0e27c2ec9de63)
Registers:
fp:ffbf28a8 ip:0000002d sp:ffbf2898 lr:f63111a5 pc:f7c502d8
HiLog:
09-16 15:14:47.777  {pid}  {pid} I C02d11/DfxSignalHandler: DFX_SigchainHandler :: sig(11), pid({pid}), tid({pid}).
"""


def make_crash_log(bundle: str = 'com.example.app', pid: int = 9639,
                   uid: int = 20010019, epoch: Optional[float] = None) -> str:
    """按真机格式生成一份崩溃日志文本（仅供模拟设备使用）。"""
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch or time.time()))
    return CRASH_TEMPLATE.format(bundle=bundle, pid=pid, uid=uid,
                                 timestamp=f'{ts}.000', tid=pid + 50)


def _fault_filename(bundle: str, uid: int, epoch: float, kind: str = 'cppcrash') -> str:
    """真机命名规律：`cppcrash-<bundleName>-<uid>-<YYYYMMDDHHMMSS>`。"""
    return f'{kind}-{bundle}-{uid}-{time.strftime("%Y%m%d%H%M%S", time.localtime(epoch))}'


# ---------------------------------------------------------------- FakeHdc

def _make_exc(kind: str, detail: str) -> BaseException:
    """按类别构造一个「真机同款」的异常。

    这里刻意复用 hdc / driver 层真实抛出的异常类型与消息措辞，
    这样 runner 的失败分类器在模拟环境下得到的结果，与真机一致 ——
    否则又会出现「模拟器全绿、真机全崩」的老问题。
    """
    from .hdc import DeviceNotFound, HdcError

    if kind == 'timeout':
        return HdcError(f'hdc 命令超时(20s): {detail}')
    if kind == 'device':
        return DeviceNotFound(f'no device found / device offline: {detail}')
    if kind == 'hdc':
        return HdcError(f'hdc 执行失败 rc=1: {detail}')
    if kind == 'app':
        return RuntimeError(f'failed to start ability. {detail}')
    if kind == 'locate':
        from .driver import DriverError
        return DriverError(f'定位失败：控件未找到 -> {detail}')
    if kind == 'assert':
        from .driver import DriverError
        return DriverError(f'断言失败：控件应存在但未找到 -> {detail}')
    if kind == 'dsl':
        from .action import DslError
        return DslError(f'不支持的动作: {detail}')
    return RuntimeError(f'unknown fault kind={kind}: {detail}')


class FaultPlan:
    """给模拟设备注入故障 —— 让重试与自愈逻辑可以被测试。

    没有它，「重试到底work不work」只能靠真机上碰运气；有了它，
    重试策略、退避、设备恢复、级联识别全部可以在 CI 里跑。

    三种注入方式：
        fail_next(target, times)   接下来 N 次调用失败（一次性故障）
        fail_every(target, every)  每 N 次调用失败一次（周期性偶发故障，用于压测）
        fail_always(target)        一直失败（模拟设备彻底掉线）

    target 取值：list_targets / dump_layout / screen_cap / pull / shell /
                 ui_input / hilog / any
    kind 取值：  timeout / device / hdc / app / locate / assert / dsl
    """

    def __init__(self):
        self._rules: List[Dict[str, Any]] = []
        self._counts: Dict[str, int] = {}
        self.injected: List[str] = []        # 实际注入过的记录，供断言

    # ---------------------------------------------------------- 配置

    def fail_next(self, target: str, times: int = 1, kind: str = 'hdc',
                  match: str = '') -> 'FaultPlan':
        self._rules.append({'mode': 'next', 'target': target, 'left': times,
                            'kind': kind, 'match': match})
        return self

    def fail_every(self, target: str, every: int, kind: str = 'hdc',
                   match: str = '', limit: int = 1000) -> 'FaultPlan':
        self._rules.append({'mode': 'every', 'target': target, 'every': max(1, every),
                            'kind': kind, 'match': match, 'limit': limit, 'fired': 0})
        return self

    def fail_always(self, target: str, kind: str = 'device',
                    match: str = '') -> 'FaultPlan':
        self._rules.append({'mode': 'always', 'target': target, 'kind': kind,
                            'match': match})
        return self

    def clear(self) -> None:
        self._rules.clear()
        self._counts.clear()

    # ---------------------------------------------------------- 判定

    def check(self, target: str, detail: str = '') -> Optional[BaseException]:
        """返回要抛出的异常；None 表示本次调用正常。"""
        for rule in self._rules:
            if rule['target'] not in (target, 'any'):
                continue
            if rule.get('match') and rule['match'] not in detail:
                continue

            mode = rule['mode']
            if mode == 'always':
                self.injected.append(f'{target}:{rule["kind"]}')
                return _make_exc(rule['kind'], detail or target)

            if mode == 'next':
                if rule['left'] > 0:
                    rule['left'] -= 1
                    self.injected.append(f'{target}:{rule["kind"]}')
                    return _make_exc(rule['kind'], detail or target)

            if mode == 'every':
                key = f'{target}|{rule.get("match","")}'
                self._counts[key] = self._counts.get(key, 0) + 1
                if (self._counts[key] % rule['every'] == 0
                        and rule['fired'] < rule['limit']):
                    rule['fired'] += 1
                    self.injected.append(f'{target}:{rule["kind"]}')
                    return _make_exc(rule['kind'], detail or target)
        return None

    @property
    def active(self) -> bool:
        return any(r['mode'] == 'always'
                   or (r['mode'] == 'next' and r['left'] > 0)
                   or (r['mode'] == 'every')
                   for r in self._rules)

    def stats(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for s in self.injected:
            out[s] = out.get(s, 0) + 1
        return out


class FakeHdc:
    """接口与 ohauto.Hdc 一致，行为由模拟设备驱动。"""

    DEVICE_TMP = '/data/local/tmp'

    def __init__(self, start_page: str = 'login', screen: Tuple[int, int] = (1080, 2340),
                 seed: Optional[Dict[str, Dict[str, Any]]] = None,
                 transitions: Optional[Dict[Tuple[str, str], str]] = None,
                 verbose: bool = False,
                 faults: Optional['FaultPlan'] = None,
                 locked: bool = False,
                 crash_logs: Optional[Sequence[Any]] = None,
                 freeze_logs: Optional[Sequence[Any]] = None,
                 hilog_text: Optional[Any] = None,
                 device_time: Optional[float] = None,
                 dead_bundles: Optional[Sequence[str]] = None,
                 screen_style: str = 'content',
                 frozen: bool = False,
                 faultlog_denied: bool = False,
                 screens: Optional[Sequence[Dict[str, Any]]] = None,
                 folded_state: Optional[str] = None,
                 responsive: bool = True):
        self.screen = screen
        # ---- 多屏 / 折叠态（跨形态测试用）
        #
        # 为什么需要这个：真机上 `hidumper -s RenderService -a screen` 返回的是
        # **每块屏一段**的结构，而 Mate X7 这类折叠设备**两块屏同时注册**。
        # 早期模拟器只回一句假的 `screen size: W x H`，格式与真机完全不同 ——
        # 于是「折叠态切换是否真的生效」在模拟环境里根本测不出来，
        # 只有上真机才暴露（又一例「模拟器不保真 = 测试全绿反而危险」）。
        #
        # `screens` 不传时从 `screen` 元组合成**单块常亮屏**，
        # 保证既有测试与既有调用方行为完全不变。
        self.screens = self._normalize_screens(screens, screen)
        self.folded_state = folded_state
        # ---- 响应式重排（跨形态测试用）
        #
        # True（默认）：控件树坐标跟随当前屏幕尺寸重排 —— 模拟真机上
        # 「应用会按屏幕宽度重新布局」的行为。跨形态比对必须有它，
        # 否则两形态的树完全一样、差异恒为 0（见 `_tree_once()`）。
        #
        # False：坐标写死不动。给「专门验证某个固定布局」的测试用。
        self.responsive = responsive
        self.pages = seed or PAGES
        self.transitions = transitions or TRANSITIONS
        self.current = start_page
        self.stack: List[str] = []
        self.verbose = verbose
        self.hdc_path = '<simulated>'
        self.target = 'simulator'
        # 实例属性，与 `Hdc.__init__` 对齐：`HdcLike` 契约要求它存在。
        # 只有 `DEVICE_TMP` 类属性过不了 isinstance（3.12+ 会连数据成员一起查），
        # 而 `Driver.refresh()` 过去正是为此写了 getattr 兜底链。
        self.tmp_dir = self.DEVICE_TMP
        self.actions: List[Dict[str, Any]] = []      # 操作留痕，供断言
        self._files: Dict[str, bytes] = {}
        self.calls: List[str] = []
        self.faults = faults or FaultPlan()          # 故障注入计划
        self.locked = locked                         # 锁屏状态（控件树全空）

        # ---- 信号采集所需的模拟能力（新增，不影响上面任何既有行为）
        # 设备时钟。默认与本机一致；要模拟「RTC 不准」就显式传一个偏移很大的值。
        self.device_time = time.time() if device_time is None else float(device_time)
        # 崩溃日志：设备侧路径 -> 文本。注册进 _files 后，cat / pull 自动可用。
        self.fault_logs: Dict[str, str] = {}
        # 目录 mtime/size，供 `ls -l` 使用
        self.fault_meta: Dict[str, Dict[str, Any]] = {}
        self.hilog_text = hilog_text
        self.dead_bundles = set(dead_bundles or ())
        # 2C 性能采样（ohauto/perf.py）需要的设备侧资源水位，均可按需改写：
        # pss_kb —— `hidumper --mem <pid>` 里的 PSS 合计（真机 DAYU200 实测口径）；
        # loadavg —— `/proc/loadavg` 原文（1/5/15 分钟负载）。
        self.pss_kb = 41322
        self.loadavg = '0.50 0.35 0.30 2/1234 5678'
        # 'content'（默认，模拟真机正常内容页）| 'solid'（纯色图，专用于测白屏判据）
        #
        # 默认值必须是 'content'。早期默认是 'solid'，后果是：**干净场景
        # （什么都没发生）也被白屏判据判成 WHITE_SCREEN，置信度 0.99** ——
        # 因为一张纯色图在真机语境下确实等于白屏，而模拟器默认就产出纯色图，
        # 等于「模拟器自己不模拟一个正常的页面」，给下游归因模块喂假证据。
        # 这与 `_b()` 返回 JSON bounds 是同一类病：模拟器与真机不一致时，
        # 测试全绿反而是最危险的状态。
        # 要测白屏判据请显式传 screen_style='solid'。
        self.screen_style = screen_style
        self.frozen = frozen                         # 卡死：控件树永不变
        self.faultlog_denied = faultlog_denied       # 模拟非 root 读不到崩溃日志
        self._frozen_tree: Optional[Dict[str, Any]] = None

        # ---- C8 备用输入通路（uinput / sendevent）模拟所需状态
        # 默认 uitest，与 `Hdc.__init__` 一致（未探测）；`detect_backend()` 后
        # 才反映真实可用通路。三者皆无时置 'none'（不可交互档）。
        self._backend_name = 'uitest'
        #: uinput 的文本输入命令（`-K -t`）不带坐标，落在**最近一次点击**处。
        self._last_tap: Optional[Tuple[int, int]] = None
        #: sendevent 手势状态：down / move / up 是**三次独立**的 hdc 往返，
        #: 手指按下的起点必须跨调用保留，否则还原不出「点击 vs 滑动」。
        self._se_state: Dict[str, Any] = {}

        for item in (crash_logs or ()):
            self.add_fault_log(item, dirname='faultlogger')
        for item in (freeze_logs or ()):
            self.add_fault_log(item, dirname='freeze')

    # ------------------------------------------------------ 多屏 / 折叠态

    @staticmethod
    def _normalize_screens(screens: Optional[Sequence[Dict[str, Any]]],
                           single: Tuple[int, int]) -> List[Dict[str, Any]]:
        """把 `screens` 参数规整成统一结构；不传则用 `single` 合成一块屏。

        每块屏的字段（与 `emulator_cli.parse_screen_info()` 的输出对齐）：
            index / power_status / backlight / width / height
            （可选的 active_width / active_height）
        """
        if not screens:
            w, h = single
            return [{'index': 0, 'power_status': 'POWER_STATUS_ON',
                     'backlight': 10, 'width': int(w), 'height': int(h)}]
        out: List[Dict[str, Any]] = []
        for i, s in enumerate(screens):
            out.append({
                'index': int(s.get('index', i)),
                'power_status': str(
                    s.get('power_status', 'POWER_STATUS_ON')),
                'backlight': int(s.get('backlight', 10)),
                'width': int(s['width']),
                'height': int(s['height']),
            })
        return out

    def set_folded_state(self, state: str) -> None:
        """切折叠态 —— 模拟 `Emulator.exe -instance <名> -foldedState <state>`。

        ★ **真实行为**（实测 Mate X7，双屏折叠）：
        `-foldedState` **不改分辨率**，只改两块屏的 `powerStatus`：

            open / half-open → screen[0] 内屏 ON（2416x2210）
            close            → screen[1] 外屏 ON（1080x2444）

        所以「切换是否生效」的判据是 **powerStatus 互换**，不是分辨率。
        这里如实复现该行为 —— 否则下游会写出「比对分辨率」的错误判据。
        """
        self.folded_state = state
        if len(self.screens) < 2:
            # 单屏设备：折叠态没有可切换的对象，保持原样（不造假动作）
            self._log(f'foldedState {state} -> 单屏设备，无变化')
            return
        # 双屏：close 点亮第二块（外屏），其余点亮第一块（内屏）
        primary, secondary = self.screens[0], self.screens[1]
        if state == 'close':
            primary['power_status'] = 'POWER_STATUS_OFF'
            secondary['power_status'] = 'POWER_STATUS_ON'
        else:                      # open / half-open / 其它一律回内屏
            primary['power_status'] = 'POWER_STATUS_ON'
            secondary['power_status'] = 'POWER_STATUS_OFF'
        self._log(f'foldedState {state} -> 点亮屏 index='
                  f'{"1" if state == "close" else "0"}')

    def _active_screen(self) -> Optional[Dict[str, Any]]:
        """当前点亮的屏（若有）。"""
        for s in self.screens:
            if 'ON' in str(s.get('power_status', '')).upper():
                return s
        return None

    @property
    def screen_size(self) -> Tuple[int, int]:
        """当前**可见**尺寸 = 点亮屏的尺寸（没有点亮的屏则退回第一块）。

        真机上截图/坐标都是相对当前可见屏的，所以折叠态切到外屏后
        尺寸应该跟着变 —— 用这个属性而不是直接读 `self.screen`。
        """
        act = self._active_screen()
        if act:
            return (int(act['width']), int(act['height']))
        return (int(self.screens[0]['width']), int(self.screens[0]['height']))

    def _screen_dump(self) -> str:
        """产出与真机**同格式**的 `hidumper -s RenderService -a screen` 文本。

        格式取自真机原文夹具：
            tests/fixtures/crossform/renderservice_screen_harmonyos7_20260918_1916.txt

        ⚠️ 别改这个格式的字段名/顺序 —— 解析器
        `emulator_cli.parse_screen_info()` 依赖它，两边必须一致。
        """
        lines = ['', '-------------------------------[ability]------------'
                 '-------------------', '', '',
                 '----------------------------------RenderService------'
                 '----------------------------', '-- ScreenInfo']
        for s in self.screens:
            w, h = s['width'], s['height']
            lines.append(
                f'screen[{s["index"]}]: id={s["index"] * 5}, '
                f'powerStatus={s["power_status"]}, '
                f'backlight={s["backlight"]}, '
                f'screenType=EXTERNAL_TYPE, '
                f'render resolution={w}x{h}, '
                f'physical resolution={w}x{h}, '
                f'isVirtual=false, skipFrameInterval=1, '
                f'expectedRefreshRate=-1, skipFrameStrategy=0')
            lines.append(f'supportedMode[0]: {w}x{h}, refreshRate=60')
            lines.append(f'activeMode: {w}x{h}, refreshRate=60')
            lines.append(f'name=express_display, phyWidth={w}, '
                         f'phyHeight={h}, supportLayers=10, '
                         f'virtualDipCount=1, propertyCount=0, '
                         f'type=DISP_INTF_UNKNOW, supportWriteBack=false')
            lines.append('isSamplingOn=0, samplingScale=1.00, '
                         'samplingTranslateX=0.00, samplingTranslateY=0.00')
            lines.append('enableVisibleRect=0, '
                         'mainScreenVisibleRect=[0,0,0,0]')
            lines.append('-- ScreenInfo')
        # 真机末尾有这两行；去掉最后一个多余的 -- ScreenInfo
        lines.pop()
        lines.append('===================')
        lines.append('foldScreenIds_ size is 0')
        return '\n'.join(lines)

    def _mem_dump(self) -> str:
        """产出与真机**同构**的 `hidumper --mem <pid>` 内存表。

        解析规则（ohauto/perf.py `_parse_pss`）依赖三个特征：表头行含 Pss；
        随后有一行**不带数字**的 Total 单位行；数值行首个数字 = PSS 合计
        （KB）。两侧格式必须一致，否则「模拟绿、真机挂」。
        """
        return ('                                Pss  Shared  Private  '
                'SwapPss  Pss\n'
                '                                Total  Clean  Dirty\n'
                f'            Total   {self.pss_kb}  2048  8192  0\n')

    # ------------------------------------------------------ 故障注入

    def _fault(self, target: str, detail: str = '') -> None:
        exc = self.faults.check(target, detail)
        if exc is not None:
            self._log(f'*** 注入故障 {target}: {type(exc).__name__} - {exc}')
            raise exc

    # ------------------------------------------------------ 崩溃日志注入

    def add_fault_log(self, item: Any, dirname: str = 'faultlogger') -> str:
        """往模拟设备的 faultlog 目录里放一份日志。

        `item` 可以是：

        * `str`  —— 日志正文。文件名按真机规律从正文的 `Module name` 推出。
        * `dict` —— 支持 `text` / `name` / `bundle` / `uid` / `epoch` /
          `dirname`，缺的按真机规律补。

        返回设备侧完整路径。
        """
        if isinstance(item, str):
            spec: Dict[str, Any] = {'text': item}
        else:
            spec = dict(item or {})

        text = spec.get('text', '')
        epoch = float(spec.get('epoch') or self.device_time)
        dirname = spec.get('dirname', dirname)

        bundle = spec.get('bundle')
        if not bundle:
            m = re.search(r'^Module name:(.+)$', text, re.M)
            bundle = m.group(1).strip() if m else 'com.example.app'
        uid = int(spec.get('uid', 20010019))
        name = spec.get('name') or _fault_filename(bundle, uid, epoch)

        path = f'{FAULTLOG_DIRS.get(dirname, dirname)}/{name}'
        self.fault_logs[path] = text
        self.fault_meta[path] = {'name': name, 'size': len(text.encode('utf-8')),
                                 'epoch': epoch, 'dirname': dirname}
        self._files[path] = text.encode('utf-8')
        self._log(f'faultlog + {path} ({len(text)} 字符)')
        return path

    def inject_crash(self, bundle: str = 'com.ohos.note', pid: int = 9639,
                     uid: int = 20010019, text: Optional[str] = None,
                     epoch: Optional[float] = None) -> str:
        """注入一次崩溃：落一份崩溃日志，并把该 bundle 的进程标记为已消失。

        这是 `kill -11 <pid>` 那条真机验证路径的模拟版。真实用法见任务卡
        第三章四；这里只用来让「注入崩溃 → 采集 → 能捕获」在无真机时也能验证。
        """
        epoch = self.device_time if epoch is None else float(epoch)
        body = text if text is not None else make_crash_log(bundle, pid, uid, epoch)
        path = self.add_fault_log({'text': body, 'bundle': bundle, 'uid': uid,
                                   'epoch': epoch}, dirname='faultlogger')
        self.dead_bundles.add(bundle)
        return path

    def inject_freeze(self, bundle: str = 'com.ohos.note', uid: int = 20010019,
                      text: Optional[str] = None,
                      epoch: Optional[float] = None) -> str:
        """注入一次 ANR（往 `freeze/` 落文件）。

        ⚠️ `freeze/` 的真实文件名格式**尚未实测验证**（任务卡第三章五），
        这里按 `appfreeze-<bundle>-<uid>-<时间戳>` 推测。凡依赖它的判据
        都必须能优雅降级。
        """
        epoch = self.device_time if epoch is None else float(epoch)
        body = text if text is not None else f'Module name:{bundle}\nReason:appfreeze\n'
        return self.add_fault_log({'text': body, 'bundle': bundle, 'uid': uid,
                                   'epoch': epoch, 'name':
                                   _fault_filename(bundle, uid, epoch, 'appfreeze')},
                                  dirname='freeze')

    def fault_dir_names(self, dirname: str) -> List[str]:
        return sorted(meta['name'] for meta in self.fault_meta.values()
                      if meta['dirname'] == dirname)

    def _ls_fault_dir(self, device_dir: str) -> Optional[Any]:
        """`ls -l <faultlog 目录>` 的模拟输出。不属于 faultlog 树时返回 None。

        返回 `(stdout, returncode, stderr)` 三元组以便模拟权限不足。
        """
        d = device_dir.rstrip('/')
        if not d.startswith('/data/log/faultlog'):
            return None
        if self.faultlog_denied:
            # 非 root 设备读不到 faultlogger/（权限 drwxr-x--- hiview log）。
            # 任务卡第三章第五节把这条列为**未验证项**，所以采集必须能降级。
            return '', 1, f'ls: {d}: Permission denied'
        names = None
        for key, path in FAULTLOG_DIRS.items():
            if d == path:
                names = key
                break
        if names is None:
            # 根目录：列出四个子目录（真机结构，任务卡第三章二）
            if d == '/data/log/faultlog':
                return ('drwxr-x--- 2 root root 4096 {ts} debug\n'
                        'drwxr-x--- 2 root log 4096 {ts} faultlogger\n'
                        'drwxr-x--- 2 root log 4096 {ts} freeze\n'
                        'drwxr-x--- 2 root log 4096 {ts} temp\n'
                        ).format(ts=time.strftime(
                            '%Y-%m-%d %H:%M', time.localtime(self.device_time))), 0, ''
            return None

        lines = []
        for path, meta in sorted(self.fault_meta.items()):
            if meta['dirname'] != names:
                continue
            lines.append('-rw-r----- 1 root log {size} {ts} {name}'.format(
                size=meta['size'],
                ts=time.strftime('%Y-%m-%d %H:%M', time.localtime(meta['epoch'])),
                name=meta['name']))
        return '\n'.join(lines), 0, ''

    # ------------------------------------------------------ 内部

    def _tree(self) -> Dict[str, Any]:
        if self.locked:
            # 真机锁屏 / 无前台窗口时的真实返回：只有一个 bounds 全 0 的根节点。
            # 复现这个形态很重要 —— 它是「控件树突然全空」的唯一根因，
            # 而且极难从报错反推。
            return {'attributes': {'type': '', 'id': '', 'text': '',
                                   'bounds': '[0,0][0,0]'},
                    'children': []}
        if self.frozen:
            # 应用卡死：界面渲染线程不再更新，dumpLayout 永远返回同一棵树
            # （哪怕上层又点了别的地方）。这是「无响应」的客观特征之一。
            if self._frozen_tree is None:
                self._frozen_tree = self._tree_once()
            return json.loads(json.dumps(self._frozen_tree))
        return self._tree_once()

    def _tree_once(self) -> Dict[str, Any]:
        tree = json.loads(json.dumps(self.pages[self.current]))
        # 靶子页面（写死坐标的应用）不做重排 —— 那正是它的特征。
        if self.responsive and self.current not in NON_RESPONSIVE_PAGES:
            # ★ 按当前屏幕尺寸重排坐标。
            #
            # 为什么必须做：`PAGES` 里的坐标是**写死的 1080x2340**。
            # 折叠态切到外屏（1080x2444）或换一台不同尺寸的设备后，
            # 若控件树仍返回 1080x2340 的坐标，跨形态比对就**永远比不出差异** ——
            # 因为两边的树一模一样。那等于替被测应用掩盖了所有适配问题，
            # 与本模块一贯坚持的「模拟器必须保真」原则直接冲突。
            #
            # 实测踩到过：不重排时跨形态报告恒为「0 差异」，
            # 而真机上同一应用确实有元素越界。
            self._relayout(tree, self.screen_size)
        return tree

    def _relayout(self, tree: Dict[str, Any],
                  size: Tuple[int, int]) -> None:
        """把一棵控件树的坐标按 `size` 做响应式重排（原地修改）。

        模型（刻意简单、可预测，别让模拟器自己造出不可解释的差异）：

        1. **横向**：按 `new_w / old_w` 等比例缩放 `left/right`。
           这条模拟「容器宽度自适应」—— 宽度变了元素跟着变。
        2. **纵向**：**不缩放**，保持原始 `top`（只夹到屏幕内）。
           因为纵向布局通常是「从上往下堆」，屏幕变矮时不是等比压缩，
           而是**底部元素被挤出可视区** —— 这正是跨形态最想检出的问题。
           若纵向也等比缩，底部元素永远刚好塞得下，就永远检不出。
        3. **根节点**：直接改成整屏，它是屏幕的映射。

        纵向「夹到屏幕内」的取舍：超出部分**保留原坐标不截断** ——
        截断会让「越界」变成「刚好在边界」，反而掩盖问题。
        真正的越界判定由 `crossform` 负责，模拟器只管如实提供坐标。
        """
        old_w, _old_h = self.screen
        new_w, _new_h = size
        if old_w <= 0 or not self.responsive:
            return
        ratio = new_w / old_w

        def rec(node: Dict[str, Any], is_root: bool = False) -> None:
            a = node.get('attributes')
            if isinstance(a, dict):
                if is_root:
                    a['bounds'] = f'[0,0][{new_w},{_new_h}]'
                else:
                    r = Rect.parse(a.get('bounds'))
                    if r.area > 0 or r.right or r.bottom:
                        nl = int(round(r.left * ratio))
                        nr = int(round(r.right * ratio))
                        a['bounds'] = f'[{nl},{r.top}][{nr},{r.bottom}]'
            for c in node.get('children') or ():
                rec(c)

        rec(tree, is_root=True)

    def _all_nodes(self, page: Optional[str] = None) -> List[Dict[str, Any]]:
        # ★ 走 `_tree()` 而不是直接读 `self.pages`：坐标必须与
        # `dumpLayout` 交给调用方的那棵树**完全一致**。
        # 早期版本直接读 `self.pages`（未重排的原始坐标），后果是
        # 「点击命中判定用旧坐标、控件树给你新坐标」—— 模拟器自己前后矛盾，
        # 上层按控件树算出的点击坐标在模拟器里会被判成没点中。
        saved = self.current
        if page:
            self.current = page
        try:
            tree = self._tree()
        finally:
            self.current = saved
        out = []

        def rec(n):
            a = n.get('attributes', {})
            out.append(a)
            for c in n.get('children', []):
                rec(c)
        rec(tree)
        return out

    def _hit(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        """反查坐标命中的控件：取面积最小（最具体）的那个。"""
        best = None
        for a in self._all_nodes():
            r = Rect.parse(a.get('bounds'))
            if r.contains(x, y) and r.area > 0:
                if best is None or r.area < Rect.parse(best['bounds']).area:
                    best = a
        return best

    def _log(self, s: str) -> None:
        if self.verbose:
            print(f'[sim] {s}')

    # ------------------------------------------------------ 设备查询

    def list_targets(self) -> List[str]:
        self._fault('list_targets', 'list targets')
        return ['simulator']

    def wait_device(self, timeout: int = 60, interval: float = 2.0) -> str:
        self._fault('list_targets', 'wait device')
        return 'simulator'

    def version(self) -> str:
        return 'Ver: 3.2.0c (simulated)'

    def uitest_version(self) -> Optional[str]:
        return 'uitest 1.0.0 (simulated)'

    # ------------------------------------------------------ 文件

    def screen_cap(self, device_path: str = None) -> str:
        """模拟 `uitest screenCap`。

        产出图由 `screen_style` 决定：默认 `'content'`（正常内容页，模拟真机），
        `'solid'`（纯色图）**只用于专门测试白屏判据**。
        """
        self._fault('screen_cap', 'uitest screenCap')
        p = device_path or f'{self.DEVICE_TMP}/ohauto_shot.png'
        # ★ 用 `screen_size`（跟随点亮屏）而不是 `self.screen`：
        # 折叠态切到外屏后，截图尺寸应随之改变（真机就是这样）。
        w, h = self.screen_size
        # 写到一个固定的临时目录，**绝对不要写进程当前目录**。
        # 早期版本写的是 os.getcwd()，结果每跑一次测试就在「你恰好所在的目录」
        # 留下 _sim_login.png / _sim_home.png 之类的垃圾 —— 项目根里实测攒了 5 个，
        # 而且因为文件小、平时不注意，很久都没人发现。
        #
        # 文件名带上 screen_style：否则两个 style 不同的 FakeHdc 实例会互相覆盖
        # 同一个 temp 文件，后跑的那次读到前一次的内容，测出来的结论是假的。
        tmp_png = os.path.join(tempfile.gettempdir(),
                               f'ohauto_sim_{self.current}_{self.screen_style}.png')
        if self.screen_style == 'content':
            _write_content_png(tmp_png, w // 8, h // 8)
        else:
            _write_png(tmp_png, w // 8, h // 8)
        with open(tmp_png, 'rb') as f:
            self._files[p] = f.read()
        self.calls.append('screenCap')
        self._log(f'screenCap -> {p} (页面 {self.current}, {self.screen_style})')
        return p

    def dump_layout(self, device_path: str = None, unfiltered: bool = False,
                    with_attrs: bool = False) -> str:
        self._fault('dump_layout', 'uitest dumpLayout')
        p = device_path or f'{self.DEVICE_TMP}/ohauto_layout.json'
        self._files[p] = json.dumps(self._tree(), ensure_ascii=False).encode('utf-8')
        self.calls.append('dumpLayout')
        self._log(f'dumpLayout -> {p} (页面 {self.current})')
        return p

    def pull(self, device_path: str, local_path: str,
             binary: bool = False) -> str:
        # binary 参数与 `Hdc.pull` 对齐（后者用它禁用二进制的 cat 兜底）。
        # 模拟环境收下但行为不变 —— 分词/内容语义与二进制无关。
        self._fault('pull', f'file recv {device_path}')
        data = self._files.get(device_path)
        if data is None:
            raise RuntimeError(f'[sim] 设备上不存在文件: {device_path}')
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        with open(local_path, 'wb') as f:
            f.write(data)
        return local_path

    def push(self, local_path: str, device_path: str) -> None:
        with open(local_path, 'rb') as f:
            self._files[device_path] = f.read()

    # ------------------------------------------------------ shell

    class _R:
        def __init__(self, out: str = '', rc: int = 0, err: str = ''):
            self.returncode, self.stdout, self.stderr = rc, out, err

        @property
        def ok(self) -> bool:
            return self.returncode == 0

    def shell(self, cmd: str, **kw):
        self.calls.append(f'shell:{cmd}')
        self._fault('shell', cmd)
        # echo 必须支持：DeviceGuard 的心跳探测就是靠 `hdc shell echo xxx`。
        # 早期版本没有这一条，导致模拟环境里心跳永远失败、设备自愈
        # 逻辑完全测不了（真机上却是好的）—— 又一个「模拟器不保真」的坑。
        if cmd.startswith('echo '):
            return self._R(cmd[5:].strip())
        # ★ `uitest dumpLayout ... && cat <路径>` —— 合并成一次往返的取树路径
        # （`driver.refresh` 的非留痕分支就走这条，实测省约 150ms/次）。
        # 模拟侧**必须同样认这条命令**：不认就等于整条取树路径在模拟环境里
        # 变成空操作 —— 这正是「真机改了、模拟没跟上」那类假通过的另一面。
        # 输出格式也要对齐真机：先一行 `DumpLayout saved to:<路径>`，再是 JSON。
        if cmd.startswith('uitest dumpLayout') and '&& cat ' in cmd:
            dev = cmd.split('&& cat ', 1)[1].strip()
            self.dump_layout(dev, unfiltered=' -i' in cmd, with_attrs=' -a' in cmd)
            data = self._files.get(dev) or b''
            return self._R(f'DumpLayout saved to:{dev}\n'
                           + data.decode('utf-8', 'replace'))
        if cmd.startswith('cat '):
            if cmd.strip() == 'cat /proc/loadavg':
                return self._R(self.loadavg)
            p = cmd[4:].strip()
            data = self._files.get(p)
            if data is None:
                return self._R('', 1, 'No such file')
            return self._R(data.decode('utf-8', 'replace'))
        # `uitest uiInput ...` 既可能经 hdc.swipe() 这类封装下发，也可能被上层
        # 直接拼成 shell 命令（DeviceGuard 的解锁手势就是后者）。真机上两种写法
        # 完全等价，这里也必须等价路由，否则「直接下发的那条路径」在模拟环境里
        # 会静默变成空操作 —— 又一类只有真机才暴露的假通过。
        if cmd.startswith('uitest uiInput'):
            # ★ 必须按 **POSIX shell 的规矩**分词（shlex），不能用 str.split()。
            # `hdc.Hdc.input_text()` 会用单引号把输入文本包成一个参数；
            # 这里若用朴素 split，引号会被当成文本的一部分写进 TYPED，
            # 两侧行为就此分叉 —— 「模拟环境绿、真机挂」的经典假通过。
            # shlex.split 对不带引号的命令与 str.split 等价，不影响其它动作。
            try:
                _parts = shlex.split(cmd)
            except ValueError:                      # 引号没配对等畸形命令
                _parts = cmd.split()
            if _parts:
                self._ui_input(*_parts[2:])
            return self._R('No Error')
        # ★ C8 备用通路**同构**：真机上写操作可能经 `uinput` / `sendevent`
        #   下发（`Hdc.detect_backend()` 选了备用通路时），模拟侧必须认这两种
        #   命令，否则备用通路在离线环境里静默变成空操作 —— 又一例
        #   「模拟器不保真 = 测试全绿反而危险」。
        if cmd.startswith('uinput') or cmd.startswith('sendevent'):
            # `uinput --help` 是 `Hdc._probe_backend('uinput')` 的**探测**命令，
            # 不是注入动作 —— 必须返回含 usage 的文本，模拟真机。
            if cmd.startswith('uinput') and '--help' in cmd.split():
                return self._R(_UINPUT_USAGE)
            self._apply_alt_input(cmd)
            return self._R('No Error')
        if 'uitest --version' in cmd:
            return self._R('uitest 1.0.0')
        if cmd.startswith('aa start'):
            self.stack.clear()
            self.current = 'login'
            self._log('aa start -> 重置到 login')
            return self._R('start ability successfully')
        if cmd.startswith('aa force-stop'):
            self._log('aa force-stop')
            return self._R('force stop successfully')
        if cmd.startswith('pidof'):
            # 注入了崩溃的 bundle 就查不到进程了 —— 真机上 `kill -11` 之后
            # `pidof` 确实返回空。这是「崩溃」的一条辅证据。
            bundle = cmd.split()[1] if len(cmd.split()) > 1 else ''
            if bundle in self.dead_bundles:
                return self._R('')
            return self._R('12345')
        if cmd.startswith('hidumper'):
            # ★ 走真机同格式的多屏输出（见 `_screen_dump()`）。
            #
            # 早期这里只回一句 `screen size: {w} x {h}`，与真机
            # `hidumper -s RenderService -a screen` 的输出**格式完全不同**。
            # 后果：`emulator_cli.parse_screen_info()` 在模拟环境里永远解析到
            # 空列表，跨形态 的折叠态判据（看 powerStatus 互换）根本测不了 ——
            # 又一类「只有上真机才暴露」的假通过。
            #
            # 仍保留对 `screen size` 这种老写法的兼容：真机上某些裁剪版本
            # 只有 DisplayManagerService，输出形如 `screen size: W x H`。
            if '--mem' in cmd:
                # 2C：真机 `hidumper --mem <pid>` 同构内存表 —— 表头含 Pss，
                # 两行 Total 开头（单位行无数字、数值行首个数字 = PSS 合计
                # KB）。解析规则见 ohauto/perf.py，两侧必须同构。
                return self._R(self._mem_dump())
            if 'RenderService' in cmd:
                return self._R(self._screen_dump())
            w, h = self.screen
            return self._R(f'screen size: {w} x {h}')
        if cmd.startswith('date'):
            # 设备时钟。默认等于本机，可显式设成别的值来模拟 RTC 不准。
            if '%s' in cmd:
                return self._R(str(int(self.device_time)))
            return self._R(time.strftime('%Y-%m-%dT%H:%M:%S',
                                         time.localtime(self.device_time)))
        if cmd.startswith('ls -l') or cmd.startswith('ls '):
            p = cmd.split()[-1]
            listing = self._ls_fault_dir(p)
            if listing is not None:
                return self._R(*listing)
            return self._R(f'-rw-r--r-- 1 root root 1024 {p}'
                           if p in self._files else '', 0)
        return self._R('')

    def run(self, args, **kw):
        self.calls.append('run:' + ' '.join(str(a) for a in args))
        if len(args) >= 2 and args[0] == 'shell':
            return self.shell(' '.join(str(a) for a in args[1:]))
        return self._R('')

    # ------------------------------------------------------ 操作注入

    def _ui_input(self, *parts) -> None:
        """`uitest uiInput ...` 的模拟入口（记录命令 + 执行动作）。"""
        cmd = 'uitest uiInput ' + ' '.join(str(p) for p in parts)
        self.calls.append(cmd)
        self._fault('ui_input', cmd)
        self._apply_input_action([str(p) for p in parts])

    def _apply_input_action(self, sp: List[str]) -> None:
        """写动作的**共同语义**（命中控件、切页、TYPED、解锁 …）。

        uitest 与备用通路（uinput / sendevent）都汇聚到这里：备用通路在
        模拟侧只是**命令形态**不同，动作语义必须与 uitest 逐字一致 ——
        否则会出现「备用通路下测试绿、真机挂」这类假通过。
        """
        kind = sp[0]

        if kind in ('click', 'doubleClick', 'longClick'):
            x, y = int(sp[1]), int(sp[2])
            node = self._hit(x, y)
            rec = {'action': kind, 'x': x, 'y': y,
                   'node': node.get('id') if node else None,
                   'page_before': self.current}
            if node is not None:
                nxt = self.transitions.get((self.current, node.get('id', '')))
                if nxt:
                    self.stack.append(self.current)
                    self.current = nxt
                    rec['page_after'] = nxt
            self.actions.append(rec)
            self._log(f'{kind} ({x},{y}) 命中 {rec["node"]} -> {self.current}')

        elif kind == 'inputText':
            x, y = int(sp[1]), int(sp[2])
            text = ' '.join(sp[3:])
            node = self._hit(x, y)
            TYPED[node.get('id') if node else f'{x},{y}'] = text
            self.actions.append({'action': 'inputText', 'x': x, 'y': y,
                                 'text': text,
                                 'node': node.get('id') if node else None})
            self._log(f'inputText ({x},{y}) = {text!r} -> {node.get("id") if node else "?"}')

        elif kind in ('swipe', 'fling', 'drag'):
            self.actions.append({'action': kind, 'args': sp[1:]})
            # 上滑手势会解锁：真机上锁屏页「上滑解锁」就是这个动作。
            # 模拟这一条，DeviceGuard.ensure_awake 才测得了。
            if self.locked:
                self.locked = False
                self._log('swipe 解锁（模拟上滑解锁）')
            self._log(f'{kind} {sp[1:]}')

        elif kind == 'dircFling':
            self.actions.append({'action': 'dircFling', 'direction': sp[1] if len(sp) > 1 else 0})

        elif kind == 'keyEvent':
            key = sp[1] if len(sp) > 1 else ''
            self.actions.append({'action': 'keyEvent', 'key': key})
            if key == 'Back':
                self.current = self.stack.pop() if self.stack else 'login'
            elif key == 'Home':
                self.stack.clear()
                self.current = 'login'
            self._log(f'keyEvent {key} -> {self.current}')

    # ---- C8：按当前通路派发写动作（与真机 `Hdc._backend` 派发同构）
    #
    # 默认 uitest —— 与改动前**逐字一致**（既有测试与调用方零迁移）。
    # `detect_backend(force='uinput')` 之后，同一个 `click()` 会下发 uinput
    # 命令并**走它自己的解析路径**，从而让「换通路、动作语义不变」这条性质
    # 在离线环境里也测得到。
    _sh_quote = staticmethod(Hdc._sh_quote)

    def _backend_object(self):
        """按当前通路取写动作执行器；uitest 时返回 None（走既有 `_ui_input` 直连）。"""
        if self._backend_name == 'uinput':
            return _UinputBackend(self)
        if self._backend_name == 'sendevent':
            w, h = self.screen_size
            return _SendeventBackend(self, '/dev/input/event5',
                                     {'x': (0, w - 1), 'y': (0, h - 1)})
        if self._backend_name == 'none':
            return _NullBackend('模拟器：没有可用的输入注入通路')
        return None

    def _require_uitest(self, action: str) -> None:
        """`fling / drag / dircFling` 是 uitest 专有扩展（与真机同纪律）。"""
        if self._backend_name != 'uitest':
            raise InputUnavailable(
                f'{action} 仅在 uitest 通路可用（当前 {self._backend_name}）')

    def _apply_alt_input(self, cmd: str) -> None:
        """C8 备用通路（uinput / sendevent）命令的解析 —— 与真机同构。

        只认**真机实测有效**的形态（`-d` / `-u` 分两次下发实测不生效，
        这里也照做「无动作」，不假装成功）：
            uinput -T -c x y                点击
            uinput -T -m fx fy tx ty        滑动
            uinput -T -m x y x y -k <ms>    长按（原地 + keep time，零位移）
            uinput -K -t <text>             文本（无坐标，落在最近一次点击处）
            uinput -K -l <code> <ms>        按键（OHOS KeyCode：1=Home 2=Back）
            sendevent <node> <t> <c> <v>    evdev 事件（可 `;` 串联多条）
        """
        try:
            tokens = shlex.split(cmd)
        except ValueError:                      # 引号没配对等畸形命令
            tokens = cmd.split()
        if not tokens:
            return
        if tokens[0] == 'sendevent':
            self._apply_sendevent_cmd(cmd)
        else:
            self._apply_uinput(tokens[1:])

    def _apply_uinput(self, args: List[str]) -> None:
        if not args:
            return
        mode, rest = args[0], args[1:]
        if mode == '-T' and rest:
            sub = rest[0]
            nums = [int(a) for a in rest[1:] if _is_int(a)]
            if sub == '-c' and len(nums) >= 2:
                self._last_tap = (nums[0], nums[1])
                self._apply_input_action(['click', nums[0], nums[1]])
            elif sub == '-m' and len(nums) >= 4:
                x1, y1, x2, y2 = nums[:4]
                if (x1, y1) == (x2, y2) and '-k' in rest:
                    # 原地 + keep time = 长按（`-T` 无独立长按命令）
                    self._last_tap = (x1, y1)
                    self._apply_input_action(['longClick', x1, y1])
                else:
                    self._apply_input_action(
                        ['swipe', x1, y1, x2, y2, 600])
            return
        if mode == '-K' and rest:
            sub = rest[0]
            if sub == '-t':
                x, y = self._last_tap or (0, 0)
                self._apply_input_action(['inputText', x, y, ' '.join(rest[1:])])
            elif sub == '-l' and len(rest) >= 2 and _is_int(rest[1]):
                key = _UINPUT_OHOS_KEYS.get(int(rest[1]))
                if key:
                    self._apply_input_action(['keyEvent', key])
        # `-d` / `-u`（按下/抬起分两次下发）真机上不生效 —— 落空，不假装成功。

    def _apply_sendevent_cmd(self, cmd: str) -> None:
        """把 `sendevent` 事件序列还原成点击 / 滑动。

        一次手势在真机上是 down → move* → up，`_SendeventBackend` 又把它们
        拆到**多次** hdc 往返 —— 所以按下的起点必须存在 `_se_state` 里跨调用
        保留，否则还原不出「点击 vs 滑动」。
        """
        st = self._se_state
        for group in cmd.split(';'):
            toks = group.split()
            if len(toks) < 5 or toks[0] != 'sendevent':
                continue
            if not (_is_int(toks[2]) and _is_int(toks[3]) and _is_int(toks[4])):
                continue
            typ, code, val = int(toks[2]), int(toks[3]), int(toks[4])
            if typ == 3:                        # EV_ABS
                if code == 0x35:                # ABS_MT_POSITION_X
                    st['x'] = val
                elif code == 0x36:              # ABS_MT_POSITION_Y
                    st['y'] = val
                if st.get('first') is not None:
                    st['moved'] = True
            elif typ == 1 and code == 0x14A:    # EV_KEY / BTN_TOUCH
                if val == 1:
                    st['first'] = (st.get('x', 0), st.get('y', 0))
                    st['moved'] = False
                else:
                    first, st['first'] = st.get('first'), None
                    if first is None:
                        continue
                    last = (st.get('x', 0), st.get('y', 0))
                    if st.get('moved') and last != first:
                        self._apply_input_action(
                            ['swipe', first[0], first[1], last[0], last[1], 600])
                    else:
                        self._last_tap = first
                        self._apply_input_action(['click', first[0], first[1]])

    def click(self, x, y):
        b = self._backend_object()
        if b is None:
            self._ui_input('click', x, y)
        else:
            b.click(x, y)

    def double_click(self, x, y):
        b = self._backend_object()
        if b is None:
            self._ui_input('doubleClick', x, y)
        else:
            b.double_click(x, y)

    def long_click(self, x, y):
        b = self._backend_object()
        if b is None:
            self._ui_input('longClick', x, y)
        else:
            b.long_click(x, y)

    # 参数名与 `Hdc` 逐一对齐（HdcLike 契约内的四个）—— 全仓都是位置调用，
    # 改名零风险；不一致会让「FakeHdc 可替换 Hdc」只对一半。
    def input_text(self, x, y, text):
        b = self._backend_object()
        if b is None:
            self._ui_input('inputText', x, y, text)
        else:
            b.input_text(x, y, text)

    def swipe(self, fx, fy, tx, ty, velocity=600):
        b = self._backend_object()
        if b is None:
            self._ui_input('swipe', fx, fy, tx, ty, velocity)
        else:
            b.swipe(fx, fy, tx, ty, velocity)

    def fling(self, fx, fy, tx, ty, v=600):
        self._require_uitest('fling')
        self._ui_input('fling', fx, fy, tx, ty, v)

    def drag(self, fx, fy, tx, ty, v=600):
        self._require_uitest('drag')
        self._ui_input('drag', fx, fy, tx, ty, v)

    def dirc_fling(self, direction, velocity=600):
        self._require_uitest('dircFling')
        self._ui_input('dircFling', direction, velocity)

    def key_event(self, *keys):
        b = self._backend_object()
        if b is None:
            self._ui_input('keyEvent', *keys)
        else:
            b.key_event(*keys)

    def back(self): self.key_event('Back')
    def home(self): self.key_event('Home')

    # ---- C8：输入通路探测（与真机 `Hdc.detect_backend` 同构）
    #
    # 结论照抄真机 DAYU200 的实测事实：uitest ✔ / uinput ✔ / sendevent ✘
    # （真机没有 `getevent`，读不到 evdev 轴范围 → sendevent 无法校准）。
    # 照抄事实、而不是「一律可用」，是为了让备用通路这条路径在离线环境里
    # 真的被走到，而不是被模拟器的乐观假设掩盖。
    BACKENDS = ('uitest', 'uinput', 'sendevent')

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def interactive(self) -> bool:
        return self._backend_name != 'none'

    def detect_backend(self, force=None, verbose=False) -> Dict[str, Any]:
        if force is not None and force not in self.BACKENDS:
            raise ValueError(
                f'force 必须是 {"/".join(self.BACKENDS)}，收到: {force!r}')
        names = list(self.BACKENDS) if force is None else [force]
        probes: List[Dict[str, Any]] = []
        selected: Optional[str] = None
        for name in names:
            ok, detail = self._probe_backend(name)
            probes.append({'name': name, 'ok': ok, 'detail': detail})
            if verbose:
                print(f"[sim] 输入通路 {name}: "
                      f"{'可用' if ok else '不可用'} —— {detail}")
            if ok and selected is None:
                selected = name
        self._backend_name = selected or 'none'
        return {'selected': self._backend_name,
                'interactive': self.interactive,
                'probes': probes}

    def _probe_backend(self, name: str) -> Tuple[bool, str]:
        """探测单个通路时**真的发命令**（与 `Hdc._probe_backend` 同法），
        不返回硬编码布尔值 —— 免得探测结论与 shell 分派失配。"""
        if name == 'uitest':
            v = self.uitest_version()
            return ((True, f'uitest {v}') if v
                    else (False, '设备不支持 uitest 命令行通路'))
        if name == 'uinput':
            out = self.shell('uinput --help').stdout or ''
            if 'usage' in out.lower():
                return True, 'uinput 存在且可执行'
            return False, (out.strip() or 'rc≠0')[:120]
        # sendevent 依赖 `getevent` 读 evdev 轴范围；模拟器与真机一致：没有
        parsed = _parse_axis_ranges(self.shell('getevent -p').stdout or '')
        if parsed is None:
            return False, 'getevent 缺失或未解析到轴范围，evdev 坐标无法校准'
        return True, parsed[0]

    # ------------------------------------------------------ 应用管理

    def install(self, hap):
        return self._R('install successfully')

    def uninstall(self, b):
        return self._R('uninstall successfully')

    def start_ability(self, bundle, ability='EntryAbility'):
        self.stack.clear()
        self.current = 'login'
        return self._R('start ability successfully')

    def force_stop(self, bundle):
        return self._R('force stop successfully')

    def is_running(self, bundle):
        return True

    def hilog(self, lines=200, grep=None):
        """模拟 `hdc shell "hilog -x -z <n>"`。

        未配置 `hilog_text` 时保持原有行为（一行占位文本），保证既有测试不变；
        配置了则返回配置内容（可取尾部 n 行，模拟 -z 的语义）。

        走 `_fault('hilog', ...)`，所以可以用
        `FaultPlan().fail_always('hilog')` 单独把 hilog 打挂，
        验证采集能不能在这一路失败时降级。
        """
        self._fault('hilog', f'hilog -x -z {lines}')
        if self.hilog_text is None:
            return f'[simulated hilog] page={self.current}'
        text = self.hilog_text
        if callable(text):
            text = text(self)
        rows = str(text).splitlines()
        if lines and lines > 0:
            rows = rows[-lines:]
        out = '\n'.join(rows)
        if grep:
            out = '\n'.join(l for l in out.splitlines() if re.search(grep, l))
        return out
