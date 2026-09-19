"""`Hdc.pull` 的单元测试 —— 锁住两个静默坑。

**全部离线**，不需要设备。

★ 背景（2026-09-19 真机采集时踩到，两个坑都是**静默**的）：

1. **正斜杠盘符路径被当相对路径**
   `D:/foo/x.png` 会被 hdc 拼到 cwd 后面 → `no such file`。
   hdc **只认反斜杠形式的盘符**（`D:\\`），所以必须 `os.path.abspath()`。
   原实现只对 `makedirs` 做了规范化，**传给 hdc 的却是原始路径**。

2. **`file recv` 失败时返回码仍是 0**
   所以不能只看 `returncode`；必须检查文件真的存在且有内容。

3. **`shell cat` 兜底对二进制有害**
   实测它会把 `\\x89PNG\\r\\n` 写成 `\\x89PNG\\r\\r\\n`（CRLF 转换），
   产出**损坏但不报错**的文件。所以 `binary=True` 时必须禁用 cat 兜底。

> 第 3 条尤其危险：当时 7 个样本里 5 张截图被悄悄损坏，
> 尺寸只有几十字节，但如果没人去看文件头，就会当成正常素材交出去。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc, HdcError, ShellResult           # noqa: E402


def make_hdc(handler):
    """造一个假的 Hdc：跳过 __init__（避免依赖真实 hdc 可执行文件）。"""
    h = Hdc.__new__(Hdc)
    h.hdc_path = 'hdc'
    h.target = None
    h.timeout = 5
    h.tmp_dir = '/data/local/tmp'
    h.verbose = False
    h.calls = []

    def fake_run(args, timeout=None, check=False, binary=False, retries=0):
        h.calls.append({'args': list(args), 'binary': binary})
        out, rc = handler(list(args))
        if binary:
            res = ShellResult(rc, '<binary>', '', ' '.join(args))
            res.raw = out if isinstance(out, bytes) else b''
        else:
            res = ShellResult(rc, out if isinstance(out, str) else '',
                              '', ' '.join(args))
        return res

    h.run = fake_run
    return h


def always_fail(_args):
    """模拟 file recv 失败：不创建文件、rc 仍为 0（真实行为）。"""
    return ('[Fail]Error opening file: no such file or directory', 0)


def always_succeed(payload=b'\x89PNG\r\n\x1a\n' + b'\x00' * 64):
    """模拟 file recv 成功：创建目标文件并写入内容。"""
    def handler(args):
        if args and args[0] == 'file':
            local = args[-1]
            os.makedirs(os.path.dirname(local) or '.', exist_ok=True)
            with open(local, 'wb') as f:
                f.write(payload)
            return ('FileTransfer finish', 0)
        return ('', 0)
    return handler


class TestPullPathNormalization(unittest.TestCase):
    """坑 1：路径必须规范化成 hdc 认得的形式。"""

    def test_sends_absolute_path_not_raw(self):
        """★ 传给 hdc 的必须是 abspath 结果。

        在 Windows 上 `os.path.abspath('D:/foo/x.png')` → `D:\\foo\\x.png`，
        而 hdc 只认后者；传原始正斜杠路径会被当相对路径。
        """
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, 'sub', 'x.png')
            h = make_hdc(always_succeed())
            h.pull('/data/local/tmp/x.png', raw, binary=True)

            sent = h.calls[0]['args'][-1]
            self.assertEqual(sent, os.path.abspath(raw),
                             'pull 必须把路径规范化后再传给 hdc')

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            nested = os.path.join(d, 'a', 'b', 'c', 'x.png')
            h = make_hdc(always_succeed())
            h.pull('/data/local/tmp/x.png', nested, binary=True)
            self.assertTrue(os.path.exists(nested))

    def test_returns_absolute_path(self):
        with tempfile.TemporaryDirectory() as d:
            h = make_hdc(always_succeed())
            got = h.pull('/data/local/tmp/x.png',
                         os.path.join(d, 'x.png'), binary=True)
            self.assertTrue(os.path.isabs(got))


class TestPullFailureDetection(unittest.TestCase):
    """坑 2：rc=0 也可能是失败 —— 必须看文件本身。"""

    def test_raises_when_rc_zero_but_file_missing(self):
        """★ 这是最关键的一条：rc=0 但文件没生成 → 必须抛错，不能静默返回。"""
        with tempfile.TemporaryDirectory() as d:
            h = make_hdc(always_fail)
            with self.assertRaises(HdcError):
                h.pull('/data/local/tmp/x.json', os.path.join(d, 'x.json'))

    def test_raises_when_file_empty(self):
        """文件存在但 0 字节也算失败。"""
        def handler(args):
            if args and args[0] == 'file':
                open(args[-1], 'wb').close()
            return ('', 0)
        with tempfile.TemporaryDirectory() as d:
            h = make_hdc(handler)
            with self.assertRaises(HdcError):
                h.pull('/data/local/tmp/x.json', os.path.join(d, 'x.json'))

    def test_stale_file_removed_before_transfer(self):
        """★ 先删残留：否则上一步的旧文件会让失败的传输看起来成功。"""
        with tempfile.TemporaryDirectory() as d:
            tgt = os.path.join(d, 'x.png')
            with open(tgt, 'wb') as f:
                f.write(b'STALE CONTENT FROM LAST RUN')
            h = make_hdc(always_fail)
            with self.assertRaises(HdcError):
                h.pull('/data/local/tmp/x.png', tgt, binary=True)
            self.assertFalse(os.path.exists(tgt),
                             '失败后不应留下旧文件冒充成功')


class TestPullBinarySafety(unittest.TestCase):
    """坑 3：二进制不能用 cat 兜底（会被 CRLF 转换损坏）。"""

    def test_binary_failure_does_not_fall_back_to_cat(self):
        """★ binary=True 失败时必须直接报错，不去试 cat。"""
        with tempfile.TemporaryDirectory() as d:
            h = make_hdc(always_fail)
            with self.assertRaises(HdcError):
                h.pull('/data/local/tmp/x.png', os.path.join(d, 'x.png'),
                       binary=True)
            cmds = [c['args'] for c in h.calls]
            self.assertFalse(
                any(a and a[0] == 'shell' for a in cmds),
                f'二进制不应走 shell cat 兜底，实际命令: {cmds}')

    def test_text_failure_may_fall_back_to_cat(self):
        """文本文件允许 cat 兜底（保持向后兼容）。"""
        def handler(args):
            if args and args[0] == 'file':
                return ('fail', 0)                     # file recv 失败
            if args and args[0] == 'shell':
                return (b'{"ok": true}', 0)            # cat 兜底成功
            return ('', 0)

        with tempfile.TemporaryDirectory() as d:
            tgt = os.path.join(d, 'x.json')
            h = make_hdc(handler)
            h.pull('/data/local/tmp/x.json', tgt)
            self.assertTrue(os.path.exists(tgt))
            self.assertEqual(open(tgt, 'rb').read(), b'{"ok": true}')

    def test_cat_fallback_passes_binary_flag(self):
        """cat 兜底本身必须以二进制方式读，避免 Python 侧再转换一次。"""
        def handler(args):
            if args and args[0] == 'file':
                return ('fail', 0)
            if args and args[0] == 'shell':
                return (b'data', 0)
            return ('', 0)

        with tempfile.TemporaryDirectory() as d:
            h = make_hdc(handler)
            h.pull('/data/local/tmp/x.json', os.path.join(d, 'x.json'))
            cat_calls = [c for c in h.calls
                         if c['args'] and c['args'][0] == 'shell']
            self.assertTrue(cat_calls)
            self.assertTrue(cat_calls[0]['binary'],
                            'cat 兜底必须 binary=True')


class TestPullSuccess(unittest.TestCase):

    def test_binary_success_keeps_bytes_intact(self):
        """成功的二进制传输必须逐字节保真（这正是 cat 做不到的）。"""
        payload = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 4
        with tempfile.TemporaryDirectory() as d:
            tgt = os.path.join(d, 'x.png')
            h = make_hdc(always_succeed(payload))
            h.pull('/data/local/tmp/x.png', tgt, binary=True)
            self.assertEqual(open(tgt, 'rb').read(), payload)

    def test_no_shell_fallback_when_file_recv_works(self):
        """正常路径下不应触发任何兜底。"""
        with tempfile.TemporaryDirectory() as d:
            h = make_hdc(always_succeed())
            h.pull('/data/local/tmp/x.png', os.path.join(d, 'x.png'),
                   binary=True)
            self.assertEqual(len(h.calls), 1, '成功时只应调用一次 hdc')
            self.assertEqual(h.calls[0]['args'][:2], ['file', 'recv'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
