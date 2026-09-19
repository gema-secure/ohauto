# UI 自动化能力层 — 技术方案与实现设计

> 目标：为 OpenHarmony 应用构建一套**多模态驱动的 UI 自动化能力层**，可作为 a24（多模态智能测试系统）或 a26（体验优化 Agent）的前置底座。
> 日期：2026-09-14
> 关键结论：**存在一条「无需测试 HAP」的纯宿主侧 hdc 通路**，这是本方案的技术基石。

---

## 一、最重要的技术发现

调研鸿蒙 UiTest 官方文档后，确认了一个**决定架构走向的事实**：

鸿蒙 UITest 提供**两套完全独立的通路**：

| | ArkTS API 通路 | **命令行通路（本项目采用）** |
|---|---|---|
| 依赖 | 需要测试 HAP + hypium + `uitest start-daemon` | **只需 hdc，无需写测试 HAP** |
| 运行位置 | 测试 HAP 内的 testRunner 进程 | 宿主 PC 通过 hdc 直连设备 |
| 能力 | 完整（含 waitForComponent 等） | **够用：截图 / 控件树 / 注入操作 / 录制** |
| 对我们的价值 | 重，需为每个被测应用打包 | **轻，通用，可脚本化，易集成 CI** |

**命令行通路的核心命令**（已实测确认参数格式）：

```bash
# ① 获取控件树（JSON，含控件类型/id/text/坐标层级）
hdc shell uitest dumpLayout -p /data/local/tmp/tree.json
hdc shell uitest dumpLayout -b <bundleName> -p /data/local/tmp/tree.json   # 指定包名
hdc shell uitest dumpLayout -i -p ...        # 不过滤不可见控件
hdc shell uitest dumpLayout -a -p ...        # 带 BackgroundColor/Content/FontSize 等属性

# ② 截图
hdc shell uitest screenCap -p /data/local/tmp/shot.png

# ③ 注入操作（全部基于坐标）
hdc shell uitest uiInput click 100 100
hdc shell uitest uiInput doubleClick 100 100
hdc shell uitest uiInput longClick 100 100
hdc shell uitest uiInput inputText 100 100 hello      # 输入文本
hdc shell uitest uiInput swipe 10 10 200 200 500      # 慢滑
hdc shell uitest uiInput fling 10 10 200 200 500      # 快滑
hdc shell uitest uiInput drag 10 10 100 100 500
hdc shell uitest uiInput dircFling 2 500              # 方向滑：0左 1右 2上 3下
hdc shell uitest uiInput keyEvent Home                # 返回桌面
hdc shell uitest uiInput keyEvent Back                # 返回上一页

# ④ 录制用户操作（产出 CSV，可直接复用为脚本素材）
hdc shell uitest uiRecord record     # Ctrl+C 结束，存 /data/local/tmp/record.csv
hdc shell uitest uiRecord read
```

**架构含义**：整个能力层可以**完全跑在宿主 PC 上**（Python/Node），设备端零部署。这意味着：
- 不需要为每个被测应用改一行代码
- 不需要打包测试 HAP
- 工具可跨应用通用，天然适合做「生态开发者工具」

---

## 二、关键技术难点与对策

### 难点 1：`uiInput` 全部基于坐标，但我们需要「按语义定位」

**问题**：`click 100 100` 是硬坐标，界面一变就失效。而我们希望表达的是"点击登录按钮"。

**对策**：建立 **控件树 → 坐标** 的解析层：

```
dumpLayout 产出 JSON（控件树）
   → 遍历树，按 (type / id / text / 层级路径) 匹配目标控件
   → 读取该控件的 bounds: {left, top, right, bottom}
   → 计算中心点 center = ((left+right)/2, (top+bottom)/2)
   → 生成 hdc shell uitest uiInput click <center_x> <center_y>
```

**这一步是整个能力层的地基**。控件树 JSON 的结构（基于官方录制数据推断）：

```jsonc
{
  "attributes": {
    "type": "Button",
    "id": "btn_login",
    "text": "登录",
    "bounds": "{\"bottom\":361,\"left\":37,\"right\":118,\"top\":280}",
    "clickable": "true",
    "visible": "true",
    "enabled": "true"
  },
  "children": [ /* 嵌套子控件 */ ]
}
```

**多属性匹配器设计**（借鉴 UiTest 的 ON 对象语义）：

```python
ON.type("Button").id("btn_login").text("登录").within(scroll_container)
```

### 难点 2：坐标体系不一致

**问题**：控件树 bounds 是 px，屏幕密度不同，且多屏/折叠屏有 displayId 概念。

**对策**：
- 用 `hdc shell uitest uiInput` 时**直接使用控件树里的 bounds 坐标**，两者同一坐标系，不要自己换算
- 多屏场景需带 `-d <displayId>`，通过 `hidumper` 获取应用窗口的 DisplayId
- 折叠屏展开/折叠时视口变化，**必须在操作前重新 dumpLayout**，不能缓存

### 难点 3：动态内容导致的时序问题

**问题**：点击后页面异步加载，立刻 dumpLayout 会拿到旧树或半截树。

