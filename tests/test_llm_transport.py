"""llm_transport.post_chat 的重试路径 —— mock urlopen，离线不碰真实网络。

钉住三条口径：transient（5xx/429/网络错）按退避重试后成功；
non-transient（4xx）一次都不重试；重试用尽抛最后一次的原始异常。
"""
import os
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from ohauto import llm_transport                         # noqa: E402


def _resp(body):
    cm = mock.MagicMock()
    cm.__enter__.return_value = cm
    cm.read.return_value = body
    return cm


class TestPostChatRetry(unittest.TestCase):

    def setUp(self):
        self.sleeps = []
        p = mock.patch('ohauto.llm_transport.time.sleep',
                       side_effect=self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    def _http_error(self, code):
        e = urllib.error.HTTPError('http://t/chat/completions', code, 'err',
                                   None, None)
        self.addCleanup(e.close)      # 不关会在 GC 时打 ResourceWarning
        return e

    @mock.patch('urllib.request.urlopen')
    def test_first_try_success(self, urlopen):
        urlopen.return_value = _resp(b'{"ok": 1}')
        out = llm_transport.post_chat('http://t', 'k', {}, max_retries=2)
        self.assertEqual(out, {'ok': 1})
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(self.sleeps, [])

    @mock.patch('urllib.request.urlopen')
    def test_no_retry_when_max_retries_zero(self, urlopen):
        urlopen.side_effect = self._http_error(500)
        with self.assertRaises(urllib.error.HTTPError):
            llm_transport.post_chat('http://t', 'k', {})
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(self.sleeps, [])

    @mock.patch('urllib.request.urlopen')
    def test_transient_500_then_success(self, urlopen):
        urlopen.side_effect = [self._http_error(500), _resp(b'{"ok": 2}')]
        out = llm_transport.post_chat('http://t', 'k', {}, max_retries=2)
        self.assertEqual(out, {'ok': 2})
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(self.sleeps, [0.5])

    @mock.patch('urllib.request.urlopen')
    def test_backoff_increments(self, urlopen):
        urlopen.side_effect = [self._http_error(429), self._http_error(503),
                               _resp(b'{"ok": 3}')]
        out = llm_transport.post_chat('http://t', 'k', {}, max_retries=2)
        self.assertEqual(out, {'ok': 3})
        self.assertEqual(self.sleeps, [0.5, 1.0])

    @mock.patch('urllib.request.urlopen')
    def test_non_transient_401_raises_immediately(self, urlopen):
        urlopen.side_effect = self._http_error(401)
        with self.assertRaises(urllib.error.HTTPError):
            llm_transport.post_chat('http://t', 'k', {}, max_retries=3)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(self.sleeps, [])

    @mock.patch('urllib.request.urlopen')
    def test_transient_exhausted_raises_last(self, urlopen):
        last = self._http_error(503)
        urlopen.side_effect = [self._http_error(502), self._http_error(429), last]
        with self.assertRaises(urllib.error.HTTPError) as cm:
            llm_transport.post_chat('http://t', 'k', {}, max_retries=2)
        self.assertIs(cm.exception, last)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(self.sleeps, [0.5, 1.0])

    @mock.patch('urllib.request.urlopen')
    def test_network_error_is_transient(self, urlopen):
        urlopen.side_effect = [urllib.error.URLError('refused'),
                               _resp(b'{"ok": 4}')]
        out = llm_transport.post_chat('http://t', 'k', {}, max_retries=1)
        self.assertEqual(out, {'ok': 4})
        self.assertEqual(urlopen.call_count, 2)

    @mock.patch('urllib.request.urlopen')
    def test_request_shape(self, urlopen):
        """URL 拼接去掉尾部斜杠，鉴权头与超时传到位。"""
        urlopen.return_value = _resp(b'{}')
        llm_transport.post_chat('http://t/', 'key-x', {}, timeout=7.5)
        req = urlopen.call_args[0][0]
        kw = urlopen.call_args[1]
        self.assertEqual(req.get_full_url(), 'http://t/chat/completions')
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.get_header('Authorization'), 'Bearer key-x')
        self.assertEqual(kw.get('timeout'), 7.5)


class TestParsers(unittest.TestCase):

    def test_extract_json_array_edges(self):
        self.assertEqual(llm_transport.extract_json_array(''), [])
        self.assertEqual(llm_transport.extract_json_array('[oops]'), [])
        self.assertEqual(llm_transport.extract_json_array('{"a": 1}'), [])
        self.assertEqual(
            llm_transport.extract_json_array('```json\n[{"a": 1}]\n```'),
            [{'a': 1}])

    @mock.patch.dict(os.environ, {'T_URL': 'http://env', 'T_KEY': 'k-env'})
    def test_env_triple_override_wins(self):
        base, key, model, missing = llm_transport.env_triple(
            ['T_URL'], ['T_KEY'], ['T_MODEL'],
            overrides={'base_url': 'http://ovr'})
        self.assertEqual((base, key, model), ('http://ovr', 'k-env', ''))
        self.assertEqual(missing, ['model'])


if __name__ == '__main__':
    unittest.main()
