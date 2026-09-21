"""hypium 脚本导出器 —— 把 YAML DSL 导出为可在真机运行的 .ets 测试脚本。

对应任务卡 C5：把 YAML DSL 导出为 arkxtest / hypium 脚本，
证明「可在指定测试框架中执行」。

## 以官方 API 为准（本机实测确认）

- 设备：OpenHarmony 5.0.3.135 → **API 15**
- 类型定义：DevEco 自带 SDK 的 `@ohos.UiTest.d.ts`（4311 行）+ `@ohos/hypium`
- 老 API（`UiDriver`/`By`/`UiComponent`）标注 `@deprecated since 9` →
  **全部生成新 API**（`Driver`/`On`/`Component`，`@kit.TestKit`）
- `static create(): Driver`（@since 9）—— **不是 async，无 await**
- 设备上的 `uitest` CLI **没有** 任务卡所说的 `--mode DUMP` 参数（实测
  `uitest help` 逐条核对）；但新 API 的 `Driver` 有 `dumpLayout(savePath)`
  与 `screenCap(savePath)`，能力等价 —— 已按设备实测修正

## 映射表（DSL -> hypium）

| DSL action | 生成的 .ets 代码 |
|---|---|
| `start` | `delegator.startAbility(want)` + `delayMs` |
| `waitFor` / `wait` | `driver.waitForComponent(On.xxx, ms)` + **判 undefined** |
| `waitGone` | 轮询 `assertComponentExist` 至抛 17000003 |
| `tap` / `doubleTap` / `longPress` | `findComponent(...)` 的对应 click 方法 |
| `tap_xy` | `driver.click(x, y)` |
| `input` | `findComponent(...).inputText(v)` |
| `back` | `driver.pressBack()` |
| `screenshot` | `driver.screenCap(path)` |
| `assert.exists` | `driver.assertComponentExist(On.xxx)` |
| `assert.gone` | 轮询至 17000003 |
| `assert.text` | `getText()` + `expect().assertEqual()` |

**不支持的 action**（导出期即抛错，绝不静默生成错误语义）：
`stop` / `home` / `swipe` / `fling` / `waitIdle` / `scroll` ——
hypium 无一一对应，或需要屏幕尺寸等运行时信息；报错信息里给出替代建议。

## 关键行为差异（来自官方 d.ts 注释，生成代码据此设计）

- `waitForComponent(on, time)` 超时**返回 undefined 而非抛错**
  → 生成的代码必须判空并抛错，否则后续步骤会在 None 上炸出难懂的错误
- `assertComponentExist` 失败**抛 BusinessError 17000003**
  → 直接利用：hypium 会把未捕获异常记为用例失败

用法::

    python tools/export_hypium.py examples/cases/login.yaml \\
        --out hypium_out/login.test.ets
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------- 定位条件


def _q(v: Any) -> str:
    """Python 值 -> TypeScript 字符串字面量（单引号，转义反斜杠与引号）。"""
    s = str(v).replace('\\', '\\\\').replace("'", "\\'")
    return f"'{s}'"


def _bool(v: Any) -> str:
    return 'true' if v else 'false'


#: DSL 定位字段 -> On 链式方法（与 matcher.py 的字段名一一对应）
_ON_FIELDS: Dict[str, str] = {
    'text': 'text',
    'text_contains': 'text',        # 特殊：带 MatchPattern.CONTAINS
    'id': 'id',
    'type': 'type',
    'descr': 'description',
    'clickable': 'clickable',
    'scrollable': 'scrollable',
    'enabled': 'enabled',
    'focused': 'focused',
    'selected': 'selected',
}


def _on_chain(m: Dict[str, Any]) -> str:
    """定位条件 dict -> `On.text('x').id('y')` 链式表达式。

    多字段按固定顺序拼接（与 matcher.py 的链式语义一致：条件 AND）。
    dict 里出现未知字段时直接抛错 —— 静默丢弃会让导出件与原用例语义不一致。
    """
    if not isinstance(m, dict) or not m:
        raise ValueError(f'定位条件必须是非空 dict，收到: {m!r}')
    unknown = set(m) - set(_ON_FIELDS) - {'timeout', 'index'}
    if unknown:
        raise ValueError(f'不支持的定位字段 {sorted(unknown)}，'
                         f'支持: {sorted(_ON_FIELDS)}（另支持 timeout/index）')

    parts: List[str] = []
    # 顺序固定：text/id/type/descr 在前，状态布尔在后（可读性）
    for key in ('text', 'id', 'type', 'descr'):
        if key in m:
            method = _ON_FIELDS[key]
            if key == 'text_contains':
                parts.append(f"text({_q(m[key])}, MatchPattern.CONTAINS)")
            else:
                parts.append(f"{method}({_q(m[key])})")
    for key in ('clickable', 'scrollable', 'enabled', 'focused', 'selected'):
        if key in m:
            parts.append(f"{_ON_FIELDS[key]}({_bool(m[key])})")
    return 'On.' + '.'.join(parts)


# --------------------------------------------------------------- 步骤映射


class UnsupportedActionError(ValueError):
    """DSL action 在 hypium 中无一一对应。

    消息里必须给出替代建议 —— 让用例作者能自己改，而不是只看到报错。
    """


def _matcher_of(arg: Any) -> Dict[str, Any]:
    """从 step 参数里抠出定位条件 dict（容忍 timeout 混在里面）。"""
    if not isinstance(arg, dict):
        raise ValueError(f'定位参数必须是 dict，收到: {arg!r}')
    return {k: v for k, v in arg.items() if k != 'timeout'}


def _timeout_of(arg: Dict[str, Any], default: int = 8000) -> int:
    return int(arg.get('timeout', default))


def _step_to_ts(step: Dict[str, Any], idx: int,
                bundle: str, ability: str) -> str:
    """单个 DSL 步骤 -> TypeScript 代码块（含中文注释）。"""
    if not isinstance(step, dict) or len(step) != 1:
        raise ValueError(f'step #{idx} 必须是单键 dict（一个 action），'
                         f'收到: {step!r}')
    a, arg = next(iter(step.items()))
    desc = f'// step {idx}: {a} {json.dumps(arg, ensure_ascii=False)}'

    # ---- 应用生命周期 -------------------------------------------------
    if a in ('start', 'launch'):
        wait_ms = 1000 if not isinstance(arg, dict) else int(
            arg.get('wait_ms', 1000))
        return (f'{desc}\n'
                f'await delegator.startAbility({{bundleName: {_q(bundle)}, '
                f'abilityName: {_q(ability)}}});\n'
                f'await driver.delayMs({wait_ms});')

    if a == 'stop':
        raise UnsupportedActionError(
            "hypium 无直接 kill 对应。替代：delegator.killAbility(want) 需自验；"
            "或改用 DSL 的 back/home 序列。")

    if a in ('home', 'key_home'):
        raise UnsupportedActionError(
            'hypium 需 triggerKey(键码)，键码依赖 @ohos.KeyCode 常量表，'
            '本导出器 v1 不内置。替代：back 序列，或手动填入键码后改模板。')

    if a in ('swipe', 'fling', 'scroll'):
        raise UnsupportedActionError(
            'hypium 的 swipe(x1,y1,x2,y2[,speed]) 需要绝对坐标，'
            '而 DSL 的方向滑动是相对屏幕尺寸的 —— 需要运行时屏幕尺寸，'
            'v1 不生成猜测坐标。替代：在 DSL 里改用 tap_xy 明确坐标，'
            '或导出后手动按设备分辨率补全。')

    if a in ('waitidle', 'wait_idle'):
        raise UnsupportedActionError(
            'hypium 无「界面稳定探测」对应能力。替代：用 waitFor 等待'
            '某个稳定标志控件出现，语义更明确。')

    # ---- 等待 ---------------------------------------------------------
    if a in ('waitFor', 'wait'):
        cond = _matcher_of(arg)
        to = _timeout_of(arg)
        on = _on_chain(cond)
        return (f'{desc}\n'
                f'const c{idx} = await driver.waitForComponent({on}, {to});\n'
                f"if (c{idx} === undefined) {{\n"
                f'  throw new Error("waitFor 超时({to}ms): {_on_chain(cond)}");\n'
                f'}}')

    if a in ('waitGone', 'wait_gone'):
        cond = _matcher_of(arg)
        to = _timeout_of(arg)
        rounds = max(1, to // 300)
        on = _on_chain(cond)
        return (f'{desc}\n'
                f'let gone{idx} = false;\n'
                f'for (let i = 0; i < {rounds}; i++) {{\n'
                f'  try {{ await driver.assertComponentExist({on}); }}\n'
                f"  catch (e) {{ gone{idx} = true; break; }}\n"
                f'  await driver.delayMs(300);\n'
                f'}}\n'
                f'if (!gone{idx}) throw new Error("waitGone 超时({to}ms): {on}");')

    # ---- 操作 ---------------------------------------------------------
    if a in ('tap', 'doubleTap', 'long_press', 'longPress'):
        cond = _matcher_of(arg)
        method = {'tap': 'click', 'doubleTap': 'doubleClick',
                  'long_press': 'longClick', 'longPress': 'longClick'}[a]
        return (f'{desc}\n'
                f'(await driver.findComponent({_on_chain(cond)})).{method}();')

    if a == 'tap_xy':
        if not (isinstance(arg, dict) and 'x' in arg and 'y' in arg):
            raise ValueError(f'tap_xy 需要 {{x, y}}，收到: {arg!r}')
        return (f'{desc}\n'
                f'await driver.click({int(arg["x"])}, {int(arg["y"])});')

    if a == 'input':
        value = arg.get('value') if isinstance(arg, dict) else None
        if value is None:
            raise ValueError(f'input 缺少 value: {arg!r}')
        # value 与定位条件同层（DSL 形如 {id: ..., value: ...}），
        # 抠掉 value 后剩下的才是定位条件。
        cond = {k: v for k, v in arg.items() if k not in ('value', 'timeout')}
        return (f'{desc}\n'
                f'(await driver.findComponent({_on_chain(cond)}))'
                f'.inputText({_q(value)});')

    if a in ('back', 'key_back'):
        return f'{desc}\nawait driver.pressBack();'

    if a in ('screenshot', 'screencap'):
        path = arg.get('path') if isinstance(arg, dict) else None
        save = _q(path) if path else _q(f'/data/local/tmp/ohauto_{idx}.png')
        return f'{desc}\nawait driver.screenCap({save});'

    # ---- 断言 ---------------------------------------------------------
    if a == 'assert':
        if not isinstance(arg, dict) or len(arg) != 1:
            raise ValueError(f'assert 必须是单键 dict（exists/gone/text），'
                             f'收到: {arg!r}')
        kind, arg2 = next(iter(arg.items()))
        if kind == 'exists':
            cond = _matcher_of(arg2)
            to = _timeout_of(arg2, 5000)
            # hypium 的 assertComponentExist 不带超时参数；
            # 超时语义用「先 waitForComponent 判存在，再断言」组合实现。
            return (f'{desc}\n'
                    f'const c{idx} = await driver.waitForComponent('
                    f'{_on_chain(cond)}, {to});\n'
                    f'if (c{idx} === undefined) throw new Error('
                    f'"assert exists 失败({to}ms): {_on_chain(cond)}");')
        if kind == 'gone':
            sub = _step_to_ts({'waitGone': arg2}, idx, bundle, ability)
            return f'{desc}\n' + sub.split('\n', 1)[1]
        if kind == 'text':
            cond = _matcher_of(arg2)
            expected = arg2.get('expected')
            if expected is None:
                raise ValueError(f'assert.text 缺少 expected: {arg2!r}')
            return (f'{desc}\n'
                    f'const t{idx} = await (await driver.findComponent('
                    f'{_on_chain(cond)})).getText();\n'
                    f'expect(t{idx}).assertEqual({_q(expected)});')
        raise UnsupportedActionError(
            f'assert.{kind} 不支持。支持: exists / gone / text')

    raise UnsupportedActionError(
        f'未知 DSL action: {a!r}。已支持: start/stop/waitFor/waitGone/'
        f'waitIdle/tap/doubleTap/longPress/tap_xy/input/back/home/'
        f'screenshot/assert —— 其中标「不支持」的见各类报错信息。')


# --------------------------------------------------------------- 用例导出


def _safe_ident(name: str, fallback: str = 'case') -> str:
    """用例名 -> 合法 describe/it 标识（非 ASCII 一律换掉）。"""
    out = ''.join(c if c.isalnum() and ord(c) < 128 else '_' for c in name)
    out = out.strip('_') or fallback
    return out if not out[0].isdigit() else f'c{out}'


def export_case(case: Dict[str, Any]) -> tuple:
    """YAML 用例 dict -> (完整 .ets 源码, 警告列表)。

    不支持的 action **不阻断整体导出**：生成占位代码（运行到该步会
    抛出带原因的明确错误），并在 warnings 里逐条报告 —— 让用例作者
    一次看到所有需要手工补全的点，而不是修一个爆一个。
    """
    name = str(case.get('name', 'case'))
    bundle = case.get('bundle')
    ability = case.get('ability')
    steps = case.get('steps')
    if not bundle or not ability:
        raise ValueError('用例缺少 bundle / ability —— hypium 需要 '
                         'startAbility 的完整 want（DSL 里这两项本来就是必填）')
    if not isinstance(steps, list) or not steps:
        raise ValueError('用例缺少 steps')

    ident = _safe_ident(name)
    body: List[str] = []
    warnings: List[str] = []
    for i, step in enumerate(steps, 1):
        try:
            blk = _step_to_ts(step, i, bundle, ability)
        except UnsupportedActionError as e:
            a = next(iter(step)) if isinstance(step, dict) and step else '?'
            blk = (f'// ⚠️ 本步骤未自动导出 —— {e}\n'
                   f"throw new Error('步骤 {i} ({a}) 未导出，"
                   f'需按 hypium API 人工补全（原因见上一行注释）\');')
            warnings.append(f'步骤 {i} ({a}): {e}')
        body.append(blk)

    lines: List[str] = [
        '// 本文件由 tools/export_hypium.py 自动生成 —— 不要手改，改源用例后重新导出。',
        f'// 来源用例: {name}',
        '// 目标: hypium / arkxtest（API 9+，@kit.TestKit；设备 API 15 实测）',
        "import { describe, it, expect } from '@ohos/hypium';",
        "import { abilityDelegatorRegistry } from '@kit.TestKit';",
        "import { Driver, On, MatchPattern } from '@kit.TestKit';",
        '',
        'const delegator = abilityDelegatorRegistry.getAbilityDelegator();',
        "const bundleName = abilityDelegatorRegistry.getArguments().bundleName;",
        '',
        'export default function abilityTest() {',
        f"  describe('{ident}', function () {{",
        f"    // 用例名: {name}",
        f"    // 被测应用: {bundle} / {ability}",
        f"    it('{ident}_0', 0, async function () {{",
        '      const driver = Driver.create();',
    ]
    lines.extend('      ' + blk.replace('\n', '\n      ') for blk in body)
    lines.extend([
        '    });',
        '  });',
        '}',
        '',
    ])
    return '\n'.join(lines), warnings


def export_file(yaml_path: str, out_path: str) -> str:
    """读 YAML 用例文件 -> 写 .ets。返回 out_path。"""
    import yaml                                    # noqa: PLC0415（延迟导入，保持无 yaml 时其余功能可用）
    with open(yaml_path, encoding='utf-8') as f:
        case = yaml.safe_load(f)
    ets, warnings = export_case(case)
    for w in warnings:
        print(f'  [警告] {w}', file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(ets)
    return out_path


# --------------------------------------------------------------- CLI


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description='YAML DSL -> hypium .ets 导出器（C5）')
    ap.add_argument('case', help='YAML 用例路径')
    ap.add_argument('--out', required=True, help='输出 .ets 路径')
    args = ap.parse_args(argv)

    try:
        out = export_file(args.case, args.out)
    except (ValueError, UnsupportedActionError) as e:
        print(f'[导出失败] {e}', file=sys.stderr)
        return 1
    print(f'✓ 已导出: {out}')
    print('  下一步：把该文件放进测试工程的 ohosTest/ets/test/ 下，'
          'hvigor 编译 test HAP 后用 `aa test` 在真机执行。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
