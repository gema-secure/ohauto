"""视觉接入探索器的回归钉 —— 半盲页触发/围栏/统计/默认关闭。

设计契约见 docs/ 的「视觉接入探索器」派活单：
- 树候选 < BLIND_PAGE_THRESHOLD 且注入了定位器 → 视觉介入（每页至多一次调用）；
- 视觉 label 过安全围栏（危险文案拦截），伪节点流进既有探索循环；
- 产物规格为 {'text': label}——**无坐标**（红线）；
- 未注入定位器 → 行为与现状完全一致。
"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from ohauto.explorer import Explorer, Budget
from ohauto.sim import FakeHdc
from ohauto.driver import Driver


def _target(label, rect='[100,200][300,300]'):
    return SimpleNamespace(label=label, rect=rect, confidence=0.9)


class _StubLocator:
    """stub 定位器：固定返回目标列表，记录调用次数与缓存计数。"""

    def __init__(self, targets):
        self.targets = targets
        self.calls = 0
        self.cache_hits = 0
        self.cache_misses = 0

    def locate(self, image_path, instruction, w, h):
        # 契约 = Provider.locate(image, instruction, w, h) —— 探索器 duck-type
        # 调用的就是 Provider 形状（vision.py:221 的签名）
        self.calls += 1
        return self.targets


def _run_explore(locator, out_dir):
    hdc = FakeHdc(start_page='login', verbose=False)
    d = Driver(bundle='com.example.app', hdc=hdc, artifact_dir=out_dir,
               verbose=False, sleep_fn=lambda s: None)
    ex = Explorer(d, artifact_dir=out_dir, verbose=False,
                  vision_locator=locator)
    ex.explore(4, Budget(max_pages=4, max_actions_per_page=3),
               return_back=False)
    return ex


class TestVisionWiring(unittest.TestCase):
    """触发/围栏/统计/默认关闭。"""

    def setUp(self):
        # 把阈值临时调高，保证「半盲页」判定在 sim 页面上稳定触发
        self._orig = Explorer.BLIND_PAGE_THRESHOLD
        Explorer.BLIND_PAGE_THRESHOLD = 99
        self._tmp = tempfile.TemporaryDirectory()
        self.out = self._tmp.name

    def tearDown(self):
        Explorer.BLIND_PAGE_THRESHOLD = self._orig
        self._tmp.cleanup()

    def test_blind_page_triggers_vision_and_taps(self):
        loc = _StubLocator([_target('视觉按钮A')])
        ex = _run_explore(loc, self.out)
        self.assertGreaterEqual(ex.vision_stats['calls'], 1)
        self.assertGreaterEqual(ex.vision_stats['suggested'], 1)
        self.assertGreaterEqual(ex.vision_stats['tapped_ok'], 1)

    def test_dangerous_vision_label_is_fenced(self):
        loc = _StubLocator([_target('支付'), _target('安全按钮B')])
        ex = _run_explore(loc, self.out)
        fenced = [s for s in ex.skipped
                  if 'dangerous(vision)' in str(s.get('reason'))]
        self.assertTrue(fenced, '危险视觉候选必须进 skipped 台账')
        self.assertGreaterEqual(ex.vision_stats['suggested'], 1,
                                '安全的候选仍应放行')

    def test_no_locator_no_vision_calls(self):
        ex = _run_explore(None, self.out)
        self.assertEqual(ex.vision_stats['calls'], 0)

    def test_generated_case_has_no_coordinates(self):
        loc = _StubLocator([_target('视觉按钮A')])
        ex = _run_explore(loc, self.out)
        f = os.path.join(self.out, 'vis_case.yaml')
        ex.generate_case(f)
        with open(f, encoding='utf-8') as fh:
            content = fh.read()
        self.assertNotIn('tap_xy', content)
        self.assertNotIn('[100,200]', content, '视觉框坐标不许进产物')


def _case_path(ex):
    import glob
    hits = glob.glob(ex.artifact_dir + '/**/*case*.yaml', recursive=True)
    return hits[0] if hits else ex.generate_case(
        ex.artifact_dir + '/vis_case.yaml')


if __name__ == '__main__':
    unittest.main(verbosity=2)
