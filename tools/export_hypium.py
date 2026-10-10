"""hypium 脚本导出器 —— 把 YAML DSL 导出为可在真机运行的 .ets 测试脚本。

把 YAML DSL 导出为 arkxtest / hypium 脚本，
证明「可在指定测试框架中执行」。

## 以官方 API 为准（本机实测确认）

- 设备：OpenHarmony 5.0.3.135 → **API 15**
- 类型定义：DevEco 自带 SDK 的 `@ohos.UiTest.d.ts`（4311 行）+ `@ohos/hypium`
- 老 API（`UiDriver`/`By`/`UiComponent`）标注 `@deprecated since 9` →
  **全部生成新 API**（`Driver`/`ON`/`Component`，`@kit.TestKit`）
- `static create(): Driver`（@since 9）—— **不是 async，无 await**
- 设备上的 `uitest` CLI **没有** `--mode DUMP` 参数（实测
  `uitest help` 逐条核对）；但新 API 的 `Driver` 有 `dumpLayout(savePath)`
  与 `screenCap(savePath)`，能力等价 —— 已按设备实测修正

## 映射表（DSL -> hypium）

| DSL action | 生成的 .ets 代码 |
|---|---|
| `start` | `delegator.startAbility(want)` + `delayMs` |
| `waitFor` / `wait` | `driver.waitForComponent(ON.xxx, ms)` + **判 undefined** |
| `waitGone` | 轮询 `assertComponentExist` 至抛 17000003 |
| `tap` / `doubleTap` / `longPress` | `findComponent(...)` 的对应 click 方法 |
| `tap_xy` | `driver.click(x, y)` |
| `swipe` | `getDisplaySize()` + 方向/比例换算 → `driver.swipe(x1,y1,x2,y2)` |
| `input` | `findComponent(...).inputText(v)` |
| `back` | `driver.pressBack()` |
| `screenshot` | `driver.screenCap(path)` |
| `assert.exists` | `driver.assertComponentExist(ON.xxx)` |
| `assert.gone` | 轮询至 17000003 |
| `assert.text` | `getText()` + `expect().assertEqual()` |

**`swipe` 的坐标从哪来（C9）**：DSL 的方向滑动是相对屏幕尺寸的，而 hypium 的
`swipe(x1,y1,x2,y2)` 要绝对坐标。屏幕尺寸在**设备上运行时**用
`driver.getDisplaySize()` 取，导出期不猜分辨率；换算公式逐字复刻
`ohauto/driver.py` 的 `Driver.swipe`（同源 `generator.swipe_endpoints`）。
DSL 里带 `anchor` 时先 `findComponent(...).getBounds()`，在控件矩形内换算。

**不支持的 action**（导出期即抛错，绝不静默生成错误语义）：
`stop` / `home` / `fling` / `scroll` / `waitIdle` —— hypium 无一一对应，
报错信息里给出替代建议。

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


#: DSL 定位字段 -> ON 链式方法（与 matcher.py 的字段名一一对应）
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
    """定位条件 dict -> `ON.text('x').id('y')` 链式表达式。

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
    # 顺序固定：text/text_contains/id/type/descr 在前，状态布尔在后（可读性）
    #
    # ⚠️ `text_contains` **必须**在这个元组里 —— 它只出现在循环里才是可达的。
    # 早前的元组里漏了它，于是：`_ON_FIELDS` 认这个字段（第 93 行的未知字段检查
    # 放行），但循环永远走不到 → **静默丢弃一个文本条件**，导出件与实际用例语义不一致。
    # 这正好撞上本函数 docstring 自己写的那句「静默丢弃会让导出件与原用例语义不一致」。
    # 生成头部的 `import { MatchPattern } from '@kit.TestKit'` 一直是有的，
    # 所以漏的不是 import，纯粹是这行元组。
    for key in ('text', 'text_contains', 'id', 'type', 'descr'):
        if key in m:
            if key == 'text_contains':
                parts.append(f"text({_q(m[key])}, MatchPattern.CONTAINS)")
            else:
                parts.append(f"{_ON_FIELDS[key]}({_q(m[key])})")
    for key in ('clickable', 'scrollable', 'enabled', 'focused', 'selected'):
        if key in m:
            parts.append(f"{_ON_FIELDS[key]}({_bool(m[key])})")
    # 注意：这里必须用大写 ON。
    # d.ts 中 `declare class On` 只有实例方法 text/id/type/...（无 static），
    # 而 `declare const ON: On`（@ohos.UiTest.d.ts:4475）才是可链式调用的预置
    # 实例，官方注释的用法即 ON.text('txt').enabled(true)。
    # 写成 On.text(...) 会触发 ArkTS 编译错误：
    #   Property 'text' does not exist on type 'typeof On'.
    if not parts:
        # 拼出来会是 `ON.` —— 那是一份**能生成却编译不过**的 .ets。
        # 宁可在这一步炸掉，也不能让「生成成功」掩盖「语义是空的」。
        raise ValueError(f'定位条件里没有任何可用字段（只剩 {sorted(m)}）：{m!r}'
                         f' —— 拼出来会是 "ON."，属非法 .ets；'
                         f'可用字段: {sorted(_ON_FIELDS)}')
    return 'ON.' + '.'.join(parts)


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

    if a == 'swipe':
        # C9：DSL 的方向滑动是**相对屏幕尺寸**的，hypium 的 swipe 要绝对坐标。
        # 关键取舍：屏幕尺寸在**设备上运行时**取（`driver.getDisplaySize()`），
        # 而不是在导出期按某个分辨率猜一组坐标 —— 猜错的分辨率会让滑动要么
        # 划不动、要么把终点甩出屏幕，而文件里看不出来。
        # 坐标算法逐字复刻 `driver.Driver.swipe`（`generator.swipe_endpoints`
        # 是同一份），否则同一条用例在「引擎执行」与「hypium 执行」下终点不一致。
        if isinstance(arg, str):
            direction, scale, anchor = arg, 0.6, None
        elif isinstance(arg, dict):
            direction = arg.get('direction', 'up')
            scale = float(arg.get('scale', 0.6))
            anchor = arg.get('anchor')
        else:
            raise ValueError(f'swipe 参数必须是方向字符串或 dict，收到: {arg!r}')
        d = str(direction).strip().lower()
        if d not in ('up', 'down', 'left', 'right'):
            raise ValueError(f'swipe.direction 必须是 up/down/left/right，'
                             f'收到: {direction!r}')

        if anchor is None:
            head = (f'const size{idx} = await driver.getDisplaySize();\n'
                    f'const l{idx} = 0, t{idx} = 0;\n'
                    f'const w{idx} = size{idx}.x, h{idx} = size{idx}.y;')
        else:
            if not isinstance(anchor, dict):
                raise ValueError(f'swipe.anchor 必须是定位条件 dict，收到: {anchor!r}')
            # 容器内滑动：先定位控件、取运行时的 bounds，再在其矩形内换算。
            head = (f'const anc{idx} = await driver.findComponent('
                    f'{_on_chain(anchor)});\n'
                    f'const box{idx} = await anc{idx}.getBounds();\n'
                    f'const l{idx} = box{idx}.left, t{idx} = box{idx}.top;\n'
                    f'const w{idx} = box{idx}.right - box{idx}.left;\n'
                    f'const h{idx} = box{idx}.bottom - box{idx}.top;')

        # Python 侧：cx=(left+right)//2 == left+w//2，故此处 Math.floor(l + w/2) 等价
        cx = f'cx{idx}'
        cy = f'cy{idx}'
        calc = [f'const cx{idx} = Math.floor(l{idx} + w{idx} / 2);',
                f'const cy{idx} = Math.floor(t{idx} + h{idx} / 2);']
        if d == 'up':
            calc.append(f'const dy{idx} = Math.floor(h{idx} * {scale} / 2);')
            p = (cx, f'{cy} + dy{idx}', cx, f'{cy} - dy{idx}')
        elif d == 'down':
            calc.append(f'const dy{idx} = Math.floor(h{idx} * {scale} / 2);')
            p = (cx, f'{cy} - dy{idx}', cx, f'{cy} + dy{idx}')
        elif d == 'left':
            calc.append(f'const dx{idx} = Math.floor(w{idx} * {scale} / 2);')
            p = (f'{cx} + dx{idx}', cy, f'{cx} - dx{idx}', cy)
        else:
            calc.append(f'const dx{idx} = Math.floor(w{idx} * {scale} / 2);')
            p = (f'{cx} - dx{idx}', cy, f'{cx} + dx{idx}', cy)
        return (f'{desc}\n'
                f'{head}\n'
                + '\n'.join(calc) + '\n'
                f'await driver.swipe({p[0]}, {p[1]}, {p[2]}, {p[3]});')

    if a in ('fling', 'scroll'):
        raise UnsupportedActionError(
            'hypium 的 fling 用的是 UiDirection 枚举而非 DSL 的 0/1/2/3，'
            'scroll 更是依赖具体可滚动控件 —— 二者语义都无法一一对应。'
            '替代：改用 swipe(direction, scale)，它已支持导出。')

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
        # 外层 await 不能省。uitest 的 API 禁止并发调用：click() 返回 Promise，
        # 不 await 的话下一条 findComponent 会和它撞车，真机报
        #   uitest-api dose not allow calling concurrently,
        #   current processing:Component.click, incoming: On.id
        return (f'{desc}\n'
                f'await (await driver.findComponent({_on_chain(cond)})).{method}();')

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
        # 同上，inputText() 也必须 await，否则与后续操作并发。
        return (f'{desc}\n'
                f'await (await driver.findComponent({_on_chain(cond)}))'
                f'.inputText({_q(value)});')

    if a in ('back', 'key_back'):
        return f'{desc}\nawait driver.pressBack();'

    if a in ('screenshot', 'screencap'):
        path = arg.get('path') if isinstance(arg, dict) else None
        name = os.path.basename(path) if path else f'ohauto_{idx}.png'
        # screenCap 只能写**应用自己的沙箱目录**。测试进程没有权限往
        # /data/local/tmp 写，原样透传宿主侧路径真机会报
        #   Invalid file path:/data/local/tmp/ohauto_8.png
        # 所以只取文件名，落到 delegator 的 filesDir 下；宿主侧要取图再用
        # hdc file recv 从沙箱目录拉。
        return (f'{desc}\n'
                f'const shot{idx} = delegator.getAppContext().filesDir '
                f'+ {_q("/" + name)};\n'
                f'await driver.screenCap(shot{idx});')

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

    # 已知但**导出器未实现**的 action（DSL 里有、hypium 侧没有一一对应）。
    # `fling` / `scroll` 在更上面单独报错（有各自的替代建议），不重复列在这里。
    known_unsupported = {
        'stop': 'hypium 无停止 ability 的等价 API',
        'home': 'hypium 无返回桌面 API（可改用 back，或 start 目标应用）',
        'waitIdle': 'hypium 无「连续两次控件树一致」的等价判据，'
                    '可改用固定 waitFor，或去掉该步',
    }
    if a in known_unsupported:
        raise UnsupportedActionError(
            f'DSL action {a!r} 导出器未实现：{known_unsupported[a]}。'
            f'已实现的 action: start/wait/waitFor/waitGone/tap/doubleTap/'
            f'longPress/tap_xy/swipe/input/back/screenshot/assert')
    raise UnsupportedActionError(
        f'未知 DSL action: {a!r}。已实现的 action: start/wait/waitFor/'
        f'waitGone/tap/doubleTap/longPress/tap_xy/swipe/input/back/'
        f'screenshot/assert')


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
    ]
    # ★ 不完整横幅
    # 起因：评审发现 hypium_out/note_stability.test.ets 里有 2 个 waitIdle 未导出，
    # 而「稳定性压测通过」的结论建立在这条脚本上 —— 但**文件自己不说**，
    # 报告里只看到 Pass，于是「跑通」被读成了「整条用例验证通过」。
    # 这和 login.yaml 踩过的坑是同一类：一次挂在无关原因上的失败，
    # 会把真正的问题盖住；反过来，一次「跑通」也会把没覆盖到的步骤盖住。
    # 所以让文件**自己声明不完整**，而不是靠人去比对 warnings。
    if warnings:
        lines += [
            '//',
            f'// ⚠️⚠️ 本文件**不完整**：{len(warnings)} 个步骤未能自动导出。',
            '//   跑到那些步骤会抛 "步骤 N 未导出" —— 此时用例失败的原因是',
            '//   **工具没导出该步骤，不是被测应用有缺陷**。',
            '//   ⚠️ 反过来：本文件「跑通」只代表**已导出的步骤**通过，',
            '//   **不能**读成「整条用例验证通过」。',
            '//',
        ]
        for w in warnings:
            lines.append(f'//   · {w}')
        lines.append('//')
    lines += [
        "import { describe, it, expect } from '@ohos/hypium';",
        "import { abilityDelegatorRegistry } from '@kit.TestKit';",
        "import { Driver, ON, MatchPattern } from '@kit.TestKit';",
        '',
        'const delegator = abilityDelegatorRegistry.getAbilityDelegator();',
        "const bundleName = abilityDelegatorRegistry.getArguments().bundleName;",
        '',
        'export default function abilityTest() {',
        # describe/it 的回调必须是箭头函数：ArkTS 禁止 function expression
        # （arkts-no-func-expressions）。
        f"  describe('{ident}', () => {{",
        f"    // 用例名: {name}",
        f"    // 被测应用: {bundle} / {ability}",
        f"    it('{ident}_0', 0, async () => {{",
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
