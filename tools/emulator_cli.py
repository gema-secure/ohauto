"""模拟器命令行封装 —— 不依赖 DevEco GUI，全部走 Emulator.exe。

**为什么要有这个**：之前的判断是「建模拟器必须点 GUI」。实测证明这是错的 ——
`tools/emulator/Emulator.exe` 有完整的命令行接口，从接受协议、下载镜像、
建实例到启动、切折叠态、抓 UI 布局，全部可以脚本化。

    C:\\Program Files\\Huawei\\DevEco Studio\\tools\\emulator\\Emulator.exe

本模块把常用操作封装成函数 + 一个 CLI，方便 跨形态测试调用。

## 实测确认的能力（2026-09-18，Emulator 26.0.0.400）

| 能力 | 命令 |
|---|---|
| 自动接受协议 | `-license accept` |
| 列可用镜像 | `-imageList [-downloaded true]` |
| 下载镜像 | `-install -deviceType <t> -osVersion <v>` |
| 列已有实例 | `-list [-details]` |
| 建实例 | `-create <name> -deviceType <t> -osVersion <v> [-screenProfile <m> -screen <w h dpi size>]` |
| 删实例 | `-delete <name> [-force]` |
| 启动 | `-start <name> [-noWindow] [-bootMode <m>] [-hdcPort <p>]` |
| 停止 | `-stop <name>` |
| **切折叠态** | `-instance <name> -foldedState <state>` |
| **抓 UI 布局** | `-instance <name> -uiLayout [-i] [-a]` |
| 截图 | `-instance <name> -screenshot [-screenshotPath <p>]` |
| 点击/滑动/填字 | `-instance <name> -click/-slide/-fill` |
| 旋转/电源/音量/摇晃 | `-instance <name> -rotation/-power/-volume/-shake` |
| 电量/GPS/传感器 | `-instance <name> -battery/-gps/-sensor` |

## 折叠态取值（实测自 `-help`）

- **FoldableFold / Pura X Max**：`open` / `half-open` / `close`
- **WideFold（Pura X Max 除外）**：`open` / `close`
- **2in1FoldableFold**：`open` / `vertical-open` / `half-open` / `close`
- **TripleFold**：`single` / `double` / `triple`，外加 6 种左右半折组合：
  `left-folded-right-half-folded` / `left-half-folded-right-expanded` /
  `left-expanded-right-folded` / `left-half-folded-right-folded` /
  `left-expanded-right-half-folded` / `left-half-folded-right-half-folded`

> ⚠️ **`-foldedState left-folded-right-half-folded` 这种三折叠的组合态是
> 我们原以为不存在的** —— 三折叠不只是「3 种形态」，而是 9 种。
> 这是 本项可以做深的一个差异维度。

## 用法

    python tools/emulator_cli.py status
    python tools/emulator_cli.py images
    python tools/emulator_cli.py profiles
    python tools/emulator_cli.py accept          # ⚠️ 需你本人授权（法律协议）
    python tools/emulator_cli.py install phone
    python tools/emulator_cli.py list
    python tools/emulator_cli.py create --name MyTablet --type tablet
    python tools/emulator_cli.py start "Mate X7"
    python tools/emulator_cli.py fold "Mate X7" half-open
    python tools/emulator_cli.py uilayout "Mate X7"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

#: Emulator.exe 的常见位置（随 DevEco 安装位置而异）
EMULATOR_PATHS = (
    r'C:\Program Files\Huawei\DevEco Studio\tools\emulator\Emulator.exe',
    r'C:\Program Files (x86)\Huawei\DevEco Studio\tools\emulator\Emulator.exe',
    os.path.expandvars(
        r'%LOCALAPPDATA%\Huawei\DevEcoStudio\tools\emulator\Emulator.exe'),
)

#: hdc.exe 的常见位置（DevEco 自带 SDK 里）
#: ⚠️ raw 字符串不能以反斜杠结尾，所以拼路径时分段要避开行尾的 '\'
HDC_PATHS = (
    os.path.join(r'C:\Program Files\Huawei\DevEco Studio', 'sdk', 'default',
                 'openharmony', 'toolchains', 'hdc.exe'),
    os.path.join(r'C:\Program Files (x86)\Huawei\DevEco Studio', 'sdk',
                 'default', 'openharmony', 'toolchains', 'hdc.exe'),
    os.path.expandvars(
        r'%LOCALAPPDATA%\Huawei\Sdk\default\openharmony\toolchains\hdc.exe'),
)

#: 默认超时。`-install` 要下几百 MB，另给大超时。
DEFAULT_TIMEOUT = 120
INSTALL_TIMEOUT = 3600


def find_emulator(explicit: Optional[str] = None) -> Optional[str]:
    """找 Emulator.exe。找不到返回 None（不猜）。"""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for p in EMULATOR_PATHS:
        if os.path.exists(p):
            return p
    return None


def run(args: List[str], exe: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        stdin: Optional[str] = None) -> Dict[str, Any]:
    """跑一条 Emulator 命令，返回 {rc, stdout, stderr, argv}。

    **不抛异常** —— 调用方自己判断 rc，因为模拟器的报错方式不统一
    （有的写 stderr，有的写 stdout 且 rc=0）。

    Parameters
    ----------
    stdin
        给子进程喂的输入。**模拟器有些命令会交互式追问**（如
        `-install` 会问「是否同意协议 (y/N)」），不喂输入就会拿到 EOF
        然后静默中止 —— 表现为「下载没开始但也不报错」。
        传 `'y\\n'` 即可自动应答。默认 None（不喂，stdin 直接 EOF）。
    """
    emu = find_emulator(exe)
    if not emu:
        return {'rc': -1, 'stdout': '', 'stderr': 'Emulator.exe 未找到',
                'argv': []}
    argv = [emu] + args
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           input=stdin.encode('utf-8') if stdin else None)
        # 控制台代码页是 GBK，用 errors='replace' 防止解码炸掉
        out = p.stdout.decode('utf-8', errors='replace')
        err = p.stderr.decode('utf-8', errors='replace')
        return {'rc': p.returncode, 'stdout': out, 'stderr': err,
                'argv': argv}
    except subprocess.TimeoutExpired:
        return {'rc': -2, 'stdout': '', 'stderr': f'超时 {timeout}s',
                'argv': argv}
    except Exception as e:                                  # noqa: BLE001
        return {'rc': -3, 'stdout': '', 'stderr': f'{type(e).__name__}: {e}',
                'argv': argv}


def _json_from(text: str) -> Optional[Any]:
    """从混合输出里抠出 JSON 数组/对象。抠不出返回 None。"""
    for open_c, close_c in (('[', ']'), ('{', '}')):
        i, j = text.find(open_c), text.rfind(close_c)
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:                               # noqa: BLE001
                continue
    return None


# ------------------------------------------------------------------ 查询

def version(exe: Optional[str] = None) -> Optional[str]:
    r = run(['-version'], exe, timeout=30)
    out = (r['stdout'] + r['stderr']).strip()
    for line in out.splitlines():
        if 'Emulator' in line:
            return line.split(':', 1)[-1].strip()
    return out or None


#: `.emu_config` 里**不是协议**的键 —— 它们是路径设置，值是路径。
#: 不排除掉的话会被 `== 'agree'` 判成「未接受」，导致误报。
_CONFIG_NON_LICENSE_KEYS = frozenset({'imagePath', 'emuPath'})


def license_status() -> Dict[str, bool]:
    """读协议接受状态（直接读配置文件，不用交互式命令）。

    `.emu_config` 是 `key:value` 混合文件，既存协议状态也存路径配置：

        HarmonyOS_Software_Service_Agreement:agree   ← 协议
        HarmonyOS_SDK_Agreement:disagree             ← 协议
        imagePath:C:\\Users\\...\\Sdk                 ← 路径，不是协议
        emuPath:C:\\Users\\...\\deployed              ← 路径，不是协议

    只返回**协议**的键。路径键被过滤掉（早期版本会把它们当成
    「未接受的协议」误报，见 `_CONFIG_NON_LICENSE_KEYS`）。
    """
    cfg = os.path.expandvars(r'%LOCALAPPDATA%\Huawei\Emulator26.0\.emu_config')
    res: Dict[str, bool] = {}
    if not os.path.exists(cfg):
        return res
    try:
        with open(cfg, encoding='utf-8', errors='replace') as f:
            for line in f:
                if ':' not in line:
                    continue
                k, v = line.split(':', 1)
                k = k.strip()
                if not k or k in _CONFIG_NON_LICENSE_KEYS:
                    continue
                res[k] = v.strip().lower() == 'agree'
    except Exception:                                       # noqa: BLE001
        pass
    return res


def license_ok() -> bool:
    """所有协议是否都已接受。

    读不到配置文件时返回 True（不阻塞）—— 可能是路径变了或非 Windows，
    此时不该拦住启动，让 `start` 自己去报错更准确。
    """
    lic = license_status()
    return all(lic.values()) if lic else True


def list_instances(exe: Optional[str] = None,
                   details: bool = False) -> List[Dict[str, Any]]:
    """列已有实例。details=True 时返回结构化 dict 列表。"""
    r = run(['-list'] + (['-details'] if details else []), exe, timeout=60)
    text = r['stdout'] + r['stderr']
    if details:
        d = _json_from(text)
        return d if isinstance(d, list) else []
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith('[')]


def list_images(exe: Optional[str] = None,
                downloaded_only: bool = False,
                device_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """列可用镜像。"""
    args = ['-imageList']
    if downloaded_only:
        args += ['-downloaded', 'true']
    if device_type:
        args += ['-deviceType', device_type]
    r = run(args, exe, timeout=90)
    d = _json_from(r['stdout'] + r['stderr'])
    return d if isinstance(d, list) else []


def list_screen_profiles(exe: Optional[str] = None,
                         details: bool = False) -> str:
    """列所有支持自定义屏幕的机型（返回原文，格式是树状的不好结构化）。"""
    args = ['-screenProfileList'] + (['-details'] if details else [])
    r = run(args, exe, timeout=90)
    return r['stdout'] or r['stderr']


# ------------------------------------------------------------------ 动作

def accept_license(exe: Optional[str] = None) -> Dict[str, Any]:
    """自动接受协议。

    ⚠️ **这是法律协议** —— 本函数只是封装命令，是否调用由你决定。
    调用即代表你本人已阅读并同意 HarmonyOS 软件服务协议与 SDK 协议。
    """
    return run(['-license', 'accept'], exe, timeout=60)


def pick_image_version(device_type: str, prefer: str = 'latest',
                       exe: Optional[str] = None) -> Optional[str]:
    """给某设备类型挑一个镜像版本。

    Parameters
    ----------
    prefer
        `'latest'`（默认）取列表第一个，即最新版；
        `'smallest'` 取**版本号最小**的（通常镜像更小、资源占用更低，
        适合内存紧张的机器）。

    Notes
    -----
    华为的 `-imageList` 是按版本倒序返回的（最新在最前）。
    **最新版 = 最大镜像 + 最高资源占用**，在只有 5.4 GB 可用内存的机器上
    未必划算 —— 所以提供 `smallest` 选项。
    """
    imgs = list_images(exe, device_type=device_type)
    if not imgs:
        return None
    if prefer == 'smallest':
        def _ver_key(item: Dict[str, Any]):
            # 'HarmonyOS 7.0.0(26.0.0)' / 'HarmonyOS 5.0.1(13)' → (5,0,1,13)
            s = str(item.get('osVersion') or '')
            nums, cur = [], ''
            for ch in s:
                if ch.isdigit():
                    cur += ch
                elif cur:
                    nums.append(int(cur))
                    cur = ''
            if cur:
                nums.append(int(cur))
            return tuple(nums) or (0,)
        try:
            return min(imgs, key=_ver_key).get('osVersion')
        except Exception:                                   # noqa: BLE001
            pass
    return imgs[0].get('osVersion')


def install_image(device_type: str, os_version: Optional[str] = None,
                  prefer: str = 'smallest',
                  exe: Optional[str] = None) -> Dict[str, Any]:
    """下载指定类型的镜像。

    Parameters
    ----------
    os_version
        显式指定版本。给了它 `prefer` 就失效。
    prefer
        未指定版本时的挑选策略，见 `pick_image_version`。
        **默认 `'smallest'`** —— 本机内存紧张，小镜像更容易跑起来。
        想看或要最新版就传 `prefer='latest'`。
    """
    if not os_version:
        os_version = pick_image_version(device_type, prefer, exe)
        if not os_version:
            return {'rc': -1, 'stdout': '', 'argv': [],
                    'stderr': f'查不到 {device_type} 的可用镜像'}
    args = ['-install', '-deviceType', device_type, '-osVersion', os_version]
    # ⚠️ `-install` 会交互式追问「是否同意协议 (y/N)」。
    # 不喂 stdin 的话拿到 EOF 直接中止，**且不报错** ——
    # 表现为「命令瞬间返回、镜像没下、rc 还是 0」，极难排查。
    return run(args, exe, timeout=INSTALL_TIMEOUT, stdin='y\n')


def create_instance(name: str, device_type: str,
                    os_version: Optional[str] = None,
                    screen_profile: Optional[str] = None,
                    screen: Optional[str] = None,
                    memory: Optional[int] = None,
                    storage: Optional[int] = None,
                    exe: Optional[str] = None) -> Dict[str, Any]:
    """建实例。

    Parameters
    ----------
    screen
        自定义屏幕，格式 `"<width> <height> <dpi> <size>"`（size = 对角线 inch）。
        例 `"2200 2480 480 7.8"`。给了它就相当于造一个**全新形态**，
        不必局限于 `-screenProfile` 的预置机型。
    """
    args = ['-create', name, '-deviceType', device_type]
    if os_version:
        args += ['-osVersion', os_version]
    if screen_profile:
        args += ['-screenProfile', screen_profile]
    if screen:
        args += ['-screen', screen]
    if memory:
        args += ['-memory', str(memory)]
    if storage:
        args += ['-storage', str(storage)]
    return run(args, exe, timeout=300)


def delete_instance(name: str, force: bool = True,
                    exe: Optional[str] = None) -> Dict[str, Any]:
    args = ['-delete', name] + (['-force'] if force else [])
    return run(args, exe, timeout=120)


def preflight(device_type: Optional[str] = None,
              exe: Optional[str] = None) -> List[str]:
    """启动前自检。返回问题列表（空 = 可以启动）。

    **为什么不直接启动**：协议没接受 / 镜像没下载这两个状态下，
    启动命令会失败但报错往往很含糊（或直接卡住），
    不如提前查出来说得清楚。
    """
    problems: List[str] = []
    lic = license_status()
    if lic and not all(lic.values()):
        bad = [k for k, v in lic.items() if not v]
        problems.append(f'协议未接受：{", ".join(bad)} —— '
                        f'需你本人执行 `accept --yes`')
    imgs = list_images(exe)
    dl = [i for i in imgs if str(i.get('downloaded')).lower() == 'true']
    if not dl:
        problems.append('镜像一个都没下载 —— 先 `install <deviceType>`')
    elif device_type:
        need = [i for i in dl if i.get('deviceType') == device_type]
        if not need:
            have = sorted({i.get('deviceType') for i in dl})
            problems.append(f'没有 {device_type} 类型的镜像'
                            f'（已下载的是：{", ".join(have)}）')
    return problems


def available_memory_gb() -> Optional[float]:
    """读可用物理内存（GB）。非 Windows 或失败返回 None。"""
    try:
        import ctypes

        class _MEM(ctypes.Structure):
            _fields_ = [
                ('dwLength', ctypes.c_ulong),
                ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
            ]
        m = _MEM()
        m.dwLength = ctypes.sizeof(_MEM)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return m.ullAvailPhys / 1024 ** 3
    except Exception:                                       # noqa: BLE001
        return None


def start(name: str, no_window: bool = False,
          hdc_port: Optional[int] = None,
          skip_check: bool = False,
          exe: Optional[str] = None,
          wait: bool = True,
          wait_timeout: int = 240) -> Dict[str, Any]:
    """启动实例并**等设备真的上线**。

    ★★ **不要用「`-start` 进程退出」判断启动成功** —— 那条路是错的，实测结论：

    1. `Emulator.exe -start` 是**前台常驻进程**，机器不死它不退出。
       用 `subprocess.run(timeout=300)` 等它，只会撑满 300s 后被超时杀掉 ——
       **而且会把已经跑起来的模拟器一起带走**。
    2. **不能用 `DETACHED_PROCESS` 启动**：完全脱离控制台后，模拟器会
       **无报错静默退出**（日志只留一行
       `Windows Hypervisor Platform accelerator is operational`）。
    3. **调用方进程退出，模拟器也会跟着死** —— 它依附调用者所在的会话。

    ⚠️ 因此在本工具链里，启动模拟器的正确姿势是**由一个长期存活的后台任务
    持有它**，例如：

        # 在 run_in_background 的 shell 里（该任务不会超时）
        "C:/.../Emulator.exe" -start "Mate X7" > _out/emu.log 2>&1

    本函数用 `Popen` 非阻塞拉起，然后**轮询 hdc 直到设备出现** ——
    判据是「设备能被 `hdc list targets` 看见 + 能 `hdc shell echo`」，
    不是进程退出码。

    Parameters
    ----------
    wait
        True（默认）轮询等设备上线；False 只拉起就返回。
    wait_timeout
        等设备上线的秒数上限（冷启动实测约 40–60s）。
    """
    if not skip_check:
        # 从实例详情推断 deviceType，用于检查「该类型的镜像下了没」
        dtype = None
        for i in list_instances(exe, details=True):
            if os.path.basename(str(i.get('instancePath', ''))).strip() == name.strip():
                dtype = i.get('deviceType')
                break
        probs = preflight(dtype, exe)
        if probs:
            return {'rc': -4, 'stdout': '', 'argv': [],
                    'stderr': '启动前自检未通过：\n  - ' + '\n  - '.join(probs)}

        # 内存提示（本机 15.6 GB 是临界值，实例要 4 GB）
        avail = available_memory_gb()
        ram = None
        for i in list_instances(exe, details=True):
            if os.path.basename(str(i.get('instancePath', ''))).strip() == name.strip():
                try:
                    ram = int(i.get('hw.ramSize') or 0) / 1024
                except (TypeError, ValueError):
                    ram = None
                break
        if avail is not None and ram:
            if avail < ram + 1.0:
                return {'rc': -5, 'stdout': '', 'argv': [],
                        'stderr': (f'内存不足：实例需要 {ram:.0f} GB，'
                                   f'当前可用仅 {avail:.1f} GB。\n'
                                   f'  请关掉浏览器/DevEco 等程序后重试，'
                                   f'或用 `skip_check=True` 强行启动。')}

    # 已经在跑就直接返回，避免重复拉起
    dev = list_targets()
    if dev:
        return {'rc': 0, 'stdout': f'设备已在线：{dev[0]}', 'stderr': '',
                'argv': [], 'device': dev[0], 'already_running': True}

    emu = find_emulator(exe)
    if not emu:
        return {'rc': -1, 'stdout': '', 'stderr': 'Emulator.exe 未找到',
                'argv': []}

    args = ['-start', name]
    if no_window:
        args += ['-noWindow']
    if hdc_port:
        args += ['-hdcPort', str(hdc_port)]

    # 非阻塞拉起。**不加 DETACHED_PROCESS**（会让它静默退出）。
    try:
        p = subprocess.Popen([emu] + args,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception as e:                                  # noqa: BLE001
        return {'rc': -3, 'stdout': '', 'argv': [emu] + args,
                'stderr': f'{type(e).__name__}: {e}'}

    res: Dict[str, Any] = {'rc': 0, 'stdout': f'已拉起 Emulator.exe pid={p.pid}',
                           'stderr': '', 'argv': [emu] + args, 'pid': p.pid}
    if not wait:
        return res

    dev = wait_for_device(timeout=wait_timeout)
    if dev:
        res['device'] = dev
        res['stdout'] += f'\n设备已上线：{dev}'
        return res
    res['rc'] = -6
    res['stderr'] = (f'拉起成功但 {wait_timeout}s 内设备未在 hdc 里出现。\n'
                     f'  排查：① 内存是否够（实例要 4 GB）'
                     f' ② 看后台任务日志 ③ `hdc list targets` 手工确认')
    return res


def wait_for_device(timeout: int = 240, interval: int = 5,
                    hdc: Optional[str] = None) -> Optional[str]:
    """轮询等设备在 hdc 里出现。返回设备号，超时返回 None。

    ★ **这是判断「模拟器起没起来」的正确方式** —— 不是等 `-start` 进程退出
    （它不会退出，见 `start()` 的说明），而是等设备真的能被 hdc 看见。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = list_targets(hdc)
        if t:
            return t[0]
        time.sleep(interval)
    return None


