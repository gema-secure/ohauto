# datasets —— 真机样本的存放约定

本目录只放**真机采集的证据**（截图 + `uitest dumpLayout` 控件树 + 采集元数据），
不放用例、不放中间产物。全部离线可用，不需要设备。

## 唯一物理副本：`gallery_13app/`

13 个应用 + 1 组重复采样，每个应用三件套：

| 文件 | 内容 |
|---|---|
| `<app>.json` | `uitest dumpLayout` 导出的控件树原文 |
| `<app>.png` | 同一时刻的截图（与控件树同坐标系） |
| `<app>.meta.json` | 采集元数据：`bundle` / `ability` / `captured_at` / 节点与 id 统计 |

应用清单（按采集批次）：

| 批次 | 应用 |
|---|---|
| 首批 7 个 | `app_note` `app_photos` `app_settings` `etsclock` `s_etsclock` `sample_calc` `sample_music` |
| 续采 7 个 | `camera` `certmanager` `contacts` `launcher` `mms` `myapp` `updateapp` |

`s_etsclock` 是 `etsclock` 的第二组采样（USB 连接方式弹窗态），因此应用数是 13、样本组是 14。

## 单一副本规则（不要违反）

**同一份样本只允许存在一个物理副本。** 采集批次、日期、用途都写进 `*.meta.json`
和本文档，而不是复制出一份带日期的目录。

历史上这里曾有 `real_samples_20260919/`、`multiapp_20261005/` 两个按日期命名的目录，
以及 `tests/fixtures/real_20260919/` 一份测试内副本 —— 三者都是 `gallery_13app/` 的
逐字节子集（共 54 个文件 / 4.07 MB）。现已删除，引用统一改指 `gallery_13app/`。
`tests/test_dataset_single_copy.py` 会阻止这种重复再长回来。

新增采集样本时：

1. 用 `tools/multiapp_survey.py --out <草稿目录>` 采集（默认输出目录是草稿区，不是本目录）；
2. 把三件套**并入 `gallery_13app/`**，并在本文档的应用清单里补一行；
3. 采集元数据保留原始 `captured_at`，这是判断「样本是否过期」的唯一依据。

## 其他内容

| 路径 | 说明 |
|---|---|
| `b15_suite/` | 自动探索轨迹用例（3 条，B1.5 沉淀产物） |
| `b15_note_tree.json` | 笔记应用的单棵控件树（B1.5 用例的运行时输入） |
