# -*- coding: utf-8 -*-
"""真机结构：**可交互性在容器上、文案在子节点上**。

这条是合并 B 的交付包时、拿 9/19 采集的真机样本核出来的：

    设备自带「设置」页，12 个可交互控件
        自身带文案           0 个
        自身无文案、子节点有  12 个       ← 100%

真实形态::

    Flex (clickable=true, text='')
     ├ Text (text='蓝牙')
     └ Text (text='已关闭')

**为什么必须钉住**：模拟夹具里 `clickable` 和 `text` 常写在同一个节点上
（干净、好写），真机却是分开的 —— 于是所有「按文案判断这个控件是干什么的」
逻辑，在模拟设备上全绿、到真机上**全部静默失效**：

- 安全策略的「删除/支付」拦截
- 探索器的关键词加权（B2 的「登录/搜索/提交」提权）
- 压测目标挑选（`pick_stress_target` 的语义关键词表）

三处失效都不会报错，只是「什么都没匹配到」—— 最难发现的那一类。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import parse_layout                        # noqa: E402

#: 真机样本（设备自带「设置」页）
REAL_SAMPLES = os.path.join(ROOT, 'datasets', 'gallery_13app')


def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _node(type_, cid, text, bounds, clickable='false', **extra):
    a = {'type': type_, 'id': cid, 'text': text, 'bounds': bounds,
         'clickable': clickable, 'visible': 'true', 'enabled': 'true'}
    a.update(extra)
    return {'attributes': a}


def _tree(children, root_type='Root', root_extra=None):
    a = {'type': root_type, 'id': 'root', 'bounds': _b(0, 0, 720, 1280),
         'visible': 'true'}
    if root_extra:
        a.update(root_extra)
    return {'attributes': a, 'children': children}


#: 真机形态：可交互容器自身没文案，文案在两个子节点上
REAL_SHAPE = _tree([
    {'attributes': {'type': 'Flex', 'id': 'item_bluetooth', 'text': '',
                    'bounds': _b(0, 200, 720, 300), 'clickable': 'true',
                    'visible': 'true'},
     'children': [
         _node('Text', '', '蓝牙', _b(40, 220, 200, 250)),
         _node('Text', '', '已关闭', _b(560, 220, 690, 250)),
     ]},
    {'attributes': {'type': 'Flex', 'id': 'item_display', 'text': '',
                    'bounds': _b(0, 300, 720, 400), 'clickable': 'true',
                    'visible': 'true'},
     'children': [_node('Text', '', '显示与亮度', _b(40, 320, 300, 350))]},
    # 危险项：文案同样在子节点上 —— 这是安全策略拦不住的那类
    {'attributes': {'type': 'Flex', 'id': 'item_reset', 'text': '',
                    'bounds': _b(0, 500, 720, 600), 'clickable': 'true',
                    'visible': 'true'},
     'children': [_node('Text', '', '恢复出厂设置', _b(40, 520, 300, 550))]},
])


class TestTextDeep(unittest.TestCase):

    def setUp(self):
        self.page = parse_layout(REAL_SHAPE)
        self.items = [n for n in self.page.walk()
                      if getattr(n, 'clickable', False)]

    def test_interactive_container_has_no_own_text(self):
        """前提：真机上可交互容器自身的 text 是空的。"""
        self.assertTrue(self.items)
        for n in self.items:
            self.assertEqual((n.text or '').strip(), '',
                             '夹具没按真机形态造 —— 那样这条测试就白测了')

    def test_text_deep_collects_from_children(self):
        labels = [n.text_deep for n in self.items]
        self.assertIn('蓝牙 已关闭', labels)
        self.assertIn('显示与亮度', labels)

    def test_label_no_longer_degrades_to_type(self):
        """★ 核心：label 不该退化成一个干巴巴的 'Flex'。"""
        for n in self.items:
            self.assertNotEqual(n.label, 'Flex',
                                'label 退化成了类型名 —— 日志和视觉提示会失去信息')
            self.assertTrue(n.label.strip())

    def test_own_text_still_wins(self):
        """自身有文案时用自身（更精确），不要被子树污染。"""
        tree = _tree([_node('Button', 'b1', '登录', _b(100, 100, 300, 160),
                            clickable='true')])
        btn = [n for n in parse_layout(tree).walk()
               if getattr(n, 'clickable', False)][0]
        self.assertEqual(btn.text_deep, '登录')
        self.assertEqual(btn.label, '登录')

    def test_danger_text_is_now_visible(self):
        """危险项「恢复出厂设置」在真机形态下也要能被看到。

        改前：只看 `node.text` → 空 → 安全策略拦不住 → 点下去就出事了。
        """
        danger = ('删除', '支付', '注销', '卸载', '恢复出厂', '清空')
        visible = [n.label for n in self.items
                   if any(k in n.text_deep for k in danger)]
        self.assertTrue(visible, '真机形态下危险控件的文案取不到 —— 安全策略会失效')
        self.assertIn('恢复出厂设置', visible)


class TestRealDeviceSamples(unittest.TestCase):
    """对 9/19 采集的真机样本跑一遍（没有样本就跳过，不当成失败）。"""

    def _sample(self, name):
        p = os.path.join(REAL_SAMPLES, name)
        if not os.path.isfile(p):
            self.skipTest(f'真机样本不在: {p}')
        with open(p, encoding='utf-8') as f:
            return parse_layout(json.load(f))

    def test_settings_page_all_interactive_have_meaningful_label(self):
        page = self._sample('app_settings.json')
        items = [n for n in page.walk() if getattr(n, 'clickable', False)]
        if not items:
            self.skipTest('样本里没有可交互控件')
        degraded = [n for n in items if n.label == n.type]
        self.assertLessEqual(
            len(degraded), len(items) * 0.2,
            f'{len(degraded)}/{len(items)} 个控件的 label 退化成了类型名')

    def test_calculator_buttons_still_have_ids(self):
        """计算器那些按钮没有文案但有 id —— label 应该退到 id，不是 type。"""
        page = self._sample('sample_calc.json')
        items = [n for n in page.walk() if getattr(n, 'clickable', False)]
        if not items:
            self.skipTest('样本里没有可交互控件')
        with_id = [n for n in items if n.id]
        if with_id:
            self.assertTrue(all(n.label for n in with_id))


if __name__ == '__main__':
    unittest.main(verbosity=2)
