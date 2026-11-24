"""系统弹窗处置的钉子 —— 「前台不是被测应用时，先把弹窗关掉」。

真机实测踩到的形状：
`uitest dumpLayout` 只抓**最上层窗口**。一条系统弹窗（USB 连接方式、
权限确认）盖住应用后，抓回来的树里全是弹窗节点 —— 被测应用明明在跑，
从树上看却是「空白」；动作注入又是按坐标点的，点击被弹窗吃掉，
而调用方拿到的仍是「点击成功」。

这组钉子守三件事：
1. 认得弹窗的「关掉」按钮（id 前缀优先，文案兜底）；
2. 只在前台不是被测应用时才动手 —— 正常路径一次点击都不能多发；
3. HdcLike 没有前台校验能力时静默跳过，**不能误点**。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import Driver                       # noqa: E402
from ohauto.layout import LayoutNode, Rect             # noqa: E402
from ohauto.sim import FakeHdc                         # noqa: E402

BUNDLE = 'com.example.myapplication'


def _node(typ, cid='', text='', x=10, y=10, clickable=False):
    return LayoutNode(type=typ, id=cid, text=text, clickable=clickable,
                      rect=Rect(x, y, 200, 60))


def _wrap(*children):
    root = LayoutNode(type='Column', attributes={'bundleName': BUNDLE},
                      rect=Rect(0, 0, 720, 1280))
    for c in children:
        c.parent = root
        root.children.append(c)
    return root


class FindButtonTest(unittest.TestCase):
    """按钮识别：id 前缀优先，文案兜底。"""

    def test_by_id_prefix(self):
        tree = _wrap(_node('Button', 'advanced_dialog_button_0', '确定'))
        hit = Driver._find_dialog_button(tree)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.id, 'advanced_dialog_button_0')

    def test_by_text_when_id_absent(self):
        tree = _wrap(_node('Button', '', '允许'))
        hit = Driver._find_dialog_button(tree)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.text, '允许')

    def test_nested_button_found(self):
        inner = _node('Button', 'advanced_dialog_button_1', '确定')
        mid = _node('Column')
        mid.children.append(inner)
        tree = _wrap(mid)
        self.assertIsNotNone(Driver._find_dialog_button(tree))

    def test_plain_page_returns_none(self):
        tree = _wrap(_node('Button', 'btn_go_second', '进入第二页', clickable=True),
                     _node('Text', 'tv_probe_always', '探针A：无条件渲染'))
        self.assertIsNone(Driver._find_dialog_button(tree))


class DismissTest(unittest.TestCase):
    """处置：前台不是应用 → 点弹窗；已经是应用 → 一下都不点。"""

    def _driver(self, hdc, tree):
        d = Driver(bundle=BUNDLE, hdc=hdc, verbose=False, artifact_dir=None)
        d.refresh = lambda unfiltered=False: tree      # 注入树，绕开设备
        d._sleep = lambda seconds: None                # 测试不真等
        return d

    def test_closes_dialog_then_stops(self):
        hdc = FakeHdc()
        clicks = []
        real_shell = hdc.shell

        def shell(cmd, **kw):
            if 'uiInput click' in cmd:
                clicks.append(cmd)
            return real_shell(cmd, **kw)

        hdc.shell = shell
        # 前两轮前台是弹窗，第三轮回到应用
        seq = [(False, 'dialog'), (False, 'dialog'), (True, 'app')]
        hdc.foreground_is = lambda bundle: seq.pop(0) if seq else (True, 'app')
        tree = _wrap(_node('Button', 'advanced_dialog_button_0', '确定', x=100, y=700))

        closed = self._driver(hdc, tree).dismiss_dialogs()

        self.assertEqual(closed, 2)
        self.assertEqual(len(clicks), 2)
        self.assertIn('uiInput click 150 380', clicks[0])   # Rect(left,top,right,bottom) 的中心

    def test_app_already_foreground_no_click(self):
        hdc = FakeHdc()
        clicks = []
        real_shell = hdc.shell

        def shell(cmd, **kw):
            if 'uiInput click' in cmd:
                clicks.append(cmd)
            return real_shell(cmd, **kw)

        hdc.shell = shell
        hdc.foreground_is = lambda bundle: (True, 'app')
        tree = _wrap(_node('Button', 'advanced_dialog_button_0', '确定'))

        self.assertEqual(self._driver(hdc, tree).dismiss_dialogs(), 0)
        self.assertEqual(clicks, [])

    def test_unknown_foreground_without_button_does_not_guess(self):
        hdc = FakeHdc()
        clicks = []
        real_shell = hdc.shell

        def shell(cmd, **kw):
            if 'uiInput click' in cmd:
                clicks.append(cmd)
            return real_shell(cmd, **kw)

        hdc.shell = shell
        hdc.foreground_is = lambda bundle: (False, 'launcher(pid=1)')
        tree = _wrap(_node('Text', 'tv_probe_always', '探针A：无条件渲染'))

        self.assertEqual(self._driver(hdc, tree).dismiss_dialogs(), 0)
        self.assertEqual(clicks, [])

    def test_no_foreground_probe_silently_skips(self):
        """HdcLike 没有 foreground_is 时不能抛，也不能点。"""
        class Bare:
            pass

        hdc = Bare()
        d = Driver(bundle=BUNDLE, hdc=hdc, verbose=False, artifact_dir=None)
        self.assertEqual(d.dismiss_dialogs(), 0)


if __name__ == '__main__':
    unittest.main()
