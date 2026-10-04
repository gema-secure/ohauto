"""
配置 hdc 路径 —— 只写项目内配置文件，不碰系统任何设置
=====================================================

**本脚本不会修改注册表、PATH 或任何环境变量。**
它只在项目内写一个 hdc.config.json，ohauto 启动时读取该文件定位 hdc。

用法:
    python configure_hdc.py                          # 自动探测常见位置
    python configure_hdc.py --path "D:/xxx/hdc.exe"  # 显式指定
    python configure_hdc.py --show                   # 只看当前配置，不写
    python configure_hdc.py --clear                  # 删除配置文件
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)                    # 仓库根（tools/ 的上一级）
TARGETS = [
    os.path.join(PROJECT_ROOT, 'ohauto', 'hdc.config.json'),
    os.path.join(PROJECT_ROOT, 'hdc.config.json'),
]

COMMON = [
    r'C:\Users\{u}\ohos-sdk\*\extracted\toolchains\hdc.exe',
    r'C:\Users\{u}\ohos-sdk\extracted\toolchains\hdc.exe',
    r'C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe',
    r'C:\Users\{u}\AppData\Local\Huawei\Sdk\openharmony\*\toolchains\hdc.exe',
    r'C:\Users\{u}\AppData\Local\OpenHarmony\Sdk\*\toolchains\hdc.exe',
]


def find_candidates():
    found = []
    # PATH 中（若已有，其实不需要配置文件）
    for name in ('hdc', 'hdc.exe'):
        p = shutil.which(name)
        if p:
            found.append(('PATH 中', p))
    # 常见位置
    u = os.environ.get('USERNAME', '')
    for pat in COMMON:
        for hit in glob.glob(pat.format(u=u)):
            if os.path.isfile(hit) and ('常见位置', hit) not in found:
                found.append(('常见位置', hit))
    # 项目内可能解压出来的
    for pat in [os.path.join(PROJECT_ROOT, '**', 'toolchains', 'hdc.exe'),
                os.path.join(PROJECT_ROOT, '**', 'hdc.exe')]:
        for hit in glob.glob(pat, recursive=True):
            if os.path.isfile(hit) and ('项目内', hit) not in found:
                found.append(('项目内', hit))
    return found


def current():
    for t in TARGETS:
        if os.path.isfile(t):
            try:
                with open(t, 'r', encoding='utf-8') as f:
                    return t, json.load(f)
            except (OSError, ValueError):
                continue
    return None, None


def verify(hdc_path):
    """实际跑一下 hdc -v，确认可用。"""
    try:
        r = subprocess.run([hdc_path, '-v'], capture_output=True,
                           text=True, timeout=20)
        out = ((r.stdout or '') + (r.stderr or '')).strip()
        return (r.returncode == 0), out
    except Exception as e:
        return False, f'{type(e).__name__}: {e}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--path', default=None, help='显式指定 hdc 路径')
    ap.add_argument('--show', action='store_true', help='只显示当前配置')
    ap.add_argument('--clear', action='store_true', help='删除配置文件')
    ap.add_argument('--out', default=None, help='指定写入哪个配置文件')
    args = ap.parse_args()

    print('=' * 66)
    print('  hdc 路径配置（仅写项目内文件，不修改系统设置）')
    print('=' * 66)
    print()

    if args.clear:
        n = 0
        for t in TARGETS:
            if os.path.isfile(t):
                os.remove(t)
                print(f'  已删除 {t}')
                n += 1
        if not n:
            print('  没有找到配置文件')
        print()
        print('  说明：若 hdc 已在系统 PATH 中，ohauto 照样能找到，无需本文件。')
        return 0

    cur_path, cur_cfg = current()
    print('[当前配置]')
    if cur_cfg:
        print(f'  文件    : {cur_path}')
        print(f'  hdc_path: {cur_cfg.get("hdc_path")}')
    else:
        print('  尚未创建配置文件')
    print()

    if args.show:
        return 0

    # ---------------------------------------------------------- 选定路径
    target = args.path
    if not target:
        cands = find_candidates()
        if not cands:
            print('[探测结果] 未找到任何 hdc')
            print()
            print('请二选一：')
            print('  A) 安装 DevEco Studio（自带 SDK 与 hdc）')
            print('  B) 运行 tools/fetch_sdk.py 下载 OpenHarmony 公开 SDK')
            print('然后重跑本脚本，或用 --path 显式指定。')
            return 1
        print('[探测到以下 hdc]')
        for i, (src, p) in enumerate(cands, 1):
            ok, ver = verify(p)
            print(f'  {i}. [{src}] {p}')
            print(f'      {"可用" if ok else "不可用"}: {ver[:70]}')
        print()
        # 优先选可用且非 PATH 的（PATH 中的其实不需要配置）
        usable = [(s, p) for s, p in cands if verify(p)[0]]
        if not usable:
            print('探测到的 hdc 都跑不起来，请用 --path 显式指定。')
            return 1
        target = usable[0][1]
        print(f'自动选用: {target}')
        print()

    if not os.path.isfile(target):
        print(f'[错误] 路径不存在: {target}')
        return 1

    ok, ver = verify(target)
    print(f'[验证] {"可用" if ok else "不可用"}: {ver}')
    if not ok:
        print('该 hdc 无法运行，未写入配置。')
        return 1

    # ---------------------------------------------------------- 写入
    out = args.out or TARGETS[0]
    payload = {
        'hdc_path': target.replace('\\', '/'),
        '_comment': '仅项目内生效。由 tools/configure_hdc.py 生成，不涉及系统设置。',
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print()
    print(f'[已写入] {out}')
    print(f'  hdc_path = {payload["hdc_path"]}')
    print()
    print('说明：')
    print('  * 这个文件只在项目内生效，不影响系统环境变量或其他软件')
    print('  * ohauto 启动时会自动读取它定位 hdc')
    print('  * 想撤销直接删掉该文件即可（或跑 --clear）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