**对策**：实现 **`waitFor` 轮询机制**（对应 ArkTS 侧的 `waitForComponent`）：

```python
def wait_for(matcher, timeout_ms=5000, interval_ms=300):
    """轮询直到匹配器命中或超时"""
    deadline = now() + timeout_ms
    while now() < deadline:
        tree = dump_layout()
        hit = find(tree, matcher)
        if hit: return hit
        sleep(interval_ms)
    raise TimeoutError(f"等待超时: {matcher}")
```

同时配合 `waitForIdle` 的等价实现：连续两次 dumpLayout 结果一致 → 认为界面稳定。**这是提升脚本成功率的关键，不能省。**

### 难点 4：控件树无法覆盖的场景

**问题**：Canvas 绘制、图标按钮、无 id 无 text 的自定义控件，控件树里是空的。

**对策**：这正是**多模态模型介入的地方**，形成双通道定位：

| 通道 | 适用 | 实现 |
|---|---|---|
| **控件树通道（主）** | 标准控件、有 id/text | 解析 dumpLayout JSON |
| **视觉通道（补）** | 图标按钮、Canvas、无属性控件 | 截图 → 多模态模型输出目标 bbox → 算中心点 |

**融合策略**：先用控件树；命中失败或控件无标识时，降级到视觉通道；两者都给出的候选做一致性校验（IoU 重叠则确认）。

---

## 三、系统架构

```
┌───────────────────────────────────────────────────────────────┐
│  L4  应用层（面向命题）                                        │
│   a24: 脚本生成器 / 自动探索 / 用例沉淀                          │
│   a26: 性能场景驱动 / 冷启动触发 / 滑动压测                       │
├───────────────────────────────────────────────────────────────┤
│  L3  语义层  ——  把「意图」翻译成「操作序列」                     │
│   Action DSL:  tap(matcher) / input(matcher, text)              │
│                swipe(dir, dist) / back() / waitFor(matcher)     │
│   多模态定位器:  截图 + 控件树 → LLM → 目标 bbox                  │
├───────────────────────────────────────────────────────────────┤
│  L2  定位层  ——  控件树解析与匹配                                 │
│   LayoutParser:  JSON → 控件树对象                              │
│   Matcher:       多属性匹配 (type/id/text/within/层级)            │
│   Resolver:      控件 → 中心坐标                                 │
├───────────────────────────────────────────────────────────────┤
│  L1  执行层  ——  hdc 通路的薄封装（零设备侧部署）                  │
│   dumpLayout() / screenCap() / uiInput.*() / uiRecord()        │
│   连接管理 / 重试 / 超时 / 截图拉取到本地                         │
├───────────────────────────────────────────────────────────────┤
│  L0  hdc  ←→  OpenHarmony 真机                                 │
└───────────────────────────────────────────────────────────────┘
```

**分层原则**：L1 稳定不变（命令格式固定），L2 是纯逻辑（易测），L3 是可替换的智能层（换模型不影响下面），L4 随命题变化。**这个分层让"打 a24 还是 a26"变成一个可以后置的决定。**

---

## 四、目录结构

```
ohauto/
├── ohauto/
│   ├── __init__.py
│   ├── hdc.py            # L1: hdc 命令封装（连接/超时/重试/文件拉取）
│   ├── layout.py         # L2: 控件树 JSON 解析 → LayoutNode 树
│   ├── matcher.py        # L2: ON 式多属性匹配器
│   ├── driver.py         # L2+L3: Driver 门面（tap/input/swipe/back/waitFor）
│   ├── vision.py         # L3: 多模态定位（可插拔 Provider）
│   ├── action.py         # L3: Action DSL + 操作序列执行器
│   ├── explorer.py       # L4: 自动探索（页面状态图 + 路径规划）
│   └── report.py         # L4: 报告生成（JSON/MD/HTML）
├── examples/
│   ├── smoke_test.py     # 最小闭环：拉起→定位→点击→断言
│   └── explore_demo.py   # 自动探索示例
├── tests/                # 单元测试（matcher/layout 是纯函数，易测）
├── docs/
│   └── architecture.md
├── requirements.txt
└── README.md
```

---

## 五、核心接口设计

### 5.1 匹配器（对齐 UiTest 的 ON 语义）

```python
from ohauto import ON

ON.text("登录")                        # 按文本
ON.id("btn_login")                     # 按 id
ON.type("Button")                      # 按控件类型
ON.text("提交").within(ON.type("Scroll"))  # 相对位置：在滚动容器内找
ON.type("Image").clickable(True)        # 组合属性
ON.text_contains("确定")                # 模糊文本
```

### 5.2 Driver 门面

```python
driver = Driver(bundle="com.example.app")

driver.start()                          # aa start -b <bundle>
node = driver.wait_for(ON.text("登录"), timeout=5000)
driver.tap(node)                        # 自动算中心坐标 + 注入 click
driver.input(ON.id("username"), "alice")
driver.swipe("up", distance=0.6)        # 按屏幕比例滑动
driver.back()
driver.screenshot("step1.png")          # 拉回本地
driver.assert_exists(ON.text("首页"))    # 断言
```

