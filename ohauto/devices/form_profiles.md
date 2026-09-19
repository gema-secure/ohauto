# 设备形态档案清单（跨形态测试用）

> **自动生成，勿手工编辑。** 重新生成：`python tools/export_form_profiles.py`

数据源：`C:\Users\<user>\AppData\Local\Huawei\Emulator26.0\productConfig.json`

共 **77** 条形态档案。


## 一、形态总览

| 类别 | 形态数 | 说明 |
|---|---:|---|
| `phone` | 47 | 直板手机 |
| `tablet` | 6 | 平板 |
| `foldable` | 4 | 横折折叠屏（展开） |
| `foldable_folded` | 4 | 横折折叠屏（折叠） |
| `car` | 2 | 车机 |
| `pc` | 2 | 2in1 笔记本 |
| `wearable` | 2 | 穿戴 |
| `wide_fold` | 2 | 竖折/阔折（展开） |
| `wide_fold_folded` | 2 | 竖折/阔折（折叠） |
| `pc_foldable` | 1 | 折叠笔记本（展开） |
| `pc_foldable_folded` | 1 | 折叠笔记本（折叠） |
| `triple_fold` | 1 | 三折叠（全展开） |
| `triple_fold_double` | 1 | 三折叠（双屏中间态） |
| `triple_fold_folded` | 1 | 三折叠（折叠态） |
| `tv` | 1 | 智慧屏 |

## 二、折叠屏多形态对照（跨形态 的核心资产）

折叠屏在配置里是**一台设备多组分辨率**，加载时展开成多条档案。下表是同一台设备各形态的对照 —— 跨形态差异就该在这些行之间比。

| 设备 | 形态 | 分辨率 | DPI | 对角线 | 圆角 | 挖孔 (x,y,w,h) |
|---|---|---|---:|---:|---|---|
| Customize | EXPANDED | 2224×2496 | 500 | 7.85" | — | — |
| Customize | FOLDED | 1080×2504 | 500 | 6.4" | — | — |
| Mate X5 | EXPANDED | 2224×2496 | 500 | 7.85" | 27.95,22.27 | (2109,28,69,69) |
| Mate X5 | FOLDED | 1080×2504 | 500 | 6.4" | 27.95,22.27 | (507,18,66,66) |
| Mate X6 | EXPANDED | 2240×2440 | 500 | 7.93" | 23.63,22.31 | (2121,30,70,70) |
| Mate X6 | FOLDED | 1080×2440 | 500 | 6.45" | 23.63,22.31 | (510,28,64,64) |
| Mate X7 | EXPANDED | 2210×2416 | 500 | 8" | 26.25,23.1 | (2092,28,70,70) |
| Mate X7 | FOLDED | 1080×2444 | 500 | 6.49" | 26.25,23.1 | (508,24,64,64) |
| Mate XT | EXPANDED | 3184×2232 | 460 | 10.2" | 8 | (479,21,65,65) |
| Mate XT | FOLDED | 2048×2232 | 460 | 7.9" | 8 | (479,21,65,65) |
| Mate XT | DOUBLE | 1008×2232 | 460 | 6.4" | 8 | (479,21,65,65) |
| MateBook Fold | EXPANDED | 3296×2472 | 288 | 18" | — | — |
| MateBook Fold | FOLDED | 2472×1648 | 288 | 13" | — | — |
| Pura X | EXPANDED | 1320×2120 | 480 | 6.3" | 25.33,31.29 | — |
| Pura X | FOLDED | 980×980 | 480 | 3.5" | 25.33,31.29 | — |
| Pura X Max | EXPANDED | 2584×1828 | 440 | 7.7" | 37.8,31.5 | (2470,28,72,72) |
| Pura X Max | FOLDED | 1264×1848 | 440 | 5.4" | 37.8,31.5 | (599,26,66,66) |

## 三、推荐形态对（比单点形态更有测试价值）

按**规格**（同宽 / 宽高比反转 / 宽度腰斩 / 跨品类）自动从档案库挑选，不是硬编码设备名 —— 华为改了配置这张表会跟着变，不会静默失效。

| 用途 | 形态 A | 形态 B | 测什么 |
|---|---|---|---|
| 同宽不同高 | 真机 720×1280 | Enjoy 90 Plus (720×1604) | 宽度同为 720 → 横向布局不该变；高度差 324px → 专测纵向滚动与底部元素 |
| 折叠展开/折叠 | Mate X6_unfolded (2240×2440) | Mate X6_folded (1080×2440) | 宽度 2240→1080（48%）→ 专测栅格列数变化、导航栏形态切换 |
| 三折叠极值 | Mate XT_unfolded (3184×2232) | Mate XT_double (1008×2232) | 宽度 3184→1008（32%）→ 专测超宽屏布局退化为窄屏时元素丢失 |
| 手机↔大屏 | Enjoy 90 Plus (720×1604) | MateBook Pro (3120×2080) | 宽度 720→3120（4.3×）→ 专测最大宽度下的布局溢出与超长留白 |

## 四、需要实测复核的档案

下列档案的静态数据有已知的不确定性，`caveats` 字段（JSON 里）已显式标注。**在这些形态上做出的结论必须先实测验证。**

| 档案 | 分辨率 | 不确定性 |
|---|---|---|
| Mate XT_double | 1008×2232 | double_form_inherits_cutout: 配置里没有为双屏中间态单独给挖孔，此处沿用展开态坐标 —— 需实测复核 |
| Mate XT_folded | 2048×2232 | folded_form_reuses_unfolded_cutout: 配置里没有 twoCutoutPath，折叠态挖孔坐标直接复用了展开态的 —— 需实测（hidumper）复核 |

## 五、⚠️ 安全区不在静态配置里

`productConfig.json` **没有任何安全区（状态栏/导航栏高度）字段**，所以 JSON 里每条的 `status_bar_h` / `nav_bar_h` 都是 0、`has_measured_safe_area` 都是 `false`，此时 `safe_area` 等于整屏。

真实值只能从真机 hidumper 实测：

```bash
hidumper -s WindowManagerService -a '-a'
```

实测样本见 `ohauto/tests/fixtures/crossform/displaymanager_20260917_172127.txt`：

```
SystemUi_StatusBar    [ 0    0    720  72  ]
SystemUi_NavigationB [ 0    1208 720  72  ]
```

