"""攻击面常驻回归钉 —— 红队测试（敌意输入）的存活钉与挂账钉。

来源：真机执行日（阶段 B 首轮当晚）的红队演练，9 类敌意输入攻击。
**现在就过**的钉子锁住「宽进解析」已验证的生存能力；
**挂账钉**（expectedFailure / skip）对应已上报、待 A/B 修复的洞——
修复后把标记摘掉即可，别删测试。

| 挂账 | 洞 | 所在模块 |
|---|---|---|

**四条挂账已全部收口**（常驻钉，不再是 skip / expectedFailure）：

- `text_matches` 的 ReDoS —— 危险形状构造期拒绝、超长输入走降级台账（`TestRegexResourceGuard`）；
- 3000 层嵌套与 `[Fail]...` 设备失败文本 —— 解析前拒绝 + 原因可读，
  超树深的输入截断留痕（`TestDeepNestingDegradesGracefully` / `TestLayoutInputErrors`）；
- `load_case` 顶层非映射 / 空文件 / tab 缩进 —— 一律归一成 `DslError`（`TestLoadCaseEdgeContract`）；
- YAML 别名炸弹 —— 实测**不成立**（别名是引用共享），改为常驻性质钉；
  真正的风险（嵌套深度、超大文本）另有闸（`TestYamlInputGuards`）。
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
    探测口径与 ohauto.doctor 一致：构造 Hdc()，抛错即视为无 hdc。"""
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


def _nested_tree_json(depth: int) -> str:
    """拼出 depth 层控件树的 JSON **文本**。

    ⚠️ 不能用 `json.dumps(嵌套 dict)` 造：那个 dict 要递归构建、dumps 也要递归，
    测的就是测试自己先撞栈，而不是解析器。
    """
    return ('{"attributes": {"type": "X"}, "children": [' * depth
            + '{"attributes": {"type": "X"}}' + ']}' * depth)


class TestDeepNestingDegradesGracefully(unittest.TestCase):
    """宽进承诺的另一半：敌意输入不许把解析器打崩。

    崩塌点有**两处**，两处都挡：`json.loads` 内部（递归，拦不住就是标准库崩）
    和 `_build` 的递归。
    """

    def test_3000_levels_refused_loudly_not_crashed(self):
        """3000 层：连 `json.loads` 都解析不了，**在解析前**拒绝并说明原因。

        为什么不是"尽力解析前半段"：标准库的 JSON 解析器本身就是递归的，
        想让它吃下 3000 层只能抬高 `setrecursionlimit` —— 那是在拿 C 栈赌命，
        崩起来是**段错误**（不可捕获），比一个能 catch 的异常糟得多。
        """
        from ohauto.layout import LayoutParseError
        with self.assertRaises(LayoutParseError) as cm:
            parse_layout(_nested_tree_json(3000))
        self.assertEqual(cm.exception.reason, 'too_deep')
        self.assertIn('嵌套', str(cm.exception))

    def test_deep_but_parseable_tree_is_truncated_with_a_mark(self):
        """能解析、但超过树深上限的：截断 + 留痕，别静默丢子树。"""
        from ohauto.layout import MAX_TREE_DEPTH
        root = parse_layout(_nested_tree_json(400))
        deepest, node = 0, root
        while node.children:
            node = node.children[0]
            deepest += 1
        self.assertLess(deepest, MAX_TREE_DEPTH)
        self.assertTrue(getattr(node, 'truncated_children', 0))

    def test_normal_depth_is_untouched(self):
        """正常深度的树一个节点都不许少（拒绝面不能误伤）。"""
        root = parse_layout(_nested_tree_json(30))
        self.assertEqual(len(list(root.walk())), 31)
        self.assertFalse(any(hasattr(n, 'truncated_children')
                             for n in root.walk()))

    def test_deep_dict_input_is_truncated_too(self):
        """不走文本的路径（直接喂 dict）同样受树深上限保护。"""
        from ohauto.layout import MAX_TREE_DEPTH
        node = {'attributes': {'type': 'X'}}
        for _ in range(400):
            node = {'attributes': {'type': 'X'}, 'children': [node]}
        root = parse_layout(node)
        self.assertLess(len(list(root.walk())), MAX_TREE_DEPTH + 1)


