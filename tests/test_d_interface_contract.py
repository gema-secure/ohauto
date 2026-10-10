"""1D 接口形式化的机器校验 —— 契约成立，隐藏状态消失。

本章钉住 1D 的两条口径：

* **S10** `HdcLike` 是**真**契约：`FakeHdc` 与真 `Hdc` 都满足它，且它
  不是空壳（少一个成员就过不了）；
* **S11** 跨调用存活的隐藏状态没了：`TieredVisionLocator._all_hints_cache`
  与模块级 `_OCR_VENV_ERR` 都已收进调用域。
"""
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

from ohauto import fusion
from ohauto.hdc import Hdc, HdcLike
from ohauto.layout import parse_layout
from ohauto.sim import FakeHdc
from ohauto.vision import MockProvider, TieredVisionLocator

#: 真 `Hdc` 构造只做「路径非空」判断，不 stat —— 传假路径即可，不会碰设备。
FAKE_HDC_PATH = os.path.join(tempfile.gettempdir(), 'ohauto-no-such-hdc.exe')

#: 最小页面：一个可交互容器 + 一个子文本。文案在子节点上（真机形态），
#: 于是 deep='搜索设置' 能被 '搜索' 命中静态候选，而精确相等判定不成立
#: → 必然落到 L2 分支，`_region_context` 被真正调到。
PAGE = {
    'attributes': {'type': 'Root', 'bounds': '[0,0][720,1280]'},
    'children': [
        {'attributes': {'type': 'Flex', 'id': 'item_search', 'text': '',
                        'clickable': 'true', 'visible': 'true',
                        'bounds': '[0,200][720,300]'},
         'children': [
             {'attributes': {'type': 'Text', 'text': '搜索设置',
                             'bounds': '[40,220][300,250]', 'visible': 'true'},
              'children': []},
         ]},
    ],
}


class HdcLikeContract(unittest.TestCase):
    """契约的双方都得过，且它得管得住事。"""

    def test_fakehdc_satisfies_the_contract(self):
        self.assertIsInstance(FakeHdc(), HdcLike)

    def test_real_hdc_satisfies_the_contract(self):
        self.assertIsInstance(Hdc(hdc_path=FAKE_HDC_PATH), HdcLike)

    def test_contract_rejects_a_random_object(self):
        """防「协议写空了恒为真」—— 空壳协议是比没有协议更坏的假安全感。"""
        self.assertNotIsInstance(object(), HdcLike)

    @unittest.skipIf(sys.version_info < (3, 12),
                     '3.9~3.11 的 runtime_checkable isinstance 不检查数据成员')
    def test_missing_tmp_dir_fails_the_contract(self):
        """`tmp_dir` 是契约的一部分，不是可有可无的装饰。

        它正是 `Driver.refresh()` 过去那条 getattr 兜底链存在的唯一原因；
        如果少一个属性还能过检，兜底就不该删。
        """
        obj = FakeHdc()
        self.assertIsInstance(obj, HdcLike)          # 对照：完整对象通过
        del obj.tmp_dir                              # 去掉契约要求的属性
        self.assertNotIsInstance(obj, HdcLike)


class RegionContextTakesHintsExplicitly(unittest.TestCase):

    def setUp(self):
        self.loc = TieredVisionLocator(provider=MockProvider())
        self.root = parse_layout(PAGE)
        self.hints = self.loc._hints(self.root)
        self.anchors = [h for h in self.hints if h.get('id') == 'item_search']
        self.assertTrue(self.anchors, '夹具没造出锚点')

    def test_hints_come_from_the_argument(self):
        """清单从参数来：传空就真的只剩锚点，说明没偷读别处的状态。"""
        self.assertEqual(self.loc._region_context(self.anchors, []),
                         self.anchors)

    def test_anchor_stays_first(self):
        ctx = self.loc._region_context(self.anchors, self.hints)
        self.assertIs(ctx[0], self.anchors[0])

    def test_locate_leaves_no_hidden_hint_cache(self):
        """走完 L2 分支后实例上不该多出 `_all_hints_cache`。"""
        reached = []
        original = self.loc._region_context

        def spy(anchors, hints):
            reached.append(hints)
            return original(anchors, hints)

        self.loc._region_context = spy
        self.loc.locate(self.root, 'shot.png', '搜索', 720, 1280)
        self.assertTrue(reached, '夹具没走到 L2 分支，这条断言就白测了')
        self.assertFalse(hasattr(self.loc, '_all_hints_cache'))


class OcrTraceIsCallScoped(unittest.TestCase):
    """OCR 失败轨迹跟着调用走，不再住模块全局。"""

    _Proc = namedtuple('_Proc', 'returncode stdout stderr')

    def setUp(self):
        # 只要「文件存在」这一条：source_ocr 的入口守卫只做 isfile。
        fd, self.png = tempfile.mkstemp(suffix='.png')
        os.close(fd)
        self.addCleanup(os.remove, self.png)

    def test_module_level_error_global_is_gone(self):
        self.assertFalse(hasattr(fusion, '_OCR_VENV_ERR'))

    def test_venv_failure_records_into_the_given_trace(self):
        trace = fusion._OcrTrace()
        with mock.patch.object(fusion, '_VENV_PY', sys.executable), \
             mock.patch('subprocess.run',
                        return_value=self._Proc(1, '', 'backend=rapidocr\nboom')):
            self.assertIsNone(
                fusion._ocr_via_venv('D:/shot.png', 'ocr', trace=trace))
        self.assertTrue(trace.errors)
        self.assertIn('boom', trace.last)

    def test_trace_is_optional_for_direct_callers(self):
        """不传 trace 时行为不变 —— 既有直接调用方（本套测试）不受影响。"""
        with mock.patch.object(fusion, '_VENV_PY', sys.executable), \
             mock.patch('subprocess.run',
                        return_value=self._Proc(1, '', 'backend=rapidocr\nboom')):
            self.assertIsNone(fusion._ocr_via_venv('D:/shot.png', 'ocr'))

    def test_consecutive_calls_do_not_share_failure_state(self):
        """两次调用的失败原因不串味。

        模块级全局时代这件事要靠每次开头的 `clear()` 补救；改成调用域对象
        之后它从根上不可能发生。这里把「不可能」钉成断言。
        """
        traces = []

        def fake_venv(image_path, name, trace=None):
            traces.append(trace)
            if trace is not None:
                trace.add('第 %d 次' % len(traces))
            return None

        # winsdk / pytesseract 都断掉，才会一路降级到最终那条 reason。
        with mock.patch.dict(sys.modules, {'winsdk': None, 'pytesseract': None}), \
             mock.patch.object(fusion, '_ocr_via_venv', fake_venv):
            first = fusion.source_ocr(self.png, backend='auto')
            second = fusion.source_ocr(self.png, backend='auto')

        self.assertIn('第 1 次', first.reason)
        self.assertIn('第 2 次', second.reason)
        self.assertNotIn('第 1 次', second.reason)
        self.assertIsNot(traces[0], traces[1])


if __name__ == '__main__':
    unittest.main()
