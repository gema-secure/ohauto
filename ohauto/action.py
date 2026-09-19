"""
L3 语义层 —— Action DSL
=======================

用声明式的步骤序列描述一个 UI 自动化用例。选择 YAML 作为中间表示，
是因为它同时满足三个角色：

    机器可生成  —— LLM 或多模态模型按固定 schema 产出步骤
    人可审阅    —— 纯文本、可 diff、可 review，避免「AI 直接操作真机」
    回归可重放  —— 解析后即可执行，天然成为回归测试用例

格式示例：

    name: 登录流程
    bundle: com.example.app
    ability: EntryAbility
    steps:
      - start: true
      - waitFor:  { text: "登录", timeout: 8000 }
      - input:    { id: "username", value: "alice" }
      - input:    { id: "password", value: "secret" }
      - tap:      { id: "btn_submit" }
      - assert:   { exists: { text: "首页" } }
      - assert:   { text: { id: "tv_title", equals: "我的账户" } }
      - swipe:    { direction: up, scale: 0.6 }
      - assert:   { gone: { text: "加载中" } }
      - screenshot: "final.png"
      - back: true
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .driver import Driver, DriverError
from .matcher import Matcher, ON

try:                                    # PyYAML 可选，缺失时降级为 JSON
    import yaml
    _HAS_YAML = True
except ImportError:                     # pragma: no cover
    _HAS_YAML = False


class DslError(ValueError):
    """DSL 解析错误。"""


# ---------------------------------------------------------------- 匹配器规格

# 规格字典里的键 → Matcher 构造方式
_SPEC_KEYS = {
    'text':          lambda m, v: m.text(v),
    'text_contains': lambda m, v: m.text_contains(v),
    'text_matches':  lambda m, v: m.text_matches(v),
    'text_in':       lambda m, v: m.text_in(v),
    'id':            lambda m, v: m.id(v),
    'id_contains':   lambda m, v: m.id(v, exact=False),
    'type':          lambda m, v: m.type(v),
    'type_contains': lambda m, v: m.type(v, exact=False),
    'descr':         lambda m, v: m.descr_contains(v),
    'label':         lambda m, v: m.label_contains(v),
    'clickable':     lambda m, v: m.clickable(bool(v)),
    'visible':       lambda m, v: m.visible(bool(v)),
    'enabled':       lambda m, v: m.enabled(bool(v)),
    'scrollable':    lambda m, v: m.scrollable(bool(v)),
}


def spec_to_matcher(spec: Any) -> Matcher:
    """把规格字典转成 Matcher。

    支持嵌套的 within 与 nth：

        {type: ListItem, text_contains: "订单", nth: 0}
        {text: "提交", within: {type: Scroll}}
    """
    if isinstance(spec, Matcher):
        return spec
    if isinstance(spec, str):
        # 裸字符串按「文本」处理，方便手写简写
        return ON.text(spec)
    if not isinstance(spec, dict):
        raise DslError(f'匹配器规格必须是 dict 或 str，收到 {type(spec)}')

    m = Matcher()
    within_spec = None
    nth_val = None
    unknown = []

    for k, v in spec.items():
        if k == 'within':
            within_spec = v
        elif k in ('nth', 'index'):
            nth_val = int(v)
        elif k in _SPEC_KEYS:
            m = _SPEC_KEYS[k](m, v)
        elif k == 'interactive':
            if v:
                m = m.interactive()
        elif k == 'size_at_least':
            if isinstance(v, dict):
                m = m.size_at_least(int(v.get('w', 0)), int(v.get('h', 0)))
            elif isinstance(v, (list, tuple)) and len(v) == 2:
                m = m.size_at_least(int(v[0]), int(v[1]))
        else:
            unknown.append(k)

    if unknown:
        raise DslError(f'未知的匹配器字段: {unknown}（可用: '
                       f'{sorted(list(_SPEC_KEYS) + ["within", "nth", "interactive", "size_at_least"])}）')

    if within_spec is not None:
        m = m.within(spec_to_matcher(within_spec))
    if nth_val is not None:
        m = m.nth(nth_val)
    return m


# ---------------------------------------------------------------- 执行

@dataclass
class RunReport:
    """一次 DSL 执行的结果。"""
    name: str = ''
    bundle: str = ''
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def to_dict(self) -> Dict[str, Any]:
        return {'name': self.name, 'bundle': self.bundle, 'total': self.total,
                'passed': self.passed, 'failed': self.failed,
                'ok': self.ok, 'elapsed_ms': self.elapsed_ms,
                'errors': self.errors}


def run_steps(driver: Driver, steps: List[Dict[str, Any]],
              stop_on_error: bool = True) -> RunReport:
    """执行步骤序列。每一步失败会被记录，默认中断后续步骤。"""
    import time
    rep = RunReport(bundle=driver.bundle, total=len(steps))
    t0 = time.time()

    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict) or not st:
            err = {'step': i, 'error': '步骤必须是非空字典', 'raw': str(st)[:200]}
            rep.failed += 1
            rep.errors.append(err)
            if stop_on_error:
                break
            continue
        action, arg = next(iter(st.items()))
        try:
            _exec_one(driver, action, arg)
            rep.passed += 1
        except Exception as e:
            rep.failed += 1
            rep.errors.append({'step': i, 'action': action,
                               'arg': _short(arg), 'error': str(e)[:500]})
            driver.log(f'步骤 {i} 失败 [{action}]: {e}')
            if stop_on_error:
                break

    rep.elapsed_ms = int((time.time() - t0) * 1000)
    return rep


def _short(v: Any, n: int = 160) -> str:
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s[:n] + ('...' if len(s) > n else '')


def exec_action(driver: Driver, action: str, arg: Any) -> None:
    """公开的单动作执行入口。

    `run_steps` 是「一条道走到黑」的朴素执行器，失败即中断；
    而 `runner.Runner` 需要在步骤级别插入重试、退避与设备恢复，
    因此把单动作执行单独暴露出来给它编排。两者共用同一套动作语义，
    保证「重试过的用例」和「直接跑的用例」行为完全一致。
    """
    _exec_one(driver, action, arg)


def _exec_one(driver: Driver, action: str, arg: Any) -> None:
    """执行单个动作。"""
    a = action.strip().lower()

    if a in ('start', 'launch'):
        driver.start(wait=bool(arg) if isinstance(arg, bool) else True)
    elif a == 'stop':
        driver.stop()
    elif a in ('tap', 'click'):
        driver.tap(spec_to_matcher(arg))
    elif a == 'tap_xy':
        x, y = (arg.get('x'), arg.get('y')) if isinstance(arg, dict) else arg
        driver.tap_xy(int(x), int(y))
    elif a in ('double_tap', 'doubleclick'):
        driver.double_tap(spec_to_matcher(arg))
    elif a in ('long_press', 'longclick'):
        driver.long_press(spec_to_matcher(arg))
    elif a in ('input', 'inputtext', 'fill'):
        if not isinstance(arg, dict) or 'value' not in arg:
            raise DslError('input 需要形如 {id: "x", value: "文本"}')
        spec, val = {k: v for k, v in arg.items() if k != 'value'}, arg['value']
        driver.input(spec_to_matcher(spec), str(val))
    elif a == 'swipe':
        if isinstance(arg, str):
            driver.swipe(arg)
        else:
            d = arg.get('direction', 'up')
            anchor = spec_to_matcher(arg['anchor']) if 'anchor' in arg else None
            node = driver.find(anchor) if anchor else None
            driver.swipe(d, float(arg.get('scale', 0.6)), anchor=node)
    elif a == 'fling':
        driver.fling(int(arg.get('direction', 2)) if isinstance(arg, dict) else int(arg),
                     int(arg.get('velocity', 800)) if isinstance(arg, dict) else 800)
    elif a in ('waitfor', 'wait_for'):
        spec, tmo = _split_timeout(arg)
        driver.wait_for(spec_to_matcher(spec), timeout=tmo)
    elif a in ('waitgone', 'wait_gone'):
        spec, tmo = _split_timeout(arg)
        driver.wait_gone(spec_to_matcher(spec), timeout=tmo)
    elif a in ('waitidle', 'wait_idle'):
        driver.wait_idle()
    elif a == 'assert':
        _exec_assert(driver, arg)
    elif a in ('screenshot', 'screencap'):
        driver.screenshot(str(arg) if arg else None)
    elif a in ('back', 'key_back'):
        driver.back()
    elif a in ('home', 'key_home'):
        driver.home()
    elif a == 'key':
        keys = arg if isinstance(arg, (list, tuple)) else [arg]
        driver.hdc.key_event(*keys)
    else:
        raise DslError(f'不支持的动作: {action}')


def _split_timeout(spec: Any) -> tuple:
    """把 spec 里的 timeout 摘出来，剩余的才是匹配条件。

    写成 `{exists: {text: "首页", timeout: 10000}}` 是最自然的表达，
    但 timeout 不是匹配器字段，必须在这里剥离，否则会被当成未知字段报错。
    """
    if isinstance(spec, dict) and 'timeout' in spec:
        rest = {k: v for k, v in spec.items() if k != 'timeout'}
        try:
            return rest, int(spec['timeout'])
        except (TypeError, ValueError):
            return rest, None
    return spec, None


def _exec_assert(driver: Driver, arg: Any) -> None:
    """断言：{exists: {...}} / {gone: {...}} / {text: {spec, equals}}"""
    if not isinstance(arg, dict) or len(arg) != 1:
        raise DslError('assert 需要形如 {exists: {..}} / {gone: {..}} / {text: {..}}')
    kind, val = next(iter(arg.items()))
    k = kind.strip().lower()

    if k == 'exists':
        spec, tmo = _split_timeout(val)
        driver.assert_exists(spec_to_matcher(spec), timeout=tmo)
    elif k == 'gone':
        spec, tmo = _split_timeout(val)
        driver.assert_gone(spec_to_matcher(spec), timeout=tmo)
    elif k in ('text', 'text_equals', 'equals'):
        if not isinstance(val, dict):
            raise DslError('assert.text 需要形如 {spec..., equals: "期望值"}')
        expected = val.get('equals', val.get('value'))
        if expected is None:
            raise DslError('assert.text 缺少 equals 字段')
        spec = {kk: vv for kk, vv in val.items()
                if kk not in ('equals', 'value')}
        driver.assert_text(spec_to_matcher(spec), str(expected))
    elif k == 'visible':
        spec, _ = _split_timeout(val)
        node = driver.find(spec_to_matcher(spec))
        if node is None:
            raise DriverError(f'断言失败：控件不可见 -> {spec}')
    else:
        raise DslError(f'不支持的断言类型: {kind}')


# ---------------------------------------------------------------- 序列化

def load_case(source: Any) -> Dict[str, Any]:
    """从 YAML 文本 / JSON 文本 / 文件路径 加载用例定义。"""
    if isinstance(source, dict):
        return source
    text = source
    if isinstance(source, str) and not source.lstrip().startswith(('{', 'name:', 'steps:')):
        if os.path.exists(source):
            with open(source, 'r', encoding='utf-8') as f:
                text = f.read()
    if isinstance(text, str) and text.lstrip().startswith('{'):
        return json.loads(text)
    if not _HAS_YAML:
        raise DslError('解析 YAML 需要 PyYAML，请先 pip install pyyaml（或改用 JSON 格式）')
    return yaml.safe_load(text)


def dump_case(case: Dict[str, Any]) -> str:
    """把用例定义序列化成 YAML（无 PyYAML 时退回 JSON）。"""
    if _HAS_YAML:
        return yaml.safe_dump(case, allow_unicode=True, sort_keys=False,
                              default_flow_style=False, width=100)
    return json.dumps(case, ensure_ascii=False, indent=2)


def run_case(driver: Driver, case: Dict[str, Any],
             stop_on_error: bool = True) -> RunReport:
    """执行一个用例定义（含 start / steps）。"""
    name = case.get('name', '')
    steps = case.get('steps') or []
    driver.log(f'开始执行用例: {name or "(未命名)"}  共 {len(steps)} 步')
    rep = run_steps(driver, steps, stop_on_error=stop_on_error)
    rep.name = name
    return rep


# ---------------------------------------------------------------- 从留痕反推脚本

def trace_to_steps(driver: Driver, include_waits: bool = True) -> List[Dict[str, Any]]:
    """把 Driver 的执行留痕转回 DSL 步骤 —— 脚本生成的雏形。

    这是「一次操作即一条用例」的关键：人工或模型驱动一次探索，
    留下的轨迹可直接沉淀成可重放的回归脚本。
    """
    out: List[Dict[str, Any]] = []
    for s in driver.steps:
        if not s.ok:
            continue
        k = s.kind
        if k == 'start':
            out.append({'start': True})
        elif k == 'tap' and s.coords:
            spec = _path_to_spec(s.node_path)
            out.append({'tap': spec} if spec else {'tap_xy': list(s.coords)})
        elif k == 'input' and s.value is not None:
            spec = _path_to_spec(s.node_path) or {}
            spec['value'] = s.value
            out.append({'input': spec})
        elif k == 'swipe':
            out.append({'swipe': {'direction': s.target or 'up', 'scale': s.value or 0.6}})
        elif k == 'back':
            out.append({'back': True})
        elif k == 'waitFor' and include_waits:
            out.append({'waitFor': _path_to_spec(s.node_path) or s.target})
        elif k.startswith('assert'):
            continue          # 断言由人工或模型补，留痕里不自动带
    return out


def _path_to_spec(path: Optional[str]) -> Dict[str, Any]:
    """把 'Column > Row > Button#btn_login' 解析成匹配器规格。

    优先用 id（最稳），其次用类型。真实场景应结合控件树补上 text。
    """
    if not path:
        return {}
    last = path.split('>')[-1].strip()
    if '#' in last:
        typ, cid = last.split('#', 1)
        return {'id': cid.strip()} if cid.strip() else {'type': typ.strip()}
    return {'type': last} if last else {}
