"""
执行引擎 —— 连续执行的健壮性与批量编排
============================================

`driver.py` 解决「一步怎么做对」，本模块解决「**一百步连续做，还能不能做完**」。

真机连续执行会遇到三类问题，单步执行器（`action.run_steps`）都处理不了：

1. **偶发失败**：控件还在动画里没渲染出来、hdc 命令偶发超时。
   这类失败重试一次就好，但单步执行器会直接判定用例失败。
2. **设备失联**：连续跑几十步后 hdc daemon 卡死、设备掉线。
   这类失败重试没用，必须做**连接级恢复**。
3. **级联失败**：第 5 步真的挂了，第 6~50 步跟着全挂。
   如果按 50 个失败上报，成功率会被严重低估，掩盖真实问题。

核心设计：

    FailureKind     失败分类   —— 不同原因必须不同处置（重试 / 恢复 / 直接判负）
    RetryPolicy     重试策略   —— 分级重试 + **线性**退避（`base * n`），断言失败不重试
    DeviceGuard     设备看护   —— 心跳 + 连接级自愈
    StepResult      步骤结果   —— 保留每一次尝试，可回溯「第几次才成功」
    CaseResult      用例结果   —— 区分「原始成功率」与「独立成功率」
    Runner          批量执行   —— 级联识别 + 产物轮转

关于成功率的两种口径（验收时要说清用哪个）：

    原始成功率   = (总步数 - 失败步数) / 总步数
                   受级联影响，一步挂可能拉低几十个点

    独立成功率   = (总步数 - 独立失败步数) / 总步数
                   排除级联后的成功率 —— **这才是执行引擎健壮性的真实指标**

执行引擎 验收标准「50 步连续执行成功率 ≥ 95%」以**独立成功率**为准，
同时报告重试挽救率（引擎到底救回了多少步）。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .action import RunReport, exec_action
from .driver import Driver, DriverError
from .hdc import DeviceNotFound, HdcError


def _count_nodes(tree: Any) -> int:
    """数一数控件树里有多少个节点 —— 用于判断「到底有没有窗口」。

    锁屏 / 无前台窗口时真机只返回 1 个根节点，据此可与「窗口存在但控件没找到」
    区分开。
    """
    if not isinstance(tree, dict):
        return 0
    n = 1
    for ck in ('children', 'Children', 'child', 'nodes', 'subNodes'):
        kids = tree.get(ck)
        if isinstance(kids, list):
            for c in kids:
                n += _count_nodes(c)
            break
    return n


# ================================================================ 失败分类

class FailureKind(str, Enum):
    """失败归因的一级分类。

    这套分类同时是信号采集与失败归因的接口雏形 ——
    分类结果会随步骤结果一起落进报告，供上游做进一步归因。
    """

    LOCATE = 'LOCATE'      # 控件没找到（多为时序问题，可重试）
    TIMEOUT = 'TIMEOUT'    # 命令/等待超时（可重试）
    DEVICE = 'DEVICE'      # 设备或 hdc 连接异常（重试 + 触发恢复）
    APP = 'APP'            # 被测应用异常（崩溃/未启动，重试前先重启应用）
    ASSERT = 'ASSERT'      # 断言不成立（**真失败，绝不重试**）
    DSL = 'DSL'            # 用例本身写错（**绝不重试**）
    UNKNOWN = 'UNKNOWN'    # 其他未归类异常


# 关键词 → 分类。顺序重要：先匹配更具体的模式。
_PATTERNS: List[Tuple[FailureKind, str]] = [
    (FailureKind.DSL,     r'需要形如|不支持的动作|不支持的断言|缺少\s*\w+\s*字段|必须是'),
    (FailureKind.ASSERT,  r'断言失败'),
    (FailureKind.DEVICE,  r'device\s*not\s*found|no\s*device|设备未连接|未检测到设备|'
                          r'offline|disconnected|设备已断开|hdc\s*server'),
    (FailureKind.APP,     r'failed to start ability|resolve ability|'
                          r'应用未启动|应用已退出|force.?stop'),
    (FailureKind.TIMEOUT, r'超时|timeout|timed? ?out|TimeoutExpired'),
    (FailureKind.LOCATE,  r'未找到|定位失败|找不到|未匹配|should exist|应存在'),
]


def classify(exc: BaseException) -> FailureKind:
    """把异常归到一级失败类别。

    先看异常类型（最可靠），再看消息文本（兜底）。
    """
    # ---- 类型优先
    if isinstance(exc, DeviceNotFound):
        return FailureKind.DEVICE
    if isinstance(exc, TimeoutError):
        return FailureKind.TIMEOUT

    name = type(exc).__name__
    if name in ('TimeoutExpired', 'Timeout'):
        return FailureKind.TIMEOUT
    if name == 'DslError':
        return FailureKind.DSL

    # ---- 消息文本兜底
    msg = str(exc)
    for kind, pat in _PATTERNS:
        if re.search(pat, msg, re.I):
            return kind

    if isinstance(exc, HdcError):
        return FailureKind.DEVICE
    if isinstance(exc, DriverError):
        return FailureKind.LOCATE
    return FailureKind.UNKNOWN


# ================================================================ 重试策略

# 默认每个类别的最大尝试次数（含首次）。1 表示不重试。
DEFAULT_MAX_ATTEMPTS: Dict[FailureKind, int] = {
    FailureKind.LOCATE:  3,     # 界面动画未结束，等一下就好
    FailureKind.TIMEOUT: 3,
    FailureKind.DEVICE:  3,     # 每次尝试之间做一次连接恢复
    FailureKind.APP:     2,
    FailureKind.UNKNOWN: 2,
    FailureKind.ASSERT:  1,     # ← 断言失败是真失败，重试只会掩盖问题
    FailureKind.DSL:     1,     # ← 用例写错了，重试一万次也没用
}

DEFAULT_BACKOFF_MS: Dict[FailureKind, int] = {
    FailureKind.LOCATE:  400,
    FailureKind.TIMEOUT: 800,
    FailureKind.DEVICE:  1200,
    FailureKind.APP:     1500,
    FailureKind.UNKNOWN: 600,
    FailureKind.ASSERT:  0,
    FailureKind.DSL:     0,
}


@dataclass
class RetryPolicy:
    """分级重试策略。

    Parameters
    ----------
    max_attempts:  各类别最大尝试次数（含首次）
    backoff_ms:    各类别退避基数，第 n 次重试等待 backoff * n（线性退避）
    jitter_ms:     随机抖动上限，避免多用例并发时重试撞在一起
    max_total_ms:  单步总时间上限，防止重试把一步拖到几分钟
    """

    max_attempts: Dict[FailureKind, int] = field(
        default_factory=lambda: dict(DEFAULT_MAX_ATTEMPTS))
    backoff_ms: Dict[FailureKind, int] = field(
        default_factory=lambda: dict(DEFAULT_BACKOFF_MS))
    jitter_ms: int = 120
    max_total_ms: int = 30000

    @classmethod
    def no_retry(cls) -> 'RetryPolicy':
        """完全不重试 —— 用于对照实验，量化重试到底救回了多少步。"""
        return cls(max_attempts={k: 1 for k in FailureKind})

    def attempts_for(self, kind: FailureKind) -> int:
        return max(1, int(self.max_attempts.get(kind, 1)))

    def retryable(self, kind: FailureKind) -> bool:
        return self.attempts_for(kind) > 1

    def wait_ms(self, kind: FailureKind, attempt: int) -> int:
        base = int(self.backoff_ms.get(kind, 0))
        if base <= 0:
            return 0
        delay = base * attempt                      # 线性退避：base, 2*base, 3*base
        if self.jitter_ms > 0:
            delay += random.randint(0, self.jitter_ms)
        return delay


# ================================================================ 设备看护

class DeviceGuard:
    """设备健康看护与连接级自愈。

    连续执行是「温水煮青蛙」：单跑 5 步毫无问题，跑到第 40 步时
    hdc daemon 可能已经卡死、设备可能已经掉线。此时重试单步毫无意义，
    必须做连接级恢复。

    ★ 一个实测得出的关键结论：**传输层（hdc shell）挂掉时，重启被测应用毫无用处。**

    因为「验证恢复是否成功」本身也要走 shell 读控件树 —— 壳都断了，
    重新拉起应用只是徒劳，还可能把已经崩掉的现场抹掉。所以要按失败类型分流：

        失败类型        真正管用的处置
        ------------    ------------------------------------------
        DEVICE          重启 hdc 服务（kill -r）+ 等设备重连
                        ← 传输层问题只能在传输层解决
        APP             重新拉起应用，把界面拉回起点
                        ← 传输层是好的，只是应用崩了

    恢复梯度（由轻到重，能不动就不动）：
        1. 心跳探测       — hdc shell echo，最轻，多数抖动在这一级就过去了
        2. 重启 hdc 服务  — kill -r 会重建连接并重新发现设备（可关闭）
        3. 重启被测应用   — 仅在传输层已确认健康、且失败类型指向应用时才做
    """

    def __init__(self, hdc, allow_hdc_restart: bool = True,
                 verbose: bool = True,
                 sleep_fn: Callable[[float], None] = time.sleep):
        self.hdc = hdc
        self.allow_hdc_restart = allow_hdc_restart
        self.verbose = verbose
        self._sleep = sleep_fn
        self.pings = 0
        self.failed_pings = 0
        self.recoveries = 0
        self.restarts = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f'[guard] {msg}')

    def ping(self, timeout: int = 12) -> bool:
        """最轻的存活探测：让设备回一个 ok。"""
        self.pings += 1
        try:
            res = self.hdc.shell('echo ohauto_ok', timeout=timeout)
            ok = 'ohauto_ok' in (getattr(res, 'stdout', '') or '')
        except Exception:
            ok = False
        if not ok:
            self.failed_pings += 1
        return ok

    def ensure_alive(self, attempts: int = 2, delay_s: float = 1.5) -> bool:
        """连续探测若干次，任一次通过即认为存活。"""
        for i in range(max(1, attempts)):
            if self.ping():
                return True
            if i < attempts - 1:
                self._sleep(delay_s)
        return False

    # ---------------------------------------------------------- 屏幕状态
    #
    # ★ 实测坑（DAYU200 / OpenHarmony 5.0.3.135）：
    #   开发板默认几十秒就息屏，息屏后回到**锁屏界面**，而锁屏不进无障碍树 ——
    #   `dumpLayout` 只会返回 1 个 bounds 全为 0 的根节点。
    #   50 步连续执行要跑好几分钟，不处理这个就会出现「跑到一半控件树突然全空」：
    #   现象看起来像引擎崩了，实际只是屏幕黑了。这类问题极难从报错反推，
    #   所以在开跑前就要把屏幕钉住。

    KEEP_AWAKE_MS = 1800000      # 默认把息屏超时覆盖成 30 分钟

    def screen_size(self, default: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
        """取设备分辨率；实测不到且未显式给 default 时抛错 —— 拒绝用猜测的几何。"""
        try:
            res = self.hdc.shell('hidumper -s RenderService -a screen', timeout=20)
            m = re.search(r'(\d+)\s*[xX*]\s*(\d+)', getattr(res, 'stdout', '') or '')
            if m:
                return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        if default is None:
            raise DriverError(
                '屏幕尺寸实测不到（hidumper RenderService 无输出），'
                '拒绝回退到写死的设备值 —— 请检查设备在线，'
                '或显式传入 default=（仅限离线/模拟环境）')
        return default

    def has_window(self) -> bool:
        """当前是否有可用窗口。

        锁屏 / 无前台窗口时 `dumpLayout` 只返回一个 bounds 全 0 的根节点。
        用这个判据可以在「找不到控件」之前一步就识别出「根本没有窗口」——
        两者的处置完全不同（前者改定位条件，后者唤醒解锁）。
        """
        try:
            dev = self.hdc.dump_layout()
            res = self.hdc.shell(f'cat {dev}', timeout=15)
            text = getattr(res, 'stdout', '') or ''
            if not text.strip():
                return False
            return _count_nodes(json.loads(text)) > 1
        except Exception:
            return False

    def ensure_awake(self, screen_off_ms: int = KEEP_AWAKE_MS,
                     unlock: bool = True) -> bool:
        """唤醒屏幕、延长息屏超时、必要时上滑解锁。返回屏幕是否可用。

        连续执行前调用一次即可。坐标按真实分辨率折算，不写死。
        """
        try:
            self.hdc.shell('power-shell wakeup', timeout=15)
        except Exception as e:
            self._log(f'唤醒屏幕失败（继续）: {e}')

        try:
            self.hdc.shell(f'power-shell timeout -o {int(screen_off_ms)}', timeout=15)
            self._log(f'已把息屏超时覆盖为 {int(screen_off_ms)} ms')
        except Exception as e:
            self._log(f'设置息屏超时失败（继续）: {e}')

        if self.has_window():
            return True

        if unlock:
            size = self.screen_size()
            if size is None:
                self._log('屏幕尺寸实测不到，跳过上滑解锁（不用猜测的几何操作设备）')
            else:
                w, h = size
                x = w // 2
                y1, y2 = int(h * 0.90), int(h * 0.35)
                self._log(f'检测到无窗口（多为锁屏），执行上滑解锁 ({x},{y1})->({x},{y2})')
                try:
                    self.hdc.shell(
                        f'uitest uiInput swipe {x} {y1} {x} {y2} 600', timeout=25)
                    self._sleep(1.2)
                except Exception as e:
                    self._log(f'解锁手势失败: {e}')

        ok = self.has_window()
        self._log('屏幕可用' if ok else '屏幕仍不可用 —— 需人工检查设备是否锁屏/死机')
        return ok

    def _restart_hdc_server(self) -> None:
        """重启 hdc 服务：会重建与设备的连接。这是传输层故障的主修手段。"""
        self._log('重启 hdc 服务 ...')
        try:
            self.hdc.run(['kill', '-r'], timeout=20, check=False)
            self.restarts += 1
            self._sleep(2.0)
            try:
                # 重启 server 后设备列表会短暂为空，等它回来
                self.hdc.wait_device(timeout=30)
            except Exception:
                pass
        except Exception as e:
            self._log(f'重启 hdc 失败: {e}')

    def _relaunch_app(self, driver: Driver) -> bool:
        """重新拉起被测应用，把界面拉回起点。"""
        self._log(f'尝试重新拉起应用 {driver.bundle} ...')
        try:
            driver.hdc.force_stop(driver.bundle)
            self._sleep(0.8)
            driver.hdc.start_ability(driver.bundle, driver.ability)
            driver.wait_idle(timeout=8000)
            return True
        except Exception as e:
            self._log(f'重新拉起应用失败: {e}')
            return False

    def recover(self, driver: Optional[Driver] = None,
                kind: Optional['FailureKind'] = None,
                restart_app: Optional[bool] = None) -> bool:
        """执行一次恢复流程。返回是否恢复成功。

        Parameters
        ----------
        driver:      被恢复的驱动器；为 None 时无法做应用级恢复
        kind:        触发恢复的失败类别，决定恢复策略（见类文档）
        restart_app: 是否重启应用；None 时按 kind 自动决定（APP 才重启）
        """
        if restart_app is None:
            restart_app = (kind == FailureKind.APP)
        label = kind.value if kind else '未知'
        self._log(f'设备疑似异常（类型={label}），开始恢复 ...')

        # ---- 1. 轻量重试：设备可能只是一时忙
        transport_ok = self.ensure_alive(attempts=2, delay_s=1.5)
        if transport_ok and not restart_app:
            self.recoveries += 1
            self._log('恢复成功（轻量重试即可，无需重启任何服务）')
            return True

        # ---- 2. 传输层不通 → 重启 hdc 服务（唯一能在传输层解决问题的动作）
        if not transport_ok:
            if not self.allow_hdc_restart:
                self._log('传输层不通，但已禁止重启 hdc 服务 —— 需要人工介入'
                          '（检查 USB 连接 / 设备是否死机）')
                return False
            self._restart_hdc_server()
            transport_ok = self.ensure_alive(attempts=3, delay_s=2.0)
            if not transport_ok:
                self._log('重启 hdc 服务后传输层仍未恢复 —— 需要人工介入'
                          '（检查 USB 连接 / 设备是否死机）')
                return False
            self._log('传输层已恢复')

        # ---- 3. 传输层健康，按需重启应用
        if restart_app and driver is not None:
            if self._relaunch_app(driver):
                self.recoveries += 1
                self._log('恢复成功（已重新拉起应用）')
                return True
            return False

        self.recoveries += 1
        self._log('恢复成功（传输层已恢复）')
        return True

    def stats(self) -> Dict[str, int]:
        return {'pings': self.pings, 'failed_pings': self.failed_pings,
                'recoveries': self.recoveries, 'hdc_restarts': self.restarts}


# ================================================================ 结果模型

@dataclass
class StepAttempt:
    """单次尝试的记录 —— 保留它才能回答「这步重试了几次、最后怎么过的」。"""
    attempt: int
    ok: bool
    kind: Optional[FailureKind] = None
    error: str = ''
    elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = {'attempt': self.attempt, 'ok': self.ok, 'elapsed_ms': self.elapsed_ms}
        if self.kind:
            d['kind'] = self.kind.value
        if self.error:
            d['error'] = self.error[:400]
        return d


@dataclass
class StepResult:
    """一个 DSL 步骤的最终结果（含全部尝试）。"""

    index: int
    action: str
    target: str = ''
    arg: Any = None
    ok: bool = True
    kind: Optional[FailureKind] = None
    attempts: List[StepAttempt] = field(default_factory=list)
    elapsed_ms: int = 0
    recovered: bool = False       # 这步通过设备恢复才救回来
    cascade: bool = False         # 疑似被前面某步的失败带崩
    cascade_of: Optional[int] = None   # 追溯到哪一步
    # 失败时顺手抓的控件树快照（本地文件路径，JSON 文本）。
    # 为什么需要：归因引擎判「定位失败」靠的是「目标控件在**所有**快照里都不存在」
    # 这条硬证据（diagnose._locator）。只有 kind/attempts 时它只能退回运行器分类，
    # 置信度从 0.85 掉到 0.6。抓不到就留空列表 —— 归因侧本来就兼容这种情况。
    trees: List[str] = field(default_factory=list)
    # 定位器 id（A 的 `LocateResult.locator_id` 原样回写）。
    # 为什么需要：归因定案为「定位失败」后要调 `A.record_locator_failure(locator_id)`
    # 回写健康度台账 —— 没有 id 就回写不了，那条链路等于断的。
    # 为什么不在归因侧现拼：id 是定位器**自己**的身份，拼出来的是假账
    # （会在 A 的台账里塞空 id 记录）。所以由执行器在定位后原样写入。
    # 2026-09-23 新增，见 docs/给B-回执-2026-09-23.md §6.1。
    locator_id: Optional[str] = None
    # 失败归因结论（`diagnose.Verdict`）。**仅当 Runner 开启 `diagnose_failures` 时才有**，
    # 默认 None 表示「没做归因」而不是「归因结论为空」—— 两者必须能区分。
    #
    # 为什么标注成 Any 而不是 `diagnose.Verdict`：runner 不该硬依赖归因引擎
    # （B 的模块），而且测试里要能注入替身。真实类型由 `Runner.diagnoser` 决定。
    # 为什么放在 StepResult 上：归因的输入就是「一个失败步」，结论天然属于它 ——
    # 放在 CaseResult 上就得再维护一份 step_index 映射，多一个能对错的地方。
    verdict: Optional[Any] = None

    @property
    def diagnosed(self) -> bool:
        """这一步是否拿到了归因结论。"""
        return self.verdict is not None

    @property
    def verdict_cn(self) -> str:
        """归因结论的中文一句话（供日志/报告直接打印）。"""
        if self.verdict is None:
            return ''
        return str(self.verdict)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def retried(self) -> bool:
        return len(self.attempts) > 1

    @property
    def rescued_by_retry(self) -> bool:
        """首次失败但最终成功 —— 引擎「救回来」的步数就是这个。"""
        return self.ok and any(not a.ok for a in self.attempts)

    def to_dict(self) -> Dict[str, Any]:
        d = {'index': self.index, 'action': self.action, 'target': self.target,
             'ok': self.ok, 'attempts': len(self.attempts),
             'elapsed_ms': self.elapsed_ms}
        if self.kind:
            d['kind'] = self.kind.value
        if self.retried:
            d['attempt_detail'] = [a.to_dict() for a in self.attempts]
        if self.recovered:
            d['recovered'] = True
        if self.cascade:
            d['cascade'] = True
            d['cascade_of'] = self.cascade_of
        if self.trees:
            d['trees'] = list(self.trees)
        # 归因结论（证据链出口）：有才输出，且**不带** None 占位 ——
        # 报告消费方用「有没有 verdict 键」就能区分「没做归因」与「归因为空」。
        if self.verdict is not None:
            d['verdict'] = (self.verdict.to_dict()
                            if hasattr(self.verdict, 'to_dict') else self.verdict)
        return d


@dataclass
class CaseResult:
    """一个用例的执行结果。"""

    name: str = ''
    bundle: str = ''
    steps: List[StepResult] = field(default_factory=list)
    elapsed_ms: int = 0
    device_recoveries: int = 0
    #: 取树账（10-08「合并往返 + 只读步骤复用」的收益必须有地方自动落账，
    #: 否则每次对外报数都要回会话记录里手工数）。按用例记增量，
    #: 同一个 driver 被多条用例复用时不会把上一条的账算到下一条头上。
    tree_dumps: int = 0
    tree_reuses: int = 0
    #: 截图账（分级留存）。按用例记增量，
    #: 口径同 tree_dumps —— 同一 driver 被多条用例复用时不串账。
    #: skipped 必须与 saved 并列出报告：省略本身是要留痕的行为。
    screenshots_saved: int = 0
    screenshots_skipped: int = 0
    #: 内存趋势曲线（2C【C4】，`collect_perf` 开启时才有）。
    #: None = 本次**没采**；「采了但缺样本」看 `analysis['pss_missing']`
    #: —— 后者必须如实留痕，不能退回 None 伪装成没采过。
    perf: Optional[Dict[str, Any]] = None

    # ---------------------------------------------------------- 统计

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.ok)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if not s.ok)

    @property
    def cascade_failed(self) -> int:
        return sum(1 for s in self.steps if not s.ok and s.cascade)

    @property
    def independent_failed(self) -> int:
        """独立失败 —— 排除级联后的真正失败数。"""
        return sum(1 for s in self.steps if not s.ok and not s.cascade)

    @property
    def success_rate(self) -> float:
        """原始成功率（受级联影响）。"""
        return round(self.passed / self.total, 4) if self.total else 1.0

    @property
    def non_cascade_failure_rate(self) -> float:
        """**非级联失败率** —— 只衡量「引擎健壮性」，**不是成功率**。

        与 `success_rate` / `ok` 是三个不同的东西，各回答一个问题：

        | 属性 | 回答的问题 |
        |---|---|
        | `non_cascade_failure_rate` | 失败会不会连累后面（引擎健壮性） |
        | `success_rate` | 这次跑通了没有（含级联影响） |
        | `ok` / `health_ok` | 用例整体是否通过 |

        ⚠️ **反例（外部评审算过、C 复核成立）**：50 步全部因同一原因失败 →
        49 步被判 cascade → 本值 = 0.98。但 `ok` 仍然是 False。
        所以它**必须与 `ok` / `all_failed` / `health_ok` 一起展示**，
        禁止单独当作通过依据。

        2026-09-23 由 `independent_success_rate` 改名而来 ——
        旧名鼓励误读（「独立成功率 0.98」在整体失败时是最危险的一句话）。
        """
        if not self.total:
            return 1.0
        return round((self.total - self.independent_failed) / self.total, 4)

    @property
    def independent_success_rate(self) -> float:
        """【已弃用】旧名容易被读成「成功率」。用 `non_cascade_failure_rate`。"""
        import warnings
        warnings.warn(
            'independent_success_rate 已改名为 non_cascade_failure_rate：'
            '它在用例整体失败时仍会给出高分（50 步全挂 → 0.98），不是成功率。'
            '判整体成败请用 ok / health_ok / all_failed。',
            DeprecationWarning, stacklevel=2)
        return self.non_cascade_failure_rate

    @property
    def all_failed(self) -> bool:
        """全部步骤都失败 —— 这种情况下任何「率」都不该被当成通过。"""
        return self.total > 0 and self.failed == self.total

    @property
    def health_ok(self) -> bool:
        """用例整体健康 —— 与 `non_cascade_failure_rate` **并列**，不互相替代。

        为什么不能只用 `all_failed` 挡：它只挡「100% 失败」，挡不住
        「50 步挂 45 步、其中 44 步判 cascade」—— 那种情况下
        `non_cascade_failure_rate = 0.98` 且 `all_failed = False`，**照样好看**。

        所以 KPI 判定要**两个条件同时**满足，报告里两个数并排打。
        """
        return self.failed == 0

    def kpi_ok(self, target: float = 0.95) -> bool:
        """引擎健壮性 KPI。**整体不健康时直接判不通过**，不管那个率多漂亮。

        两道闸缺一不可（见 `health_ok` 的说明）：
        ① `health_ok` —— 有步骤失败就不算通过；
        ② `non_cascade_failure_rate >= target` —— 失败也没大量连累后面。
        """
        return self.health_ok and self.non_cascade_failure_rate >= target

    @property
    def retry_attempts(self) -> int:
        return sum(max(0, s.attempt_count - 1) for s in self.steps)

    @property
    def rescued_by_retry(self) -> int:
        return sum(1 for s in self.steps if s.rescued_by_retry)

    @property
    def retry_rescue_rate(self) -> float:
        """重试挽救率 —— 引擎到底救回了多少步。0 尝试时返回 1.0。"""
        if self.retry_attempts == 0:
            return 1.0
        return round(self.rescued_by_retry / self.retry_attempts, 4)

    @property
    def tree_reuse_rate(self) -> float:
        """取树复用率 = 复用 /（真取 + 复用）。没取过树时返回 0.0。

        口径与 `Driver.tree_reuse_rate` 一致（宿主侧调用口径、非设备往返数）。
        """
        total = self.tree_dumps + self.tree_reuses
        return round(self.tree_reuses / total, 4) if total else 0.0

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def failures_by_kind(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for s in self.steps:
            if s.ok or not s.kind:
                continue
            out[s.kind.value] = out.get(s.kind.value, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def slowest(self, n: int = 5) -> List[StepResult]:
        return sorted(self.steps, key=lambda s: -s.elapsed_ms)[:n]

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'name': self.name, 'bundle': self.bundle,
            'total': self.total, 'passed': self.passed, 'failed': self.failed,
            'cascade_failed': self.cascade_failed,
            'independent_failed': self.independent_failed,
            'success_rate': self.success_rate,
            'non_cascade_failure_rate': self.non_cascade_failure_rate,
            # 旧 key 保留到 W4（下游报告/示例可能还在读），值同新 key。
            'independent_success_rate': self.non_cascade_failure_rate,
            'ok': self.ok,
            'health_ok': self.health_ok,
            'all_failed': self.all_failed,
            'elapsed_ms': self.elapsed_ms,
            'device_recoveries': self.device_recoveries,
            'retry_attempts': self.retry_attempts,
            'rescued_by_retry': self.rescued_by_retry,
            'retry_rescue_rate': self.retry_rescue_rate,
            'tree_dumps': self.tree_dumps,
            'tree_reuses': self.tree_reuses,
            'tree_reuse_rate': self.tree_reuse_rate,
            'screenshots_saved': self.screenshots_saved,
            'screenshots_skipped': self.screenshots_skipped,
            'failures_by_kind': self.failures_by_kind(),
            'slowest': [s.to_dict() for s in self.slowest()],
            'steps': [s.to_dict() for s in self.steps],
        }
        if self.perf is not None:
            d['perf'] = self.perf
        return d


@dataclass
class SuiteResult:
    """一批用例的执行结果。"""

    cases: List[CaseResult] = field(default_factory=list)
    elapsed_ms: int = 0
    guard_stats: Dict[str, int] = field(default_factory=dict)
    #: 汇总级内存趋势曲线（各用例曲线合并后重算斜率，见 `_merge_perf_curves`）。
    #: None = 没采；报告直接消费它（`report._pick_curve`）。
    perf: Optional[Dict[str, Any]] = None

    @property
    def total(self) -> int:
        return sum(c.total for c in self.cases)

    @property
    def passed(self) -> int:
        return sum(c.passed for c in self.cases)

    @property
    def failed(self) -> int:
        return sum(c.failed for c in self.cases)

    @property
    def cascade_failed(self) -> int:
        return sum(c.cascade_failed for c in self.cases)

    @property
    def independent_failed(self) -> int:
        return sum(c.independent_failed for c in self.cases)

    @property
    def success_rate(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 1.0

    @property
    def non_cascade_failure_rate(self) -> float:
        """**非级联失败率** —— 只衡量「引擎健壮性」，**不是成功率**。

        见 `CaseResult.non_cascade_failure_rate` 的完整说明与反例。
        汇总级同样必须与 `ok` / `all_failed` / `health_ok` 一起展示。
        """
        if not self.total:
            return 1.0
        return round((self.total - self.independent_failed) / self.total, 4)

    @property
    def independent_success_rate(self) -> float:
        """【已弃用】旧名容易被读成「成功率」。用 `non_cascade_failure_rate`。"""
        import warnings
        warnings.warn(
            'independent_success_rate 已改名为 non_cascade_failure_rate：'
            '它在用例整体失败时仍会给出高分，不是成功率。'
            '判整体成败请用 ok / health_ok / all_failed。',
            DeprecationWarning, stacklevel=2)
        return self.non_cascade_failure_rate

    @property
    def all_failed(self) -> bool:
        """全部步骤都失败 —— 这种情况下任何「率」都不该被当成通过。"""
        return self.total > 0 and self.failed == self.total

    @property
    def health_ok(self) -> bool:
        """所有用例都健康 —— 与 `non_cascade_failure_rate` **并列**。"""
        return self.failed == 0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.cases)

    def kpi_ok(self, target: float = 0.95) -> bool:
        """执行引擎 验收：**整体健康** 且 非级联失败率达标（默认 95%）。

        两道闸缺一不可 —— 只靠那个率会让「大部分同类失败」的用例集
        拿到漂亮分数（见 `CaseResult.health_ok` 的说明）。
        """
        return self.health_ok and self.non_cascade_failure_rate >= target

    def failures_by_kind(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for c in self.cases:
            for k, v in c.failures_by_kind().items():
                out[k] = out.get(k, 0) + v
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    # ---- 取树账（10-08 提效的可复现来源；进 suite_report 供趋势对比）

    @property
    def tree_dumps(self) -> int:
        """整批用例**真取树**次数合计（dumpLayout 已与 cat 合并为一次往返）。"""
        return sum(c.tree_dumps for c in self.cases)

    @property
    def tree_reuses(self) -> int:
        """整批用例**复用**次数合计（跨越只读步骤的取树）。"""
        return sum(c.tree_reuses for c in self.cases)

    @property
    def tree_reuse_rate(self) -> float:
        """整批取树复用率 = 复用 /（真取 + 复用）。没取过树时返回 0.0。"""
        total = self.tree_dumps + self.tree_reuses
        return round(self.tree_reuses / total, 4) if total else 0.0

    def tree_dumps_per_step(self) -> float:
        """每步摊销真取次数（口径可比：用例步数不同也能横向比）。

        参考值：`note_stability`（10 步）10-08 改前 1.2 次/步 → 改后 0.9 次/步。
        """
        return round(self.tree_dumps / self.total, 3) if self.total else 0.0

    # ---- 截图账（分级留存）

    @property
    def screenshots_saved(self) -> int:
        """整批用例实际落盘的截图合计（tap 前置图 / 失败补图 / 独立截图）。"""
        return sum(c.screenshots_saved for c in self.cases)

    @property
    def screenshots_skipped(self) -> int:
        """整批用例按分级策略**有意省略**的截图合计 —— 省略必须可见。"""
        return sum(c.screenshots_skipped for c in self.cases)

    def refresh_perf(self) -> None:
        """从各用例曲线重算汇总级内存曲线（2C【C4】）。

        `Runner.run_suite` 自动调；**增量拼装 suite 的调用方**（逐轮
        `run_suite` 再把 `cases` 并进来，如 examples/run_suite.py 的长稳
        写法）要在拼完后自己调一次，否则汇总曲线会漏。
        """
        curves = [c.perf for c in self.cases if c.perf]
        if len(curves) == 1:
            self.perf = curves[0]
        elif curves:
            self.perf = _merge_perf_curves(curves)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'cases': len(self.cases),
            'total_steps': self.total, 'passed': self.passed,
            'failed': self.failed, 'cascade_failed': self.cascade_failed,
            'independent_failed': self.independent_failed,
            'success_rate': self.success_rate,
            'non_cascade_failure_rate': self.non_cascade_failure_rate,
            # 旧 key 保留到 W4（下游报告/示例可能还在读），值同新 key。
            'independent_success_rate': self.non_cascade_failure_rate,
            'ok': self.ok,
            'health_ok': self.health_ok,
            'all_failed': self.all_failed,
            'elapsed_ms': self.elapsed_ms,
            'failures_by_kind': self.failures_by_kind(),
            'guard_stats': self.guard_stats,
            'tree_dumps': self.tree_dumps,
            'tree_reuses': self.tree_reuses,
            'tree_reuse_rate': self.tree_reuse_rate,
            'tree_dumps_per_step': self.tree_dumps_per_step(),
            'screenshots_saved': self.screenshots_saved,
            'screenshots_skipped': self.screenshots_skipped,
            'case_results': [c.to_dict() for c in self.cases],
        }
        if self.perf is not None:
            d['perf'] = self.perf
        return d


def _merge_perf_curves(curves: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把多条用例的曲线合并成汇总曲线（长稳多轮场景）。

    序列按用例顺序拼接；斜率由 `perf.analyze_samples` 对合并后的原始样本
    **重算** —— 不能平均各自的斜率，那会在用例衔接处（如冷启动后水位回落）
    严重失真。样本不足时 analyze_samples 自己会拒绝判定（不起斜率）。
    """
    from .perf import analyze_samples
    series: List[Dict[str, Any]] = []
    raw: List[Dict[str, Any]] = []
    for c in curves:
        for s in (c.get('series') or []):
            series.append(dict(s))
            raw.append({'pss_kb': s.get('pss_kb'), 'load1': s.get('load1')})
    return {
        'bundle': curves[0].get('bundle') or '',
        'series': series,
        'analysis': analyze_samples(raw),
        'merged_cases': len(curves),
    }


