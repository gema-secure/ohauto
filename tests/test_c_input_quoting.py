"""`Hdc.input_text` 的 shell 转义 —— 钉住「文本含空格被拆散」这个坑。

**全部离线**，不需要设备。

★ 背景：设备侧 shell 会对 `hdc shell` 过来的命令**重新分词**。
原先 `input_text()` 把文本用空格直接拼进 `uitest uiInput inputText x y <文本>`，
含空格/引号/`$` 的文本会被拆成多个参数 —— 而第 393 行的注释
声称「已保证被当作单个参数传入」，**声称做到了但实际没做**。

修法是 POSIX 单引号包裹（`Hdc._sh_quote`）。这条最容易翻车的不是引号本身，
而是**两侧的分词规则必须一致**：hdc 加了引号，`sim.py` 就必须按同一套 POSIX 规矩
还原（`shlex.split`），否则模拟环境会把引号当成文本的一部分写进去 ——
「模拟环境绿、真机挂」的经典假通过。下面最后一类测试就是钉这个的。
"""
import os
import shlex
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc, ShellResult, _UitestBackend   # noqa: E402
from ohauto.sim import FakeHdc               # noqa: E402


def make_recording_hdc():
    """造一个只记录命令、不做任何真实执行的 Hdc。"""
    h = Hdc.__new__(Hdc)
    h.hdc_path = 'hdc'
    h.target = None
    h.timeout = 5
    h.tmp_dir = '/data/local/tmp'
    h.verbose = False
    h.calls = []
    # 手工构造必须与 `Hdc.__init__` 对齐：写动作现在经 backend 派发，
    # 少了这个属性 `input_text` 会直接 AttributeError。
    h._backend = _UitestBackend(h)

    def fake_run(args, timeout=None, check=False, binary=False, retries=0):
        h.calls.append(list(args))
        joined = ' '.join(str(a) for a in args)
        return ShellResult(0, '', '', joined)

    h.run = fake_run
    return h


def _cmd_of(h):
    """取出被下发到 `run()` 的命令 args。"""
    return h.calls[-1]


# 一批「UI 测试里真的会输入」的文本 —— 不是刻意刁难
_TRICKY = [
    'hello',                    # 无空格（回归：不能因为改了就坏）
    'hello world',              # 单个空格
    '张 三 的 名字',             # 多个空格 + 中文
    '$100 & `whoami`',          # shell 元字符
    "it's fine",                # 内含单引号（唯一需要转义的字符）
    '{"name": "a b"}',          # JSON 片段
    '',                         # 空输入
]


class TestShQuote(unittest.TestCase):
    """`_sh_quote` 本身：包出来的东西必须能被 POSIX shell 还原成原文。"""

    def test_roundtrip(self):
        for t in _TRICKY:
            with self.subTest(text=t):
                quoted = Hdc._sh_quote(t)
                got = shlex.split('x ' + quoted)[1] if t else ''
                self.assertEqual(got, t,
                                 '带引号的文本经 shell 分词后应与原文一致')

    def test_empty_is_quoted_pair(self):
        self.assertEqual(Hdc._sh_quote(''), "''")


class TestInputTextQuoting(unittest.TestCase):
    """命令串层面：文本必须作为一个整体参数下去。"""

    def test_text_is_quoted(self):
        h = make_recording_hdc()
        h.input_text(100, 200, 'hello world')
        cmd = _cmd_of(h)[-1]
        self.assertIn("inputText 100 200 'hello world'", cmd,
                      '文本必须整体被单引号包成一个参数')

    def test_space_does_not_split(self):
        """核心回归：含空格的文本不能被拆成多个参数。

        用 shlex 反解（等价于设备侧 shell 的分词结果），第 6 个词必须是完整文本。
        """
        for t in _TRICKY:
            with self.subTest(text=t):
                h = make_recording_hdc()
                h.input_text(10, 20, t)
                parts = shlex.split(_cmd_of(h)[-1])
                self.assertEqual(parts[:3], ['uitest', 'uiInput', 'inputText'])
                self.assertEqual(parts[3], '10')
                self.assertEqual(parts[4], '20')
                self.assertEqual(len(parts), 6, '不该被拆出超过 6 个词')
                self.assertEqual(parts[5], t, '原文应完整到达，不多不少')

    def test_meta_chars_are_literal(self):
        h = make_recording_hdc()
        h.input_text(1, 2, '$HOME & `id`')
        cmd = _cmd_of(h)[-1]
        self.assertIn("'$HOME & `id`'", cmd,
                      '$ / 反引号 / & 必须退化成字面字符')

    def test_inner_single_quote_escaped(self):
        h = make_recording_hdc()
        h.input_text(1, 2, "it's")
        parts = shlex.split(_cmd_of(h)[-1])
        self.assertEqual(parts[5], "it's")

    def test_coordinates_stay_plain(self):
        """坐标仍必须是裸整数 —— 加引号会让设备侧拿不到坐标。"""
        h = make_recording_hdc()
        h.input_text(100, 200, 'x')
        parts = shlex.split(_cmd_of(h)[-1])
        self.assertEqual(parts[3:5], ['100', '200'])


class TestSimMatchesHdc(unittest.TestCase):
    """两侧一致性：hdc 拼出来的命令，sim 必须还原出**同一个文本**。

    这条是整套的名字缘由 —— 单引号是加给设备侧 shell 看的，
    `sim.py` 若用的是 `str.split()`，引号会被当成文本的一部分留在记录里，
    于是模拟环境与真机就此分叉。
    """

    def test_sim_sees_the_same_text(self):
        sim = FakeHdc(start_page='login')
        for t in _TRICKY:
            with self.subTest(text=t):
                h = make_recording_hdc()
                h.input_text(10, 20, t)
                before = len(sim.actions)
                sim.run(_cmd_of(h))          # 走的是与真机相同的 shell 通道
                new = sim.actions[before:]
                self.assertTrue(new, 'sim 应该记录到这次 inputText')
                rec = new[0]
                self.assertEqual(rec['action'], 'inputText')
                self.assertEqual(rec['text'], t,
                                 '引号只属于传输层，不该混进文本内容')

    def test_sim_does_not_keep_quotes(self):
        """反向：直接喂一条带引号的命令，记录里不能有裸露的引号。"""
        sim = FakeHdc(start_page='login')
        sim.shell("uitest uiInput inputText 5 5 'hi there'")
        rec = sim.actions[-1]
        self.assertEqual(rec['text'], 'hi there')
        self.assertNotIn("'", rec['text'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
