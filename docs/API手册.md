# API 手册

> 面向使用者与 CI 集成：`ohauto` 包的公共 API、命令行工具、退出码约定与结果信号格式。
> 快速上手见 [README](../README.md)。

---

## 一、包导入与公共 API

所有公开名称从包根导入：`from ohauto import Driver, Runner, Generator`。
权威清单是 `ohauto/__init__.py` 的 `__all__`，本节按功能域分组摘录。

### 设备与驱动

| 名称 | 说明 |
|---|---|
| `Hdc` | hdc 命令封装。`Hdc.shell()` 返回 `ShellResult`（不是 str）；`Hdc.transient_retries = 2` 提供通道级瞬时失败重试（`run(..., transient=False)` 可关） |
| `HdcError` / `DeviceNotFound` | 设备连接异常 |
| `Driver` | 执行驱动。`refresh()` 取控件树（合并 dump+cat 一次往返）；只读步骤自动复用树，动作步骤前置重取；`tree_dumps` / `tree_reuses` 对外计数 |
| `DriverError` / `Step` | 驱动异常与步骤定义 |
| `HdcLike`（`ohauto.hdc`） | 设备接口**窄契约**（15 方法 + `tmp_dir`），`Hdc` 与 `FakeHdc` 都满足；删除契约成员会让引用处类型检查失败 |
| `InputBackend` / `InputUnavailable`（`ohauto.hdc`） | 写动作注入通路窄契约；通路不可用或某动作在该通路无法表达时响亮失败 |
| `Hdc.detect_backend(force=, verbose=)` | 探测并切换输入通路，返回 `{'selected','interactive','probes'}`；顺序 uitest → uinput → sendevent，皆无则 `none` |
| `Hdc.backend_name` / `Hdc.interactive` | 当前生效通路名（uitest / uinput / sendevent / none）与「能否做写操作」 |

### 控件树与定位

| 名称 | 说明 |
|---|---|
| `parse_layout` / `LayoutNode` | 控件树解析（深度/嵌套保护，超限截断留痕） |
| `ON` / `Matcher` | 控件匹配器（构造期拒绝危险正则形状） |
| `LocatorManager` | 定位管理：`locate(target, page, ...)`；同位置连续失败达阈值触发换代重生 |
| `LocatorSpec` / `LocateResult` / `LocatorHealth` / `RepairReport` / `LocatorMissError` | 定位规格、结果、健康度与自愈报告（失败留并列候选 `ambiguous`） |
| `repair_locators(..., image_path=, screen_size=)` | 树内线索全灭时回退视觉通道定位（三道闸），无图时行为不变 |
| `HybridLocator` / `TieredVisionLocator` / `VisualTarget` | 截图视觉定位（`build_provider()` 按 `OHAUTO_VISION_*` 环境变量构建） |
| `summarize_tree` / `summary_lines` / `count_nodes` / `control_catalog` | 控件树摘要与控件清单 |

### 用例执行

| 名称 | 说明 |
|---|---|
| `Runner` / `run` / `run_case` / `run_suite` | 执行入口。`Runner.run_case(driver, case) -> CaseResult`；套件级 `run_suite`；`Runner(collect_perf=True)` 每步采内存 / 负载 |
| `CaseResult` / `StepResult` / `StepAttempt` / `SuiteResult` | 结果对象（`SuiteResult` 含 `tree_dumps` / `tree_reuses` / `tree_reuse_rate` 提效计数；`collect_perf` 开启时含 `perf` 内存趋势曲线） |
| 断言原语 | `Driver.assert_checked` / `assert_enabled` / `assert_count` / `assert_memory_below`（性能断言，缺样本不判通过） |
| `DeviceGuard` / `RetryPolicy` / `FailureKind` | 息屏保活、重试策略、失败分类 |
| `load_case` / `dump_case` / `run_steps` / `trace_to_steps` | 用例 DSL 装载 / 导出 / 步骤执行 / 轨迹反推（`DslError` 为格式异常） |

