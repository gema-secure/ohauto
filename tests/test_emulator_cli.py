"""模拟器命令行封装 —— 单元测试。

**全部测试都不需要真的启动模拟器、不需要镜像、不需要装 DevEco。**

分两类：
1. **纯逻辑测试** —— 折叠态取值表、参数拼装、JSON 抠取（永远可跑）
2. **环境探测测试** —— 装了 DevEco 就验证真实路径，没装就 skip

设计原则：把「能不能调 Emulator.exe」和「调的时候参数对不对」分开测。
前者依赖环境，后者是我们自己的代码 —— 后者必须永远可验证。
"""
import json
import os
import subprocess
import sys
import unittest

#: Windows 的「完全脱离控制台」标志 —— **启动模拟器时绝不能用**
#: （实测：带了它，Emulator.exe 会无报错静默退出，日志只留一行加速器提示）
DETACHED_PROCESS = 0x00000008

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import emulator_cli as ec                                      # noqa: E402


# ============================================================ 纯逻辑

class TestFindEmulator(unittest.TestCase):
    def test_explicit_missing_returns_none(self):
        self.assertIsNone(ec.find_emulator(r'C:\no\such\Emulator.exe'))

    def test_explicit_existing_returned(self):
        # 用本文件自己当「存在的文件」
        me = os.path.abspath(__file__)
        self.assertEqual(ec.find_emulator(me), me)

    def test_none_when_not_installed(self):
        # 不传参数时可能找到也可能找不到，取决于机器 —— 只验证类型
        r = ec.find_emulator()
        self.assertTrue(r is None or isinstance(r, str))


class TestJsonExtraction(unittest.TestCase):
    """从混合输出里抠 JSON —— 模拟器的输出常夹着提示行。"""

    def test_array_with_prefix_noise(self):
        text = 'some log line\n[\n {"a": 1}\n]\ntrailing'
        self.assertEqual(ec._json_from(text), [{'a': 1}])

    def test_object(self):
        self.assertEqual(ec._json_from('x {"b": 2} y'), {'b': 2})

    def test_garbage_returns_none(self):
        for bad in ('', 'no json here', '[broken'):
            with self.subTest(bad=bad):
                self.assertIsNone(ec._json_from(bad))

    def test_nested_array_is_parsed(self):
        payload = [{'deviceType': 'foldable', 'downloaded': 'false'},
                   {'deviceType': 'phone', 'downloaded': 'true'}]
        text = 'warn\n' + json.dumps(payload) + '\n'
        self.assertEqual(ec._json_from(text), payload)


class TestFoldStates(unittest.TestCase):
    """★ 折叠态取值表 —— 实测自 Emulator.exe -help，是硬事实不是猜的。"""

    def test_foldable_states(self):
        self.assertEqual(ec.FOLD_STATES['foldable'],
                         ('open', 'half-open', 'close'))

    def test_wide_fold_has_no_half_open(self):
        # 实测：WideFold 只有 open/close（Pura X Max 除外）
        self.assertEqual(ec.FOLD_STATES['wide_fold'], ('open', 'close'))

    def test_pc_foldable_has_vertical_open(self):
        self.assertIn('vertical-open', ec.FOLD_STATES['pc_foldable'])

    def test_triple_fold_has_nine_states(self):
        """★ 三折叠不是 3 种形态，是 9 种。

        single / double / triple 三个主态，外加 6 种左右半折组合。
        这是 本项可以做深的一个差异维度 —— 原以为只有 3 种。
        """
        st = ec.FOLD_STATES['triple_fold']
        self.assertEqual(len(st), 9)
        self.assertEqual(st[:3], ('single', 'double', 'triple'))
        for combo in ('left-folded-right-half-folded',
                      'left-half-folded-right-expanded',
                      'left-expanded-right-folded',
                      'left-half-folded-right-folded',
                      'left-expanded-right-half-folded',
                      'left-half-folded-right-half-folded'):
            with self.subTest(combo=combo):
                self.assertIn(combo, st)

    def test_all_states_are_lowercase_kebab(self):
        for kind, states in ec.FOLD_STATES.items():
            for s in states:
                with self.subTest(kind=kind, state=s):
                    self.assertEqual(s, s.lower())
                    self.assertNotIn(' ', s)


