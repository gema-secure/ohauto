# 夹具说明 · Mate X7 桌面跨形态实测样本

> 采集时间：2026-09-18 21:24 ｜ 设备：DevEco 模拟器 `Mate X7`（HarmonyOS 7.0.0）
> 采集方式：`python tools/crossform_run.py --device "Mate X7" --baseline open --target close`

## 这批夹具是什么

**同一台设备、同一个界面（系统桌面 `SCBDesktop`），在两种折叠态下**的真实控件树。
它是跨形态比对引擎的**真机保真度基准** —— 引擎的一切判据都要能解释这批数据，
否则就是引擎写错了。

| 文件 | 内容 |
|---|---|
| `matex7_desktop_open_20260918.json` | 展开态（内屏）控件树，100 节点 / 89 个可识别身份 |
| `matex7_desktop_open_screens_20260918.txt` | 展开态 `hidumper -s RenderService -a screen` 原文 |
| `matex7_desktop_close_20260918.json` | 折叠态（外屏）控件树，89 节点 / 81 个可识别身份 |
| `matex7_desktop_close_screens_20260918.txt` | 折叠态同上 |

形态参数（从 screens 原文解析，非配置值）：

| 形态 | 点亮屏 | 尺寸 | powerStatus |
|---|---|---|---|
| `open` | screen[0] 内屏 | **2416×2210** | `POWER_STATUS_ON` |
| `close` | screen[1] 外屏 | **1080×2444** | `POWER_STATUS_ON` |

> ⚠️ 判据是**两块屏的 powerStatus 互换**，不是分辨率变化 ——
> 分辨率本来就不变（内屏恒 2416×2210、外屏恒 1080×2444）。

## ★ 实测结论（引擎在这批数据上应判出的结果）

```
Mate X7_open (2416x2210) -> Mate X7_close (1080x2444)
缺失 11 / 越界 0 / 不可达 0 / 溢出 1
```

### 缺失的 11 处是什么

分两类，**都是系统桌面自身在窄屏下的响应式行为**，不是缺陷：

**① 桌面第二页的图标网格（`SwiperPage_Grid_WorkSpace_1`）**

展开态 bounds `[1108,170,2162,1933]`，是桌面 swiper 的第二页。
切到外屏后屏宽只剩 1080 —— 该页整体落在屏幕外，**系统不渲染它**，
所以身份消失。

相关的连带项：`hp:List@Column>Row>Row>Stack`、`hp:Row@Stack>Flex>Column>Row`、
`hp:Stack@Flex>Column>Row>Row` 等（都是这一页里的容器与列表）。

**② 底部 Dock 栏（`DOCK_*`）**

| 身份 | 展开态 bounds |
|---|---|
| `id:DOCK_RESIDENT_BG` | `[721,2079,1222,2317]` |
| `id:DOCK_recent_BG` | `[1253,2079,1491,2317]` |
| `id:DOCK_recent_DIVIDER` | `[1222,2160,1253,2235]` |

外加 Dock 里的图库图标（`recentcom.huawei.hmos.photos` 等）。
展开态下 Dock 横向铺在 x∈[721,1491]，外屏宽仅 1080 →
系统重排了 Dock，这一组容器与图标**换了身份或改了布局**，因此按身份匹配不上。

> 💡 **这 11 处「缺失」的正确读法**：它们证明**引擎确实能检出跨形态的
> 元素消失**，而不是说系统桌面有 11 个 bug。要判「是不是 bug」需要
> 人看：桌面第二页在折叠态本来就不该显示（合理），Dock 栏换了 id
> （可能合理，也可能是 id 生成不稳定）。

### 溢出的 1 处（`id:LiveMetaBallBaseVm`）

```
元素 [321,0,759,121] 超出父容器 [496,12,584,100]
（该元素仅在目标形态出现，基准形态没有）
```

这是折叠态下**新出现**的智慧语音悬浮球。父容器 `[496,12,584,100]`
只有 88×88，而子元素 438×121 —— 子元素显著大于父容器。

> ⚠️ **这条要按「新元素」读，不是「跨形态退化」**。
> `is_new_in_target=True` 已标在 evidence 里。
> 真机上这类「子元素大于父容器」常见于**动画/悬浮层**（父容器是逻辑锚点，
> 不做裁剪），**未必是缺陷**。要下结论得看渲染截图。

## 为什么这批夹具很重要

1. **它验证了「身份匹配」策略在真实控件树上可用** ——
   真实桌面的 id 里有 `[com.huawei.hmos.photos]`、`phone_photos0_436207618_1`
   这类**带包名和实例号的长 id**（见缺失清单第 5、6 条）。
   这类 id 在切形态后可能重新生成，导致身份对不上 ——
   这正是「裸 id 匹配」的已知短板，批次里如实暴露了出来。

2. **它暴露了「采集未就绪」这个危险失效模式** ——
   首次采集时折叠态拿到过一份**只有根节点的空树**（切换后桌面还没重绘），
   直接比对会得出「89 个元素缺失」的假结论。
   修复：`capture_online` 加重试 + 最小元素数校验
   （见 `tools/crossform_run.py` 的 `capture_online`）。

3. **根节点的 `type` / `id` 都是空字符串** ——
   这是该版本 dumpLayout 的真实行为。早期按「根节点有 id」写死的逻辑
   会在这里失效。引擎已改为按 `parent is None` 判根，不依赖 id。

## 复现方式

```bash
cd D:\project\guochuang-2026\ohauto

# 需要模拟器在线（见 docs/指南-折叠屏模拟器.md）
python tools\crossform_run.py --device "Mate X7" \
    --baseline open --target close --stem matex7_real
```

离线回归（不需要设备）：见 `tests/test_crossform.py::TestRealFixture`。
