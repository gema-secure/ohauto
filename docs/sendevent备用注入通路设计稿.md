# sendevent 备用注入通路 —— 设计稿（C8）

> 状态：**已实现，真机核对完成**（DAYU200 / OpenHarmony，屏幕 720x1280，
> `2f011130375330303010b120b3832c00`）。实现落在 `ohauto/hdc.py`
> （`InputBackend` 契约 + 三个 backend + `detect_backend`）、`ohauto/sim.py`
> （`FakeHdc` 同构）、`ohauto/doctor.py`（逐条列出可用性）、
> `ohauto/report.py`（披露实际通路）；钉子 `tests/test_c_input_backend.py`。
> 问题（C8）：技术路线**单点依赖 uitest** —— 设备没有 `uitest` 命令行通路时，
> 引擎从「可用」直接掉到「零」，中间没有缓冲档。

## 一、现状：写操作全挂在 uitest 上

所有「写」动作都经 `Hdc._ui_input()` 拼出 `uitest uiInput ...` 下发
（`ohauto/hdc.py`）：

| 方法 | 下发的子命令 |
|---|---|
| `click` / `double_click` / `long_click` | `click` / `doubleClick` / `longClick` |
| `swipe` / `fling` / `drag` / `dirc_fling` | `swipe` / `fling` / `drag` / `dircFling` |
| `input_text` | `inputText` |
| `key_event` / `back` / `home` | `keyEvent` |

读操作（`dump_layout` / `screen_cap`）同样走 `uitest` 子命令。
`doctor.py` 第 6 关把「uitest 命令行通路」判为 BAD 时**直接 return**，
后续检查项一律不再执行 —— 即「没有 uitest」被当成「整台设备不可用」。

对照组：`start_ability` / `force_stop` 走 `aa` 命令，**不依赖 uitest**；
`hidumper`（屏幕尺寸、2C 性能采集）也独立。所以「没有 uitest」时，
设备其实仍有一部分能力可用，只是现在这层能力没有被识别出来。

## 二、备选通路盘点（能力矩阵）

⚠️ 下表已在真机逐格核对（DAYU200）。结论：`uitest` ✔（5.0.1.2）、
`uinput` ✔（`uinput --help` 有 usage，命令形态见下）、`sendevent` ✘
（该机**没有 `getevent`**，拿不到 evdev 轴范围 → 判不可用，不硬编码范围）。

| 通路 | 能力 | 依赖 | 零部署 | 风险 |
|---|---|---|---|---|
| `uitest`（现状） | 读 + 写全套 | 设备带 uitest | 是 | 单点 |
| `uinput` | 写：点击 / 滑动 / 长按 / 按键 / 文本 | `/system/bin/uinput` 存在且可执行 | 是 | 不同版本参数形态可能不同 |
| `sendevent` | 写：最全（直接写 evdev 事件） | `/dev/input/eventN` 可写 + 需 `getevent` 读轴范围 | 是 | 坐标是 evdev 绝对值，需先校准轴范围 |
| `aa`（已在用） | 仅启动 / 停止 ability | 设备带 aa | 是 | 无 |
| `hidumper`（已在用） | 只读：屏幕尺寸 / 性能 | 设备带 hidumper | 是 | 无 |

### 2.1 `uinput -T --touch` 命令形态（真机实测）

`-T` 的**文档命令只有** `-d` / `-u` / `-i` / `-m` / `-c`，没有 `-g`：

| 动作 | 命令 | 真机结果 |
|---|---|---|
| 点击 | `uinput -T -c x y` | ✔（-c 的 interval 须 <450ms，不带即默认） |
| 滑动 | `uinput -T -m fx fy tx ty` | ✔ 下拉通知栏 199→72 节点、上滑 72→199 |
| 长按 | `uinput -T -m x y x y -k <ms>` | ✔ 桌面图标长按弹出「打开/服务卡片/卡片中心/卸载」 |
| 按键 | `uinput -K -l <OHOS KeyCode> <ms>` | ✔（1=Home 2=Back；`-d`/`-u` 分两次下发**不生效**） |
| 文本 | `uinput -K -t <text>` | ✔（`-t` 不可与其它命令同用） |

结论：备用通路的目标是**把「写」补齐到够用**（点击 / 滑动 / 长按 / 文本 / 返回），
而不是复刻 uitest 的全部能力。

## 三、设计

### 3.1 抽象落点：`InputBackend`

新增一个窄协议，只声明上层真正会调的写动作：

```
class InputBackend(Protocol):
    name: str
    def click(self, x: int, y: int) -> None: ...
    def double_click(self, x: int, y: int) -> None: ...
    def long_click(self, x: int, y: int) -> None: ...
    def swipe(self, fx, fy, tx, ty, velocity: int) -> None: ...
    def input_text(self, x: int, y: int, text: str) -> None: ...
    def key_event(self, *keys) -> None: ...
```

