# 真机采集 · 2026-09-19

> 设备：润开鸿 DAYU200（`2f01****c00（设备序列号，已脱敏）`）
> 系统：OpenHarmony 5.0.3.135 ｜ 屏幕：720×1280 @69Hz ｜ uitest：5.0.1.2
> 采集方式：`hdc shell uitest dumpLayout` + `screenCap`（零部署通路）
> 采集工具：`ohauto/tools/capture_app.py`（本次新建，内化全部已知坑）

---

## 一、采集清单（7 个样本 / 800 节点）

| 样本 | 来源 | 节点 | 可交互 | id | text | 截图 |
|---|---|---:|---:|---:|---:|---|
| `app_settings.json/.png` | `com.ohos.settings` | 183 | 28 | **0** | 18 | 69K |
| `app_note.json/.png` | `com.ohos.note` | 151 | 12 | 13 | 13 | 62K |
| `sample_calc.json/.png` | `ohos.samples.distributedcalc` | 144 | 20 | 21 | — | 55K |
| `s_etsclock.json/.png` | ★ **USB 对话框**（误采，有用） | 103 | 5 | 2 | 7 | 47K |
| `sample_music.json/.png` | `ohos.samples.distributedmusicplayer` | 93 | 5 | 4 | 8 | 328K |
| `etsclock.json/.png` | `ohos.samples.etsclock` | 77 | **0** | 0 | 4 | 57K |
| `app_photos.json/.png` | `com.ohos.photos` | 49 | 3 | 5 | 4 | 80K |
| **合计** | | **800** | **73** | **45** | | |

截图全部为有效的 **720×1280** PNG（已逐个校验文件头）。
每个样本另有一份 `<名字>.meta.json`（采集时间、成因、统计），由工具自动生成。

---

## 二、★★ 最重要的发现：**id 覆盖率只有 5.6%**

```
800 个节点 → 只有 45 个带 id  →  5.6%
可交互元素只有 73 个          →  9.1%
```

**这不是采集问题，是真机的真实情况**：OpenHarmony 应用的控件树
**普遍不设置 id**。其中 `com.ohos.settings`（183 节点）和
`ohos.samples.etsclock`（77 节点）**一个 id 都没有**。

### 这个数字对项目的直接影响

| 谁 | 影响 |
|---|---|
| **A（定位与感知）** | 定位降级链里「**id 精确匹配**」这一级**大部分时候用不上**，直接落到 text / 层级路径。而这正是 A3「定位器自愈」要解决的问题域 |
| **A** | KPI「标准控件定位准确率 ≥95%（手工标注 **100 个控件**）」—— 按 5.6% 覆盖率，要凑 100 个**有 id 的**控件需采约 **1800 个节点**。**这条 KPI 的样本门槛比想象中高，要早做打算** |
| **A** | 反过来，「无标识控件定位成功率 ≥80%」才是**主流场景**，不是边缘场景 |
| **B（探索与生成）** | 探索覆盖率的分母是 **73**（可交互），不是 800（全部节点）。用错分母覆盖率会严重失真 |
| **全员** | ★ **「视觉通道」的必要性被这组数据坐实了** —— 当 94% 的控件没有 id、91% 的节点不可交互时，纯控件树定位的天花板很低 |

> 💡 命题第 1 条要求「采集**截图**并结合布局信息识别可交互元素」。
> 这份数据说明了**为什么必须是双通道**：布局信息本身信息量不足。

---

## 三、其他三个发现

### 发现 2：etsclock 是纯 Canvas 绘制，**可交互元素为 0**

```
节点 77 ｜ 可交互 0 ｜ id 0 ｜ text 4（全是状态栏）
类型：Row 42 / Stack 6 / Flex 6 / Text 5 / root 4 / Image 4 /
      Column 4 / GridItem 3 / Canvas 1
```

表盘、指针、数字时间全是 Canvas 画的 —— **控件树完全无法定位任何东西**。

**→ A 的视觉通道最理想的测试场景，也是「为什么需要双通道」的正面证据。**

> ⚠️ 选靶子注意：要验证视觉通道就**必须找这种界面**，
> 纯控件树应用测不出视觉通道的价值。

### 发现 3：插 USB 会弹「USB 连接方式」对话框，**且反复弹**

- 盖住被测应用 → 采集拿到 103 节点的对话框而不是应用（见 `s_etsclock.*`）
- **点掉「确定」之后还会再弹**
- 规避：点掉后**立即**采集，不要等待（工具已内化）
- **这是 B2 任务卡里「弹窗内可交互控件提为最高优先级」的真实样本**

### 发现 4：息屏锁屏 → 控件树只剩 377 字节

锁屏界面**不进无障碍树**。必须三步（工具已内化）：

```bash
hdc shell power-shell wakeup
hdc shell power-shell timeout -o 1800000                      # ★ 钉住
hdc shell uitest uiInput swipe 360 1100 360 300 800           # 上滑解锁
```

不做会**静默出错**：采集成功、不报错、数据是空的。

---

## 四、★ 采集过程中发现并修复的两个坑

### 坑 A：`is_interactive` 漏括号 → 统计静默失真

