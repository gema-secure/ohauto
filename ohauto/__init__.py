"""
ohauto — OpenHarmony 应用 UI 自动化能力层
==========================================

面向 OpenHarmony 应用的 UI 自动化框架，基于 hdc 命令行通路（设备侧零部署）。

分层设计：
    L1 执行层   hdc.py       —— hdc 命令封装（连接 / 超时 / 重试 / 文件拉取）
    L2 定位层   layout.py    —— 控件树 JSON 解析 + 控件 → 屏幕坐标（LayoutNode.center）
                matcher.py   —— 多属性匹配器（对齐 UiTest ON 语义）
    L3 语义层   driver.py    —— Driver 门面（tap / input / swipe / waitFor）
                action.py    —— Action DSL（YAML 序列化，可生成 / 可沉淀 / 可重放）
                vision.py    —— 多模态定位（可插拔 Provider）
    L4 应用层   explorer.py  —— 自动探索（页面状态图）
                report.py    —— 报告生成
                runner.py    —— 批量执行 + 单步重试 + 设备自愈 + 级联识别
                                （driver 解决「一步怎么做对」，
                                  runner 解决「一百步连续做，还能不能做完」）

典型用法：
    from ohauto import Driver, ON

    with Driver(bundle='com.example.app') as d:
        d.start()
        d.tap(ON.text('登录'))
        d.input(ON.id('username'), 'alice')
        d.assert_exists(ON.text('首页'))

连续执行用 Runner：
    from ohauto import Runner, DeviceGuard, run_suite
    g = DeviceGuard(d.hdc)
    Runner(guard=g).run_case(d, case)

★ 系统对外接口契约（所有调用方统一走这个）：
    from ohauto import run
    rep = run(cases, device)          # -> RunReport
    rep.ok                            # 契约字段
    rep.suite.failures_by_kind()      # 富信息（归因输入）

    `run` 返回的是契约类型 RunReport；`Runner.run_case/run_suite` 是内部实现，
    返回信息更细的 CaseResult/SuiteResult。**契约类型负责对外，内部类型负责细节。**
"""

from .hdc import Hdc, HdcError, DeviceNotFound
from .layout import LayoutNode, parse_layout
from .matcher import ON, Matcher
from .driver import Driver, DriverError
from .action import RunReport
from .runner import (Runner, DeviceGuard, RetryPolicy, FailureKind,
                     CaseResult, SuiteResult, StepResult, StepAttempt,
                     run, run_case, run_suite)
from .signals import (collect_signals, Signals, CrashRecord, Anomaly,
                      parse_crash_log, parse_fault_filename)

__version__ = '0.3.0'

__all__ = [
    'Hdc', 'HdcError', 'DeviceNotFound',
    'LayoutNode', 'parse_layout',
    'ON', 'Matcher',
    'Driver', 'DriverError',
    'RunReport',
    'Runner', 'DeviceGuard', 'RetryPolicy', 'FailureKind',
    'CaseResult', 'SuiteResult', 'StepResult', 'StepAttempt',
    'run', 'run_case', 'run_suite',
    # 信号采集
    'collect_signals', 'Signals', 'CrashRecord', 'Anomaly',
    'parse_crash_log', 'parse_fault_filename',
]
