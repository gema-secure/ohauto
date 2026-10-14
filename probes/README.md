# probes — 定位能力的测量脚本

这一族脚本**只做测量**，不修改仓库行为。它们在一次跨机器的真机测试里
被用来回答一个问题：换掉示例应用之后，定位到底还剩多少能力。

三个结论性的量：

| 事实 | 量 |
|---|---|
| 真机上可交互控件里没有任何语义标识（无 id、无自身文案）的比例 | 79.9%（仓库样本集口径） |
| 这些控件在层级路径那一级（L3）的命中率 | 28.0% |
| 把子树文案（`text_deep`）计入打分后的命中率 | 86.0% |

## 脚本

| 脚本 | 用途 |
|---|---|
| `probe_apps_real.py` | 逐应用：语义标识分布 + L3 命中率 |
| `probe_l3_real.py` | 只看无标识控件的 L3 命中率与命中级别 |
| `probe_l3_with_deep.py` | 把 `text_deep` 计入打分的前后对照（子类覆盖，不改仓库） |
| `_diag_l3.py` | 诊断落空原因：同型候选数、最高分、同分候选数 |
| `_diag_deep.py` | 诊断同分组能否用子树文案区分 |
| `_common.py` | 共用件：样本加载、控件口径、定位规格重建 |

## 用法

```bash
# 离线：直接分析仓库内的多应用样本
python probes/probe_apps_real.py
python probes/probe_l3_real.py
python probes/probe_l3_with_deep.py
python probes/_diag_l3.py
python probes/_diag_deep.py

# 真机：先采集（需设备在线），再分析
python probes/probe_apps_real.py --capture tools/_out/multiapp_survey
```

## 口径

* **可交互** = `LayoutNode.is_interactive()`
  （`ohauto/layout.py`：clickable or scrollable or type 属于一组容器类型）。
  不要用 `clickable` 单标志代替 —— 它会漏掉容器型控件，同一页能差出两倍多。
* **无标识** = 无 id 且无自身文案。
* **L3 命中** = `_lv_path_type` 返回的节点就是目标节点本身；该方法的纪律是
  同类型候选里打分必须唯一最高，两个同分即落空。
* 样本默认取 `datasets/gallery_13app/`。

## 边界

* 这些脚本**不做探索**。「探索页数与覆盖度」需要真机真实点击，
  用既有的 `tools/e2e_smoke_real.py`，这里不重复实现。
* `_diag_l3.py` / `_diag_deep.py` 内的打分公式与
  `ohauto/locator.py::_lv_path_type` 的 `score()` 一一对应；
  `locator.py` 若改动打分项，这两处需同步，否则诊断结论会失真。
