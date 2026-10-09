"""系统权限弹窗识别与处置（C3）的单元测试。

两类输入：
  * **真机夹具** `tests/fixtures/permission/multi_device_dialog_1009.json`
    —— 新单板上 `ohos.samples.distributedcalc` 冷启动弹出的
    「允许“计算器”使用多设备协同？」原文，用于钉住真实结构；
  * **合成树** —— 钉住边界：应用自己的对话框不算权限门、权限 UI 的普通页面
    不算弹窗、不可见按钮不算「能作答」。

全部离线，不需要设备。
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.driver import DriverError                            # noqa: E402
from ohauto.explorer import Budget, Explorer                     # noqa: E402
from ohauto.layout import parse_layout                           # noqa: E402
from ohauto.permission import (DEFAULT_POLICY, ENV_POLICY,       # noqa: E402
                               PermissionDialogVerdict, POLICY_ALLOW,
                               POLICY_DENY, POLICY_RECORD,
                               detect_permission_dialog, resolve_policy)

FIXTURE = os.path.join(HERE, 'fixtures', 'permission',
                       'multi_device_dialog_1009.json')
PERMISSION_BUNDLE = 'com.ohos.permissionmanager'


# ---------------------------------------------------------------- 造树

def _node(type_, text='', bounds='[0,0][100,100]', clickable='false',
          bundle='', visible='true', enabled='true', children=None):
    a = {'type': type_, 'text': text, 'bounds': bounds, 'clickable': clickable,
         'visible': visible, 'enabled': enabled}
    if bundle:
        a['bundleName'] = bundle
    return {'attributes': a, 'children': list(children or [])}


def _tree(children, bounds='[0,0][720,1280]'):
    return {'attributes': {'type': 'root', 'bounds': bounds},
            'children': children}


def _app_page(title='计算器'):
    """应用页：一个标题文本 + 一段不可点的说明（没有候选控件）。"""
    return _tree([_node('root', bounds='[0,72][720,1208]',
                        bundle='ohos.samples.distributedcalc',
                        children=[_node('Text', title, '[36,120][200,160]'),
                                  _node('Text', '按 = 出结果',
                                        '[36,200][400,240]')])])


class _DialogDriver:
    """最小驱动桩：先给权限弹窗的树，作答之后给应用页的树。"""

    bundle = 'ohos.samples.distributedcalc'
    ability = 'MainAbility'
    artifact_dir = None

    def __init__(self, before, after, fail_tap=False):
        self._before = before
        self._after = after
        self.fail_tap = fail_tap
        self.taps = []
        self._answered = False
        self.root = before

    def refresh(self):
        self.root = self._after if self._answered else self._before
        return self.root

    def tap(self, target, post_idle=False):
        self.taps.append(target)
        if self.fail_tap:
            raise DriverError('模拟点击失败')
        self._answered = True


class PermissionFixtureTest(unittest.TestCase):
    """真机夹具：识别依据必须在真实结构上成立。"""

    def setUp(self):
        with open(FIXTURE, encoding='utf-8') as f:
            self.root = parse_layout(f.read())

    def test_real_dialog_is_recognised(self):
        v = detect_permission_dialog(self.root)
        self.assertTrue(v.is_permission_dialog, v.evidence)
        self.assertEqual(v.owner, PERMISSION_BUNDLE)
        self.assertIn('多设备协同', v.title)

    def test_answer_buttons_are_located(self):
        v = detect_permission_dialog(self.root)
        self.assertIsNotNone(v.allow)
        self.assertIsNotNone(v.deny)
        self.assertEqual(v.deny.text.strip(), '禁止')
        self.assertEqual(v.allow.text.strip(), '允许')
        # 位置必须来自真实框：禁止在左、允许在右（点错等于替用户授权）
        self.assertLess(v.deny.rect.center[0], v.allow.rect.center[0])

    def test_evidence_explains_the_verdict(self):
        v = detect_permission_dialog(self.root)
        joined = ' '.join(v.evidence)
        self.assertIn(PERMISSION_BUNDLE, joined)
        self.assertIn('禁止', joined)
        self.assertIn('允许', joined)

    def test_verdict_serialises(self):
        d = detect_permission_dialog(self.root).to_dict()
        self.assertTrue(d['is_permission_dialog'])
        self.assertEqual(d['deny'], '禁止')
        self.assertIsInstance(d['evidence'], list)


class PermissionBoundaryTest(unittest.TestCase):
    """边界：不要把「应用自己的对话框」和「权限 UI 的普通页面」也算成权限门。"""

    def test_none_root_is_not_a_dialog(self):
        self.assertFalse(detect_permission_dialog(None).is_permission_dialog)

    def test_empty_page_is_not_a_dialog(self):
        self.assertFalse(
            detect_permission_dialog(parse_layout(_app_page())).is_permission_dialog)

    def test_app_own_dialog_with_allow_cancel_is_not_a_permission_gate(self):
        """属主不是系统权限 UI → 那是业务对话框，不该由本模块代答。"""
        tree = _tree([_node('root', bounds='[0,72][720,1208]',
                            bundle='com.demo.app',
                            children=[_node('Dialog', clickable='true',
                                            bounds='[100,400][620,700]'),
                                      _node('Button', '允许', '[120,600][340,660]',
                                            clickable='true'),
                                      _node('Button', '取消', '[380,600][600,660]',
                                            clickable='true')])])
        self.assertFalse(
            detect_permission_dialog(parse_layout(tree)).is_permission_dialog)

    def test_permission_ui_page_without_answer_buttons_is_not_a_dialog(self):
        """权限管理器的设置页属于权限 UI，但没有作答按钮 → 不是弹窗。"""
        tree = _tree([_node('root', bounds='[0,72][720,1208]',
                            bundle=PERMISSION_BUNDLE,
                            children=[_node('Text', '权限管理', '[36,120][200,160]'),
                                      _node('Button', '相机', '[36,200][400,260]',
                                            clickable='true')])])
        self.assertFalse(
            detect_permission_dialog(parse_layout(tree)).is_permission_dialog)

    def test_invisible_button_does_not_count_as_answerable(self):
        tree = _tree([_node('root', bounds='[0,72][720,1208]',
                            bundle=PERMISSION_BUNDLE,
                            children=[_node('Text', '允许“相机”使用相机？',
                                            '[150,596][570,631]'),
                                      _node('Button', '允许', '[373,695][636,755]',
                                            clickable='true', visible='false'),
                                      _node('Button', '禁止', '[84,695][347,755]',
                                            clickable='true', visible='false')])])
        self.assertFalse(
            detect_permission_dialog(parse_layout(tree)).is_permission_dialog)

    def test_custom_bundle_list_is_honoured(self):
        tree = _tree([_node('root', bounds='[0,72][720,1208]',
                            bundle='com.vendor.permgate',
                            children=[_node('Button', '允许', '[373,695][636,755]',
                                            clickable='true')])])
        self.assertFalse(detect_permission_dialog(
            parse_layout(tree)).is_permission_dialog)
        self.assertTrue(detect_permission_dialog(
            parse_layout(tree), bundles=('com.vendor.permgate',)
        ).is_permission_dialog)


class PermissionPolicyTest(unittest.TestCase):

    def test_default_policy_is_deny(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_POLICY, None)
            self.assertEqual(resolve_policy(), DEFAULT_POLICY)
            self.assertEqual(DEFAULT_POLICY, POLICY_DENY)

    def test_explicit_value_wins_over_env(self):
        with mock.patch.dict(os.environ, {ENV_POLICY: POLICY_ALLOW}):
            self.assertEqual(resolve_policy(POLICY_RECORD), POLICY_RECORD)

    def test_env_var_is_honoured(self):
        with mock.patch.dict(os.environ, {ENV_POLICY: POLICY_ALLOW}):
            self.assertEqual(resolve_policy(), POLICY_ALLOW)

    def test_invalid_policy_raises_instead_of_falling_back(self):
        """策略写错会改变设备状态，必须当场报错而不是静默用默认值。"""
        with self.assertRaises(ValueError):
            resolve_policy('auto-magic')


class ExplorerPermissionIntegrationTest(unittest.TestCase):
    """Explorer 必须在「把弹窗当页面」之前先按策略作答。"""

    def setUp(self):
        with open(FIXTURE, encoding='utf-8') as f:
            self.dialog_tree = parse_layout(f.read())

    def _run(self, policy, fail_tap=False):
        drv = _DialogDriver(self.dialog_tree, parse_layout(_app_page()),
                            fail_tap=fail_tap)
        ex = Explorer(drv, verbose=False, permission_policy=policy)
        ex.explore(budget=Budget(max_pages=1, max_actions_per_page=1,
                                 max_steps=1, max_seconds=5))
        return drv, ex

    def test_deny_policy_answers_and_explores_the_app_page(self):
        drv, ex = self._run(POLICY_DENY)
        self.assertEqual(len(drv.taps), 1)
        self.assertEqual(drv.taps[0].text.strip(), '禁止')
        # 起始页面必须是**作答之后**的应用页，不是弹窗
        titles = [s.title for s in ex.graph.states.values()]
        self.assertIn('计算器', titles)
        self.assertNotIn('允许“计算器”使用多设备协同？', titles)

    def test_allow_policy_taps_the_allow_button(self):
        drv, _ = self._run(POLICY_ALLOW)
        self.assertEqual([t.text.strip() for t in drv.taps], ['允许'])

    def test_record_policy_observes_without_answering(self):
        """record = 保持「观察到」语义：不动作，页面照旧被当成弹窗。"""
        drv, ex = self._run(POLICY_RECORD)
        self.assertEqual(drv.taps, [])
        self.assertEqual(len(ex.permission_events), 1)
        self.assertEqual(ex.permission_events[0]['answered'], '')

    def test_every_answer_is_written_to_the_ledger(self):
        """替用户作答是有副作用的动作，台账必须留证。"""
        _, ex = self._run(POLICY_DENY)
        self.assertEqual(len(ex.permission_events), 1)
        ev = ex.permission_events[0]
        self.assertEqual(ev['policy'], POLICY_DENY)
        self.assertEqual(ev['owner'], PERMISSION_BUNDLE)
        self.assertEqual(ev['answered'], '禁止')
        self.assertTrue(ev['evidence'])

    def test_tap_failure_is_recorded_and_does_not_raise(self):
        drv, ex = self._run(POLICY_DENY, fail_tap=True)
        self.assertEqual(len(drv.taps), 1)
        self.assertEqual(len(ex.permission_events), 1)
        self.assertTrue(ex.permission_events[0]['answered'], '台账要记「打算点哪个」')

    def test_policy_is_readable_for_reports(self):
        _, ex = self._run(POLICY_DENY)
        self.assertEqual(ex.permission_policy, POLICY_DENY)
        self.assertIsInstance(ex.permission, PermissionDialogVerdict)


if __name__ == '__main__':
    unittest.main()
