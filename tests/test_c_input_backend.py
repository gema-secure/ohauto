"""C8 输入注入通路（`InputBackend` 窄契约 + 探测降级）的离线钉子测试。

钉三类规则：

1. **契约**：三个 backend 都满足 `InputBackend`；删掉任一方法即不再满足
   （与 `tests/test_d_interface_contract.py` 同法）。
2. **探测降级**：uitest → uinput → sendevent 首个可用者胜出；三者皆无则
   落「不可交互」档，写操作**响亮失败**（不是静默空操作）。
3. **模拟同构**：`FakeHdc` 认 uinput / sendevent 命令，且换通路后动作语义
   与 uitest 逐字一致 —— 「模拟器不保真 = 测试全绿反而危险」的反面钉子。
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto.hdc import (Hdc, InputBackend, InputUnavailable,   # noqa: E402
                        _NullBackend, _parse_axis_ranges,
                        _SendeventBackend, _UitestBackend, _UinputBackend)
from ohauto.sim import FakeHdc                                 # noqa: E402

FAKE_PATH = 'fake-hdc'

#: 一台**有 `getevent`** 的设备（与 DAYU200 相反），用于验证 sendevent 正例。
GETEVENT = (
    'add device 5: /dev/input/event5\n'
    '  name: "VSoC touchscreen"\n'
    '  ABS_MT_POSITION_X     : min 0, max 719\n'
    '  ABS_MT_POSITION_Y     : min 0, max 1279\n'
)


def _proc(rc=0, stdout=b'', stderr=b''):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _router(pairs, default=None):
    """按「命令里含某关键词」路由假 hdc 的返回，便于逐条摆布探测结果。"""
    def handler(cmd, kw):
        joined = ' '.join(str(c) for c in cmd)
        for key, val in pairs:
            if key in joined:
                return val
        return default if default is not None else _proc()
    return handler


class _FakeSubprocess:
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def run(self, cmd, **kw):
        self.calls.append((list(cmd), dict(kw)))
        res = self._handler(list(cmd), dict(kw))
        # doctor 用 `subprocess.run(..., text=True)`，而 `Hdc.run` 自己解码 ——
        # 这里按 `text` 标志**返回一个副本**（不改原对象，两者共用同一组桩
        # 返回时不会被前一次调用污染），两种调用方都能喂。
        if kw.get('text'):
            def _t(v):
                return v.decode('utf-8', 'replace') if isinstance(v, bytes) else v
            return SimpleNamespace(returncode=res.returncode,
                                   stdout=_t(res.stdout), stderr=_t(res.stderr))
        return res


class _HdcBase(unittest.TestCase):
    """给一条「永远成功」的假 hdc + 假时钟，逐条摆布探测返回。"""

    def setUp(self):
        import time as _real_time
        self.sub = _FakeSubprocess(lambda cmd, kw: _proc())
        self.calls = self.sub.calls
        for target, stub in (
                ('ohauto.hdc.subprocess', self.sub),
                ('ohauto.hdc.time', SimpleNamespace(
                    time=_real_time.time, sleep=lambda *_: None))):
            p = mock.patch(target, stub)
            p.start()
            self.addCleanup(p.stop)
        self.hdc = Hdc(hdc_path=FAKE_PATH)

    def _cmdline(self, i=-1):
        return ' '.join(self.calls[i][0])


class TestInputBackendContract(unittest.TestCase):
    """契约钉子：删方法即不再满足协议。"""

    def _instances(self):
        return {
            'uitest': _UitestBackend(object()),
            'uinput': _UinputBackend(object()),
            'sendevent': _SendeventBackend(object(), '/dev/input/event5',
                                           {'x': (0, 719), 'y': (0, 1279)}),
            'none': _NullBackend('测试'),
        }

    def test_every_backend_satisfies_the_protocol(self):
        for name, b in self._instances().items():
            with self.subTest(name):
                self.assertIsInstance(b, InputBackend)
                self.assertEqual(b.name, name)

    def test_protocol_rejects_an_incomplete_backend(self):
        class _Half:
            name = 'half'

            def click(self, x, y):
                pass
        self.assertNotIsInstance(_Half(), InputBackend)

    def test_null_backend_fails_loudly_on_every_write(self):
        b = _NullBackend('没有可用通路')
        for call in (lambda: b.click(1, 2), lambda: b.double_click(1, 2),
                     lambda: b.long_click(1, 2), lambda: b.swipe(1, 2, 3, 4),
                     lambda: b.input_text(1, 2, 'x'),
                     lambda: b.key_event('Back')):
            with self.subTest(call):
                with self.assertRaises(InputUnavailable):
                    call()

    def test_sendevent_text_input_is_refused_not_skipped(self):
        b = _SendeventBackend(object(), '/dev/input/event5',
                              {'x': (0, 719), 'y': (0, 1279)})
        with self.assertRaises(InputUnavailable):
            b.input_text(1, 2, '中文')


class TestAxisRangeParsing(unittest.TestCase):
    """sendevent 的坐标口径：轴范围**从设备读**，不硬编码。"""

    def test_parses_node_and_ranges(self):
        node, ranges = _parse_axis_ranges(GETEVENT)
        self.assertEqual(node, '/dev/input/event5')
        self.assertEqual(ranges['x'], (0, 719))
        self.assertEqual(ranges['y'], (0, 1279))

    def test_returns_none_when_unparseable(self):
        self.assertIsNone(_parse_axis_ranges(''))
        self.assertIsNone(_parse_axis_ranges('getevent: not found'))


class TestDetectAndDispatch(_HdcBase):
    """探测顺序 + 降级 + 派发。"""

    def test_default_is_uitest_and_dispatch_unchanged(self):
        self.assertEqual(self.hdc.backend_name, 'uitest')
        self.assertTrue(self.hdc.interactive)
        self.hdc.click(1, 2)
        self.assertIn('uiInput click 1 2', self._cmdline())

    def test_selects_uinput_when_uitest_missing(self):
        self.sub._handler = _router([
            ('uitest --version', _proc(rc=1, stderr=b'uitest: not found')),
            ('uinput --help', _proc(stdout=b'usage: uinput [-T|-K|-M]')),
            ('getevent', _proc()),
        ])
        rep = self.hdc.detect_backend()
        self.assertEqual(rep['selected'], 'uinput')
        self.assertTrue(rep['interactive'])
        self.hdc.click(3, 4)
        self.assertIn('uinput -T -c 3 4', self._cmdline())

    def test_selects_sendevent_when_it_is_the_only_option(self):
        self.sub._handler = _router([
            ('uitest --version', _proc(rc=1, stderr=b'not found')),
            ('uinput --help', _proc(rc=1, stderr=b'uinput: not found')),
            ('getevent', _proc(stdout=GETEVENT.encode())),
        ], default=_proc(stdout=b'activeMode: 720x1280'))
        rep = self.hdc.detect_backend()
        self.assertEqual(rep['selected'], 'sendevent')
        self.hdc.click(10, 20)
        self.assertIn('sendevent /dev/input/event5', self._cmdline())

    def test_falls_to_none_when_nothing_works(self):
        self.sub._handler = _router([
            ('uitest --version', _proc(rc=1, stderr=b'not found')),
            ('uinput --help', _proc(rc=1, stderr=b'no uinput')),
            ('getevent', _proc()),
        ])
        rep = self.hdc.detect_backend()
        self.assertEqual(rep['selected'], 'none')
        self.assertFalse(rep['interactive'])
        self.assertFalse(self.hdc.interactive)
        with self.assertRaises(InputUnavailable):
            self.hdc.click(1, 2)

    def test_fling_is_refused_off_uitest(self):
        self.hdc.detect_backend(force='uinput')
        self.sub._handler = _router([('uinput --help', _proc(stdout=b'usage'))])
        with self.assertRaises(InputUnavailable):
            self.hdc.fling(1, 2, 3, 4)

    def test_force_rejects_unknown_name(self):
        with self.assertRaises(ValueError):
            self.hdc.detect_backend(force='magic')

    def test_probe_report_lists_all_three_backends(self):
        self.sub._handler = _router([
            ('uitest --version', _proc(stdout=b'5.0.1.2')),
            ('uinput --help', _proc(stdout=b'usage')),
            ('getevent', _proc()),
        ])
        rep = self.hdc.detect_backend()
        self.assertEqual(rep['selected'], 'uitest')
        self.assertEqual([p['name'] for p in rep['probes']],
                         ['uitest', 'uinput', 'sendevent'])


class TestFakeHdcAltPaths(unittest.TestCase):
    """模拟侧同构：认备用通路命令，且换通路不改动作语义。"""

    def test_direct_uinput_command_is_interpreted(self):
        f = FakeHdc()
        f.shell('uinput -T -c 100 200')
        self.assertEqual(f.actions[-1]['action'], 'click')
        f.shell('uinput -T -m 1 2 3 4')
        self.assertEqual(f.actions[-1]['action'], 'swipe')
        f.shell('uinput -T -m 5 6 5 6 -k 1200')
        self.assertEqual(f.actions[-1]['action'], 'longClick')
        f.shell('uinput -K -l 2 3000')
        self.assertEqual(f.actions[-1]['key'], 'Back')

    def test_uinput_down_up_alone_is_a_documented_noop(self):
        """`-d` / `-u` 分两次下发真机上不生效 —— 模拟侧也不假装成功。"""
        f = FakeHdc()
        f.shell('uinput -K -d 2 -u 2')
        self.assertEqual(f.actions, [])

    def test_backend_switch_keeps_action_semantics(self):
        uit = FakeHdc()
        uit.click(100, 200)
        uit.swipe(10, 20, 30, 40)
        uit.back()

        alt = FakeHdc()
        alt.detect_backend(force='uinput')
        self.assertEqual(alt.backend_name, 'uinput')
        alt.click(100, 200)
        alt.swipe(10, 20, 30, 40)
        alt.back()
        self.assertEqual([a['action'] for a in alt.actions],
                         [a['action'] for a in uit.actions])

    def test_detect_backend_mirrors_the_real_device(self):
        f = FakeHdc()
        rep = f.detect_backend()
        self.assertEqual(rep['selected'], 'uitest')
        self.assertTrue(rep['interactive'])
        self.assertEqual({p['name']: p['ok'] for p in rep['probes']},
                         {'uitest': True, 'uinput': True, 'sendevent': False})

    def test_forced_none_refuses_writes(self):
        f = FakeHdc()
        f.detect_backend(force='sendevent')
        self.assertEqual(f.backend_name, 'none')
        self.assertFalse(f.interactive)
        with self.assertRaises(InputUnavailable):
            f.click(1, 2)


class TestDoctorReportsBackupPaths(unittest.TestCase):
    """AC1：uitest 不可用时不再直接终止自检，而是逐条列出备用通路。"""

    def _run_doctor(self, router):
        import ohauto.doctor as doc
        sub = _FakeSubprocess(router)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch('ohauto.doctor.subprocess', sub))
            stack.enter_context(mock.patch('ohauto.hdc.subprocess', sub))
            stack.enter_context(mock.patch('ohauto.doctor.shutil.which',
                                           lambda *_: FAKE_PATH))
            stack.enter_context(mock.patch.object(doc, 'results', []))
            stack.enter_context(mock.patch.object(sys, 'argv', ['doctor']))
            buf = io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(buf))
            rc = doc.main()
        return rc, buf.getvalue()

    def test_lists_backup_backend_when_uitest_missing(self):
        rc, out = self._run_doctor(_router([
            ('uitest --version', _proc(rc=1, stderr=b'uitest: not found')),
            ('uinput --help', _proc(stdout=b'usage: uinput')),
            ('getevent', _proc()),
        ], default=_proc(stdout=b'Vi 1.0\n')))
        self.assertIn('输入注入通路（备用）', out)
        self.assertIn('uinput', out)
        self.assertEqual(rc, 1)          # uitest 缺失仍是失败项，但不再终止

    def test_marks_not_interactive_when_nothing_works(self):
        rc, out = self._run_doctor(_router([
            ('uitest --version', _proc(rc=1, stderr=b'uitest: not found')),
            ('uinput --help', _proc(rc=1, stderr=b'no uinput')),
            ('getevent', _proc()),
        ], default=_proc(stdout=b'Vi 1.0\n')))
        self.assertIn('不可交互', out)
        self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()