"""攻击面常驻回归钉 —— 红队测试（敌意输入）的存活钉与挂账钉。

来源：真机执行日（阶段 B 首轮当晚）的红队演练，9 类敌意输入攻击。
**现在就过**的钉子锁住「宽进解析」已验证的生存能力；
**挂账钉**（expectedFailure / skip）对应已上报、待 A/B 修复的洞——
修复后把标记摘掉即可，别删测试。

| 挂账 | 洞 | 归属 |
|---|---|---|
| expectedFailure | 3000 层嵌套 JSON 裸崩 RecursionError（宽进承诺） | A（layout） |
| expectedFailure | load_case 顶层 list / 空文件未归一为 DslError | B（action） |
| skip | text_matches ReDoS（`(a+)+$` 对 28 字符回溯 16.6s） | A（matcher） |
| skip | YAML 别名炸弹（safe_load 不防，量足即挂） | B（action） |
"""
import ast
import json
import unittest

from ohauto.layout import parse_layout
from ohauto.matcher import ON


def _hostile_doc():
    """null / 错型 / 超大整数 属性的敌意树。"""
    return {'attributes': {'type': 'Root', 'text': None, 'id': 123}, 'children': [
        {'attributes': {'type': None, 'text': 'ok'}, 'bounds': '[0,0][10,10]'},
        {'attributes': {'type': 'Big'},
         'bounds': '[0,0][99999999999999999,99999999999999999]'}]}


class TestParserSurvivesHostileInput(unittest.TestCase):
    """宽进解析在敌意属性下必须存活——解析器改一行就先过这里。"""

    def test_null_and_wrongtype_attributes_survive(self):
        root = parse_layout(json.dumps(_hostile_doc()))
        nodes = list(root.walk())
        self.assertGreaterEqual(len(nodes), 3)

    def test_hostile_children_types_swallowed(self):
        for bad in ('not-a-list', {'a': 1}, 42):
            with self.subTest(bad=bad):
                root = parse_layout(json.dumps(
                    {'attributes': {'type': 'R'}, 'children': bad}))
                self.assertIsNotNone(root)

    def test_huge_integer_bounds_no_crash(self):
        root = parse_layout(json.dumps(_hostile_doc()))
        big = [n for n in root.walk() if n.type == 'Big'][0]
        cx, cy = big.center          # 只要不抛异常就算过
        self.assertIsInstance(cx, int)

    def test_matcher_on_hostile_tree(self):
        root = parse_layout(json.dumps(_hostile_doc()))
        self.assertEqual(len(ON.text_contains('ok').filter(list(root.walk()))), 1)


def _hdc_available() -> bool:
    """本机是否装了 hdc——超长文本护栏的拦截发生在 exec 层构造 Hdc 时，
    无 hdc 的机器（如 CI runner）上这条测的是环境而非护栏本身。
    探测口径与 doctor.py 一致：构造 Hdc()，抛错即视为无 hdc。"""
    try:
        from ohauto.hdc import Hdc as _H
        _H()
        return True
    except Exception:
        return False


class TestInputTextLengthGuard(unittest.TestCase):
    """50KB 文本会让 hdc 命令超 Windows 32767 上限直接炸 subprocess——
    红队实测，故超限必须响亮拒绝而不是碰运气。"""

    @unittest.skipUnless(_hdc_available(), '无 hdc：exec 层拦截在装 hdc 的机器上验')
    def test_oversized_text_rejected_loudly(self):
        from ohauto.hdc import Hdc, HdcError
        hdc = Hdc()
        with self.assertRaises(HdcError) as cm:
            hdc.input_text(1, 1, 'a' * 40000)
        self.assertIn('30000', str(cm.exception))

    def test_normal_text_passes_guard(self):
        from ohauto.hdc import Hdc
        quoted = Hdc._sh_quote("hello world \"q\" $100 It's")
        self.assertLess(len(quoted), 30000)


class TestEscapingSurvivesSimChain(unittest.TestCase):
    """转义端到端： hdc 侧单引号包裹 + sim 侧 shlex 分词，两侧必须对齐。"""

    def test_weird_text_reaches_sim_byte_identical(self):
        from ohauto.hdc import Hdc
        from ohauto.sim import FakeHdc
        from ohauto.driver import Driver
        from ohauto.matcher import ON
        import ohauto.sim as sim
        hdc = FakeHdc(start_page='login', verbose=False)
        d = Driver(bundle='com.example.app', hdc=hdc, verbose=False,
                   sleep_fn=lambda s: None)
        weird = "hello world \"quoted\" $100 It's"
        d.input(ON.id('username'), weird)
        self.assertEqual(sim.TYPED.get('username'), weird)


@unittest.expectedFailure   # 挂账 A（layout）：修复后此钉自动转正
class TestDeepNestingDegradesGracefully(unittest.TestCase):
    def test_3000_levels_degrade_not_crash(self):
        node = {'attributes': {'type': 'X'}}
        for _ in range(3000):
            node = {'attributes': {'type': 'X'}, 'children': [node]}
        root = parse_layout(json.dumps(node))   # 期望：优雅降级；现状：RecursionError
        self.assertIsNotNone(root)


@unittest.expectedFailure   # 挂账 B（action）：应归一为 DslError
class TestLoadCaseEdgeContract(unittest.TestCase):
    def test_toplevel_list_raises_dslerror(self):
        import tempfile
        from ohauto.action import load_case, DslError
        p = tempfile.mktemp(suffix='.json')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('[1,2,3]')
        with self.assertRaises(DslError):
            load_case(p)


@unittest.skip('挂账 A（matcher）：text_matches 资源上限修复后启用'
               '——现在 (a+)+$ 对 28 字符回溯 16.6s，测试会拖慢门禁')
class TestRedosGuard(unittest.TestCase):
    def test_catastrophic_regex_bounded(self):
        from ohauto.matcher import ON
        from ohauto.layout import LayoutNode, Rect
        n = LayoutNode(type='Text', text='a' * 28 + 'b', rect=Rect(0, 0, 10, 10))
        ON.text_matches(r'(a+)+$').match(n)


@unittest.skip('挂账 B（action）：YAML 别名炸弹防护后启用（safe_load 不防）')
class TestYamlBombGuard(unittest.TestCase):
    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
