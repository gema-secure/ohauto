"""真机样本回归 —— **「模拟器通过 ≠ 真机可用」的护栏**。

样本来源：C 在 2026-09-19 采的 7 组「截图 + `uitest dumpLayout` 控件树」配对，
设备润和 DAYU200 / OpenHarmony 5.0.3.135 / 屏幕 **720×1280**，
原包在 `C交付-给B-2026-09-22-2/datasets/real_samples_20260919/`，
样本现统一存放于仓库内 `datasets/gallery_13app/`（唯一物理副本），
本测试直接读该目录的 json；`.png` 与之同目录，测试用不到图。

| 样本 | bundle | 页面 |
|---|---|---|
| `app_note` | ohos.samples.note | 全部笔记 |
| `app_photos` | ohos.samples.photo | 查看按时间分组的照片 |
| `app_settings` | com.ohos.settings | 设置（**0 个 id**） |
| `etsclock` | ohos.samples.etsclock | 时钟（**0 个可交互控件**） |
| `s_etsclock` | com.ohos.systemui | **USB 连接方式弹窗**（唯一的真弹窗页） |
| `sample_calc` | ohos.samples.distributedcalc | 计算器（20 个可交互、19 个只有 id） |
| `sample_music` | ohos.samples.media | 播放器 |

★ 为什么值得单独一个文件：**这些缺陷只有真机能暴露**。本轮（2026-09-23）
用它抓到两条，都是模拟设备上永远不会红的：

  1. `detect_dialog` 的「多窗口」判据在真机上**恒为真** ——
     真机 `dumpLayout` 返回整个窗口栈（状态栏 / 系统窗 / 导航栏 / 应用），
     于是 **7/7 页全被判成弹窗**，四档优先级里「弹窗置顶」永远生效；
  2. `pick_stress_target` 在真机上返回 `None` ——
     真机的可点击容器**自己没有 id/text/descr**，标签在子节点上。

这两条都不是「逻辑写错了」，而是**模拟器的数据形态和真机不一样**。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.action import spec_to_matcher                                  # noqa: E402
from ohauto.diagnose import BLOCKED, _usability, load_snapshots            # noqa: E402
from ohauto.explorer import (Explorer, build_page_signature,              # noqa: E402
                             detect_dialog)
from ohauto.generator import control_catalog, pick_stress_target          # noqa: E402
from ohauto.layout import flatten, parse_layout                           # noqa: E402
from ohauto.matcher import ON                                             # noqa: E402

FIXTURES = os.path.join(ROOT, 'datasets', 'gallery_13app')

SAMPLES = ('app_note', 'app_photos', 'app_settings', 'etsclock',
           's_etsclock', 'sample_calc', 'sample_music')

#: 真机实测的页面标题（`_page_title()` 应该给出来的东西）
EXPECTED_TITLES = {
    'app_note': '全部笔记',
    'app_photos': '查看按时间分组的照片',
    'app_settings': '设置',
    'etsclock': '',                    # 时钟页确实没有标题文案
    's_etsclock': 'USB 连接方式',
    'sample_calc': '计算器',
    'sample_music': 'dynamic.wav',
}

#: 状态栏窗口的 hostWindowId（真机实测：状态栏与「应用窗」是两个 hostWindowId）
STATUS_BAR_WIN = '7'


def _load(name):
    with open(os.path.join(FIXTURES, name + '.json'), encoding='utf-8') as fh:
        return json.load(fh)


def _meta(name):
    p = os.path.join(FIXTURES, name + '.meta.json')
    if not os.path.exists(p):
        return None
    with open(p, encoding='utf-8') as fh:
        return json.load(fh)


class _StubDriver:
    """只提供 `root` 的最小 driver 桩 —— `_page_title()` 只读这一个字段。"""

    def __init__(self, root):
        self.root = root
        self.bundle = 'com.demo.app'
        self.ability = 'EntryAbility'
        self.artifact_dir = None


class TestRealTreesParse(unittest.TestCase):
    """真机控件树能解析，且节点数与 C 的 meta 一致 —— 夹具没被改坏。"""

    def test_all_samples_parse(self):
        for name in SAMPLES:
            root = parse_layout(_load(name))
            self.assertGreater(len(flatten(root, only_visible=False)), 0, name)

    def test_node_count_matches_meta(self):
        for name in SAMPLES:
            meta = _meta(name)
            if meta is None:
                continue               # 截图-only 的配对没有 meta
            root = parse_layout(_load(name))
            self.assertEqual(len(flatten(root, only_visible=False)),
                             meta['node_count'], f'{name} 节点数变了，夹具可能被改过')

    def test_screen_is_720x1280(self):
        """真机屏幕尺寸进断言 —— 后面所有边界/坐标结论都建立在它上面。"""
        root = parse_layout(_load('sample_calc'))
        self.assertEqual((root.rect.width, root.rect.height), (720, 1280))


class TestSignaturesOnRealTrees(unittest.TestCase):
    """双签名在真机控件树上成立 —— 7 个页面必须给出 7 个不同的结构签名。"""

    def test_all_seven_pages_have_distinct_structural_signatures(self):
        sigs = {}
        for name in SAMPLES:
            meta = _meta(name) or {}
            root = parse_layout(_load(name))
            sig = build_page_signature(root,
                                       bundle=meta.get('bundle', ''),
                                       ability=meta.get('ability', ''))
            sigs[name] = sig.structural_key
        self.assertEqual(len(set(sigs.values())), len(SAMPLES),
                         f'真机页面没有互相区分开：{sigs}')

    def test_content_keys_are_distinct_too(self):
        keys = set()
        for name in SAMPLES:
            meta = _meta(name) or {}
            root = parse_layout(_load(name))
            keys.add(build_page_signature(root, bundle=meta.get('bundle', ''),
                                          ability=meta.get('ability', '')).content_key)
        self.assertEqual(len(keys), len(SAMPLES))


class TestDialogOnRealTrees(unittest.TestCase):
    """★ 真机缺陷回归：**7/7 张真机页面都曾被判成弹窗**。

    原判据是「≥2 个不同 hostWindowId 即多窗口叠加」。真机 `dumpLayout`
    返回的是整个窗口栈：

        状态栏  hostWindowId=7   [0,0][720,72]
        系统窗  hostWindowId=9   [0,0][720,32]
        导航栏  hostWindowId=8   [0,1208][720,1280]
        应用    hostWindowId=<各自> [0,72][720,1208]

    —— 四个窗口，判据恒真。后果不是「偶尔误报」，是**弹窗置顶永远生效**
    （HIGH 档覆盖一切），B2 的四档优先级整体失效。

    现在改成几何判据：两个窗口**重叠面积 ≥ 屏幕 5%** 才算叠加
    （状态栏与系统窗只重叠 2.5%，真弹窗 ≥ 9%）。
    """

    def test_only_the_real_dialog_page_is_a_dialog(self):
        got = {}
        for name in SAMPLES:
            got[name] = detect_dialog(parse_layout(_load(name))).is_dialog
        self.assertEqual([k for k, v in got.items() if v], ['s_etsclock'],
                         f'真机页面里只有 USB 弹窗那一页是弹窗：{got}')

    def test_normal_pages_carry_no_dialog_evidence(self):
        for name in ('app_settings', 'sample_calc', 'app_note'):
            v = detect_dialog(parse_layout(_load(name)))
            self.assertFalse(v.is_dialog, name)
            self.assertEqual(v.evidence, [], name)

    def test_real_dialog_page_names_its_evidence(self):
        v = detect_dialog(parse_layout(_load('s_etsclock')))
        self.assertTrue(v.evidence)
        self.assertTrue(any('弹窗语义' in e for e in v.evidence), v.evidence)

    def test_window_stack_would_have_fooled_the_old_rule(self):
        """把「为什么必须改」钉住：这几页**确实**有 ≥2 个 hostWindowId。"""
        for name in ('app_settings', 'sample_calc'):
            wins = set()
            for n in parse_layout(_load(name)).walk():
                v = str(n.attributes.get('hostWindowId') or '')
                if v:
                    wins.add(v)
            self.assertGreaterEqual(len(wins), 2,
                                    f'{name} 的窗口栈本就有 {sorted(wins)} 个 hostWindowId')


class TestPageTitleOnRealTrees(unittest.TestCase):
    """★ 真机缺陷回归（C 在 DAYU200 上抓的）：页面标题全取成状态栏文本。

    真机状态栏永远是 `top≈32` 且带文本（`没有 SIM 卡` / `11%`），
    而原实现取「最靠上的文本」→ 六个页面标题全成了同一句。
    """

    def _title(self, name):
        root = parse_layout(_load(name))
        return Explorer(_StubDriver(root))._page_title()

    def test_titles_match_real_device(self):
        got = {n: self._title(n) for n in SAMPLES}
        self.assertEqual(got, EXPECTED_TITLES, got)

    def test_status_bar_text_never_becomes_the_title(self):
        checked = 0
        for name in SAMPLES:
            root = parse_layout(_load(name))
            status_texts = {n.text for n in flatten(root, only_visible=True)
                            if str(n.attributes.get('hostWindowId')) == STATUS_BAR_WIN
                            and n.text}
            if not status_texts:
                continue        # 全屏页（app_photos）状态栏没有文本，跳过
            checked += 1
            self.assertNotIn(self._title(name), status_texts,
                             f'{name}: 标题取到状态栏去了')
        self.assertGreaterEqual(checked, 5,
                                '大多数样本的状态栏都该有文本（没文本这条就没测到）')

    def test_clock_page_has_no_title_rather_than_a_wrong_one(self):
        """没有标题就返回空串 —— **宁可空，也不要拿状态栏凑**。"""
        self.assertEqual(self._title('etsclock'), '')


class TestUsabilityOnRealTrees(unittest.TestCase):
    """★ `zIndex` / `opacity` 判据的真机证据。

    真机控件树里 **`opacity` 真的会不是 1**：7 张样本里有 6 张存在
    `opacity=0.7` 的节点（`Row`，列表项主色/禁用态），`sample_calc` 的
    `result` 甚至是 `0.38`。

    于是 C 复核的那条缺陷在真机上**真的会误判**：
    原判据 `opacity < 1.0 → 不可用` 会把所有这些节点判成「点不动」，
    再顺着推到 `CASE_DEFECT`。
    """

    def _nodes_with_opacity(self, name):
        root = parse_layout(_load(name))
        out = []
        for n in flatten(root, only_visible=True):
            raw = str(n.attributes.get('opacity') or '').strip()
            try:
                op = float(raw)
            except ValueError:
                continue
            if op < 1.0:
                out.append((n, op, root))
        return out

    def test_real_trees_do_contain_faded_nodes(self):
        """先把「证据基础」钉住：没有这些节点，下面两条就没有意义。"""
        with_faded = [n for n in SAMPLES if self._nodes_with_opacity(n)]
        self.assertGreaterEqual(len(with_faded), 5,
                                f'真机样本里本该有多个 <1.0 的 opacity：{with_faded}')

    def test_faded_but_clickable_nodes_are_not_judged_unusable(self):
        """`opacity=0.7` → 只算「疑似」，**不许**判不可用。"""
        checked = 0
        for name in SAMPLES:
            for node, op, root in self._nodes_with_opacity(name):
                if op >= 0.5:
                    verdict, why = _usability(node, root)
                    self.assertNotEqual(
                        verdict, BLOCKED,
                        f'{name} 的 {node.type} opacity={op} 被判不可用：{why}')
                    checked += 1
        self.assertGreater(checked, 0, '没检查到任何节点，断言是空转')

    def test_genuinely_faint_node_is_still_blocked(self):
        """但真的近乎透明（`sample_calc.result` = 0.38）仍应判不可用。"""
        root = parse_layout(_load('sample_calc'))
        found = [n for n in flatten(root, only_visible=True)
                 if n.id == 'result']
        self.assertTrue(found, 'sample_calc 里应当有 id=result 的节点')
        verdict, why = _usability(found[0], root)
        self.assertEqual(verdict, BLOCKED, f'{why}')

    def test_zindex_clue_is_absent_from_these_captures(self):
        """诚实留白：这批样本里**没有** `zIndex != 0` 的节点。

        所以「zIndex 方向反了」那条在真机上只做了几何推演与单测构造，
        **没有真机现场数据**。等谁抓一张带 zIndex 的真机树再补。
        """
        for name in SAMPLES:
            root = parse_layout(_load(name))
            zs = {str(n.attributes.get('zIndex') or '').strip()
                  for n in flatten(root, only_visible=False)}
            zs.discard('')
            zs.discard('0')
            self.assertEqual(zs, set(), f'{name} 出现非零 zIndex={zs}，'
                                        f'说明夹具变了，可以补真机回归了')


class TestStressTargetOnRealTrees(unittest.TestCase):
    """★ 真机缺陷回归：`pick_stress_target` 在真机上返回 `None`。

    真机的可点击容器（`Row` / `ListItem`）**自己没有 id/text/descr**，
    标签挂在子 `Text` 上 —— 而原实现只看节点自身，于是
    `app_settings`（0 个 id）/ `etsclock` / `app_photos` 全部挑不出目标。
    """

    def _target(self, name):
        return pick_stress_target(parse_layout(_load(name)))

    def test_most_real_pages_yield_a_target(self):
        got = {n: self._target(n) for n in SAMPLES}
        picked = [k for k, v in got.items() if v]
        self.assertGreaterEqual(len(picked), 6,
                                f'真机页面应当几乎都能挑出目标：{got}')
        for name, spec in got.items():
            if spec is None:
                continue
            self.assertTrue(set(spec) & {'id', 'text', 'descr', 'type'},
                            f'{name} 挑出来的规格不像匹配器：{spec}')

    def test_target_never_contains_coordinates(self):
        """红线第 5 条：**永远不吐坐标**，即使在真机上兜底。"""
        for name in SAMPLES:
            spec = self._target(name)
            if not spec:
                continue
            blob = json.dumps(spec, ensure_ascii=False).lower()
            for bad in ('tap_xy', '"x"', '"y"', 'startx', 'coord'):
                self.assertNotIn(bad, blob, f'{name}: {spec}')

    def test_clock_page_honestly_yields_none(self):
        """时钟页是真的没有可点控件 —— `None` 是诚实答案，不是失败。"""
        root = parse_layout(_load('etsclock'))
        clickable = [n for n in flatten(root, only_visible=True)
                     if n.clickable or n.is_interactive()]
        self.assertEqual(clickable, [], '夹具变了：时钟页居然有可点控件了')
        self.assertIsNone(self._target('etsclock'))

    def test_picked_spec_resolves_back_to_a_real_node(self):
        """挑出来的规格必须能在同一棵树里**匹配回来** —— 否则等于编了个 id。

        ★ 这里必须走**与 DSL 生产消费同源**的那条转换（`action.spec_to_matcher`），
        而不是另写一个。早前这个测试写的是

            self.assertTrue(ON.spec(spec).filter(...) if hasattr(ON, 'spec') else True)

        而 `matcher.py` 里根本没有 `spec` 这个属性（`ON = _ONFactory()`，
        全是显式 staticmethod，没有 `__getattr__`）→ `hasattr` **恒为 False**
        → 每次都退化成 `assertTrue(True)`。**一条约束都没有，却显示全绿。**

        判据要验「区分度」：一个在所有输入下都为真的断言，等于没有断言。
        """
        for name in SAMPLES:
            spec = self._target(name)
            if not spec:
                continue
            root = parse_layout(_load(name))
            hits = spec_to_matcher(spec).filter(flatten(root, only_visible=True))
            self.assertTrue(
                hits,
                f'{name}：挑出来的规格 {spec} 在同一棵树里匹配不到任何节点')


class TestCatalogOnRealTrees(unittest.TestCase):
    """控件清单在「没有 id」的真机页面上仍然可用（B3 发提示词要用）。"""

    def test_settings_page_catalog_is_built_from_text(self):
        catalog = control_catalog(parse_layout(_load('app_settings')))
        self.assertTrue(catalog.strip())
        self.assertNotEqual(catalog, '(控件树里没有可用控件)')
        self.assertIn('蓝牙', catalog, '真机文案应当出现在清单里')

    def test_calculator_catalog_is_built_from_ids(self):
        catalog = control_catalog(parse_layout(_load('sample_calc')))
        self.assertIn('id=', catalog)


class TestSnapshotLoaderOnRealFixtures(unittest.TestCase):
    """`load_snapshots` 能直接吃真机控件树 json（闭环接入用）。"""

    def test_real_layout_json_is_recognised_as_a_snapshot(self):
        trees = load_snapshots(FIXTURES)
        self.assertGreaterEqual(len(trees), 5,
                                '真机控件树应当被认成快照（meta.json 要被排除）')
        self.assertTrue(all(t.rect.area > 0 for t in trees))

    def test_meta_files_are_not_mistaken_for_layouts(self):
        names = os.listdir(FIXTURES)
        self.assertIn('sample_calc.meta.json', names)
        trees = load_snapshots(FIXTURES)
        # meta 里没有 bounds，解析不出带面积的控件树 → 不该被当成快照
        ids = {t.id for t in trees}
        self.assertNotIn('node_count', ids)


if __name__ == '__main__':
    unittest.main(verbosity=2)
