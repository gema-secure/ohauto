"""截图分级留存 —— 钉住「必留/可省/省略留痕」三条口径（docs/截图分级留存策略.md）。

判定输入由调用方传入（explorer 知道新页，调用方知道降级），driver 不猜；
但 driver 自己能观察到的失败不归调用方管：跳了前置图的失败步必须补失败现场。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import Driver                       # noqa: E402
from ohauto.matcher import ON                          # noqa: E402
from ohauto.sim import FakeHdc                         # noqa: E402
from ohauto.explorer import Explorer, Budget           # noqa: E402


def _no_sleep(_s: float) -> None:
    pass


class ScreenshotTierCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ohauto_shottier_')
        self.sim = FakeHdc(start_page='login')
        self.d = Driver(bundle='com.demo.app', hdc=self.sim,
                        artifact_dir=self.tmp, verbose=False,
                        sleep_fn=_no_sleep)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _png_files(self):
        return sorted(f for f in os.listdir(self.tmp) if f.endswith('.png'))


class TestTapKeepShot(ScreenshotTierCase):

    def test_default_keeps_before_shot(self):
        """keep_shot 缺省 = 现行口径：动作前图照留（直接调用方零迁移）。"""
        self.d.tap(ON.text('登录'))
        step = self.d.steps[-1]
        self.assertTrue(step.screenshot and os.path.exists(step.screenshot))
        self.assertFalse(step.shot_skipped)
        self.assertEqual(self.d.shots_saved, 1)
        self.assertEqual(self.d.shots_skipped, 0)

    def test_keep_shot_false_skips_and_marks(self):
        """调用方判「不必拍」→ 不落盘，但 Step 必须留痕省略。"""
        before = set(os.listdir(self.tmp))
        self.d.tap(ON.text('登录'), keep_shot=False)
        step = self.d.steps[-1]
        self.assertIsNone(step.screenshot)
        self.assertTrue(step.shot_skipped)
        self.assertEqual(self._png_files(), [], '省略时不得产出截图文件')
        self.assertTrue(before.issuperset(os.listdir(self.tmp)))
        self.assertEqual((self.d.shots_saved, self.d.shots_skipped), (0, 1))
        self.assertNotIn('screenshot', step.to_dict())
        self.assertIn('shot_skipped', step.to_dict())

    def test_keep_shot_true_explicit(self):
        self.d.tap(ON.text('登录'), keep_shot=True)
        self.assertEqual((self.d.shots_saved, self.d.shots_skipped), (1, 0))

    def test_failed_tap_gets_evidence_shot_even_when_skipped(self):
        """失败必留：跳了前置图的失败步，driver 兜底补失败现场。"""
        with self.assertRaises(Exception):
            self.d.tap(ON.text('不存在'), timeout=100, keep_shot=False)
        step = self.d.steps[-1]
        self.assertFalse(step.ok)
        self.assertTrue(step.screenshot and os.path.exists(step.screenshot),
                        '失败步必须有失败现场图')
        self.assertEqual((self.d.shots_saved, self.d.shots_skipped), (1, 0))

    def test_failed_tap_without_skip_still_single_shot(self):
        """定位失败发生在截图分支之前 → 失败步只补一张现场图，不叠加。"""
        with self.assertRaises(Exception):
            self.d.tap(ON.text('不存在'), timeout=100)
        step = self.d.steps[-1]
        self.assertTrue(step.screenshot.endswith('fail_tap.png'))
        self.assertEqual(self.d.shots_saved, 1)

    def test_no_artifact_dir_makes_keep_shot_moot(self):
        """没配 artifact_dir 时一切照旧：不落盘、不计数。"""
        d = Driver(bundle='com.demo.app', hdc=FakeHdc(start_page='login'),
                   artifact_dir=None, verbose=False, sleep_fn=_no_sleep)
        d.tap(ON.text('登录'), keep_shot=False)
        self.assertEqual((d.shots_saved, d.shots_skipped), (0, 0))
        self.assertFalse(d.steps[-1].shot_skipped)


class TestExplorerTiers(ScreenshotTierCase):

    def test_first_arrival_kept_repeats_skipped(self):
        """探索口径：新页首图与起始页前置图在场，已留档页的重复交互为省略。"""
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        ex.explore(5, Budget(max_pages=5, max_actions_per_page=6,
                             max_seconds=60, max_steps=100))
        self.assertEqual(len(ex.graph.states), 3)
        # 起始页无页面图（登记时未截），其余新页首图必留且文件在场
        shots = [s.screenshot for s in ex.graph.states.values()]
        self.assertIsNone(shots[0], '起始页按现状不登记页面图')
        for p in shots[1:]:
            self.assertTrue(p and os.path.exists(p), f'新页首图缺失: {p}')
        # 省略必须发生且留痕：已留档页上的后续点击
        self.assertGreaterEqual(self.d.shots_skipped, 1)
        skipped_steps = [s for s in self.d.steps if s.shot_skipped]
        self.assertTrue(skipped_steps)
        for s in skipped_steps:
            self.assertIsNone(s.screenshot)
        # 留存也必须发生：起始页前置图 + 新页首图
        self.assertGreaterEqual(self.d.shots_saved, 2)

    def test_backtrack_taps_skip_before_shot(self):
        """回溯重放属已留档页面的重复交互 → 前置图省略；失败由补图兜底。"""
        ex = Explorer(self.d, artifact_dir=self.tmp, verbose=False)
        ex.explore(5, Budget(max_pages=5, max_actions_per_page=6,
                             max_seconds=60, max_steps=100))
        target = next(s for s in ex.graph.states.values() if s.path)
        ok = ex._navigate_to(target)
        self.assertTrue(ok)
        replay = [s for s in self.d.steps
                  if s.index > 0 and s.kind == 'tap'][-len(target.path):]
        for s in replay:
            self.assertTrue(s.shot_skipped, '回溯步必须省前置图')
            self.assertTrue(s.ok)


class TestAccounting(ScreenshotTierCase):

    def test_summary_exposes_both_counters(self):
        self.d.tap(ON.text('登录'))                       # -> home
        self.d.tap(ON.text('退出登录'), keep_shot=False)   # -> login
        s = self.d.summary()
        self.assertEqual(s['screenshots_saved'], 1)
        self.assertEqual(s['screenshots_skipped'], 1)

    def test_screenshot_counts_into_saved(self):
        self.d.screenshot('page.png')
        self.assertEqual(self.d.shots_saved, 1)


if __name__ == '__main__':
    unittest.main()
