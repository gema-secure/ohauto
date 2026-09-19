"""跨形态测试 —— 形态档案模块单元测试。

**全部测试都不需要真机、也不需要装 DevEco**，一律由 `fixtures/crossform/` 下的
两份实测样本驱动（任务卡第七章一：无设备机器上必须全绿）。

    productConfig.json              DevEco 模拟器的设备产品配置原文
                                    （68 台设备 / 11 个类型，23 KB）
    displaymanager_20260917_172127.txt
                                    真机 hidumper 原文（三份合并）——
                                    用于核对「配置里的形态」与「真机实测的
                                    形态」是否对得上

设计原则：**宁可 None 不可猜**。挖孔坐标一旦猜错，下游「控件是否被挖孔
遮挡」的判定会全盘错位 —— 所以解析失败的用例都要验「是否明确拒绝」。
"""
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.devices import (                                    # noqa: E402
    FORM_DOUBLE, FORM_EXPANDED, FORM_FOLDED,
    FormProfile, find_product_config, find_profile,
    is_no_cutout_placeholder, load_product_config, load_profiles,
    parse_cutout_path,
)

sys.path.insert(0, os.path.join(ROOT, 'tools'))
from export_form_profiles import build_recommended_pairs      # noqa: E402

FIXTURE_DIR = os.path.join(HERE, 'fixtures', 'crossform')
CFG = os.path.join(FIXTURE_DIR, 'productConfig.json')
DM_TXT = os.path.join(FIXTURE_DIR, 'displaymanager_20260917_172127.txt')


def profiles():
    return load_profiles(CFG)


# ============================================================ 挖孔 path 解析

class TestParseCutoutPath(unittest.TestCase):
    """SVG path 极简子集 → 包围盒。"""

    def test_rect_from_real_path(self):
        # 真机/配置原文实测样本
        self.assertEqual(parse_cutout_path('M624 42 L694 42 v 70 h -70 Z'),
                         (624, 42, 70, 70))

    def test_absolute_commands(self):
        # L 是绝对坐标；v/h 是小写相对位移
        self.assertEqual(parse_cutout_path('M100 20 L180 20 v 60 h -80 Z'),
                         (100, 20, 80, 60))

    def test_uppercase_relative_is_absolute(self):
        # 大写 V/H = 绝对坐标（SVG 规范），别当相对算
        self.assertEqual(parse_cutout_path('M10 10 L90 10 V 80 H 10 Z'),
                         (10, 10, 80, 70))

    def test_tokens_may_be_space_separated(self):
        # ★ 回归护栏：指令之间有空格的写法（'L694 42 v 70'）必须能连续分词。
        # 曾经的 bug：正则没允许前导空白，第二次 match 就断在空格上，
        # 导致**所有**含空格的 path 全部解析失败、挖孔全军覆没。
        multi_space = parse_cutout_path('M 624  42  L 694  42  v 70  h -70  Z')
        self.assertEqual(multi_space, (624, 42, 70, 70))

    def test_garbage_returns_none_not_guess(self):
        for bad in ('', 'hello world', 'Q1 2 3 4', 'M1', 'Z'):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_cutout_path(bad))

    def test_zero_size_returns_none(self):
        # 零尺寸不是合法挖孔（占位符语义，交由 is_no_cutout_placeholder 判）
        self.assertIsNone(parse_cutout_path('M0 0 L0 0 v 0 h -0 Z'))

    def test_non_string_returns_none(self):
        self.assertIsNone(parse_cutout_path(None))
        self.assertIsNone(parse_cutout_path(123))


class TestNoCutoutPlaceholder(unittest.TestCase):
    """华为用**零尺寸 path** 表示「无挖孔」，不是省略字段。"""

    def test_recognizes_placeholder(self):
        self.assertTrue(is_no_cutout_placeholder('M0 0 L0 0 v 0 h -0 Z'))

    def test_real_cutout_is_not_placeholder(self):
        self.assertFalse(is_no_cutout_placeholder('M624 42 L694 42 v 70 h -70 Z'))

    def test_empty_and_none(self):
        self.assertFalse(is_no_cutout_placeholder(None))
        self.assertFalse(is_no_cutout_placeholder(''))


