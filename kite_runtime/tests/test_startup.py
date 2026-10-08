import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kite_runtime import auth as kite_auth
from kite_runtime import launcher as start
from kiteconnect.exceptions import TokenException


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / '.env'
        self.env.write_text('KITE_API_KEY=test\nKITE_API_SECRET=secret\nKITE_ACCESS_TOKEN=old\nOTHER=value\n')
        self.env_patch = patch.object(kite_auth, 'ENV_FILE', self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.os_patch = patch.dict(os.environ, {}, clear=True)
        self.os_patch.start()
        self.addCleanup(self.os_patch.stop)

    @patch('kite_runtime.auth.capture_request_token')
    @patch('kite_runtime.auth.KiteConnect')
    def test_valid_token_skips_login(self, client, capture):
        self.assertEqual(kite_auth.ensure_session(), 'old')
        client.return_value.profile.assert_called_once()
        capture.assert_not_called()
        client.return_value.generate_session.assert_not_called()

    @patch('kite_runtime.auth.capture_request_token', return_value='request')
    @patch('kite_runtime.auth.KiteConnect')
    def test_expired_token_is_replaced_in_shared_env(self, client, capture):
        client.return_value.profile.side_effect = TokenException('expired')
        client.return_value.generate_session.return_value = {'access_token': 'new'}
        self.assertEqual(kite_auth.ensure_session(), 'new')
        self.assertIn('KITE_ACCESS_TOKEN=new', self.env.read_text())
        self.assertIn('OTHER=value', self.env.read_text())
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.environ['KITE_ACCESS_TOKEN'], 'new')
        client.return_value.generate_session.assert_called_once_with('request', api_secret='secret')

    @patch('kite_runtime.auth.capture_request_token')
    @patch('kite_runtime.auth.KiteConnect')
    def test_network_error_does_not_force_login_or_overwrite(self, client, capture):
        client.return_value.profile.side_effect = ConnectionError()
        with self.assertRaises(ConnectionError):
            kite_auth.ensure_session()
        capture.assert_not_called()
        self.assertIn('KITE_ACCESS_TOKEN=old', self.env.read_text())

    @patch('kite_runtime.auth.webbrowser.open')
    @patch('kite_runtime.auth.secrets.token_urlsafe', return_value='expected')
    @patch('kite_runtime.auth.HTTPServer')
    def test_callback_rejects_wrong_state_and_accepts_valid_redirect(self, server, _state, browser):
        replies = []
        paths = iter(['/callback?request_token=bad&state=wrong',
                      '/callback?request_token=good&state=expected'])

        def request():
            handler_class = server.call_args.args[1]
            handler = object.__new__(handler_class)
            handler.path = next(paths)
            handler.wfile = io.BytesIO()
            handler.send_response = replies.append
            handler.send_header = MagicMock()
            handler.end_headers = MagicMock()
            handler.do_GET()

        server.return_value.__enter__.return_value.handle_request.side_effect = request
        client = MagicMock()
        client.login_url.return_value = 'https://example.test/login?api_key=test'
        self.assertEqual(kite_auth.capture_request_token(client), 'good')
        self.assertEqual(replies, [400, 200])
        self.assertIn('redirect_params=state%3Dexpected', browser.call_args.args[0])
        server.assert_called_once_with(('127.0.0.1', 8765), unittest.mock.ANY)


class LauncherTests(unittest.TestCase):
    def test_windows_children_use_process_groups_and_supported_stop_signal(self):
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(start.sys, 'platform', 'win32'), \
                patch.object(start.subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, create=True), \
                patch.object(start.signal, 'CTRL_BREAK_EVENT', 1, create=True):
            self.assertEqual(start.service_process_options(), {'creationflags': 512})
            start.stop_services([child])
        child.send_signal.assert_called_once_with(1)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_failed_signal_falls_back_and_still_stops_remaining_children(self):
        first, second = MagicMock(), MagicMock()
        first.poll.return_value = second.poll.return_value = None
        first.send_signal.side_effect = OSError('console unavailable')
        start.stop_services([first, second])
        first.terminate.assert_called_once()
        second.send_signal.assert_called_once()
        first.wait.assert_called_once()
        second.wait.assert_called_once()

    @patch('kite_runtime.launcher.signal.signal')
    @patch('kite_runtime.launcher.stop_services')
    @patch('kite_runtime.launcher.subprocess.Popen')
    @patch('kite_runtime.launcher.ensure_session')
    def test_starts_only_selected_app(self, auth, popen, stop, _signal):
        for app in ('kite', 'mtf'):
            with self.subTest(application=app):
                popen.reset_mock()
                stop.reset_mock()
                process = MagicMock()
                process.poll.return_value = 2
                popen.return_value = process
                with patch.object(start.sys, 'argv', ['kite_runtime', app]):
                    self.assertEqual(start.main(), 2)
                popen.assert_called_once()
                command = popen.call_args.args[0]
                if app == 'kite':
                    self.assertIn('dashboard:app', command)
                    self.assertEqual(popen.call_args.kwargs['cwd'], start.ROOT / 'kite')
                else:
                    self.assertEqual(command[-1], 'mtf_alert')
                    self.assertEqual(popen.call_args.kwargs['cwd'], start.ROOT)
                stop.assert_called_once_with([process])
        self.assertEqual(auth.call_count, 2)

    @patch('kite_runtime.launcher.time.monotonic', return_value=0)
    def test_stuck_child_is_killed_after_graceful_stop(self, _clock):
        child = MagicMock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('service', 10), 0]
        start.stop_services([child])
        child.send_signal.assert_called_once_with(start.signal.SIGINT)
        child.kill.assert_called_once()


if __name__ == '__main__':
    unittest.main()