`Hdc` 持有一个 backend 实例；`_ui_input(...)` 改为经 backend 派发。
契约纪律与 `HdcLike` 同法（`ohauto/hdc.py`）：**只声明实际消费的方法**，
删除契约内的方法必须让类型检查失败；`runtime_checkable` 的两条限制同样适用。

### 3.2 探测与降级

- 探测顺序：`uitest` → `uinput` → `sendevent`；**首个可用者胜出**。
- 探测判据一律看**返回码 + 实际效果**，不看「有没有输出」——
  与 `Hdc.uitest_version()` 同一条纪律。
- 探测结果写入批次元数据（与 2C 的 `hdc -v` 版本留痕同法），
  报告里显式写出「本次用的是哪个 backend」。
- `doctor.py` 第 6 关由「BAD 即 return」改为「BAD → 继续探测备用通路」，
  并把每个 backend 的可用性逐条列出。

### 3.3 坐标与按键的换算口径

- `uinput`：像素坐标，与 uitest 一致，直接透传。
- `sendevent`：`ABS_MT_POSITION_X/Y` 是 evdev 绝对量，必须先读
  `getevent -p` 拿到该设备的轴范围，再按屏幕尺寸做线性映射；
  **映射范围从设备读，不硬编码**。
- 按键：uitest 的 `Back` / `Home` 不能硬映射成某个键码 —— 键码来自设备的
  keylayout，需先读再查表。查不到就如实报「该键在备用通路上不可用」。

### 3.4 能力降级矩阵（没有备用通路时的最小可用集）

| 能力 | 有 uitest | 仅 uinput | 仅 sendevent | 都无 |
|---|---|---|---|---|
| 启动 / 停止应用（aa） | ✔ | ✔ | ✔ | ✔ |
| 性能采集 / 屏幕尺寸（hidumper） | ✔ | ✔ | ✔ | ✔ |
| 点击 / 滑动 / 返回 | ✔ | ✔ | ✔ | ✘ |
| 读控件树 / 截图（uitest） | ✔ | ✘ | ✘ | ✘ |

最后一列（三者皆无）就是现在的「零」档：只剩 aa + hidumper，
即**能做只读观测与应用拉起，不能做任何交互**。这时引擎要如实置位为
「不可交互」并停止探索 / 执行，而不是假装在跑。

### 3.5 诚实纪律（本设计的红线）

探测不到就必须**如实报不可用**。禁止出现「以为降级成功、其实没注入」
的假绿 —— 这与 `sim` / `vision` 里已有的两条纪律是同一类
（「模拟器不保真 = 测试全绿反而危险」）。

## 四、边界：不做的事

- 不实现 evdev 的手势高级特性（多指、压力、轨迹插值）。
- 不改 L3 / L4 —— backend 对 `Driver` 完全透明，上层一行不动。
- 不在 `sendevent` 上做「文本输入」（中文/emoji 无法用 evdev 直接表达）；
  备用通路下 `input` 步骤应显式失败并给替代建议，而不是静默跳过。

## 五、验收标准（实现时照此收口）

- [x] `doctor.py` 能区分三种 backend，并在报告里**逐条**列出可用性
      —— 第 6 关 uitest 判 BAD 时不再 `return`，逐条打印
      `uitest/uinput/sendevent` 的可用性与说明；三者皆无则报「不可交互」档。
- [x] `FakeHdc` 增 sendevent / uinput 分支，与 uitest 分支**同构**
      （「模拟器不保真 = 测试全绿反而危险」）—— `_apply_alt_input` 认
      `-T -c` / `-T -m` / `-K -t` / `-K -l` / `sendevent`；`-d`/`-u` 照真机落空。
- [x] 契约测试钉住 `InputBackend` 协议（删方法即失败）—— `tests/test_c_input_backend.py`
      `TestInputBackendContract`（含 `_Half` 反例）。
- [x] 真机：至少在一台**强制禁用 uitest** 的设备上跑通「点击 + 滑动 + 返回」三步
      —— `detect_backend(force='uinput')` 下：滑动（下拉通知栏 199→72、上滑 72→199）、
      点击（备忘录列表 111→217，键盘弹出）、返回（217→111）三步均有节点/截图证据。
- [x] 报告里出现本次实际使用的 backend 名称；三者皆无时显式置位「不可交互」
      —— `report.collect` 落 `input_backend`，Markdown 出「输入通路」行，
      `none` 显示为「none（不可交互）」。

真机诚实记录：本机（DAYU200）**没有 `getevent`** → `sendevent` 判**不可用**
（不是猜一组轴范围）；`sendevent` 的 evdev 代码路径由离线测试
（`GETEVENT` 夹具）覆盖，未在真机取得实际注入证据。本机 `uitest` 与 `uinput`
均可用 —— 因此「强制禁用 uitest」以 `force='uinput'` 模拟，非物理移除。