class TestLayoutInputErrors(unittest.TestCase):
    """输入问题要**说得清**，不能崩成 JSONDecodeError ——
    那会让人以为是代码 bug，而真相往往是设备没插。"""

    def test_device_fail_text_is_identified(self):
        from ohauto.layout import LayoutParseError
        dev = '[Fail]Not match target founded, check config or confirm the key'
        with self.assertRaises(LayoutParseError) as cm:
            parse_layout(dev)
        self.assertEqual(cm.exception.reason, 'device_fail')
        self.assertIn('设备', str(cm.exception))

    def test_plain_garbage_is_not_json(self):
        from ohauto.layout import LayoutParseError
        with self.assertRaises(LayoutParseError) as cm:
            parse_layout('hello world')
        self.assertEqual(cm.exception.reason, 'not_json')

    def test_empty_string_is_empty_reason(self):
        from ohauto.layout import LayoutParseError
        with self.assertRaises(LayoutParseError) as cm:
            parse_layout('   ')
        self.assertEqual(cm.exception.reason, 'empty')

    def test_error_type_stays_a_valueerror(self):
        """归一成 ValueError 子类 —— 既有 `except ValueError` 的调用方不受影响。"""
        from ohauto.layout import LayoutParseError
        self.assertTrue(issubclass(LayoutParseError, ValueError))
        with self.assertRaises(ValueError):
            parse_layout('[Fail]whatever')