def device_ready(hdc: Optional[str] = None) -> bool:
    """设备是否真的可用 —— 光在列表里不够，还要能执行 shell 命令。"""
    hdc_exe = hdc or find_hdc()
    if not hdc_exe:
        return False
    try:
        p = subprocess.run([hdc_exe, 'shell', 'echo READY'],
                           capture_output=True, timeout=30)
        return 'READY' in p.stdout.decode('utf-8', errors='replace')
    except Exception:                                       # noqa: BLE001
        return False


def stop(name: str, exe: Optional[str] = None) -> Dict[str, Any]:
    return run(['-stop', name], exe, timeout=120)


#: 各设备类型支持的折叠态（实测自 `-help`）
FOLD_STATES: Dict[str, tuple] = {
    'foldable': ('open', 'half-open', 'close'),
    'wide_fold': ('open', 'close'),
    'pc_foldable': ('open', 'vertical-open', 'half-open', 'close'),
    'triple_fold': ('single', 'double', 'triple',
                    'left-folded-right-half-folded',
                    'left-half-folded-right-expanded',
                    'left-expanded-right-folded',
                    'left-half-folded-right-folded',
                    'left-expanded-right-half-folded',
                    'left-half-folded-right-half-folded'),
}


def set_folded_state(name: str, state: str,
                     exe: Optional[str] = None) -> Dict[str, Any]:
    """★ 切换折叠态 —— 跨形态测试的核心动作。

    切换后**必须重新抓控件树**，之前缓存的坐标一律失效。
    """
    return run(['-instance', name, '-foldedState', state], exe, timeout=120)


