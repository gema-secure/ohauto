"""版本号单源化 —— 防止 `pyproject.toml` 之外再出现第二份版本号。

口径：

    1. `ohauto.__version__` 必须是合法版本串，且不是回退值；
    2. 它与 `pyproject.toml` 的 `[project].version` 一致；
    3. 本环境若装了 ohauto 发行元数据，元数据也必须一致；
    4. `ohauto/*.py` 里不允许再出现 `__version__ = "<字面量>"` 赋值。

第 4 条是防反弹的关键：前三条在「有人把两处一起改回硬编码」时**照样通过**，
只有扫描源码才能拦住。全部离线，不需要设备，也不依赖 pyyaml。
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ohauto                                                # noqa: E402

_PYPROJECT = os.path.join(ROOT, 'pyproject.toml')
PKG_DIR = os.path.join(ROOT, 'ohauto')


def _pyproject_version() -> str:
    """读 `pyproject.toml` 里的 `[project].version`。"""
    with open(_PYPROJECT, encoding='utf-8') as f:
        found = re.search(r'^version\s*=\s*["\']([^"\']+)', f.read(),
                          re.MULTILINE)
    return found.group(1) if found else ''


class VersionSingleSourceTest(unittest.TestCase):

    def test_version_looks_like_a_release(self):
        self.assertRegex(ohauto.__version__, r'^\d+\.\d+\.\d+')
        self.assertNotIn('unknown', ohauto.__version__)

    def test_version_matches_pyproject(self):
        self.assertEqual(ohauto.__version__, _pyproject_version())

    def test_version_matches_distribution_metadata_when_installed(self):
        from importlib.metadata import PackageNotFoundError, version
        try:
            dist = version('ohauto')
        except PackageNotFoundError:
            self.skipTest('未安装发行元数据（源码直用），回退到 pyproject 同名源')
        self.assertEqual(dist, ohauto.__version__)

    def test_package_has_no_hardcoded_version_literal(self):
        pattern = re.compile(r"^\s*__version__\s*=\s*['\"]")
        hits = []
        for name in sorted(os.listdir(PKG_DIR)):
            if not name.endswith('.py'):
                continue
            with open(os.path.join(PKG_DIR, name), encoding='utf-8') as f:
                for lineno, line in enumerate(f, 1):
                    if pattern.match(line):
                        hits.append(f'ohauto/{name}:{lineno}')
        self.assertEqual(hits, [],
                         f'版本号字面量必须回到 pyproject.toml：{hits}')


if __name__ == '__main__':
    unittest.main()
