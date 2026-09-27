# -*- coding: utf-8 -*-
"""HAP 签名 —— 把 2026-09-21 手工走通的三步固化成一条命令。

背景
----
DevEco 的**自动签名对 OpenHarmony 无效**（那是 HarmonyOS 专属，要华为账号），
所以给 OpenHarmony 设备出包必须手动签。那天我是照着 SDK 材料手工敲的三步，
中间踩了两个必须知道的坑（见下），敲完没有留下可执行记录 ——
下次换机器/重装环境就得从头再来一遍。

这个脚本把三步串起来，并且把两个坑写进代码。

三步
----
1. 生成**应用证书链**（`generate-app-cert`）
   ⚠️ 坑 1：`-appCertFile` 要求的是**链**，只给一张叶子证书会被拒：
      `Profile cert 'xxx.pem' must a cert chain`
2. 签 **provisioning profile**（`sign-profile`）
   ⚠️ 坑 2：SDK 模板**不能直接用**：
      · `validity` 早已过期（debug 模板是 2021-01 ~ 2024-01）
      · 模板内嵌的 `development-certificate` 与 p12 里 `openharmony application release`
        的证书**不是同一张**（前者由 CA 签发、后者自签，序列号都不同），
        而设备是拿 profile 里内嵌证书的**公钥**去验 HAP 签名的 —— 必须一致
      所以 profile 要自己重写（见 `make_profile.py`）
3. 签 **HAP**（`sign-app`）

用法::

    # 一条命令签一个 HAP
    python tools/sign_hap.py --in entry-default-unsigned.hap

    # 主 HAP + 测试 HAP 一起签
    python tools/sign_hap.py --in a-unsigned.hap --in b-unsigned.hap --out-dir signed/

    # 只重做 profile（改了 bundleName / 换设备时）
    python tools/sign_hap.py --refresh-profile --bundle com.example.myapplication

前提：`--sdk-lib` 指向 DevEco 或 OpenHarmony SDK 的 `toolchains/lib`。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _local_cfg(key: str, default: str = '') -> str:
    """从项目内 hdc.config.json（gitignored）读本机配置；env 变量优先。

    签名材料/签名目录这类本机路径属于隐私项，不入源码 —— 写在本地配置里。
    """
    env = os.environ.get('OHAUTO_' + key.upper(), '')
    if env:
        return env
    cfg = os.path.join(ROOT, 'hdc.config.json')
    try:
        with open(cfg, encoding='utf-8') as f:
            return (json.load(f) or {}).get(key) or default
    except Exception:
        return default


#: SDK 自带的签名材料目录（按优先级找）。本机自定路径走 env
#: `OHAUTO_SDK_LIB` 或 hdc.config.json 的 `sdk_lib` —— 隐私项不入源码。
DEFAULT_SDK_LIBS = [
    _local_cfg('sdk_lib'),
    r'C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\lib',
]

KEYSTORE = 'OpenHarmony.p12'
STORE_PASS = '123456'                    # SDK 自带的固定口令
KEY_ALIAS = 'openharmony application release'
PROFILE_KEY_ALIAS = 'openharmony application profile release'
PROFILE_CERT = 'OpenHarmonyProfileRelease.pem'
PROFILE_TEMPLATE = 'UnsgnedDebugProfileTemplate.json'

CA_ISSUER = 'C=CN,O=OpenHarmony,OU=OpenHarmony Team,CN=OpenHarmony Application CA'
APP_SUBJECT = 'C=CN,O=OpenHarmony,OU=OpenHarmony Team,CN=OpenHarmony Application Release'


def find_java() -> str:
    exe = shutil.which('java')
    if exe:
        return exe
    for c in (r'C:\Program Files\Common Files\Oracle\Java\javapath\java.exe',
              r'D:\javacode\jdk-21\bin\java.exe'):
        if os.path.isfile(c):
            return c
    raise SystemExit('[err] 找不到 java —— 签名工具是 jar，需要 JRE')


def pick_sdk_lib(explicit: str = '') -> str:
    for p in ([explicit] if explicit else []) + DEFAULT_SDK_LIBS:
        if p and os.path.isfile(os.path.join(p, 'hap-sign-tool.jar')):
            return p
    raise SystemExit('[err] 找不到 hap-sign-tool.jar；用 --sdk-lib 指定 toolchains/lib'
                     '，或把 sdk_lib 写进 hdc.config.json')


def find_keytool(java: str, explicit: str = '') -> str:
    """定位 keytool。

    ⚠️ 不能靠 `dirname(dirname(java))` 推 —— PATH 上的 java 常来自
    `C:\\Program Files\\Common Files\\Oracle\\Java\\javapath\\`（一层符号链接目录），
    推出来的是 `...\\Oracle\\Java\\bin\\keytool.exe`，那是个**不存在的路径**。

    可靠办法是问 JVM 自己：`java -XshowSettings:properties -version` 会打印
    `java.home`，真实 JDK 根目录就在那儿。
    """
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise SystemExit(f'[err] 指定的 keytool 不存在：{explicit}')

    exe = 'keytool.exe' if os.name == 'nt' else 'keytool'

    # ① java 同目录（正常 JDK 布局）
    cand = os.path.join(os.path.dirname(java), exe)
    if os.path.isfile(cand):
        return cand

    # ② 问 JVM 要 java.home
    try:
        r = subprocess.run([java, '-XshowSettings:properties', '-version'],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=20)
        for line in ((r.stdout or '') + (r.stderr or '')).splitlines():
            if 'java.home' in line and '=' in line:
                jh = line.split('=', 1)[1].strip()
                cand = os.path.join(jh, 'bin', exe)
                if os.path.isfile(cand):
                    return cand
    except Exception:
        pass

    # ③ 常见位置兜底
    for c in (shutil.which('keytool') or '',
              r'D:\javacode\jdk-21\bin\keytool.exe'):
        if c and os.path.isfile(c):
            return c
    raise SystemExit('[err] 找不到 keytool —— 用 --keytool 显式指定')


def run(cmd: list, keep: bool = False) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    out = (r.stdout or '') + (r.stderr or '')
    if r.returncode != 0 or 'ERROR' in out:
        print(out[-2500:])
        raise SystemExit(f'[err] 失败：{os.path.basename(cmd[0])} …')
    if not keep:
        for line in out.splitlines():
            if 'INFO' in line and ('success' in line or 'exist' in line):
                print('    ' + line.strip())
    return out


def step1_make_chain(java, lib, sign_dir, keytool: str) -> str:
    """生成应用证书链（3 张：叶子 + Application CA + Root CA）。"""
    out = os.path.join(sign_dir, 'OpenHarmonyApplication.pem')
    root = os.path.join(sign_dir, 'root-ca.cer')
    sub = os.path.join(sign_dir, 'sub-app-ca.cer')

    if not os.path.isfile(keytool):
        raise SystemExit(f'[err] 找不到 keytool：{keytool}')
    ks = os.path.join(lib, KEYSTORE)
    for alias, dest in (('openharmony application root ca', root),
                        ('openharmony application ca', sub)):
        run([keytool, '-exportcert', '-alias', alias, '-keystore', ks,
             '-storepass', STORE_PASS, '-storetype', 'PKCS12', '-rfc',
             '-file', dest])

    run([java, '-jar', os.path.join(lib, 'hap-sign-tool.jar'), 'generate-app-cert',
         '-keyAlias', KEY_ALIAS, '-keyPwd', STORE_PASS,
         '-issuer', CA_ISSUER, '-issuerKeyAlias', 'openharmony application ca',
         '-issuerKeyPwd', STORE_PASS, '-subject', APP_SUBJECT,
         '-validity', '3650', '-signAlg', 'SHA256withECDSA',
         '-rootCaCertFile', root, '-subCaCertFile', sub,
         '-keystoreFile', ks, '-keystorePwd', STORE_PASS,
         '-outForm', 'certChain', '-outFile', out])
    n = open(out, encoding='utf-8').read().count('BEGIN CERTIFICATE')
    print(f'  [1/3] 应用证书链 -> {out}  （{n} 张证书）')
    return out


def step2_make_profile(java, lib, sign_dir, bundle: str, udid: str) -> str:
    """重写模板并签 profile。模板的 validity 与内嵌证书都必须换掉。"""
    tpl = os.path.join(lib, PROFILE_TEMPLATE)
    with open(tpl, encoding='utf-8') as fh:
        prof = json.load(fh)

    pem_path = os.path.join(sign_dir, 'OpenHarmonyApplication.pem')
    pem = open(pem_path, encoding='utf-8').read()
    leaf = re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                      pem, re.S)[0].strip()

    now = int(time.time())
    prof['validity'] = {'not-before': now, 'not-after': now + 365 * 86400}
    prof['bundle-info']['bundle-name'] = bundle
    # 必须内嵌**我们自己的叶子证书**（设备拿它的公钥验 HAP 签名）
    prof['bundle-info']['development-certificate'] = leaf + '\n'
    if udid:
        prof['debug-info']['device-ids'] = [udid]

    pj = os.path.join(sign_dir, 'profile.json')
    with open(pj, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(prof, fh, indent=4, ensure_ascii=False)

    out = os.path.join(sign_dir, 'profile.p7b')
    run([java, '-jar', os.path.join(lib, 'hap-sign-tool.jar'), 'sign-profile',
         '-mode', 'localSign',
         '-keyAlias', PROFILE_KEY_ALIAS, '-keyPwd', STORE_PASS,
         '-profileCertFile', os.path.join(lib, PROFILE_CERT),
         '-inFile', pj, '-signAlg', 'SHA256withECDSA',
         '-keystoreFile', os.path.join(lib, KEYSTORE), '-keystorePwd', STORE_PASS,
         '-outFile', out])
    print(f'  [2/3] profile -> {out}  （bundle={bundle}）')
    return out


def step3_sign_haps(java, lib, sign_dir, haps: list, out_dir: str) -> list:
    """用应用私钥签 HAP。"""
    app_cert = os.path.join(sign_dir, 'OpenHarmonyApplication.pem')
    profile = os.path.join(sign_dir, 'profile.p7b')
    signed = []
    for h in haps:
        name = os.path.basename(h)
        dest = os.path.join(out_dir, name.replace('-unsigned', '-signed'))
        run([java, '-jar', os.path.join(lib, 'hap-sign-tool.jar'), 'sign-app',
             '-mode', 'localSign',
             '-keyAlias', KEY_ALIAS, '-keyPwd', STORE_PASS,
             '-appCertFile', app_cert, '-profileFile', profile,
             '-inFile', os.path.abspath(h), '-signAlg', 'SHA256withECDSA',
             '-keystoreFile', os.path.join(lib, KEYSTORE), '-keystorePwd', STORE_PASS,
             '-outFile', os.path.abspath(dest)])
        size = os.path.getsize(dest) if os.path.isfile(dest) else 0
        print(f'  [3/3] {name} -> {dest}  （{size} B）')
        signed.append(dest)
    return signed


def device_udid(hdc: str = '', target: str = '') -> str:
    """取本机 UDID 用于 profile。

    ★ 这一步**不能省**。实测（2026-09-22）：UDID 取空时 profile 会保留
    SDK 模板里的**旧 device-ids**，设备直接拒绝安装：

        error: failed to install bundle. code:9568322
        error: signature verification failed due to not trusted app source

    ⚠️ 那句报错**完全没提 UDID/device-ids**，只说「来源不受信任」，
    极易被误判成证书链或 profile 签名有问题 —— 所以这里取不到就显式告警。

    ⚠️ hdc 通道会间歇报 `E000004 The communication channel is being
    established`，必须重试。
    """
    hdc = hdc or shutil.which('hdc') or os.environ.get('HDC_PATH') or ''
    if not hdc:
        for c in (r'C:\Program Files\Huawei\DevEco Studio\sdk\default'
                  r'\openharmony\toolchains\hdc.exe',):
            if os.path.isfile(c):
                hdc = c
                break
    if not hdc:
        return ''
    cmd = [hdc] + (['-t', target] if target else []) + ['shell', 'bm', 'get', '-u']
    for _ in range(6):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=30)
            out = (r.stdout or '') + (r.stderr or '')
            if 'communication channel' in out or 'connect-key' in out:
                time.sleep(4)
                continue
            m = re.search(r'\b([0-9A-Fa-f]{64})\b', out)
            if m:
                return m.group(1)
        except Exception:
            pass
        time.sleep(2)
    return ''


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='haps', action='append', default=[],
                    help='未签名的 .hap（可重复）')
    ap.add_argument('--out-dir', default='', help='签名产物目录（默认与输入同目录）')
    ap.add_argument('--sdk-lib', default='', help='SDK 的 toolchains/lib')
    ap.add_argument('--sign-dir', default='', help='签名材料工作目录')
    ap.add_argument('--bundle', default='', help='被测应用 bundleName')
    ap.add_argument('--udid', default='', help='设备 UDID（默认自动取）')
    ap.add_argument('--target', default='',
                    help='hdc 设备序列号（多设备时指定，仅用于取 UDID）')
    ap.add_argument('--keytool', default='', help='keytool 路径')
    ap.add_argument('--refresh-profile', action='store_true',
                    help='强制重做证书链与 profile')
    args = ap.parse_args(argv)

    java = find_java()
    lib = pick_sdk_lib(args.sdk_lib)
    sign_dir = (args.sign_dir or _local_cfg('sign_dir')
                or os.path.join(ROOT, '_out', 'sign'))
    os.makedirs(sign_dir, exist_ok=True)

    print('=' * 66)
    print('  HAP 签名')
    print('=' * 66)
    print(f'  SDK lib   {lib}')
    print(f'  材料目录  {sign_dir}')

    bundle = args.bundle or 'com.example.myapplication'
    chain = os.path.join(sign_dir, 'OpenHarmonyApplication.pem')
    profile = os.path.join(sign_dir, 'profile.p7b')
    # ⚠️ 两个都要检查：只查证书链的话，profile 缺失时会跳过第 2 步、
    # 然后在第 3 步才报 "profile.p7b not exist"，白跑一趟。
    need = (args.refresh_profile
            or not os.path.isfile(chain) or not os.path.isfile(profile))
    if not need:
        print('  证书链与 profile 都已存在 → 跳过第 1、2 步'
              '（要重做加 --refresh-profile）')

    if need:
        kt = find_keytool(java, args.keytool)
        step1_make_chain(java, lib, sign_dir, kt)
        udid = args.udid or device_udid(target=args.target)
        if udid:
            print(f'  profile 内嵌 UDID: {udid}')
        else:
            print('  ⚠️ 未取到设备 UDID —— profile 会沿用 SDK 模板里的旧 device-ids，')
            print('     装机会报 code:9568322 "signature verification failed due to')
            print('     not trusted app source"（报错不提 UDID，很容易误判成证书问题）。')
            print('     可用 --udid 手工指定，或确认设备已连接。')
        step2_make_profile(java, lib, sign_dir, bundle, udid)

    if not args.haps:
        print('\n没有传 --in，只做了材料准备。')
        return 0

    out_dir = args.out_dir or os.path.dirname(os.path.abspath(args.haps[0]))
    os.makedirs(out_dir, exist_ok=True)
    signed = step3_sign_haps(java, lib, sign_dir, args.haps, out_dir)

    print()
    print('  签名完成。装机：')
    for s in signed:
        # ⚠️ hdc 的本地路径参数按「相对 cwd」解析 —— 必须给纯文件名或先 cd
        print(f'    cd "{os.path.dirname(s)}" && hdc install -r "{os.path.basename(s)}"')
    return 0


if __name__ == '__main__':
    sys.exit(main())
