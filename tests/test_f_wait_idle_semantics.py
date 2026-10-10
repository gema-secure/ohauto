"""wait_idle 判稳语义的钉子。

159988c 之后，wait_idle 的判稳证据从「动作后连看两眼一致」变为
「动作前基线 + 一次动作后采样一致」。审阅裁定：有条件通过 —— 条件是
把依赖关系钉死在测试里：

    wait_idle 的「一次采样即判稳」之所以安全，是因为**页面状态决策**
    （explorer._page_signatures 等）全部走无条件 refresh，晚到跳转
    （带外自变）在那里被兜住。本文件两条测试分别钉住因果链的两端：

    1. driver 端：第一眼与动作前一致 → wait_idle 返回 True（判稳只负责
       「此刻稳」，一次新鲜采样即可）；
    2. explorer 端：随后带外页面变更 → _page_signatures 必须真重取
       （dump 计数 +1、签名反映新页面），不许吃 driver 的缓存树。

    若将来有人改 _tree_dirty 语义或给 _page_signatures 加缓存，
    这两条会红 —— 那时必须同时改 wait_idle 的种子策略（见其 docstring
    的反向依赖声明），不能只改一半。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import Driver                     # noqa: E402
from ohauto.hdc import ShellResult                   # noqa: E402
from ohauto.explorer import Explorer, build_page_signature  # noqa: E402


def _tree(btn_id: str, text: str) -> str:
    """最小合法布局：根 + 一个按钮。id/text 不同 → 签名必不同。"""
    import json
    return json.dumps({
        'attributes': {'type': 'Root', 'bounds': '[0,0][1080,2340]'},
        'children': [
            {'attributes': {'type': 'Button', 'id': btn_id, 'text': text,
                            'bounds': '[100,100][400,200]'},
             'children': []},
        ],
    })


class ScriptedHdc:
    """按脚本顺序返回 dumpLayout 结果的假 hdc —— 最后一个布局无限重复。

    `flip_after`：第 N 次 dump 之后切换到第二套布局，模拟「带外自变」
    （页面在 driver 不知情时自己变了）。
    """

    tmp_dir = '/data/local/tmp'

    def __init__(self, first: str, second: str, flip_after: int):
        self._first, self._second = first, second
        self._flip_after = flip_after
        self.dump_calls = 0

    def shell(self, cmd: str, check: bool = False) -> ShellResult:
        if 'dumpLayout' in cmd:
            n = self.dump_calls
            self.dump_calls += 1
            out = self._first if n < self._flip_after else self._second
            return ShellResult(0, out, '', cmd)
        raise AssertionError(f'ScriptedHdc 未预期收到命令: {cmd}')


class TestWaitIdleSemantics(unittest.TestCase):

    def setUp(self):
        # X / Y 两棵树签名必然不同（type|id|text 全不同）。
        self.tree_x = _tree('btn_a', 'A')
        self.tree_y = _tree('btn_b', 'B')
        # 前两次 dump 都是 X（基线 + 动作后第一眼），之后设备自己变成 Y。
        self.hdc = ScriptedHdc(self.tree_x, self.tree_y, flip_after=2)
        self.d = Driver(bundle='com.demo.app', hdc=self.hdc, verbose=False)

    def test_first_look_matching_baseline_is_stable(self):
        """钉子 1（driver 端）：动作后第一眼与动作前一致 → 判稳，只采一次。

        语义口径：判稳只负责「此刻稳」——基线（动作前）+ 一次新鲜采样
        一致即返回 True。页面稍后才变属于带外自变，不由本函数兜底
        （由 explorer 的无条件 refresh 兜底，见钉子 2）。
        """
        self.d.refresh()                                   # dump#1 → 基线 X
        dumps_before = self.hdc.dump_calls
        self.assertTrue(self.d.wait_idle(stable_rounds=2, interval=1,
                                         timeout=1000))
        # 恰好 1 次新鲜采样：基线复用，不额外取树。若有人改回「连看两眼」
        # 或把种子弄丢导致多取，这条都会红 —— 语义变更必须过这道闸。
        self.assertEqual(self.hdc.dump_calls, dumps_before + 1)

    def test_explorer_refetches_after_outofband_change(self):
        """钉子 2（explorer 端）：判稳后页面带外自变 → 签名必须真重取。

        这是 wait_idle 种子策略安全性的另一半：哪怕 wait_idle 在带外
        变更前一刻返回 True，页面签名也不吃缓存树。砍掉
        _page_signatures 的无条件 refresh，本条立刻红（159988c 试过，
        test_explorer_b1 两条同样红）。
        """
        ex = Explorer(self.d, verbose=False)
        self.d.refresh()                                   # 基线 X
        baseline = ex._page_signatures()
        self.d.wait_idle(stable_rounds=2, interval=1, timeout=1000)
        # 此刻（flip_after=2 之后）设备已带外切到 Y。
        dumps_before = self.hdc.dump_calls
        now = ex._page_signatures()
        # 相对计数：签名这一次调用恰好真取 1 次（无条件 refresh），
        # 复用缓存树则不会增加 —— 这是本钉子要守的核心。
        self.assertEqual(self.hdc.dump_calls, dumps_before + 1,
                         '页面签名必须真重取，不得复用')
        self.assertNotEqual(now.content, baseline.content,
                            '带外变更后的签名必须反映新页面')
        self.assertEqual(now.content,
                         build_page_signature(self.d.root).content,
                         '签名必须来自刚重取的树')


if __name__ == '__main__':
    unittest.main()