`LayoutNode.is_interactive` 是**方法**（不是 property），而同类里
`clickable` / `scrollable` / `visible` / `path` 都是 property。
**漏括号 → 拿到方法对象 → 恒为真值 → 静默得出「所有节点都可交互」。**

第一次汇总就踩了：报「169 个节点全部可交互」，而原始 JSON 里只有 11 个
`clickable=true`。**是交叉核对原始数据才发现的** —— 光看统计数字看不出来。

**处置**：

1. 修掉工具里的漏括号（`capture_app.py`）
2. 在 `layout.py` 的 `is_interactive` 上加醒目警告注释
3. 新增 `tests/test_core.py::TestIsInteractiveCallTrap`（7 项），
   把陷阱机制**钉成可执行文档**
4. ⚠️ **未改接口**：把它改成 property 会破坏 A/B 的调用点，属对外接口变更，
   需先通知再动。**这一条留给 A/B 决定。**

> 💡 通用教训：**统计数字必须与原始数据交叉核对。**
> 一个恒真的布尔表达式会让所有统计同时失真，且完全静默。

### 坑 B：`Hdc.pull` 导致**截图被静默损坏**（更危险的一个）

第一次批量采集后，7 个样本里 **5 张截图只有几十字节**：

```
app_note.png        87 B   ← 正常应 60KB+
sample_calc.png     10 B
app_settings.png   105 B
```

**但采集过程不报任何错**，`meta.json` 里也写着「采集干净」。
如果没人去校验文件，这些坏图就会被当成素材交出去。

**根因是三个问题叠加**（都在 `Hdc.pull` 里）：

| # | 问题 | 后果 |
|---|---|---|
| 1 | **正斜杠盘符路径被当相对路径** —— `D:/foo/x.png` 被 hdc 拼到 cwd 后面 | `file recv` 失败 |
| 2 | **`file recv` 失败时返回码仍是 0** | 判据失效，误判为成功 |
| 3 | **`shell cat` 兜底对二进制做 CRLF 转换** —— `\x89PNG\r\n` 变成 `\x89PNG\r\r\n` | **产出损坏但不报错的文件** |

**修复**（`ohauto/ohauto/hdc.py`）：

1. 用 `os.path.abspath()` 规范化路径再传给 hdc
   （原实现只对 `makedirs` 规范化了，**传给 hdc 的却是原始路径**）
2. **传输前先删残留文件** —— 否则「文件存在」可能来自上次运行，掩盖本次失败
3. 判据改为「文件存在**且有内容**」，不再只看返回码
4. 新增 `binary` 参数：`binary=True` 时**禁用 cat 兜底**，
   直接报错 —— 宁可失败，也不要一个损坏的文件

**新增 `tests/test_hdc_pull.py`（11 项）**，把三个问题各钉一条测试。

> 💡 **教训**：`file recv` 返回 0 不等于成功；`cat` 兜底对二进制是有害的。
> 更根本的一条 —— **采集工具必须校验产物本身**（文件头、大小、可解析），
> 而不是相信命令的返回码。这次的损坏正是靠「检查 PNG 文件头」发现的。


---

## 五、给谁用

| 给谁 | 用什么 | 为什么 |
|---|---|---|
| **A** | `etsclock.*` | ★ 无标识控件的极致案例（0 id / 0 可交互），视觉通道验收素材 |
| **A** | `app_settings.*`、`sample_calc.*` | ★ **0 id** 的复杂界面，测「分层路径 + 类型」降级 |
| **A** | `s_etsclock.*` | 弹窗遮挡场景 |
| **B** | 全部 | 弹窗优先级（B2）真实样本；页面双签名（B1）真实输入 |
| **B** | 全部 | ★ 覆盖率分母校准（52 而非 747） |
| **C** | 全部 | 命题合规项「基于示例应用验证」的材料 |

---

## 六、复现方式

```bash
cd D:\project\guochuang-2026\ohauto

# 一条命令搞定（自动处理屏幕/弹窗/路径/校验）
python tools\capture_app.py --bundle ohos.samples.etsclock ^
    --ability MainAbility --name etsclock --out <输出目录>

# 只采当前页面（手动导航后）
python tools\capture_app.py --name my_page --no-launch --out <输出目录>
```

工具会自动：唤醒 → 钉屏 → 解锁 → 启动 → 检测并关闭系统弹窗 →
采集 → 拉回 → **有效性校验**（空树/弹窗会重采，不合格明确报错）。

---

## 七、各应用 ability 名（实测，别照惯例写 `EntryAbility`）

| Bundle | Ability |
|---|---|
| `ohos.samples.etsclock` | `MainAbility` |
| `ohos.samples.distributedcalc` | `MainAbility` |
| `ohos.samples.distributedmusicplayer` | `ohos.samples.distributedmusicplayer.MainAbility` |
| `com.ohos.settings` | `com.ohos.settings.MainAbility` |
| `com.ohos.photos` | `com.ohos.photos.MainAbility` |
| `com.ohos.note` | `MainAbility` |
| `com.ohos.camera` | `com.ohos.camera.MainAbility` |

```bash
# 查任意应用的 ability 名
hdc shell bm dump -n <bundle> | grep -A12 abilityInfos
```