class TestLoadCaseEdgeContract(unittest.TestCase):
    """边界输入一律归一成 `DslError` —— 用例的规格问题要留在 DSL 家族里，
    不能让归因链把它读成引擎故障。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, name: str, text: str) -> str:
        import os
        p = os.path.join(self._tmp.name, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
        return p

    def test_toplevel_list_raises_dslerror(self):
        from ohauto.action import load_case, DslError
        with self.assertRaises(DslError) as cm:
            load_case(self._write('list.json', '[1,2,3]'))
        self.assertIn('映射', str(cm.exception))

    def test_empty_file_raises_dslerror(self):
        from ohauto.action import load_case, DslError
        with self.assertRaises(DslError) as cm:
            load_case(self._write('empty.yaml', ''))
        self.assertIn('空文件', str(cm.exception))

    def test_tab_indented_yaml_raises_dslerror(self):
        """YAML 用 tab 缩进是硬错误，要说得清是哪一类，而不是裸抛 ScannerError。"""
        from ohauto.action import load_case, DslError
        with self.assertRaises(DslError) as cm:
            load_case(self._write('tab.yaml', 'name: x\nsteps:\n\t- waitIdle: true\n'))
        self.assertIn('YAML', str(cm.exception))

    def test_scalar_toplevel_raises_dslerror(self):
        from ohauto.action import load_case, DslError
        with self.assertRaises(DslError):
            load_case('just a string')

    def test_valid_case_still_loads(self):
        """拒绝面不能误伤正常用例。"""
        from ohauto.action import load_case
        case = load_case(self._write('ok.yaml',
                                     'name: 演示\nsteps:\n  - waitIdle: true\n'))
        self.assertEqual(case['name'], '演示')
        self.assertEqual(len(case['steps']), 1)


class TestRegexResourceGuard(unittest.TestCase):
    """正则的来源不可信（用例 YAML 里能写，模型生成的规格里也能写）——
    危险形状在**构造期**拒绝，超长输入走降级而不是硬跑。"""

    @staticmethod
    def _node(text):
        from ohauto.layout import LayoutNode, Rect
        return LayoutNode(type='Text', text=text, rect=Rect(0, 0, 10, 10))

    def test_catastrophic_shapes_refused_loudly(self):
        from ohauto.matcher import ON, RegexSafetyError
        for pattern in (r'(a+)+$', r'(a*)*$', r'(\w+\s?)+$', r'(a+){2,}$'):
            with self.subTest(pattern=pattern):
                with self.assertRaises(RegexSafetyError):
                    ON.text_matches(pattern)

    def test_safe_patterns_still_work(self):
        """拒绝面必须**只**覆盖危险形状 —— 括号套量词是正常写法，别误杀。"""
        from ohauto.matcher import ON
        cases = [(r'^第\d+页$', '第3页', 1), (r'^忘记', '忘记密码', 1),
                 (r'^(登录|注册)$', '登录', 1), (r'(\w+)\s*=\s*(\w+)', 'a = b', 1),
                 (r'abc{2}', 'abcc', 1), (r'^第\d+页$', '首页', 0)]
        for pattern, text, want in cases:
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    len(ON.text_matches(pattern).filter([self._node(text)])), want)

    def test_invalid_regex_is_a_safety_error(self):
        from ohauto.matcher import ON, RegexSafetyError
        with self.assertRaises(RegexSafetyError):
            ON.text_matches('(unclosed')

    def test_overlong_text_degrades_and_is_recorded(self):
        """超长输入不硬跑：不匹配、不阻塞，且**记进台账**（不许静默返回 False）。"""
        import time
        from ohauto.matcher import ON, MAX_REGEX_INPUT, REGEX_DEGRADATIONS
        m = ON.text_matches(r'(a|b)+$')
        node = self._node('ab' * (MAX_REGEX_INPUT + 10))
        t0 = time.perf_counter()
        self.assertEqual(m.filter([node]), [])
        self.assertLess(time.perf_counter() - t0, 0.1)
        self.assertTrue(m.degradations)
        self.assertIn('跳过', m.degradations[0])
        self.assertIn(m.degradations[0], REGEX_DEGRADATIONS)
        self.assertIn('降级', str(m))

    def test_degradation_survives_chaining(self):
        """`nth()` 会新建 Matcher，降级台账必须跟着走 —— 否则上层读不到。"""
        from ohauto.matcher import ON, MAX_REGEX_INPUT
        m = ON.text_matches(r'(a|b)+$').nth(0)
        m.filter([self._node('ab' * (MAX_REGEX_INPUT + 10))])
        self.assertTrue(m.degradations)

    def test_adversarial_text_at_the_cap_still_bounded(self):
        """贴着上限的敌意长串也不能拖慢（能回溯的是长度，不是形状）。"""
        import time
        from ohauto.matcher import ON, MAX_REGEX_INPUT
        m = ON.text_matches(r'^a+b$')
        t0 = time.perf_counter()
        m.match(self._node('a' * MAX_REGEX_INPUT))
        self.assertLess(time.perf_counter() - t0, 0.05)

    def test_dsl_layer_turns_refusal_into_dslerror(self):
        """用例 YAML 里写了危险正则 → 应该是「用例缺陷」，不是引擎崩。"""
        from ohauto.action import spec_to_matcher, DslError
        with self.assertRaises(DslError):
            spec_to_matcher({'text_matches': r'(a+)+$'})


def _alias_bomb(levels: int = 30, fan: int = 12) -> str:
    """经典「别名炸弹」文本：每层用上一层的引用铺 fan 份。"""
    lines = ['a0: &a0 ["x","x","x","x","x","x","x","x","x","x"]']
    for i in range(1, levels + 1):
        lines.append('a%d: &a%d [%s]'
                     % (i, i, ','.join(['*a%d' % (i - 1)] * fan)))
    return '\n'.join(lines) + '\n'


class TestYamlInputGuards(unittest.TestCase):
    """红队洞②的**实测结论**：别名炸弹在 PyYAML 上不成立。

    别名是**引用共享**而不是拷贝（`b: [*x, *x]` 里 `b[0] is b[1] is x`），
    对象图不会指数膨胀，所以这里不需要"防护"，需要的是把这个性质**钉住**，
    免得以后有人把 `safe_load` 换成会深拷贝的加载器而没人发现。
    真正会崩的是嵌套深度，那是 `load_case` 的闸在管。
    """

    def test_classic_alias_bomb_does_not_expand(self):
        import time
        from ohauto.action import load_case
        text = _alias_bomb()
        self.assertLess(len(text), 4096)             # 炸弹文本本身只有 2 KB
        t0 = time.perf_counter()
        data = load_case(text)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertIs(data['a1'][0], data['a0'])     # 引用共享，不是拷贝

    def test_deep_flow_nesting_refused(self):
        from ohauto.action import load_case, DslError
        with self.assertRaises(DslError) as cm:
            load_case('name: x\nsteps: ' + '[' * 5000 + ']' * 5000 + '\n')
        self.assertIn('嵌套过深', str(cm.exception))

    def test_deep_block_nesting_refused(self):
        """块状缩进不走括号闸 —— 靠撞栈处兜回来，同样要归成 DslError。"""
        from ohauto.action import load_case, DslError
        text = (''.join('  ' * i + f'k{i}:\n' for i in range(800))
                + '  ' * 800 + 'v: 1\n')
        with self.assertRaises(DslError):
            load_case(text)

    def test_oversized_text_refused(self):
        from ohauto.action import load_case, DslError, MAX_CASE_TEXT_BYTES
        with self.assertRaises(DslError) as cm:
            load_case('name: x\nsteps: []\n# ' + 'A' * MAX_CASE_TEXT_BYTES)
        self.assertIn('超过上限', str(cm.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