def ui_layout(name: str, interactive: bool = False,
              all_windows: bool = False,
              exe: Optional[str] = None) -> Dict[str, Any]:
    """★ 抓模拟器的 UI 控件树（输出 markdown 文件）。

    这是模拟器**自带的**控件树导出，与 `uitest dumpLayout` 相互印证 ——
    当两边结果不一致时，说明有一方的解析有问题。

    ⚠️ **输出路径是写死的，没有自定义参数**（实测：`-uiLayoutPath` 会报
    `Unknown option`）。固定落在：

        %LOCALAPPDATA%\\Huawei\\Emulator\\deployed\\<实例名>\\uiLayout\\analysis.md

    好在它**同时把内容打印到 stdout**（先一行 `Analysis saved to: ...`，
    再接 markdown 正文），所以不用读文件也能拿到控件树。
    返回的 dict 里额外给了 `path` 和 `markdown` 两个便捷字段。
    """
    args = ['-instance', name, '-uiLayout']
    if interactive:
        args += ['-i']
    if all_windows:
        args += ['-a']
    r = run(args, exe, timeout=120)

    # 从 stdout 里切出 markdown 正文，并抓出落盘路径
    text = r['stdout'] + r['stderr']
    r['path'] = None
    for line in text.splitlines():
        if line.startswith('Analysis saved to:'):
            r['path'] = line.split(':', 1)[1].strip()
            break
    idx = text.find('# Widget Tree Analysis')
    r['markdown'] = text[idx:] if idx >= 0 else ''

    # 控件数：markdown 里以 '- ' 开头的行
    r['widget_count'] = sum(
        1 for ln in r['markdown'].splitlines() if ln.strip().startswith('- '))
    return r