class TestArgvBuilding(unittest.TestCase):
    """参数拼装 —— 用假 exe 路径拦截，不真的执行。

    `run()` 在 exe 不存在时直接返回 rc=-1 且不带 argv，
    所以这里改用 `subprocess.run` 打桩来捕获真实 argv。
    """

    def setUp(self):
        self._orig = ec.subprocess.run
        self._targets = ec.list_targets
        self._popen = ec.subprocess.Popen
        self.captured = []
        self.stdin_seen = []

        class FakeProc:
            returncode = 0
            stdout = b'{}'
            stderr = b''

        def fake_run(argv, **kw):
            self.captured.append(list(argv))
            self.stdin_seen.append(kw.get('input'))
            return FakeProc()

        ec.subprocess.run = fake_run
        self._exists = ec.os.path.exists
        ec.os.path.exists = lambda p: True          # 假装 exe 存在
        # ★ `start()` 会先探测「设备是不是已经在跑」。不打桩的话它会走
        # `list_targets()` → 被上面的 fake_run 拦到 → 误判「设备在线」→
        # 提前返回，argv 里就只剩 `hdc list targets`（踩过）。
        ec.list_targets = lambda *a, **kw: []
        # start() 现在用 Popen 拉起，没有 Popen 桩就会真的启动模拟器。
        # 同时把 argv 也记进 captured —— 这样「参数拼装」测试依旧有效，
        # 因为它关心的就是**最终拼出来的命令行**。
        class FakePopen:
            pid = 4242

            def __init__(self, argv, **kw):
                self.argv = list(argv)

        def fake_popen(argv, **kw):
            self.captured.append(list(argv))
            return FakePopen(argv, **kw)

        ec.subprocess.Popen = fake_popen

    def tearDown(self):
        ec.subprocess.run = self._orig
        ec.os.path.exists = self._exists
        ec.list_targets = self._targets
        ec.subprocess.Popen = self._popen

    def _argv(self, **kw):
        self.captured.clear()
        ec.run(['-version'], **kw)
        return self.captured[0][1:]                 # 去掉 exe 本身

    def test_create_minimal(self):
        ec.create_instance('T1', 'tablet', exe='X')
        argv = self.captured[-1]
        self.assertIn('-create', argv)
        self.assertIn('T1', argv)
        self.assertIn('-deviceType', argv)
        self.assertIn('tablet', argv)

    def test_create_with_custom_screen(self):
        """★ 自定义屏幕 —— 造「配置里不存在的形态」的关键参数。"""
        ec.create_instance('T2', 'phone', screen='2200 2480 480 7.8', exe='X')
        argv = self.captured[-1]
        i = argv.index('-screen')
        self.assertEqual(argv[i + 1], '2200 2480 480 7.8')

    def test_create_with_screen_profile(self):
        ec.create_instance('T3', 'foldable', screen_profile='Mate X7', exe='X')
        argv = self.captured[-1]
        i = argv.index('-screenProfile')
        self.assertEqual(argv[i + 1], 'Mate X7')

    def test_create_optional_args_omitted_when_none(self):
        ec.create_instance('T4', 'phone', exe='X')
        argv = self.captured[-1]
        for flag in ('-screen', '-screenProfile', '-memory', '-storage'):
            self.assertNotIn(flag, argv)

    def test_start_no_window(self):
        """只测参数拼装 —— 必须跳过自检门，且不等设备。

        自检门会调 `list_instances` / `preflight`，它们内部走真实
        `subprocess.run`，会把噪声行 append 进 `self.captured`，
        导致这里取到的 `[-1]` 不是 `-start` 那条。
        参数拼装与自检门是两件事，分开测（自检门见 TestStartPreflightGate）。

        ⚠️ `wait=False` 也是必须的 —— 本类的 `list_targets` 被打桩成
        「无设备」，不跳等待就会**真轮询 240 秒**（踩过）。
        """
        ec.start('X1', no_window=True, hdc_port=10086, exe='X',
                 skip_check=True, wait=False)
        argv = self.captured[-1]
        self.assertIn('-start', argv)
        self.assertIn('-noWindow', argv)
        i = argv.index('-hdcPort')
        self.assertEqual(argv[i + 1], '10086')

    def test_flags_use_single_leading_dash(self):
        """★ 回归护栏：每个开关必须是「单个 `-` + 小驼峰」。

        踩过的坑：把 `--noWindow`（双横线）或 `NONWINDOW`（全大写无横线）
        塞进 argv，模拟器会静默忽略而不是报错。
        """
        ec.start('X1', no_window=True, hdc_port=10086, exe='X',
                 skip_check=True, wait=False)
        argv = self.captured[-1]
        flags = [t for t in argv if t.startswith('-')]
        self.assertTrue(flags)
        for tok in flags:
            with self.subTest(tok=tok):
                self.assertFalse(tok.startswith('--'),
                                 f'开关 {tok} 用了双横线')
                # 去掉前导横线后首字母必须小写
                body = tok.lstrip('-')
                self.assertEqual(body[:1], body[:1].lower(),
                                 f'开关 {tok} 首字母不是小写')

    def test_folded_state(self):
        ec.set_folded_state('Mate X7', 'half-open', exe='X')
        argv = self.captured[-1]
        # argv[0] 是 exe 本身，从 [1] 开始才是参数
        self.assertEqual(argv[1:4], ['-instance', 'Mate X7', '-foldedState'])
        self.assertEqual(argv[4], 'half-open')

    def test_ui_layout_flags(self):
        ec.ui_layout('Mate X7', interactive=True, all_windows=True, exe='X')
        argv = self.captured[-1]
        self.assertIn('-uiLayout', argv)
        self.assertIn('-i', argv)
        self.assertIn('-a', argv)

    def test_screenshot_default_has_no_path(self):
        ec.screenshot('Mate X7', exe='X')
        self.assertNotIn('-screenshotPath', self.captured[-1])

    def test_screenshot_with_path(self):
        ec.screenshot('Mate X7', path='D:/out/a.png', exe='X')
        argv = self.captured[-1]
        self.assertEqual(argv[argv.index('-screenshotPath') + 1], 'D:/out/a.png')

    def test_delete_force(self):
        ec.delete_instance('T5', force=True, exe='X')
        self.assertIn('-force', self.captured[-1])

    def test_install_image(self):
        ec.install_image('phone', 'HarmonyOS 7.0.0(26.0.0)', exe='X')
        argv = self.captured[-1]
        self.assertIn('-install', argv)
        self.assertIn('phone', argv)

    def test_install_feeds_yes_to_stdin(self):
        """★ 回归护栏：`-install` 必须喂 stdin。

        实测：`-install` 会交互式追问「是否同意协议 (y/N)」。
        不喂输入就拿到 EOF 直接中止，**而且不报错、rc 还是 0** ——
        表现为「命令瞬间返回、镜像没下」，极难排查。
        """
        ec.install_image('phone', 'HarmonyOS 7.0.0(26.0.0)', exe='X')
        # subprocess 的 input 是 bytes
        self.assertEqual(self.stdin_seen, [b'y\n'],
                         f'install 没有喂 stdin，实际喂了 {self.stdin_seen!r}')

    def test_non_install_commands_do_not_feed_stdin(self):
        """只有 install 需要应答 —— 别的命令不该乱喂输入。"""
        self.captured.clear()
        self.stdin_seen.clear()
        ec.list_images(exe='X')
        self.assertEqual(self.stdin_seen, [None])

    def test_install_image_auto_picks_smallest_by_default(self):
        """★ 默认挑版本号最小的 —— 小镜像在内存紧张的机器上更容易跑起来。"""
        orig = ec.list_images
        ec.list_images = lambda *a, **kw: [
            {'deviceType': 'phone', 'osVersion': 'HarmonyOS 7.0.0(26.0.0)'},
            {'deviceType': 'phone', 'osVersion': 'HarmonyOS 5.0.1(13)'},
        ]
        try:
            ec.install_image('phone', exe='X')
        finally:
            ec.list_images = orig
        argv = self.captured[-1]
        self.assertIn('-osVersion', argv)
        ver = argv[argv.index('-osVersion') + 1]
        self.assertEqual(ver, 'HarmonyOS 5.0.1(13)',
                         f'默认策略应挑最小版，实际挑了 {ver}')

    def test_list_downloaded_only(self):
        ec.list_images(downloaded_only=True, exe='X')
        argv = self.captured[-1]
        self.assertIn('-downloaded', argv)
        self.assertIn('true', argv)


