# -*- coding: utf-8 -*-
"""用**真机控件树**验证 A3 降级链的 L3 与自愈换代。

为什么是这个形态
----------------
A 的交付说明里，A3 的验收口径是「改 5 个控件 id 自动恢复 5/5」以及
「id + 结构全变也能到阈值自动换代」—— 但这两个数字都是**模拟设备**上自测的。

在真机上直接验有困难：要触发 L3 得让 L1/L2 落空，即「控件还在，但 id 和 text
都变了」，而真机上没法改别人的应用。装上自己改过 id 的 APK 又太重
（要重新编译被测应用）。

所以这里用**真机采集到的真实控件树**做底料，人工改掉 id / 结构，
再喂给 `LocatorManager` —— 数据是真的（真机的节点层级、类型、bounds、
真实存在的 id），只是不在真机上跑那一步。这样能验的是**降级链在真实树形上的行为**，
验不了的是「真机上改了 id 的应用」——后者需要被测应用配合。

用法::

    python tools/verify_locator_degrade.py            # 自动连真机抓树
    python tools/verify_locator_degrade.py --from-json saved.json   # 用现成的树
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import Driver, Hdc, LocatorManager              # noqa: E402
from ohauto.layout import parse_layout                      # noqa: E402
from preflight import require_device                  # noqa: E402

DEVICE_HDC = None    # None = 走 Hdc 自动定位；本机路径写 hdc.config.json（隐私项不入源码）
BUNDLE = 'ohos.samples.distributedcalc'
ABILITY = 'MainAbility'


def grab_tree(hdc_path: str, target: str = '') -> dict:
    """从真机抓一份原始控件树 JSON。"""
    hdc = require_device(hdc_path=hdc_path, target=target or None)
    d = Driver(bundle=BUNDLE, ability=ABILITY, hdc=hdc, verbose=False)
    try:
        d.start()
        time.sleep(2)
    except Exception as e:
        print(f'  [警告] 启动应用失败（继续用当前界面）: {e}')
    dev = d.hdc.dump_layout()
    text = d.hdc.shell(f'cat {dev}').stdout
    return json.loads(text)


def find_first(node: dict, pred):
    """深度优先找第一个满足条件的节点包装，返回 (父链, 节点)。

    ⚠️ 别写成 `if walk(node): return list(chain), node` —— 那样返回的是**根节点**，
    不是找到的那个（第一版就这么写的，结果拿到 `id=''` 的空靶子，
    后面三个场景全连锁失败）。
    """
    chain = []
    found = []

    def walk(n):
        if pred(n):
            found.append(n)
            return True
        chain.append(n)
        for c in (n.get('children') or []):
            if walk(c):
                return True
        chain.pop()
        return False

    walk(node)
    return (list(chain), found[0]) if found else ([], None)


def rewrite_ids(tree: dict, mapping: dict) -> int:
    """按 mapping 批量改 id，返回改了几个。"""
    n = 0

    def walk(node):
        nonlocal n
        a = node.get('attributes') or {}
        old = a.get('id')
        if old in mapping:
            a['id'] = mapping[old]
            n += 1
        for c in (node.get('children') or []):
            walk(c)

    walk(tree)
    return n


def strip_text_under(node: dict, max_top: int) -> int:
    """把某个区域内的 text 清空（模拟「文案改了」）。"""
    n = 0

    def walk(x):
        nonlocal n
        a = x.get('attributes') or {}
        if a.get('text'):
            a['text'] = ''
            n += 1
        for c in (x.get('children') or []):
            walk(c)

    if node:
        walk(node)
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--hdc', default=DEVICE_HDC)
    ap.add_argument('--target', default='')
    ap.add_argument('--from-json', default='', help='用现成的控件树 JSON')
    ap.add_argument('--out', default='', help='把抓到的树存下来（便于复跑）')
    ap.add_argument('--id', default='',
                    help='指定靶子控件的 id（默认取第一个数字 id）')
    args = ap.parse_args(argv)

    if args.from_json:
        raw = json.load(open(args.from_json, encoding='utf-8'))
        print(f'  用现成控件树: {args.from_json}')
    else:
        print('  从真机抓控件树...')
        raw = grab_tree(args.hdc, args.target)
        if args.out:
            json.dump(raw, open(args.out, 'w', encoding='utf-8'),
                      ensure_ascii=False)
            print(f'  已存到 {args.out}')

    page_v1 = parse_layout(copy.deepcopy(raw))

    # 选靶子：默认第一个数字 id（计算器的数字键）
    if args.id:
        def has_id(n):
            return (n.get('attributes') or {}).get('id') == args.id
    else:
        def has_id(n):
            a = n.get('attributes') or {}
            return (a.get('id') or '').isdigit()

    chain, target = find_first(raw, has_id)
    if target is None:
        print('  [err] 树里找不到匹配的控件 —— 换个 id 或应用再试')
        return 1
    old_id = target['attributes']['id']
    old_text = target['attributes'].get('text') or ''
    print(f'\n  靶子控件: id={old_id!r} text={old_text!r} '
          f'type={target["attributes"].get("type")!r}')

    lm = LocatorManager()
    lm.register({'id': old_id}, page_v1)
    print(f'  已注册定位器: {list(lm.all_health().keys())}')

    results = []

    # ---------------------------------------------------------- 场景 A
    print('\n' + '=' * 68)
    print('  场景 A · 只改 id（text 保留）→ 期望 L2 id_fuzzy_text 接住')
    print('=' * 68)
    t2 = copy.deepcopy(raw)
    n = rewrite_ids(t2, {old_id: f'{old_id}_renamed_by_test'})
    print(f'  改动 id 数: {n}')
    r = lm.locate({'id': old_id}, parse_layout(t2))
    if r:
        print(f'  → channel={r.channel} level={r.level} conf={r.confidence:.2f} '
              f'id={r.locator_id} rect={r.rect}')
    else:
        print('  → 未命中（None）')
    results.append(('A 改 id（L2）', bool(r), getattr(r, 'level', '-')))

    # ---------------------------------------------------------- 场景 B
    print('\n' + '=' * 68)
    print('  场景 B · id 换成**完全不相干**的词 → 期望降到 L3 path_type')
    print('=' * 68)
    print('  注：上一版把 id 改成 "7_renamed_by_test" 是错的 —— 里面还含着 "7"，')
    print('      L2 的模糊匹配直接命中，根本够不到 L3。要触 L3 必须让 id 完全不含旧值。')
    t3 = copy.deepcopy(raw)
    rewrite_ids(t3, {old_id: 'btn_seven_xyz'})
    r = lm.locate({'id': old_id}, parse_layout(t3))
    if r:
        print(f'  → channel={r.channel} level={r.level} conf={r.confidence:.2f} '
              f'id={r.locator_id} rect={r.rect}')
    else:
        print('  → 未命中（None）')
    results.append(('B id 全换（L3）', bool(r), getattr(r, 'level', '-')))

    # ---------------------------------------------------------- 场景 C
    print('\n' + '=' * 68)
    print('  场景 C · 同上结构保留 → 连续失败到阈值应触发自愈换代')
    print('=' * 68)
    print('  注：**不要**把全树 id 都抹平 —— 那样等于把重新匹配的唯一性证据')
    print('      也一起毁了，自愈会如实报「无唯一匹配候选」。要让自愈有机会成功，')
    print('      只改靶子控件的 id，其余保持真实。')
    t4 = copy.deepcopy(raw)
    rewrite_ids(t4, {old_id: 'btn_seven_xyz'})
    for attempt in range(lm.failure_threshold + 2):
        r = lm.locate({'id': old_id}, parse_layout(t4))
        h = list(lm.all_health().values())
        cf = h[0].get('consecutive_failures') if h else '?'
        print(f'  第 {attempt + 1} 次: 命中={"是" if r else "否"}  '
              f'channel={getattr(r, "channel", "-")}  '
              f'level={getattr(r, "level", "-")}  连续失败={cf}')

    rep = lm.repair_locators(page_signature='', page=parse_layout(t4), force=True)
    print(f'  自愈报告: repaired={getattr(rep, "repaired", None)}')
    print(f'            failed={getattr(rep, "failed", None)}')
    healed = bool(getattr(rep, 'repaired', None))
    results.append(('C 自愈触发', healed, 'repaired' if healed else '未修好'))

    # 自愈后是否直连命中
    if healed:
        r2 = lm.locate({'id': old_id}, parse_layout(t4))
        hit = bool(r2) and getattr(r2, 'level', '') in ('id_exact', 'path_type')
        print(f'  自愈后定位: 命中={"是" if r2 else "否"} '
              f'level={getattr(r2, "level", "-")}')
        results.append(('D 自愈后直连命中', hit, getattr(r2, 'level', '-')))

    # ---------------------------------------------------------- 汇总
    print('\n' + '=' * 68)
    print('  汇总')
    print('=' * 68)
    for name, ok, detail in results:
        print(f'  [{"PASS" if ok else "FAIL"}] {name:<22} {detail}')
    print('\n  注：数据是真机采集的真实控件树；改 id/结构是人工模拟「应用改版」，')
    print('      所以验的是降级链在真实树形上的行为，不是「真机上改了 id 的应用」。')
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == '__main__':
    sys.exit(main())
