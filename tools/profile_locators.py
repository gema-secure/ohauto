from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

STRONG_KEYS: Tuple[str, ...] = ('id',)
WEAK_KEYS: Tuple[str, ...] = ('text', 'description', 'hint')
IGNORED_KEYS: Tuple[str, ...] = ('accessibilityId', 'hashcode', 'key')


def _attrs(node: Dict[str, Any]) -> Dict[str, Any]:
    return node.get('attributes') or {}


def _nonempty(attrs: Dict[str, Any], key: str) -> bool:
    return bool(str(attrs.get(key) or '').strip())


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() == 'true'


def _is_interactive(attrs: Dict[str, Any]) -> bool:
    return _is_true(attrs.get('clickable')) or _is_true(attrs.get('scrollable'))


def _locator_level(attrs: Dict[str, Any]) -> str:
    if any(_nonempty(attrs, k) for k in STRONG_KEYS):
        return 'strong'
    if any(_nonempty(attrs, k) for k in WEAK_KEYS):
        return 'weak'
    return 'none'


def _element_record(attrs: Dict[str, Any], path: str, depth: int) -> Dict[str, Any]:
    return {
        'type': str(attrs.get('type') or ''),
        'bounds': str(attrs.get('bounds') or ''),
        'hierarchy': str(attrs.get('hierarchy') or ''),
        'depth': depth,
        'path': path,
        'pagePath': str(attrs.get('pagePath') or ''),
        'abilityName': str(attrs.get('abilityName') or ''),
        'clickable': _is_true(attrs.get('clickable')),
        'scrollable': _is_true(attrs.get('scrollable')),
        'longClickable': _is_true(attrs.get('longClickable')),
        'enabled': _is_true(attrs.get('enabled')),
        'visible': _is_true(attrs.get('visible')),
    }


def _new_stat() -> Dict[str, int]:
    return {
        'nodes': 0,
        'interactive': 0,
        'strong': 0,
        'weak': 0,
        'none': 0,
        'with_text': 0,
        'with_description': 0,
        'with_hint': 0,
        'by_type_id': 0,
        'by_type_text': 0,
        'by_type_other': 0,
    }


def _walk(node: Dict[str, Any], stat: Dict[str, int],
          hard: List[Dict[str, Any]], depth: int = 0,
          path: str = '') -> None:
    attrs = _attrs(node)
    stat['nodes'] += 1
    if _nonempty(attrs, 'text'):
        stat['with_text'] += 1
    if _nonempty(attrs, 'description'):
        stat['with_description'] += 1
    if _nonempty(attrs, 'hint'):
        stat['with_hint'] += 1

    if _is_interactive(attrs):
        stat['interactive'] += 1
        level = _locator_level(attrs)
        stat[level] += 1
        label = f"{attrs.get('type') or '?'}[{depth}]"
        here = f'{path}/{label}' if path else label
        if level == 'none':
            hard.append(_element_record(attrs, here, depth))
    else:
        here = path

    for index, child in enumerate(node.get('children') or []):
        child_attrs = _attrs(child)
        child_label = f"{child_attrs.get('type') or '?'}#{index}"
        child_path = f'{path}/{child_label}' if path else child_label
        _walk(child, stat, hard, depth + 1, child_path)


def analyze_file(path: str) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    with open(path, encoding='utf-8') as handle:
        tree = json.load(handle)
    stat = _new_stat()
    hard: List[Dict[str, Any]] = []
    _walk(tree, stat, hard)
    for record in hard:
        record['source'] = os.path.basename(path)
    return stat, hard


def analyze_dir(directory: str) -> Dict[str, Any]:
    files = sorted(p for p in glob.glob(os.path.join(directory, '*.json'))
                   if not p.endswith('.meta.json'))
    if not files:
        raise ValueError(f'目录中未找到控件树 JSON: {directory}')

    total = _new_stat()
    per_file: List[Dict[str, Any]] = []
    hard_all: List[Dict[str, Any]] = []

    for path in files:
        stat, hard = analyze_file(path)
        for key, value in stat.items():
            total[key] += value
        hard_all.extend(hard)
        per_file.append({
            'file': os.path.basename(path),
            **stat,
            'hard_elements': len(hard),
        })

    total['hard_elements'] = len(hard_all)
    return {
        'directory': os.path.abspath(directory),
        'file_count': len(files),
        'total': total,
        'per_file': per_file,
        'hard_elements': hard_all,
    }


def _ratio(part: int, whole: int) -> float:
    return round(part / whole * 100, 1) if whole else 0.0


def print_report(result: Dict[str, Any]) -> None:
    total = result['total']
    interactive = total['interactive'] or 1

    print(f"目录: {result['directory']}")
    print(f"控件树文件: {result['file_count']} 个\n")

    header = (f"{'应用':<24}{'节点':>7}{'可交互':>8}{'有id':>7}"
              f"{'仅弱标识':>10}{'无标识':>8}")
    print(header)
    print('-' * len(header))
    for row in result['per_file']:
        name = row['file']
        if len(name) > 22:
            name = name[:19] + '...'
        print(f"{name:<24}{row['nodes']:>7}{row['interactive']:>8}"
              f"{row['strong']:>7}{row['weak']:>10}{row['none']:>8}")
    print('-' * len(header))
    print(f"{'合计':<24}{total['nodes']:>7}{total['interactive']:>8}"
          f"{total['strong']:>7}{total['weak']:>10}{total['none']:>8}")

    print()
    print(f"  可交互元素（clickable / scrollable）: {total['interactive']}")
    print(f"  强标识 id                           : {total['strong']}"
          f"  ({_ratio(total['strong'], interactive)}%)")
    print(f"  弱标识 text / description / hint    : {total['weak']}"
          f"  ({_ratio(total['weak'], interactive)}%)")
    print(f"  无任何语义标识                      : {total['none']}"
          f"  ({_ratio(total['none'], interactive)}%)")
    print()
    print(f"  全节点带 text                       : {total['with_text']}"
          f"  (占全部节点 {_ratio(total['with_text'], total['nodes'])}%)")
    print(f"  全节点带 description                : {total['with_description']}")
    print(f"  全节点带 hint                       : {total['with_hint']}")
    print()
    print('  注: accessibilityId / hashcode / hierarchy 为平台内部编号或结构路径，')
    print('      不作为语义标识计入；它们随控件树结构变化而改变。')


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='统计真机控件树的定位标识可用性（面向定位模块的 KPI 口径）')
    parser.add_argument('directory', help='含 *.json 控件树的目录')
    parser.add_argument('--json', dest='json_out',
                        help='把完整统计写入该 JSON 文件')
    parser.add_argument('--export-hard',
                        help='把「无任何语义标识的可交互元素」清单写入该 JSON 文件')
    parser.add_argument('--quiet', action='store_true',
                        help='只输出汇总，不打印大表')
    args = parser.parse_args(argv)

    try:
        result = analyze_dir(args.directory)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'[分析失败] {exc}', file=sys.stderr)
        return 1

    if args.quiet:
        total = result['total']
        print(json.dumps({'files': result['file_count'], **total},
                         ensure_ascii=False))
    else:
        print_report(result)

    if args.json_out:
        with open(args.json_out, 'w', encoding='utf-8') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(f'\n  完整统计已写入 {args.json_out}')

    if args.export_hard:
        payload = {
            'directory': result['directory'],
            'count': len(result['hard_elements']),
            'elements': result['hard_elements'],
        }
        with open(args.export_hard, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f'  无标识元素清单已写入 {args.export_hard}'
              f'（{len(result["hard_elements"])} 个）')

    return 0


if __name__ == '__main__':
    sys.exit(main())
