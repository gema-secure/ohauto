# -*- coding: utf-8 -*-
"""把两处派活打成交付 zip（给 A / 给 B 分开，各含 readme 引导 + 证据）。

用法::

    python tools/make_handoff_zips.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.dirname(ROOT)          # D:/project/（与历史交付包同级）
PY = sys.executable

README_A = """# 派活 · 给 A（2026-09-25）：自愈幂等代次修复

## 先看这个（5 分钟）

1. `派活-给A-自愈幂等代次.md` —— 根因行号级证据 + 可直接抄的修法 + 验收命令
2. `证据-wire_locator_sink自测输出.txt` —— 当前断点的现场记录（连续失败停在 1）
3. `参考-C侧接线wire_locator_sink.py` —— C 侧怎么把 attempt 传进来，你照着对齐签名

## 一句话背景

挑战 #5「归因→自愈」闭环里，**归因侧全通**（结论带 locator_id、回写生效），
卡在最后一环：你的幂等闸把执行器回写误判成重复记账 → 连续失败停在 1
→ 自愈阈值够不着 → 生产链路上自愈**一次都不会触发**。

## 你要改的（一个文件、一个函数）

`ohauto/locator.py`：`record_locator_failure` 与 `_count_failure` 加**可选**参数
`attempt`（执行尝试序号），幂等键优先用它；不传时退回现有 `_locate_seq`
—— 内部 `locate()` 路径行为零变化，你的测试不用动。

## C 侧已备好（你改完自动生效）

- `ohauto/runner.py`：每次回写递增 `_attempt_seq`，按 sink 签名自适应传出
- `tools/wire_locator_sink.py`：sink 收到 attempt 后三参转发、`TypeError` 退回两参

## 验收（三条都要过）

```
python tools/wire_locator_sink.py      # 连续失败递增到 6、自愈换代触发
python -m unittest tests.test_a_locator -q
python tools/quality_gate.py           # 1080+ 项 OK / 0 error / 88%
```

改完把 diff 发群里，C 复跑验收并回填 `docs/指标汇总`。
"""

README_B = """# 派活 · 给 B（2026-09-25）：空壳用例校验补闸

## 先看这个（5 分钟）

1. `派活-给B-空壳用例校验.md` —— 根因行号级证据 + 修法建议 + 验收命令
2. `证据-L3真机评测报告.md` —— 空壳用例 2/2 全过、真引用控件 0/3 的现场数据

## 一句话背景

`validate_case` 只做**逐步**检查，没有「用例整体必须做事」的总闸 →
只含 `start`/`waitIdle`/`screenshot` 的空壳用例每步都合法、顺利放行。
L3 真机实测：**空壳 2/2 全过，真引用控件的 3 条 0/3 整全过**。

「可执行」不等于「做了事」—— 这句会被评审问倒，所以必须补这条规则。

## 你要改的（一个文件、一条检查）

`ohauto/generator.py::validate_case`：用例级总闸——所有动作都属于
「不引用控件」集合时直接拒（集合口径请把关 `waitGone` 等边界）。
拒绝分类可复用 `EMPTY` 或新增，并补 `reason.cn` 映射。

## 验收

```
# 三步空壳用例 {'start':True},{'waitIdle':2},{'screenshot':'x'} → issues 非空
python -m unittest tests.test_core tests.test_diagnose -q
python tools/eval_nl_generation_real.py --execute   # 负样本误放 3/6 → 0/6（需 key+真机，可与 C 联跑）
```

改完把 diff 发群里，C 复跑验收并回填 `docs/指标汇总`。
"""


def _run(cmd: list) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', timeout=180, cwd=ROOT)
        return (r.stdout or '') + (r.stderr or '')
    except Exception as e:
        return '(采集失败: %s: %s)' % (type(e).__name__, e)


def _zip(path: str, members) -> None:
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for arc, src in members:
            if src is None:      # 占位成员：内容稍后用 writestr 填
                continue
            if os.path.isfile(src):
                z.write(src, arc)
            else:
                z.writestr(arc, '(缺失: %s)' % src)
    print('已生成 %s（%.0f KB）' % (path, os.path.getsize(path) / 1024.0))


def main() -> int:
    print('采集证据（跑自测）…')
    ev_a = _run([PY, os.path.join('tools', 'wire_locator_sink.py')])

    a = os.path.join(OUT, 'C交付-给A-2026-09-25.zip')
    _zip(a, [
        ('README-给A.md', None),
        ('派活-给A-自愈幂等代次.md',
         os.path.join(ROOT, 'docs', '派活-给A-自愈幂等代次-2026-09-25.md')),
        ('证据-wire_locator_sink自测输出.txt', None),
        ('参考-C侧接线wire_locator_sink.py',
         os.path.join(ROOT, 'tools', 'wire_locator_sink.py')),
    ])
    # README/证据用字符串写入
    with zipfile.ZipFile(a, 'a', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README-给A.md', README_A)
        z.writestr('证据-wire_locator_sink自测输出.txt', ev_a)

    b = os.path.join(OUT, 'C交付-给B-2026-09-25.zip')
    _zip(b, [
        ('README-给B.md', None),
        ('派活-给B-空壳用例校验.md',
         os.path.join(ROOT, 'docs', '派活-给B-空壳用例校验-2026-09-25.md')),
        ('证据-L3真机评测报告.md',
         os.path.join(ROOT, 'tools', '_out', 'nl_eval', 'nl_eval.md')),
    ])
    with zipfile.ZipFile(b, 'a', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README-给B.md', README_B)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
