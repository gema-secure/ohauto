"""用例生成的最小可跑示例 —— 六阶段全流程（**离线，不需要 LLM key**）
================================================================

对应文档：`docs/用例生成说明.md`

这个例子的用例**故意写得有问题**（固定等待 + 硬编码坐标），
目的是让「校验 → 修复」这两阶段真的跑起来 —— 一个一上来就正确的用例
展示不出修复器的价值，也证明不了红线扫描真的在拦。

跑法：
    python examples/generate_demo.py

它会打印六阶段的中间产物，最后给出一条可执行用例。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ohauto.driver import Driver
from ohauto.generator import (Generator, ScriptedProvider,
                              dry_run, parse_test_points_lenient,
                              repair_locally, validate_case)
from ohauto.sim import FakeHdc

OUT = os.path.join(HERE, '_out', 'generate_demo')
DESC = '验证登录页：输入账号密码后点击登录，应能进入首页'

# ---------------------------------------------------------------- 模型回复（离线脚本化）
# 真机上这两段由 LLM 产出；这里用 ScriptedProvider 固定住，好让示例可复现。

# 阶段① 的回复：测试点清单
REPLY_POINTS = """{"test_points": [
  {"kind": "功能", "title": "正常登录", "precondition": "应用停在登录页",
   "expect": "点击登录后进入首页"},
  {"kind": "异常", "title": "空密码登录", "precondition": "只填账号",
   "expect": "给出错误提示且停留在登录页"}
]}"""

# 阶段② 的回复：**故意含两处违规**（固定等待 + 硬编码坐标）
REPLY_CASE = """{"name": "登录页基本检查", "steps": [
  {"start": true},
  {"sleep": 2},
  {"tap_xy": {"x": 300, "y": 770}},
  {"assert": {"exists": {"text": "登录"}}}
]}"""


def hr(title):
    print('\n' + '=' * 72)
    print('  ' + title)
    print('=' * 72)


def main():
    # ---------------------------------------------------------------- 准备：模拟设备 + 控件树
    sim = FakeHdc(start_page='login', verbose=False)
    d = Driver(bundle='com.demo.app', ability='EntryAbility', hdc=sim,
               artifact_dir=os.path.join(OUT, 'artifacts'), verbose=False)
    tree = d.refresh()
    print('模拟设备已就绪：当前页 %r，控件树 %d 个节点'
          % (sim.current, len(list(tree.walk()))))

    provider = ScriptedProvider([REPLY_POINTS, REPLY_CASE])

    hr('阶段① 描述 → 测试点清单')
    points = parse_test_points_lenient(provider.complete('(测试点提示词)'))
    for p in points:
        print('  [%s] %s  期望=%s' % (p.kind_cn, p.title, p.expect))
    point = points[0]

    hr('阶段② 测试点 → DSL 草案')
    gen = Generator(provider=provider, bundle='com.demo.app', page=tree)
    case = gen.draft(DESC, point, tree)
    for i, st in enumerate(case.steps, 1):
        print('  %d. %s' % (i, st))

    hr('阶段③ 静态校验（红线扫描 + DSL 合法性 + 控件存在性）')
    issues = validate_case(case, tree)
    for it in issues:
        print('  [%s] 第 %s 步：%s' % (it.reason.cn, it.step_index, it.detail))
    print('  -> 共 %d 处问题' % len(issues))

    hr('阶段④ 本地修复（免费启发式，先于回 LLM）')
    fixed, notes = repair_locally(case, issues, tree)
    for n in notes:
        print('  ' + n)

    hr('阶段⑤ 再校验')
    issues2 = validate_case(fixed, tree)
    print('  剩余问题：%d 处' % len(issues2))
    print('  修复后步骤：')
    for i, st in enumerate(fixed.steps, 1):
        print('    %d. %s' % (i, st))

    hr('阶段⑥ 干跑（只执行无副作用步骤，有副作用的一步不跑）')
    res = dry_run(fixed, d)
    print('  干跑结论: ok=%s ｜ 真的执行 %d 步 ｜ 跳过 %d 步'
          % (res.ok, res.executed, res.skipped))
    for f in res.failures:
        print('  失败: %s' % f)

    hr('最终产物（可执行用例）')
    print(fixed.to_dsl())
    path = fixed.save(os.path.join(OUT, 'case.yaml'))
    print('已落盘: %s' % path)

    # ---------------------------------------------------------------- 一站式 API（实际用法）
    hr('一站式 API：Generator.generate() —— 上面六阶段它一次跑完')
    sim2 = FakeHdc(start_page='login', verbose=False)
    d2 = Driver(bundle='com.demo.app', ability='EntryAbility', hdc=sim2,
                artifact_dir=os.path.join(OUT, 'artifacts2'), verbose=False)
    gen2 = Generator(provider=ScriptedProvider([REPLY_POINTS, REPLY_CASE]),
                     bundle='com.demo.app', page=d2.refresh())
    out = gen2.generate(DESC, driver=d2)
    print('  ok=%s ｜ 可执行=%s ｜ 修复过=%s ｜ attempts=%d'
          % (out.ok, out.case is not None,
             bool(out.case and out.case.repaired), out.attempts))
    if not out.ok:
        print('  原因: [%s] %s' % (out.reason.cn, out.detail))
    print()
    print('  单条结果 to_dict():')
    print(json.dumps(out.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
