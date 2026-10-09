"""数据集单一副本 —— 同一份样本只允许一个物理副本。

为什么要有这条：真机样本曾被按「日期目录 + 测试内副本」的方式复制，
同一张截图与控件树在仓库里最多出现三份（54 个文件 / 4.07 MB）。
副本不会自动跟随原件更新，还会让人分不清哪份是准的；
而且它们逐字节相同，属于纯粹的仓库体积浪费。

检查范围只有两个资产目录（`datasets/` 与 `tests/fixtures/`）。其它目录
可能有正当的同内容文件（导出产物、本地备份），不在这里下判断。
"""
import hashlib
import os
import sys
import unittest
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

ASSET_DIRS = (os.path.join(ROOT, 'datasets'),
              os.path.join(ROOT, 'tests', 'fixtures'))


def _sha256(path: str) -> str:
    """流式算摘要：单个样本可能接近 1 MB，不值得整体读进内存。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_files(base: str):
    """遍历目录下的文件（跳过 `__pycache__`）。"""
    for cur, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for name in names:
            yield os.path.join(cur, name)


class DatasetSingleCopyTest(unittest.TestCase):

    def test_no_byte_identical_duplicates_across_asset_dirs(self):
        seen = defaultdict(list)
        for base in ASSET_DIRS:
            for path in _walk_files(base):
                seen[_sha256(path)].append(os.path.relpath(path, ROOT))
        dups = {h: paths for h, paths in seen.items() if len(paths) > 1}
        detail = '\n'.join('  ' + ' == '.join(sorted(paths))
                           for paths in sorted(dups.values()))
        self.assertEqual(dups, {},
                         '发现逐字节相同的重复样本，请合并为单一副本：\n' + detail)


if __name__ == '__main__':
    unittest.main()
