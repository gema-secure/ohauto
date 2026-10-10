"""C 集成层回归钉子：视觉 Provider 的 available() 必须与 from_env() 一致。

来源：`available()` 被改成「任一一组三个变量配齐」，
而 `from_env()` 是**逐变量**回退取值 —— 跨组混搭配置下两者分叉
（集成实测 2/5 场景矛盾），上层会据此**静默跳过视觉通道**，
而这正是此前专门修掉的那个「自相矛盾」。

C 在集成层把 available() 对齐回逐变量语义（vision.py 的 available()
docstring 已写明理由），本文件钉住该行为，防止再被改回去。

复现脚本：`python _out/probe_env_consistency.py`
"""
import os
import unittest

from ohauto.vision import OpenAICompatibleProvider, VisionConfigError

ALL_ENV = ('OHAUTO_VISION_BASE_URL', 'OHAUTO_VISION_API_KEY', 'OHAUTO_VISION_MODEL',
           'OHAUTO_LLM_BASE_URL', 'OHAUTO_LLM_API_KEY', 'OHAUTO_LLM_MODEL',
           'OH_LLM_BASE_URL', 'OH_LLM_API_KEY', 'OH_LLM_MODEL')


class TestAvailableMatchesFromEnv(unittest.TestCase):
    """available() 与 from_env() 必须同进同出。

    否则出现「from_env() 装配成功、available() 说不可用」→ 上层据
    available() 决定是否启用视觉通道 → 用户配了 key 却静默没有视觉能力。
    """

    def _probe(self, env):
        """在受控环境下返回 (available(), from_env() 是否成功)。"""
        # 9 个变量全部先摘掉（含整条回退链），避免开发机上的真实配置
        # 把「全空」场景污染成「本机绿、CI 红」的飘忽状态。
        saved = {k: os.environ.pop(k, None) for k in ALL_ENV}
        try:
            os.environ.update(env)
            av = OpenAICompatibleProvider.available()
            try:
                OpenAICompatibleProvider.from_env()
                fe = True
            except VisionConfigError:
                fe = False
            return av, fe
        finally:
            os.environ.update({k: v for k, v in saved.items() if v})

    def test_group_configs_agree(self):
        """任一一组配齐 —— 两种判定都必须为真（正常路径）。"""
        for env in (
            {'OHAUTO_VISION_BASE_URL': 'https://x/v1',
             'OHAUTO_VISION_API_KEY': 'k', 'OHAUTO_VISION_MODEL': 'm'},
            {'OHAUTO_LLM_BASE_URL': 'https://x/v1',
             'OHAUTO_LLM_API_KEY': 'k', 'OHAUTO_LLM_MODEL': 'm'},
            {'OH_LLM_BASE_URL': 'https://x/v1',
             'OH_LLM_API_KEY': 'k', 'OH_LLM_MODEL': 'm'},
        ):
            av, fe = self._probe(env)
            self.assertTrue(av, f'配齐一组应判可用: {sorted(env)}')
            self.assertTrue(fe, f'配齐一组应能装配: {sorted(env)}')

    def test_mixed_prefix_config_agrees(self):
        """跨组混搭：from_env() 装得出来，available() 就不能说不可用。

        这正是 A 版「按组配齐」判定会分叉的两个场景（集成实测 C / D）。
        """
        for env in (
            {'OHAUTO_VISION_BASE_URL': 'https://x/v1',
             'OHAUTO_LLM_API_KEY': 'k', 'OHAUTO_LLM_MODEL': 'm'},
            {'OHAUTO_VISION_BASE_URL': 'https://x/v1',
             'OHAUTO_VISION_API_KEY': 'k', 'OH_LLM_MODEL': 'm'},
        ):
            av, fe = self._probe(env)
            self.assertTrue(fe, f'逐变量回退应能装配成功: {sorted(env)}')
            self.assertEqual(av, fe,
                             f'混搭配置下 available/from_env 分叉: {sorted(env)}')

    def test_empty_env_both_unavailable(self):
        """全空：两者都必须判不可用（且 from_env 抛错、不静默退 Mock）。"""
        av, fe = self._probe({})
        self.assertFalse(av, '全空必须判不可用')
        self.assertFalse(fe, '全空必须抛 VisionConfigError 而不是静默退回 Mock')


if __name__ == '__main__':
    unittest.main()
