# -*- coding: utf-8 -*-
"""闭环演示一键重放 —— 注入缺陷 → 检出 → 修复 → 回归，全程可重放。

★ 这是干嘛的
------------

把在官方样例 BasicVibration 上手工走通的三幕闭环固化成脚本：

    第 0 幕 基线    声明 vs 运行时全对上（reset 在「确认存在」）
    第 1 幕 注入    源码仍声明 reset，但 visibility=None 让它不渲染
                    → 融合器检出 🔴 真缺失，指到源码出处
    第 2 幕 修复    还原源码 → 重建重装 → reset 回到「确认存在」

三幕的构建/签名/安装/采集/融合全部走项目现有工具
（hvigor + tools/sign_hap.py + hdc + tools/capture_app.py + tools/trifusion.py），
本脚本只做编排，不另立检测逻辑。

⚠️ 两条红线内化在代码里：
    * 注入方式是**运行时不渲染**（visibility=None），不是删声明 ——
      删声明等于把主张也删了，融合器无从比对（实测依据）；
    * 每幕的证据都落盘（截图 / 控件树 / 融合报告），不报没有产物的结论。

用法::

    python tools/demo_closed_loop.py \\
        --project "D:/project/appsamples/code/BasicFeature/DeviceManagement/Vibrator/BasicVibration" \\
        --bundle com.samples.etsvibrator --ability EntryAbility --page pages/Index

退出码：0 = 三幕全部符合预期；1 = 任一断言失败。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

#: 注入缺陷 = 给 reset 按钮加 visibility(None)（声明仍在，运行时不渲染）
INJECT_ANCHOR = "      Button('reset')\n        .width(200)"
INJECT_PATCH = "      Button('reset')\n        .visibility(Visibility.None)\n        .width(200)"
TARGET_FILE = os.path.join('entry', 'src', 'main', 'ets', 'common',
                           'TextTimerComponent.ets')
ORIGINAL_SUFFIX = '.orig'


def _tail(r: subprocess.CompletedProcess, n: int = 150) -> str:
    return ((r.stdout or b'') + (r.stderr or b'')).decode('utf-8', errors='replace')[-n:].strip()


class ClosedLoop:

    def _run(self, cmd: List[str], timeout: int = 300,
             cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           cwd=cwd or self.project)
        return r

    def __init__(self, project: str, bundle: str, ability: str, page: str,
                 hdc: str, serial: str, node: str, hvigorjs: str,
                 sdk_lib: str, out_dir: str):
        self.project = project
        self.bundle = bundle
        self.ability = ability
        self.page = page
        self.hdc = hdc
        self.serial = serial
        self.node = node
        self.hvigorjs = hvigorjs
        self.sdk_lib = sdk_lib
        self.out = out_dir
        self.target = os.path.join(project, TARGET_FILE)
        self.orig = self.target + ORIGINAL_SUFFIX
        self.hap_unsigned = os.path.join(
            project, 'entry', 'build', 'default', 'outputs', 'default',
            'entry-default-unsigned.hap')
        self.hap_signed = os.path.join(
            project, 'entry', 'build', 'default', 'outputs', 'default',
            'entry-default-signed.hap')
        self.env = dict(os.environ)
        self.env['DEVECO_SDK_HOME'] = 'D:\\ohos-sdk\\layout'
        self.timeline: List[Dict] = []

    # ------------------------------------------------------------ 幕内步骤

    def restore_source(self) -> None:
        """从 .orig 还原源码（保证每轮起点一致）。"""
        if not os.path.isfile(self.orig):
            shutil.copy(self.target, self.orig)
        shutil.copy(self.orig, self.target)
        print('  [源码] 已从 .orig 还原')

    def inject(self) -> None:
        s = open(self.target, encoding='utf-8').read()
        if INJECT_PATCH in s:
            print('  [源码] 缺陷已处于注入状态')
            return
        assert INJECT_ANCHOR in s, '找不到注入锚点（样例代码变了？）'
        open(self.target, 'w', encoding='utf-8').write(
            s.replace(INJECT_ANCHOR, INJECT_PATCH, 1))
        print('  [源码] 已注入 visibility=None 渲染缺陷')

    def build_sign_install_relaunch(self) -> None:
        r = self._run([self.node, self.hvigorjs, 'assembleHap', '--no-daemon'],
                      timeout=300)
        assert 'BUILD SUCCESSFUL' in _tail(r, 4000), '构建失败: ' + _tail(r, 400)
        print('  [构建] SUCCESSFUL')
        r = self._run([sys.executable, os.path.join(HERE, 'sign_hap.py'),
                       '--in', self.hap_unsigned, '--bundle', self.bundle,
                       '--sdk-lib', self.sdk_lib], timeout=180, cwd=HERE)
        assert os.path.isfile(self.hap_signed), '签名失败: ' + _tail(r, 400)
        print('  [签名] OK')
        for cmd in (['shell', 'aa', 'force-stop', self.bundle],
                    ['install', '-r', self.hap_signed],
                    ['shell', 'aa', 'start', '-a', self.ability,
                     '-b', self.bundle]):
            r = self._run([self.hdc, '-t', self.serial] + cmd, timeout=120,
                          cwd=None)
            time.sleep(2)
        print('  [装机] 已重装并拉起')
        time.sleep(3)

    def capture_and_fuse(self, name: str) -> Dict:
        """采集 + 融合，返回本幕判定结果。"""
        cap_json = os.path.join(ROOT, '_out', 'capture', name + '.json')
        r = self._run([sys.executable, os.path.join(HERE, 'capture_app.py'),
                       '--bundle', self.bundle, '--ability', self.ability,
                       '--name', name], timeout=180, cwd=ROOT)
        assert os.path.isfile(cap_json), '采集失败: ' + _tail(r, 400)
        md_path = os.path.join(self.out, f'trifusion_{name}.md')
        r = self._run([sys.executable, os.path.join(HERE, 'trifusion.py'),
                       '--static-project', self.project,
                       '--runtime-json', cap_json,
                       '--out', md_path], timeout=180, cwd=ROOT)
        md = open(md_path, encoding='utf-8').read()

        def cnt(label: str) -> int:
            m = re.search(r'\|\s*[^|]*' + label + r'[^|]*\|\s*(\d+)\s*\|', md)
            return int(m.group(1)) if m else -1

        seg_real = ''
        i = md.find('## 🔴 声明了但没出现')
        if i > -1:
            seg_real = md[i:md.find('##', i + 5)]
        res = {
            'act': name,
            'screenshot': os.path.join(ROOT, '_out', 'capture', name + '.png'),
            'report': md_path,
            'real_missing': cnt('真缺失'),
            'ondemand_missing': cnt('按需渲染控件未出现'),
            'reset_missing': f'`{self.page}`' not in md and False or
                             bool(re.search(r'\*\*`reset`\*\*', seg_real)),
            'reset_confirmed': bool(re.search(
                r'\| `reset` \| id \| static \| runtime \|', md)),
        }
        print(f"  [融合] 真缺失={res['real_missing']}  "
              f"按需={res['ondemand_missing']}  "
              f"reset缺失={res['reset_missing']}  reset确认={res['reset_confirmed']}")
        return res

    # ------------------------------------------------------------ 三幕

    def act(self, name: str, inject: bool) -> Dict:
        print(f'—— 第 {name} 幕 ——')
        if inject:
            self.inject()
        else:
            self.restore_source()
        self.build_sign_install_relaunch()
        res = self.capture_and_fuse(name)
        self.timeline.append(res)
        return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--project', required=True, help='样例 ArkTS 工程根目录')
    ap.add_argument('--bundle', required=True)
    ap.add_argument('--ability', required=True)
    ap.add_argument('--page', default='pages/Index')
    ap.add_argument('--hdc', default=r'C:\Program Files\Huawei\DevEco Studio'
                                     r'\sdk\default\openharmony\toolchains\hdc.exe')
    ap.add_argument('--serial', default='127.0.0.1:5555')
    ap.add_argument('--node', default=r'C:\Program Files\Huawei\DevEco Studio'
                                     r'\tools\node\node.exe')
    ap.add_argument('--hvigorjs', default=r'C:\Program Files\Huawei\DevEco Studio'
                                          r'\tools\hvigor\bin\hvigorw.js')
    ap.add_argument('--sdk-lib', default=r'D:\ohos-sdk\15\toolchains\lib')
    a = ap.parse_args()

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = os.path.join(ROOT, '_out', 'closed_loop', stamp)
    os.makedirs(out_dir, exist_ok=True)
    loop = ClosedLoop(a.project, a.bundle, a.ability, a.page, a.hdc, a.serial,
                      a.node, a.hvigorjs, a.sdk_lib, out_dir)

    base = loop.act('baseline', inject=False)
    defect = loop.act('inject', inject=True)
    fixed = loop.act('fix', inject=False)

    checks = [
        ('基线：reset 确认存在', base['reset_confirmed']),
        ('缺陷：reset 进真缺失', defect['reset_missing']),
        ('修复：reset 回到确认存在', fixed['reset_confirmed']),
    ]
    print('=' * 56)
    ok = True
    for label, passed in checks:
        print(f'  {"✅" if passed else "❌"} {label}')
        ok = ok and passed
    print('=' * 56)
    with open(os.path.join(out_dir, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump({'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
                   'timeline': loop.timeline, 'checks': checks, 'passed': ok},
                  f, ensure_ascii=False, indent=1)
    print(f'  产物目录: {out_dir}')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
