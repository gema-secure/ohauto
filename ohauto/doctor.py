"""
环境自检 —— 一条命令确认 UI 自动化能力层能否跑起来
==================================================

依次检查：
    1. Python 版本与依赖（含 pyyaml，缺了 DSL 只能用 JSON）
    2. hdc 是否可执行
    3. 设备是否连接
    4. 设备时间是否正常  ← 开发板 RTC 未走时会让所有时间戳失真
    5. 设备是否支持 uitest 命令行通路  ← 决定技术路线，最关键
    6. 截图 / 控件树 是否可用（需设备）

用法:
    python -m ohauto.doctor
    python -m ohauto.doctor --bundle com.example.app   # 额外检查指定应用是否可拉起
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys

OK, WARN, BAD = '[ 通过 ]', '[ 警告 ]', '[ 失败 ]'
results = []


def _find_via_config():
    """从项目内配置文件读 hdc 路径（与 ohauto.hdc 的查找顺序一致）。"""
    try:
        from ohauto.hdc import Hdc
        return Hdc._from_config() or Hdc._locate()
    except Exception:
        return None


def _find_known_location():
    """在常见安装位置里找 hdc（只读，不写任何配置）。"""
    import glob
    user = os.environ.get('USERNAME', '')
    pats = [
        r'C:\Users\{u}\ohos-sdk\*\extracted\toolchains\hdc.exe',
        r'C:\Users\{u}\ohos-sdk\extracted\toolchains\hdc.exe',
        r'C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe',
        r'C:\Users\{u}\AppData\Local\Huawei\Sdk\openharmony\*\toolchains\hdc.exe',
        r'C:\Users\{u}\AppData\Local\OpenHarmony\Sdk\*\toolchains\hdc.exe',
    ]
    for pat in pats:
        for hit in glob.glob(pat.format(u=user)):
            if os.path.isfile(hit):
                return hit
    return None


def check(name: str, status: str, detail: str = '') -> None:
    results.append((status, name, detail))
    print(f'{status} {name}')
    if detail:
        for line in detail.splitlines():
            print(f'          {line}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--bundle', default='', help='额外检查该应用能否拉起')
    ap.add_argument('--ability', default='EntryAbility')
    args = ap.parse_args()

    print('=' * 68)
    print('  OpenHarmony UI 自动化能力层 · 环境自检')
    print('=' * 68)
    print()

    # ---------------------------------------------------------- 1 Python
    v = sys.version_info
    if v >= (3, 9):
        check('Python 版本', OK, f'{v.major}.{v.minor}.{v.micro}')
    else:
        check('Python 版本', BAD, f'{v.major}.{v.minor} 过低，需要 3.9+')

    # ---------------------------------------------------------- 2 依赖
    missing = []
    for mod, hint in [('yaml', 'pip install pyyaml')]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(f'{mod}  (可选，缺失时 DSL 退化为 JSON)')
    if missing:
        check('Python 依赖', WARN, '\n'.join(missing))
    else:
        check('Python 依赖', OK, 'pyyaml 已安装')

    # ---------------------------------------------------------- 3 hdc
    hdc = shutil.which('hdc') or shutil.which('hdc.exe')
    if not hdc:
        cand = _find_via_config() or _find_known_location()
        if cand:
            check('hdc 可执行文件', OK,
                  f'{cand}\n（来自项目内配置或常见安装位置，未修改任何系统设置）')
            hdc = cand
        else:
            check('hdc 可执行文件', BAD,
                  '未找到 hdc。请任选一种方式（都不会改系统环境变量）：\n'
                  '  A) 安装 DevEco Studio（自带 SDK 与 hdc）\n'
                  '  B) 运行 tools/fetch_sdk.py 获取 OpenHarmony SDK\n'
                  '  C) 运行 tools/configure_hdc.py --path <你的hdc路径>\n'
                  '  D) 代码里显式传参：Hdc(hdc_path=r"...\\hdc.exe")')
            return _summary()
    else:
        check('hdc 可执行文件', OK, f'{hdc}\n（来自系统 PATH）')

    # ---------------------------------------------------------- 4 hdc 版本
    try:
        r = subprocess.run([hdc, '-v'], capture_output=True, text=True, timeout=20)
        ver = (r.stdout or r.stderr).strip()
        check('hdc 可运行', OK, ver)
    except Exception as e:
        check('hdc 可运行', BAD, f'{type(e).__name__}: {e}')
        return _summary()

    # ---------------------------------------------------------- 5 设备
    try:
        r = subprocess.run([hdc, 'list', 'targets'], capture_output=True,
                           text=True, timeout=25)
        out = (r.stdout or '').strip()
        if not out or 'Empty' in out:
            check('设备连接', BAD,
                  '未检测到设备。请确认：\n'
                  '  1. 真机已用 USB 连接本机（或 hdc tconn <ip>:<port> 无线连接）\n'
                  '  2. 设备已开启「开发者模式」\n'
                  '  3. 设备已开启「USB 调试」\n'
                  '  4. 设备上已弹窗授权本机调试')
            return _summary()
        check('设备连接', OK, out.replace('\n', ' | '))
    except Exception as e:
        check('设备连接', BAD, f'{type(e).__name__}: {e}')
        return _summary()

    # ---------------------------------------------------------- 5b 设备时间
    # 开发板 RTC 未走时很常见（实测润和 DAYU200 出厂停留在 2017-08-05）。
    # 设备时间错会让报告时间戳、截图文件名、hilog 时序全部不可信，
    # 还会让任何「N 秒内应超时」这类断言失效。所以独立列一项。
    try:
        r = subprocess.run([hdc, 'shell', 'date'], capture_output=True,
                           text=True, timeout=20)
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        year_now = datetime.datetime.now().year
        m = re.search(r'\b(20\d{2})\b', out)
        if m and int(m.group(1)) == year_now:
            check('设备时间', OK, out)
        else:
            check('设备时间', WARN,
                  f'{out or "(无输出)"}\n'
                  f'-> 设备年份不是 {year_now}，报告时间戳与超时断言会失真。\n'
                  f'   修复: python tools/sync_device_time.py')
    except Exception as e:
        check('设备时间', WARN, f'{type(e).__name__}: {e}')

    # ---------------------------------------------------------- 6 uitest 通路
    # C8：uitest 不可用**不再直接终止自检** —— 写动作还有 uinput / sendevent
    # 两条备用通路（设计稿 §3.2 探测顺序 uitest → uinput → sendevent）。
    # 逐条探测并如实列出结论；三者皆无才落「不可交互」档。
    uitest_ok = False
    try:
        r = subprocess.run([hdc, 'shell', 'uitest --version'], capture_output=True,
                           text=True, timeout=25)
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        uitest_ok = (r.returncode == 0 and bool(out)
                     and 'not found' not in out.lower())
        if uitest_ok:
            check('uitest 命令行通路', OK,
                  f'{out}\n-> 技术路线：走轻量的 hdc 命令行通路（设备侧零部署）')
        else:
            check('uitest 命令行通路', BAD, f'设备不支持 uitest 命令：{out[:200]}')
    except Exception as e:
        check('uitest 命令行通路', BAD, f'{type(e).__name__}: {e}')

    if not uitest_ok:
        from ohauto.hdc import Hdc
        d = Hdc(hdc_path=hdc)
        rep = d.detect_backend()
        lines = [f'{p["name"]:<9} {"可用" if p["ok"] else "不可用"}  {p["detail"]}'
                 for p in rep['probes']]
        if rep['interactive']:
            check('输入注入通路（备用）', OK,
                  f'uitest 不可用，改用备用通路：{rep["selected"]}\n'
                  + '\n'.join(lines))
        else:
            check('输入注入通路（备用）', BAD,
                  '三个输入通路全部不可用 —— 置为「不可交互」档：只能做**只读观测**'
                  '（截图 / 控件树 / aa 拉起），任何写操作都会响亮失败\n'
                  + '\n'.join(lines))

    # ---------------------------------------------------------- 7 截图
    try:
        p = '/data/local/tmp/ohauto_doctor.png'
        r = subprocess.run([hdc, 'shell', f'uitest screenCap -p {p}'],
                           capture_output=True, text=True, timeout=30)
        r2 = subprocess.run([hdc, 'shell', f'ls -l {p}'],
                            capture_output=True, text=True, timeout=20)
        if p.split('/')[-1] in (r2.stdout or ''):
            check('截图能力', OK, (r2.stdout or '').strip()[:120])
        else:
            check('截图能力', WARN, f'截图后未确认到文件。返回：{(r.stdout+r.stderr)[:160]}')
    except Exception as e:
        check('截图能力', WARN, f'{type(e).__name__}: {e}')

    # ---------------------------------------------------------- 8 控件树
    try:
        p = '/data/local/tmp/ohauto_doctor.json'
        subprocess.run([hdc, 'shell', f'uitest dumpLayout -p {p}'],
                       capture_output=True, text=True, timeout=40)
        r2 = subprocess.run([hdc, 'shell', f'ls -l {p}'],
                            capture_output=True, text=True, timeout=20)
        if p.split('/')[-1] in (r2.stdout or ''):
            check('控件树导出', OK, (r2.stdout or '').strip()[:120] + '\n'
                  '-> 建议接着跑: python examples/dump_tree.py 核对真实 JSON 结构')
        else:
            check('控件树导出', WARN, '导出后未确认到文件')
    except Exception as e:
        check('控件树导出', WARN, f'{type(e).__name__}: {e}')

    # ---------------------------------------------------------- 9 应用拉起
    if args.bundle:
        try:
            r = subprocess.run([hdc, 'shell',
                                f'aa start -b {args.bundle} -a {args.ability}'],
                               capture_output=True, text=True, timeout=30)
            out = ((r.stdout or '') + (r.stderr or '')).strip()
            # ⚠️ 判据只能看**输出**，不能看 rc —— hdc shell 的 rc 表示
            # 「shell 通道本身成功」，aa start 拉起失败时 rc 照样是 0。
            # 原来写的是 `or r.returncode == 0`，等于恒通过（评审 #11）：
            # 应用根本没拉起来，doctor 也报 OK，误导后面所有真机步骤。
            # 真机实测失败输出含 "Error"/"Failed to start"；成功才含
            # "start ability successfully"（大小写不定，统一 lower）。
            low = out.lower()
            if 'start ability successfully' in low:
                check(f'拉起应用 {args.bundle}', OK, out[:160])
            else:
                check(f'拉起应用 {args.bundle}', WARN,
                      (out[:200] or '（无输出，拉起很可能失败）'))
        except Exception as e:
            check(f'拉起应用 {args.bundle}', WARN, f'{type(e).__name__}: {e}')

    return _summary()


def _summary() -> int:
    print()
    print('=' * 68)
    bad = [r for r in results if r[0] == BAD]
    warn = [r for r in results if r[0] == WARN]
    if bad:
        print(f'结论：环境未就绪（{len(bad)} 项失败，{len(warn)} 项警告）')
        print('请按上面的提示逐项处理。')
        return 1
    if warn:
        print(f'结论：环境基本就绪（{len(warn)} 项警告，不影响主流程）')
        return 0
    print('结论：环境完全就绪，可以开始跑自动化。')
    print()
    print('下一步：')
    print('  python examples/dump_tree.py --bundle <包名>    # 核对控件树结构')
    print('  python examples/smoke_test.py --bundle <包名>   # 跑最小闭环')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
