# -*- coding: utf-8 -*-
"""层级路径（L3）打分必须计入子树文案。

真机形态：可交互性在容器上、文案在子节点上 —— 容器自身既无 id 也无文案。
这类同类型、同尺寸的兄弟，`control_key` 会算出同一个值（该 key 刻意不含
坐标），打分因此相同；而 L3 的纪律是「唯一最高分才认」，不把子树文案计
进去，它们就整组落空 —— 真机上六成到八成的可交互控件属于这一类。

本文件钉两件事：

1. 同类型同尺寸的无标识兄弟，靠子树文案必须能唯一定位到目标那一个；
2. 子树文案也相同或为空时，仍然按纪律落空 —— 不许硬猜。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.identity import control_key, type_fingerprint       # noqa: E402
from ohauto.layout import parse_layout                          # noqa: E402
from ohauto.locator import LocatorManager, LocatorSpec          # noqa: E402


def _b(l, t, r, bo):
    return f'[{l},{t}][{r},{bo}]'


def _text(label, bounds):
    return {'attributes': {'type': 'Text', 'id': '', 'text': label,
                           'bounds': bounds, 'visible': 'true',
                           'enabled': 'true', 'clickable': 'false'}}


def _item(l, t, r, bo, label):
    """可交互容器（ListItem）+ 一个子节点文案 —— 真机的标准形态。"""
    return {'attributes': {'type': 'ListItem', 'id': '', 'text': '',
                           'bounds': _b(l, t, r, bo), 'visible': 'true',
                           'enabled': 'true', 'clickable': 'false'},
            'children': [_text(label, _b(l + 20, t + 20, r - 20, bo - 20))]}


def _item_no_child(l, t, r, bo):
    """容器下没有任何文案 —— 用于「确实无区分线索」那一档。"""
    return {'attributes': {'type': 'ListItem', 'id': '', 'text': '',
                           'bounds': _b(l, t, r, bo), 'visible': 'true',
                           'enabled': 'true', 'clickable': 'false'}}


def _page(items):
    return {'attributes': {'type': 'root', 'id': 'root',
                           'bounds': _b(0, 0, 720, 1280), 'visible': 'true'},
            'children': [{'attributes': {'type': 'List', 'id': '',
                                         'bounds': _b(0, 72, 720, 1208),
                                         'visible': 'true', 'enabled': 'true'},
                          'children': items}]}


def _ancestors(node):
    out, p = [], node.parent
    while p is not None:
        out.append(p)
        p = p.parent
    return out


def _spec_of(node):
    """按 LocatorManager.register 的字段重建定位规格。"""
    spec = LocatorSpec(locator_id='t', description='t', target_type=node.type)
    spec.type_fp = type_fingerprint(node)
    spec.control_key = control_key(node)
    spec.text_deep = node.text_deep
    spec.parent_types = tuple(p.type for p in _ancestors(node))
    return spec


def _items(page):
    return [n for n in page.walk() if n.type == 'ListItem']


THREE = [_item(0, 100, 700, 200, '蓝牙 已关闭'),
         _item(0, 200, 700, 300, '移动网络'),
         _item(0, 300, 700, 400, '应用')]


class TestLvPathTypeUsesTextDeep(unittest.TestCase):

    def test_brothers_share_one_control_key(self):
        """前提：这几个兄弟本来就同分，否则测不到目标行为。"""
        items = _items(parse_layout(_page(THREE)))
        self.assertEqual(len(items), 3)
        self.assertEqual(len({control_key(n) for n in items}), 1,
                         '前提不成立：这三个兄弟的 control_key 本来就不同')
        for n in items:
            self.assertEqual((n.text or '').strip(), '',
                             '夹具没按真机形态造：容器自身不该有文案')

    def test_each_brother_is_uniquely_located(self):
        """核心：子树文案不同，L3 必须各自唯一定位到目标那一个。"""
        page = parse_layout(_page(THREE))
        lm = LocatorManager()
        for node in _items(page):
            got, level = lm._lv_path_type(_spec_of(node), page)
            self.assertIs(got, node, f'子树文案 {node.text_deep!r} 没能唯一定位')
            self.assertEqual(level, 'path_type')

    def test_identical_text_deep_still_refuses_to_guess(self):
        """线索确实相同就该落空，交给下一级 —— 不许挑第一个充数。"""
        page = parse_layout(_page([
            _item(0, 100, 700, 200, '相同文案'),
            _item(0, 200, 700, 300, '相同文案'),
        ]))
        items = _items(page)
        got, _ = LocatorManager()._lv_path_type(_spec_of(items[0]), page)
        self.assertIsNone(got, '两个同分候选说明线索不够，必须落空')

    def test_empty_text_deep_still_refuses_to_guess(self):
        """容器下没有任何文案时，同样不许硬猜。"""
        page = parse_layout(_page([
            _item_no_child(0, 100, 700, 200),
            _item_no_child(0, 200, 700, 300),
        ]))
        items = _items(page)
        got, _ = LocatorManager()._lv_path_type(_spec_of(items[0]), page)
        self.assertIsNone(got, '没有任何区分线索时必须落空')

    def test_own_id_still_wins_through_the_id_level(self):
        """回归护栏：有 id 的控件走的仍是前几级，不受本级改动影响。"""
        page = parse_layout(_page([_item(0, 100, 700, 200, '蓝牙 已关闭')]))
        item = _items(page)[0]
        item.id = 'item_bluetooth'
        spec = _spec_of(item)
        spec.target_id = 'item_bluetooth'
        self.assertIs(LocatorManager()._lv_id_exact(spec, page), item)


if __name__ == '__main__':
    unittest.main(verbosity=2)
