"""把设备时间同步到 PC 时间，并把 RTC 一并写好。

★ 为什么这个脚本必须存在
------------------------

`doctor.py` 的时间检查失败时会建议运行本脚本 —— 但本脚本此前**并不存在**
（doctor.py 指向了一个不存在的文件）。而设备时间错乱是**每次接真机几乎都会遇到**
的问题：开发板 RTC 停在出厂日期（实测 DAYU200 为 `2017-08-05`），
会让报告时间戳、截图文件名、hilog 时序全部失真，
也会让「N 秒内应超时」这类断言失效。

★ 关键坑：`hwclock` 必须带 `-u`
-------------------------------

OpenHarmony 上通常**没有 `/etc/adjtime`**，`hwclock` 便按 **localtime** 处理，
而设备 RTC 实际存的是 **UTC**。实测（东八区）：

| 操作 | RTC 原始读数 | 判定 |
|---|---|---|
| 系统本地 CST | `13:59:42` | — |
| 系统 UTC | `05:59:42` | — |
| `hwclock -w` | `13:59:42` | ❌ 写入本地时间 → 下次开机**快 8 小时** |
| `hwclock -w -u` | `05:59:43` | ✅ 正确 |

**判据**：`hwclock -r` 的读数应等于 `date -u` 的读数。
若等于 `date`（本地时间）的读数，说明 `-u` 漏了。

用法
----

    python tools/sync_device_time.py             # 校时 + 校验
    python tools/sync_device_time.py --reboot     # 校时后重启再校验
    python tools/sync_device_time.py --check      # 只检查，不改动

⚠️ 完全断电后 RTC 能否继续走时，取决于板上是否装了纽扣电池
（DAYU200 需自行装 CR2032）。没装电池则软件无解，
只能每次开机重跑本脚本。
"""
import argparse
import datetime
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc, HdcError                       # noqa: E402


#: 设备时间输出。**实测有两种形态，正则必须同时吃下**：
#:   `date`       → `Sat Aug  5 17:01:03 CST 2017`（有时区，年份在最后）
#:   `hwclock -r` → `Sat Sep 19 06:38:29 2026  0.000000 seconds`（无时区，年份紧跟秒后）
#: 差别在于「年份前有没有时区字段」，用可选的 `(?:\S+\s+)?` 兼容；
#: 年份约束成 `\d{4}` 是为了避免把 drift 里的 `0`（`0.000000 seconds`）当年份。
_DATE_RE = re.compile(
    r'(\w{3})\s+(\w{3})\s+(\d+)\s+(\d+):(\d+):(\d+)\s+(?:(\S+)\s+)?(\d{4})')
_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _parse_date_like(text: str):
    """解析设备 `date` / `hwclock -r` 的输出，失败返回 None。

    抽成独立函数是因为三处都要用（读本地时间 / 读 UTC / 解析 RTC），
    而且**两种格式的差异必须只在这一处处理**。
    """
    m = _DATE_RE.search(text)
    if not m:
        return None
    _wd, mon, day, hh, mm, ss, _tz, year = m.groups()
    try:
        return datetime.datetime(int(year), _MONTHS.index(mon) + 1, int(day),
                                 int(hh), int(mm), int(ss))
    except ValueError:
        return None


def _read_device_local(hdc: Hdc) -> datetime.datetime:
    """取设备**本地**时间（读 `date`）。"""
    r = hdc.shell('date')
    if not r.ok:
        raise HdcError(f'读设备时间失败: {r}')
    dt = _parse_date_like(r.stdout)
    if dt is None:
        raise HdcError(f'无法解析设备 date 输出: {r.stdout!r}')
    return dt


def _read_device_utc(hdc: Hdc) -> datetime.datetime:
    """取设备 **UTC** 时间（读 `date -u`）。"""
    r = hdc.shell('date -u')
    if not r.ok:
        raise HdcError(f'读设备 UTC 失败: {r}')
    dt = _parse_date_like(r.stdout)
    if dt is None:
        raise HdcError(f'无法解析 date -u 输出: {r.stdout!r}')
    return dt


def _read_rtc(hdc: Hdc) -> str:
    """读 RTC 原始读数（`hwclock -r`）。"""
    r = hdc.shell('hwclock -r')
    return r.stdout.strip() if r.ok else f'<读取失败 rc={r.returncode}>'


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def check(hdc: Hdc) -> bool:
    """只检查，不改动。返回是否正常（年份合理 + RTC 与 UTC 一致）。"""
    pc = datetime.datetime.now()
    try:
        dev = _read_device_local(hdc)
        dev_utc = _read_device_utc(hdc)
    except HdcError as e:
        print(f'  [失败] {e}')
        return False

    drift = abs((dev - pc).total_seconds())
    print(f'  PC 本地   : {_fmt(pc)}')
    print(f'  设备本地  : {_fmt(dev)}   偏差 {drift:.1f}s')
    print(f'  设备 UTC  : {_fmt(dev_utc)}')

    ok = True
    if dev.year != pc.year:
        print(f'  [警告] 设备年份 {dev.year} != PC 年份 {pc.year} —— 需校时')
        ok = False
    if drift > 60:
        print(f'  [警告] 偏差 {drift:.0f}s 超过 60s —— 需校时')
        ok = False

    # RTC 与 UTC 一致性：这是「-u 有没有漏」的判据
    rtc = _read_rtc(hdc)
    print(f'  RTC 读数  : {rtc}')
    rtc_dt = _parse_date_like(rtc)
    if rtc_dt is not None:
        gap_utc = abs((rtc_dt - dev_utc).total_seconds())
        gap_local = abs((rtc_dt - dev).total_seconds())
        if gap_utc <= 5:
            print('  [通过] RTC == 设备 UTC → -u 正确')
        elif gap_local <= 5:
            print(f'  [警告] RTC == 设备本地时间（差 UTC {gap_utc:.0f}s）'
                  ' → 写 RTC 时漏了 -u，下次开机将快/慢一个时区')
            ok = False
        else:
            print(f'  [警告] RTC 与两者都不一致（距 UTC {gap_utc:.0f}s）—— 需重写')
            ok = False
    else:
        print('  [提示] 无法解析 RTC 读数（部分系统 hwclock -r 需 root）')

    return ok


