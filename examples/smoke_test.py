"""
最小闭环冒烟测试 —— 接上真机的第一件事
=====================================

按顺序验证：设备连通 → 拉起应用 → 定位控件 → 点击 → 断言 → 截图。
每一步都打印结果，任何一步失败都会指出原因。

用法:
    python examples/smoke_test.py --bundle com.example.app
    python examples/smoke_test.py --bundle com.example.app --expect "首页"
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto import report                        # noqa: E402
from ohauto.driver import Driver, DriverError    # noqa: E402
from ohauto.hdc import Hdc, HdcError             # noqa: E402
from ohauto.layout import flatten                # noqa: E402
from ohauto.matcher import ON                    # noqa: E402

OUT = os.path.join(HERE, '_out', 'smoke')


def step(n, title):
    print(f'\n[{n}] {title}')
    print('-' * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bundle', required=True, help='被测应用包名')
    ap.add_argument('--ability', default='EntryAbility')
    ap.add_argument('--expect', default='', help='启动后应出现的文本（可选）')
    ap.add_argument('--hdc', default=None)
    args = ap.parse_args()

    print('=' * 60)
    print('  UI 自动化最小闭环冒烟测试')
    print('=' * 60)

    # ---------------------------------------------------------- 1
    step(1, '查找 hdc 并检查设备')
    try:
        hdc = Hdc(hdc_path=args.hdc)
    except HdcError as e:
        print(f'  [失败] {e}')
        return 1
    print(f'  hdc: {hdc.hdc_path}')
    try:
        targets = hdc.list_targets()
    except HdcError as e:
        print(f'  [失败] 无法查询设备: {e}')
        return 1
    if not targets:
        print('  [失败] 未检测到设备。请检查：')
        print('    1. USB 已连接（或 hdc tconn <ip>:<port> 无线连接）')
        print('    2. 设备已开启「开发者模式 + USB 调试」')
        print('    3. 设备上已授权本机调试')
        return 1
    print(f'  [通过] 在线设备: {targets}')
    if not hdc.target:
        hdc.target = targets[0] or None

    # ---------------------------------------------------------- 2
    step(2, '检查 uitest 命令行通路')
    uv = hdc.uitest_version()
    if uv:
        print(f'  [通过] {uv}')
        print('         -> 技术路线：hdc 命令行通路（设备侧零部署）')
    else:
        print('  [警告] 设备不支持 uitest 命令，需改用 ArkTS API 通路')
        print('         本能力层的 L1 将无法工作，请确认设备版本')

    # ---------------------------------------------------------- 3
    d = Driver(bundle=args.bundle, ability=args.ability, hdc=hdc,
               artifact_dir=OUT, verbose=True)

    step(3, f'拉起应用 {args.bundle}')
    try:
        ms = d.start()
        print(f'  [通过] 冷启动耗时约 {ms} ms')
    except Exception as e:
        print(f'  [失败] {e}')
        print('         请确认包名与 Ability 名正确，且应用已安装：')
        print(f'         hdc shell aa start -b {args.bundle} -a {args.ability}')
        return 1

    # ---------------------------------------------------------- 4
    step(4, '导出并解析控件树')
    try:
        tree = d.refresh()
        all_nodes = flatten(tree, only_visible=True)
        inter = flatten(tree, only_visible=True, only_interactive=True)
        print(f'  [通过] 根={tree.type}  可见节点={len(all_nodes)}  '
              f'可交互={len(inter)}')
        print('  前 12 个可交互控件:')
        for n in inter[:12]:
            print(f'    {n.type:<13} id={n.id[:20]:<20} '
                  f'label={n.label[:16]!r:<18} center={n.center}')
        if not inter:
            print('  [警告] 没有识别到可交互控件，可能是字段名差异。')
            print('         请跑 examples/dump_tree.py 核对结构。')
    except Exception as e:
        print(f'  [失败] {e}')
        return 1

    # ---------------------------------------------------------- 5
    step(5, '语义定位 -> 坐标 -> 点击')
    target = None
    if inter:
        # 优先找一个安全的按钮（避开危险控件）
        from ohauto.explorer import SafetyPolicy
        pol = SafetyPolicy()
        for n in inter:
            ok, _ = pol.is_clickable_candidate(n)
            if ok and n.type in ('Button', 'Toggle', 'Checkbox'):
                target = n
                break
        if target is None:
            for n in inter:
                if pol.is_clickable_candidate(n)[0]:
                    target = n
                    break
    if target is None:
        print('  [跳过] 没有找到安全的可点击控件')
    else:
        print(f'  目标控件: {target.type} id={target.id} label={target.label!r}')
        print(f'  解析出的点击坐标: {target.center}')
        try:
            d.tap(target)
            print('  [通过] 点击已注入')
        except Exception as e:
            print(f'  [失败] 点击失败: {e}')
            return 1

    # ---------------------------------------------------------- 6
    step(6, '截图')
    try:
        p = d.screenshot()
        if p and os.path.exists(p):
            print(f'  [通过] {p}  ({os.path.getsize(p)} bytes)')
        else:
            print('  [警告] 截图未落地')
    except Exception as e:
        print(f'  [警告] 截图失败: {e}')

    # ---------------------------------------------------------- 7
    if args.expect:
        step(7, f'断言文本「{args.expect}」存在')
        try:
            d.assert_exists(ON.text_contains(args.expect), timeout=8000)
            print(f'  [通过] 已找到「{args.expect}」')
        except DriverError as e:
            print(f'  [失败] {e}')
    else:
        step(7, '断言（未指定 --expect，跳过）')

    # ---------------------------------------------------------- 汇总
    s = d.summary()
    print()
    print('=' * 60)
    print(f"  共 {s['steps_total']} 步，失败 {s['steps_failed']}，"
          f"成功率 {s['success_rate'] * 100:.1f}%")
    print('=' * 60)

    data = report.collect(d, extra={'note': '冒烟测试'})
    paths = report.write_all(data, OUT, stem='smoke_report')
    print('\n报告:')
    for k, v in paths.items():
        print(f'  {k:<9} {v}')

    print('\n结论: 最小闭环' + ('打通。' if s['steps_failed'] == 0 else
                               '存在问题，请查看上面的失败信息。'))
    return 0 if s['steps_failed'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
