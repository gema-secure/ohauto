"""Python 侧桥接：调用 Node 的 ArkTS 静态分析器。

**为什么这一段要两个语言各写一半**
------------------------------------
本项目的主控是 Python（AI/ML、编排、图像处理），但「读 ArkTS 源码」这件
事在 Node 生态里更顺手 —— 调研发现业界同类项目都这么切：
HapTest 的静态分析模块、HmTest 的 arkanalyzer（npm）、OpenHarmony 官方
工具链（ohpm/hvigor）全在 Node 侧。所以：

    Node（ohauto/static_arkts/analyze.mjs）  负责「读源码」
        ↓ JSON
    Python（本模块）                          负责「用起来」

两边只靠一份 JSON 契约耦合，谁都不必知道对方内部怎么实现。

**降级原则**：Node 找不到 / 脚本失败时**返回 None 并留下原因**，
绝不让静态分析这一环把主流程搞崩 —— 它是增强项，不是必选项。

命令行::

    python -m ohauto.static_arkts.bridge <工程根目录> [-o out.json]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYZER = os.path.join(HERE, 'analyze.mjs')

#: 依次尝试的 node 可执行文件（托管版优先，再退 PATH，再退系统 Node）
NODE_CANDIDATES: List[str] = [
    os.path.expandvars(
        r'%USERPROFILE%\.workbuddy\binaries\node\versions\22.22.2-3\node.exe'),
    'node',
    'node.exe',
]


def find_node() -> Optional[str]:
    """找到可用的 node，找不到返回 None。"""
    for cand in NODE_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.isfile(cand):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def analyze_project(root: str, *, timeout: int = 60) -> Optional[Dict[str, Any]]:
    """分析一个 ArkTS 工程，返回结构化结果；做不了就返回 None。

    Returns
    -------
    dict | None
        None 表示**没做分析**（Node 不可用 / 脚本失败），原因见 `last_error()`。
        注意区分「返回 None」与「分析成功但没有控件」—— 后者是一个正常的
        `controls: []` 结果。
    """
    node = find_node()
    if not node:
        _LAST_ERROR.append('未找到 node（已尝试托管版 / PATH / node.exe）')
        return None
    if not os.path.isdir(root):
        _LAST_ERROR.append(f'工程目录不存在: {root}')
        return None

    try:
        proc = subprocess.run(
            [node, ANALYZER, root],
            capture_output=True, text=True, encoding='utf-8',
            timeout=timeout, cwd=HERE)
    except (OSError, subprocess.SubprocessError) as e:
        _LAST_ERROR.append(f'调用 node 失败: {type(e).__name__}: {e}')
        return None

    if proc.returncode != 0:
        _LAST_ERROR.append(f'分析器返回 {proc.returncode}: '
                           f'{(proc.stderr or proc.stdout or "")[:300]}')
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        _LAST_ERROR.append(f'分析器输出不是合法 JSON: {e}')
        return None


_LAST_ERROR: List[str] = []


def last_error() -> str:
    """最近一次失败的原因（没有则空串）。"""
    return _LAST_ERROR[-1] if _LAST_ERROR else ''


# ---------------------------------------------------------------- 便捷视图

def page_hints(info: Optional[Dict[str, Any]]) -> List[str]:
    """源码里声明的页面路由 —— 可用于**引导探索**优先访问这些页面。"""
    if not info:
        return []
    return list(info.get('pages') or [])


def control_hints(info: Optional[Dict[str, Any]],
                  *, test_code: bool = False) -> List[Dict[str, Any]]:
    """源码里出现的控件（id / 文案 / 是否可点 / 是否危险）。

    test_code=False 时过滤掉 `ohosTest/` 下的测试代码引用 —— 那是测试脚本
    里的选择器，不是被测 UI 的声明（虽然常常能对上，但口径不同）。
    """
    if not info:
        return []
    out = []
    for c in info.get('controls') or []:
        f = str(c.get('file') or '')
        if not test_code and ('ohosTest' in f or f.endswith('.test.ets')):
            continue
        out.append(c)
    return out


def dangerous_controls(info: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """源码里命中的危险控件 —— 探索前就该知道哪些不能点。"""
    if not info:
        return []
    return [c for c in (info.get('controls') or []) if c.get('dangerous')]


def catalog_lines(info: Optional[Dict[str, Any]], limit: int = 60) -> List[str]:
    """把静态分析结果压成「控件清单」的行，供提示词使用。

    与 `ohauto.treesum.control_catalog` 的输出是**互补**的两份：
      * treesum   = 运行时控件树（此刻界面上有什么）
      * 本函数    = 静态源码（工程里声明了什么）
    运行时看不到但源码里有的控件（比如还没进入的页面），只有静态分析能给。
    """
    lines: List[str] = []
    for c in control_hints(info)[:limit]:
        parts = [c.get('id') or '', c.get('text') or '']
        tag = []
        if c.get('clickable'):
            tag.append('clickable')
        if c.get('dangerous'):
            tag.append('DANGEROUS')
        s = ' '.join(x for x in parts if x)
        if tag:
            s += f"  [{'/'.join(tag)}]"
        if s:
            lines.append(s)
    return lines


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description='ArkTS 静态分析（Node + Python 双语）')
    ap.add_argument('root', help='ArkTS 工程根目录')
    ap.add_argument('-o', '--out', help='结果落盘路径（JSON）')
    args = ap.parse_args()

    info = analyze_project(args.root)
    if info is None:
        print('⚠️ 静态分析未执行：%s' % (last_error() or '未知原因'))
        print('   （这是降级，不是故障 —— 静态分析是增强项，不影响主流程）')
        return 1

    print('页面路由 : %s' % (info.get('pages') or []))
    print('源码跳转 : %s' % (info.get('routes_in_source') or []))
    print('abilities: %s' % (info.get('abilities') or []))
    hints = control_hints(info)
    print('控件     : %d 个（已排除测试代码）' % len(hints))
    for c in hints[:10]:
        print('   %-28s %s' % (c.get('id') or c.get('text') or '?',
                               '可点击' if c.get('clickable') else ''))
    if dangerous_controls(info):
        print('⚠️ 危险控件: %s' % [c.get('id') or c.get('text')
                                  for c in dangerous_controls(info)])
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print('已写入 %s' % args.out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
