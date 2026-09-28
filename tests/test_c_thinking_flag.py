"""`tools/eval_vision_offline.py` 的 `disable_thinking` 透传钉子。

★ 背景（2026-09-27，评审高危遗留第 7 条）：
基类 `OpenAICompatibleProvider.from_env(**kw)` 只挑 base_url / api_key /
model / timeout，**其余 kwarg 被静默吞掉**。`--read-page` / `--page-intent`
传的 `disable_thinking=True` 从未生效 —— 思考实际开着，报告头却写着
「thinking=disabled」（假信息）。

主评测路径（`--no-thinking`）不受影响：`NoThinkingProvider` 是在
`__init__` 里自塞 True 的，不依赖 `from_env` 透传。

⚠️ 修法在工装侧（eval_vision_offline.py 的 `RecordingProvider.from_env`
覆写），不动 A 的 `vision.py` —— 分工卡边界。

**全部离线**：不发起任何 HTTP 请求（`_post` 用 mock 拦截）。
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import eval_vision_offline as ev                        # noqa: E402

_ENV = {'OHAUTO_VISION_BASE_URL': 'https://example.invalid/v1',
        'OHAUTO_VISION_API_KEY': 'sk-test-not-a-real-key',
        'OHAUTO_VISION_MODEL': 'test-model'}


class FromEnvThinkingFlag(unittest.TestCase):
    """`from_env(disable_thinking=...)` 必须真的落到实例属性上。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV}
        os.environ.update(_ENV)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_explicit_true_reaches_instance(self):
        """★ 修前 FAIL（复现）：from_env 吞掉 disable_thinking → 属性恒 False。"""
        prov = ev.RecordingProvider.from_env(disable_thinking=True)
        self.assertTrue(prov.disable_thinking,
                        'disable_thinking=True 被 from_env 吞了 —— '
                        '--read-page/--page-intent 的思考开关仍是开的')
        self.assertTrue(prov.name.endswith('(thinking=disabled)'))

    def test_default_is_false(self):
        """不带参数时保持默认（开着思考），不误关。"""
        prov = ev.RecordingProvider.from_env()
        self.assertFalse(prov.disable_thinking)
        self.assertFalse(prov.name.endswith('(thinking=disabled)'))

    def test_no_thinking_provider_from_env_still_disables(self):
        """★ 反向回归：覆写不得把 NoThinkingProvider 在 __init__ 里
        自塞的 True 清掉（2026-09-22 的 --no-thinking 全量评测走这条）。"""
        prov = ev.NoThinkingProvider.from_env()
        self.assertTrue(prov.disable_thinking,
                        'NoThinkingProvider.from_env 的开关被覆写清掉了 —— '
                        '94.7% 口径的「关思考」将失效')


class PostInjectsThinkingSwitch(unittest.TestCase):
    """`disable_thinking` 的最终作用点：payload 注入 `thinking` 字段。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV}
        os.environ.update(_ENV)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 1x1 透明 PNG（locate() 会真开文件读字节，空路径会 FileNotFoundError）
    _PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
            b'\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
            b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r'
            b'\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')

    @staticmethod
    def _capture_payload(prov, png_path):
        """拦住基类传输层，返回 _post 收到的 payload。"""
        captured = {}

        def fake_post(_self, payload):
            captured.update(payload)
            return {'choices': [{'message': {'content': '[]'}}]}

        with mock.patch.object(ev.OpenAICompatibleProvider, '_post', fake_post):
            prov.locate(image_path=png_path, instruction='找按钮',
                        screen_w=720, screen_h=1280)
        return captured

    def _with_png(self):
        import shutil
        import tempfile
        d = tempfile.mkdtemp(prefix='ohauto_think_')
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, 'shot.png')
        with open(p, 'wb') as f:
            f.write(self._PNG)
        return p

    def test_payload_carries_thinking_disabled_when_flagged(self):
        prov = ev.RecordingProvider.from_env(disable_thinking=True)
        captured = self._capture_payload(prov, self._with_png())
        self.assertEqual(captured.get('thinking'), {'type': 'disabled'})

    def test_payload_untouched_by_default(self):
        prov = ev.RecordingProvider.from_env()
        captured = self._capture_payload(prov, self._with_png())
        self.assertNotIn('thinking', captured)


if __name__ == '__main__':
    unittest.main()
