# -*- coding: utf-8 -*-
"""把两处派活打成交付 zip（给 A / 给 B 分开，各含 readme 引导 + 证据）。

用法::

    python tools/make_handoff_zips.py

产物落在项目上级目录（与历史交付包同级）。
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

README_A = """# 派活 · 给 A：视觉模块收尾（两条 + 一条研究性）

## 先看这个（5 分钟）

1. `派活-给A-视觉模块收尾.md` —— 行号级证据 + 修法约束 + 验收命令
2. `参考-vision模块当前副本.py` —— 打包时刻的 vision.py，对照用（勿直接改它交付）
3. `证据-视觉复测95.md` —— B13 真模型复测 95.0%（19/20），thinking 真关口径

## 两条正式任务

1. `vision.py` 的 `_hints` 两份逐字拷贝（304/573 行）→ 提取公共函数合一，补一致性钉子；
2. `from_env` 转发 `disable_thinking` —— 工装侧已覆写兜底（见
   `tools/eval_vision_offline.py` 的 RecordingProvider），彻底修法在你这里；
   **约束**：转发必须「显式传了才覆盖」，NoThinkingProvider 在 __init__
   自塞 True，别把它的开关清掉（回归钉 test_c_thinking_flag.py 必须保持绿）。
   改完知会 C，C 拆工装侧覆写。

## 一条选做（研究性，需真机与 C 排期）

B10 真机自愈修复失败：重探索「无唯一匹配候选」→ 修复候选唯一性策略
引入 text_deep / 并列候选清单。离线自愈 100% 与真机边界两行并列汇报。

## 门禁与纪律（本周起生效）

- `NAR001` 棘轮：你文件里的注释/docstring 基线已冻结，**新增日期戳/回执/
  修复前叙事 → error 阻断门禁**（过程归 commit message，约束才留代码）；
- `T101`：tests 里 try+assert* 被 except Exception:pass 吞 → error；
- 交付前自测：`python tools/quality_gate.py --fast` 全绿再交，diff 发群里；
- 真机被 C 长稳占用（今晚），📱 验收先做离线钉子。
"""

README_B = """# 派活 · 给 B：生成与探索收尾（四改一确认）

## 先看这个（5 分钟）

1. `派活-给B-生成与探索收尾.md` —— 四条改动 + 一条复核，行号级证据都在里面
2. `证据-nl_eval.md` —— B8 真机执行报告：L3 两条用例各挂 1 步，根因同源
3. `证据-note用例真机10轮失败.log` —— note_stability 用例过期的现场

## 四条正式任务

1. **断言目标选择**（B8 抓到的生成质量缺陷）：模型把导航前页面控件
   （tv_probe_always）写进导航后断言 → 两条用例用例级 0/2。修法方向：
   断言目标与当前页 page_path 一致性校验（PageState.page_path 已可用）；
2. `trace_to_steps` 未处理 kind 静默丢（double_tap/long_press/fling/
   screenshot/home/key）→ unresolved 占位，与你 336-340 行自己的红线对齐；
3. `generator.py` dry_run 消费端不看 `kind='assert'` → 带导航后断言的
   用例被系统性误杀，转「需真机确认」类；
4. 分辨率家族 B 侧 3 处（1307/1725/1827 的 1080×2340）→ 真机 720×1280，
   能取实测取实测，回落值注明出处。

## 一条复核确认（一行字回执即可）

`explorer.PageState.page_path` 是 C 代补（签名层有、状态层没带出）——
核对字段位置、add_state 透传、与你的双签名口径是否一致，确认即销账。

## 门禁与纪律（本周起生效）

- `NAR001` 棘轮：你的文件基线已冻结，注释新增日期戳/回执/修复前 → error；
- `T101`：tests 里 try+assert* 被 except Exception:pass 吞 → error；
- 交付前自测：`python tools/quality_gate.py --fast` 全绿再交，diff 发群里；
- 真机被 C 长稳占用（今晚），📱 验收先做离线钉子，与 C 排期联跑。
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
    ev_a = _run([PY, '-m', 'unittest', 'tests.test_a_vision',
                 'tests.test_a_locator', '-q'])
    ev_b = _run([PY, '-m', 'unittest', 'tests.test_generator',
                 'tests.test_explorer_b1', 'tests.test_c_thinking_flag', '-q'])

    a = os.path.join(OUT, 'C交付-给A-2026-09-28.zip')
    _zip(a, [
        ('README-给A.md', None),
        ('派活-给A-视觉模块收尾.md',
         os.path.join(ROOT, 'docs', '派活-给A-视觉模块收尾-2026-09-28.md')),
        ('参考-vision模块当前副本.py', os.path.join(ROOT, 'ohauto', 'vision.py')),
        ('证据-视觉复测95.md',
         os.path.join(ROOT, '_out', 'vision_eval', 'vision_eval.md')),
        ('证据-A侧相关测试基线.txt', None),
    ])
    with zipfile.ZipFile(a, 'a', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README-给A.md', README_A)
        z.writestr('证据-A侧相关测试基线.txt', ev_a)

    b = os.path.join(OUT, 'C交付-给B-2026-09-28.zip')
    _zip(b, [
        ('README-给B.md', None),
        ('派活-给B-生成与探索收尾.md',
         os.path.join(ROOT, 'docs', '派活-给B-生成与探索收尾-2026-09-28.md')),
        ('证据-nl_eval.md', os.path.join(ROOT, 'tools', '_out', 'nl_eval',
                                         'nl_eval.md')),
        ('证据-note用例真机10轮失败.log',
         os.path.join(ROOT, 'tools', '_out', 'b5_note_stale_evidence.log')),
        ('证据-B侧相关测试基线.txt', None),
    ])
    with zipfile.ZipFile(b, 'a', zipfile.ZIP_DEFLATED) as z:
        z.writestr('README-给B.md', README_B)
        z.writestr('证据-B侧相关测试基线.txt', ev_b)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