### 5.3 Action DSL（脚本可序列化 = 可生成 = 可沉淀）

```yaml
# 生成的脚本长这样，既可直接执行，也是"用例沉淀"的载体
steps:
  - tap:    { text: "登录" }
  - input:  { id: "username", value: "alice" }
  - input:  { id: "password", value: "******" }
  - tap:    { id: "btn_submit" }
  - waitFor: { text: "首页", timeout: 8000 }
  - assert: { exists: { text: "我的" } }
```

**这个 YAML 是关键设计**：它是 L3 的输出、L4 的输入，也是最终提交给评委的"可复用测试用例"。**机器可以生成它，人可以审阅它，回归测试可以重放它**——一举解决命题里的"脚本生成"和"用例沉淀"两个要求。

---

## 六、实施路线（按优先级）

| 阶段 | 交付 | 验收标准 | 依赖 |
|---|---|---|---|
| **P0** | hdc 封装 + 控件树解析 + 匹配器 | 能 dumpLayout 并把树解析成对象，匹配到任意控件 | 真机 |
| **P0** | Driver 最小闭环 | 打通`拉起→定位→点击→断言→截图` | P0 |
| **P1** | waitFor / waitForIdle | 连续 20 次操作成功率 ≥ 90% | P0 |
| **P1** | Action DSL + 执行器 | YAML 脚本可执行、可序列化 | P0 |
| **P2** | 多模态定位 | 无标识图标按钮能被点中 | P1 + 模型接入 |
| **P2** | 报告生成 | 输出 JSON/MD/HTML，含每步截图 | P1 |
| **P3** | 自动探索 + 页面状态图 | 能自主发现 3 个页面的跳转关系 | P2 |
| **P3** | 脚本自动生成 | 从探索轨迹产出可重放 YAML | P3 |

**建议**：**P0 + P1 先做透**。这两阶段不依赖任何模型，做完就能显著提升团队对真机的掌控力，且无论最终打 a24 还是 a26 都用得上。P2/P3 才是"创新性"部分。

---

## 七、风险登记

| 风险 | 影响 | 对策 |
|---|---|---|
| 真机 OS 版本过旧，`uitest` 命令不支持 | P0 通路不可用 | 先跑 `hdc shell uitest --version` 验证；旧版本降级到 ArkTS API 通路 |
| 部分应用防自动化（禁截图/禁注入） | 无法驱动 | 优先选测试目标为**自研或开源示例应用**，避开有防护的商用 App |
| 控件树过大导致解析慢 | 影响性能 | 流式解析 + 只保留可交互节点 + 缓存子树 |
| 多模态模型幻觉定位 | 点错位置 | 视觉结果必须与控件树交叉校验；无把握时降级为"报告可疑"而非盲目点 |
| 坐标系随折叠/旋转变化 | 操作偏移 | 每次操作前重新 dumpLayout，绝不缓存坐标 |
| **拿不到真机联调** | 无法验证 | **这是最大风险。必须先确认设备可达，否则一切停在纸面** |

---

## 八、为什么这套设计对命题有价值

### 对 a24（多模态智能测试系统）

命题要求里几乎每一条都能对应上：

| 命题要求 | 本方案对应 |
|---|---|
| 采集截图并结合布局信息识别可交互元素 | L1 dumpLayout + screenCap，L2 解析 |
| 支持 3 类控件识别与脚本生成 | Action DSL 覆盖 tap/input/swipe/back |
| 生成可复用脚本，含明确步骤与断言 | YAML DSL，含 `assert` 步骤 |
| 支持 2 类测试场景 | 探索性（explorer）+ 回归（YAML 重放） |
| 闭环：采集→理解→规划→生成→执行→分析→沉淀 | 正好是 L1→L4 的完整链路 |
| 挑战：自然语言生成复现脚本 | YAML DSL 作为中间表示，天然可被 LLM 填充 |

### 对 a26（体验优化 Agent）

a26 也需要"输入包名，执行测试或读取测试数据"——**驱动应用触发性能场景**正是这套能力层能提供的：
```python
# a26 用法示意
driver.start()
driver.tap(ON.text("商品列表"))
for _ in range(20):
    driver.swipe("up", 0.8)          # 持续滑动压测，同时采集帧率
driver.mark("scroll_stress_done")
```

---

## 九、下一步需要你确认的事项

要把纸面变成可运行的代码，我需要知道：

1. **真机能不能连上**——`hdc list targets` 能否看到设备？OS 版本是多少？（决定 P0 走 CLI 通路还是 ArkTS 通路）
2. **有没有可用的被测应用**——自研 Demo？还是要选一个开源鸿蒙示例应用？
3. **宿主语言偏好**——Python（我建议，生态好、开发快）还是 Node/TypeScript？
4. **模型接入方式**——云端 API（哪个）、本地模型、还是先不接（P0/P1 不依赖模型）？

**只要第 1 条确认，我就可以立刻开始写 P0 的代码并在你们的真机上验证。**
