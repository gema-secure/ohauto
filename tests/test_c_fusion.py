"""通用融合内核（`ohauto/fusion.py`）的回归钉子。

这里钉的是**契约与规则**，不是某个具体源的实现细节 ——
因为内核的卖点就是「加源不用改规则」，规则一旦被改坏，
所有源都会跟着错，而各源自己的测试**测不出来**。

重点用例：
  * 三条规则（confirmed / missing / undeclared）不被写偏；
  * **`ok=False` 的源不参与融合**，且必须出现在报告里（降级要可见）；
  * `page` 类缺失与 `id`/`text` 类缺失**措辞不同**（前者不是缺陷）。
"""
import os
import sys
import types
import unittest
from collections import namedtuple
from unittest import mock

from ohauto import fusion

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, 'datasets', 'gallery_13app')


def _src(name, role, claims, ok=True, reason=''):
    return fusion.SourceResult(name, role, ok=ok, reason=reason,
                               claims=[fusion.Claim(*c) if isinstance(c, tuple)
                                       else c for c in claims])


class TestFusionRules(unittest.TestCase):
    """三条规则 —— 通用性的核心，改坏了大面积出事。"""

    def test_declared_and_seen_is_confirmed(self):
        rep = fusion.fuse([
            _src('static', 'declaration', [('id', 'btn')]),
            _src('runtime', 'observation', [('id', 'btn')]),
        ])
        t = rep.targets[0]
        self.assertEqual(t.status, 'confirmed')
        self.assertEqual(t.declared_by, ['static'])
        self.assertEqual(t.seen_by, ['runtime'])
        self.assertGreater(t.confidence, 0)

    def test_declared_but_not_seen_is_missing(self):
        rep = fusion.fuse([
            _src('static', 'declaration', [('id', 'ghost')]),
            _src('runtime', 'observation', [('id', 'other')]),
        ])
        self.assertIn('ghost', [t.value for t in rep.by_status('missing')])

    def test_seen_but_not_declared_is_undeclared(self):
        rep = fusion.fuse([_src('runtime', 'observation', [('id', 'sys')])])
        self.assertEqual(rep.targets[0].status, 'undeclared')

    def test_rules_do_not_depend_on_source_names(self):
        """**关键**：换个名字当声明源，规则照样成立（这才是通用）。"""
        rep = fusion.fuse([
            _src('设计稿', 'declaration', [('id', 'x')]),
            _src('摄像头', 'observation', [('text', 'y')]),
        ])
        self.assertEqual(rep.by_status('undeclared')[0].value, 'y')
        self.assertEqual(rep.by_status('missing')[0].value, 'x')


class TestDegradation(unittest.TestCase):
    """降级必须可见 —— 否则「没查到」与「没运行」分不清，报告会骗人。"""

    def test_not_ok_source_is_excluded_from_fusion(self):
        rep = fusion.fuse([
            _src('static', 'declaration', [], ok=False, reason='没有源码'),
            _src('runtime', 'observation', [('id', 'a')]),
        ])
        self.assertEqual(len(rep.targets), 1)
        self.assertEqual(rep.targets[0].status, 'undeclared',
                         '声明源没运行时，不该有 missing')

    def test_not_ok_source_still_listed_in_report(self):
        rep = fusion.fuse([
            _src('static', 'declaration', [], ok=False, reason='没有源码'),
            _src('runtime', 'observation', [('id', 'a')]),
        ])
        md = fusion.render_md(rep)
        self.assertIn('没有源码', md)
        self.assertIn('不要把', md, '必须警告「缺源 ≠ 没问题」')

    def test_usable_counts_only_running_sources(self):
        rep = fusion.fuse([_src('a', 'declaration', [], ok=False),
                           _src('b', 'observation', [('id', 'x')])])
        self.assertEqual([s.name for s in rep.usable()], ['b'])


