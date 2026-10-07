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
from dataclasses import dataclass
from typing import List, Optional, Sequence


# ---------------------------------------------------------------- 异常

class HdcError(RuntimeError):
    """hdc 命令执行失败。"""


class DeviceNotFound(HdcError):
    """没有找到可用的鸿蒙设备。"""


# ---------------------------------------------------------------- 结果

@dataclass
class ShellResult:
    returncode: int
    stdout: str
    stderr: str
    command: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __str__(self) -> str:
        return f'<ShellResult rc={self.returncode} cmd={self.command!r}>'


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
                with open(c, 'r', encoding='utf-8') as f:
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

    def run(
        self,
        args: Sequence[str],
        timeout: Optional[int] = None,
        check: bool = False,
        binary: bool = False,
        retries: int = 0,
    ):
        """执行一条 hdc 命令。

        Parameters
        ----------
        args:     传给 hdc 的参数（不含 hdc 本身）
        timeout:  超时秒数
        check:    为 True 时非零返回码抛 HdcError
        binary:   为 True 时返回 bytes（用于截图等二进制内容）
        retries:  失败重试次数（针对设备偶发超时）
        """
        cmd = self._base_cmd() + list(args)
        last_err = None

        for attempt in range(retries + 1):
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
                    res = ShellResult(proc.returncode, '<binary>', err, ' '.join(cmd))
                    res.raw = out  # type: ignore[attr-defined]
                else:
                    res = ShellResult(proc.returncode, out, err, ' '.join(cmd))

                if self.verbose:
                    print(f'[hdc] {" ".join(cmd)}  -> rc={proc.returncode}')

                if res.ok:
                    return res
                if not check and attempt >= retries:
                    # check=False：失败不抛错，但 **retries 仍要生效** ——
                    # 原实现在这里无条件 return，retries 参数形同虚设
                    # （2026-09-26 全项目评审 #10）。重试耗尽后返回最后一次
                    # 的结果，调用方按 rc 自行判断 —— 这才是 check=False 的本意。
                    return res
                last_err = HdcError(f'hdc 执行失败 rc={proc.returncode}: {err.strip()[:300]}')
            except subprocess.TimeoutExpired:
                last_err = HdcError(f'hdc 命令超时({timeout or self.timeout}s): {" ".join(cmd)}')
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

        ★ 2026-09-19 实测踩到的两个坑，都在这里修掉：

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
        raw = self.run(['shell', f'cat {device_path}'], binary=True,
                       timeout=self.timeout * 3, check=True)
        data = getattr(raw, 'raw', b'')
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

    def click(self, x: int, y: int) -> None:
        self._ui_input('click', int(x), int(y))

    def double_click(self, x: int, y: int) -> None:
        self._ui_input('doubleClick', int(x), int(y))

    def long_click(self, x: int, y: int) -> None:
        self._ui_input('longClick', int(x), int(y))

    def input_text(self, x: int, y: int, text: str) -> None:
        # 文本必须整体作为**一个**参数下到设备侧 shell —— 见 `_sh_quote` 的说明。
        # 含空格/引号/$ 的输入文本在 UI 测试里是高频场景，不是边角情况。
        quoted = self._sh_quote(text)
        if len(quoted) > 30000:
            # Windows 单条命令行上限 32767 字符，超限是 subprocess 直接炸
            # （红队实测 50KB 文本命中）。宁可响亮地失败——分段注入需要
            # 真机验证「追加语义」，没验证过的分支不做。
            raise HdcError(
                '输入文本过长（引号后 %d 字符 > 30000 上限）——请拆分用例步骤；'
                '自动分段注入需真机验证后开放' % len(quoted))
        self._ui_input('inputText', int(x), int(y), quoted)

    def swipe(self, fx: int, fy: int, tx: int, ty: int, velocity: int = 600) -> None:
        self._ui_input('swipe', int(fx), int(fy), int(tx), int(ty), velocity)

    def fling(self, fx: int, fy: int, tx: int, ty: int, velocity: int = 600) -> None:
        self._ui_input('fling', int(fx), int(fy), int(tx), int(ty), velocity)

    def drag(self, fx: int, fy: int, tx: int, ty: int, velocity: int = 600) -> None:
        self._ui_input('drag', int(fx), int(fy), int(tx), int(ty), velocity)

    def dirc_fling(self, direction: int, velocity: int = 600) -> None:
        """方向滑动：0=左 1=右 2=上 3=下"""
        if direction not in (0, 1, 2, 3):
            raise ValueError('direction 必须是 0(左)/1(右)/2(上)/3(下)')
        self._ui_input('dircFling', direction, velocity)

    def key_event(self, *keys) -> None:
        """实体按键：Home / Back / Power / KeyCode 数字。最多三个组合键。"""
        if len(keys) > 3:
            raise ValueError('最多支持三个按键组合')
        self._ui_input('keyEvent', *keys)

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
