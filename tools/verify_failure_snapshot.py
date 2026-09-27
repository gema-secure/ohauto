# -*- coding: utf-8 -*-
"""验证「失败时留控件树快照」真的把归因置信度从 0.6 提到 0.85。

背景（B 的交付包 C-2）：
    `runner.StepResult` 原本只带 kind / attempts，没有控件树快照。
    `diagnose._locator()` 判「定位失败」靠的是「目标控件在**所有**快照里都不存在」
    这条硬证据；没有快照时它只能退回运行器分类，置信度下调。

本脚本用模拟设备跑同一条失败用例，**同一份失败**分别按「有快照 / 无快照」
归因，把两个置信度摆在一起对照。

用法::

    python tools/verify_failure_snapshot.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ohauto import Driver, Runner, diagnose                      # noqa: E402
from ohauto.diagnose import ExecutionRecord                      # noqa: E402
from ohauto.explorer import build_page_signature                 # noqa: E402
from ohauto.layout import parse_layout                           # noqa: E402
from ohauto.sim import FakeHdc                                   # noqa: E402

BUNDLE = 'com.demo.app'
#: 设备上根本不存在的控件 —— 触发「定位失败」
MISSING = {'id': 'this_control_does_not_exist'}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix='ohauto_snap_')
    try:
        sim = FakeHdc(start_page='login', verbose=False)
        d = Driver(bundle=BUNDLE, ability='EntryAbility', hdc=sim,
                   artifact_dir=tmp, verbose=False)
        runner = Runner(sleep_fn=lambda s: None, verbose=False)

        case = {'name': 'snapshot_demo',
                'steps': [{'tap': MISSING}]}
        res = runner.run_case(d, case)

        steps = list(res.steps)
        failed = [s for s in steps if not s.ok]
        if not failed:
            print('[FAIL] 用例本应失败，却没有失败步')
            return 1
        step = failed[0]

        print('=' * 74)
        print('① 运行器侧：失败步有没有留下现场？')
        print('=' * 74)
        print(f'  步骤          : #{step.index} {step.action} {step.target}')
        print(f'  失败类别      : {getattr(step.kind, "value", step.kind)}')
        print(f'  快照数量      : {len(step.trees)}')
        for i, t in enumerate(step.trees, 1):
            kind = '路径' if isinstance(t, str) else type(t).__name__
            shown = t if isinstance(t, str) else '(LayoutNode 对象)'
            size = f'{os.path.getsize(t)} B' if isinstance(t, str) and os.path.isfile(t) else '-'
            print(f'    快照{i} [{kind}] {shown}  {size}')
        files = sorted(f for f in os.listdir(tmp) if f.endswith('.json'))
        print(f'  产物目录      : {tmp}')
        print(f'  落盘的控件树  : {files}')

        if not step.trees:
            print('\n[FAIL] 失败步没有快照 —— C-2 没生效')
            return 2

        print()
        print('=' * 74)
        print('② 归因侧：同一份失败，有快照 vs 无快照')
        print('=' * 74)

        # 用第一张快照算出本页签名，作为「用例声明的期望页面」。
        # 不传它时归因会加一句「无法核对落点是否为目标页」再降一档置信度 ——
        # 那是正常的（用例确实没声明），但对照实验要控制变量，所以补上。
        sig = build_page_signature(parse_layout(step.trees[0]), bundle=BUNDLE)
        expect_page = sig.content_key

        with_snap = ExecutionRecord.from_case_result(
            res, expected_target=MISSING, bundle=BUNDLE,
            expected_page=expect_page)
        # 手工把快照抹掉，模拟「C-2 没做」时的归因输入
        without = ExecutionRecord.from_case_result(
            res, expected_target=MISSING, bundle=BUNDLE,
            expected_page=expect_page, trees=[])

        v_on = diagnose(with_snap)
        v_off = diagnose(without)

        def row(tag, v):
            print(f'  {tag:<12} category={v.category.value:<10} '
                  f'confidence={v.confidence:.2f}')
            for ev in (v.evidence or [])[:3]:
                print(f'                 · {ev}')

        row('有快照', v_on)
        row('无快照', v_off)

        delta = v_on.confidence - v_off.confidence
        same = v_on.category == v_off.category
        print()
        print(f'  置信度差      : {delta:+.2f}')
        print(f'  类别是否一致  : {"一致" if same else "★ 不一致"}')
        print(f'  快照张数      : {len(with_snap.trees)}  '
              f'(期望 2：失败瞬间 + 稳定后)')

        if not same:
            print()
            print(f'  ★ 没有快照不只是「置信度低」，它会把类别**判错**：')
            print(f'      {v_on.category.value} → {v_off.category.value}')
            print(f'    原因：没快照时 _page_expectation 返回 unknown，')
            print(f'    「落点与 expected_page 不符」这条判据被误当成用例缺陷。')

        ok = len(with_snap.trees) == 2 and delta > 0
        print()
        print('[PASS] C-2 生效：失败现场已留存，归因置信度提升'
              if ok else
              '[WARN] 快照有了，但置信度没提升 —— 看上面的 evidence 判断是否正常')
        return 0 if ok else 1   # 约定见 docs/约定-退出码.md：1=未达标，不造 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