### 用例生成（NL→用例）

| 名称 | 说明 |
|---|---|
| `Generator` | 两阶段生成（测试点→用例）。`generate(description, *, page=None, pages=None)`；多页时控件池取并集，导航后断言只能引用目标页控件 |
| `generate` / `generate_many` | 模块级便捷入口 |
| `LLMProvider` / `ScriptedProvider` / `NullProvider` / `OpenAICompatibleProvider` / `MockProvider` | Provider 抽象与实现（环境变量 `OHAUTO_LLM_BASE_URL` / `OHAUTO_LLM_MODEL` / `OHAUTO_LLM_API_KEY`） |
| `Case` / `TestPoint` / `RejectReason` / `ValidationIssue` / `GenerationReport` | 用例对象与校验产物（空壳闸 `NO_SUBSTANCE` / 测试点闸 `NO_TEST_POINT`） |
| `generate_stress` / `stress_cases` / `SwipeSafety` | 压测用例生成与滑动安全检查 |

### 失败归因与自愈

| 名称 | 说明 |
|---|---|
| `diagnose` / `diagnose_all` / `diagnose_failed_step` | 失败归因（多源证据 → `Verdict`，带置信度与证据链） |
| `Category` / `CATEGORY_CN` / `ExecutionRecord` / `load_snapshots` | 归因分类与执行记录装载 |
| `collect_signals` / `Signals` / `CrashRecord` / `Anomaly` / `parse_crash_log` | 结果信号采集（见 §四） |

### 跨形态比对与设备形态

| 名称 | 说明 |
|---|---|
| `compare_forms(...) -> CompareReport` | 折叠展开两态差异比对（`DiffKind` / `Severity` / `has_high()`） |
| `crossform_report.write_all(report, out_dir, stem)` | 一次性产出 JSON / Markdown / HTML 报告 |
| `FormProfile` / `load_profiles` / `find_profile` | 设备形态档（安全区 / 挖孔 / 折叠态判定） |

### 权限弹窗

| 名称 | 说明 |
|---|---|
| `detect_permission_dialog` / `PermissionDialogVerdict` | 识别系统权限门（窗口属主 + 可见可点允许 / 禁止按钮，两条同时成立） |
| `resolve_policy` / `POLICY_DENY` / `POLICY_ALLOW` / `POLICY_RECORD` | 策略解析与三档取值（默认 `deny`），见 §五 |

### 性能与资源采样

| 名称 | 说明 |
|---|---|
| `device_sample` / `host_sample` / `take_sample` | 单轮采样：设备 PSS / loadavg / 进程存活、宿主内存 |
| `analyze_samples` / `PSS_SLOPE_THRESHOLD` / `MIN_PSS_SAMPLES` | 趋势分析：前后半均值涨幅判据，样本不足不判 |
| `PerfChannel` | 面向编排的采样通道：持有一串采样点，出内存趋势曲线 |
| `collect_signals(..., collect_perf=True)` | 采样一轮并随信号一起返回（见 §四） |

---

## 二、命令行工具（tools/）

