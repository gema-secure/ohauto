"""A1 视觉 Provider + A2 双通道融合 + 分层成本控制 的单元测试。

不依赖真网络：OpenAICompatibleProvider 的 HTTP 出口已抽成 _post，
测试用子类覆写模拟服务端。不依赖真机：控件树用构造的 LayoutNode。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.layout import LayoutNode, Rect                     # noqa: E402
from ohauto.vision import (MockProvider, OpenAICompatibleProvider,  # noqa: E402
                           HybridLocator, TieredVisionLocator,
                           VisionConfigError, VisualTarget, build_provider,
                           _collect_hints)


def png_file(tmpdir, name='shot.png'):
    p = os.path.join(tmpdir, name)
    with open(p, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 16)   # 最小假 PNG 字节
    return p


def node(type_, id='', text='', bounds=(0, 0, 0, 0), clickable=False,
         scrollable=False, descr='', hint='', parent=None):
    n = LayoutNode(type=type_, id=id, text=text, descr=descr, hint=hint,
                   clickable=clickable, scrollable=scrollable,
                   rect=Rect(*bounds))
    if parent is not None:
        n.parent = parent
        parent.children.append(n)
    return n


def simple_page():
    root = node('root', bounds=(0, 0, 720, 1280))
    node('Button', id='btn_login', text='登录', bounds=(40, 900, 200, 960),
         clickable=True, parent=root)
    node('TextInput', id='user_input', hint='用户名', bounds=(40, 700, 400, 750),
         clickable=True, parent=root)
    return root


# ================================================================ A1

class FakeServer(OpenAICompatibleProvider):
    """覆写 _post 模拟服务端，不需要真网络。"""

    def __init__(self, reply='[]', fail=False):
        super().__init__('http://fake', 'sk-test', 'fake-model')
        self.reply = reply
        self.fail = fail
        self.last_payload = None
        self.calls = 0

    def _post(self, payload):
        self.calls += 1
        self.last_payload = payload
        if self.fail:
            raise IOError('connection reset')
        return {'choices': [{'message': {'content': self.reply}}]}


class TestA1Provider(unittest.TestCase):
    def test_parse_plain_json_array(self):
        out = OpenAICompatibleProvider._parse(
            '[{"label":"搜索","bbox":[600,40,680,110],"confidence":0.9}]')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].rect.center, (640, 75))
        self.assertAlmostEqual(out[0].confidence, 0.9)

    def test_parse_markdown_wrapped(self):
        out = OpenAICompatibleProvider._parse(
            '好的，结果如下：\n```json\n[{"label":"x","bbox":[1,2,3,4]}]\n```\n以上')
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].rect.to_dict(), {'left': 1, 'top': 2,
                                                 'right': 3, 'bottom': 4})

    def test_parse_no_json_returns_empty(self):
        self.assertEqual(OpenAICompatibleProvider._parse('找不到'), [])

    def test_parse_garbage_items_skipped(self):
        out = OpenAICompatibleProvider._parse(
            '[{"nonsense":1}, {"label":"ok","bbox":[0,0,10,10],"confidence":"0.7"}]')
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].confidence, 0.7)

    def test_from_env_missing_raises_not_silent_mock(self):
        """key 缺失必须抛错而不是静默退回 Mock —— 假装接了真模型的
        测试比没有测试更危险。"""
        # ⚠️ 清理列表必须覆盖**整条回退链**（含 OHAUTO_LLM_*）。
        # 回退链是 OHAUTO_VISION_* → OHAUTO_LLM_* → OH_LLM_*，只清第一层的话，
        # 开发机一旦配了通用 LLM 变量，from_env() 会从回退层读到值、不再抛错，
        # 这条测试就变成「本机红、CI 绿」的飘忽状态。
        saved = {k: os.environ.pop(k, None) for k in
                 ('OHAUTO_VISION_BASE_URL', 'OHAUTO_VISION_API_KEY',
                  'OHAUTO_VISION_MODEL',
                  'OHAUTO_LLM_BASE_URL', 'OHAUTO_LLM_API_KEY', 'OHAUTO_LLM_MODEL',
                  'OH_LLM_BASE_URL', 'OH_LLM_API_KEY', 'OH_LLM_MODEL')}
        try:
            with self.assertRaises(VisionConfigError):
                OpenAICompatibleProvider.from_env()
        finally:
            os.environ.update({k: v for k, v in saved.items() if v})

    def test_from_env_reads_variables(self):
        os.environ.update({'OHAUTO_VISION_BASE_URL': 'http://env-url/',
                           'OHAUTO_VISION_API_KEY': 'sk-env',
                           'OHAUTO_VISION_MODEL': 'env-model'})
        try:
            p = OpenAICompatibleProvider.from_env()
            self.assertEqual(p.base_url, 'http://env-url')  # 尾斜杠被去掉
            self.assertEqual(p.model, 'env-model')
        finally:
            for k in ('OHAUTO_VISION_BASE_URL', 'OHAUTO_VISION_API_KEY',
                      'OHAUTO_VISION_MODEL'):
                os.environ.pop(k, None)

    def test_build_provider_openai_from_env(self):
        os.environ.update({'OHAUTO_VISION_BASE_URL': 'http://x',
                           'OHAUTO_VISION_API_KEY': 'k',
                           'OHAUTO_VISION_MODEL': 'm'})
        try:
            p = build_provider('openai')
            self.assertEqual(p.name, 'openai-compatible')
        finally:
            for k in ('OHAUTO_VISION_BASE_URL', 'OHAUTO_VISION_API_KEY',
                      'OHAUTO_VISION_MODEL'):
                os.environ.pop(k, None)

    def test_no_key_in_code(self):
        """红线：实例字段来自入参/环境变量，源码里不许出现硬编码 key。"""
        src = os.path.join(ROOT, 'ohauto', 'vision.py')
        with open(src, 'r', encoding='utf-8') as f:
            code = f.read()
        for bad in ('sk-', 'Bearer sk', 'api_key = "', "api_key = '"):
            self.assertNotIn(bad, code, f'vision.py 里疑似硬编码了密钥: {bad}')

    def test_locate_sends_image_and_parses(self):
        with tempfile.TemporaryDirectory() as td:
            img = png_file(td)
            srv = FakeServer(reply='[{"label":"登录","bbox":[40,900,200,960],'
                                   '"confidence":0.88}]')
            out = srv.locate(img, '点登录按钮', 720, 1280)
            self.assertEqual(len(out), 1)
            self.assertEqual(srv.calls, 1)
            content = srv.last_payload['messages'][0]['content']
            self.assertTrue(any(c.get('type') == 'image_url' for c in content))
            url = next(c for c in content if c.get('type') == 'image_url')[
                'image_url']['url']
            self.assertTrue(url.startswith('data:image/png;base64,'))
            self.assertIn('登录', content[0]['text'])

    def test_jpeg_mime_detected(self):
        with tempfile.TemporaryDirectory() as td:
            img = png_file(td, 'shot.jpg')
            srv = FakeServer(reply='[]')
            srv.locate(img, 'x', 720, 1280)
            url = srv.last_payload['messages'][0]['content'][1][
                'image_url']['url']
            self.assertTrue(url.startswith('data:image/jpeg;base64,'),
                            'jpg 扩展名应映射到 image/jpeg')

    def test_server_failure_wrapped_as_vision_error(self):
        from ohauto.vision import VisionError
        with tempfile.TemporaryDirectory() as td:
            img = png_file(td)
            srv = FakeServer(fail=True)
            with self.assertRaises(VisionError):
                srv.locate(img, 'x', 720, 1280)


# ================================================================ A2

class TestA2HybridFusion(unittest.TestCase):
    def _provider_returning(self, rect, conf=0.8):
        class P(MockProvider):
            def locate(self, image_path, instruction, w, h, hints=None):
                return [VisualTarget(label='模型说的', rect=Rect(*rect),
                                     confidence=conf, source='vision')]
        return P()

    def test_overlap_adopts_tree_rect_and_boosts(self):
        """IoU ≥ 0.3：采用控件树精确边界 + 置信度提升 + 非 uncertain。"""
        hl = HybridLocator(provider=self._provider_returning((45, 905, 195, 955)),
                           verbose=False)
        t = hl.locate(simple_page(), 'unused.png', '登录', 720, 1280)
        self.assertIsNotNone(t)
        self.assertEqual(t.source, 'hybrid')
        self.assertEqual(t.rect.to_dict(), {'left': 40, 'top': 900,
                                            'right': 200, 'bottom': 960})
        self.assertGreaterEqual(t.confidence, 0.8)
        self.assertFalse(t.uncertain)
        self.assertGreater(t.raw.get('iou', 0), 0.3)

    def test_no_overlap_marks_uncertain_but_returns(self):
        """IoU = 0 且控件树无命中：不盲点 —— 照常返回但 uncertain=True。"""
        hl = HybridLocator(provider=self._provider_returning((600, 40, 680, 110)),
                           verbose=False)
        t = hl.locate(simple_page(), 'unused.png', '购物车图标', 720, 1280)
        self.assertIsNotNone(t)
        self.assertTrue(t.uncertain)
        self.assertEqual(t.rect.to_dict(), {'left': 600, 'top': 40,
                                            'right': 680, 'bottom': 110})

    def test_contradiction_tree_wins_deterministically(self):
        """IoU 有值但 < 0.3（视觉与树各说各话）：控件树命中可解释、
        可验证，必须确定性获胜；不返回不确定的视觉候选。"""
        hl = HybridLocator(provider=self._provider_returning((500, 900, 720, 1280)),
                           verbose=False)
        t = hl.locate(simple_page(), 'unused.png', '登录', 720, 1280)
        self.assertIsNotNone(t)
        self.assertEqual(t.source, 'tree')
        self.assertEqual(t.rect.to_dict(), {'left': 40, 'top': 900,
                                            'right': 200, 'bottom': 960})

    def test_uncertain_vision_still_marked_when_tree_wins(self):
        """树获胜时，被弃用的视觉候选也必须带上 uncertain 标记——
        上层做置信度审计时要能分清「视觉没说话」和「视觉说了但没背书」。"""
        captured = {}

        class P(MockProvider):
            def locate(self, *a, **kw):
                captured['v'] = VisualTarget(label='模型说的',
                                             rect=Rect(600, 40, 680, 110),
                                             confidence=0.8, source='vision')
                return [captured['v']]
        hl = HybridLocator(provider=P(), verbose=False)
        t = hl.locate(simple_page(), 'unused.png', '登录', 720, 1280)
        self.assertEqual(t.source, 'tree')
        self.assertTrue(captured['v'].uncertain)

    def test_vision_failure_falls_back_to_tree(self):
        class P(MockProvider):
            def locate(self, *a, **kw):
                raise RuntimeError('模型挂了')
        hl = HybridLocator(provider=P(), verbose=False)
        t = hl.locate(simple_page(), 'unused.png', '登录', 720, 1280)
        self.assertIsNotNone(t)
        self.assertEqual(t.source, 'tree')

    def test_both_miss_returns_none(self):
        class P(MockProvider):
            def locate(self, *a, **kw):
                return []
        hl = HybridLocator(provider=P(), verbose=False)
        self.assertIsNone(hl.locate(simple_page(), 'x.png', '不存在的按钮',
                                    720, 1280))


# ================================================================ A4

class StaticUniqueProvider(MockProvider):
    """L1 能唯一静态命中的场景下 provider 不应被调用。"""
    def __init__(self):
        self.calls = 0

    def locate(self, *a, **kw):
        self.calls += 1
        return []


class CountingProvider(MockProvider):
    def __init__(self, targets):
        self.targets = targets
        self.calls = 0

    def locate(self, image_path, instruction, w, h, hints=None):
        self.calls += 1
        return list(self.targets)


class TestA4Tiered(unittest.TestCase):
    def test_l1_static_unique_no_model_call(self):
        tp = StaticUniqueProvider()
        tl = TieredVisionLocator(provider=tp)
        t = tl.locate(simple_page(), 'x.png', 'btn_login', 720, 1280)
        self.assertIsNotNone(t)
        self.assertEqual(tp.calls, 0, 'L1 静态唯一命中不应调模型')
        self.assertEqual(t.source, 'tree')

    def test_l2_called_when_l1_ambiguous(self):
        # ⚠ ️ 用例数据按新 L1 口径更新（配合 A 的新 vision.py）。
        #    旧口径「静态候选必须唯一命中」→ 两候选同文案即为歧义、升级 L2。
        #    新口径「取最紧 + 最紧候选文案与指令**精确相等**才免调模型」：
        #    候选文案若与指令相等，L1 直接返回、不再升级 L2 —— 这正是免调
        #    模型率从 10% 提到 70% 的原因。故把候选文案改为**包含但不相等**
        #    （'搜索设置' ⊃ '搜索'），保住「歧义且非精确 → 升级 L2」的原意。
        root = simple_page()
        node('Button', id='btn_a', text='搜索设置', bounds=(10, 10, 60, 50),
             clickable=True, parent=root)
        node('Button', id='btn_b', text='搜索设置', bounds=(10, 60, 60, 100),
             clickable=True, parent=root)
        target = VisualTarget(label='搜索', rect=Rect(10, 10, 60, 50),
                              confidence=0.9)
        cp = CountingProvider([target])
        tl = TieredVisionLocator(provider=cp)
        t = tl.locate(root, 'x.png', '搜索', 720, 1280)
        self.assertEqual(cp.calls, 1)
        self.assertEqual(t.confidence, 0.9)

    def test_l3_fullscreen_when_l2_empty(self):
        root = simple_page()
        node('Button', id='btn_a', text='搜索', bounds=(10, 10, 60, 50),
             clickable=True, parent=root)
        node('Button', id='btn_b', text='搜索', bounds=(10, 60, 60, 100),
             clickable=True, parent=root)
        target = VisualTarget(label='全屏找到的', rect=Rect(10, 10, 60, 50),
                              confidence=0.75)
        cp = CountingProvider([target])
        tl = TieredVisionLocator(provider=cp)
        t = tl.locate(root, 'x.png', '图标长得像搜索的按钮', 720, 1280)
        self.assertEqual(cp.calls, 1)
        self.assertIsNotNone(t)

    def test_cache_hit_no_second_model_call(self):
        root = simple_page()
        target = VisualTarget(label='v', rect=Rect(1, 2, 3, 4), confidence=0.9)
        cp = CountingProvider([target])
        tl = TieredVisionLocator(provider=cp)
        r1 = tl.locate(root, 'x.png', '搜索图标', 720, 1280)
        r2 = tl.locate(root, 'x.png', '搜索图标', 720, 1280)
        self.assertEqual(cp.calls, 1)
        self.assertEqual(r1, r2)

    def test_cache_key_distinguishes_pages_and_targets(self):
        root = simple_page()
        target = VisualTarget(label='v', rect=Rect(1, 2, 3, 4), confidence=0.9)
        cp = CountingProvider([target])
        tl = TieredVisionLocator(provider=cp)
        tl.locate(root, 'x.png', '描述A', 720, 1280)            # miss
        tl.locate(root, 'x.png', '描述B', 720, 1280)            # miss（不同描述）
        tl.locate(root, 'x.png', '描述A', 720, 1280,
                  page_fingerprint='page2')                     # miss（不同页面）
        tl.locate(root, 'x.png', '描述A', 720, 1280)            # hit
        self.assertEqual(cp.calls, 3)

    def test_cache_hit_rate_and_avg_ms_stats(self):
        root = simple_page()
        target = VisualTarget(label='v', rect=Rect(1, 2, 3, 4), confidence=0.9)
        cp = CountingProvider([target])
        tl = TieredVisionLocator(provider=cp)
        for _ in range(5):
            tl.locate(root, 'x.png', '搜索图标', 720, 1280)
        s = tl.stats()
        self.assertEqual(s['model_calls'], 1)
        self.assertAlmostEqual(s['cache_hit_rate'], 0.8, places=3)
        self.assertGreaterEqual(s['avg_model_ms'], 0.0)

    def test_crop_fn_hook_used_in_l2(self):
        # 同上：候选文案改「包含但不相等」，确保仍走到 L2（真裁剪钩子）。
        root = simple_page()
        node('Button', id='btn_a', text='搜索设置', bounds=(10, 10, 60, 50),
             clickable=True, parent=root)
        node('Button', id='btn_b', text='搜索设置', bounds=(10, 60, 60, 100),
             clickable=True, parent=root)
        cropped = {'used': False}

        def fake_crop(path, region):
            cropped['used'] = True
            return path

        target = VisualTarget(label='v', rect=Rect(10, 10, 60, 50),
                              confidence=0.9)
        tl = TieredVisionLocator(provider=CountingProvider([target]),
                                 crop_fn=fake_crop)
        tl.locate(root, 'x.png', '搜索', 720, 1280)
        self.assertTrue(cropped['used'], '配置了裁剪钩子时 L2 应走真裁剪')


class HintsCollectionIsShared(unittest.TestCase):
    """两个融合定位器的候选清单必须**同源**。

    这段以前是两份逐字符相同的拷贝（`vision.py` 的 `HybridLocator._hints`
    与 `TieredVisionLocator._hints`），已经发生过漂移风险；合一之后这里钉住
    「两入口产出一致」，免得哪天只改了其中一半。
    """

    def test_both_locators_produce_identical_hints(self):
        root = simple_page()
        node('Button', id='btn_a', text='确定', bounds=(10, 10, 60, 50),
             clickable=True, parent=root)
        hy = HybridLocator(provider=MockProvider())
        tl = TieredVisionLocator(provider=MockProvider())
        self.assertEqual(hy._hints(root), tl._hints(root))
        self.assertEqual(hy._hints(root), _collect_hints(root))

    def test_hints_keep_the_visible_and_interactive_filter(self):
        """合一的时候别顺手把口径改掉：仍然只收「可见且可交互」。"""
        root = simple_page()
        node('Button', id='btn_a', text='确定', bounds=(10, 10, 60, 50),
             clickable=True, parent=root)
        node('Text', id='tv_title', text='标题', bounds=(10, 60, 60, 90),
             parent=root)
        ids = [h['id'] for h in _collect_hints(root)]
        self.assertIn('btn_a', ids)
        self.assertNotIn('tv_title', ids)


if __name__ == '__main__':
    unittest.main()