# ============================================================ FormProfile 判定

class TestFormProfileJudgement(unittest.TestCase):
    """跨形态四类差异的判据 helper。"""

    def setUp(self):
        # 一块假想的异形屏：1080x2400，状态栏 72、导航栏 96，右上角一个孔
        self.p = FormProfile(
            name='fake', device='Fake', kind='phone',
            width=1080, height=2400,
            status_bar_h=72, nav_bar_h=96,
            cutouts=((900, 30, 90, 90),),
        )

    def test_safe_area_excludes_bars(self):
        self.assertEqual(self.p.safe_area, (0, 72, 1080, 2304))

    def test_has_measured_safe_area(self):
        self.assertTrue(self.p.has_measured_safe_area)
        self.assertFalse(FormProfile('x', 'X', 'phone', 100, 200).has_measured_safe_area)

    def test_unmeasured_safe_area_is_whole_screen(self):
        # ⚠️ 没测过时 safe_area == 整屏，语义是「还没测」不是「没安全区」
        p = FormProfile('x', 'X', 'phone', 100, 200)
        self.assertEqual(p.safe_area, (0, 0, 100, 200))

    def test_rect_within_screen(self):
        self.assertTrue(self.p.rect_within_screen((0, 0, 1080, 2400)))
        self.assertFalse(self.p.rect_within_screen((0, 0, 1081, 2400)))
        self.assertFalse(self.p.rect_within_screen((-1, 0, 100, 100)))

    def test_rect_hits_cutout_intersection(self):
        # 元素压到孔上 → 看得见但可能点不到
        self.assertTrue(self.p.rect_hits_cutout((880, 20, 1000, 140)))
        self.assertFalse(self.p.rect_hits_cutout((0, 0, 100, 100)))

    def test_center_hits_cutout_is_stricter(self):
        # 元素与孔相交，但中心点在孔外 → 还能点到
        big = (800, 0, 1080, 300)
        self.assertTrue(self.p.rect_hits_cutout(big))
        self.assertFalse(self.p.center_hits_cutout(big))
        # 中心点落进孔里 → 真正点不到
        self.assertTrue(self.p.center_hits_cutout((900, 20, 990, 140)))

    def test_no_cutout_never_hits(self):
        p = FormProfile('x', 'X', 'phone', 100, 200)
        self.assertFalse(p.rect_hits_cutout((0, 0, 200, 300)))
        self.assertFalse(p.center_hits_cutout((0, 0, 200, 300)))

    def test_rect_overflows_parent(self):
        self.assertTrue(self.p.rect_overflows_parent((0, 0, 100, 100),
                                                     (10, 10, 90, 90)))
        self.assertFalse(self.p.rect_overflows_parent((10, 10, 90, 90),
                                                      (10, 10, 90, 90)))

    def test_malformed_rect_is_false_not_crash(self):
        for bad in ((), (1, 2, 3), (1, 2, 3, 4, 5)):
            with self.subTest(bad=bad):
                self.assertFalse(self.p.rect_within_screen(bad))
                self.assertFalse(self.p.rect_hits_cutout(bad))
                self.assertFalse(self.p.center_hits_cutout(bad))

    def test_is_folded_covers_all_fold_kinds(self):
        # ★ 回归护栏：曾经只判 foldable_folded / triple_fold_folded，
        # 漏了 wide_fold_folded 与 pc_foldable_folded。
        for kind in ('foldable_folded', 'wide_fold_folded',
                     'triple_fold_folded', 'pc_foldable_folded'):
            with self.subTest(kind=kind):
                self.assertTrue(FormProfile('n', 'D', kind, 100, 200).is_folded)
        for kind in ('phone', 'foldable', 'tablet', 'wide_fold'):
            with self.subTest(kind=kind):
                self.assertFalse(FormProfile('n', 'D', kind, 100, 200).is_folded)

    def test_is_double(self):
        self.assertTrue(FormProfile('n', 'D', 'triple_fold_double', 1, 1).is_double)
        self.assertFalse(FormProfile('n', 'D', 'foldable_folded', 1, 1).is_double)

    def test_to_dict_is_json_serializable(self):
        d = self.p.to_dict()
        json.dumps(d)                       # 不抛异常即通过
        self.assertEqual(d['safe_area'], [0, 72, 1080, 2304])
        self.assertFalse(d['is_folded'])
        self.assertTrue(d['has_measured_safe_area'])


