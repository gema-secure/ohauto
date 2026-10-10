"""
L1 基元 —— 性能 / 资源采样原语（2C【C4】）
==========================================

从 tools/stability_telemetry.py 收编进包（2C【C4】）：
hidumper PSS / loadavg 负载 / pidof 存活 / 宿主内存的**采样与分析**单源在这里，
该工具退化为薄 CLI 壳。周期采样与趋势曲线的编排入口在
`ohauto.signals.PerfChannel`。

三条硬约束（继承长稳遥测的设计约定）
------------------------------------
1. **采样失败记 None + 警告，绝不编造、绝不中断** —— 采样是旁路行为，
   「缺样本 ≠ 正常」；消费方（断言 / 分析）必须能区分「没采到」和
   「采到了且达标」，所以缺样本绝不能伪装成通过。
2. 单轮采样 < 2 秒（默认 60 秒间隔下占空比 ~3%，不影响被测跑测的延迟口径）。
3. 零依赖：hdc 交互只走 `HdcLike.shell`，宿主内存解析走 tasklist CSV。

PSS 解析格式（真机 DAYU200 / OpenHarmony 5.0.3.135 实测定稿）
------------------------------------------------------------
`hidumper --mem <pid>` 的表头行含 `Pss`，**数值在紧随其后的 `Total` 行** ——
表头行本身没有数字。表格有**两行** Total 开头：先是单位行（Total Clean
Dirty…，无数字），后才是数值行（Total 41322 …）。规则：见到 Pss 表头后，
扫其后首个带数字的 Total 行，取第一个数字 = PSS 合计（KB）。
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional

#: PSS 斜率判据：后半程均值 / 前半程均值，涨幅超过它 → 报泄漏嫌疑。
#: 首轮校准前是**暂行值**，首轮长稳跑完按实测分布冻结（先测再定）。
PSS_SLOPE_THRESHOLD = 30.0

#: 有效 PSS 样本少于这个数时不判斜率 —— 前后半均值至少要各有支撑。
MIN_PSS_SAMPLES = 4


# ---------------------------------------------------------------- 采集

def device_sample(hdc: Any, bundle: str) -> Dict[str, Any]:
    """一次设备侧采样：被测应用 PSS / 负载 / 存活。失败记 None + 警告。

    返回键：`pss_kb / load1 / alive / pid / warn`。任何一项取不到就是
    None（或 alive=None），**绝不用「合理默认值」顶替** —— 把「没采到」
    误报成「正常」比缺数据更危险。
    """
    s: Dict[str, Any] = {'pss_kb': None, 'load1': None, 'alive': None,
                         'pid': None, 'warn': []}
    try:
        r = hdc.shell('pidof %s' % bundle, timeout=15)
        pids = (r.stdout or '').split()
        if not pids:
            s['alive'] = False
            s['warn'].append('被测应用进程不在运行')
            return s
        s['alive'] = True
        s['pid'] = pids[0]
    except Exception as e:                     # noqa(采样是旁路，失败必须降级为警告)
        s['warn'].append('pidof 失败: %s: %s' % (type(e).__name__, e))
        return s

    try:
        r = hdc.shell('hidumper --mem %s' % s['pid'], timeout=30)
        pss = _parse_pss(r.stdout or '')
        if pss is None:
            s['warn'].append('hidumper 输出里没解析到 PSS（表头+Total 结构缺失）')
        else:
            s['pss_kb'] = pss
    except Exception as e:                     # noqa(采样是旁路，失败必须降级为警告)
        s['warn'].append('hidumper 失败: %s: %s' % (type(e).__name__, e))

    try:
        r = hdc.shell('cat /proc/loadavg', timeout=10)
        m = re.match(r'\s*([0-9.]+)', r.stdout or '')
        if m:
            s['load1'] = float(m.group(1))
        else:
            s['warn'].append('loadavg 输出无法解析（缺样本 ≠ 正常，显式留痕）')
    except Exception as e:                     # noqa(采样是旁路，失败必须降级为警告)
        s['warn'].append('loadavg 失败: %s: %s' % (type(e).__name__, e))
    return s


def _parse_pss(text: str) -> Optional[int]:
    """从 `hidumper --mem` 输出里解析 PSS 合计（KB）。解析不到返回 None。

    规则见模块 docstring：Pss 表头之后、首个**带数字**的 Total 行的第一个数。
    """
    seen_header = False
    for line in text.splitlines():
        up = line.upper()
        if 'PSS' in up:
            seen_header = True
            continue
        if seen_header and re.match(r'\s*Total\b', line, re.IGNORECASE):
            nums = re.findall(r'(\d+)', line)
            if nums:
                return int(nums[0])
    return None


def host_sample(host_pid: Optional[int]) -> Dict[str, Any]:
    """宿主跑测进程的内存（tasklist CSV，Windows）。host_pid 空则跳过。"""
    s: Dict[str, Any] = {'host_mem_kb': None, 'warn': []}
    if not host_pid:
        return s
    try:
        r = subprocess.run(['tasklist', '/FI', 'PID eq %d' % host_pid,
                            '/FO', 'CSV', '/NH'],
                           capture_output=True, text=True, timeout=20)
        m = re.search(r'"\s?([\d,\s]+)\s?K"', r.stdout or '')
        if m:
            s['host_mem_kb'] = int(m.group(1).replace(',', ''))
    except Exception as e:                     # noqa(采样是旁路，失败必须降级为警告)
        s['warn'].append('tasklist 失败: %s' % e)
    return s


def take_sample(hdc: Any, bundle: str, host_pid: Optional[int] = None,
                rounds_done: Optional[int] = None) -> Dict[str, Any]:
    """一轮完整采样 = 设备侧 + 宿主侧，带本机时间戳与可选轮次标记。

    两侧的警告**合并**而非覆盖 —— 宿主侧的空警告不能抹掉设备侧已发生的
    降级记录，否则「缺样本」的证据就丢了。
    """
    s: Dict[str, Any] = {'ts': datetime.now().isoformat(timespec='seconds')}
    d = device_sample(hdc, bundle)
    h = host_sample(host_pid)
    warns = list(d.get('warn') or []) + list(h.get('warn') or [])
    d.update(h)
    d['warn'] = warns
    s.update(d)
    if rounds_done is not None:
        s['rounds_done'] = rounds_done
    return s


# ---------------------------------------------------------------- 分析

def analyze_samples(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一批采样点做趋势分析（**纯函数**，不打印 —— 展示归 CLI / 报告）。

    斜率口径：PSS 有效样本按时间序取前半 / 后半均值，涨幅百分比超过
    `PSS_SLOPE_THRESHOLD` → `leak_suspect=True`（嫌疑，不是定案 ——
    长稳判泄漏要结合曲线形态人工确认）。

    返回键：`samples`（总点数）、`pss_n / pss_missing`、`pss_min / pss_max`、
    `pss_mean_first / pss_mean_second / slope_pct`、`leak_suspect`、
    `load_mean / load_peak`、`insufficient`（有效 PSS 不足 MIN_PSS_SAMPLES，
    此时 slope_pct 为 None，不判斜率）。
    """
    pss = [s['pss_kb'] for s in samples if s.get('pss_kb') is not None]
    missing = sum(1 for s in samples if s.get('pss_kb') is None)
    loads = [s['load1'] for s in samples if s.get('load1') is not None]

    out: Dict[str, Any] = {
        'samples': len(samples),
        'pss_n': len(pss),
        'pss_missing': missing,
        'pss_min': min(pss) if pss else None,
        'pss_max': max(pss) if pss else None,
        'pss_mean_first': None,
        'pss_mean_second': None,
        'slope_pct': None,
        'leak_suspect': False,
        'load_mean': round(sum(loads) / len(loads), 4) if loads else None,
        'load_peak': max(loads) if loads else None,
        'insufficient': len(pss) < MIN_PSS_SAMPLES,
    }
    if out['insufficient'] or not pss:
        return out
    half = len(pss) // 2
    m1 = sum(pss[:half]) / half
    m2 = sum(pss[half:]) / (len(pss) - half)
    pct = (m2 - m1) / m1 * 100 if m1 else 0.0
    out['pss_mean_first'] = round(m1, 2)
    out['pss_mean_second'] = round(m2, 2)
    out['slope_pct'] = round(pct, 2)
    out['leak_suspect'] = pct > PSS_SLOPE_THRESHOLD
    return out