class TestPickImageVersion(unittest.TestCase):
    """版本挑选策略 —— 纯逻辑，用假 imageList 喂进去。"""

    FAKE = [
        {'deviceType': 'phone', 'osVersion': 'HarmonyOS 7.0.0(26.0.0)'},
        {'deviceType': 'phone', 'osVersion': 'HarmonyOS 6.1.1(24)'},
        {'deviceType': 'phone', 'osVersion': 'HarmonyOS 10.0.0(99)'},
        {'deviceType': 'phone', 'osVersion': 'HarmonyOS 5.0.1(13)'},
    ]

    def setUp(self):
        self._orig = ec.list_images
        ec.list_images = lambda *a, **kw: list(self.FAKE)

    def tearDown(self):
        ec.list_images = self._orig

    def test_latest_returns_first(self):
        self.assertEqual(ec.pick_image_version('phone', 'latest'),
                         'HarmonyOS 7.0.0(26.0.0)')

    def test_smallest_picks_lowest_version(self):
        """★ 按数值比较，不是按字符串 —— 否则 '10.0.0' 会输给 '7.0.0'。"""
        self.assertEqual(ec.pick_image_version('phone', 'smallest'),
                         'HarmonyOS 5.0.1(13)')

    def test_empty_list_returns_none(self):
        ec.list_images = lambda *a, **kw: []
        self.assertIsNone(ec.pick_image_version('phone'))