# ================================================================ Runner

class Runner:
    """批量执行器：带重试、设备看护、级联识别与产物轮转。

    Parameters
    ----------
    policy:           重试策略，默认 RetryPolicy()
    guard:            设备看护，默认自动创建
    continue_on_fail: 单步失败后是否继续（默认 True，与 action.run_steps 相反）
    artifact_budget:  每个用例跑完后最多保留多少个产物文件，超出按「失败优先」清理
                      0 或 None 表示不清理。50 步连续执行会产出上百个截图，
                      不限制会直接撑爆磁盘。
    diagnose_failures: 失败步是否做归因（闭环）。**默认 False**，不改变既有行为。
                      开启后每个失败步调一次 `diagnose_failed_step`，结论写进
                      `StepResult.verdict`，并随报告落盘。
    diagnoser:        归因函数，签名 `(step_result, driver) -> Verdict`。
                      None 时用 `diagnose.diagnose_failed_step`（延迟导入，避免硬依赖）。
                      测试可以注入替身，不必真跑归因引擎。
    diagnose_signals: 归因时是否**采集设备侧信号**（崩溃/白屏/无响应/无窗口）。
                      默认 False = 只用产物目录里已有的控件树快照做离线归因（无设备往返）。
                      置 True 才会调 `collect_signals()` —— 每步失败多一次设备往返，
                      真机批量时明显变慢，所以单独开关。
    diagnose_locator_sink: 归因为「定位失败」时回写 A 的自愈台账，签名
                      `(locator_id, reason)`。⚠️ 与上面的 `locator_sink` **不是同一个东西**
                      （那个递的是 DSL 原始规格，这个递的是定位器 id）—— 见下面注释。
                      ⚠️ 更要紧的一条：**不要和 `locator_sink` 同时接** ——
                      两条路都回写会把同一次失败计两遍，把 A 修好的幂等又破坏掉。
                      集成层目前选的是执行侧那条（`locator_sink`）。
    locator_id_resolver: 规格 → locator_id 的反查（集成层注入）。**只给归因结论补 id**，
                      不回写。不接也能跑，只是归因结论里 locator_id 为空。
    collect_perf:     是否在每步之后采一轮设备内存/负载（2C【C4】）。**默认 False**
                      —— 开启后每步多两次设备往返（pidof + hidumper），
                      诊断链路不该背它；长稳场景（note_stability）才显式开。
                      曲线挂 `CaseResult.perf` / `SuiteResult.perf`，随报告落盘。
    """

    def __init__(
        self,
        policy: Optional[RetryPolicy] = None,
        guard: Optional[DeviceGuard] = None,
        continue_on_fail: bool = True,
        artifact_budget: Optional[int] = 60,
        verbose: bool = True,
        sleep_fn: Callable[[float], None] = time.sleep,
        locator_sink: Optional[Callable[[Dict[str, Any], str], None]] = None,
        diagnose_failures: bool = False,
        diagnoser: Optional[Callable[..., Any]] = None,
        diagnose_signals: bool = False,
        diagnose_locator_sink: Optional[Callable[[str, str], None]] = None,
        locator_id_resolver: Optional[Callable[[Dict[str, Any]], str]] = None,
        collect_perf: bool = False,
    ):
        self.policy = policy or RetryPolicy()
        self.guard = guard
        self.continue_on_fail = continue_on_fail
        self.artifact_budget = artifact_budget or 0
        self.verbose = verbose
        self._sleep = sleep_fn
        # 定位失败回写钩子（分工卡第五章契约：执行失败要回写，让 A 的自愈有输入）。
        # 签名为 sink(target_spec, reason)，target_spec 是 DSL 里那个原始规格
        # （如 {'id': '7'}），reason 是失败原因。
        #
        # ⚠️ 为什么传「规格」而不是「locator_id」：locator_id 是 A 的
        # `LocatorManager` 在自己册子里发的水号（L001_xxx），而执行器的定位
        # 走的是 matcher/layout，**手里根本没有这个号**。硬要 runner 认识它
        # 就得把 LocatorManager 塞进执行链路（改动太大）。所以这里定义成
        # 「把规格原样递出去」，由集成层（tools/wire_locator_sink.py）负责
        # 反查成 locator_id —— 两边都不必知道对方的内部结构。
        self.locator_sink = locator_sink

        # ---- 失败归因（闭环）
        # 在此之前 `diagnose_failed_step()` 已经就绪、`StepResult.trees/locator_id`
        # 也早就备好了，但**生产链路一次都没调用过** —— 归因只能靠 examples 手动跑，
        # 所以「崩溃 ✅ / 白屏 ✅」没有证据链。这里把它接进失败分支。
        self.diagnose_failures = bool(diagnose_failures)
        self.diagnoser = diagnoser
        self.diagnose_signals = bool(diagnose_signals)
        # ⚠️ 与 self.locator_sink **不是同一个东西**，签名也不同：
        #     self.locator_sink(spec, reason)        ← 执行器递「DSL 原始规格」
        #     diagnose_locator_sink(locator_id, reason) ← 归因递「定位器 id」
        # 直接复用会把规格当 id 传出去，在 A 的健康度台账里塞假账 —— 所以分成两个参数。
        self.diagnose_locator_sink = diagnose_locator_sink
        # ── 自愈喂料（2026-09-25）：给 sink 递「执行尝试序号」──────────
        # A 的幂等键是 (定位代次, locator_id)，而代次只在 locate() 推进；
        # 执行器走 matcher，从不调 locate() → 代次恒 0，第 2 次回写起
        # 全被 A 判成「同一次定位的重复记账」丢弃 → 连续失败停在 1，
        # 自愈阈值永远够不着（tools/wire_locator_sink.py 自测诊断段有证据）。
        # 修法：runner 每次回写递增一个单调序号随 sink 传出；A 侧把它
        # 当幂等键（locator.py 需加可选参数，见 docs/派活-给A-自愈幂等代次）。
        # sink 侧做签名自适应：旧的两参 sink 照常工作（A 修完前不炸）。
        self._attempt_seq = 0
        self._sink_takes_attempt = self._probe_sink_signature(locator_sink)
        # 规格 → locator_id 的反查（集成层注入）。**只用于给归因结论补 id**，
        # 不回写 —— 回写只有 `_report_locator_failure` 一条路，见那里。
        self.locator_id_resolver = locator_id_resolver
        # ---- 内存趋势采集（2C【C4】并入报告）--------------------------------
        # **默认关**：每步多两次设备往返（pidof + hidumper），诊断链路不该背它，
        # 长稳场景才显式开。开启后每步采一轮，曲线挂 CaseResult.perf，
        # 由 SuiteResult 合并后进 Markdown 报告（验收原句「产出内存趋势曲线
        # 并入报告」）。
        self.collect_perf = bool(collect_perf)
        # 归因自身的失败（不是被测应用的失败）。归因是**旁路**，它挂了不能影响执行，
        # 但也不能静默 —— 攒起来供报告/调用方检查。
        self.diagnose_errors: List[str] = []

    # ---------------------------------------------------------- 日志

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f'[runner] {msg}')

    # ---------------------------------------------------------- 失败归因（闭环）

    def _diagnose_step(self, driver: Driver, sr: StepResult) -> Optional[Any]:
        """对一个失败步做归因，返回 `Verdict`；不做/做不了都返回 None。

        **归因是旁路**：它自己出错绝不能影响执行结果 —— 被测用例跑挂了是事实，
        不能因为归因引擎抛异常把整轮执行也带崩。所以这里吞掉一切异常、
        记进 `self.diagnose_errors`。

        「没做归因」和「归因结论为空」是两回事，靠 `StepResult.verdict is None`
        与 `diagnose_failures` 区分 —— 报告消费方要能看出差别。
        """
        if not self.diagnose_failures:
            return None
        try:
            if self.diagnoser is not None:          # 注入的替身（测试/自定义）
                return self.diagnoser(sr, driver)
            # 延迟导入：runner 不硬依赖归因引擎（B 的模块），也避免包级导入顺序问题。
            from .diagnose import diagnose_failed_step
            adir = getattr(driver, 'artifact_dir', None)
            # 只有显式开了 diagnose_signals 才把 hdc 传下去 —— 传了它就会去采
            # 崩溃日志/截图/控件树，那是每步失败一次设备往返。
            hdc = getattr(driver, 'hdc', None) if self.diagnose_signals else None
            return diagnose_failed_step(
                sr,
                bundle=getattr(driver, 'bundle', '') or '',
                ability=getattr(driver, 'ability', 'EntryAbility') or 'EntryAbility',
                artifact_dir=adir, hdc=hdc, out_dir=adir,
                locator_sink=self.diagnose_locator_sink)
        except Exception as e:
            self.diagnose_errors.append(
                f'步骤 {sr.index} 归因失败（已降级为「不做归因」，不影响执行结果）：'
                f'{type(e).__name__}: {e}')
            return None

    # ---------------------------------------------------------- 单步

    def run_step(self, driver: Driver, index: int, action: str,
                 arg: Any) -> StepResult:
        """执行单个步骤，按策略重试，直到成功或耗尽尝试次数。"""
        sr = StepResult(index=index, action=action,
                        target=_describe(action, arg), arg=arg)
        kind = FailureKind.UNKNOWN
        max_attempts = 1
        t_step = time.time()

        attempt = 0
        while True:
            attempt += 1
            t0 = time.time()
            try:
                exec_action(driver, action, arg)
                sr.attempts.append(StepAttempt(
                    attempt=attempt, ok=True,
                    elapsed_ms=int((time.time() - t0) * 1000)))
                sr.ok = True
                break
            except Exception as e:
                kind = classify(e)
                sr.attempts.append(StepAttempt(
                    attempt=attempt, ok=False, kind=kind,
                    error=f'{type(e).__name__}: {e}',
                    elapsed_ms=int((time.time() - t0) * 1000)))

                # 首轮就定下该类别的尝试上限
                if attempt == 1:
                    max_attempts = self.policy.attempts_for(kind)

                exhausted = attempt >= max_attempts
                too_long = (time.time() - t_step) * 1000 >= self.policy.max_total_ms
                if exhausted or too_long:
                    sr.ok = False
                    sr.kind = kind
                    # 判定失败之前先留现场 —— 归因引擎靠它把置信度从 0.6 提到 0.85
                    self._capture_trees(driver, sr)
                    # 定位失败要回写，否则 A 的定位器自愈永远拿不到输入
                    self._report_locator_failure(sr)
                    break

                # 设备类 / 应用类失败 → 先做恢复，再重试
                if (kind in (FailureKind.DEVICE, FailureKind.APP)
                        and self.guard is not None):
                    self._log(f'步骤 {index} 判定为 {kind.value} 异常，'
                              f'触发恢复（第 {attempt} 次尝试后）')
                    if self.guard.recover(driver, kind=kind):
                        sr.recovered = True

                delay = self.policy.wait_ms(kind, attempt)
                if delay:
                    self._sleep(delay / 1000.0)

        sr.elapsed_ms = int((time.time() - t_step) * 1000)
        return sr

    # ---------------------------------------------------------- 失败现场

    #: 抓两张快照之间的间隔。第二张用来区分「控件自始不存在」与「界面还没稳定」。
    SNAPSHOT_GAP_MS = 200

    def _capture_trees(self, driver: Driver, sr: StepResult) -> None:
        """步骤判定失败时留现场：抓控件树快照，供归因引擎判「定位失败」。

        **为什么抓两张**：失败瞬间一张、稍等之后一张 ——
          * 两张里都没有目标控件 → 定位失败（界面里就是没有）
          * 只有后一张有         → 时序问题（界面还没稳定）
        `diagnose._locator` / `_timing` 就是按这个前提写的。

        **为什么整段包 try/except**：抓快照是**尽力而为**的旁路。它失败绝不能
        改写原判定 —— 本来是「定位失败」，不能因为抓图失败变成「未知异常」。
        抓不到就留空列表（归因侧本来就兼容：退回运行器分类、置信度下调、
        并在 evidence 里写明没有快照）。

        **为什么只抓失败步**：成功步抓图会让 50 步的用例多出上百次 dumpLayout，
        既拖慢执行又会把 artifact_budget 挤爆 —— 产物轮转是按「失败优先」
        保护文件的（见 `_prune_artifacts`），失败的现场必须留得下。
        """
        adir = getattr(driver, 'artifact_dir', None)
        for delay_ms in (0, self.SNAPSHOT_GAP_MS):
            try:
                if delay_ms:
                    self._sleep(delay_ms / 1000.0)
                before = (set(os.listdir(adir))
                          if adir and os.path.isdir(adir) else set())
                node = driver.refresh(save=True)
                if adir:
                    # refresh(save=True) 会把 JSON 落到 <artifact_dir>/NNNN_layout.json，
                    # 但不回传路径；用目录差集拿到它，避免去动 driver 的既有签名。
                    after = (set(os.listdir(adir))
                             if os.path.isdir(adir) else set())
                    fresh = [os.path.join(adir, f)
                             for f in sorted(after - before)
                             if f.endswith('.json')]
                    if fresh:
                        sr.trees.append(fresh[0])
                        continue
                if node is not None:
                    # 没配 artifact_dir：退回内存形态（归因侧接受 LayoutNode）
                    sr.trees.append(node)          # type: ignore[arg-type]
            except Exception as e:                 # noqa: BLE001 —— 旁路，绝不外抛
                self._log(f'步骤 {sr.index} 控件树快照抓取失败（不影响原判定）：'
                          f'{type(e).__name__}: {e}')
                return
        # ★ 回填到 driver 的 step 上 —— 这一步不能省。
        # 报告的数据源是 **driver.Step**（`report.collect()` 取的是
        # `driver.summary()['failed_steps']`，而那是 `driver.Step.to_dict()`），
        # 不回填的话快照只活在 `runner.StepResult` 里，报告里一个字都看不到。
        # 顺带填了 `driver.Step.layout_json` —— 那个字段一直在 `to_dict` 的输出
        # 清单里，但**从来没有任何地方给它赋过值**，所以报告里的「控件树」一直是空的。
        self._attach_trees_to_driver_step(driver, sr)

    @staticmethod
    def _attach_trees_to_driver_step(driver: Driver, sr: StepResult) -> None:
        """把快照路径挂到 driver 最后一个 step 上（报告靠它渲染）。"""
        try:
            steps = getattr(driver, 'steps', None)
            if not steps or not sr.trees:
                return
            paths = [str(t) for t in sr.trees if isinstance(t, str)]
            if not paths:
                return
            step = steps[-1]
            step.layout_json = paths[0]                      # 失败瞬间
            if len(paths) > 1:
                step.extra['layout_after'] = paths[1]        # 稳定后
        except Exception as e:                              # noqa: BLE001 —— 旁路
            self._log(f'快照回填 driver 失败（不影响原判定）：{type(e).__name__}: {e}')

    # ---------------------------------------------------------- 失败回写

    def _resolve_locator_id(self, sr: StepResult) -> str:
        """把「目标规格」翻成 A 的 `locator_id`，填进 `StepResult.locator_id`。

        为什么需要这一步（2026-09-23 闭环接线的最后一环）：
        归因定案「定位失败」后，结论里要带上 `locator_id` —— 否则归因只能说
        「没能回写定位器自愈」（`diagnose.py:929` 就是这么写的），
        读者看不出该去修哪个定位器。

        但执行器的定位走 `matcher`/`layout`，手里只有 DSL 里的原始规格
        （如 `{'id': '7'}`），**没有** A 册子里发的那个号（`L001_7`）。
        runner **不认识也不该认识** `LocatorManager`（那是 A 的模块），
        所以做成钩子：由集成层注入反查函数
        （`tools/wire_locator_sink.make_locator_id_resolver`）。

        ⚠️ 本方法**只填 id、不回写**。回写仍然是 `_report_locator_failure`
        的唯一职责 —— 两条路径都回写会把失败**计两次**，把 A 修好的幂等
        又破坏掉（表现为自愈阈值被提前触发）。
        """
        if self.locator_id_resolver is None or sr.kind is not FailureKind.LOCATE:
            return ''
        try:
            spec = sr.arg if isinstance(sr.arg, dict) else {'arg': sr.arg}
            return str(self.locator_id_resolver(dict(spec)) or '')
        except Exception as e:                 # noqa: BLE001 —— 旁路，绝不上抛
            self._log(f'locator_id 反查异常（不影响原判定）：{type(e).__name__}: {e}')
            return ''

    def _report_locator_failure(self, sr: StepResult) -> None:
        """定位失败时回写，供 A 的定位器健康度与自愈使用（分工卡第五章契约）。

        **只在 `kind is LOCATE` 时回写。** 设备掉线、应用崩溃同样会让步骤失败，
        但它们不是「这个定位器找不到了」—— 混进去会污染健康度账本，
        进而让自愈在错误的时机换代（把好好的定位器换掉）。

        旁路：回写出任何问题都绝不改写原判定，整段包 try/except。
        """
        if self.locator_sink is None or sr.kind is not FailureKind.LOCATE:
            return
        try:
            spec = sr.arg if isinstance(sr.arg, dict) else {'arg': sr.arg}
            reason = ''
            for a in reversed(sr.attempts):
                if not a.ok and a.error:
                    reason = a.error
                    break
            self._attempt_seq += 1      # 每次「定案失败的回写」= 一次新的执行尝试
            if self._sink_takes_attempt:
                self.locator_sink(dict(spec), reason or f'步骤 {sr.index} 定位失败',
                                  self._attempt_seq)
            else:
                self.locator_sink(dict(spec), reason or f'步骤 {sr.index} 定位失败')
        except Exception as e:                 # noqa: BLE001 —— 旁路，绝不上抛
            self._log(f'定位失败回写异常（不影响原判定）：{type(e).__name__}: {e}')

    @staticmethod
    def _probe_sink_signature(sink) -> bool:
        """sink 是否接受第三个位置参数 attempt（旧两参 sink 兼容）。"""
        if sink is None:
            return False
        try:
            import inspect
            for p in inspect.signature(sink).parameters.values():
                if p.kind is inspect.Parameter.VAR_POSITIONAL:
                    return True
                if p.name == 'attempt' and p.kind in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD):
                    return True
        except (TypeError, ValueError):
            return False
        return False

    # ---------------------------------------------------------- 用例

    def run_case(self, driver: Driver, case: Dict[str, Any]) -> CaseResult:
        """执行一个用例，带单步重试与级联识别。"""
        steps = case.get('steps') or []
        res = CaseResult(name=case.get('name') or '', bundle=driver.bundle)
        t0 = time.time()

        # 取树账按**增量**记：同一个 driver 被多条用例复用时（run_suite.py 之外的
        # 调用方会这么做），直接读累计值会把上一条用例的账算到这一条头上。
        _dumps0 = int(getattr(driver, 'tree_dumps', 0) or 0)
        _reuses0 = int(getattr(driver, 'tree_reuses', 0) or 0)
        _shots0 = int(getattr(driver, 'shots_saved', 0) or 0)
        _skips0 = int(getattr(driver, 'shots_skipped', 0) or 0)

        # ---- 内存趋势采集通道（2C【C4】）------------------------------------
        # 延迟导入：signals 与 runner 同层，但不让执行器硬依赖它。
        perf_ch = None
        if self.collect_perf:
            from .signals import PerfChannel
            hdc = getattr(driver, 'hdc', None)
            if hdc is not None:
                perf_ch = PerfChannel(hdc, driver.bundle or '')

        first_failure: Optional[int] = None      # 首个失败步号，用于级联追溯

        for i, st in enumerate(steps, 1):
            if not isinstance(st, dict) or not st:
                # 这一支**不做归因**：步骤本身不是合法 DSL，不是被测应用的问题，
                # 归因引擎对它只能给 UNKNOWN + 低置信度，纯属噪音。
                sr = StepResult(index=i, action='?', ok=False,
                                kind=FailureKind.DSL,
                                attempts=[StepAttempt(attempt=1, ok=False,
                                                      kind=FailureKind.DSL,
                                                      error='步骤必须是非空字典')])
                res.steps.append(sr)
                if first_failure is None:
                    first_failure = i
                if not self.continue_on_fail:
                    break
                continue

            action, arg = next(iter(st.items()))
            sr = self.run_step(driver, i, action, arg)
            # 每步跑完看一眼内存/负载水位（2C【C4】）。sample() 是旁路，
            # 采不到只记警告绝不中断 —— 「缺样本 ≠ 正常」的降级纪律
            # 在 ohauto/perf.py，这里只负责按步推进时序。
            if perf_ch is not None:
                perf_ch.sample(rounds_done=i)

            # ---- 级联识别
            # 判定依据：前一步失败过，且本步失败类别与首个失败类别相同。
            # 这不是精确因果，但足以把「一步挂带崩一片」从统计里摘出来。
            if sr.ok:
                # 中间成功过一次，说明应用已经恢复正常。
                # 之后再失败属于新问题，不能再挂到最早那次失败上 ——
                # 否则会掩盖真实存在的第二个缺陷。
                first_failure = None
            else:
                if first_failure is not None:
                    lead = res.steps[first_failure - 1]
                    if sr.kind is not None and sr.kind == lead.kind:
                        sr.cascade = True
                        sr.cascade_of = first_failure
                else:
                    first_failure = i

            # ---- 失败归因（闭环）
            # 放在级联判定之后：级联步也照样归因 —— 「这步为什么挂」和
            # 「它是不是被前面带崩的」是两个独立问题，报告的读者两个都要看。
            # 归因是旁路，抛异常已在 _diagnose_step 内降级，不会影响执行。
            if not sr.ok:
                # 先把 locator_id 补上 —— 归因结论要靠它指认「该修哪个定位器」。
                # 注意这里**不回写**：回写已在 run_step 里由 _report_locator_failure
                # 做过一次，再来一次就是重复计数。
                if not sr.locator_id:
                    lid = self._resolve_locator_id(sr)
                    if lid:
                        sr.locator_id = lid
                sr.verdict = self._diagnose_step(driver, sr)

            if self.verbose:
                flag = 'OK  ' if sr.ok else 'FAIL'
                extra = ''
                if sr.retried:
                    extra += f' [{sr.attempt_count} 次尝试]'
                if sr.recovered:
                    extra += ' [设备恢复]'
                if sr.cascade:
                    extra += f' [级联<-{sr.cascade_of}]'
                self._log(f'{flag} {i:>3}/{len(steps)} {action:<12} '
                          f'{sr.target[:38]:<38} ({sr.elapsed_ms}ms){extra}')
                # 归因结论必须当场可见 —— 藏在产物 JSON 里的结论，
                # 在没人翻文件的批处理里等于没做（同「只打印警告等于不存在」）。
                if sr.diagnosed:
                    self._log(f'     归因: {sr.verdict_cn}')

            res.steps.append(sr)
            if not sr.ok and not self.continue_on_fail:
                self._log(f'步骤 {i} 失败，按配置中断用例')
                break

        res.elapsed_ms = int((time.time() - t0) * 1000)
        res.device_recoveries = sum(1 for s in res.steps if s.recovered)
        # 取树账落在 CaseResult 上（suite 级由它汇总）—— 提效收益以后从
        # suite_report.json 直接读得到，不必再回会话记录里手工数。
        res.tree_dumps = max(0, int(getattr(driver, 'tree_dumps', 0) or 0) - _dumps0)
        res.tree_reuses = max(0, int(getattr(driver, 'tree_reuses', 0) or 0) - _reuses0)
        # 截图账同口径增量（分级留存）
        res.screenshots_saved = max(0, int(getattr(driver, 'shots_saved', 0) or 0) - _shots0)
        res.screenshots_skipped = max(0, int(getattr(driver, 'shots_skipped', 0) or 0) - _skips0)
        # 曲线只在**真的采过**时挂上（空步骤的用例没有样本 → 保持 None，
        # 「没采」与「采了但缺样本」由此可辨）。
        if perf_ch is not None and perf_ch.samples:
            res.perf = perf_ch.curve()
        if self.artifact_budget:
            self._prune_artifacts(driver, res)
        return res

    # ---------------------------------------------------------- 批量

    def run_suite(
        self,
        items: Sequence[Tuple[Optional[Driver], Dict[str, Any]]],
        guard: Optional[DeviceGuard] = None,
    ) -> SuiteResult:
        """批量执行。items 为 (driver, case) 序列。

        driver 传 None 时由调用方通过 driver_factory 预先构造好 —— 
        这里不做构造，避免 Runner 掺和设备创建策略（用例间是否复用连接）。
        """
        suite = SuiteResult()
        t0 = time.time()

        # 连续执行前先把屏幕钉住：息屏即锁屏，锁屏后控件树全空，
        # 那种失败看起来像引擎崩了，实际只是屏幕黑了。
        if guard is not None and items:
            self._log('执行前检查屏幕状态（唤醒 + 延长息屏超时）...')
            if not guard.ensure_awake():
                self._log('警告：屏幕/窗口状态异常，连续执行中途可能取不到控件树')

        for idx, (driver, case) in enumerate(items, 1):
            if driver is None:
                self._log(f'第 {idx} 项没有 driver，跳过')
                continue
            name = case.get('name') or f'case{idx}'
            self._log(f'===== 用例 {idx}/{len(items)}: {name} =====')
            suite.cases.append(self.run_case(driver, case))
        suite.elapsed_ms = int((time.time() - t0) * 1000)
        if guard is not None:
            suite.guard_stats = guard.stats()
        # 汇总级内存曲线：多轮/多用例合并成一条（单条曲线直接沿用，不重算）。
        suite.refresh_perf()
        return suite

    # ---------------------------------------------------------- 产物轮转

    def _prune_artifacts(self, driver: Driver, res: CaseResult) -> None:
        """按「失败优先」清理产物，防止连续执行撑爆磁盘。

        保留规则：
          * 失败步骤的截图/控件树 —— 排查问题全靠它，必留
          * 每个用例最后 N 个步骤的产物 —— 崩溃现场常在末尾
          * 其余按文件时间从旧到新删，直到降到预算以内
        """
        adir = getattr(driver, 'artifact_dir', None)
        if not adir or not os.path.isdir(adir) or not self.artifact_budget:
            return

        try:
            files = [os.path.join(adir, f) for f in os.listdir(adir)]
            files = [f for f in files if os.path.isfile(f)]
        except OSError:
            return
        if len(files) <= self.artifact_budget:
            return

        protected = set()
        # 失败步骤对应的产物（按步骤序号前缀匹配）
        bad_prefixes = {f'{s.index:04d}' for s in res.steps if not s.ok}
        for f in files:
            base = os.path.basename(f)
            if base[:4] in bad_prefixes:
                protected.add(f)
        # 末尾若干步
        tail_prefixes = {f'{s.index:04d}' for s in res.steps[-3:]}
        for f in files:
            if os.path.basename(f)[:4] in tail_prefixes:
                protected.add(f)

        removable = sorted((f for f in files if f not in protected),
                           key=lambda p: os.path.getmtime(p))
        budget = max(0, self.artifact_budget - len(protected))
        to_remove = removable[:max(0, len(removable) - budget)]
        removed = 0
        for f in to_remove:
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
        if removed:
            self._log(f'产物轮转：清理 {removed} 个中间产物，'
                      f'保留 {len(files) - removed} 个（预算 {self.artifact_budget}）')