| 命令 | 用途 |
|---|---|
| `python -m ohauto.doctor` | 环境自检（设备在场 / SDK / 依赖）；最后一关逐条列出输入通路 uitest / uinput / sendevent 的可用性 |
| `python tools/quality_gate.py` | 一条命令全量门禁（单测 + 覆盖率 + 静态检查） |
| `python tools/demo_full_chain.py [--real]` | 探索→生成→校验→执行→归因→自愈→沉淀 一键整链演示 |
| `python tools/eval_kpi_offline.py` | 离线 KPI 评测（归因 / 自愈 / 探索冒烟，免设备） |
| `python tools/trifusion.py` | N 源融合定位 CLI（静态/控件树/视觉/OCR 证据裁决） |
| `python tools/profile_locators.py <样本目录>` | 控件树 id/文案覆盖率画像 |
| `python tools/eval_vision_offline.py --provider openai --no-thinking` | 视觉定位离线评测（免设备，需 key） |
| `python tools/verify_locator_degrade.py [--vision]` | 定位降级/自愈真机验证（四场景 A/B/C/D） |
| `python tools/measure_locate_latency.py --rounds 20 --label <场景>` | 定位延迟分段测量（设备侧/宿主侧） |
| `python tools/case_health.py` | 用例健康度（可失败性校验） |
| `python tools/crossform_run.py --device <实例> --baseline open --target close` | 跨形态差异比对（退出码见 §三） |
| `python tools/stability_telemetry.py` / `stability_trend.py` / `b15_app_stability.py` | 长稳遥测与趋势（用法见 §六） |
| `python tools/export_hypium.py` / `sign_hap.py` | hypium 脚本导出与 HAP 签名 |
| `python tools/emulator_cli.py start --skip-check` | DevEco 模拟器管理（用法见 §六） |
| `python examples/collect_signals.py --bundle <包名> [--sim]` | 信号采集 CLI（`--sim` 无真机走通） |
| `python examples/run_suite.py <用例...> [--perf]` | 套件执行（`--perf` 每步采内存 / 负载，趋势曲线并入报告） |

---

## 三、退出码约定（三态）

所有 `tools/` 脚本遵守统一三态，CI 据此区分「失败」与「跳过」：

| 退出码 | 含义 | CI 应如何处理 |
|---|---|---|
| **0** | 通过（检查项全过 / 产物正常生成） | 继续 |
| **1** | **未达标**（跑完了但不合格：门禁红、用例失败、前置资源缺失、用法错误） | **失败**，必须处理 |
| **2** | **设备不在场**（真机没插 / 模拟器未就绪——补环境后同一命令能过） | **跳过**（不是代码的错） |

判定口诀：**这个失败，把设备插上会不会消失？** 会 → `2`；不会 → `1`；都没问题 → `0`。

两条豁免：argparse 用法错误天然返 2（伴随 stderr 用法文本，调用方以此区分）；
`tools/ocr_worker.py` 是子进程私有协议（父进程只判 `rc != 0` 即降级），不受本约定管辖。

---

## 四、结果信号（signals）

`collect_signals()` 在失败/崩溃现场打包多源证据，除参数错误外**不抛异常**，
任何一步失败记入 `warnings` 并降级：

```python
from ohauto import collect_signals, Signals, CrashRecord, Anomaly, parse_crash_log

sig: Signals = collect_signals(hdc, bundle='com.ohos.note')
# sig.crashes:  List[CrashRecord]   —— cppcrash/faultlogger 日志解析（parse_crash_log）
# sig.anomalies: List[Anomaly]      —— 白屏 / 无窗口 / 布局异常，带置信度
# sig.warnings:  采集过程的降级说明
```

性能采样随信号一并返回：`collect_perf=True` 时 `sig.perf` 是 `PerfChannel`，
持有一串采样点、可出内存趋势曲线（并入报告）。缺样本记 `None` + 警告，
绝不编造 —— 「没采到」与「采到了且达标」必须可分。

时间基准一律取**设备侧时钟**（本机时间比对会把所有崩溃误判成窗口外）；
时钟回跳留下的旧日志自动识别剔除。信号夹具与模拟注入见
`tests/fixtures/signals/` 与 `python examples/collect_signals.py --sim`。

---

## 五、环境变量

| 变量 | 用途 |
|---|---|
| `OHAUTO_LLM_BASE_URL` / `OHAUTO_LLM_MODEL` / `OHAUTO_LLM_API_KEY` | 用例生成 Provider（OpenAI 兼容接口） |
| `OHAUTO_VISION_BASE_URL` / `OHAUTO_VISION_MODEL` / `OHAUTO_VISION_API_KEY` | 视觉定位 Provider |
| `OHAUTO_SDK_ROOT` | SDK 脚本的根目录覆写 |
| `OHAUTO_TARGET_SERIAL` | 目标设备串号（多设备时必须显式指定） |
| `OHAUTO_VENV_PY` | OCR 子进程桥用的解释器（默认指向项目约定 venv） |
| `OHAUTO_PERMISSION_POLICY` | 系统权限弹窗策略：`record` / `allow` / `deny`（默认 `deny`） |