class TestPreflight(unittest.TestCase):
    """启动前自检 —— 把「含糊的启动失败」提前变成「说得清的问题」。"""

    def setUp(self):
        self._lic = ec.license_status
        self._imgs = ec.list_images

    def tearDown(self):
        ec.license_status = self._lic
        ec.list_images = self._imgs

    def test_no_problems_when_all_ready(self):
        ec.license_status = lambda: {'A': True, 'B': True}
        ec.list_images = lambda *a, **kw: [
            {'deviceType': 'foldable', 'downloaded': 'true'}]
        self.assertEqual(ec.preflight('foldable'), [])

    def test_flags_unaccepted_license(self):
        ec.license_status = lambda: {'A': False, 'B': True}
        ec.list_images = lambda *a, **kw: [
            {'deviceType': 'foldable', 'downloaded': 'true'}]
        probs = ec.preflight('foldable')
        self.assertTrue(any('协议' in p for p in probs))

    def test_flags_no_images_at_all(self):
        ec.license_status = lambda: {'A': True}
        ec.list_images = lambda *a, **kw: [
            {'deviceType': 'foldable', 'downloaded': 'false'}]
        probs = ec.preflight('foldable')
        self.assertTrue(any('镜像' in p for p in probs))

    def test_flags_wrong_device_type_image(self):
        """下了 tablet 镜像但要起 foldable —— 报错要说清已下载的是哪些。"""
        ec.license_status = lambda: {'A': True}
        ec.list_images = lambda *a, **kw: [
            {'deviceType': 'tablet', 'downloaded': 'true'}]
        probs = ec.preflight('foldable')
        self.assertTrue(any('foldable' in p and 'tablet' in p for p in probs))

    def test_missing_license_file_does_not_block(self):
        # 读不到协议文件时不该误报（可能不是 Windows 或路径变了）
        ec.license_status = lambda: {}
        ec.list_images = lambda *a, **kw: [
            {'deviceType': 'phone', 'downloaded': 'true'}]
        self.assertEqual(ec.preflight('phone'), [])