class TestReportWording(unittest.TestCase):
    """不同 kind 的「缺失」含义完全不同，措辞不能混。"""

    def test_page_missing_is_not_called_defect(self):
        rep = fusion.fuse([
            _src('static', 'declaration', [('page', 'pages/Second')]),
            _src('runtime', 'observation', [('page', 'pages/Index')]),
        ])
        md = fusion.render_md(rep)
        self.assertIn('未观察到的页面', md)
        self.assertIn('不是缺陷', md)

    def test_id_missing_is_flagged(self):
        rep = fusion.fuse([
            _src('static', 'declaration', [('id', 'btn')]),
            _src('runtime', 'observation', []),
        ])
        md = fusion.render_md(rep)
        self.assertIn('声明了但没出现', md)


class TestAdapters(unittest.TestCase):
    """适配器：真实数据上跑得通，且该降级时如实降级。"""

    def test_runtime_source_from_real_fixture(self):
        p = os.path.join(FIX, 'sample_music.json')
        with open(p, encoding='utf-8') as f:
            src = fusion.source_runtime_tree(f.read())
        self.assertTrue(src.ok)
        self.assertGreater(len(src.claims), 0)
        self.assertIn('可见节点', src.note)

    def test_runtime_source_none_is_not_ok(self):
        src = fusion.source_runtime_tree(None)
        self.assertFalse(src.ok)
        self.assertIn('未提供', src.reason)

    def test_static_source_missing_dir_is_not_ok(self):
        src = fusion.source_static_project('D:/不存在这个目录')
        self.assertFalse(src.ok)
        self.assertIn('没有源码', src.reason)

    def test_case_source_extracts_controls(self):
        p = os.path.join(ROOT, 'examples', 'cases', 'calculator.yaml')
        if not os.path.isfile(p):
            self.skipTest('没有 calculator.yaml')
        src = fusion.source_case(p)
        self.assertTrue(src.ok)
        vals = {c.value for c in src.claims}
        self.assertIn('7', vals)
        self.assertIn('result', vals)

    def test_case_source_missing_file_is_not_ok(self):
        src = fusion.source_case('D:/nope.yaml')
        self.assertFalse(src.ok)

    def test_ocr_source_missing_image_is_not_ok(self):
        src = fusion.source_ocr('D:/nope.png')
        self.assertFalse(src.ok)
        self.assertIn('未提供截图', src.reason)

    def test_ocr_source_degrades_when_no_backend(self):
        """本机没装 OCR 后端时必须**如实报原因**，不能假装做过。"""
        # ⚠️ 截图与控件树都在 `datasets/gallery_13app/` 里（仓库内唯一副本）
        png = os.path.join(ROOT, 'datasets', 'gallery_13app', 'etsclock.png')
        if not os.path.isfile(png):
            self.skipTest('没有 etsclock.png')
        src = fusion.source_ocr(png)
        if not src.ok:
            self.assertTrue(src.reason, '降级必须带原因')
            self.assertIn('OCR', src.reason)


