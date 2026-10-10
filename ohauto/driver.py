"""
L3 语义层 —— Driver 门面
========================

把「语义定位」翻译成「坐标操作」的中枢，串起完整的操作循环：

    定位：dumpLayout → 解析控件树 → 匹配器筛选 → 取 bounds 中心
    操作：uiInput 注入坐标事件
    等待：waitFor 轮询 / waitForIdle 界面稳定判定
    断言：assert_exists / assert_text / assert_gone
            assert.checked / assert.enabled / assert.count / assert.memory_below
    留痕：每步自动记录（截图 + 控件树 + 操作），供报告与脚本生成

这是整个能力层唯一需要「懂业务」的地方，其余各层都是纯机械转换。
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .hdc import DeviceNotFound, Hdc, HdcError, HdcLike
from .layout import LayoutNode, Rect, flatten, parse_layout
from .matcher import Matcher
from .perf import device_sample


def _json_tail(text: str) -> str:
    """切出 `uitest dumpLayout -p X && cat X` 输出里的 JSON 部分。

    那条命令前面会带一行 `DumpLayout saved to:...`，整段直接喂给 `parse_layout`
    会被判成"不是 JSON"。找不到 `{` / `[` 就原样返回 —— 让 `parse_layout`
    去报它那条更清楚的错（设备失败文本 vs 不是 JSON）。
    """
    for i, ch in enumerate(text or ''):
        if ch in '{[':
            return text[i:]
    return text or ''


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
    #: 按截图分级留存策略**有意省略**了本步截图。
    #: 省略必须留痕 —— 「没有图」要能区分「没拍」和「判了不必拍」。
    shot_skipped: bool = False
    layout_json: Optional[str] = None
    node_path: Optional[str] = None
    #: 可定位规格（id/text/text_deep/type）—— 用例沉淀的原料。
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
        if self.shot_skipped:
            d['shot_skipped'] = True
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
    hdc:          自定义设备实例（满足 HdcLike 契约即可，如 sim.FakeHdc）；
                  为 None 时自动创建真机 Hdc
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
        hdc: Optional[HdcLike] = None,
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
        #: 页面是否可能已经变了。**只有动作步骤会置脏**（见 `_mark_mutated`）——
        #: 只读步骤（waitIdle/assert/screenshot）之间页面不可能变，
        #: 这段时间里重复 dump 是纯浪费（一次约 1.6s，实测占用例墙钟 83%）。
        self._tree_dirty = True
        self.tree_dumps = 0        # 真取了几次树（对外可读，便于量收益）
        self.tree_reuses = 0       # 复用了几次
        #: 截图账（分级留存策略）：
        #: saved 含 tap 前置图 / 失败补图 / 独立 screenshot() 落盘；
        #: skipped 只记「判了不必拍」的省略 —— 省略必须可见。
        self.shots_saved = 0
        self.shots_skipped = 0
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
        """留痕附带可定位规格 —— 用例沉淀的原料。

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
        """重新拉取并解析控件树 —— **无条件重取**（动作前要看到最新的）。

        重要：坐标随折叠 / 旋转 / 滚动变化，**动作步骤前必须刷新**，不要复用。
        想省这次取树请用 `driver.root`（它会判断页面是不是真的变过）。

        无需留痕时把 `dumpLayout` 与 `cat` **合并成一次 shell 调用**：
        实测分两次是 1182+161ms、合并后 1193ms，**省约 150ms/次**。
        """
        if save and self.artifact_dir:
            # 要留痕就走文件：dumpLayout 落盘 + pull 回本地（多一次往返，值）
            dev = self.hdc.dump_layout(unfiltered=unfiltered,
                                       with_attrs=with_attrs)
            local = self._art('layout.json')
            self.hdc.pull(dev, local)
            self._root = parse_layout(local)
        else:
            flags = ('-i' if unfiltered else '') + ('-a' if with_attrs else '')
            # 设备侧临时目录：由 HdcLike 契约保证存在，不再 getattr 兜底 ——
            # 兜底会把「实现没满足契约」悄悄伪装成「用了默认目录」。
            dev = f'{self.hdc.tmp_dir}/ohauto_layout.json'
            res = self.hdc.shell(f'uitest dumpLayout{flags} -p {dev} && cat {dev}',
                                 check=True)
            self._root = parse_layout(_json_tail(res.stdout))
        self.tree_dumps += 1
        self._tree_dirty = False
        return self._root

    def _mark_mutated(self) -> None:
        """动作步骤执行后调用：页面可能变了，下次取树必须重新 dump。

        **只读步骤不要调它** —— 「少取树」这件事全靠这个标记来保证安全：
        没被标记 = 中间没有任何动作 = 复用必然看到同一张树。
        """
        self._tree_dirty = True

    @property
    def root(self) -> LayoutNode:
        """当前页面的控件树：**有效的就直接复用，脏了才重取**。

        与 `refresh()` 的分工：要"动作前一定是最新的"用 `refresh()`；
        要"读点什么、能省就省"用本属性（只读步骤都走这条路）。
        """
        if self._root is not None and not self._tree_dirty:
            self.tree_reuses += 1
            return self._root
        return self.refresh()

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
            first = True
            while time.time() < deadline:
                # 首探走 `root`（页面自上次取树以来没变过就直接复用）；
                # 之后每一探都必须真读 —— 等的就是"它出现"，缓存了语义就错了。
                node = self.find(m, refresh=not first)
                first = False
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

        首份基线直接用内存里已有的树（`_root`，典型是定位目标时那次 dump，
        即「动作时刻的画面」），不为它单独跑一次全量 dump：动作后的第一份
        新鲜签名与它一致，等价于「动作前后画面没动」，比「动作后连看两眼」
        证据不弱、还省一次 dump。页面真跳转了，第一份新鲜签名就会与基线
        不同，后续轮次与原逻辑完全一致 —— 最坏情况不比原来慢。

        ★ 反向依赖声明：「一次新鲜
        采样即可判稳」的证据强度，依赖 explorer 对页面签名/候选的**无条件
        refresh 兜底**（晚到跳转在那里被捕获，见 `_page_signatures` 文档；
        钉子：tests/test_f_wait_idle_semantics.py）。若有调用方脱离该兜底、
        直接拿本函数返回值做导航决策，必须为它禁用基线种子，恢复
        「动作后连取两次」的语义 —— 降级必须显式，不能靠默认值蒙混。
        """
        timeout = timeout if timeout is not None else self.default_timeout
        deadline = time.time() + timeout / 1000.0
        last, same = None, 0
        if self._root is not None:
            last = self._signature_of(self._root)
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
        return self._signature_of(root)

    @staticmethod
    def _signature_of(root: LayoutNode) -> str:
        """对**已有**的树算轻量签名 —— 不碰设备，wait_idle 的基线种子用它。"""
        parts = [f'{n.type}|{n.id}|{n.text}'
                 for n in flatten(root, only_visible=True)]
        return ';'.join(parts)

    # -------------------------------------------------------- 操作

    def tap(self, target, timeout: Optional[int] = None,
            post_idle: bool = True,
            keep_shot: Optional[bool] = None) -> LayoutNode:
        """点击：target 可为 Matcher 或 LayoutNode。

        `keep_shot` —— 截图分级留存的**调用方判定输入**，
        本方法不猜：`None`（默认）按现行口径留「动作前」图；调用方确知页面已留档
        且非降级命中时传 `False` 省 2 次 hdc 往返，省略记入 `step.shot_skipped`
        与 `shots_skipped` 计数。失败路径不受它影响：本步失败且没留过图时，
        兜底补一张失败现场 —— 失败证据只多不少。
        """
        step = self._new_step('tap')
        t0 = time.time()
        try:
            node = self._resolve_target(target, timeout)
            x, y = node.center
            step.target = str(target)
            step.node_path, step.coords = node.path, (x, y)
            self._fill_node_spec(step, node)
            if self.artifact_dir:
                if keep_shot is False:
                    step.shot_skipped = True
                    self.shots_skipped += 1
                else:
                    step.screenshot = self._art('before_tap.png')
                    self.hdc.pull(self.hdc.screen_cap(), step.screenshot)
                    self.shots_saved += 1
            self.hdc.click(x, y)
            self._mark_mutated()
            if post_idle:
                self.wait_idle(timeout=timeout or self.default_timeout)
            return node
        except Exception as e:
            step.ok, step.error = False, str(e)[:400]
            step.target = step.target or str(target)
            if self.artifact_dir and not step.screenshot:
                # 失败必留（分级矩阵第二行）：跳过了前置图的失败步，
                # 补一张失败现场。补图本身失败则放弃 —— 证据尽力而为，
                # 不能让留痕动作把原本的错误信息顶掉。
                try:
                    step.screenshot = self._art('fail_tap.png')
                    self.hdc.pull(self.hdc.screen_cap(), step.screenshot)
                    self.shots_saved += 1
                except Exception:                          # noqa: BLE001
                    step.screenshot = None
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
            self._mark_mutated()
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
                    self._mark_mutated()
                except HdcError as e:
                    # 不抛出：全选没成功不等于输入失败，后面 input_text 会暴露真问题。
                    # 但**不许静默** —— 写进 extra 留痕，否则「清空没生效」和
                    # 「本来就没内容」看起来一模一样。
                    step.extra['clear_first'] = 'Ctrl+A 未生效: %s' % e

            self.hdc.input_text(x, y, text)
            self._mark_mutated()
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
                size = self.screen_size()
                if size is None:
                    raise RuntimeError(
                        '屏幕尺寸实测不到（hidumper 与控件树均不可用），'
                        '拒绝用猜测的全屏几何执行滑动')
                r = Rect(0, 0, size[0], size[1])

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
            self._mark_mutated()
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

    def screen_size(self) -> Optional[Tuple[int, int]]:
        """获取屏幕尺寸（px）。

        返回 None 表示**实测不到**——宁可交 None 让调用方降级，
        也不再硬编码 1080×2340 去猜（真机是 720×1280，旧兜底值是错的，
        评审 P2：压测几何曾被系统性带偏）。
        """
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
        return None       # 实测不到 ≠ 允许瞎猜

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
        self.shots_saved += 1
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

    # -- 断言原语扩展 ------------

    def assert_checked(self, m: Matcher, expected: bool = True,
                       timeout: Optional[int] = None) -> LayoutNode:
        """断言控件（复选框/开关类）的选中状态。"""
        return self._assert_flag(m, 'checked', expected, timeout)

    def assert_enabled(self, m: Matcher, expected: bool = True,
                       timeout: Optional[int] = None) -> LayoutNode:
        """断言控件可用（未禁用）。"""
        return self._assert_flag(m, 'enabled', expected, timeout)

    def _assert_flag(self, m: Matcher, attr: str, expected: bool,
                     timeout: Optional[int]) -> LayoutNode:
        """assert.checked / assert.enabled 的共用骨架：先等存在，再比状态。"""
        step = self._new_step(f'assert.{attr}', str(m), expected)
        t0 = time.time()
        try:
            node = self.assert_exists(m, timeout=timeout)
            actual = bool(getattr(node, attr))
            if actual != bool(expected):
                raise DriverError(
                    f'断言失败：控件应 {attr}={expected}\n  实际: {actual}')
            return node
        except Exception as e:                     # noqa(断言留痕旁路，与 assert.text 同款)
            step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def assert_count(self, m: Matcher, expected: int,
                     timeout: Optional[int] = None,
                     interval: Optional[int] = None) -> int:
        """断言匹配控件的数量等于 expected；轮询到相等或超时。返回实际数量。"""
        step = self._new_step('assert.count', str(m), expected)
        t0 = time.time()
        try:
            timeout = timeout if timeout is not None else self.default_timeout
            interval = interval or self.poll_interval
            deadline = time.time() + timeout / 1000.0
            actual = -1
            while True:
                actual = len(self.find_all(m, refresh=True))
                if actual == expected:
                    return actual
                if time.time() >= deadline:
                    raise DriverError(
                        f'断言失败：控件数量应为 {expected}\n  实际: {actual}')
                self._sleep(interval / 1000.0)
        except Exception as e:                     # noqa(断言留痕旁路，与 assert.text 同款)
            step.ok, step.error = False, str(e)[:400]
            raise
        finally:
            step.elapsed_ms = int((time.time() - t0) * 1000)
            self._record(step)

    def assert_memory_below(self, max_pss_kb: int,
                            bundle: Optional[str] = None) -> Dict[str, Any]:
        """断言被测应用 PSS 低于阈值 —— 性能采样进断言原语的桥。

        **缺样本不判通过**：采不到 PSS 就算失败 —— 「没采到」绝不能伪装成
        「采到了且达标」（ohauto/perf.py 的「缺样本 ≠ 正常」约定）。
        返回采样字典（pss_kb / load1 / alive / warn），报告可直接消费。
        """
        target = bundle or self.bundle
        step = self._new_step('assert.memory_below', target, max_pss_kb)
        t0 = time.time()
        try:
            s = device_sample(self.hdc, target)
            step.extra['pss_kb'] = s.get('pss_kb')
            step.extra['alive'] = s.get('alive')
            if s.get('pss_kb') is None:
                raise DriverError(
                    '断言失败：未采到 PSS（缺样本不判通过）\n原因: '
                    + '; '.join(s.get('warn') or []))
            if s['pss_kb'] > max_pss_kb:
                raise DriverError(
                    f'断言失败：PSS 超阈值\n  阈值: {max_pss_kb} KB\n'
                    f'  实际: {s["pss_kb"]} KB')
            return s
        except Exception as e:                     # noqa(断言留痕旁路，与 assert.text 同款)
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
            self._mark_mutated()
            self._sleep(0.6)
            t_start = time.time()
            self.hdc.start_ability(self.bundle, self.ability)
            self._mark_mutated()
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

    @property
    def tree_reuse_rate(self) -> float:
        """取树复用率 = 复用次数 /（真取 + 复用）。一次树都没取过时返回 0.0。

        ⚠️ 口径两条（对外引用时必须带上）：
          ① 这是**宿主侧调用口径**，不是设备侧往返数 —— `refresh()` 已把
             `dumpLayout && cat` 合并成**一次往返**，但真取一次仍只记 1；
          ② 复用率高低本身不是目标，「动作步骤前必须重取」是红线 ——
             复用只发生在**跨越只读步骤**的取树之间（见 `_mark_mutated`）。
        """
        total = self.tree_dumps + self.tree_reuses
        return round(self.tree_reuses / total, 4) if total else 0.0

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
            # 取树效率（10-08 落地「合并往返 + 只读步骤复用」后必须能自动量到，
            # 之前只能靠会话记录手工数；延迟口径讨论全靠这两个数）
            'tree_dumps': self.tree_dumps,
            'tree_reuses': self.tree_reuses,
            'tree_reuse_rate': self.tree_reuse_rate,
            # 截图账（分级留存）：省略数与留存数并列，省略才可见。
            'screenshots_saved': self.shots_saved,
            'screenshots_skipped': self.shots_skipped,
        }