# ================================================================ 便捷入口

def _describe(action: str, arg: Any) -> str:
    """把动作参数压成一句人看得懂的描述，用于日志与报告。"""
    a = (action or '').strip().lower()
    if a in ('start', 'launch'):
        return '启动应用'
    if a == 'stop':
        return '停止应用'
    if a in ('waitidle', 'wait_idle'):
        return '等待界面稳定'
    if a in ('back', 'key_back'):
        return '返回'
    if a in ('home', 'key_home'):
        return '回桌面'
    if a in ('screenshot', 'screencap'):
        return str(arg) if arg else '截图'
    if isinstance(arg, dict):
        for k in ('id', 'text', 'text_contains', 'label', 'descr', 'anchor',
                  'exists', 'gone', 'text'):
            if k in arg:
                v = arg[k]
                if isinstance(v, dict):
                    return f'{k}={_describe(action, v)}'
                return f'{k}={v}'
        if 'direction' in arg:
            return f"direction={arg['direction']}"
        return ','.join(list(arg.keys())[:3])
    if isinstance(arg, bool):
        return '是' if arg else '否'
    if isinstance(arg, (str, int, float)) or arg is None:
        return str(arg)
    return type(arg).__name__


def run_case(driver: Driver, case: Dict[str, Any],
             policy: Optional[RetryPolicy] = None,
             guard: Optional[DeviceGuard] = None,
             verbose: bool = True) -> CaseResult:
    """便捷函数：用默认 Runner 跑一个用例。"""
    r = Runner(policy=policy, guard=guard, verbose=verbose)
    return r.run_case(driver, case)