class TestVisionSource(unittest.TestCase):
    """视觉源（云端 VLM）—— 降级路径必须如实，且**测试不能依赖本机是否配了 key**。"""

    ENV_KEYS = ('OHAUTO_VISION_BASE_URL', 'OHAUTO_VISION_API_KEY', 'OHAUTO_VISION_MODEL',
                'OHAUTO_LLM_BASE_URL', 'OHAUTO_LLM_API_KEY', 'OHAUTO_LLM_MODEL',
                'OH_LLM_BASE_URL', 'OH_LLM_API_KEY', 'OH_LLM_MODEL')

    def setUp(self):
        # 把 key 相关环境变量全摘掉再跑，否则本机配过 key 时这条测试行为会变
        self._saved = {k: os.environ.pop(k, None) for k in self.ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_missing_image_is_not_ok(self):
        src = fusion.source_vision('D:/nope.png')
        self.assertFalse(src.ok)
        self.assertIn('未提供截图', src.reason)

    def test_degrades_without_key_and_says_what_to_configure(self):
        png = os.path.join(ROOT, 'datasets', 'gallery_13app', 'etsclock.png')
        if not os.path.isfile(png):
            self.skipTest('没有 etsclock.png')
        src = fusion.source_vision(png)
        self.assertFalse(src.ok, '没配 key 时必须降级，不能假装看过图')
        self.assertIn('OHAUTO_VISION', src.reason, '要告诉用户配什么')
        self.assertIn('deepseek', src.reason.lower(),
                      '顺带提示可用的多模态模型名，省得用户去查')

    def test_injected_provider_is_used(self):
        """注入替身时必须真的用替身 —— 否则测试没法脱离网络。"""
        class _T:
            label = 'HelloWorld'
            confidence = 0.9
            uncertain = False
            rect = (1, 2, 3, 4)

        class _P:
            def locate(self, img, ins, w, h, hints=None):
                return [_T()]

        png = os.path.join(ROOT, 'datasets', 'gallery_13app', 'etsclock.png')
        if not os.path.isfile(png):
            self.skipTest('没有 etsclock.png')
        src = fusion.source_vision(png, provider=_P())
        self.assertTrue(src.ok)
        self.assertIn('HelloWorld', [c.value for c in src.claims])


class TestConfidenceUsesWeights(unittest.TestCase):
    def test_static_weighs_more_than_case(self):
        """只用声明源比 —— 混入观察源会被 cap 到 1.0，把差异盖住。

        （第一版就是拿"观察+声明"比的，两边都是 1.0，测试挂了才发现
          自己写得不严谨：**用会被上限截断的量去比大小**。）
        """
        b = fusion.fuse([_src('static', 'declaration', [('id', 'x')])])
        a = fusion.fuse([_src('case', 'declaration', [('id', 'x')])])
        self.assertGreater(b.targets[0].confidence, a.targets[0].confidence,
                           'static(0.9) 应比 case(0.6) 贡献更多置信度')

    def test_confidence_capped_at_one(self):
        rep = fusion.fuse([_src('a', 'observation', [('id', 'x')]),
                           _src('b', 'observation', [('id', 'x')]),
                           _src('c', 'observation', [('id', 'x')])])
        self.assertLessEqual(rep.targets[0].confidence, 1.0)


class TestClaimAndWeights(unittest.TestCase):
    """Claim 契约与权重参数 —— 小而容易漏的边角。"""

    def test_claim_key_includes_scope(self):
        c = fusion.Claim('id', 'a', scope='pages/Index')
        self.assertEqual(c.key, ('id', 'a', 'pages/Index'))

    def test_custom_weights_change_confidence(self):
        # ⚠️ 只用**单个声明源**比：混入观察源或双源并加，置信度会被
        # cap 到 1.0，拿被截断的量比大小永远相等（本文件 TestConfidence
        # 里现有注释警告过这个坑，第一版就栽过一次）。
        base = fusion.fuse([_src('s', 'declaration', [('id', 'x')])])
        boosted = fusion.fuse([_src('s', 'declaration', [('id', 'x')])],
                              weights={'s': 2.0})
        self.assertGreater(boosted.targets[0].confidence,
                           base.targets[0].confidence)

    def test_evidence_recorded_and_deduped(self):
        """证据要带上「谁说的」，且同一来源的同一条不重复（09-24 实测踩过）。"""
        rep = fusion.fuse([
            fusion.SourceResult('static', 'declaration', claims=[
                fusion.Claim('id', 'btn', evidence='Index.ets')]),
            fusion.SourceResult('runtime', 'observation', claims=[
                fusion.Claim('id', 'btn', evidence='[10,20]')]),
        ])
        t = rep.targets[0]
        self.assertIn('static: Index.ets', t.evidence)
        self.assertIn('runtime: [10,20]', t.evidence)
        self.assertEqual(len(t.evidence), len(set(t.evidence)), '证据不该重复')


class TestStaticSourceBridge(unittest.TestCase):
    """source_static_project 主体：bridge 可用 / 返回空 / import 失败，各有钉子。"""

    def _install_bridge(self, analyze_result):
        mod = types.ModuleType('bridge')
        mod.analyze_project = lambda root: analyze_result
        mod.last_error = lambda: '解析失败'
        mod.control_hints = lambda info: info.get('controls') or []
        sys.modules['bridge'] = mod

    def tearDown(self):
        sys.modules.pop('bridge', None)

    def test_ok_path_builds_claims(self):
        self._install_bridge({
            'pages': ['pages/Index', 'pages/Second'],
            'widget_pages': ['WidgetCard.ets'],
            'controls': [
                {'id': 'btn_go', 'text': '下一页', 'file': 'pages/Index.ets'},
                {'text': '标题', 'file': 'pages/Second.ets'},
            ]})
        src = fusion.source_static_project(ROOT, page='pages/Index')
        self.assertTrue(src.ok)
        kinds = {(c.kind, c.value) for c in src.claims}
        self.assertIn(('id', 'btn_go'), kinds)
        self.assertIn(('text', '下一页'), kinds)
        # 第二页的控件被 default_mapper 过滤掉；页面主张不受页面过滤、全保留
        self.assertNotIn(('text', '标题'), kinds)
        self.assertIn(('page', 'pages/Index'), kinds)
        self.assertIn(('page', 'pages/Second'), kinds)
        self.assertIn('已排除桌面卡片 1 个', src.note)

    def test_mapper_override_is_used(self):
        self._install_bridge({'pages': ['pages/Index'], 'widget_pages': [],
                              'controls': [{'text': '标题',
                                            'file': 'pages/Second.ets'}]})
        seen = {}

        def mapper(f, pg):
            seen['args'] = (f, pg)
            return True

        # ⚠️ root 必须是真实存在的目录（函数先做 isdir 检查），bridge 已 mock
        src = fusion.source_static_project(ROOT, page='pages/Index',
                                           mapper=mapper)
        self.assertTrue(src.ok)
        self.assertEqual(seen['args'][1], 'pages/Index')
        self.assertIn(('text', '标题'),
                      {(c.kind, c.value) for c in src.claims})

    def test_bridge_failure_degrades_with_reason(self):
        self._install_bridge(None)
        src = fusion.source_static_project(ROOT)
        self.assertFalse(src.ok)
        self.assertEqual(src.reason, '解析失败')

    def test_bridge_import_error_degrades(self):
        with mock.patch.dict(sys.modules, {'bridge': None}):
            src = fusion.source_static_project(ROOT)
        self.assertFalse(src.ok)
        self.assertIn('静态分析模块不可用', src.reason)


class TestCaseSourceRegexFallback(unittest.TestCase):
    def test_bad_yaml_falls_back_to_regex(self):
        """YAML 解析失败时退回正则提取，不是直接报废 —— 用例还是要能进来。"""
        import tempfile
        bad = ('x: [1, 2\n'
               'tap: {id: "btn_x"}\n'
               'assert: {exists: {text: "首页"}}\n')
        fd, p = tempfile.mkstemp(suffix='.yaml')
        os.write(fd, bad.encode('utf-8'))
        os.close(fd)
        try:
            src = fusion.source_case(p)
        finally:
            os.unlink(p)
        self.assertTrue(src.ok)
        vals = {c.value for c in src.claims}
        self.assertIn('btn_x', vals)
        self.assertIn('首页', vals)


class TestRuntimeSourceDegrade(unittest.TestCase):
    def test_bad_text_degrades_not_crashes(self):
        """设备缺席时 dumpLayout 返回 [Fail]... 文本 —— 必须降级，不能崩栈。"""
        src = fusion.source_runtime_tree('[Fail]Not match target founded')
        self.assertFalse(src.ok)
        self.assertIn('解析失败', src.reason)


class TestOcrVenvBridge(unittest.TestCase):
    """_ocr_via_venv 的子进程桥：worker 三种结局（失败 / 空结果 / 成功）。"""

    _Proc = namedtuple('_Proc', 'returncode stdout stderr')

    def _run(self, rc=0, stdout='', stderr=''):
        proc = self._Proc(rc, stdout, stderr)
        # _VENV_PY 必须指向**真实存在的文件**：函数先做 isfile 检查，
        # 指到假路径会触发「venv 解释器不存在」的降级分支而不是子进程桥
        with mock.patch.object(fusion, '_VENV_PY', sys.executable), \
             mock.patch('subprocess.run', return_value=proc):
            return fusion._ocr_via_venv('D:/shot.png', 'ocr')

    def test_worker_failure_returns_none(self):
        res = self._run(rc=1, stderr='backend=rapidocr\nboom\n')
        self.assertIsNone(res)

    def test_worker_no_text_returns_none(self):
        res = self._run(rc=0, stdout='  \n')
        self.assertIsNone(res)

    def test_worker_success_builds_claims(self):
        res = self._run(rc=0, stdout='14:54:11\n', stderr='backend=rapidocr\n')
        self.assertIsNotNone(res)
        self.assertTrue(res.ok)
        self.assertEqual([c.value for c in res.claims], ['14:54:11'])
        self.assertIn('rapidocr', res.note)
        self.assertIn('via venv', res.claims[0].evidence)

    def test_env_var_overrides_the_default_interpreter(self):
        """OHAUTO_VENV_PY 优先于模块内默认路径 —— 换机器不必改源码。"""
        with mock.patch.dict(os.environ, {'OHAUTO_VENV_PY': 'X:/venv/py.exe'}):
            self.assertEqual(fusion._venv_python(), 'X:/venv/py.exe')

    def test_env_var_pointing_nowhere_degrades_instead_of_raising(self):
        """环境变量指到不存在的解释器 → 如实降级，不拉子进程也不抛错。"""
        with mock.patch.dict(os.environ, {'OHAUTO_VENV_PY': 'X:/nope/py.exe'}):
            with mock.patch('subprocess.run') as run:
                self.assertIsNone(fusion._ocr_via_venv('D:/shot.png', 'ocr'))
        run.assert_not_called()


class TestOcrTesseractBackend(unittest.TestCase):
    PNG = os.path.join(ROOT, 'datasets', 'gallery_13app', 'etsclock.png')

    def setUp(self):
        if not os.path.isfile(self.PNG):
            self.skipTest('没有 etsclock.png')

    @staticmethod
    def _install(fn):
        pt = types.ModuleType('pytesseract')
        pt.image_to_string = fn
        pil = types.ModuleType('PIL')
        img = types.ModuleType('PIL.Image')
        img.open = lambda p: object()
        pil.Image = img
        return mock.patch.dict(sys.modules, {'pytesseract': pt, 'PIL': pil,
                                             'PIL.Image': img})

    def test_tesseract_ok(self):
        with self._install(lambda im, lang=None: '14:54\n时间'):
            src = fusion.source_ocr(self.PNG, backend='tesseract')
        self.assertTrue(src.ok)
        self.assertIn('14:54', [c.value for c in src.claims])
        self.assertIn('pytesseract', src.note)

    def test_tesseract_error_degrades(self):
        def boom(im, lang=None):
            raise RuntimeError('no langpack')
        with self._install(boom):
            src = fusion.source_ocr(self.PNG, backend='tesseract')
        self.assertFalse(src.ok)
        self.assertIn('pytesseract 失败', src.reason)

    def test_no_backend_reports_tried_list(self):
        with mock.patch.dict(sys.modules, {'pytesseract': None}):
            src = fusion.source_ocr(self.PNG, backend='tesseract')
        self.assertFalse(src.ok)
        self.assertIn('未装任何 OCR 后端', src.reason)
        self.assertIn('pip install winsdk', src.reason)

    def test_winsdk_explicit_import_error(self):
        with mock.patch.dict(sys.modules, {'winsdk': None}):
            src = fusion.source_ocr(self.PNG, backend='winsdk')
        self.assertFalse(src.ok)
        self.assertIn('未安装 winsdk', src.reason)


class TestVisionSourceEdgeCases(unittest.TestCase):
    """source_vision 的异常分支：定位抛错 / from_env 抽风 / 空标签 / 降权。"""

    PNG = os.path.join(ROOT, 'datasets', 'gallery_13app', 'etsclock.png')

    def setUp(self):
        if not os.path.isfile(self.PNG):
            self.skipTest('没有 etsclock.png')

    def test_locate_raises_degrades(self):
        class _P:
            def locate(self, *a, **k):
                raise RuntimeError('网络不通')

        src = fusion.source_vision(self.PNG, provider=_P())
        self.assertFalse(src.ok)
        self.assertIn('视觉调用失败', src.reason)
        self.assertIn('RuntimeError', src.reason)

    def test_from_env_unexpected_error(self):
        from ohauto import vision
        with mock.patch.object(vision.OpenAICompatibleProvider, 'from_env',
                               side_effect=RuntimeError('坏配置')):
            src = fusion.source_vision(self.PNG)
        self.assertFalse(src.ok)
        self.assertIn('装配视觉 Provider 失败', src.reason)

    def test_empty_label_skipped_and_uncertain_downweighted(self):
        class _T:
            def __init__(self, label, confidence=0.9, uncertain=False,
                         rect=(1, 2, 3, 4)):
                self.label = label
                self.confidence = confidence
                self.uncertain = uncertain
                self.rect = rect

        class _P:
            def __init__(self, targets):
                self._t = targets

            def locate(self, *a, **k):
                return self._t

        src = fusion.source_vision(self.PNG, provider=_P(
            [_T('   '), _T('时间', uncertain=True)]))
        self.assertTrue(src.ok)
        self.assertEqual(len(src.claims), 1, '空标签不该进主张')
        self.assertLessEqual(src.claims[0].confidence, 0.5,
                             'uncertain 必须降权')
        self.assertIn('读到 1 条', src.note)


class TestRenderMdSections(unittest.TestCase):
    """render_md 的每一段：confirmed 表 / 出处 / 作用域 / 单观察源高亮。"""

    def test_confirmed_table_rendered(self):
        rep = fusion.fuse([_src('static', 'declaration', [('id', 'btn')]),
                           _src('runtime', 'observation', [('id', 'btn')])])
        md = fusion.render_md(rep)
        self.assertIn('确认存在', md)
        self.assertIn('| `btn` | id |', md)

    def test_evidence_listed_for_missing(self):
        rep = fusion.fuse([
            fusion.SourceResult('static', 'declaration', claims=[
                fusion.Claim('id', 'ghost', evidence='Index.ets')]),
            _src('runtime', 'observation', []),
        ])
        self.assertIn('出处：static: Index.ets', fusion.render_md(rep))

    def test_scope_rendered(self):
        rep = fusion.fuse([_src('a', 'declaration', [('id', 'x')])],
                          scope='pages/Index')
        self.assertIn('作用域', fusion.render_md(rep))

    def test_single_observer_highlighted(self):
        """三个观察源：ocr 独见一条被高亮，其余两个源都看到的一条进「多源」段。"""
        rep = fusion.fuse([
            _src('runtime', 'observation', [('text', '树里有的')]),
            _src('vision', 'observation', [('text', '树里有的')]),
            _src('ocr', 'observation', [('text', '14:54:11')]),
        ])
        md = fusion.render_md(rep)
        self.assertIn('只有一个观察源看到', md)
        self.assertIn('只有 **`ocr`** 看到', md)
        self.assertIn('其余（多源都看到）', md)

    def test_single_observer_not_flagged_with_one_source(self):
        """只有一个观察源时没有「独见」可言，高亮必须关闭（否则是噪音）。"""
        rep = fusion.fuse([_src('ocr', 'observation', [('text', 'x')])])
        self.assertNotIn('只有一个观察源看到', fusion.render_md(rep))


if __name__ == '__main__':
    unittest.main()