#: 屏幕信息行的解析正则 —— 抽成模块级常量，**让模拟器与解析器共用**。
#: 曾经的教训：`sim.py` 里 hidumper 只返回一句假的 `screen size: W x H`，
#: 与真机格式完全不同，导致模拟环境测不出问题、真机才暴露。
#: 现在模拟端也按这个格式产出，两边用同一份定义。
SCREEN_LINE_RE = re.compile(
    r'screen\[(\d+)\]:.*?powerStatus=(\w+),.*?backlight=(\d+),'
    r'.*?render resolution=(\d+)x(\d+)')
_ACTIVE_MODE_RE = re.compile(r'(\d+)x(\d+)')


def parse_screen_info(text: str) -> List[Dict[str, Any]]:
    """★ 把 `hidumper -s RenderService -a screen` 的**原文**解析成结构化列表。

    纯函数，不碰 subprocess —— 这样：
      - 真机路径：`screen_info()` 拿 hdc 输出喂进来
      - 模拟路径：`sim.py` 的 `FakeHdc` 用同一份正则产出**同格式**输出
    两边共用一处定义，避免「模拟和解析各写一套正则、迟早不一致」。

    真机原文形如::

        screen[0]: id=0, powerStatus=POWER_STATUS_ON, backlight=1,
                   screenType=EXTERNAL_TYPE, render resolution=2416x2210, ...
        supportedMode[0]: 2416x2210, refreshRate=60
        activeMode: 2416x2210, refreshRate=60
        name=express_display, phyWidth=158, phyHeight=141, ...

    返回每块屏一个 dict：
        {'index', 'power_status', 'backlight', 'width', 'height',
         'active_width', 'active_height'}

    `activeMode` 是可选的（有些屏没有），取不到就不放那两个键。
    """
    screens: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in text.replace('\r', '').splitlines():
        m = SCREEN_LINE_RE.search(line)
        if m:
            cur = {'index': int(m.group(1)), 'power_status': m.group(2),
                   'backlight': int(m.group(3)),
                   'width': int(m.group(4)), 'height': int(m.group(5))}
            screens.append(cur)
        elif cur is not None and line.startswith('activeMode:'):
            am = _ACTIVE_MODE_RE.search(line)
            if am:
                cur['active_width'] = int(am.group(1))
                cur['active_height'] = int(am.group(2))
    return screens


