"""
L3 语义层 —— Driver 门面
========================

把「语义定位」翻译成「坐标操作」的中枢，串起完整的操作循环：

    定位：dumpLayout → 解析控件树 → 匹配器筛选 → 取 bounds 中心
    操作：uiInput 注入坐标事件
    等待：waitFor 轮询 / waitForIdle 界面稳定判定
    断言：assert_exists / assert_text / assert_gone
    留痕：每步自动记录（截图 + 控件树 + 操作），供报告与脚本生成

这是整个能力层唯一需要「懂业务」的地方，其余各层都是纯机械转换。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .hdc import DeviceNotFound, Hdc, HdcError
from .layout import LayoutNode, Rect, flatten, parse_layout
from .matcher import Matcher


class DriverError(RuntimeError):
    """Driver 层错误（定位失败、断言失败、超时等）。"""


# 未配置 artifact_dir 时产物的默认落盘目录。
# **故意不放在进程当前目录** —— 那会在「你恰好所在的目录」留下垃圾，
# 项目根实测被污染过 7 个文件。放在系统临时目录下与项目彻底解耦。
DEFAULT_ARTIFACT_DIR = os.path.join(tempfile.gettempdir(), 'ohauto_artifacts')


# ---------------------------------------------------------------- 步骤记录

@dataclass
class Step:
    """一次操作的完整留痕 —— 报告与脚本生成的原始素材。"""
    index: int
    kind: str                       # tap / input / swipe / waitFor / assert ...
    target: str = ''                # 匹配器描述
    value: Any = None               # 输入文本等
    ok: bool = True
    elapsed_ms: int = 0
    screenshot: Optional[str] = None
    layout_json: Optional[str] = None
    node_path: Optional[str] = None
    #: 可定位规格（id/text/text_deep/type）—— 挑战 #6 用例沉淀的原料。
    #: 只记客观属性、**绝不记坐标**（红线）；留痕只有 node_path(type+id) 时，
    #: 真机 id 覆盖率仅 5.62%，沉淀出的用例会退化成按 type 歧义匹配。
    node_spec: Optional[Dict[str, Any]] = None
    coords: Optional[Tuple[int, int]] = None
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {'index': self.index, 'kind': self.kind, 'target': self.target,
             'value': self.value, 'ok': self.ok, 'elapsed_ms': self.elapsed_ms}
        for k in ('screenshot', 'layout_json', 'node_path', 'error'):
            v = getattr(self, k)
            if v:
                d[k] = v
        if self.node_spec:
            d['node_spec'] = dict(self.node_spec)
        if self.coords:
            d['coords'] = list(self.coords)
        if self.extra:
            d.update(self.extra)
        return d


# ---------------------------------------------------------------- Driver

class Driver:
    """OpenHarmony 应用 UI 自动化驱动器。

    Parameters
    ----------
    bundle:       被测应用包名，如 'com.example.app'
    ability:      入口 Ability 名，默认 'EntryAbility'
    hdc:          自定义 Hdc 实例；为 None 时自动创建
    artifact_dir: 留痕目录（截图 / 控件树 / 报告），为 None 则不落盘
    default_timeout: waitFor 默认超时（毫秒）
    poll_interval:   waitFor 轮询间隔（毫秒）
    sleep_fn:        休眠函数，默认 time.sleep。单测里注入空实现可让
                     50 步连续执行的用例从两分钟压到几秒 —— 等待逻辑本身
                     的正确性不依赖真实时间流逝。
    """

    def __init__(
        self,
        bundle: str,
        ability: str = 'EntryAbility',
        hdc: Optional[Hdc] = None,
        artifact_dir: Optional[str] = None,
        default_timeout: int = 8000,
        poll_interval: int = 300,
        verbose: bool = True,
        sleep_fn: Callable[[float], None] = time.sleep,
        idle_error_tolerance: int = 3,
    ):
        self.bundle = bundle
        self.ability = ability
        self.hdc = hdc or Hdc()
        self.artifact_dir = artifact_dir
        self.default_timeout = default_timeout
        self.poll_interval = poll_interval
        self.verbose = verbose
        self._sleep = sleep_fn
        # wait_idle 期间允许的「连续取不到控件树签名」次数。偶发超时被吸收，
        # 连续失败则上报 —— 见 _signature 的说明。
        self.idle_error_tolerance = max(1, idle_error_tolerance)
        self._sig_errors = 0

        self.steps: List[Step] = []
        self._root: Optional[LayoutNode] = None
        self._step_no = 0
        self._seq = 0

        if self.artifact_dir:
            os.makedirs(self.artifact_dir, exist_ok=True)

    # -------------------------------------------------------- 上下文

    def __enter__(self) -> 'Driver':
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """收尾：可选地停掉被测应用。默认不动，避免影响手工排查。"""
        pass

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f'[ohauto] {msg}')

    # -------------------------------------------------------- 留痕

    def _art(self, ext: str) -> Optional[str]:
        if not self.artifact_dir:
            return None
        self._seq += 1
        return os.path.join(self.artifact_dir, f'{self._seq:04d}_{ext}')

    def _record(self, step: Step) -> None:
        self.steps.append(step)
        if self.verbose:
            flag = 'OK ' if step.ok else 'FAIL'
            tgt = f' {step.target}' if step.target else ''
            val = f' {step.value!r}' if step.value is not None else ''
            coord = f' @{step.coords}' if step.coords else ''
            err = f'  <- {step.error}' if step.error else ''
            self.log(f'{flag} #{step.index} {step.kind}{tgt}{val}{coord} '
                     f'({step.elapsed_ms}ms){err}')

    def _new_step(self, kind: str, target: str = '', value: Any = None) -> Step:
        self._step_no += 1
        return Step(index=self._step_no, kind=kind, target=target, value=value)

    @staticmethod
    def _fill_node_spec(step: Step, node: LayoutNode) -> None:
        """留痕附带可定位规格 —— 挑战 #6 沉淀用例的原料。

        只记「重放时能重新匹配到」的客观属性：
          id 最稳（但真机覆盖率仅 5.62%）；文案次之 —— 可点容器自身
          id/text 常全空，文案在子节点上，所以 text 空时取 text_deep 兜底；
          type 不进 spec（同类型成堆，沉淀侧拿它必然歧义）。
        """
        spec: Dict[str, Any] = {}
        if node.id:
            spec['id'] = node.id
        text = (node.text or '').strip() or (getattr(node, 'text_deep', '') or '').strip()
        if text:
            spec['text'] = text
        if spec:
            step.node_spec = spec

    # -------------------------------------------------------- 控件树

    def refresh(self, unfiltered: bool = False, with_attrs: bool = False,
                save: bool = False) -> LayoutNode:
        """重新拉取并解析控件树。

        重要：坐标随折叠 / 旋转 / 滚动变化，**每次操作前都应刷新**，不要缓存。
        """
        dev = self.hdc.dump_layout(unfiltered=unfiltered, with_attrs=with_attrs)
        local = None
        if save and self.artifact_dir:
            local = self._art('layout.json')
            self.hdc.pull(dev, local)
            self._root = parse_layout(local)
        else:
            # 无需留痕时，直接读设备侧内容，省一次文件落地
            res = self.hdc.shell(f'cat {dev}')
            self._root = parse_layout(res.stdout)
        return self._root

    @property
    def root(self) -> LayoutNode:
        if self._root is None:
            self.refresh()
        return self._root  # type: ignore[return-value]

    def dump_text(self, limit: int = 200) -> str:
        """打印控件树，便于人工核对真实结构。"""
        lines = self.root.describe().splitlines()
        if len(lines) > limit:
            lines = lines[:limit] + [f'... （共 {len(lines)} 行，已截断）']
        return '\n'.join(lines)

    # -------------------------------------------------------- 定位

    def find(self, m: Matcher, refresh: bool = True) -> Optional[LayoutNode]:
        """查找单个控件；找不到返回 None。"""
        root = self.refresh() if refresh else self.root
        hits = m.filter(flatten(root))
        return hits[0] if hits else None

    def find_all(self, m: Matcher, refresh: bool = True,
                 include_invisible: bool = False) -> List[LayoutNode]:
        root = self.refresh() if refresh else self.root
        pool = list(root.walk()) if include_invisible else flatten(root)
        return m.filter(pool)

    def require(self, m: Matcher, refresh: bool = True) -> LayoutNode:
        """查找单个控件；找不到直接抛 DriverError（带控件树摘要，便于排查）。"""
        t0 = time.time()
        node = self.find(m, refresh=refresh)
        if node is None:
            top = self._tree_hint()
            raise DriverError(
                f'未找到控件: {m}\n'
                f'当前页面可交互控件摘要:\n{top}'
            )
        return node

    def _tree_hint(self, limit: int = 25) -> str:
        """定位失败时给出线索，避免只报「找不到」。"""
        try:
            nodes = flatten(self.root, only_interactive=True)
            lines = [f'  {n.type:<14} id={n.id[:22]:<22} text={n.text[:20]!r}'
                     for n in nodes[:limit]]
            if not lines:
                nodes = flatten(self.root)[:limit]
                lines = [f'  {n.type:<14} id={n.id[:22]:<22} text={n.text[:20]!r}'
                         for n in nodes]
            return '\n'.join(lines) or '  （控件树为空）'
        except Exception as e:                       # 诊断信息本身不能反过来报错
            return f'  （无法生成摘要: {e}）'

    # -------------------------------------------------------- 等待

    def wait_for(self, m: Matcher, timeout: Optional[int] = None,
                 interval: Optional[int] = None,
                 _record: bool = True) -> LayoutNode:
        """轮询直到控件出现 —— 对应 UiTest 的 waitForComponent。

        _record：内部调用（如 assert_exists）时传 False，避免同一次失败
        被记录成「waitFor + assert」两条，污染报告。
        """
        timeout = timeout if timeout is not None else self.default_timeout
        interval = interval or self.poll_interval
        step = self._new_step('waitFor', str(m)) if _record else None
        t0 = time.time()
        deadline = t0 + timeout / 1000.0
        try:
            while time.time() < deadline:
                node = self.find(m, refresh=True)
                if node is not None:
                    if step is not None:
                        step.node_path = node.path
                        self._fill_node_spec(step, node)
                    return node
                self._sleep(interval / 1000.0)
            raise DriverError(f'{timeout}ms 内未等到控件: {m}\n'
                              f'当前可交互控件:\n{self._tree_hint()}')
        except Exception as e:
            if step is not None:
                step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            if step is not None:
                step.elapsed_ms = int((time.time() - t0) * 1000)
                self._record(step)

    def wait_gone(self, m: Matcher, timeout: Optional[int] = None,
                  interval: Optional[int] = None,
                  _record: bool = True) -> None:
        """轮询直到控件消失。"""
        timeout = timeout if timeout is not None else self.default_timeout
        interval = interval or self.poll_interval
        step = self._new_step('waitGone', str(m)) if _record else None
        t0 = time.time()
        try:
            deadline = time.time() + timeout / 1000.0
            while time.time() < deadline:
                if self.find(m, refresh=True) is None:
                    return
                self._sleep(interval / 1000.0)
            raise DriverError(f'{timeout}ms 内控件仍未消失: {m}')
        except Exception as e:
            if step is not None:
                step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            if step is not None:
                step.elapsed_ms = int((time.time() - t0) * 1000)
                self._record(step)

    def wait_idle(self, stable_rounds: int = 2, interval: int = 350,
                  timeout: Optional[int] = None) -> bool:
        """等待界面稳定：连续 N 次控件树签名一致即认为空闲。

        等价于 UiTest 的 waitForIdle —— 点击后用它判断页面跳转完成。
        """
        timeout = timeout if timeout is not None else self.default_timeout
        deadline = time.time() + timeout / 1000.0
        last, same = None, 0
        while time.time() < deadline:
            sig = self._signature()
            if sig == last:
                same += 1
                if same >= stable_rounds - 1:
                    return True
            else:
                same = 0
                last = sig
            self._sleep(interval / 1000.0)
        return False

    def _signature(self) -> str:
        """控件树的轻量签名，用于稳定性判定。

        这里对异常的处理是**分级**的，不要改成一律吞掉：

        * `DeviceNotFound` —— 设备没了，重试多少次都白搭，**立刻向上抛**，
          让上层的设备看护（runner.DeviceGuard）接管。这是 执行引擎 设备自愈
          唯一的触发信号。
        * 其他异常（偶发 hdc 超时、控件树解析异常）—— 属于「这一次签名没取到」，
          返回一个必定不相等的标记即可。但**连续**取不到就要抛：那说明不是
          偶发抖动，而是设备真的有问题。

        早期版本一律吞掉并返回带时间戳的错误串，后果是 wait_idle 永远判不出
        「稳定」而返回 False，调用方又忽略这个 False —— 设备掉线被静默降级成
        「界面有点慢」，用例照常报通过，设备自愈永远等不到触发时机。
        """
        try:
            root = self.refresh()
        except DeviceNotFound:
            self._sig_errors = 0
            raise
        except Exception:
            self._sig_errors += 1
            if self._sig_errors >= self.idle_error_tolerance:
                self._sig_errors = 0
                raise
            return f'<err:{time.time()}>'
        self._sig_errors = 0
        parts = [f'{n.type}|{n.id}|{n.text}'
                 for n in flatten(root, only_visible=True)]
        return ';'.join(parts)

    # -------------------------------------------------------- 操作

    def tap(self, target, timeout: Optional[int] = None,
            post_idle: bool = True) -> LayoutNode:
        """点击：target 可为 Matcher 或 LayoutNode。"""
        step = self._new_step('tap')
        t0 = time.time()
        try:
            node = self._resolve_target(target, timeout)
            x, y = node.center
            step.target = str(target)
            step.node_path, step.coords = node.path, (x, y)
            self._fill_node_spec(step, node)
            if self.artifact_dir:
                step.screenshot = self._art('before_tap.png')
                self.hdc.pull(self.hdc.screen_cap(), step.screenshot)
            self.hdc.click(x, y)
            if post_idle:
                self.wait_idle(timeout=timeout or self.default_timeout)
            return node
        except Exception as e:
            step.ok, step.error = False, str(e)[:400]
            step.target = step.target or str(target)
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def tap_xy(self, x: int, y: int) -> None:
        """直接点坐标 —— 视觉通道定位到目标后走这条路。"""
        step = self._new_step('tap_xy', value=(x, y))
        step.coords = (x, y)
        t0 = time.time()
        try:
            self.hdc.click(x, y)
            self.wait_idle(timeout=self.default_timeout)
        except Exception as e:
            step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def double_tap(self, target, timeout: Optional[int] = None) -> LayoutNode:
        node = self._resolve_target(target, timeout)
        self.hdc.double_click(*node.center)
        self.wait_idle(timeout=self.default_timeout)
        return node

    def long_press(self, target, timeout: Optional[int] = None,
                   ms: int = 800) -> LayoutNode:
        node = self._resolve_target(target, timeout)
        step = self._new_step('longPress', str(target), ms)
        step.coords = node.center
        self._fill_node_spec(step, node)
        t0 = time.time()
        try:
            self.hdc.long_click(*node.center)
            self._sleep(ms / 1000.0)
            self.wait_idle(timeout=self.default_timeout)
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)
        return node

    def input(self, target, text: str, timeout: Optional[int] = None,
              clear_first: bool = True) -> LayoutNode:
        """向输入框输入文本。

        注意：uiInput inputText 按坐标输入，所以必须先定位到输入框。

        `clear_first` 的实际机制是「**先全选，再让新文本覆盖选中区**」
        （`Ctrl+A` 之后紧接着 `inputText`，全选状态下的输入会替换选中内容）。
        早期注释写的是「全选 + 删除」，暗示会再发一个 Del —— **实现从未发过**
        （见下面 `if clear_first` 块里的说明），这里按实际行为订正。
        """
        step = self._new_step('input', str(target), text)
        t0 = time.time()
        try:
            node = self._resolve_target(target, timeout)
            x, y = node.center
            step.node_path, step.coords = node.path, (x, y)
            self._fill_node_spec(step, node)

            self.hdc.click(x, y)              # 先聚焦输入框
            self.wait_idle(timeout=2000)

            if clear_first:
                # Ctrl+A 全选 —— **这里刻意不再发 Del(2075)**。
                #
                # 依据：紧接着的 `uiInput inputText` 在「全选状态下」会用新文本
                # **替换**选中区，已经等价于「清空并写入」。补发一个 Del 只是
                # 多一次注入、多一处时序埋雷，在某些输入框上还会触发额外行为。
                # （旧注释「Ctrl+A 全选，Del 删除」描述的是早期设想，与实现不符，
                #  被当成 bug 报了 —— 这次按实际行为订正，而不是照注释去补发。）
                try:
                    self.hdc.key_event(2072, 2038)   # Ctrl+A（部分版本生效）
                except HdcError as e:
                    # 不抛出：全选没成功不等于输入失败，后面 input_text 会暴露真问题。
                    # 但**不许静默** —— 写进 extra 留痕，否则「清空没生效」和
                    # 「本来就没内容」看起来一模一样。
                    step.extra['clear_first'] = 'Ctrl+A 未生效: %s' % e

            self.hdc.input_text(x, y, text)
            self.wait_idle(timeout=2000)
            return node
        except Exception as e:
            step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def swipe(self, direction: str, scale: float = 0.6,
              anchor: Optional[LayoutNode] = None) -> None:
        """按屏幕比例滑动。

        direction: 'up' / 'down' / 'left' / 'right'
        scale:     滑动距离占屏幕对应边长的比例
        anchor:    在指定控件内部滑动（默认整屏）
        """
        step = self._new_step('swipe', direction, scale)
        t0 = time.time()
        try:
            if anchor is not None:
                r = anchor.rect
            else:
                w, h = self.screen_size()
                r = Rect(0, 0, w, h)

            cx, cy = r.center
            dx, dy = int(r.width * scale / 2), int(r.height * scale / 2)
            d = direction.lower()
            if d == 'up':
                p = (cx, cy + dy, cx, cy - dy)
            elif d == 'down':
                p = (cx, cy - dy, cx, cy + dy)
            elif d == 'left':
                p = (cx + dx, cy, cx - dx, cy)
            elif d == 'right':
                p = (cx - dx, cy, cx + dx, cy)
            else:
                raise ValueError("direction 必须是 up/down/left/right")

            step.coords = (p[0], p[1])
            self.hdc.swipe(*p)
            self.wait_idle(timeout=3000)
        except Exception as e:
            step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def fling(self, direction: int, velocity: int = 800) -> None:
        """方向快滑：0=左 1=右 2=上 3=下。用于滑动压测。"""
        step = self._new_step('fling', value=direction)
        t0 = time.time()
        try:
            self.hdc.dirc_fling(direction, velocity)
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def back(self) -> None:
        step = self._new_step('back')
        t0 = time.time()
        try:
            self.hdc.back()
            self.wait_idle(timeout=3000)
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def home(self) -> None:
        self.hdc.home()
        self.wait_idle(timeout=3000)

    def _resolve_target(self, target, timeout: Optional[int]) -> LayoutNode:
        if isinstance(target, LayoutNode):
            return target
        if isinstance(target, Matcher):
            node = self.find(target, refresh=True)
            if node is None:
                return self.wait_for(target, timeout=timeout)
            return node
        raise TypeError(f'target 必须是 Matcher 或 LayoutNode，收到 {type(target)}')

    # -------------------------------------------------------- 屏幕

    def screen_size(self) -> Tuple[int, int]:
        """获取屏幕尺寸（px）。"""
        res = self.hdc.shell('hidumper -s RenderService -a screen')
        m = None
        import re
        m = re.search(r'(\d+)\s*[xX*]\s*(\d+)', res.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
        # 兜底：从控件树根的 bounds 推断
        try:
            r = self.root.rect
            if r.width and r.height:
                return r.width, r.height
        except Exception:
            pass
        return 1080, 2340       # 最后兜底值

    # -------------------------------------------------------- 截图

    def screenshot(self, name: Optional[str] = None) -> Optional[str]:
        """截图并拉回本地，返回本地路径。

        落盘位置规则：

        * 配了 `artifact_dir` → 相对文件名落到那里
        * 没配 `artifact_dir` → 落到**默认产物目录**（系统临时目录下的
          `ohauto_artifacts/`），并打印提示

        ⚠️ 第二条是踩出来的。早期实现把相对名字直接交给 `pull()`，于是文件落到
        **进程当前目录** —— 从项目根跑一次契约入口或 `--no-artifacts`，用例里的
        `screenshot: "01_list.png"` 就把图丢在项目根，跑几次攒一堆垃圾，
        而且完全没有提示。实测在项目根清理出 7 个这类文件。

        「不污染用户目录」比「省一次路径拼接」重要得多，所以默认位置必须是
        一个与项目无关的固定目录。
        """
        if not self.artifact_dir and not name:
            return None
        local = name or self._art('shot.png')
        if not os.path.isabs(local):
            base = self.artifact_dir or DEFAULT_ARTIFACT_DIR
            if not self.artifact_dir:
                self.log(f'提示：未配置 artifact_dir，产物落在默认目录 {base}')
            os.makedirs(base, exist_ok=True)
            local = os.path.join(base, os.path.basename(local))
        self.hdc.pull(self.hdc.screen_cap(), local)
        return local

    # -------------------------------------------------------- 断言

    def assert_exists(self, m: Matcher, timeout: Optional[int] = None) -> LayoutNode:
        step = self._new_step('assert.exists', str(m))
        t0 = time.time()
        try:
            return self.wait_for(m, timeout=timeout, _record=False)
        except Exception as e:
            # 记录「断言语义 + 底层原因」，两者都要 —— 只留底层原因会让人
            # 看不出这是一次断言失败；只留断言语义则会丢掉排查线索。
            msg = f'断言失败：控件应存在但未找到 -> {m}\n原因: {e}'
            step.ok, step.error = False, msg[:900]
            raise DriverError(f'断言失败：控件应存在但未找到 -> {m}') from e
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def assert_gone(self, m: Matcher, timeout: Optional[int] = None) -> None:
        step = self._new_step('assert.gone', str(m))
        t0 = time.time()
        try:
            self.wait_gone(m, timeout=timeout, _record=False)
        except Exception as e:
            msg = f'断言失败：控件应消失但仍然存在 -> {m}\n原因: {e}'
            step.ok, step.error = False, msg[:900]
            raise DriverError(f'断言失败：控件应消失但仍然存在 -> {m}') from e
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def assert_text(self, m: Matcher, expected: str) -> LayoutNode:
        """断言控件文本等于期望值。"""
        step = self._new_step('assert.text', str(m), expected)
        t0 = time.time()
        try:
            node = self.assert_exists(m)
            if node.text != expected:
                raise DriverError(
                    f'断言失败：文本不匹配\n  期望: {expected!r}\n  实际: {node.text!r}')
            return node
        except Exception as e:
            step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    # -------------------------------------------------------- 应用生命周期

    def start(self, wait: bool = True, timeout: int = 6000) -> float:
        """拉起被测应用。返回冷启动耗时（毫秒）—— a26 场景可直接复用。"""
        step = self._new_step('start', self.bundle)
        t0 = time.time()
        try:
            self.hdc.force_stop(self.bundle)     # 保证是冷启动
            self._sleep(0.6)
            t_start = time.time()
            self.hdc.start_ability(self.bundle, self.ability)
            if wait:
                self.wait_idle(timeout=timeout)
            elapsed = int((time.time() - t_start) * 1000)
            step.extra['startup_ms'] = elapsed
            self.log(f'应用已拉起，冷启动耗时约 {elapsed}ms')
            return elapsed
        except Exception as e:
            step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def stop(self) -> None:
        self.hdc.force_stop(self.bundle)

    # -------------------------------------------------------- 导出

    def steps_to_dict(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.steps]

    def summary(self) -> Dict[str, Any]:
        total = len(self.steps)
        failed = [s for s in self.steps if not s.ok]
        return {
            'bundle': self.bundle,
            'steps_total': total,
            'steps_failed': len(failed),
            'success_rate': round((total - len(failed)) / total, 4) if total else 0.0,
            'total_elapsed_ms': sum(s.elapsed_ms for s in self.steps),
            'failed_steps': [s.to_dict() for s in failed],
        }