class TestAvailableMemory(unittest.TestCase):
    def test_returns_float_or_none(self):
        v = ec.available_memory_gb()
        self.assertTrue(v is None or (isinstance(v, float) and v > 0))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows 专属')
    def test_reads_plausible_value(self):
        v = ec.available_memory_gb()
        self.assertIsNotNone(v)
        # 0.1 GB ~ 1 TB 之间都算合理
        self.assertGreater(v, 0.1)
        self.assertLess(v, 1024)


class TestStartPreflightGate(unittest.TestCase):
    """★ `start` 必须在协议/镜像/内存不满足时拒绝启动，而不是含糊失败。"""

    def setUp(self):
        self._pre = ec.preflight
        self._cnt = ec.list_instances
        self._mem = ec.available_memory_gb
        self._targets = ec.list_targets
        self._popen = subprocess.Popen
        self._emu = ec.find_emulator
        self.called = []
        # start() 现在走 Popen（非阻塞）而不是 run()，所以拦 Popen
        ec.find_emulator = lambda *a, **kw: 'EMU'
        ec.list_targets = lambda *a, **kw: []      # 默认：没有设备在跑
        subprocess.Popen = self._fake_popen(rc=0)

    def _fake_popen(self, rc=0):
        class _P:
            def __init__(self, argv, **kw):
                self.argv = argv
                self.pid = 12345
                self.returncode = rc
        def factory(argv, **kw):
            self.called.append(argv)
            return _P(argv, **kw)
        return factory

    def tearDown(self):
        ec.preflight = self._pre
        ec.list_instances = self._cnt
        ec.available_memory_gb = self._mem
        ec.list_targets = self._targets
        ec.find_emulator = self._emu
        subprocess.Popen = self._popen

    def test_refuses_when_preflight_fails(self):
        ec.preflight = lambda *a, **kw: ['协议未接受']
        ec.list_instances = lambda *a, **kw: []
        r = ec.start('X')
        self.assertEqual(r['rc'], -4)
        self.assertEqual(self.called, [], '自检没过竟然还是启动了！')

    def test_refuses_when_memory_low(self):
        ec.preflight = lambda *a, **kw: []
        ec.list_instances = lambda *a, **kw: [
            {'instancePath': 'C:/x/X', 'hw.ramSize': '4096'}]
        ec.available_memory_gb = lambda: 2.0        # 只有 2 GB，不够 4 GB
        r = ec.start('X')
        self.assertEqual(r['rc'], -5)
        self.assertIn('内存', r['stderr'])
        self.assertEqual(self.called, [], '内存不够竟然还是启动了！')

    def test_starts_when_everything_ok(self):
        ec.preflight = lambda *a, **kw: []
        ec.list_instances = lambda *a, **kw: [
            {'instancePath': 'C:/x/X', 'hw.ramSize': '4096'}]
        ec.available_memory_gb = lambda: 8.0
        r = ec.start('X', wait=False)          # wait=False 免得真去轮询
        self.assertEqual(r['rc'], 0)
        self.assertEqual(len(self.called), 1)
        self.assertIn('-start', self.called[0])

    def test_skip_check_bypasses_gate(self):
        ec.preflight = lambda *a, **kw: ['协议未接受']
        ec.list_instances = lambda *a, **kw: []
        r = ec.start('X', skip_check=True, wait=False)
        self.assertEqual(r['rc'], 0)
        self.assertEqual(len(self.called), 1)

    def test_does_not_double_launch_when_device_already_up(self):
        """★ 已有设备在线时直接返回，不重复拉起（否则会抢同一个 hdc 端口）。"""
        ec.preflight = lambda *a, **kw: []
        ec.list_instances = lambda *a, **kw: []
        ec.list_targets = lambda *a, **kw: ['127.0.0.1:5555']
        r = ec.start('X')
        self.assertEqual(r['rc'], 0)
        self.assertTrue(r.get('already_running'))
        self.assertEqual(self.called, [], '设备已在跑，不该再拉起一个！')


