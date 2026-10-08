"""B8 路线①「多页上下文」的钉子 —— 生成期把**每一页**的控件清单给模型。

背景（真机实测，证据见 docs 下的 B8 详情文档；过程写在 commit message 里）：
生成期只喂一张树时，模型拿第一页的控件去写「导航之后」的断言 ——
两条真机用例都在 `tap btn_go_second` 之后断言 `tv_probe_always`，
而该 id 只在 `Index.ets:56` 定义，真机必然失败（用例级 0/2）。
**信息不在，模型只能按手里的写。**

这组钉子守三件事：
1. 多页清单把每一页分开、标出来；
2. **单页路径逐字节不变**（零回归是验收第一条）；
3. **修复轮也拿到多页清单** —— 只改生成不改修复，修复会把错页控件又写回去。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.generator import (Generator, ScriptedProvider,  # noqa: E402
                              build_case_prompt, build_repair_prompt,
                              control_catalog, control_catalog_pages)
from ohauto.layout import LayoutNode, Rect                    # noqa: E402


def _tree(ids):
    """按 (类型, id, 文案) 造一棵最小树，根带 bundleName。"""
    root = LayoutNode(type='Column', attributes={'bundleName': 'com.demo.app'},
                      rect=Rect(0, 0, 720, 1280))
    for typ, cid, text in ids:
        n = LayoutNode(type=typ, id=cid, text=text, clickable=True,
                       rect=Rect(10, 10, 100, 60), parent=root)
        root.children.append(n)
    return root


INDEX = _tree([('Button', 'btn_go_second', '进入第二页'),
               ('Text', 'tv_probe_always', '无条件渲染')])
SECOND = _tree([('Text', 'tv_second_title', '第二页'),
                ('Button', 'btn_go_third', '去第三页')])
THIRD = _tree([('Text', 'tv_third_title', '第三页'),
               ('Button', 'btn_inc', '加一')])

VERDICTS = ('{"kind": "功能", "title": "点一下", "precondition": "", "expect": ""}',)


class TestCatalogBlocks(unittest.TestCase):

    def test_each_page_is_labelled_separately(self):
        text = control_catalog_pages([('Index', INDEX), ('Second', SECOND)])
        self.assertIn('页面 1｜Index', text)
        self.assertIn('页面 2｜Second', text)

    def test_controls_of_every_page_are_listed(self):
        text = control_catalog_pages([('Index', INDEX), ('Second', SECOND)])
        for cid in ('btn_go_second', 'tv_probe_always', 'btn_go_third'):
            self.assertIn(cid, text)

    def test_bare_trees_are_accepted_too(self):
        """只给树（不给页名）也要能用，页名从树上取。"""
        text = control_catalog_pages([INDEX, SECOND])
        self.assertIn('页面 1｜com.demo.app', text)

    def test_unnamed_empty_label_does_not_crash(self):
        bare = LayoutNode(type='Column', rect=Rect(0, 0, 100, 100))
        self.assertIn('未命名', control_catalog_pages([('', bare)]))

    def test_json_text_input_is_parsed(self):
        """入参可以是 JSON 文本 —— 工装手里拿到的就是设备导出的那份文本。"""
        raw = ('{"attributes": {"type": "Column"}, "children": '
               '[{"attributes": {"type": "Text", "id": "tv_from_text",'
               ' "text": "来自文本", "bounds": "[0,0][100,40]"}}]}')
        text = control_catalog_pages([('Second', raw)])
        self.assertIn('tv_from_text', text)


class TestSinglePagePathIsUnchanged(unittest.TestCase):
    """零回归：不给 `pages` 时，提示词与改动前逐字节一致。"""

    def test_prompt_without_multi_page_has_no_new_rule(self):
        cat = control_catalog(INDEX)
        prompt = build_case_prompt('点第二页', _point(), cat, 'com.demo.app', 'EntryAbility')
        self.assertNotIn('多页约束', prompt)
        self.assertIn(cat, prompt)

    def test_prompt_with_multi_page_carries_the_rule(self):
        cat = control_catalog_pages([('Index', INDEX), ('Second', SECOND)])
        prompt = build_case_prompt('点第二页', _point(), cat, 'com.demo.app',
                                   'EntryAbility', multi_page=True)
        self.assertIn('多页约束', prompt)
        self.assertIn('只能引用目标页面上的控件', prompt)

    def test_repair_prompt_carries_the_rule_too(self):
        from ohauto.generator import Case
        case = Case(name='x', steps=[{'start': True}])
        prompt = build_repair_prompt(case, [], 'CATALOG', multi_page=True)
        self.assertIn('多页约束', prompt)
        self.assertIn('CATALOG', prompt)

    def test_catalog_source_is_single_page_when_pages_are_absent(self):
        gen = Generator(provider=ScriptedProvider(list(VERDICTS)), page=INDEX)
        self.assertEqual(gen._catalog_for(INDEX, None), control_catalog(INDEX))
        self.assertNotIn('页面 1｜', gen._catalog_for(INDEX, None))

    def test_catalog_source_switches_to_blocks_when_pages_are_given(self):
        gen = Generator(provider=ScriptedProvider(list(VERDICTS)), page=INDEX)
        multi = gen._catalog_for(INDEX, [('Index', INDEX), ('Second', SECOND)])
        self.assertIn('页面 1｜Index', multi)
        self.assertIn('页面 2｜Second', multi)


def _point():
    """用项目自己的宽松解析器造测试点 —— 手搓 `TestPoint(kind='功能')` 会踩
    `kind` 必须是 `TestKind` 枚举的细节，走公开入口最省事。"""
    from ohauto.generator import parse_test_points_lenient
    return parse_test_points_lenient(
        '{"kind": "功能", "title": "点一下", "precondition": "", "expect": ""}')[0]


class TestPromptSentToTheModel(unittest.TestCase):
    """真正发出去的提示词里必须两页都有 —— 这才是"把信息给了模型"。"""

    def _generate(self, pages):
        provider = ScriptedProvider([
            '{"kind": "功能", "title": "点一下"}',
            '{"name": "跳转", "steps": [{"start": true},'
            ' {"tap": {"id": "btn_go_second"}}]}',
        ], strict=True)
        gen = Generator(provider=provider, bundle='com.demo.app',
                        ability='EntryAbility', page=INDEX)
        # 走 generate_many(prefetch=False)：与工装同一条路径，顺带覆盖预取分支
        rep = gen.generate_many(['点击进入第二页'], page=INDEX, pages=pages,
                                prefetch=False)
        return provider, rep.outcomes[0]

    def test_multi_page_context_reaches_the_draft_prompt(self):
        provider, out = self._generate([('Index', INDEX), ('Second', SECOND)])
        self.assertTrue(out.ok, out.detail)
        draft_prompt = provider.calls[-1]
        self.assertIn('多页约束', draft_prompt)
        self.assertIn('btn_go_third', draft_prompt)      # 第二页的控件也在

    def test_without_pages_the_prompt_stays_single_page(self):
        provider, out = self._generate(None)
        self.assertTrue(out.ok, out.detail)
        self.assertNotIn('多页约束', provider.calls[-1])
        self.assertNotIn('btn_go_third', provider.calls[-1])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestValidationUsesEveryCollectedPage(unittest.TestCase):
    """校验的控件池要取**并集** —— 否则多页上下文会把 L2 打成 0/3。

    实测（10-08 第一轮多页）：模型开始正确引用第二页的控件
    （`tv_second_title` / `input_second`），而只对照入口页的校验器把它们一律判成
    CONTROL_MISSING → 可执行率 2/3 → **0/3**。信息给对了，判据却还在按单页判。
    """

    def _case(self, cid):
        from ohauto.generator import Case
        return Case(name='x', steps=[{'start': True}, {'assert': {'exists': {'id': cid}}}])

    def test_second_page_control_is_accepted_when_pages_are_given(self):
        from ohauto.generator import validate_case
        issues = validate_case(self._case('tv_second_title'), INDEX,
                              pages=[('Index', INDEX), ('Second', SECOND)])
        self.assertEqual([i for i in issues
                          if i.reason.name == 'CONTROL_MISSING'], [])

    def test_second_page_control_is_rejected_without_pages(self):
        """不给 pages 时保持原行为：仍然按当前页判（旧口径可复现）。"""
        from ohauto.generator import validate_case
        issues = validate_case(self._case('tv_second_title'), INDEX)
        self.assertTrue([i for i in issues if i.reason.name == 'CONTROL_MISSING'])

    def test_message_says_where_it_looked(self):
        from ohauto.generator import validate_case
        issues = validate_case(self._case('btn_zzz'), INDEX,
                              pages=[('Index', INDEX), ('Second', SECOND)])
        self.assertIn('已采集的 2 页里都没有',
                      [i.detail for i in issues
                       if i.reason.name == 'CONTROL_MISSING'][0])

    def test_bare_trees_are_accepted_in_the_pool_too(self):
        from ohauto.generator import validate_case
        issues = validate_case(self._case('tv_third_title'), INDEX,
                              pages=[INDEX, SECOND, THIRD])
        self.assertEqual([i for i in issues
                          if i.reason.name == 'CONTROL_MISSING'], [])
