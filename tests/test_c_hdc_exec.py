"""`Hdc` 执行层（`ohauto/hdc.py`）的离线测试 —— 不碰真机、不碰 hdc 二进制。

为什么单开一个文件：`tests/` 里原有的一百多处 hdc 断言全部走 `sim.FakeHdc`
（模拟设备），**真正的 `Hdc` 类只被 `__init__` 的错误分支碰到过** ——
于是「命令怎么拼、失败怎么报、重试怎么算」这条最贴近设备的一层长期只覆盖
54%。这层出错的代价最大（它对面是硬件），所以单独钉。

手法：把 `ohauto.hdc.subprocess`（以及 `time`）换成桩，逐条断言**命令形状**
与**返回/异常语义**。全离线、可重复、不依赖本机装没装 hdc。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import Hdc, HdcError, DeviceNotFound, ShellResult   # noqa: E402

FAKE_PATH = 'fake-hdc'


def _proc(rc=0, stdout=b'', stderr=b''):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class _FakeSubprocess:
    """替掉 `ohauto.hdc` 里的 `subprocess` 模块。

    只实现调用方真正用到的两样：`run` 与 `TimeoutExpired`
    （`run` 的 except 子句引用后者，缺了会 AttributeError）。
    """

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), dict(kw)))
        return self._handler(list(cmd), dict(kw))


class _Base(unittest.TestCase):
    """给出一条「永远成功」的假 hdc，子类按需覆盖 handler。

    `time.sleep` 打桩成记账（重试退避不真等）；**不假造 `time.time()`** ——
    造一个跳变的时钟会让 `wait_device` 的 deadline 判据失真，
    「等到设备」这条正例就永远测不到。要测超时就把 timeout 设 0。
    """

    def setUp(self):
        import time as _real_time
        self.sub = _FakeSubprocess(lambda cmd, kw: _proc())
        self.calls = self.sub.calls
        self.slept = []
        self.clock = SimpleNamespace(time=_real_time.time,
                                     sleep=self.slept.append)
        for target, stub in (('ohauto.hdc.subprocess', self.sub),
                             ('ohauto.hdc.time', self.clock)):
            p = mock.patch(target, stub)
            p.start()
            self.addCleanup(p.stop)
        self.hdc = Hdc(hdc_path=FAKE_PATH)

    def _argv(self, i=0):
        return self.calls[i][0]

    def _cmdline(self, i=0):
        return ' '.join(self.calls[i][0])


class TestCommandShape(_Base):
    """命令怎么拼 —— 这是「零部署」得以成立的那一层。"""

    def test_base_cmd_without_target(self):
        self.assertEqual(self.hdc._base_cmd(), [FAKE_PATH])

    def test_base_cmd_with_target(self):
        self.hdc.target = 'SN-123'
        self.assertEqual(self.hdc._base_cmd(), [FAKE_PATH, '-t', 'SN-123'])

    def test_run_appends_args_and_keeps_argv_shape(self):
        self.hdc.run(['list', 'targets'])
        self.assertEqual(self._argv(), [FAKE_PATH, 'list', 'targets'])
        self.assertEqual(self.calls[0][1]['shell'], False)

    def test_shell_wraps_in_shell_subcommand(self):
        self.hdc.shell('pidof com.demo.app')
        self.assertEqual(self._argv(), [FAKE_PATH, 'shell', 'pidof com.demo.app'])

    def test_screen_cap_command_and_path(self):
        self.assertEqual(self.hdc.screen_cap(), f'{Hdc.DEVICE_TMP}/ohauto_shot.png')
        self.assertIn('uitest screenCap -p', self._cmdline())

    def test_dump_layout_flags(self):
        self.hdc.dump_layout(unfiltered=True, with_attrs=True)
        line = self._cmdline()
        self.assertIn('dumpLayout -i -a -p', line)

    def test_dump_layout_default_has_no_flags(self):
        self.hdc.dump_layout()
        self.assertIn('dumpLayout -p', self._cmdline())
        self.assertNotIn(' -i', self._cmdline())

    def test_ui_input_wrappers(self):
        self.hdc.click(1, 2)
        self.hdc.double_click(3, 4)
        self.hdc.long_click(5, 6)
        self.hdc.swipe(1, 2, 3, 4)
        self.hdc.fling(1, 2, 3, 4, velocity=900)
        self.hdc.drag(1, 2, 3, 4)
        got = [self._cmdline(i) for i in range(6)]
        for want, line in zip(
                ('uiInput click 1 2', 'uiInput doubleClick 3 4',
                 'uiInput longClick 5 6', 'uiInput swipe 1 2 3 4 600',
                 'uiInput fling 1 2 3 4 900', 'uiInput drag 1 2 3 4 600'), got):
            self.assertIn(want, line)

    def test_key_event_and_shortcuts(self):
        self.hdc.back()
        self.hdc.home()
        self.assertIn('uiInput keyEvent Back', self._cmdline(0))
        self.assertIn('uiInput keyEvent Home', self._cmdline(1))

    def test_key_event_rejects_more_than_three(self):
        with self.assertRaises(ValueError):
            self.hdc.key_event('Home', 'Back', 'Power', 'KeyCode')

    def test_dirc_fling_rejects_unknown_direction(self):
        self.hdc.dirc_fling(3)
        self.assertIn('uiInput dircFling 3 600', self._cmdline())
        with self.assertRaises(ValueError):
            self.hdc.dirc_fling(9)

    def test_app_management_commands(self):
        self.hdc.start_ability('com.demo.app')
        self.hdc.force_stop('com.demo.app')
        self.assertIn('aa start -b com.demo.app -a EntryAbility', self._cmdline(0))
        self.assertIn('aa force-stop com.demo.app', self._cmdline(1))

    def test_is_running_reads_pidof(self):
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'12345\n')
        self.assertTrue(self.hdc.is_running('com.demo.app'))
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'\n')
        self.assertFalse(self.hdc.is_running('com.demo.app'))

    def test_install_and_uninstall(self):
        self.hdc.install('a.hap')
        self.hdc.uninstall('com.demo.app')
        self.assertEqual(self._argv(0), [FAKE_PATH, 'install', '-r', 'a.hap'])
        self.assertEqual(self._argv(1), [FAKE_PATH, 'uninstall', 'com.demo.app'])
        self.assertEqual(self.calls[0][1]['timeout'], self.hdc.timeout * 6)

    def test_version_strips_output(self):
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'Ver 3.1.0e\n')
        self.assertEqual(self.hdc.version(), 'Ver 3.1.0e')


class TestRunSemantics(_Base):
    """失败怎么报、重试怎么算 —— 对面是硬件，这层的错最难查。"""

    def test_success_returns_shell_result(self):
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'hello\n')
        res = self.hdc.run(['list'])
        self.assertIsInstance(res, ShellResult)
        self.assertTrue(res.ok)
        self.assertEqual(res.stdout, 'hello\n')

    def test_nonzero_without_check_returns_result(self):
        """check=False 时**不抛**：调用方按 rc 自己判断。"""
        self.sub._handler = lambda cmd, kw: _proc(rc=1, stderr=b'boom')
        res = self.hdc.run(['list'])
        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, 1)
        self.assertEqual(res.stderr, 'boom')

    def test_nonzero_with_check_raises_with_rc_in_message(self):
        self.sub._handler = lambda cmd, kw: _proc(rc=7, stderr=b'boom')
        with self.assertRaises(HdcError) as cm:
            self.hdc.run(['list'], check=True)
        self.assertIn('rc=7', str(cm.exception))
        self.assertIn('boom', str(cm.exception))

    def test_timeout_raises_hdc_error(self):
        def boom(cmd, kw):
            raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
        self.sub._handler = boom
        with self.assertRaises(HdcError) as cm:
            self.hdc.run(['list'], check=True)
        self.assertIn('超时', str(cm.exception))

    def test_retries_are_honoured_and_backoff_recorded(self):
        """check=False 也必须重试 —— 原实现在这里无条件 return，retries 形同虚设。"""
        calls = {'n': 0}

        def flaky(cmd, kw):
            calls['n'] += 1
            return _proc(rc=1, stderr=b'flaky')
        self.sub._handler = flaky
        res = self.hdc.run(['list'], retries=2)
        self.assertEqual(calls['n'], 3)
        self.assertFalse(res.ok)
        self.assertEqual(self.slept, [0.8, 1.6])       # 退避递增

    def test_retries_exhausted_with_check_raises(self):
        self.sub._handler = lambda cmd, kw: _proc(rc=1, stderr=b'always')
        with self.assertRaises(HdcError):
            self.hdc.run(['list'], check=True, retries=1)
        self.assertEqual(len(self.calls), 2)

    def test_binary_mode_returns_raw_bytes(self):
        payload = b'\x89PNG\r\n\x1a\n'
        self.sub._handler = lambda cmd, kw: _proc(stdout=payload)
        res = self.hdc.run(['shell', 'cat x'], binary=True)
        self.assertEqual(res.stdout, '<binary>')
        self.assertEqual(res.raw, payload)

    def test_raw_is_a_declared_field_with_an_empty_default(self):
        """`raw` 必须是声明字段：它存在与否不该取决于走了哪条分支。"""
        from dataclasses import fields
        self.assertIn('raw', {f.name for f in fields(ShellResult)})
        self.assertEqual(ShellResult(0, '', '', 'cmd').raw, b'')
        # 非二进制分支同样带上默认值，调用方无需 getattr 兜底
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'{}')
        self.assertEqual(self.hdc.run(['list']).raw, b'')

    def test_verbose_prints_the_command(self):
        self.hdc.verbose = True
        with mock.patch('builtins.print') as pr:
            self.hdc.run(['list'])
        self.assertIn('[hdc]', str(pr.call_args))

    def test_custom_timeout_is_forwarded(self):
        self.hdc.run(['list'], timeout=7)
        self.assertEqual(self.calls[0][1]['timeout'], 7)


class TestTransientChannelFailure(_Base):
    """通道未就绪（`[Fail][E000004]:The communication channel is being established`）是
    **环境状态**，不是命令缺陷 —— 重发就过。实测（本机 DAYU200）同一条命令隔两秒即成功，
    而且这段文本会出现在 **stdout**（`cat` 的输出）里，不只是 stderr。
    """

    _FAIL_STDOUT = (b'[Fail][E000004]:The communication channel is being established.\n'
                    b'Please wait for several seconds and try again.\n'
                    b'[Fail]ExecuteCommand need connect-key? please confirm a device')

    def _flaky_then_ok(self, fail_times=1):
        state = {'n': 0}

        def handler(cmd, kw):
            state['n'] += 1
            if state['n'] <= fail_times:
                return _proc(rc=1, stdout=self._FAIL_STDOUT)
            return _proc(stdout=b'{"attributes": {}}')
        return handler, state

    def test_transient_failure_in_stdout_is_recognised(self):
        from ohauto.hdc import ShellResult
        res = ShellResult(1, self._FAIL_STDOUT.decode(), '', 'cmd')
        self.assertTrue(Hdc.is_transient_failure(res))

    def test_plain_failure_is_not_transient(self):
        from ohauto.hdc import ShellResult
        self.assertFalse(Hdc.is_transient_failure(
            ShellResult(1, '', 'no such file', 'cmd')))

    def test_zero_rc_with_fail_text_is_still_retried(self):
        """★ 关键形状：hdc 在通道未就绪时**返回码是 0**，`[Fail][E000004]` 打在 stdout。

        只在 `not res.ok` 的分支里查瞬时标记会**整个漏掉**这种情况
        （实机第一次复现时就是这样漏过去的）。
        """
        state = {'n': 0}

        def handler(cmd, kw):
            state['n'] += 1
            if state['n'] == 1:
                return _proc(rc=0, stdout=self._FAIL_STDOUT)
            return _proc(rc=0, stdout=b'{"attributes": {}}')
        self.sub._handler = handler
        res = self.hdc.run(['shell', 'cat x'], check=True)
        self.assertTrue(res.ok)
        self.assertEqual(state['n'], 2)
        self.assertNotIn('[Fail]', res.stdout)

    def test_transient_can_be_turned_off_per_call(self):
        """要拿 stdout 当普通文本的场景（grep 日志里恰含这几个字）可关掉。"""
        self.sub._handler = lambda cmd, kw: _proc(rc=0, stdout=self._FAIL_STDOUT)
        res = self.hdc.run(['shell', 'cat log'], transient=False)
        self.assertIn('[Fail]', res.stdout)
        self.assertEqual(len(self.calls), 1)

    def test_retries_until_the_channel_comes_up(self):
        handler, state = self._flaky_then_ok(fail_times=2)
        self.sub._handler = handler
        res = self.hdc.run(['shell', 'cat x'], check=True)
        self.assertTrue(res.ok)
        self.assertEqual(state['n'], 3)                 # 两次未就绪 + 一次成功
        self.assertEqual(self.slept, [0.8, 1.6])

    def test_transient_budget_is_bounded(self):
        """永远未就绪时不能无限重试 —— 预算用完就按失败返回/抛出。"""
        self.sub._handler = lambda cmd, kw: _proc(rc=1, stdout=self._FAIL_STDOUT)
        res = self.hdc.run(['shell', 'cat x'])
        self.assertFalse(res.ok)
        self.assertEqual(len(self.calls), 1 + self.hdc.transient_retries)

    def test_transient_retries_apply_even_with_check(self):
        """`check=True` 时也要重试 —— 通道没建好时抛错毫无意义。"""
        handler, state = self._flaky_then_ok(fail_times=1)
        self.sub._handler = handler
        res = self.hdc.run(['list', 'targets'], check=True)
        self.assertTrue(res.ok)
        self.assertEqual(state['n'], 2)

    def test_plain_failure_still_honours_only_retries(self):
        """普通失败不被瞬时预算放大：retries=1 → 恰好 2 次调用。"""
        self.sub._handler = lambda cmd, kw: _proc(rc=1, stderr=b'boom')
        with self.assertRaises(HdcError):
            self.hdc.run(['list'], check=True, retries=1)
        self.assertEqual(len(self.calls), 2)

    def test_timeout_is_not_multiplied_by_the_transient_budget(self):
        """超时很贵，不能因为瞬时预算多等几轮。"""
        def boom(cmd, kw):
            raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
        self.sub._handler = boom
        with self.assertRaises(HdcError):
            self.hdc.run(['list'], check=True)
        self.assertEqual(len(self.calls), 1)


class TestDeviceListing(_Base):

    def test_empty_output_means_no_device(self):
        self.assertEqual(self.hdc.list_targets(), [])

    def test_explicit_empty_marker(self):
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'[Empty]\n')
        self.assertEqual(self.hdc.list_targets(), [])

    def test_parses_one_device_per_line(self):
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'SN-1\nSN-2\n')
        self.assertEqual(self.hdc.list_targets(), ['SN-1', 'SN-2'])

    def test_wait_device_pins_the_first_target(self):
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'SN-9\n')
        self.assertEqual(self.hdc.wait_device(timeout=1), 'SN-9')
        self.assertEqual(self.hdc.target, 'SN-9')

    def test_wait_device_times_out_with_actionable_message(self):
        with self.assertRaises(DeviceNotFound) as cm:
            self.hdc.wait_device(timeout=0)
        msg = str(cm.exception)
        for hint in ('USB', '开发者模式', '授权'):
            self.assertIn(hint, msg)

    def test_uitest_version_reads_stdout(self):
        self.sub._handler = lambda cmd, kw: _proc(stdout=b'uitest 5.0.1.2\n')
        self.assertEqual(self.hdc.uitest_version(), 'uitest 5.0.1.2')

    def test_uitest_version_is_none_when_the_command_fails(self):
        """★ 判据是**返回码**，不是"有没有输出"。

        设备上没有 uitest 时 rc≠0、stderr 里是一句错误文本。老实现
        `stdout or stderr` 会把那句错误文本当版本号返回，
        `examples/smoke_test.py` 的 `if uv:` 于是打印「[通过] no uitest」
        并宣称技术路线可用 —— 假绿比报错更难发现。
        """
        self.sub._handler = lambda cmd, kw: _proc(rc=1, stderr=b'no uitest')
        self.assertIsNone(self.hdc.uitest_version())

    def test_uitest_version_is_none_on_empty_success_output(self):
        self.sub._handler = lambda cmd, kw: _proc(rc=0, stdout=b'\n')
        self.assertIsNone(self.hdc.uitest_version())

    def test_uitest_version_is_none_when_hdc_itself_raises(self):
        def boom(cmd, kw):
            raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
        self.sub._handler = boom
        self.assertIsNone(self.hdc.uitest_version())

    def test_hilog_without_and_with_grep(self):
        self.sub._handler = lambda cmd, kw: _proc(
            stdout=b'line keep\nline drop\nkeep again\n')
        self.assertEqual(len(self.hdc.hilog().splitlines()), 3)
        filtered = self.hdc.hilog(grep='keep')
        self.assertEqual(filtered.splitlines(), ['line keep', 'keep again'])


class TestLocate(unittest.TestCase):
    """查找顺序：环境变量 → 配置文件 → PATH → 常见安装位置。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ('HDC_PATH', 'USERNAME'):
            p = mock.patch.dict(os.environ, {name: ''}, clear=False)
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop('HDC_PATH', None)

    def _real_file(self, name='hdc.exe'):
        p = os.path.join(self.tmp.name, name)
        with open(p, 'wb') as f:
            f.write(b'MZ')
        return p

    def test_env_var_wins(self):
        p = self._real_file()
        os.environ['HDC_PATH'] = p
        self.assertEqual(Hdc._locate(), p)

    def test_env_var_pointing_nowhere_is_ignored(self):
        os.environ['HDC_PATH'] = os.path.join(self.tmp.name, 'nope.exe')
        with mock.patch('ohauto.hdc.Hdc._from_config', return_value=None), \
             mock.patch('ohauto.hdc.shutil.which', lambda *a: None), \
             mock.patch('glob.glob', lambda *a: []):
            self.assertIsNone(Hdc._locate())

    def test_config_file_is_used(self):
        p = self._real_file()
        cfg = os.path.join(self.tmp.name, 'hdc.config.json')
        with open(cfg, 'w', encoding='utf-8') as f:
            json.dump({'hdc_path': p}, f)
        with mock.patch('ohauto.hdc.os.getcwd', lambda: self.tmp.name):
            self.assertEqual(Hdc._from_config(), p)

    def test_config_accepts_short_key(self):
        p = self._real_file()
        with open(os.path.join(self.tmp.name, '.ohauto.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'hdc': p}, f)
        with mock.patch('ohauto.hdc.os.getcwd', lambda: self.tmp.name):
            self.assertEqual(Hdc._from_config(), p)

    def test_config_written_with_a_utf8_bom_is_still_read(self):
        """带 BOM 的配置必须能读 —— 读不了就会被静默跳过。

        PowerShell 5.1 的 `Set-Content -Encoding UTF8` 默认写 BOM，
        而 `utf-8` 解码会抛 JSONDecodeError；异常被 `_from_config` 吞掉后
        `_locate()` 会落到「常见安装位置」，**悄悄换成另一个 hdc 二进制**。
        """
        p = self._real_file()
        cfg = os.path.join(self.tmp.name, 'hdc.config.json')
        with open(cfg, 'w', encoding='utf-8-sig') as f:
            json.dump({'hdc_path': p}, f)
        with mock.patch('ohauto.hdc.os.getcwd', lambda: self.tmp.name):
            self.assertEqual(Hdc._from_config(), p)

    def test_config_with_broken_json_is_skipped(self):
        with open(os.path.join(self.tmp.name, 'hdc.config.json'), 'w',
                  encoding='utf-8') as f:
            f.write('{ not json')
        with mock.patch('ohauto.hdc.os.getcwd', lambda: self.tmp.name):
            self.assertIsNone(Hdc._from_config())

    def test_config_pointing_to_missing_file_is_skipped(self):
        with open(os.path.join(self.tmp.name, 'hdc.config.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'hdc_path': os.path.join(self.tmp.name, 'gone.exe')}, f)
        with mock.patch('ohauto.hdc.os.getcwd', lambda: self.tmp.name):
            self.assertIsNone(Hdc._from_config())

    def test_path_lookup_is_after_config(self):
        with mock.patch('ohauto.hdc.Hdc._from_config', return_value=None), \
             mock.patch('ohauto.hdc.shutil.which',
                        lambda name: '/usr/bin/hdc' if name == 'hdc' else None):
            self.assertEqual(Hdc._locate(), '/usr/bin/hdc')

    def test_common_paths_are_the_last_resort(self):
        p = self._real_file()
        with mock.patch('ohauto.hdc.Hdc._from_config', return_value=None), \
             mock.patch('ohauto.hdc.shutil.which', lambda *a: None), \
             mock.patch('glob.glob', lambda *a: [p]):
            self.assertEqual(Hdc._locate(), p)

    def test_missing_hdc_raises_with_five_solutions(self):
        with mock.patch('ohauto.hdc.Hdc._locate', return_value=None):
            with self.assertRaises(HdcError) as cm:
                Hdc()
        msg = str(cm.exception)
        self.assertIn('HDC_PATH', msg)
        self.assertIn('hdc.config.json', msg)


if __name__ == '__main__':
    unittest.main(verbosity=2)
