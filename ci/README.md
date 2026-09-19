# CI 质量门禁

对应规划「工程规范与 CI 质量门禁」高地。

## 一条命令跑全部检查

```bash
cd ohauto
python ci/quality_gate.py
```

不需要真机、不需要模拟器、不需要联网。

| 选项 | 用途 |
|---|---|
| `--fast` | 跳过覆盖率（本地快速自测，约 100s） |
| `--only style` | 只跑某一关 |
| `--json out.json` | 输出机器可读结果（供平台/看板消费） |

**退出码**：`0` = 全绿；`1` = 有关卡失败。可直接作为 CI 的阻断条件。

---

## 四道关卡

| # | 关卡 | 实现 | 阻断条件 |
|---|---|---|---|
| 1 | **静态检查** | `ci/static_check.py`（**零依赖**，用 `ast` 实现） | 有 error 级问题 |
| 2 | **单元测试** | `unittest discover`（589 项） | 任一失败 |
| 3 | **覆盖率** | `coverage` + `.coveragerc` 的 `fail_under` | 低于 **70%** |
| 4 | **离线端到端** | `examples/offline_demo.py`（模拟设备） | 非零退出 |

### 为什么静态检查不引 flake8 / mypy

项目红线约束是**「只用标准库和项目已有模块，不引入新依赖」**
（`requirements.txt` 里只有 `pyyaml`）。

为 lint 引入一堆重依赖既违背约束，也会让「任何人 clone 后能跑起来」变难。
所以用 `ast` 自己实现规划要求的检查项：

| 规划要求 | 实现方式 |
|---|---|
| 类型注解全覆盖 | 公共函数必须有参数与返回值注解（**warning**，见下） |
| 统一格式化 | tab 缩进 / 行尾空白 / 文件末尾换行 / 行长度 ≤100 |
| 命名规范 | 函数 snake_case、类 PascalCase（`_` 前缀与 `visit_Xxx` 豁免） |
| 模块边界清晰 | **生产代码不得反向 import 测试/示例/CI**（`B401`） |
| 危险模式 | 裸 `except:` / `eval` / `exec` / 可变默认参数 |

**检查范围**（30 个文件）：`ohauto/` + `tools/` + `ci/` + `examples/` + `doctor.py`。
`tests/` 有意排除（测试代码不必强制类型注解与命名细节）。

> ⚠️ 早期版本漏掉了 `examples/` 的 6 个文件 —— 而它们是**给人看的示例**，
> 是文档的一部分，质量要求只会更高。已补入。

---

## ★ 排查中修掉的坑（都值得记）

质量门禁 初版「全绿」之后做了一次专项排查，发现的每一处都是**真实的**：

### 1. 时间炸弹：CI 上午绿、晚上红

`test_signals.py` 里 3 个测试用 `lookback_s=3 * 86400`（**相对现在**往前推 3 天）
去匹配一个**文件名时间戳写死为 `2026-09-16 15:14:46`** 的真机样本：

```
样本时间戳  : 2026-09-16 15:14:46
3 天窗口起点: 2026-09-16 21:59:28   ← 随时间推移越过了样本时刻
```

测试写于 9/17（当时窗口能覆盖），**到 9/19 21:59 窗口起点越过样本 → 3 个测试失败**。
上午 15:05 跑出的「全绿」是**时间巧合**，晚上就红了。

**修法**：改用 `since=CRASH_SAMPLE_SINCE`（**绝对时间点**，锚定到样本时刻），
窗口永远覆盖样本，与当前时间无关。

> 💡 **教训**：`lookback_s`（相对）和固定时间戳样本是危险组合。
> 凡是拿真机样本做端到端测试，一律用**绝对时间**锚定。
> 这类 bug 在 CI 里最难查 —— 今天绿明天红，而且改代码改不出来。

### 2. `--json` 不建父目录 → 崩溃掩盖结果

`--json _out/x.json` 在 `_out/` 不存在时抛 `FileNotFoundError`。
而它发生在**所有检查跑完之后** —— 明明检查完了，却因为写文件崩掉，
**用户看到 traceback，根本不知道检查过没过**。

**修法**：建父目录；且写入失败只报警告，**不改退出码**
（退出码必须反映「检查是否通过」，而不是「附加产物能不能写出去」）。

### 3. 死代码 + 误导

`quality_gate.py` 里有两个 `parse` 出来的字段**从未被读取**：
`has_error`（用了 `--quiet` 后恒为 False，却看起来像在检查错误）、
`failed_marks`。已删除。

### 4. e2e 判据太弱

原本只看 `offline_demo.py` 的返回码 —— 脚本内部 `try/except` 吞掉异常后
仍以 0 退出，就会**漏检**。已加强：退出码 0 但输出里有 `Traceback` 也算失败。

> 注意**不能**用「失败」这类中文词做判据 —— 演示脚本自己会输出
> 「失败 0 条」的正常文案，会造成假阳性。

### 5. workflow 与 `quality_gate.py` 逻辑重复

初版把四个关卡在 `.github/workflows/quality-gate.yml` 里又抄了一遍 ——
两套逻辑必然漂移（改了一处忘另一处）。
已改为 workflow **只调 `ci/quality_gate.py`**，自己只负责「跑」和「归档产物」。

