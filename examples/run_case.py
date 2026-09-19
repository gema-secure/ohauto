"""
执行一个 DSL 用例
=================

用法:
    python examples/run_case.py examples/cases/login.yaml --bundle com.example.app
    python examples/run_case.py examples/cases/login.yaml --bundle com.example.app \\
        --sim           # 用模拟设备跑（验证用例语法是否正确）
    python examples/run_case.py examples/cases/login.yaml --bundle com.example.app \\
        --ability EntryAbility --out ./run_out
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto import action, report                 # noqa: E402
from ohauto.driver import Driver                  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('case', help='用例文件（YAML 或 JSON）')
    ap.add_argument('--bundle', default='', help='被测应用包名（覆盖用例里的）')
    ap.add_argument('--ability', default='', help='入口 Ability（覆盖用例里的）')
    ap.add_argument('--out', default=os.path.join(HERE, '_out', 'run'),
                    help='产物目录')
    ap.add_argument('--hdc', default=None, help='hdc 路径')
    ap.add_argument('--sim', action='store_true',
                    help='用模拟设备执行（不需要真机，用于校验用例语法）')
    ap.add_argument('--no-artifacts', action='store_true',
                    help='不保存截图与控件树，跑得更快')
    args = ap.parse_args()

    case = action.load_case(args.case)
    bundle = args.bundle or case.get('bundle') or 'com.unknown'
    ability = args.ability or case.get('ability') or 'EntryAbility'
    artifact_dir = None if args.no_artifacts else args.out

    print('=' * 68)
    print(f'  执行用例: {case.get("name") or args.case}')
    print(f'  应用    : {bundle}')
    print(f'  模式    : {"模拟设备" if args.sim else "真机"}')
    print('=' * 68)

    if args.sim:
        from ohauto.sim import FakeHdc
        hdc = FakeHdc(start_page='login', verbose=False)
        # 模拟设备没有真实应用的控件，用例里的定位条件会被改写为示例页面的，
        # 以便验证「DSL 解析 + 编排 + 报告」这条链路本身。
        case = _adapt_for_sim(case)
        print('  （已把定位条件映射到模拟设备的控件上）')
    else:
        hdc = None
        if args.hdc:
            from ohauto.hdc import Hdc
            hdc = Hdc(hdc_path=args.hdc)

    d = Driver(bundle=bundle, ability=ability, hdc=hdc,
               artifact_dir=artifact_dir, verbose=True)

    if not args.sim:
        try:
            t = d.hdc.list_targets()
            if not t:
                print('\n未检测到设备。请先连接真机并开启 USB 调试。')
                print('想先验证用例语法？加 --sim 参数。')
                return 1
            if not d.hdc.target:
                d.hdc.target = t[0] or None
            print(f'在线设备: {t}\n')
        except Exception as e:
            print(f'设备检测失败: {e}')
            return 1

    rep = action.run_case(d, case)

    print()
    print('-' * 68)
    print(f'  通过 {rep.passed} / 共 {rep.total} 步，'
          f'结论 = {"通过" if rep.ok else "未通过"}，耗时 {rep.elapsed_ms} ms')
    print('-' * 68)
    if rep.errors:
        print('\n失败明细:')
        for e in rep.errors:
            print(f"  步骤 {e['step']} [{e.get('action')}]: {e.get('error', '')[:300]}")

    out = args.out if not args.no_artifacts else os.path.join(HERE, '_out', 'report')
    data = report.collect(d, run_report=rep,
                          extra={'case_file': os.path.abspath(args.case)})
    paths = report.write_all(data, out, stem='case_report')
    print('\n报告:')
    for k, v in paths.items():
        print(f'  {k:<9} {v}')

    return 0 if rep.ok else 1


def _adapt_for_sim(case):
    """把用例里的定位条件映射到模拟设备的控件上。

    模拟设备的控件 id/text 是固定的示例页面（login/home/order），
    因此这里按「语义关键词」兜底替换，保证链路可验证。
    """
    import copy
    c = copy.deepcopy(case)
    mapping = {
        'username': {'id': 'username'},
        'password': {'id': 'password'},
        '登录': {'text': '登录'},
        '首页': {'text': '首页'},
        '我的订单': {'text': '我的订单'},
        '订单': {'text': '订单'},
    }
    # 长键优先，避免「我的订单」被更短的「订单」抢先子串命中
    ordered = sorted(mapping.items(), key=lambda kv: -len(kv[0]))

    def fix(spec):
        if not isinstance(spec, dict):
            return spec
        for key in ('id', 'text', 'text_contains', 'label', 'descr'):
            v = spec.get(key)
            if v is None:
                continue
            sv = str(v)
            for k, repl in ordered:
                if sv == k or k in sv:
                    out = dict(spec)
                    out.pop(key, None)
                    out.update(repl)
                    return out
        return spec

    for st in c.get('steps', []):
        for act in ('tap', 'waitFor', 'waitGone', 'input'):
            if act in st and isinstance(st[act], dict):
                if act == 'input':
                    spec, val = dict(st[act]), st[act].get('value')
                    spec.pop('value', None)
                    st[act] = {**fix(spec), 'value': val}
                else:
                    st[act] = fix(st[act])
        if 'assert' in st and isinstance(st['assert'], dict):
            for k, v in st['assert'].items():
                if isinstance(v, dict):
                    st['assert'][k] = fix(v)
    return c


if __name__ == '__main__':
    raise SystemExit(main())
