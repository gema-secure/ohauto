"""
OpenHarmony SDK 下载与提取
=========================
从华为云镜像下载 ohos-sdk-windows_linux-public.tar.gz，只提取 Windows 部分，
再解压出 toolchains（含 hdc）。无需登录，公开可用。

用法:
    python fetch_sdk.py             # 下载 + 提取 windows/toolchains
    python fetch_sdk.py --all       # 提取 windows 下全部子包
    python fetch_sdk.py --print     # 只打印 tar 成员清单，不下载（需完整流）
"""
import os, sys, ssl, json, tarfile, zipfile, time, urllib.request, io

VERSION   = '6.1-Release'
URL       = f'https://repo.huaweicloud.com/harmonyos/os/{VERSION}/ohos-sdk-windows_linux-public.tar.gz'
ROOT      = os.environ.get('OHAUTO_SDK_ROOT') or os.path.join(os.path.expanduser('~'), 'ohos-sdk')
DL_DIR    = os.path.join(ROOT, '_download')
OUT_DIR   = os.path.join(ROOT, VERSION)
TARBALL   = os.path.join(DL_DIR, 'ohos-sdk-windows_linux-public.tar.gz')
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE


def human(n):
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '%.1f %s' % (n, u)
        n /= 1024
    return '%.1f TB' % n


def download():
    os.makedirs(DL_DIR, exist_ok=True)
    if os.path.exists(TARBALL):
        sz = os.path.getsize(TARBALL)
        print('  已存在，复用: %s (%s)' % (TARBALL, human(sz)))
        return
    req = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        total = int(r.headers.get('Content-Length', 0))
        print('  开始下载 %s' % human(total))
        got, t0, last = 0, time.time(), 0
        with open(TARBALL + '.part', 'wb') as f:
            while True:
                chunk = r.read(4 * 1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if got - last >= 200 * 1024 * 1024:
                    sp = got / max(time.time() - t0, 0.01) / 1024**2
                    pct = got / total * 100 if total else 0
                    eta = (total - got) / max(got / max(time.time() - t0, 0.01), 1)
                    print('    %5.1f%%  %s / %s  %.0f MB/s  剩余约 %.0fs'
                          % (pct, human(got), human(total), sp, eta))
                    last = got
    os.replace(TARBALL + '.part', TARBALL)
    print('  下载完成: %s (%s)' % (TARBALL, human(os.path.getsize(TARBALL))))


def extract(want_all=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    picked = []
    print('  扫描 tar 成员（提取 windows/ 部分）...')
    with tarfile.open(TARBALL, mode='r|gz') as tf:
        for m in tf:
            if not m.isfile():
                continue
            name = m.name.split('/', 1)[-1]
            if not m.name.startswith('windows/'):
                continue
            if not want_all and 'toolchains' not in name:
                continue
            dest = os.path.join(OUT_DIR, os.path.basename(name))
            print('    提取 %-52s %s' % (os.path.basename(name), human(m.size)))
            with open(dest, 'wb') as out:
                src = tf.extractfile(m)
                while True:
                    b = src.read(4 * 1024 * 1024)
                    if not b:
                        break
                    out.write(b)
            picked.append(dest)
            if not want_all:
                break     # 只要 toolchains，拿到就停，省流量
    return picked


def unzip_zips(paths):
    results = []
    for p in paths:
        if not p.endswith('.zip'):
            continue
        target = os.path.join(OUT_DIR, 'extracted')
        os.makedirs(target, exist_ok=True)
        print('  解压 %s -> %s' % (os.path.basename(p), target))
        with zipfile.ZipFile(p) as z:
            z.extractall(target)
            names = z.namelist()
            results.append((p, target, len(names)))
    return results


def find_hdc():
    hits = []
    for base, _, files in os.walk(ROOT):
        for fn in files:
            if fn.lower() in ('hdc.exe', 'hdc', 'hdc_std.exe'):
                hits.append(os.path.join(base, fn))
    return hits


if __name__ == '__main__':
    print('=' * 62)
    print('OpenHarmony SDK 获取  |  版本 %s' % VERSION)
    print('=' * 62)
    want_all = '--all' in sys.argv

    print('\n[1/4] 下载')
    download()

    print('\n[2/4] 提取 tar 内的 windows 子包')
    zips = extract(want_all)

    print('\n[3/4] 解压 zip')
    unzip_zips(zips)

    print('\n[4/4] 定位 hdc')
    hits = find_hdc()
    if hits:
        for h in hits:
            print('  找到: %s' % h)
    else:
        print('  未找到 hdc，请检查提取结果')

    with open(os.path.join(ROOT, 'sdk_info.json'), 'w', encoding='utf-8') as f:
        json.dump({'version': VERSION, 'url': URL, 'root': ROOT,
                   'out_dir': OUT_DIR, 'hdc': hits}, f, ensure_ascii=False, indent=2)
    print('\n信息已写入 %s' % os.path.join(ROOT, 'sdk_info.json'))