def sync(hdc: Hdc) -> bool:
    """校时：设置系统时间 → 写 RTC（**带 -u**）→ 校验。"""
    pc = datetime.datetime.now()
    stamp = _fmt(pc)

    print(f'  [1/4] 设置设备时间 -> {stamp}')
    r = hdc.shell(f'date "{stamp}"')
    if not r.ok:
        print(f'        失败: {r.stderr.strip() or r.stdout.strip()}')
        return False

    print('  [2/4] 写 RTC（hwclock -w -u ★ 必须带 -u）')
    r = hdc.shell('hwclock -w -u')
    if not r.ok:
        # 部分设备 hwclock 需要 root；给出明确指引而不是静默失败
        print(f'        失败: {r.stderr.strip() or r.stdout.strip()}')
        print('        提示: 可能需 root —— 试 `hdc shell smode` 后重跑；'
              '仅设置系统时间（不写 RTC）也能让本次会话的时序正常')
        return False

    print('  [3/4] 校验设备时间')
    try:
        dev = _read_device_local(hdc)
    except HdcError as e:
        print(f'        失败: {e}')
        return False
    drift = (dev - pc).total_seconds()
    print(f'        设备 {_fmt(dev)} ｜ 偏差 {drift:+.1f}s')
    if abs(drift) > 5:
        print('        [警告] 偏差超过 5s，可能未生效')
        return False

    print('  [4/4] 校验 RTC')
    try:
        dev_utc = _read_device_utc(hdc)
    except HdcError as e:
        print(f'        失败: {e}')
        return False
    rtc = _read_rtc(hdc)
    print(f'        RTC   {rtc}')
    print(f'        UTC   {_fmt(dev_utc)}')
    rtc_dt = _parse_date_like(rtc)
    if rtc_dt is not None:
        gap = abs((rtc_dt - dev_utc).total_seconds())
        if gap <= 5:
            print('        [通过] RTC == UTC（-u 生效）')
        else:
            print(f'        [警告] RTC 距 UTC {gap:.0f}s —— -u 可能未生效')
            return False
    else:
        print('        [提示] 无法解析 RTC 读数，跳过这一项校验')
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='同步设备时间到 PC（含 RTC）')
    ap.add_argument('--target', default=None, help='设备序列号（多设备时指定）')
    ap.add_argument('--check', action='store_true', help='只检查，不改动')
    ap.add_argument('--reboot', action='store_true',
                    help='校时后重启再校验（验证开机能否从 RTC 恢复）')
    args = ap.parse_args(argv)

    print('=' * 60)
    print('  设备校时（只检查）' if args.check else '  设备校时')
    print('=' * 60)

    try:
        hdc = Hdc(target=args.target)
        hdc.list_targets()
    except (HdcError, Exception) as e:                     # noqa: BLE001
        print(f'[失败] 无法连接设备: {e}')
        return 2                       # 设备不在场（约定见 docs/API手册.md §三）

    if args.check:
        ok = check(hdc)
        print('=' * 60)
        print('结论：' + ('时间正常' if ok else '需要校时'))
        return 0 if ok else 1          # 时间不正常=未达标，设备明明在场

    if not sync(hdc):
        print('=' * 60)
        print('结论：校时未完全成功（见上方提示）')
        return 1

    if args.reboot:
        print('\n  [追加] 重启验证开机路径（DAYU200 约 25s 回来）')
        try:
            hdc.shell('reboot')
        except Exception:                                  # noqa: BLE001
            pass                                           # reboot 断开连接属正常
        time.sleep(10)
        hdc.wait_device(timeout=120)
        print('  重启后复核：')
        ok = check(hdc)
        print('=' * 60)
        print('结论：' + ('开机后时间正确（RTC 已被正确恢复）' if ok
                          else '开机后时间不正确 —— 检查是否漏 -u 或 RTC 电池'))
        return 0 if ok else 1          # 校验未达标，不是设备不在场

    print('=' * 60)
    print('结论：校时成功。建议加 --reboot 验证开机路径。')
    print('      注：完全断电后能否保持取决于板上纽扣电池（DAYU200 需装 CR2032）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
