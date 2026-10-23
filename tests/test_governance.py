"""1E 治理机制的钉子 —— LAY001 分层闸 + EXC001 宽泛 except 棘轮。

两条规则都在 tools/static_check.py 里，本文件钉住它们的行为边界：
    * 分层闸：低层 import 高层必须报 error（S7 的机器化固化）；
    * 棘轮：无标记宽泛 except 超基线报 error、不超报 warning、带标记不计数；
    * 全仓现状：零 LAY001 / 零 EXC001 error —— 这就是「只减不增」的起点。

与 test_export_hypium.py 同风格：tools 不是包，靠 sys.path 挂仓库根。
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import static_check                            # noqa: E402
from tools.static_check import (                          # noqa: E402
    BROAD_EXCEPT_BASELINE,
    _check_broad_except,
    _check_layering,
)


def _layer(rel: str, src: str):
    """对伪造源码跑分层闸，返回 findings。"""
    return _check_layering('<test>', rel, ast.parse(src))


def _broad(rel: str, src: str):
    """对伪造源码跑宽泛 except 棘轮，返回 finding（可为 None）。"""
    return _check_broad_except('<test>', rel, src, ast.parse(src))


SRC_PLAIN_IMPORT = 'from .explorer import Explorer\n'


class TestLayerGate(unittest.TestCase):
    """LAY001：低层不得 import 高层。"""

    def test_l3_import_l4_is_error(self):
        """S7 原案重演：locator(L3) import explorer(L4) 必须被拦下。"""
        findings = _layer('ohauto/locator.py', SRC_PLAIN_IMPORT)
        self.assertEqual(1, len(findings))
        self.assertEqual('error', findings[0].level)
        self.assertEqual('LAY001', findings[0].code)
        self.assertIn('explorer', findings[0].message)

    def test_l1_import_l2_is_error(self):
        findings = _layer('ohauto/identity.py', 'from .driver import Driver\n')
        self.assertEqual(1, len(findings))
        self.assertIn('L1', findings[0].message)

    def test_absolute_import_path_covered(self):
        """绝对导入（from ohauto.xxx）也必须覆盖 —— doctor.py 真实存在这条。"""
        findings = _layer('ohauto/identity.py',
                          'from ohauto.driver import Driver\n')
        self.assertEqual(1, len(findings))

    def test_same_layer_ok(self):
        """同层互相引用不拦（runner/signals/diagnose 在 L4 内部成环是既有事实）。"""
        self.assertEqual([], _layer('ohauto/signals.py',
                                    'from .runner import Runner\n'))

    def test_downward_ok(self):
        self.assertEqual([], _layer('ohauto/explorer.py',
                                    'from .action import tap\n'))

    def test_absolute_same_layer_ok(self):
        self.assertEqual([], _layer('ohauto/doctor.py',
                                    'import ohauto.hdc\n'))

    def test_from_package_import_member_form(self):
        """`from ohauto import xxx` 形态：取导入名判层。"""
        findings = _layer('ohauto/matcher.py',
                          'from ohauto import generator\n')
        self.assertEqual(1, len(findings))

    def test_init_facade_exempt(self):
        """__init__.py 是对外门面（re-export 一切），不参与分层检查。"""
        self.assertEqual([], _layer('ohauto/__init__.py',
                                    'from .explorer import Explorer\n'))

    def test_tools_not_checked(self):
        self.assertEqual([], _layer('tools/demo_onepager.py',
                                    'from ohauto.explorer import Explorer\n'))


class TestBroadExceptRatchet(unittest.TestCase):
    """EXC001：无标记的 except Exception 只减不增。"""

    SRC_TWO = ('def f():\n'
               '    try:\n'
               '        a()\n'
               '    except Exception:\n'
               '        pass\n'
               '    try:\n'
               '        b()\n'
               '    except Exception:\n'
               '        pass\n')

    SRC_MARKED = ('def f():\n'
                  '    try:\n'
                  '        a()\n'
                  '    except Exception:  # noqa: BLE001 —— 外部输入解析\n'
                  '        pass\n')

    SRC_THREE = SRC_TWO + ('    try:\n'
                           '        c()\n'
                           '    except Exception:\n'
                           '        pass\n')

    def test_new_file_unmarked_is_error(self):
        """不在基线表里的文件，一处无标记即 error（新增必须带标记）。"""
        f = _broad('ohauto/brand_new_module.py',
                   'try:\n    a()\nexcept Exception:\n    pass\n')
        self.assertIsNotNone(f)
        self.assertEqual('error', f.level)
        self.assertEqual('EXC001', f.code)

    def test_marked_not_counted(self):
        """带 # noqa 标记的不进计数 —— 治理逼的是留痕，不是消灭。"""
        self.assertIsNone(_broad('ohauto/brand_new_module.py', self.SRC_MARKED))

    def test_at_baseline_is_warning(self):
        """恰好等于基线 → warning（存量，逐步清）。"""
        f = _broad('ohauto/locator.py', self.SRC_TWO)   # locator 基线 = 2
        self.assertIsNotNone(f)
        self.assertEqual('warning', f.level)
        self.assertIn('2/基线 2', f.message)

    def test_above_baseline_is_error(self):
        """超出基线 → error（反弹即阻断）。"""
        f = _broad('ohauto/locator.py', self.SRC_THREE)
        self.assertIsNotNone(f)
        self.assertEqual('error', f.level)
        self.assertIn('新增 1 处', f.message)

    def test_tuple_with_exception_counted(self):
        """元组里混着 Exception 也算宽泛捕获。"""
        src = ('try:\n'
               '    a()\n'
               'except (ValueError, Exception):  # noqa: 计数豁免由下一处验证\n'
               '    pass\n'
               'try:\n'
               '    b()\n'
               'except (Exception, KeyError):\n'
               '    pass\n')
        f = _broad('ohauto/brand_new_module.py', src)
        self.assertIsNotNone(f)          # 第二处无标记 → error
        self.assertEqual('error', f.level)


class TestRepoState(unittest.TestCase):
    """全仓现状：治理闸零 error —— 「只减不增」从干净起点出发。"""

    def test_no_governance_errors_in_repo(self):
        findings = static_check.run(root=ROOT, quiet=True)
        bad = [f for f in findings
               if f.code in ('LAY001', 'EXC001') and f.level == 'error']
        self.assertEqual([], bad)

    def test_baseline_files_all_exist(self):
        """基线表里的文件必须都真实存在 —— 死条目会掩盖「该文件已删除」。"""
        for rel in BROAD_EXCEPT_BASELINE:
            self.assertTrue(
                os.path.isfile(os.path.join(ROOT, *rel.split('/'))),
                f'基线条目指向不存在的文件: {rel}')

    def test_layers_cover_all_package_modules(self):
        """层级表必须覆盖 ohauto/ 全部模块 —— 漏登记 = 该模块游离在闸外。"""
        pkg = os.path.join(ROOT, 'ohauto')
        mods = {n[:-3] for n in os.listdir(pkg)
                if n.endswith('.py') and n != '__init__.py'}
        self.assertEqual(set(), mods - set(static_check._LAYER_OF),
                         '有模块未登记进 LAYERS')
        self.assertEqual(
            set(), set(static_check._LAYER_OF) - mods - {'static_arkts'},
            'LAYERS 里有不存在（或子包占位之外）的模块')


if __name__ == '__main__':
    unittest.main()