def run_suite(drivers_cases: Sequence[Tuple[Optional[Driver], Dict[str, Any]]],
              policy: Optional[RetryPolicy] = None,
              guard: Optional[DeviceGuard] = None,
              verbose: bool = True) -> SuiteResult:
    """便捷函数：用默认 Runner 跑一批用例。"""
    r = Runner(policy=policy, guard=guard, verbose=verbose)
    return r.run_suite(drivers_cases, guard=guard)


# ================================================================ 契约入口

def run(cases: Sequence[Any], device: Any = None, *,
        policy: Optional[RetryPolicy] = None,
        guard: Optional[DeviceGuard] = None,
        out_dir: Optional[str] = None,
        continue_on_fail: bool = True,
        artifact_budget: int = 60,
        kpi: Optional[float] = None,
        default_timeout: int = 8000,
        poll_interval: int = 300,
        verbose: bool = True) -> RunReport:
    """**系统对外接口契约**：

        run(cases, device) -> RunReport

    `Runner.run_case` / `Runner.run_suite` 是内部实现，返回信息更细的
    `CaseResult` / `SuiteResult`；本函数负责把它们适配成契约里声明的
    `RunReport`，让 A、B 与 CI 有一条统一、稳定的调用路径。

    为什么要有这一层：契约里的返回类型是 `RunReport`（`action.py` 里已有），
    而 本版的内部实现返回 `SuiteResult`。两者字段不同 —— 如果不在接口冻结前
    对齐，W2 之后的集成会直接断在这里。**契约类型负责对外，内部类型负责细节。**

    适配后的 `RunReport` 附带一个 `suite` 属性，指向完整的 `SuiteResult`，
    需要失败归因分布、卡顿点、重试明细的调用方可以直接取用：

        rep = run(cases, device)
        rep.ok                     # 契约字段
        rep.suite.failures_by_kind()   # 富信息（信号采集 / B 的归因输入）

    Parameters
    ----------
    cases:  用例序列。元素可以是 case 字典、YAML/JSON 文件路径，或
            (driver, case) 二元组（需要精细控制连接复用时）
    device: 设备描述。支持四种形态：
              None            —— 真机，自动定位 hdc
              dict            —— {'sim': True} 走模拟设备；
                                 {'bundle','ability','hdc_path','screen',
                                  'start_page','faults','locked'} 精细控制
              Hdc / FakeHdc   —— 直接复用已有连接
              Driver          —— 直接复用已有驱动器
    kpi:    给了就顺带判定验收阈值，结果挂在 `rep.kpi_ok` 上
    """
    from .action import load_case
    from .hdc import Hdc

    # ---------------------------------------------------------- 解析用例
    norm: List[Dict[str, Any]] = []
    for c in cases:
        if isinstance(c, dict):
            norm.append(c)
        elif isinstance(c, str):
            norm.append(load_case(c))
        else:
            raise TypeError(f'用例必须是 dict 或文件路径，收到 {type(c).__name__}')
    if not norm:
        return RunReport(name='(空)', total=0, passed=0, failed=0)

    # 底座 bundle/ability：首用例兜底，设备 dict 优先（向后兼容原行为）。
    # ★ 多用例时每个用例可自带 bundle/ability 覆盖底座 —— 不能只取 norm[0]，
    #   否则混包用例会被静默发到第一个包（评审 P2）。
    base_bundle = norm[0].get('bundle') or ''
    base_ability = norm[0].get('ability') or 'EntryAbility'
    if isinstance(device, dict):
        base_bundle = device.get('bundle') or base_bundle
        base_ability = device.get('ability') or base_ability
    base_bundle = base_bundle or 'com.unknown'

    # ---------------------------------------------------------- 解析设备
    dev_obj = None                     # Hdc / FakeHdc
    shared_driver: Optional[Driver] = None
    if isinstance(device, Driver):
        shared_driver = device
        dev_obj = device.hdc
        base_bundle = device.bundle
        base_ability = device.ability
    elif device is not None and hasattr(device, 'dump_layout'):
        dev_obj = device
    else:
        cfg = device if isinstance(device, dict) else {}
        if cfg.get('sim'):
            from .sim import FakeHdc
            dev_obj = FakeHdc(
                start_page=cfg.get('start_page', 'login'),
                # 此默认值是 **模拟设备夹具**（sim 种子数据按 1080×2340 搭建），
                # 不是真机几何；真机几何只走 Driver.screen_size() 实测链（评审 P2 澄清）
                screen=tuple(cfg.get('screen', (1080, 2340))),
                verbose=False,
                faults=cfg.get('faults'),
                locked=bool(cfg.get('locked')))
        else:
            dev_obj = Hdc(hdc_path=cfg.get('hdc_path')) if cfg.get('hdc_path') else Hdc()

    if dev_obj is not None and getattr(dev_obj, 'target', None) is None:
        try:
            ts = dev_obj.list_targets()
            dev_obj.target = (ts[0] if ts else None)
        except Exception:
            pass

    # ---------------------------------------------------------- 执行
    drv_for_guard = shared_driver
    if guard is None and dev_obj is not None:
        guard = DeviceGuard(dev_obj, verbose=verbose)

    runner = Runner(policy=policy, guard=guard, verbose=verbose,
                    continue_on_fail=continue_on_fail,
                    artifact_budget=artifact_budget)

    items: List[Tuple[Optional[Driver], Dict[str, Any]]] = []
    for case in norm:
        case_bundle = case.get('bundle') or base_bundle
        case_ability = case.get('ability') or base_ability
        d = shared_driver or Driver(bundle=case_bundle, ability=case_ability, hdc=dev_obj,
                                    artifact_dir=out_dir,
                                    default_timeout=default_timeout,
                                    poll_interval=poll_interval,
                                    verbose=False)
        drv_for_guard = drv_for_guard or d
        items.append((d, case))

    suite = runner.run_suite(items, guard=guard)

    # ---------------------------------------------------------- 适配成契约类型
    rep = RunReport(
        name='、'.join((c.get('name') or '?') for c in norm[:3])
             + ('…' if len(norm) > 3 else ''),
        bundle=base_bundle,
        total=suite.total,
        passed=suite.passed,
        failed=suite.failed,
        elapsed_ms=suite.elapsed_ms,
    )
    for cr in suite.cases:
        for sr in cr.steps:
            if sr.ok:
                continue
            last = sr.attempts[-1] if sr.attempts else None
            rep.errors.append({
                'case': cr.name,
                'step': sr.index,
                'action': sr.action,
                'arg': sr.target,
                'kind': sr.kind.value if sr.kind else 'UNKNOWN',
                'attempts': sr.attempt_count,
                'cascade': sr.cascade,
                'error': (last.error if last else '')[:500],
            })
    # 富信息随契约对象一起带出去，避免调用方为了细节再走一遍内部实现
    rep.suite = suite                       # type: ignore[attr-defined]
    rep.kpi_target = kpi                    # type: ignore[attr-defined]
    rep.kpi_ok = suite.kpi_ok(kpi) if kpi is not None else None   # type: ignore[attr-defined]
    return rep
