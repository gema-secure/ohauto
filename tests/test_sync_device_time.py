"""`tools/sync_device_time.py` 的单元测试。

**全部离线**，不需要设备 —— 用 MockHdc 注入预设的设备输出。

★ 本文件最要紧的一条测试是 `test_sync_must_write_rtc_with_u`：
   `hwclock` 漏了 `-u` 会让设备下次开机**快 8 小时**（东八区），
   而且这个错误**不会报错**，只会让后续所有时间戳悄悄失真。
   所以必须有一条测试把「命令里带 -u」钉死。
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from ohauto.hdc import ShellResult                          # noqa: E402
import sync_device_time as sdt                              # noqa: E402


def dev_stamp(dt: datetime.datetime) -> str:
    """把 datetime 格式化成设备 `date` 的输出形态。"""
    return dt.strftime('%a %b %d %H:%M:%S CST %Y')


class MockHdc:
    """只实现 `shell()` 的假 Hdc，记录收到的命令。"""

    def __init__(self, handler=None):
        self.calls = []
        self._handler = handler or (lambda c: ('', 0))

    def shell(self, cmd, **kw):
        self.calls.append(cmd)
        out, rc = self._handler(cmd)
        return ShellResult(returncode=rc, stdout=out, stderr='', command=cmd)

    def list_targets(self):
        return ['MOCK_DEVICE']

    def written(self):
        """返回所有**写**类命令。

        ⚠️ 注意别把 `date -u`（读）误判成写 —— 写命令的特征是
        `date "<时间>"`（带引号的参数）或 `hwclock -w`。
        """
        return [c for c in self.calls
                if c.startswith('date "') or c.startswith('hwclock -w')]


def make_hdc(local: datetime.datetime, utc: datetime.datetime,
             rtc: str, hwclock_rc: int = 0):
    """构造一个时间状态已知的假设备。

    `utc` 是设备 UTC（= 本地 - 时区偏移），`rtc` 是 `hwclock -r` 的原始读数。
    """
    def handler(cmd):
        if cmd == 'date':
            return (dev_stamp(local) + '\n', 0)
        if cmd == 'date -u':
            return (dev_stamp(utc) + '\n', 0)
        if cmd == 'hwclock -r':
            return (rtc + '\n', 0)
        if cmd.startswith('hwclock -w'):
            return ('', hwclock_rc)
        if cmd.startswith('date '):
            return ('', 0)
        return ('', 0)
    return MockHdc(handler)


# ------------------------------------------------------------ 解析

class TestParse(unittest.TestCase):

    def test_regex_parses_real_device_output(self):
        """真实 DAYU200 输出：`Sat Aug  5 17:01:03 CST 2017`（日号有双空格）。"""
        m = sdt._DATE_RE.search('Sat Aug  5 17:01:03 CST 2017')
        self.assertIsNotNone(m)
        _, mon, day, hh, mm, ss, tz, year = m.groups()
        self.assertEqual((mon, day, hh, mm, ss, tz, year),
                         ('Aug', '5', '17', '01', '03', 'CST', '2017'))

    def test_parses_real_date_output(self):
        dt = sdt._parse_date_like('Sat Aug  5 17:01:03 CST 2017')
        self.assertEqual(dt, datetime.datetime(2017, 8, 5, 17, 1, 3))

    def test_parses_real_hwclock_output(self):
        """★ 真实 `hwclock -r` 输出，格式与 `date` **不一样**：

            Sat Sep 19 06:38:29 2026  0.000000 seconds

        没有时区字段，年份紧跟秒后，末尾还有 drift 的小数。
        第一版正则只覆盖 `date` 的格式，在真机上把 drift 的 `0` 当年份
        （`ValueError: year 0 is out of range`）当场崩掉 ——
        这条测试就是补这个洞，防止再犯。
        """
        dt = sdt._parse_date_like('Sat Sep 19 06:38:29 2026  0.000000 seconds')
        self.assertIsNotNone(dt, '真实 hwclock -r 格式必须能解析')
        self.assertEqual(dt, datetime.datetime(2026, 9, 19, 6, 38, 29))

    def test_hwclock_output_with_dash_drift(self):
        """hwclock 另一种写法：年份后带 `- 0.5 seconds`。"""
        dt = sdt._parse_date_like('Sat Sep 19 06:38:29 2026 - 0.5 seconds')
        self.assertEqual(dt, datetime.datetime(2026, 9, 19, 6, 38, 29))

    def test_parse_returns_none_on_garbage(self):
        self.assertIsNone(sdt._parse_date_like('hwclock: command not found'))
        self.assertIsNone(sdt._parse_date_like(''))

    def test_parse_returns_none_on_impossible_date(self):
        """月份名不认识时不抛异常，返回 None。"""
        self.assertIsNone(sdt._parse_date_like('Sat Xxx 19 06:38:29 2026'))

    def test_read_device_local(self):
        dt = datetime.datetime(2026, 9, 19, 14, 40, 0)
        hdc = make_hdc(dt, dt - datetime.timedelta(hours=8),
                       dev_stamp(dt - datetime.timedelta(hours=8)))
        self.assertEqual(sdt._read_device_local(hdc), dt)

    def test_read_device_utc(self):
        dt = datetime.datetime(2026, 9, 19, 6, 40, 0)
        hdc = make_hdc(dt + datetime.timedelta(hours=8), dt, dev_stamp(dt))
        self.assertEqual(sdt._read_device_utc(hdc), dt)

    def test_unparsable_output_raises(self):
        hdc = MockHdc(lambda c: ('garbage output', 0))
        with self.assertRaises(sdt.HdcError):
            sdt._read_device_local(hdc)

    def test_nonzero_rc_raises(self):
        hdc = MockHdc(lambda c: ('', 1))
        with self.assertRaises(sdt.HdcError):
            sdt._read_device_local(hdc)


# ------------------------------------------------------------ check

class TestCheck(unittest.TestCase):

    def test_ok_when_time_matches_and_rtc_is_utc(self):
        """正常状态：时间接近 PC、RTC == UTC → 通过。"""
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc))
        self.assertTrue(sdt.check(hdc))

    def test_fails_on_wrong_year(self):
        """★ 典型故障：RTC 停在出厂年份 2017。"""
        pc = datetime.datetime.now()
        wrong = pc.replace(year=2017)
        hdc = make_hdc(wrong, wrong - datetime.timedelta(hours=8),
                       dev_stamp(wrong - datetime.timedelta(hours=8)))
        self.assertFalse(sdt.check(hdc))

    def test_detects_missing_u(self):
        """★ RTC 存的是**本地**时间（不是 UTC）→ 说明写的时候漏了 -u。

        这是最隐蔽的错误：时间看着对，但重启后会快一个时区。
        """
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        # RTC 写成 本地时间 而不是 UTC → 应判失败
        hdc = make_hdc(now, utc, dev_stamp(now))
        self.assertFalse(sdt.check(hdc))

    def test_fails_when_drift_exceeds_one_minute(self):
        now = datetime.datetime.now().replace(microsecond=0)
        off = now - datetime.timedelta(minutes=5)
        hdc = make_hdc(off, off - datetime.timedelta(hours=8),
                       dev_stamp(off - datetime.timedelta(hours=8)))
        self.assertFalse(sdt.check(hdc))

    def test_check_never_writes(self):
        """★ check 是只读操作 —— 绝不能有副作用。"""
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc))
        sdt.check(hdc)
        self.assertEqual(hdc.written(), [],
                         f'check 不应发出写命令，实际: {hdc.written()}')

    def test_check_tolerates_unreadable_rtc(self):
        """部分系统 hwclock -r 需 root，读不到时不该崩，只提示。"""
        now = datetime.datetime.now().replace(microsecond=0)
        hdc = make_hdc(now, now - datetime.timedelta(hours=8), 'hwclock: fail')
        sdt.check(hdc)   # 不抛异常即通过

    def test_check_with_real_hwclock_format(self):
        """★ 回归：RTC 用**真实** hwclock 输出格式时，check 必须正常判定。

        真机上第一版就是在这一步崩的（`ValueError: year 0 is out of range`）。
        """
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        rtc_real = utc.strftime('%a %b %d %H:%M:%S %Y') + '  0.000000 seconds'
        hdc = make_hdc(now, utc, rtc_real)
        self.assertTrue(sdt.check(hdc), 'RTC 为真实格式且正确时，应判为正常')

    def test_check_detects_missing_u_with_real_format(self):
        """★ 真实格式下也要能检出「漏 -u」—— RTC 存了本地时间。"""
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        rtc_wrong = now.strftime('%a %b %d %H:%M:%S %Y') + '  0.000000 seconds'
        hdc = make_hdc(now, utc, rtc_wrong)
        self.assertFalse(sdt.check(hdc),
                         'RTC 存本地时间（漏 -u）必须判为失败')


# ------------------------------------------------------------ sync

class TestSync(unittest.TestCase):

    def test_sync_must_write_rtc_with_u(self):
        """★★ 核心：写 RTC 必须带 `-u`。

        漏掉 `-u` → 设备下次开机快 8 小时，**且不报任何错**。
        这条测试是防回归的闸门。
        """
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc))
        sdt.sync(hdc)

        writes = [c for c in hdc.calls if c.startswith('hwclock -w')]
        self.assertTrue(writes, f'必须写 RTC，实际命令: {hdc.calls}')
        self.assertIn('-u', writes[0],
                      f'★ 写 RTC 的命令必须带 -u，实际: {writes[0]!r}')

    def test_sync_sets_system_time_first(self):
        """顺序：先设系统时间，再写 RTC（反过来会把旧时间写进 RTC）。"""
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc))
        sdt.sync(hdc)

        idx_date = next(i for i, c in enumerate(hdc.calls)
                        if c.startswith('date "'))
        idx_rtc = next(i for i, c in enumerate(hdc.calls)
                       if c.startswith('hwclock -w'))
        self.assertLess(idx_date, idx_rtc,
                        '必须先 date 设时间，再 hwclock 写 RTC')

    def test_sync_passes_current_pc_time(self):
        """设置的应是 PC 当前时间（允许几秒误差）。"""
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc))
        sdt.sync(hdc)

        cmd = next(c for c in hdc.calls if c.startswith('date "'))
        stamp = cmd.split('"')[1]
        parsed = datetime.datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S')
        self.assertLess(abs((parsed - now).total_seconds()), 60)

    def test_sync_reports_failure_when_hwclock_fails(self):
        """★ hwclock 失败必须明确返回 False，不能静默通过。

        真实场景：非 root 时 hwclock 可能被拒。静默通过会让人以为校时成功。
        """
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc), hwclock_rc=1)
        self.assertFalse(sdt.sync(hdc))

    def test_sync_returns_true_on_success(self):
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc))
        self.assertTrue(sdt.sync(hdc))

    def test_sync_verifies_after_writing(self):
        """写完必须回读校验 —— 不能发完命令就宣布成功。"""
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        hdc = make_hdc(now, utc, dev_stamp(utc))
        sdt.sync(hdc)

        self.assertIn('hwclock -r', hdc.calls, '必须回读 RTC 校验')
        # date 至少被调用两次：一次设、一次读回来校验
        date_reads = [c for c in hdc.calls if c == 'date']
        self.assertGreaterEqual(len(date_reads), 1,
                                '必须回读设备时间校验')


# ------------------------------------------------------------ main

class TestMain(unittest.TestCase):

    def test_check_mode_exit_code_zero_when_ok(self):
        """--check 在时间正常时返回 0。"""
        now = datetime.datetime.now().replace(microsecond=0)
        utc = now - datetime.timedelta(hours=8)
        fake = make_hdc(now, utc, dev_stamp(utc))

        class _HdcCls:                    # 替掉 main 里的 Hdc 构造
            def __init__(self, *a, **kw):
                pass

            def __getattr__(self, name):
                return getattr(fake, name)

        orig = sdt.Hdc
        sdt.Hdc = _HdcCls
        try:
            rc = sdt.main(['--check'])
        finally:
            sdt.Hdc = orig
        self.assertEqual(rc, 0)

    def test_help_works(self):
        with self.assertRaises(SystemExit) as cm:
            sdt.main(['--help'])
        self.assertEqual(cm.exception.code, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