class TestStartIsNonBlocking(unittest.TestCase):
    """★★ `-start` 是前台常驻进程 —— 绝不能用 subprocess.run(timeout=) 等它。

    实测教训：用 `run(timeout=300)` 等它，会撑满 300s 后超时杀掉，
    **并连带把已经跑起来的模拟器一起带走**。所以 start 必须：
      ① 用 Popen 非阻塞拉起；
      ② 判据换成「hdc 能看见设备」，不是「进程退出码」。
    """

    def setUp(self):
        self._popen = subprocess.Popen
        self._emu = ec.find_emulator
        self._targets = ec.list_targets
        self._pre = ec.preflight
        self._cnt = ec.list_instances
        self._mem = ec.available_memory_gb
        ec.find_emulator = lambda *a, **kw: 'EMU'
        ec.preflight = lambda *a, **kw: []
        ec.list_instances = lambda *a, **kw: []
        self.popen_calls = []

    def tearDown(self):
        subprocess.Popen = self._popen
        ec.find_emulator = self._emu
        ec.list_targets = self._targets
        ec.preflight = self._pre
        ec.list_instances = self._cnt
        ec.available_memory_gb = self._mem

    def _install_popen(self):
        class _P:
            pid = 999
        def factory(argv, **kw):
            self.popen_calls.append(kw)
            return _P()
        subprocess.Popen = factory

    def test_uses_popen_without_detached_process(self):
        """★ Popen 可以，但**不能带 DETACHED_PROCESS** —— 实测它会静默退出。"""
        self._install_popen()
        # 必须先让「无设备在线」，否则 start 会走 already_running 分支直接返回
        ec.list_targets = lambda *a, **kw: []
        ec.start('X', wait=False)
        self.assertEqual(len(self.popen_calls), 1)
        kw = self.popen_calls[0]
        flags = kw.get('creationflags', 0)
        self.assertFalse(flags & DETACHED_PROCESS,
                         '带了 DETACHED_PROCESS —— 模拟器会无报错静默退出！')

    def test_reports_device_when_it_appears(self):
        """设备出现 = 启动成功，与进程退出码无关。"""
        self._install_popen()
        ec.list_targets = lambda *a, **kw: ['127.0.0.1:5555']
        r = ec.start('X', wait=True)
        self.assertEqual(r['rc'], 0)
        self.assertEqual(r.get('device'), '127.0.0.1:5555')

    def test_reports_failure_when_device_never_appears(self):
        self._install_popen()
        ec.list_targets = lambda *a, **kw: []
        r = ec.start('X', wait=True, wait_timeout=0)
        self.assertEqual(r['rc'], -6)
        self.assertIn('未在 hdc 里出现', r['stderr'])


class TestHdcHelpers(unittest.TestCase):
    """hdc 相关小工具 —— 不依赖真机，纯逻辑。"""

    def setUp(self):
        self._hdc = ec.find_hdc
        self._targets = ec.list_targets

    def tearDown(self):
        ec.find_hdc = self._hdc
        ec.list_targets = self._targets

    def test_active_screen_picks_powered_on(self):
        screens = [
            {'index': 0, 'power_status': 'POWER_STATUS_OFF', 'width': 2416},
            {'index': 1, 'power_status': 'POWER_STATUS_ON', 'width': 1080},
        ]
        self.assertEqual(ec.active_screen(screens)['index'], 1)

    def test_active_screen_none_when_all_off(self):
        screens = [{'index': 0, 'power_status': 'POWER_STATUS_OFF'}]
        self.assertIsNone(ec.active_screen(screens))

    def test_wait_for_device_returns_first_target(self):
        ec.list_targets = lambda *a, **kw: ['127.0.0.1:5555']
        self.assertEqual(ec.wait_for_device(timeout=1), '127.0.0.1:5555')

    def test_wait_for_device_times_out(self):
        ec.list_targets = lambda *a, **kw: []
        self.assertIsNone(ec.wait_for_device(timeout=0, interval=0))


