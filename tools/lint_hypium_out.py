# -*- coding: utf-8 -*-
"""导出产物（.ets）静态规则检查 —— 守住 ArkTS/hypium 那几条会炸编译的规则。

为什么需要它
------------
2026-09-21/22 两天里，`tools/export_hypium.py` 一共被抓出 **5 个 bug**，
每一个都是「生成了代码、Python 侧测试全绿，但真机编译/运行才炸」：

| # | 问题 | 真机报错 |
|---|---|---|
| 1 | 定位链写成 `On.`（应为 `ON.`） | `Property 'text' does not exist on type 'typeof On'` |
| 2 | hypium 回调用了 `function () {}` | `arkts-no-func-expressions` |
| 3 | `click()` / `inputText()` 漏了外层 `await` | `uitest-api dose not allow calling concurrently` |
| 4 | `screenCap` 写到 `/data/local/tmp` | `Invalid file path:/data/local/tmp/...` |

问题在于：**CI 里没法编译 ArkTS**（要拉 4 GB SDK，太重）。
于是这些错误一路裸奔到真机才暴露，每次都要重新编译+签名+装机才能发现。

这个脚本用**纯文本规则**把已知的 4 类钉死 —— 几十行、零依赖、秒级，
可以直接进 CI。它挡不住"未知的新坑"，但能挡住**已经踩过的每一个**。

用法::

    python tools/lint_hypium_out.py                    # 扫默认产物目录
    python tools/lint_hypium_out.py path/to/x.ets ...  # 扫指定文件
    python tools/lint_hypium_out.py --dir somedir      # 扫目录下所有 .ets

退出码：0 = 干净，1 = 发现问题。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DEFAULT_DIRS = [
    os.path.join(ROOT, 'hypium_out'),
    os.path.join(ROOT, '..', 'ohauto-hypium-test', 'entry', 'src', 'ohosTest', 'ets', 'test'),
]


class Issue:
    def __init__(self, path, line_no, rule, line, why):
        self.path, self.line_no = path, line_no
        self.rule, self.line, self.why = rule, line.strip(), why

    def __str__(self):
        return (f'  {self.path}:{self.line_no}  [{self.rule}]\n'
                f'      {self.line}\n'
                f'      → {self.why}')


# ---------------------------------------------------------------- 规则

#: 1) 定位链必须大写 ON —— `On` 是类且只有实例方法（无 static）
RE_ON_LOWER = re.compile(r'(?<![\w.])On\.(text|id|type|descr|clickable|'
                         r'longClickable|scrollable|enabled|focused|selected|'
                         r'checked|checkable|isBefore|isAfter|within|inWindow)\s*\(')

#: 2) ArkTS 禁止 function expression（arkts-no-func-expressions）
RE_FUNC_EXPR = re.compile(r'function\s*\(\s*\)\s*\{')

#: 3) Promise 操作必须被 await 接住。
#:    形态一：`(await driver.findComponent(...)).click()` —— 内层 await 了，外层没有
RE_UNSAWN_CHAIN = re.compile(r'^\(\s*await\s+[\w.]+\([^)]*\)\s*\)\s*\.\s*(\w+)\s*\(')
#:    形态二：裸调用 `driver.findComponent(...).click()`
RE_BARE_CALL = re.compile(r'(?<!await\s)(?<!\(\s)(?:driver|self\.driver)\.'
                          r'findComponent\([^)]*\)\s*\.\s*(click|doubleClick|'
                          r'longClick|inputText)\s*\(')
#:    形态三：裸 `driver.xxx(` 且该 xxx 返回 Promise<void>
PROMISE_VOID_METHODS = ('click', 'doubleClick', 'longClick', 'inputText',
                        'pressBack', 'screenCap', 'delayMs', 'swipe',
                        'triggerKey', 'fling')
RE_BARE_PROMISE = re.compile(
    r'^\s*(?:driver|self\.driver)\.(' + '|'.join(PROMISE_VOID_METHODS) +
    r')\s*\(')

#: 4) screenCap 只能写应用沙箱
RE_SCREENCAP_BAD_PATH = re.compile(r'screenCap\s*\(\s*[\'"][^\'"]*/(?:data|system)/')


def lint_text(path: str, text: str) -> list:
    issues = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip('\n')
        stripped = line.strip()
        # 注释行不参与（导出器会在注释里说明"别写成这样"）
        if stripped.startswith('//'):
            continue

        if RE_ON_LOWER.search(line):
            issues.append(Issue(path, i, 'ON-CASE', line,
                                '定位链必须用大写 ON —— On 是类且没有静态方法，'
                                '真机报 Property \'text\' does not exist on type \'typeof On\''))
        if RE_FUNC_EXPR.search(line):
            issues.append(Issue(path, i, 'ARROW-FN', line,
                                'ArkTS 禁止 function expression（arkts-no-func-expressions）；'
                                'describe/it 回调必须写成箭头函数'))
        m = RE_UNSAWN_CHAIN.search(stripped)
        if m:
            issues.append(Issue(path, i, 'AWAIT-OUTER', line,
                                f'外层 await 缺失 —— .{m.group(1)}() 返回 Promise，'
                                '不 await 会与下一条操作并发，真机报 '
                                'uitest-api dose not allow calling concurrently'))
        if RE_BARE_CALL.search(line):
            issues.append(Issue(path, i, 'AWAIT-OUTER', line,
                                'click/inputText 前面必须有 await（形态：await (await ...).click();）'))
        m2 = RE_BARE_PROMISE.search(line)
        if m2 and 'await' not in stripped:
            issues.append(Issue(path, i, 'AWAIT-MISSING', line,
                                f'driver.{m2.group(1)}() 返回 Promise<void>，必须 await'))
        if RE_SCREENCAP_BAD_PATH.search(line):
            issues.append(Issue(path, i, 'SCREENCAP-PATH', line,
                                'screenCap 只能写应用自己的沙箱；写 /data/local/tmp 等路径'
                                '真机报 Invalid file path。用 delegator.getAppContext().filesDir'))
    return issues


def iter_ets(paths: list, dirs: list) -> list:
    out = list(paths)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith('.ets'):
                out.append(os.path.join(d, name))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='*', help='要检查的 .ets 文件')
    ap.add_argument('--dir', action='append', default=[],
                    help='要检查的目录（可重复；默认扫 hypium_out 与测试工程）')
    args = ap.parse_args(argv)

    files = iter_ets(args.files, args.dir or ([] if args.files else DEFAULT_DIRS))
    if not files:
        print('没有找到要检查的 .ets —— 先跑一次 tools/export_hypium.py')
        return 0

    all_issues = []
    for p in files:
        try:
            with open(p, encoding='utf-8') as fh:
                text = fh.read()
        except OSError as e:
            print(f'  [跳过] {p}: {e}')
            continue
        all_issues.extend(lint_text(p, text))

    print(f'检查 {len(files)} 个 .ets 文件')
    if not all_issues:
        print('  ✓ 干净：4 类已知的 ArkTS/hypium 规则全部满足')
        return 0

    print(f'\n发现 {len(all_issues)} 处问题：\n')
    for it in all_issues:
        print(it)
    print('\n这些规则对应的都是「Python 侧测试全绿、真机才炸」的坑，'
          '修完记得重跑 tools/e2e_smoke_real.py。')
    return 1


if __name__ == '__main__':
    sys.exit(main())