### 6. 文档声称了没实现的东西

`static_check.py` 的说明里写着「禁止跨层 import」，**但根本没实现**。
与其删掉声称，不如实现一个**零误报**的版本：`B401` ——
生产代码不得反向 `import tests/ / examples/ / ci/`。
（更复杂的层次规则容易误伤，没做。）

---

## 怎么自己复现这轮排查

**核心手段：模拟一次「干净 clone」。** 这是排查 CI 最有效的一招：

```bash
# 复制源码到临时目录，排除所有产物/缓存/本地状态
# skip = {'__pycache__', '_out', '.git', 'node_modules', '.coverage'}
# 然后在副本里跑 CI
python ci/quality_gate.py
```

它能暴露三类只在「别人机器上」才出现的问题：

| 症状 | 真实原因 |
|---|---|
| 本地全过、副本失败 | 测试依赖 `_out/` 或其它本地产物 |
| `FileNotFoundError` | 某处假设目录已存在 |
| 路径全错 | 配置假设的「仓库根」与实际结构不符 |

> 本轮就是靠它一次挖出两个问题（时间炸弹、`--json` 目录），
> 而**在本地工作目录里跑了三次都是全绿**。

**当前状态**：干净环境完整 CI **全绿**（97s，589 项测试通过）。

---

## 覆盖率基线（实测，不是拍脑袋定的）

2026-09-19 全量 589 项测试后的真实数字：

```
核心包 ohauto/  合计 85%（4038 语句 / 611 未覆盖）
```

| 模块 | 覆盖率 | | 模块 | 覆盖率 |
|---|---:|---|---|---:|
| `__init__.py` | 100% | | `runner.py` | 87% |
| `crossform.py` | 97% | | `explorer.py` | 83% |
| `crossform_report.py` | 94% | | `action.py` | 81% |
| `matcher.py` | 94% | | `driver.py` | 76% |
| `devices.py` | 93% | | `vision.py` | **61%** |
| `signals.py` | 93% | | `hdc.py` | **36%** |
| `report.py` | 91% | | `sim.py` | 90% |
| `layout.py` | 88% | | | |

**规划要求 ≥70%，当前 85%，留了 15 个百分点余量。**

### 两个低点是合理的，不是缺陷

- **`hdc.py` 36%** —— 大部分代码是真实 `hdc` subprocess 调用，
  离线测不到。已用 `tests/test_hdc_pull.py`（11 项）覆盖了能离线测的部分
- **`vision.py` 61%** —— 视觉通道（A 负责），真实 Provider 还没接入

### 为什么只统计 `ohauto/` 包

`tools/` 下多是 CLI 包装（要真机/模拟器才能跑），算进硬门禁会让数字失去意义。
它们的覆盖率单独看，不阻断合并。

---

## 静态检查的 85 个 warning 怎么处理

当前 `error` 为 **0**（不阻断），`warning` **85** 个，分布：

| 代码 | 数量 | 含义 |
|---|---:|---|
| `ANN001` | 76 | 缺少类型注解 |
| `E501` | 7 | 行超长 |
| `S308` | 1 | `__import__`（确认参数非外部输入） |
| `W291` | 1 | 行尾空白 |

**为什么不升级成 error**：规划要求「类型注解全覆盖」，但一次性补 76 处
注解会让当前所有分支的 CI 立刻变红 —— 那会让人**关掉门禁**，
反而失去意义。

**处置方式**：把 warning 数量当作**不许变差的基线**。
新增代码若推高数量，review 时要求补齐。逐步收敛。

---

## 在托管平台上启用

`.github/workflows/quality-gate.yml` 已备好（GitHub Actions 格式，多数平台兼容）。

⚠️ **本项目当前还不是 git 仓库**（`git rev-parse` 报 not a repository），
所以这份配置暂时不会生效。本地用 `python ci/quality_gate.py` 跑的是**同一套检查**。

启用步骤：

```bash
cd guochuang-2026/ohauto
git init
git add .
git commit -m "init"
git remote add origin <仓库地址>
git push -u origin main
```

> 用 AtomGit 或其它平台时：把 `ci.yml` 的 `steps` 原样搬过去即可 ——
> 里面只有 `pip install` 和 `python ci/quality_gate.py`，没有平台特定语法。

---

## 加新检查怎么做

在 `ci/quality_gate.py` 的 `build_stages()` 里加一个 `Stage`：

```python
stages.append(Stage(
    'mychk', '我的新检查',
    [PY, 'ci/my_check.py'],
    parse=my_parser,        # 可选：从输出里提取要展示的指标
    timeout=300,
))
```

`Stage.run()` 会捕获返回码，非 0 即视为失败并进入汇总。

---

## 交付证据（评审可直接引用）

| 证据 | 怎么拿 |
|---|---|
| 全绿输出 | `python ci/quality_gate.py` 的截图 |
| 覆盖率报告 | `python -m coverage html` → `_out/coverage_html/index.html` |
| 机器可读结果 | `python ci/quality_gate.py --json _out/ci_result.json` |
| CI 徽章 | 仓库推到平台后，从 Actions 页面取 badge 链接 |