# ============================================================ 配置加载

class TestLoadConfig(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(find_product_config(CFG), CFG)

    def test_missing_explicit_path_returns_none(self):
        self.assertIsNone(find_product_config(os.path.join(HERE, 'nope.json')))

    def test_load_fixture(self):
        cfg = load_product_config(CFG)
        self.assertIn('Foldable', cfg)
        self.assertEqual(len(cfg['Phone']), 47)

    def test_load_missing_returns_empty_not_crash(self):
        self.assertEqual(load_product_config(os.path.join(HERE, 'nope.json')), {})


# ============================================================ 形态档案展开

class TestLoadProfiles(unittest.TestCase):
    """折叠屏多形态展开 —— 跨形态 的核心资产。"""

    def setUp(self):
        self.ps = profiles()

    def test_counts_match_config(self):
        by_kind = {}
        for p in self.ps:
            by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
        # 直板/非折叠：与配置 1:1
        self.assertEqual(by_kind['phone'], 47)
        self.assertEqual(by_kind['tablet'], 6)
        self.assertEqual(by_kind['tv'], 1)
        self.assertEqual(by_kind['car'], 2)
        # 折叠屏：每款展开成 2 条（展开 + 折叠）
        self.assertEqual(by_kind['foldable'], 4)
        self.assertEqual(by_kind['foldable_folded'], 4)
        self.assertEqual(by_kind['wide_fold'], 2)
        self.assertEqual(by_kind['wide_fold_folded'], 2)
        self.assertEqual(by_kind['pc_foldable'], 1)
        self.assertEqual(by_kind['pc_foldable_folded'], 1)
        # ★ 三折叠：3 条（展开 + 双屏 + 折叠）
        self.assertEqual(by_kind['triple_fold'], 1)
        self.assertEqual(by_kind['triple_fold_double'], 1)
        self.assertEqual(by_kind['triple_fold_folded'], 1)

    def test_every_fold_device_has_folded_form(self):
        # 每条 *_folded 都要有对应的展开态，不能凭空空降
        names = {p.name for p in self.ps}
        for p in self.ps:
            if p.is_folded:
                with self.subTest(name=p.name):
                    self.assertIn(p.name.replace('_folded', '_unfolded'), names)

    def test_mate_xt_has_three_forms(self):
        xt = {p.fold_status for p in self.ps if p.device == 'Mate XT'}
        self.assertEqual(xt, {FORM_EXPANDED, FORM_DOUBLE, FORM_FOLDED})

    def test_mate_xt_double_uses_double_screen_size(self):
        dbl = find_profile('Mate XT_double', CFG)
        self.assertIsNotNone(dbl)
        # outerDoubleScreenWidth=1008, Height=2232（不是展开态的 3184）
        self.assertEqual((dbl.width, dbl.height), (1008, 2232))

    def test_folded_uses_outer_screen_size(self):
        uf = find_profile('Mate X7_unfolded', CFG)
        fd = find_profile('Mate X7_folded', CFG)
        self.assertEqual((uf.width, uf.height), (2210, 2416))
        self.assertEqual((fd.width, fd.height), (1080, 2444))
        self.assertTrue(fd.is_folded)
        self.assertFalse(uf.is_folded)

    def test_folded_and_unfolded_take_different_cutouts(self):
        """★ 回归护栏：`twoCutoutPath` 是**折叠态的孔**，不是「第二个孔」。

        曾经的 bug：把 oneCutoutPath + twoCutoutPath 都塞进展开态，
        结果展开态凭空多出一个不存在的孔，折叠态反而没孔。
        """
        uf = find_profile('Mate X7_unfolded', CFG)
        fd = find_profile('Mate X7_folded', CFG)
        self.assertEqual(len(uf.cutouts), 1)
        self.assertEqual(len(fd.cutouts), 1)
        # 展开态孔靠右（内屏右侧），折叠态孔居中（外屏顶部）
        self.assertGreater(uf.cutouts[0][0], uf.width * 0.8)
        self.assertLess(fd.cutouts[0][0], fd.width * 0.7)
        self.assertNotEqual(uf.cutouts, fd.cutouts)

    def test_kinds_filter(self):
        only_fold = load_profiles(CFG, kinds=['Foldable'])
        self.assertTrue(only_fold)
        self.assertTrue(all(p.kind.startswith('foldable') for p in only_fold))
        self.assertEqual(len(only_fold), 8)         # 4 款 × 2 形态

    def test_missing_config_returns_empty(self):
        self.assertEqual(load_profiles(os.path.join(HERE, 'nope.json')), [])

    def test_names_unique(self):
        """★ 回归护栏：配置里四类通用模板都叫 `Customize`，会撞名。

        撞名的后果是 `find_profile()` 随机命中其中一个 —— 跨形态测试
        选错基准形态比没有形态更危险。加载时应自动补类别前缀消歧。
        """
        names = [p.name for p in self.ps]
        self.assertEqual(len(names), len(set(names)), '档案名有重复')

    def test_customize_templates_are_disambiguated(self):
        """撞名的 4 个补了类别前缀；折叠类的自带后缀、无需消歧。"""
        customs = [p for p in self.ps if 'Customize' in p.name]
        # Phone/Tablet/2in1/Car 四个通用模板撞名 → 带前缀
        prefixed = [p for p in customs if '/' in p.name]
        self.assertEqual(len(prefixed), 4)
        self.assertEqual({p.kind for p in prefixed},
                         {'phone', 'tablet', 'pc', 'car'})
        self.assertEqual(len({(p.width, p.height) for p in prefixed}), 4)
        # 折叠类的 Customize 靠 _unfolded/_folded 后缀区分，不撞名
        self.assertEqual({p.kind for p in customs if '/' not in p.name},
                         {'foldable', 'foldable_folded'})


# ============================================================ 数据质量

class TestDataQuality(unittest.TestCase):
    """配置数据的结构与可信度 —— 有问题要让 caveats 说出来。"""

    def setUp(self):
        self.ps = profiles()

    def test_all_profiles_have_positive_size(self):
        for p in self.ps:
            with self.subTest(name=p.name):
                self.assertGreater(p.width, 0)
                self.assertGreater(p.height, 0)
                self.assertGreater(p.density, 0)

    def test_most_phones_have_cutout(self):
        phones = [p for p in self.ps if p.kind == 'phone']
        with_cut = [p for p in phones if p.cutouts]
        # 实测：56/68 台设备有孔，Phone 里绝大多数有
        self.assertGreater(len(with_cut), len(phones) * 0.8)

    def test_cutout_inside_screen(self):
        # 孔必须落在屏幕内，否则我们的解析一定错了
        for p in self.ps:
            for (x, y, w, h) in p.cutouts:
                with self.subTest(name=p.name):
                    self.assertGreaterEqual(x, 0)
                    self.assertGreaterEqual(y, 0)
                    self.assertLessEqual(x + w, p.width)
                    self.assertLessEqual(y + h, p.height)

    def test_no_fake_parsed_cutout(self):
        # 解析失败要留痕（caveats），不能静默当成「无孔」
        for p in self.ps:
            if p.extra.get('raw_cutout') and not p.cutouts:
                raw = p.extra['raw_cutout']
                with self.subTest(name=p.name):
                    self.assertTrue(
                        is_no_cutout_placeholder(raw) or
                        any('cutout_unparsed' in c for c in p.caveats),
                        f'{p.name}: 有 raw_cutout 但既没解析出来也不是占位符，'
                        f'且没记 caveat → 会被误当成无孔设备')

    def test_mate_xt_uncertainty_is_flagged(self):
        # Mate XT 配置里只有一个孔坐标，三形态复用 —— 必须显式标注
        for p in self.ps:
            if p.device == 'Mate XT' and p.fold_status in (FORM_FOLDED, FORM_DOUBLE):
                with self.subTest(form=p.fold_status):
                    self.assertTrue(
                        any('cutout' in c for c in p.caveats),
                        f'{p.name} 复用了展开态挖孔，却没标 caveat')

    def test_caveats_is_tuple_and_serializable(self):
        for p in self.ps:
            self.assertIsInstance(p.caveats, tuple)
            json.dumps(p.to_dict())


# ============================================================ find_profile

class TestFindProfile(unittest.TestCase):
    def test_exact_and_normalized(self):
        for q in ('Mate X7_unfolded', 'mate x7 unfolded', 'MateX7_unfolded'):
            with self.subTest(q=q):
                self.assertIsNotNone(find_profile(q, CFG))

    def test_missing_returns_none(self):
        self.assertIsNone(find_profile('No Such Device', CFG))

    def test_accepts_preloaded_pool(self):
        pool = profiles()
        p = find_profile('Mate X6_folded', profiles=pool)
        self.assertIsNotNone(p)
        self.assertIs(p, [x for x in pool if x.name == 'Mate X6_folded'][0])


# ============================================================ 与真机实测对齐

class TestAgainstRealDevice(unittest.TestCase):
    """真机 hidumper 原文 —— 核对配置与实测能否对得上。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(DM_TXT):
            raise unittest.SkipTest('缺真机 hidumper 夹具')
        with io.open(DM_TXT, encoding='utf-8', errors='replace') as f:
            cls.raw = f.read()

    def test_fixture_has_three_sections(self):
        # 夹具是三份原文合并：-s -a（屏幕列表）/ -d -a（显示信息）/ 窗口列表
        self.assertIn('DisplayManagerService', self.raw)
        self.assertIn('WindowManagerService', self.raw)

    def test_real_device_size_is_outside_emulator_device_set(self):
        """★ 实测结论：真机 720x1280 这个**具体尺寸**不在模拟器设备集里。

        真机（OpenHarmony 开发板）是 720x1280；配置里最接近的是
        Enjoy 90 / Enjoy 90 Plus 的 **720x1604 —— 宽度一样、高度差 324**。
        所以真机不能被任何模拟器设备替代，它自己的形态必须靠 hidumper
        实测补进形态矩阵。

        这条断言的作用：一旦配置里真出现了 720x1280，它就红，提醒我们
        「真机 = 独立形态」这个前提要重新评估。
        """
        ps = profiles()
        hit = [p for p in ps if (p.width, p.height) == (720, 1280)]
        self.assertEqual(hit, [],
                         '配置里出现了与真机同尺寸的形态，需重新评估测试矩阵')

    def test_real_device_shares_width_with_phone_forms(self):
        """真机 720 宽与配置里的窄屏 Phone 同宽 —— 这是**可比形态**。

        用途：跨形态 的「同宽不同高」差异对，可以直接拿真机（720x1280）和
        Enjoy 90（720x1604）组一对 —— 宽度相同意味着横向布局不该变，
        高度差 324px 则专测纵向滚动/底部元素是否丢失。
        这是不用买新设备就能造出来的高质量形态对。
        """
        ps = profiles()
        same_width = [p for p in ps
                      if p.width == 720 and p.kind == 'phone']
        self.assertTrue(same_width,
                        '没有 720 宽的模拟器机型，真机将缺少同宽对照')

    def test_mobile_forms_are_wider_than_real_device_except_wearable(self):
        """手机类形态不能比真机更窄（手表/车机等其它品类不在此列）。"""
        mobile_kinds = ('phone', 'foldable', 'foldable_folded',
                        'wide_fold', 'wide_fold_folded',
                        'triple_fold', 'triple_fold_folded', 'triple_fold_double')
        mobile = [p for p in profiles() if p.kind in mobile_kinds]
        min_w = min(p.width for p in mobile)
        self.assertGreaterEqual(min_w, 720,
                                f'出现比真机更窄的手机类形态（{min_w}）')

    def test_safe_area_comes_from_wms_not_config(self):
        """★ 实测结论：安全区只能从 WindowManagerService 拿，配置里没有。

        所以静态加载出来的档案 `has_measured_safe_area` 必须都是 False ——
        如果有人往配置里填了安全区，这条会红，提醒我们数据来源变了。
        """
        for p in profiles():
            with self.subTest(name=p.name):
                self.assertFalse(
                    p.has_measured_safe_area,
                    f'{p.name} 的 safe_area 有值，但 productConfig.json 里'
                    f'并没有安全区字段 —— 数据来源变了，需要复核')


# ============================================================ 推荐形态对

class TestRecommendedPairs(unittest.TestCase):
    """导出工具按**规格**挑形态对，不硬编码设备名。

    硬编码设备名（'Mate 60'）的风险：华为改一次配置就静默失效，
    而且失效时不会报错 —— 表格还照常生成，只是里面的话不再成立。
    """

    def setUp(self):
        self.pairs = build_recommended_pairs(profiles())

    def test_produces_all_four_categories(self):
        names = [p[0] for p in self.pairs]
        self.assertEqual(names, ['同宽不同高', '折叠展开/折叠',
                                 '三折叠极值', '手机↔大屏'])

    def test_every_pair_names_real_profiles_or_real_device(self):
        """形态 B 必须点名档案库里真实存在的档案。"""
        pool = {p.name for p in profiles()}
        for name, a, b, why in self.pairs:
            with self.subTest(pair=name):
                # 形如 'Mate X6_unfolded (2240×2440)' → 取括号前的名字
                target = b.split(' (')[0].strip()
                self.assertIn(target, pool,
                              f'{name} 的形态 B {target!r} 不在档案库里')

    def test_pair_a_either_real_profile_or_real_device(self):
        pool = {p.name for p in profiles()}
        for name, a, _b, _why in self.pairs:
            target = a.split(' (')[0].strip()
            with self.subTest(pair=name):
                self.assertTrue(
                    target in pool or target.startswith('真机'),
                    f'{name} 的形态 A {target!r} 既不是档案也不是真机')

    def test_fold_pair_really_is_fold_switch(self):
        """折叠配对必须是同一设备的展开态 + 折叠态。"""
        fold_pair = next(p for p in self.pairs if p[0] == '折叠展开/折叠')
        a = fold_pair[1].split(' (')[0]
        b = fold_pair[2].split(' (')[0]
        pa, pb = find_profile(a, CFG), find_profile(b, CFG)
        self.assertEqual(pa.device, pb.device)
        self.assertFalse(pa.is_folded)
        self.assertTrue(pb.is_folded)
        self.assertLess(pb.width, pa.width)      # 折叠后更窄

    def test_every_reason_has_concrete_numbers(self):
        """每条的「测什么」都要有具体数字，不能是空话。"""
        for name, _a, _b, why in self.pairs:
            with self.subTest(pair=name):
                self.assertRegex(why, r'\d', f'{name} 的说明里没有任何数字')

    def test_empty_pool_does_not_crash(self):
        self.assertEqual(build_recommended_pairs([]), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)