class TestRunErrorHandling(unittest.TestCase):
    """`run` 不抛异常 —— 模拟器报错方式不统一，调用方要拿到 rc 自己判。"""

    def test_missing_exe_returns_rc_minus_1(self):
        r = ec.run(['-version'], exe=r'C:\no\such.exe')
        self.assertEqual(r['rc'], -1)
        self.assertIn('未找到', r['stderr'])

    def test_timeout_returns_rc_minus_2(self):
        import subprocess

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd='x', timeout=1)

        orig = ec.subprocess.run
        exists = ec.os.path.exists
        ec.subprocess.run = boom
        ec.os.path.exists = lambda p: True
        try:
            r = ec.run(['-version'], exe='X')
            self.assertEqual(r['rc'], -2)
            self.assertIn('超时', r['stderr'])
        finally:
            ec.subprocess.run = orig
            ec.os.path.exists = exists

    def test_generic_exception_returns_rc_minus_3(self):
        def boom(*a, **kw):
            raise ValueError('pfft')

        orig = ec.subprocess.run
        exists = ec.os.path.exists
        ec.subprocess.run = boom
        ec.os.path.exists = lambda p: True
        try:
            r = ec.run(['-version'], exe='X')
            self.assertEqual(r['rc'], -3)
            self.assertIn('ValueError', r['stderr'])
        finally:
            ec.subprocess.run = orig
            ec.os.path.exists = exists


class TestLicenseStatus(unittest.TestCase):
    """协议状态解析 —— 从配置文件读，不用交互式命令。"""

    def _with_config(self, content):
        """把 `license_status` 指向一份临时配置，返回调用结果。"""
        import tempfile
        with tempfile.NamedTemporaryFile(
                'w', suffix='.emu_config', delete=False,
                encoding='utf-8') as f:
            f.write(content)
            path = f.name
        orig = ec.os.path.expandvars
        ec.os.path.expandvars = lambda p: path
        try:
            return ec.license_status()
        finally:
            ec.os.path.expandvars = orig
            os.unlink(path)

    def test_parses_config_file(self):
        st = self._with_config(
            'HarmonyOS_Software_Service_Agreement:disagree\n'
            'HarmonyOS_SDK_Agreement:agree\n')
        self.assertFalse(st['HarmonyOS_Software_Service_Agreement'])
        self.assertTrue(st['HarmonyOS_SDK_Agreement'])

    def test_path_keys_are_not_treated_as_licenses(self):
        """★ 回归护栏：`imagePath` / `emuPath` 是路径，不是协议。

        真实 `.emu_config` 是混合文件：

            HarmonyOS_Software_Service_Agreement:agree
            HarmonyOS_SDK_Agreement:agree
            imagePath:C:\\Users\\...\\Sdk          ← 值不是 agree
            emuPath:C:\\Users\\...\\deployed       ← 值不是 agree

        早期实现把这两行也收进返回值，值不等于 'agree' 就被判成
        「未接受的协议」，于是 `status` 显示一个已全接受的机器
        「有未接受的协议 → 启动会失败」，`preflight` 也会误报拦住启动。
        """
        st = self._with_config(
            'HarmonyOS_Software_Service_Agreement:agree\n'
            'HarmonyOS_SDK_Agreement:agree\n'
            'imagePath:C:\\Users\\<user>\\AppData\\Local\\Huawei\\Sdk\n'
            'emuPath:C:\\Users\\<user>\\AppData\\Local\\Huawei\\Emulator\\deployed\n')
        self.assertNotIn('imagePath', st)
        self.assertNotIn('emuPath', st)
        self.assertEqual(sorted(st), ['HarmonyOS_SDK_Agreement',
                                      'HarmonyOS_Software_Service_Agreement'])
        self.assertTrue(all(st.values()), '全接受却被判成未接受')

    def test_value_with_colon_is_preserved(self):
        """值里含 `:`（Windows 路径）不能被 split 截断。"""
        st = self._with_config(
            'imagePath:C:\\a\\b\n'
            'HarmonyOS_SDK_Agreement:agree\n')
        self.assertTrue(st['HarmonyOS_SDK_Agreement'])

    def test_missing_file_returns_empty(self):
        orig = ec.os.path.expandvars
        ec.os.path.expandvars = lambda p: r'C:\no\such\.emu_config'
        try:
            self.assertEqual(ec.license_status(), {})
        finally:
            ec.os.path.expandvars = orig