def screen_info(name_or_none: Optional[str] = None,
                exe: Optional[str] = None,
                hdc: Optional[str] = None) -> List[Dict[str, Any]]:
    """★ 抓**屏幕级**信息（分辨率 / 电源状态 / 背光）—— 折叠态验证的硬证据。

    走的是设备侧 `hidumper -s RenderService -a screen`，比只有分辨率的
    判断更可靠：**折叠态真正生效时，两块屏的 `power_status` 会互换**。

    > 实测 Mate X7（双屏折叠）：`open`/`half-open` 点亮内屏 2416x2210，
    > `close` 点亮外屏 1080x2444。**只看分辨率看不出差别，必须看电源状态。**

    解析交给 `parse_screen_info()`（纯函数，可与模拟端共用）。

    `name_or_none` 目前不参与命令（hidumper 走 hdc，设备由 hdc 自己选），
    保留参数是为了调用方语义清晰。
    """
    hdc_exe = hdc or find_hdc()
    if not hdc_exe:
        return []
    try:
        p = subprocess.run(
            [hdc_exe, 'shell', 'hidumper -s RenderService -a screen'],
            capture_output=True, timeout=60)
        out = p.stdout.decode('utf-8', errors='replace')
    except Exception:                                       # noqa: BLE001
        return []
    return parse_screen_info(out)


