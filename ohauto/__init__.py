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

★ 分工卡接口总表约定的 C 对外接口（A、B、CI 请统一走这个）：
    from ohauto import run
    rep = run(cases, device)          # -> RunReport
    rep.ok                            # 契约字段
    rep.suite.failures_by_kind()      # 富信息（归因输入）

    `run` 返回的是契约类型 RunReport；`Runner.run_case/run_suite` 是内部实现，
    返回信息更细的 CaseResult/SuiteResult。**契约类型负责对外，内部类型负责细节。**
"""

import os
import re

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
from .diagnose import (diagnose, diagnose_all, summarize, Verdict, Category,
                       ExecutionRecord, CATEGORY_CN)
# 2026-09-23 追加（B 闭环接入 API）：失败步 → Verdict 的一站式入口。
# ⚠️ 这是**追加**，不是覆盖 —— B 交付包里的 __init__.py 基线是 9-22 之前的，
#    整文件覆盖会丢掉下方 A 模块（locator/treesum/vision）的全部导出。
#    见 docs/给B-回执-2026-09-23.md §2.1。
from .diagnose import diagnose_failed_step, load_snapshots
from .generator import (Generator, Case, TestPoint, GenerationReport,
                        GenerationOutcome, GenerationError, RejectReason,
                        ValidationIssue, LLMProvider, ScriptedProvider,
                        NullProvider, OpenAICompatibleProvider,
                        generate, generate_many,
                        StressKind, StressSpec, SwipeSafety, STRESS_KIND_CN,
                        generate_stress, stress_cases, build_stress_case,
                        split_stress_cases, check_swipe_safety,
                        swipe_endpoints, swipe_safe_scale, stress_safety_report,
                        pick_stress_target, SWIPE_EDGE_MARGIN_PX)
# A 模块（定位与感知）—— 分工卡接口总表：A -> B/C 的 locate / 自愈
from .locator import (LocatorManager, LocateResult, LocatorSpec,
                      LocatorHealth, RepairReport, LocatorMissError)
# 2026-09-23 追加（A5 正式版）：B3 的 Generator(catalog_fn=...) 控件清单。
# ⚠️ 同样是**追加**而非覆盖 —— A 交付包按边界声明没带 __init__.py，
#    避免用旧版基线覆盖这里的合并版导出。
from .treesum import (summarize_tree, summary_lines, count_nodes,
                      control_catalog)
# ⚠️ 集成时发现的重名：vision.py:99 与 generator.py:211 **各有一个
#    OpenAICompatibleProvider**，基类不同（VisionProvider vs LLMProvider）、
#    用途不同（看图出 bbox vs 文本生成用例）。两者单独开发时都无感，
#    只有三方合并后在**包级导出**这里才会互相覆盖。
#    处理：给 A 的那个起别名，B 的保持原名不动（不破坏已有引用）。
#    模块内继续用原名即可 —— 全仓测试都是全限定导入（from ohauto.vision import …）。
from .vision import (MockProvider, OpenAICompatibleProvider as VisionOpenAIProvider,
                     HybridLocator, TieredVisionLocator, VisualTarget,
                     VisionError, VisionConfigError, build_provider)
# C3 权限弹窗：识别 + 策略（默认点「禁止」，见 ohauto/permission.py）
from .permission import (PermissionDialogVerdict, detect_permission_dialog,
                         resolve_policy, POLICIES, DEFAULT_POLICY, ENV_POLICY,
                         POLICY_RECORD, POLICY_ALLOW, POLICY_DENY)

# ---------------------------------------------------------------- 版本


def _resolve_version() -> str:
    """取发行版本号 —— `pyproject.toml` 是唯一源，本文件不再写字面量。

    已安装（含可编辑安装）时读发行元数据；源码直用（仓库根下 `import ohauto`）
    时元数据不存在，就回退解析同一个 `pyproject.toml` —— 仍属同一个源，
    不是第二份版本号。
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version('ohauto')
    except PackageNotFoundError:
        pass

    pyproject = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'pyproject.toml')
    try:
        with open(pyproject, encoding='utf-8') as f:
            found = re.search(r'^version\s*=\s*["\']([^"\']+)', f.read(),
                              re.MULTILINE)
    except OSError:
        return '0.0.0+unknown'
    return found.group(1) if found else '0.0.0+unknown'


__version__ = _resolve_version()

__all__ = [
    'Hdc', 'HdcError', 'DeviceNotFound',
    'LayoutNode', 'parse_layout',
    'ON', 'Matcher',
    'Driver', 'DriverError',
    'RunReport',
    'Runner', 'DeviceGuard', 'RetryPolicy', 'FailureKind',
    'CaseResult', 'SuiteResult', 'StepResult', 'StepAttempt',
    'run', 'run_case', 'run_suite',
    # C4 信号采集
    'collect_signals', 'Signals', 'CrashRecord', 'Anomaly',
    'parse_crash_log', 'parse_fault_filename',
    # B4 失败归因（分工卡接口总表：B -> C 的 diagnose）
    'diagnose', 'diagnose_all', 'summarize',
    'Verdict', 'Category', 'CATEGORY_CN', 'ExecutionRecord',
    # B4 闭环接入（2026-09-23 追加）
    'diagnose_failed_step', 'load_snapshots',
    # B3 自然语言转用例（分工卡接口总表：B -> C 的 generate）
    'generate', 'generate_many',
    'Generator', 'Case', 'TestPoint', 'RejectReason', 'ValidationIssue',
    'GenerationReport', 'GenerationOutcome', 'GenerationError',
    'LLMProvider', 'ScriptedProvider', 'NullProvider', 'OpenAICompatibleProvider',
    # B5 压测用例生成
    'generate_stress', 'stress_cases', 'build_stress_case', 'split_stress_cases',
    'StressKind', 'StressSpec', 'STRESS_KIND_CN', 'SwipeSafety',
    'check_swipe_safety', 'swipe_endpoints', 'swipe_safe_scale',
    'stress_safety_report', 'pick_stress_target', 'SWIPE_EDGE_MARGIN_PX',
    # A 定位与感知（分工卡接口总表：A 的 locate / record_locator_failure /
    # repair_locators；A5 控件树摘要器；视觉 Provider）
    # 注意 'VisionOpenAIProvider' 是别名 —— 见上方 import 处的重名说明
    'LocatorManager', 'LocateResult', 'LocatorSpec', 'LocatorHealth',
    'RepairReport', 'LocatorMissError',
    'summarize_tree', 'summary_lines', 'count_nodes', 'control_catalog',
    'MockProvider', 'VisionOpenAIProvider', 'HybridLocator',
    'TieredVisionLocator', 'VisualTarget', 'VisionError',
    'VisionConfigError', 'build_provider',
    # C3 权限弹窗识别与处置（策略默认 deny）
    'PermissionDialogVerdict', 'detect_permission_dialog', 'resolve_policy',
    'POLICIES', 'DEFAULT_POLICY', 'ENV_POLICY',
    'POLICY_RECORD', 'POLICY_ALLOW', 'POLICY_DENY',
]