class TestLicenseOk(unittest.TestCase):
    """`license_ok` —— 布尔化封装，读不到配置时不阻塞。"""

    def setUp(self):
        self._st = ec.license_status

    def tearDown(self):
        ec.license_status = self._st

    def test_all_accepted(self):
        ec.license_status = lambda: {'A': True, 'B': True}
        self.assertTrue(ec.license_ok())

    def test_one_pending(self):
        ec.license_status = lambda: {'A': True, 'B': False}
        self.assertFalse(ec.license_ok())

    def test_missing_config_does_not_block(self):
        """读不到文件不该拦住启动 —— 让 start 自己报错更准确。"""
        ec.license_status = lambda: {}
        self.assertTrue(ec.license_ok())

    def test_no_path_keys_leak_in(self):
        """路径键污染后，一台已全接受的机器不该被判为未接受。"""
        ec.license_status = lambda: {'A': True, 'B': True}
        self.assertTrue(ec.license_ok())


class TestAcceptGuard(unittest.TestCase):
    """★ `accept` 必须带 --yes 才执行 —— 这是法律协议，不能误触。"""

    def setUp(self):
        """把「真正执行命令」这条路径彻底堵死。

        只打桩 `run` 不够 —— `accept` 分支里还会调 `license_status()`
        读真实配置文件。两处都必须隔离，否则测试可能产生真实副作用。
        """
        self.called = []
        self._run = ec.run
        self._lic = ec.license_status
        ec.run = lambda *a, **kw: self.called.append(a) or {
            'rc': 0, 'stdout': '', 'stderr': '', 'argv': []}
        ec.license_status = lambda: {}

    def tearDown(self):
        ec.run = self._run
        ec.license_status = self._lic

    def test_without_yes_does_not_execute(self):
        # 抑制提示输出 —— 否则会把测试结果行淹掉，
        # 也会让 CI 里靠 tail 抓结果行的脚本误判
        import contextlib
        import io as _io
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ec.main(['accept'])
        self.assertEqual(rc, 2)
        self.assertEqual(self.called, [], 'accept 不带 --yes 竟然执行了命令！')
        # 提示里必须明确要 --yes
        self.assertIn('--yes', buf.getvalue())

    def test_with_yes_executes(self):
        import contextlib
        import io as _io
        with contextlib.redirect_stdout(_io.StringIO()):
            rc = ec.main(['accept', '--yes'])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.called), 1)
        self.assertIn('-license', self.called[0][0])
        self.assertIn('accept', self.called[0][0])


# ============================================================ 环境探测

@unittest.skipUnless(ec.find_emulator(), '本机未安装 DevEco 模拟器')
class TestRealEnvironment(unittest.TestCase):
    """装了 DevEco 才跑 —— 验证真实能调通。"""

    def test_version_parses(self):
        v = ec.version()
        self.assertIsNotNone(v)
        self.assertRegex(v, r'\d+\.\d+\.\d+')

    def test_list_instances_returns_list(self):
        insts = ec.list_instances()
        self.assertIsInstance(insts, list)

    def test_list_instances_details_structured(self):
        insts = ec.list_instances(details=True)
        self.assertIsInstance(insts, list)
        for i in insts:
            with self.subTest(inst=i.get('instancePath')):
                self.assertIn('deviceType', i)
                self.assertIn('instancePath', i)

    def test_list_images_returns_list(self):
        imgs = ec.list_images()
        self.assertIsInstance(imgs, list)
        if imgs:
            self.assertIn('deviceType', imgs[0])

    def test_help_mentions_folded_state(self):
        """`-help` 必须包含 foldedState —— 这是 跨形态 的核心依赖。

        如果哪天这条红了，说明模拟器版本升级后改了这个参数，
        跨形态 的折叠态切换会静默失效。
        """
        r = ec.run(['-help'], timeout=30)
        self.assertIn('-foldedState', r['stdout'] + r['stderr'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