def active_screen(screens: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从 `screen_info()` 结果里挑出**当前点亮**的那块屏。"""
    for s in screens:
        if 'ON' in str(s.get('power_status', '')).upper():
            return s
    return None


def find_hdc(explicit: Optional[str] = None) -> Optional[str]:
    """找 hdc.exe（DevEco 自带）。找不到返回 None（不猜）。"""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for p in HDC_PATHS:
        if os.path.exists(p):
            return p
    return None


def list_targets(hdc: Optional[str] = None) -> List[str]:
    """`hdc list targets` —— 返回已连上的设备列表（如 127.0.0.1:5555）。"""
    hdc_exe = hdc or find_hdc()
    if not hdc_exe:
        return []
    try:
        p = subprocess.run([hdc_exe, 'list', 'targets'],
                           capture_output=True, timeout=30)
        out = p.stdout.decode('utf-8', errors='replace').replace('\r', '')
    except Exception:                                       # noqa: BLE001
        return []
    return [ln.strip() for ln in out.splitlines()
            if ln.strip() and not ln.strip().startswith('[')]


def screenshot(name: str, path: Optional[str] = None,
               exe: Optional[str] = None) -> Dict[str, Any]:
    args = ['-instance', name, '-screenshot']
    if path:
        args += ['-screenshotPath', path]
    return run(args, exe, timeout=120)


def rotate(name: str, direction: str, exe: Optional[str] = None
           ) -> Dict[str, Any]:
    """旋转。direction = 'left' / 'right'。"""
    return run(['-instance', name, '-rotation', direction], exe, timeout=60)


def power(name: str, exe: Optional[str] = None) -> Dict[str, Any]:
    """模拟电源键（亮屏/息屏）—— 测锁屏场景用。"""
    return run(['-instance', name, '-power'], exe, timeout=60)


# ------------------------------------------------------------------ CLI

def _out(title: str, text: str) -> None:
    print(f'\n=== {title} ===')
    print(text.rstrip() if text.strip() else '(无输出)')


def _cmd_status(exe: Optional[str]) -> int:
    v = version(exe)
    print(f'Emulator 版本 : {v or "未找到"}')
    emu = find_emulator(exe)
    print(f'可执行文件     : {emu or "未找到"}')
    if not emu:
        return 1

    lic = license_status()
    print('\n协议状态：')
    if lic:
        for k, ok in lic.items():
            print(f'  {"[已接受]" if ok else "[未接受]"} {k}')
        if not all(lic.values()):
            print('  ⚠️ 有未接受的协议 → 启动会失败。'
                  '需你本人执行 `emulator_cli.py accept`')
    else:
        print('  (读不到 .emu_config)')

    insts = list_instances(exe, details=True)
    print(f'\n已有实例 ({len(insts)})：')
    for i in insts:
        print(f'  - {os.path.basename(i.get("instancePath", "?")):20s} '
              f'type={i.get("deviceType"):10s} '
              f'running={i.get("isRunning")}')

    imgs = list_images(exe)
    dl = [i for i in imgs if str(i.get('downloaded')).lower() == 'true']
    print(f'\n镜像：{len(dl)}/{len(imgs)} 已下载')
    if not dl:
        print('  ⚠️ 一个镜像都没下载 → 实例建了也跑不起来')
    for i in imgs:
        mark = '[已下载]' if str(i.get('downloaded')).lower() == 'true' \
            else '[未下载]'
        print(f'  {mark} {i.get("deviceType"):16s} {i.get("osVersion")}')

    # 内存 —— 本机 15.6 GB 是 DevEco 官方最低线，这是最可能卡住的地方
    avail = available_memory_gb()
    if avail is not None:
        print(f'\n可用内存：{avail:.1f} GB')
        need = 4.0            # 实测实例 hw.ramSize = 4096
        if avail < need + 1.0:
            print(f'  ⚠️ 实例默认要 {need:.0f} GB，余量不足 → 启动前请关掉'
                  f'浏览器/DevEco 等程序')
        else:
            print(f'  ✅ 够跑一个实例（默认 {need:.0f} GB）')

    problems = preflight(exe=exe)
    print('\n启动前自检：')
    if problems:
        for p in problems:
            print(f'  ❌ {p}')
    else:
        print('  ✅ 可以启动')
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description='模拟器命令行封装（不依赖 DevEco GUI）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('--exe', default=None, help='指定 Emulator.exe 路径')
    sub = ap.add_subparsers(dest='cmd')

    sub.add_parser('status', help='★ 一键自检：版本/协议/实例/镜像')
    sub.add_parser('images', help='列可用镜像')
    sub.add_parser('profiles', help='列支持自定义屏幕的机型')

    p = sub.add_parser('accept', help='⚠️ 自动接受协议（需你本人授权）')
    p.add_argument('--yes', action='store_true',
                   help='确认已阅读并同意协议，真正执行')

    p = sub.add_parser('install', help='下载镜像')
    p.add_argument('device_type')
    p.add_argument('--os-version', default=None,
                   help='显式指定版本，如 "HarmonyOS 6.1.1(24)"')
    p.add_argument('--prefer', choices=('smallest', 'latest'),
                   default='smallest',
                   help='未指定版本时的策略（默认 smallest：镜像更小、'
                        '内存占用更低）')

    p = sub.add_parser('list', help='列实例')
    p.add_argument('--details', action='store_true')

    p = sub.add_parser('create', help='建实例')
    p.add_argument('--name', required=True)
    p.add_argument('--type', required=True)
    p.add_argument('--os-version', default=None)
    p.add_argument('--screen-profile', default=None)
    p.add_argument('--screen', default=None,
                   help='自定义屏幕 "<w> <h> <dpi> <size>"')
    p.add_argument('--memory', type=int, default=None)
    p.add_argument('--storage', type=int, default=None)

    p = sub.add_parser('delete', help='删实例')
    p.add_argument('name')

    p = sub.add_parser('start', help='启动实例（带启动前自检）')
    p.add_argument('name')
    p.add_argument('--no-window', action='store_true')
    p.add_argument('--hdc-port', type=int, default=None)
    p.add_argument('--skip-check', action='store_true',
                   help='跳过协议/镜像/内存自检，强行启动')

    p = sub.add_parser('stop', help='停止实例')
    p.add_argument('name')

    p = sub.add_parser('fold', help='★ 切换折叠态')
    p.add_argument('name')
    p.add_argument('state')
    p.add_argument('--show-states', action='store_true',
                   help='连带打印该设备类型支持的全部取值')

    p = sub.add_parser('uilayout', help='抓 UI 控件树')
    p.add_argument('name')
    p.add_argument('-i', action='store_true', dest='interactive')
    p.add_argument('-a', action='store_true', dest='all_windows')

    p = sub.add_parser('screenshot', help='截图')
    p.add_argument('name')
    p.add_argument('--path', default=None)

    p = sub.add_parser('rotate', help='旋转 left/right')
    p.add_argument('name')
    p.add_argument('direction')

    p = sub.add_parser('power', help='电源键（亮/息屏）')
    p.add_argument('name')

    args = ap.parse_args(argv)
    exe = args.exe

    if not args.cmd or args.cmd == 'status':
        return _cmd_status(exe)

    if args.cmd == 'images':
        imgs = list_images(exe)
        _out('可用镜像', '\n'.join(
            f'{"[已下载]" if str(i.get("downloaded")).lower() == "true" else "[未下载]"} '
            f'{i.get("deviceType"):16s} {i.get("osVersion")}' for i in imgs))
        return 0

    if args.cmd == 'profiles':
        _out('支持自定义屏幕的机型', list_screen_profiles(exe, details=True))
        return 0

    if args.cmd == 'accept':
        if not args.yes:
            print('⚠️  这是法律协议，需要你本人确认。\n')
            print('    确认已阅读并同意以下协议后，加 --yes 重新执行：')
            print('      · HarmonyOS 软件服务协议')
            print('      · HarmonyOS SDK 协议')
            print(f'\n    届时执行：python {os.path.basename(__file__)} '
                  f'accept --yes')
            return 2
        r = run(['-license', 'accept'], exe, timeout=60)
        print('已执行。当前协议状态：')
        for k, ok in license_status().items():
            print(f'  {"[已接受]" if ok else "[未接受]"} {k}')
        print(f'\n(rс={r["rc"]})')
        return 0

    if args.cmd == 'install':
        ver = args.os_version or pick_image_version(args.device_type,
                                                    args.prefer, exe)
        print(f'准备下载 {args.device_type} 镜像：{ver}')
        print('（可能要几分钟，镜像约 1–2 GB）\n')
        r = install_image(args.device_type, args.os_version,
                          args.prefer, exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    if args.cmd == 'list':
        if args.details:
            insts = list_instances(exe, details=True)
            for i in insts:
                print(f'--- {os.path.basename(i.get("instancePath", "?"))}')
                for k in ('deviceType', 'imageSubPath', 'isRunning',
                          'hw.lcd.density', 'hw.lcd.number'):
                    print(f'  {k:18s} = {i.get(k)}')
        else:
            print('\n'.join(list_instances(exe)))
        return 0

    if args.cmd == 'create':
        r = create_instance(args.name, args.type, args.os_version,
                            args.screen_profile, args.screen,
                            args.memory, args.storage, exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    if args.cmd in ('delete', 'start', 'stop'):
        if args.cmd == 'start':
            r = start(args.name, no_window=args.no_window,
                      hdc_port=args.hdc_port, skip_check=args.skip_check,
                      exe=exe)
        elif args.cmd == 'delete':
            r = delete_instance(args.name, exe=exe)
        else:
            r = stop(args.name, exe=exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    if args.cmd == 'fold':
        if args.show_states:
            print('该设备类型支持的折叠态：')
            for t, states in FOLD_STATES.items():
                print(f'  {t:14s} {", ".join(states)}')
            print()
        r = set_folded_state(args.name, args.state, exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    if args.cmd == 'uilayout':
        r = ui_layout(args.name, args.interactive, args.all_windows, exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    if args.cmd == 'screenshot':
        r = screenshot(args.name, args.path, exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    if args.cmd == 'rotate':
        r = rotate(args.name, args.direction, exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    if args.cmd == 'power':
        r = power(args.name, exe)
        print((r['stdout'] + r['stderr']).strip() or f'rc={r["rc"]}')
        return 0 if r['rc'] == 0 else 1

    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
