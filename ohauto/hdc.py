"""
L1 执行层 —— hdc 命令行封装
============================

基于鸿蒙 UITest 的**命令行通路**（`hdc shell uitest ...`），无需测试 HAP，
整个能力层可完全运行在宿主 PC 上，设备侧零部署。

覆盖能力：
    截图        screenCap
    控件树      dumpLayout
    操作注入    uiInput click / doubleClick / longClick / inputText /
                swipe / fling / drag / dircFling / keyEvent
    应用管理    install / uninstall / aa start / aa force-stop
    日志        hilog
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import (Any, Dict, List, Optional, Protocol, Sequence, Tuple,
                    runtime_checkable)


# ---------------------------------------------------------------- 异常

class HdcError(RuntimeError):
    """hdc 命令执行失败。"""


class DeviceNotFound(HdcError):
    """没有找到可用的鸿蒙设备。"""


#: hdc 通道的**瞬时**失败特征 —— 重连 / 唤醒 / 刚 kill 过守护进程之后发命令常命中。
#: 实测（本机 DAYU200）：`[Fail][E000004]:The communication channel is being
#: established. Please wait for several seconds and try again` —— 同一条命令隔两秒
#: 重发就成功，而且这段文本会出现在 **stdout**（`cat` 的输出）里，不只是 stderr。
#: 这类失败重试是**正确处置**而不是掩盖问题：它不是"命令写错了"，是"通道还没建好"。
TRANSIENT_MARKERS = ('E000004', 'communication channel is being established',
                     'need connect-key')   # 同一场竞态的另一句台词，实测会单独出现


# ---------------------------------------------------------------- 结果

@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str
    command: str
    #: 二进制输出的原始字节，仅 `run(..., binary=True)` 时有意义
    #: （此时 `stdout` 是占位串 `<binary>`）。做成正式字段而不是事后
    #: 动态挂属性：`pull()` 的 `cat` 兜底要直接读它，属性是否存在不该
    #: 取决于走了哪条分支。`repr=False` 避免把整个文件塞进日志。
    raw: bytes = field(default=b'', repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __str__(self) -> str:
        return f'<ShellResult rc={self.returncode} cmd={self.command!r}>'


# ---------------------------------------------------------------- 接口契约

@runtime_checkable
class HdcLike(Protocol):
    """`Driver` 依赖的设备接口 —— **窄契约**。

    只声明 `Driver` 实际调用到的一面，不照抄 `Hdc` 的整个公共面：
    接口隔离，换实现的人只需要满足这 15 个方法与 1 个属性。真正驱动
    这层抽象的是 `Driver.refresh()` —— 它过去用 `getattr` 三元兜底去猜
    对面是 `Hdc` 还是 `FakeHdc`，契约写明之后兜底就没有存在理由了。

    两处**已知的口径差异**（有意保留，不是遗漏）：

    * `shell` 标注返回 `ShellResult`，但 `FakeHdc.shell` 回的是内部的
      轻量替身（只有 `returncode / stdout / stderr / ok`，没有 `command`
      与 `raw`）。`Driver` 只读 `.stdout`，实际用到的比标注还窄 ——
      本仓库没有类型检查器，不为它再拆一个 `ShellResultLike`。
    * `start_ability` / `force_stop` 的返回值 `Driver` 不接收。

    注意 `runtime_checkable` 的两条限制（本仓库 CI 为 3.13，本机 3.14，
    两者行为一致）：

    * 3.12 起 `isinstance()` **会**连数据成员 `tmp_dir` 一起查 —— 所以
      `FakeHdc` 必须真的有这个实例属性，光有 `DEVICE_TMP` 类属性过不了。
    * `issubclass()` 遇到数据成员直接抛 `TypeError`，检查请一律走
      `isinstance()`。声明里写了 `requires-python >= 3.9`，而 3.9~3.11 的
      `isinstance()` 会跳过数据成员；CI 不跑那几个版本，不为此加兼容。
    """
    tmp_dir: str

    def shell(self, cmd: str, **kw) -> ShellResult: ...
    def screen_cap(self, device_path: str = None) -> str: ...
    def dump_layout(self, device_path: str = None, unfiltered: bool = False,
                    with_attrs: bool = False) -> str: ...
    def pull(self, device_path: str, local_path: str,
             binary: bool = False) -> str: ...
    def click(self, x: int, y: int) -> None: ...
    def double_click(self, x: int, y: int) -> None: ...
    def long_click(self, x: int, y: int) -> None: ...
    def input_text(self, x: int, y: int, text: str) -> None: ...
    def swipe(self, fx: int, fy: int, tx: int, ty: int,
              velocity: int = 600) -> None: ...
    def dirc_fling(self, direction: int, velocity: int = 600) -> None: ...
    def key_event(self, *keys: int) -> None: ...
    def back(self) -> None: ...
    def home(self) -> None: ...
    def start_ability(self, bundle: str,
                      ability: str = 'EntryAbility') -> ShellResult: ...
    def force_stop(self, bundle: str) -> ShellResult: ...


# ---------------------------------------------------------------- 输入通路
#
# 写动作原先把 `uitest uiInput` 焊死在 `Hdc._ui_input` 上 —— 设备没有
# uitest 命令行通路时，引擎从「可用」直接掉到「零」，中间没有缓冲档。
# 这里把写动作收敛到一个窄契约 `InputBackend`，`Hdc` 持一个实例并派发；
# 探测顺序 uitest → uinput → sendevent，首个可用者胜出（`Hdc.detect_backend`）。

class InputUnavailable(HdcError):
    """输入注入通路不可用，或某个动作在该通路上无法表达。

    必须**响亮失败**：备用通路下「以为降级成功、其实没注入」是假绿，
    与 sim / vision 里已有的两条纪律同类。
    """


@runtime_checkable
class InputBackend(Protocol):
    """写动作注入通路 —— **窄契约**。

    只声明上层（`Driver`）真正会调的写动作；`fling / drag / dircFling`
    是 uitest 专有扩展，不进契约。契约纪律与 `HdcLike` 同法：
    删除契约内的方法必须让引用处报错。
    """
    name: str

    def click(self, x: int, y: int) -> None: ...
    def double_click(self, x: int, y: int) -> None: ...
    def long_click(self, x: int, y: int) -> None: ...
    def swipe(self, fx: int, fy: int, tx: int, ty: int,
              velocity: int = 600) -> None: ...
    def input_text(self, x: int, y: int, text: str) -> None: ...
    def key_event(self, *keys) -> None: ...


#: uinput `-K` 收的是 **OpenHarmony KeyCode**（不是 Linux keycode）：
#: 真机实测 `1` 触发 Home、`2` 触发 Back。**只登记实测过的键**，不猜。
_UINPUT_KEYCODES = {'Home': 1, 'Back': 2}


class _UitestBackend:
    """现状通路：`uitest uiInput ...`（读 + 写全套，首选）。"""

    name = 'uitest'

    def __init__(self, hdc: 'Hdc') -> None:
        self.hdc = hdc

    def _run(self, *parts) -> None:
        self.hdc.shell('uitest uiInput ' + ' '.join(str(p) for p in parts),
                       check=True)

    def click(self, x: int, y: int) -> None:
        self._run('click', int(x), int(y))

    def double_click(self, x: int, y: int) -> None:
        self._run('doubleClick', int(x), int(y))

    def long_click(self, x: int, y: int) -> None:
        self._run('longClick', int(x), int(y))

    def swipe(self, fx: int, fy: int, tx: int, ty: int,
              velocity: int = 600) -> None:
        self._run('swipe', int(fx), int(fy), int(tx), int(ty), velocity)

    def input_text(self, x: int, y: int, text: str) -> None:
        self._run('inputText', int(x), int(y), self.hdc._sh_quote(text))

    def key_event(self, *keys) -> None:
        self._run('keyEvent', *keys)


class _UinputBackend:
    """备选通路一：`uinput` 命令行注入。

    命令形态来自**真机实测**（DAYU200 / OpenHarmony，720x1280）：

        click  `uinput -T -c x y`
        long   `uinput -T -m x y x y -k <hold>`（原地 + keep time）
        swipe  `uinput -T -m fx fy tx ty`
        text   `uinput -K -t <text>`
        key    `uinput -K -l <keycode> <ms>`

    ⚠️ 实测坑：按键用 `-d` / `-u` 分两次下发**不生效**，只有 `-l`
    （press & hold，ms 允许 3000~15000）才被系统接纳 —— 于是按键走 `-l`，
    默认 3000ms（工具下限）。
    """

    name = 'uinput'
    KEY_HOLD_MS = 3000          # uinput -l 的允许下限（实测 3000~15000）
    LONG_PRESS_MS = 1200        # 长按保持时长（`-m ... -k <ms>` 的 keep time）

    def __init__(self, hdc: 'Hdc') -> None:
        self.hdc = hdc

    def _run(self, *parts) -> None:
        self.hdc.shell('uinput ' + ' '.join(str(p) for p in parts), check=True)

    def click(self, x: int, y: int) -> None:
        self._run('-T', '-c', int(x), int(y))

    def double_click(self, x: int, y: int) -> None:
        # uinput 触屏没有 double-click 子命令；双击=两次单击，语义等价。
        self.click(x, y)
        self.click(x, y)

    def long_click(self, x: int, y: int) -> None:
        # `-T --touch` 的文档命令只有 -d/-u/-i/-m/-c，**没有** -g —— uinput
        # 触屏无独立长按命令。长按 = 原地 `-m`（起点=终点，零位移）+ `-k` 保持
        # 按下（keep time）。真机实测：桌面图标长按弹出「打开/服务卡片/卡片中心/
        # 卸载」菜单；同参数下 `-g` 无回显（非 -T 命令），故不采用。
        self._run('-T', '-m', int(x), int(y), int(x), int(y),
                  '-k', self.LONG_PRESS_MS)

    def swipe(self, fx: int, fy: int, tx: int, ty: int,
              velocity: int = 600) -> None:
        # velocity 在 uinput 触屏上没有对应参数（平滑时间固定），收下不用。
        self._run('-T', '-m', int(fx), int(fy), int(tx), int(ty))

    def input_text(self, x: int, y: int, text: str) -> None:
        self._run('-K', '-t', self.hdc._sh_quote(text))

    def key_event(self, *keys) -> None:
        for k in keys:
            code = _UINPUT_KEYCODES.get(str(k))
            if code is None:
                raise InputUnavailable(
                    f'uinput 通路不认识按键 {k!r}：只登记了实测过的 '
                    f'{sorted(_UINPUT_KEYCODES)}；其它键请改用 uitest 通路')
            self._run('-K', '-l', code, self.KEY_HOLD_MS)


class _SendeventBackend:
    """备选通路二：`sendevent` 直接写 evdev 事件（最后一档）。

    ⚠️ 真机实测（DAYU200）：设备**没有 `getevent`**，拿不到 evdev 轴范围
    （ABS_MT_POSITION_X/Y 的 min/max），无法把像素坐标映射到绝对量 ——
    该设备上此通路判**不可用**，而不是猜一组硬编码范围（猜错的落点
    在产物里看不出来）。有 `getevent` 的设备由 `_parse_axis_ranges()`
    解析轴范围后可用。

    文本输入**刻意不做**：中文 / emoji 无法用 evdev 直接表达，
    调用即报 `InputUnavailable` 并给替代建议，绝不静默跳过。
    """

    name = 'sendevent'

    # Linux input 事件常量（内核稳定，非设备相关）
    EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
    SYN_REPORT = 0
    BTN_TOUCH = 0x14A
    ABS_MT_SLOT = 0x2F
    ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
    ABS_MT_TRACKING_ID = 0x39
    SLOT = 0                    # 单指恒定 slot 0（多指手势不在设计范围内）
    SWIPE_STEPS = 12            # 轨迹插值步数（够平滑，又不至于太多往返）

    def __init__(self, hdc: 'Hdc', node: str,
                 ranges: Dict[str, Tuple[int, int]]) -> None:
        self.hdc = hdc
        self.node = node
        self.ranges = ranges

    def _map(self, x: int, y: int) -> Tuple[int, int]:
        """像素坐标 → evdev 绝对量。映射范围**从设备读**（`getevent -p`）。"""
        (x0, x1), (y0, y1) = self.ranges['x'], self.ranges['y']
        w, h = self.hdc.screen_size()
        mx = x0 + int(round((x1 - x0) * x / max(w - 1, 1)))
        my = y0 + int(round((y1 - y0) * y / max(h - 1, 1)))
        return mx, my

    def _seq(self, *events) -> None:
        # 多条 sendevent 合到**一次** hdc 往返 —— 否则一次滑动要十几次往返。
        cmds = '; '.join(f'sendevent {self.node} {t} {c} {v}'
                         for t, c, v in events)
        self.hdc.shell(cmds, check=True)

    def _down(self, x: int, y: int) -> None:
        mx, my = self._map(x, y)
        self._seq((self.EV_ABS, self.ABS_MT_SLOT, self.SLOT),
                  (self.EV_ABS, self.ABS_MT_TRACKING_ID, 1),
                  (self.EV_ABS, self.ABS_MT_POSITION_X, mx),
                  (self.EV_ABS, self.ABS_MT_POSITION_Y, my),
                  (self.EV_KEY, self.BTN_TOUCH, 1),
                  (self.EV_SYN, self.SYN_REPORT, 0))

    def _move(self, x: int, y: int) -> None:
        mx, my = self._map(x, y)
        self._seq((self.EV_ABS, self.ABS_MT_POSITION_X, mx),
                  (self.EV_ABS, self.ABS_MT_POSITION_Y, my),
                  (self.EV_SYN, self.SYN_REPORT, 0))

    def _up(self) -> None:
        self._seq((self.EV_ABS, self.ABS_MT_SLOT, self.SLOT),
                  (self.EV_ABS, self.ABS_MT_TRACKING_ID, -1),
                  (self.EV_KEY, self.BTN_TOUCH, 0),
                  (self.EV_SYN, self.SYN_REPORT, 0))

    def click(self, x: int, y: int) -> None:
        self._down(x, y)
        self._up()

    def double_click(self, x: int, y: int) -> None:
        self.click(x, y)
        self.click(x, y)

    def long_click(self, x: int, y: int) -> None:
        self._down(x, y)
        time.sleep(0.8)
        self._up()

    def swipe(self, fx: int, fy: int, tx: int, ty: int,
              velocity: int = 600) -> None:
        self._down(fx, fy)
        for i in range(1, self.SWIPE_STEPS + 1):
            self._move(fx + (tx - fx) * i // self.SWIPE_STEPS,
                       fy + (ty - fy) * i // self.SWIPE_STEPS)
        self._up()

    def input_text(self, x: int, y: int, text: str) -> None:
        raise InputUnavailable(
            'sendevent 通路不支持文本输入：中文 / emoji 无法用 evdev 直接表达。'
            '请改用 uitest / uinput 通路，或把该步改为点选控件')

    def key_event(self, *keys) -> None:
        # 原始 evdev 走 Linux keycode（与 uinput 的 OHOS KeyCode 不同）。
        table = {'Home': 102, 'Back': 158, 'Power': 116}
        for k in keys:
            code = table.get(str(k))
            if code is None:
                raise InputUnavailable(
                    f'sendevent 通路不认识按键 {k!r}：只登记了 {sorted(table)}')
            self._seq((self.EV_KEY, code, 1), (self.EV_SYN, self.SYN_REPORT, 0),
                      (self.EV_KEY, code, 0), (self.EV_SYN, self.SYN_REPORT, 0))


class _NullBackend:
    """三者皆无：**不可交互**档（只剩只读观测 + aa 拉起）。

    任何写动作即时响亮失败 —— 「假装在跑」比报错危险得多。
    """

    name = 'none'

    def __init__(self, reason: str = '') -> None:
        self.reason = reason or '没有可用的输入注入通路'

    def _fail(self, action: str) -> None:
        raise InputUnavailable(f'{self.reason}（{action} 无法注入）')

    def click(self, x: int, y: int) -> None:
        self._fail('click')

    def double_click(self, x: int, y: int) -> None:
        self._fail('double_click')

    def long_click(self, x: int, y: int) -> None:
        self._fail('long_click')

    def swipe(self, fx: int, fy: int, tx: int, ty: int,
              velocity: int = 600) -> None:
        self._fail('swipe')

    def input_text(self, x: int, y: int, text: str) -> None:
        self._fail('input_text')

    def key_event(self, *keys) -> None:
        self._fail('key_event')


def _parse_axis_ranges(text: str) -> Optional[Tuple[str, Dict[str, Tuple[int, int]]]]:
    """从 `getevent -p` 输出里解析触摸屏节点与 X/Y 轴范围。

    返回 `(节点, {'x': (min, max), 'y': (min, max)})`；解析不到返回 None。
    真机 / 版本间输出可能略有出入，这里只认最稳的「add device N: /dev/input/eventN」
    + 「ABS_MT_POSITION_X ... min X max Y」两段。
    """
    node = None
    m = re.search(r'add device \d+:\s*(/dev/input/event\d+)', text)
    if m:
        node = m.group(1)
    ranges: Dict[str, Tuple[int, int]] = {}
    for axis, key in (('ABS_MT_POSITION_X', 'x'), ('ABS_MT_POSITION_Y', 'y')):
        am = re.search(rf'{axis}.*?min\s+(-?\d+),\s*max\s+(-?\d+)', text)
        if am:
            ranges[key] = (int(am.group(1)), int(am.group(2)))
    if node and 'x' in ranges and 'y' in ranges:
        return node, ranges
    return None


# ---------------------------------------------------------------- 主类

class Hdc:
    """hdc 客户端。

    Parameters
    ----------
    hdc_path:
        hdc 可执行文件路径。为 None 时按顺序自动查找：
        1. PATH 中的 hdc / hdc.exe
        2. 常见 DevEco Studio / SDK 安装位置
        3. 本项目的 CONFIG_NAMES 配置文件（hdc.config.json / .ohauto.json，
           从当前目录向上逐级查找，也查 ~/.ohauto/）
    target:
        设备序列号（多设备时必须指定）。为 None 时使用唯一在线设备。
    timeout:
        单条命令默认超时（秒）。
    tmp_dir:
        设备侧临时目录，用于存放截图与控件树。
    """

    DEFAULT_TIMEOUT = 30
    DEVICE_TMP = '/data/local/tmp'

    #: 「通道未就绪」这类瞬时失败额外允许的重试次数（见 TRANSIENT_MARKERS）。
    #: 与 `run(retries=...)` 分开：那个是调用方对**命令级失败**的要求。
    transient_retries = 2

    # 常见 hdc 位置（含通配展开）
    #
    # 顺序即优先级。前面几条命中就返回，所以**只在末尾追加**新位置 ——
    # 冒然插到前面会改变既有环境的解析结果（hdc 本身跨版本兼容，
    # 但换一个 binary 就换一套默认行为，不值得为此改变现状）。
    COMMON_PATHS = [
        r'C:\Users\{user}\ohos-sdk\*\extracted\toolchains\hdc.exe',
        r'C:\Users\{user}\ohos-sdk\extracted\toolchains\hdc.exe',
        r'C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe',
        r'C:\Users\{user}\AppData\Local\Huawei\Sdk\openharmony\*\toolchains\hdc.exe',
        r'C:\Users\{user}\AppData\Local\OpenHarmony\Sdk\*\toolchains\hdc.exe',
        # OpenHarmony 官方 release SDK 解压后的布局：
        #   <盘>\ohos-sdk\<apiVersion>\toolchains\hdc.exe
        # 本机也可能把 SDK 解压到任意盘的这个布局下，所以按盘符通配兜底
        # （具体在哪台机器的哪个盘，属于隐私项，走配置而不是写死）。
        r'D:\ohos-sdk\*\toolchains\hdc.exe',
        r'C:\ohos-sdk\*\toolchains\hdc.exe',
    ]

    # 配置文件候选位置（相对于当前工作目录向上查找，以及用户目录）
    CONFIG_NAMES = ('hdc.config.json', '.ohauto.json')

    def __init__(
        self,
        hdc_path: Optional[str] = None,
        target: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        tmp_dir: str = DEVICE_TMP,
        verbose: bool = False,
    ):
        self.hdc_path = hdc_path or self._locate()
        self.target = target
        self.timeout = timeout
        self.tmp_dir = tmp_dir
        self.verbose = verbose
        #: 输入注入通路。默认 uitest —— 与改动前行为**逐字一致**；
        #: 要启用备用通路请显式调 `detect_backend()`（doctor / 入口脚本会调）。
        self._backend: InputBackend = _UitestBackend(self)

        if not self.hdc_path:
            raise HdcError(
                '未找到 hdc 可执行文件。\n'
                '可选方案（都不需要改系统环境变量）：\n'
                '  1) 项目内配置文件：在项目根目录放 hdc.config.json，内容\n'
                '     {"hdc_path": "D:/xxx/toolchains/hdc.exe"}\n'
                '  2) 环境变量（仅当前进程）：设置 HDC_PATH\n'
                '  3) 代码里显式传参：Hdc(hdc_path=r"...\\hdc.exe")\n'
                '  4) 安装 DevEco Studio（自带 SDK 与 hdc）\n'
                '  5) 运行 tools/fetch_sdk.py 获取 OpenHarmony SDK'
            )

    # ------------------------------------------------------------ 查找

    @classmethod
    def _locate(cls) -> Optional[str]:
        """按优先级查找 hdc：环境变量 -> 配置文件 -> PATH -> 常见安装位置。

        刻意把 PATH 排在后面，且**不提供任何写 PATH 的代码** ——
        查不到就应该让用户显式指定，而不是去改用户的系统设置。
        """
        # 1) 环境变量（仅当前进程，不持久化）
        env = os.environ.get('HDC_PATH')
        if env and os.path.isfile(env):
            return env

        # 2) 项目 / 用户配置文件
        cfg = cls._from_config()
        if cfg:
            return cfg

        # 3) PATH
        for name in ('hdc', 'hdc.exe', 'hdc_std', 'hdc_std.exe'):
            p = shutil.which(name)
            if p:
                return p

        # 4) 常见安装位置
        import glob
        user = os.environ.get('USERNAME', '')
        for pat in cls.COMMON_PATHS:
            for hit in glob.glob(pat.format(user=user)):
                if os.path.isfile(hit):
                    return hit
        return None

    @classmethod
    def _from_config(cls) -> Optional[str]:
        """从配置文件读取 hdc 路径。只读，不写。"""
        import json as _json
        cands = []

        # 从当前工作目录逐级向上找
        cur = os.path.abspath(os.getcwd())
        for _ in range(6):
            for name in cls.CONFIG_NAMES:
                cands.append(os.path.join(cur, name))
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

        # 用户级配置
        home = os.path.expanduser('~')
        for name in cls.CONFIG_NAMES:
            cands.append(os.path.join(home, '.ohauto', name))

        for c in cands:
            if not os.path.isfile(c):
                continue
            try:
                # ⚠️ 必须用 utf-8-sig：PowerShell 5.1 的
                #    `Set-Content -Encoding UTF8` 会写 BOM，而带 BOM 的 JSON
                #    用 `utf-8` 读会抛 JSONDecodeError。它被下面的 except 吞掉后
                #    本函数静默返回 None，`_locate()` 就落到「常见安装位置」——
                #    结果是**换了另一个 hdc 二进制而没有任何提示**（实测踩到）。
                with open(c, 'r', encoding='utf-8-sig') as f:
                    data = _json.load(f)
                p = data.get('hdc_path') or data.get('hdc')
                if p and os.path.isfile(p):
                    return p
            except (OSError, ValueError):
                continue
        return None

    # ------------------------------------------------------------ 执行

    def _base_cmd(self) -> List[str]:
        cmd = [self.hdc_path]
        if self.target:
            cmd += ['-t', self.target]
        return cmd

    @staticmethod
    def is_transient_failure(res: 'ShellResult') -> bool:
        """是不是「通道还没建好」这类瞬时失败。

        查 stdout **和** stderr —— 实测 `cat` 把 `[Fail][E000004]...` 打在 stdout 上，
        只看 stderr 会漏。
        """
        blob = f'{res.stdout}\n{res.stderr}'
        low = blob.lower()
        return any(m.lower() in low for m in TRANSIENT_MARKERS)

    def run(
        self,
        args: Sequence[str],
        timeout: Optional[int] = None,
        check: bool = False,
        binary: bool = False,
        retries: int = 0,
        transient: bool = True,
    ):
        """执行一条 hdc 命令。

        Parameters
        ----------
        args:     传给 hdc 的参数（不含 hdc 本身）
        timeout:  超时秒数
        check:    为 True 时非零返回码抛 HdcError
        binary:   为 True 时返回 bytes（用于截图等二进制内容）
        retries:  失败重试次数（针对设备偶发超时）
        transient:
            为 True（默认）时，「通道未就绪」这类瞬时失败会**额外**重试
            `transient_retries` 次。**判据按文本而不是返回码** —— 实测 hdc 在
            通道未就绪时会把 `[Fail][E000004]...` 打在 stdout 上、返回码仍是 0，
            只看 rc 会漏。要拿 stdout 当普通文本处理的场景（如 grep 设备日志里
            恰好含这几个字）可以传 False 关掉。
        """
        cmd = self._base_cmd() + list(args)
        last_err = None
        #: 循环上限 = 调用方要的重试次数 + 「通道未就绪」的额外预算。
        #: 两者分开算：**普通失败**到 `retries` 就停（不空转），
        #: **瞬时失败**额外允许 `transient_retries` 次（通道建好就好）。
        budget = retries + self.transient_retries

        for attempt in range(budget + 1):
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout or self.timeout,
                    shell=False,
                )
                out = proc.stdout if binary else proc.stdout.decode('utf-8', 'replace')
                err = proc.stderr.decode('utf-8', 'replace')
                if binary:
                    res = ShellResult(proc.returncode, '<binary>', err,
                                      ' '.join(cmd), raw=out)
                else:
                    res = ShellResult(proc.returncode, out, err, ' '.join(cmd))

                if self.verbose:
                    print(f'[hdc] {" ".join(cmd)}  -> rc={proc.returncode}')

                # 通道未就绪：**check 是真是假、rc 是不是 0 都要重试** ——
                # 它是环境状态不是命令缺陷，重发就好（实测隔两秒即成功）。
                # ⚠️ 这一条必须放在 `if res.ok` **之前**：实测 rc=0 也会带 Fail 文本。
                if (transient and attempt < self.transient_retries
                        and self.is_transient_failure(res)):
                    if self.verbose:
                        print(f'[hdc] 通道未就绪，重试 {attempt + 1}/'
                              f'{self.transient_retries}')
                    time.sleep(0.8 * (attempt + 1))
                    continue
                if res.ok:
                    return res
                if not check and attempt >= retries:
                    # check=False：失败不抛错，但 **retries 仍要生效** ——
                    # 原实现在这里无条件 return，retries 参数形同虚设
                    # （全项目评审 #10）。重试耗尽后返回最后一次
                    # 的结果，调用方按 rc 自行判断 —— 这才是 check=False 的本意。
                    return res
                last_err = HdcError(f'hdc 执行失败 rc={proc.returncode}: {err.strip()[:300]}')
                if attempt >= retries:
                    break                     # 普通预算用完，别空转
            except subprocess.TimeoutExpired:
                last_err = HdcError(f'hdc 命令超时({timeout or self.timeout}s): {" ".join(cmd)}')
                if attempt >= retries:
                    break                     # 超时很贵，不因瞬时预算多等几轮
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))

        raise last_err  # type: ignore[misc]

    def shell(self, cmd: str, **kw) -> ShellResult:
        """执行设备侧 shell 命令：hdc shell <cmd>"""
        return self.run(['shell', cmd], **kw)

    # ------------------------------------------------------------ 设备

    def list_targets(self) -> List[str]:
        """列出在线设备。返回 [''] 表示有一个未命名设备。"""
        res = self.run(['list', 'targets'], check=True)
        out = res.stdout.strip()
        if not out or 'Empty' in out:
            return []
        return [ln.strip() for ln in out.splitlines()
                if ln.strip() and not ln.startswith('[')]

    def wait_device(self, timeout: int = 60, interval: float = 2.0) -> str:
        """等待设备上线，返回设备标识。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            targets = self.list_targets()
            if targets:
                self.target = targets[0] or None
                return self.target or '<default>'
            time.sleep(interval)
        raise DeviceNotFound(f'{timeout}s 内未检测到设备。请确认：\n'
                             '  1. 真机已用 USB 连接\n'
                             '  2. 已在设备上开启「开发者模式 + USB 调试」\n'
                             '  3. 设备上已授权本机调试请求')

    def version(self) -> str:
        return self.run(['-v'], check=True).stdout.strip()

    def uitest_version(self) -> Optional[str]:
        """检测设备是否支持 uitest 命令行通路 —— 决定技术路线。

        **判据是返回码，不是"有没有输出"**：设备上没有 uitest 时 rc≠0，
        stderr 里是一句错误文本。老实现 `stdout or stderr` 会把那句错误文本
        当成"版本号"返回，`examples/smoke_test.py` 的 `if uv:` 于是打印
        「[通过] no uitest」并宣称技术路线可用 —— 那是假绿，
        比报错更难发现。查不到就返回 None。
        """
        try:
            res = self.shell('uitest --version', timeout=15)
        except HdcError:
            return None
        if not res.ok:
            return None
        return res.stdout.strip() or None

    # ------------------------------------------------------------ 输入通路探测

    @property
    def backend_name(self) -> str:
        """当前生效的输入通路名：uitest / uinput / sendevent / none。

        默认是 uitest（未探测）。`detect_backend()` 之后才反映真实可用通路。
        """
        return self._backend.name

    @property
    def interactive(self) -> bool:
        """能否做写操作。未探测时恒为 True（默认 uitest）；探测后才可信。"""
        return self._backend.name != 'none'

    def screen_size(self) -> Tuple[int, int]:
        """设备主屏像素尺寸 (w, h)。解析 hidumper 的 RenderService 输出。

        sendevent 的 evdev 轴映射需要它（像素 → 绝对量）。真机实测输出同时
        含 `render size:` 与 `activeMode:`；老裁剪版本可能只有
        `screen size: W x H`，一并兜住。
        """
        out = self.shell('hidumper -s RenderService -a screen',
                         timeout=20).stdout
        for pat in (r'activeMode:\s*(\d+)x(\d+)',
                    r'render size:\s*(\d+)x(\d+)',
                    r'screen size:\s*(\d+)\s*x\s*(\d+)'):
            m = re.search(pat, out)
            if m:
                return int(m.group(1)), int(m.group(2))
        raise HdcError('无法从 hidumper 解析屏幕尺寸（sendevent 需要它做轴映射）')

    def detect_backend(self, force: Optional[str] = None,
                       verbose: bool = False) -> Dict[str, Any]:
        """探测可用输入通路并设为当前通路，返回探测报告。

        顺序 uitest → uinput → sendevent，首个可用者胜出。`force` 指定
        'uitest'/'uinput'/'sendevent' 时只探该通路（用于验证备用通路，等价于
        「强制禁用 uitest」）。三者皆无 → `_NullBackend`（不可交互档）。

        判据看**返回码 + 输出不是错误文本**，不看「有没有输出」——
        与 `uitest_version()` 同一纪律。这是**非侵入式**探测（不真去点屏幕），
        完整的「实际效果」由真机执行时验证。
        """
        if force is not None and force not in ('uitest', 'uinput', 'sendevent'):
            raise ValueError(
                f'force 必须是 uitest/uinput/sendevent，收到: {force!r}')
        names = ['uitest', 'uinput', 'sendevent'] if force is None else [force]
        probes: List[Dict[str, Any]] = []
        selected: Optional[InputBackend] = None
        for name in names:
            ok, detail, backend = self._probe_backend(name)
            probes.append({'name': name, 'ok': ok, 'detail': detail})
            if verbose:
                print(f"[hdc] 输入通路 {name}: "
                      f"{'可用' if ok else '不可用'} —— {detail}")
            if ok and selected is None:
                selected = backend
        if selected is None:
            reasons = '；'.join(f'{p["name"]}: {p["detail"]}' for p in probes)
            selected = _NullBackend(f'没有可用的输入注入通路（{reasons}）')
        self._backend = selected
        return {'selected': selected.name, 'interactive': self.interactive,
                'probes': probes}

    def _probe_backend(self, name: str):
        """探测单个通路，返回 (是否可用, 说明, backend 实例或 None)。"""
        try:
            if name == 'uitest':
                ver = self.uitest_version()
                if ver:
                    return True, f'uitest {ver}', _UitestBackend(self)
                return False, '设备不支持 uitest 命令行通路', None
            if name == 'uinput':
                res = self.shell('uinput --help', timeout=15)
                out = f'{res.stdout}\n{res.stderr}'
                if res.ok and 'usage' in out.lower():
                    return True, 'uinput 存在且可执行', _UinputBackend(self)
                return False, (out.strip() or 'rc≠0')[:120], None
            # sendevent：必须能读到 evdev 轴范围（靠 getevent）才算可用
            res = self.shell('getevent -p', timeout=20)
            parsed = _parse_axis_ranges(f'{res.stdout}\n{res.stderr}')
            if parsed is None:
                return False, 'getevent 缺失或未解析到轴范围，evdev 坐标无法校准', None
            node, ranges = parsed
            detail = f'{node} x{ranges["x"]} y{ranges["y"]}'
            return True, detail, _SendeventBackend(self, node, ranges)
        except HdcError as e:
            return False, f'{type(e).__name__}: {e}', None

    # ------------------------------------------------------------ 截图

    def screen_cap(self, device_path: str = None) -> str:
        """设备侧截图，返回设备上的文件路径。"""
        device_path = device_path or f'{self.tmp_dir}/ohauto_shot.png'
        self.shell(f'uitest screenCap -p {device_path}', check=True)
        return device_path

    # ------------------------------------------------------------ 控件树

    def dump_layout(self, device_path: str = None, unfiltered: bool = False,
                    with_attrs: bool = False) -> str:
        """导出控件树到设备侧文件，返回该路径。

        unfiltered: -i 不过滤不可见控件
        with_attrs: -a 附带 BackgroundColor / Content / FontSize 等属性
        """
        device_path = device_path or f'{self.tmp_dir}/ohauto_layout.json'
        flags = ''
        if unfiltered:
            flags += ' -i'
        if with_attrs:
            flags += ' -a'
        self.shell(f'uitest dumpLayout{flags} -p {device_path}', check=True)
        return device_path

    # ------------------------------------------------------------ 文件传输

    def pull(self, device_path: str, local_path: str,
             binary: bool = False) -> str:
        """从设备拉取文件到本地。

        ★ 实测踩到的两个坑，都在这里修掉：

        **坑 1：传正斜杠盘符路径会被当成相对路径。**
        `D:/foo/x.png` 会被 hdc 拼到 cwd 后面变成
        `D:\\cwd\\D:/foo/x.png` → 报 no such file。
        **hdc 只认反斜杠形式的盘符**（`D:\\`），所以必须用
        `os.path.abspath()` 规范化。
        （原实现只对 `makedirs` 做了规范化，**传给 hdc 的却是原始路径**。）

        **坑 2：`file recv` 失败时返回码仍然是 0。**
        所以判据不能只看 `returncode`，必须检查文件**真的存在且有内容**。

        Parameters
        ----------
        binary:
            目标是否为二进制文件。为 True 时**禁用 `cat` 兜底** ——
            实测 `hdc shell cat` 会对二进制做 CRLF 转换
            （PNG 头 `\\x89PNG\\r\\n` 被写成 `\\x89PNG\\r\\r\\n`），
            产出**损坏但不报错**的文件，比直接失败更危险。
        """
        abs_local = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(abs_local), exist_ok=True)
        # ★ 先删残留：否则下一步的「文件存在」可能来自上次运行，掩盖本次失败
        if os.path.exists(abs_local):
            os.remove(abs_local)

        res = self.run(['file', 'recv', device_path, abs_local],
                       timeout=self.timeout * 3)
        if os.path.exists(abs_local) and os.path.getsize(abs_local) > 0:
            return abs_local

        if binary:
            raise HdcError(
                f'拉取失败（二进制，不用 cat 兜底以免产生损坏文件）: '
                f'{device_path} -> {abs_local}\n'
                f'rc={res.returncode} stdout={res.stdout.strip()[:200]}')

        # 兜底：部分版本 file recv 不稳，用 shell cat（仅文本安全）
        cat = self.run(['shell', f'cat {device_path}'], binary=True,
                       timeout=self.timeout * 3, check=True)
        data = cat.raw
        if not data:
            raise HdcError(
                f'拉取失败: {device_path} -> {abs_local}\n'
                f'rc={res.returncode} stdout={res.stdout.strip()[:300]}')
        with open(abs_local, 'wb') as f:
            f.write(data)
        return abs_local

    def push(self, local_path: str, device_path: str) -> None:
        """推送文件到设备。"""
        self.run(['file', 'send', local_path, device_path], check=True)

    # ------------------------------------------------------------ 操作注入

    def _ui_input(self, *parts) -> None:
        self.shell('uitest uiInput ' + ' '.join(str(p) for p in parts), check=True)

    @staticmethod
    def _sh_quote(s) -> str:
        """POSIX shell 单引号包裹 —— 让整段文本被设备侧 shell 当成**一个**参数。

        为什么必须加这一层：`hdc shell` 过来的命令在设备侧会被 shell **重新分词**，
        含空格的文本会被当场拆成多个参数。

        为什么用单引号而不是双引号：单引号内 `$`、反引号、`"`、`\\`、空格全部退化成
        字面字符，不需要逐个转义 —— 而 UI 测试里输入 `$100`、路径、JSON 片断这类
        文本是常态，逐个转义既容易漏、也容易转义错。
        单引号内**只有单引号本身**需要特殊处理：先结束引号，插一个转义的单引号，再重开。

        ⚠️ 配套：`ohauto/sim.py` 解析命令时必须用 `shlex.split`（而不是 `str.split`），
        否则模拟环境会把引号当成文本的一部分，两侧行为就此分叉。
        """
        return "'" + str(s).replace("'", "'\\''") + "'"

    def _require_uitest(self, action: str) -> None:
        """`fling / drag / dircFling` 是 uitest 专有扩展 —— 备用通路直接报错。"""
        if self._backend.name != 'uitest':
            raise InputUnavailable(
                f'{action} 仅在 uitest 通路可用（当前 {self._backend.name}）；'
                f'请改用 swipe(direction, scale) 或换回 uitest 通路')

    def click(self, x: int, y: int) -> None:
        self._backend.click(int(x), int(y))

    def double_click(self, x: int, y: int) -> None:
        self._backend.double_click(int(x), int(y))

    def long_click(self, x: int, y: int) -> None:
        self._backend.long_click(int(x), int(y))

    def input_text(self, x: int, y: int, text: str) -> None:
        # 文本必须整体作为**一个**参数下到设备侧 shell —— 见 `_sh_quote` 的说明。
        # 含空格/引号/$ 的输入文本在 UI 测试里是高频场景，不是边角情况。
        # 长度闸与通路无关（Windows 单条命令行上限对三种通路都成立），留在这里。
        quoted = self._sh_quote(text)
        if len(quoted) > 30000:
            # Windows 单条命令行上限 32767 字符，超限是 subprocess 直接炸
            # （红队实测 50KB 文本命中）。宁可响亮地失败——分段注入需要
            # 真机验证「追加语义」，没验证过的分支不做。
            raise HdcError(
                '输入文本过长（引号后 %d 字符 > 30000 上限）——请拆分用例步骤；'
                '自动分段注入需真机验证后开放' % len(quoted))
        self._backend.input_text(int(x), int(y), text)

    def swipe(self, fx: int, fy: int, tx: int, ty: int, velocity: int = 600) -> None:
        self._backend.swipe(int(fx), int(fy), int(tx), int(ty), velocity)

    def fling(self, fx: int, fy: int, tx: int, ty: int, velocity: int = 600) -> None:
        self._require_uitest('fling')
        self._ui_input('fling', int(fx), int(fy), int(tx), int(ty), velocity)

    def drag(self, fx: int, fy: int, tx: int, ty: int, velocity: int = 600) -> None:
        self._require_uitest('drag')
        self._ui_input('drag', int(fx), int(fy), int(tx), int(ty), velocity)

    def dirc_fling(self, direction: int, velocity: int = 600) -> None:
        """方向滑动：0=左 1=右 2=上 3=下"""
        if direction not in (0, 1, 2, 3):
            raise ValueError('direction 必须是 0(左)/1(右)/2(上)/3(下)')
        self._require_uitest('dircFling')
        self._ui_input('dircFling', direction, velocity)

    def key_event(self, *keys) -> None:
        """实体按键：Home / Back / Power / KeyCode 数字。最多三个组合键。"""
        if len(keys) > 3:
            raise ValueError('最多支持三个按键组合')
        self._backend.key_event(*keys)

    def back(self) -> None:
        self.key_event('Back')

    def home(self) -> None:
        self.key_event('Home')

    # ------------------------------------------------------------ 应用管理

    def install(self, hap_path: str) -> ShellResult:
        return self.run(['install', '-r', hap_path], timeout=self.timeout * 6, check=True)

    def uninstall(self, bundle: str) -> ShellResult:
        return self.run(['uninstall', bundle], timeout=self.timeout * 3)

    def start_ability(self, bundle: str, ability: str = 'EntryAbility') -> ShellResult:
        """拉起应用。等价于 aa start -b <bundle> -a <ability>"""
        return self.shell(f'aa start -b {bundle} -a {ability}', check=True)

    def force_stop(self, bundle: str) -> ShellResult:
        return self.shell(f'aa force-stop {bundle}')

    def is_running(self, bundle: str) -> bool:
        res = self.shell(f'pidof {bundle}')
        return bool(res.stdout.strip())

    # ------------------------------------------------------------ 日志

    def hilog(self, lines: int = 200, grep: Optional[str] = None) -> str:
        res = self.shell(f'hilog -x -z {lines}', timeout=self.timeout * 2)
        text = res.stdout
        if grep:
            text = '\n'.join(l for l in text.splitlines() if re.search(grep, l))
        return text
