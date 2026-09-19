"""
核对真实控件树结构 —— 接上真机后的第一步
=======================================

各家 OpenHarmony 版本的 dumpLayout JSON 字段命名存在差异。本脚本把真实
控件树抓下来并做结构分析，用于核对 ohauto/layout.py 里的字段别名表是否
需要微调。

用法:
    python examples/dump_tree.py --bundle com.example.app
    python examples/dump_tree.py --bundle com.example.app --raw     # 只存原始 JSON
    python examples/dump_tree.py --from-file tree.json              # 离线分析已有 JSON
"""
import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto.hdc import Hdc, HdcError            # noqa: E402
from ohauto.layout import (ATTR_ALIASES, CHILD_KEYS, ATTR_KEYS,   # noqa: E402
                           flatten, parse_layout)

OUT = os.path.join(HERE, '_out')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bundle', default='', help='先拉起该应用再抓取')
    ap.add_argument('--ability', default='EntryAbility')
    ap.add_argument('--raw', action='store_true', help='只保存原始 JSON，不做解析')
    ap.add_argument('--hdc', default=None, help='hdc 路径（默认自动查找）')
    ap.add_argument('--from-file', default=None,
                    help='离线分析已有的控件树 JSON，不连设备（便于无真机时核对字段）')
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)

    if args.from_file:
        local = os.path.abspath(args.from_file)
        if not os.path.isfile(local):
            print(f'文件不存在: {local}')
            return 1
        print(f'离线模式：分析已有控件树 {local}')
    else:
        hdc = Hdc(hdc_path=args.hdc)
        print(f'hdc: {hdc.hdc_path}')
        print(f'目标: {hdc.target or "(默认设备)"}')

        try:
            targets = hdc.list_targets()
            if not targets:
                print('未检测到设备。请在真机上开启开发者模式与 USB 调试后重试。')
                return 1
            print(f'在线设备: {targets}')
            if not hdc.target:
                hdc.target = targets[0] or None
        except HdcError as e:
            print(f'检测设备失败: {e}')
            return 1

        if args.bundle:
            print(f'拉起应用 {args.bundle} ...')
            hdc.start_ability(args.bundle, args.ability)
            import time
            time.sleep(3)

        # ------------------------------------------------------ 抓取
        print('导出控件树 ...')
        dev = hdc.dump_layout(with_attrs=True, unfiltered=False)
        local = os.path.join(OUT, 'real_tree.json')
        try:
            hdc.pull(dev, local)
        except HdcError as e:
            print(f'拉取失败: {e}')
            return 1
        print(f'原始 JSON: {local}  ({os.path.getsize(local)} bytes)')

    if args.raw:
        return 0

    # ---------------------------------------------------------- 结构分析
    with open(local, 'r', encoding='utf-8', errors='replace') as f:
        raw_text = f.read()
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f'JSON 解析失败: {e}')
        print('前 500 字符:')
        print(raw_text[:500])
        return 1

    print()
    print('=' * 66)
    print('  结构分析（用于核对 layout.py 的字段别名表）')
    print('=' * 66)

    keys_top = list(data.keys()) if isinstance(data, dict) else [f'<{type(data).__name__}>']
    print(f'\n顶层键: {keys_top}')

    # 收集所有节点出现过的键
    key_counter = Counter()

    def walk_raw(n, depth=0):
        if not isinstance(n, dict):
            return
        key_counter.update(n.keys())
        # 记录嵌套一层 attributes 的键
        for ak in ATTR_KEYS:
            if isinstance(n.get(ak), dict):
                key_counter.update(k for k in n[ak].keys())
        for ck in CHILD_KEYS:
            kids = n.get(ck)
            if isinstance(kids, list):
                for c in kids:
                    walk_raw(c, depth + 1)
                break

    walk_raw(data)

    print(f'\n所有节点出现过的字段（共 {len(key_counter)} 种）:')
    for k, c in key_counter.most_common():
        mark = ''
        for sem, alts in ATTR_ALIASES.items():
            if k in alts:
                mark = f'  -> 语义: {sem}'
                break
        if k in CHILD_KEYS:
            mark = '  -> 子节点容器'
        if k in ATTR_KEYS:
            mark = '  -> 属性容器'
        print(f'  {k:<28} x{c:<5}{mark}')

    # ---------------------------------------------------------- 字段覆盖核对
    print('\n字段覆盖核对（别名表 12 个语义逐个比对）:')
    missing_sem = []
    for sem, alts in ATTR_ALIASES.items():
        found = [k for k in key_counter if k in alts]
        if found:
            hit = ', '.join(sorted(found, key=lambda x: -key_counter[x]))
            print(f'  [命中]   {sem:<12} <- {hit}')
        else:
            missing_sem.append(sem)
            print(f'  [未命中] {sem:<12} 别名表候选: {", ".join(alts)}')

    # 真实树里有、但别名表完全没收录的字段 —— 需要人工判断归属
    known = set(CHILD_KEYS) | set(ATTR_KEYS)
    for alts in ATTR_ALIASES.values():
        known.update(alts)
    unknown = sorted(k for k in key_counter if k not in known)

    print(f'\n未收录字段（{len(unknown)} 个，需判断是否归入某个语义）:')
    if unknown:
        for k in unknown:
            print(f'  {k:<30} x{key_counter[k]}')
    else:
        print('  无 —— 别名表已覆盖真实树的全部字段')

    critical = ['type', 'id', 'text', 'bounds']
    bad_critical = [s for s in critical if s in missing_sem]

    # 落一份机器可读的差异报告，可直接发给 A 去改 layout.py
    diff = {
        'semantic_hits': {s: [k for k in key_counter if k in alts]
                          for s, alts in ATTR_ALIASES.items()},
        'semantic_missing': missing_sem,
        'critical_missing': bad_critical,
        'unknown_fields': {k: key_counter[k] for k in unknown},
    }
    diff_path = os.path.join(OUT, 'field_diff.json')
    with open(diff_path, 'w', encoding='utf-8') as f:
        json.dump(diff, f, ensure_ascii=False, indent=2)
    print(f'\n差异报告已存: {diff_path}')

    # ---------------------------------------------------------- 解析验证
    print('\n尝试解析为对象树 ...')
    try:
        root = parse_layout(data)
        nodes = list(root.walk())
        print(f'  解析成功: 根={root.type}  节点数={len(nodes)}')
        vis = flatten(root, only_visible=True)
        inter = flatten(root, only_visible=True, only_interactive=True)
        print(f'  可见节点={len(vis)}  可交互节点={len(inter)}')
        no_rect = [n for n in vis if n.rect.area == 0]
        if no_rect:
            print(f'  **警告**: {len(no_rect)} 个可见节点的 bounds 解析为 0，'
                  f'可能是坐标字段名不同。示例: {no_rect[0].type}/{no_rect[0].id}')
        no_id = [n for n in inter if not (n.id or n.text)]
        if no_id:
            print(f'  提示: {len(no_id)} 个可交互节点既无 id 也无 text，'
                  f'需靠视觉通道定位。示例: {no_id[0].type}')

        print('\n前 30 个可交互节点:')
        for n in inter[:30]:
            print(f'  {n.type:<14} id={n.id[:22]:<22} text={n.text[:18]!r:<20} '
                  f'center={n.center}')

        print('\n控件树前 60 行（核对层级）:')
        for line in root.describe().splitlines()[:60]:
            print('  ' + line)

    except Exception as e:
        print(f'  解析失败: {type(e).__name__}: {e}')
        print('  请对照上面的字段列表，补充 layout.py 的 ATTR_ALIASES / CHILD_KEYS')
        return 1

    print()
    if bad_critical:
        print(f'结论：关键语义 {bad_critical} 未命中 —— 必须补 ATTR_ALIASES，否则定位不可用。')
    elif missing_sem:
        print(f'结论：关键语义齐备；{missing_sem} 未命中 ——')
        print('      这些能力若要用到（如无障碍定位、滚动、勾选态），需按上面的候选名补别名。')
    elif unknown:
        print(f'结论：12 个语义全部命中；另有 {len(unknown)} 个未收录字段，'
              f'确认用不到即可忽略。')
    else:
        print('结论：字段覆盖完整，无需调整 layout.py。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