> key 只走环境变量，不落盘、不入库。

### 系统权限弹窗（C3）

应用冷启动常弹系统权限门（属主 `com.ohos.permissionmanager`）。它盖住内容区时
`dumpLayout` 返回的是**弹窗的树**，定位、探索、压测会一起卡在上面 —— 真机实测过
一整套冒烟因为一个弹窗从 6/6 掉到 4/6。

`Explorer` 会在把当前页当页面之前先识别并按策略作答，动作记进
`explorer.permission_events`（含属主、标题、点的是哪个按钮、判据）：

```python
ex = Explorer(driver, permission_policy='deny')   # 或读 OHAUTO_PERMISSION_POLICY
```

| 策略 | 行为 |
|---|---|
| `deny`（默认） | 点「禁止」——不在陌生设备上新增授权，但弹窗必须消掉 |
| `allow` | 点「允许」——需要走通相机/通讯录这类需授权的流程时用 |
| `record` | 只记录不动，保持「观察到」语义（收集证据时用） |

识别要**同时**满足两条：窗口属主是系统权限 UI 进程 + 存在可见可点的允许/禁止
按钮。只有前者会把权限管理器的普通页面当成弹窗；只有后者会把应用自己的
「允许/取消」对话框当成系统权限门。

## 六、常见操作要点

### 长稳测试

```bash
python -u examples/run_suite.py examples/cases/calculator.yaml \
    --repeat 220 --out tools/_out/stability_2h \
    --artifact-budget 60 --quiet > tools/_out/stability.log 2>&1
```

- `-u` 禁用输出缓冲，`--out` 每轮增量落盘 `suite_report_partial.json`，
  异常终止只损失正在执行的一轮；
- 验收：`python tools/stability_trend.py --report <report> --strict`，
  判据为退出码 0、全部步骤通过、半程中位漂移小于 ±15%
  （逐轮中位存在约 8% 的自然振荡，阈值必须高于该幅度）；
- 执行期间不关闭终端、不手动操作设备；息屏无影响（引擎每轮重设息屏超时）。

### 模拟器与折叠形态

模拟器实例由 `tools/emulator_cli.py` 管理（DevEco Emulator 26.0.0.400）：

- 启动必须由常驻后台任务持有 `Emulator.exe -start` 进程：
  等待超时会连同模拟器一起终止，分离启动会无报错退出；
- 折叠设备的镜像归 `phone` 类目（以实例 `imageDir` 字段为准，非 `deviceType`）；
  `install` 默认策略为最小版，已有实例应显式传 `--os-version`；
- 折叠态是否生效以 `hidumper -s RenderService -a screen` 的电源状态为准
  （内屏/外屏分辨率恒定，分辨率不变不代表切换失败）；
- `-foldedState` 返回成功后界面尚未完成重绘，立即采集有较大概率得到空树；
  采集必须重试并校验元素数下限（`crossform_run.py` 已内置）；
- 单实例约占 4 GB 内存，同时只运行一个；用完执行 `stop` 释放。

### 连接新设备

- 首次连接先执行 `python examples/dump_tree.py --bundle <包名>` 核对控件树
  字段覆盖度；发现未识别字段时补充 `ohauto/layout.py` 的 `ATTR_ALIASES`；
- `aa start` 会复用既有实例，启动前先 force-stop 保证回到入口页；
- 设备息屏时 `dumpLayout` 返回近乎空白的树且不报错，长用例使用
  `DeviceGuard.ensure_awake()` 维持亮屏；
- 设备 RTC 可能不准确，信号采集窗口一律锚定设备时钟域。
