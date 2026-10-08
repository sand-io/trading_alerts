import unittest
from unittest.mock import patch

from notifier import Notifier


class TestLinuxNotifier(unittest.TestCase):
    def notifier(self):
        notifier = Notifier()
        notifier.cooldown_seconds = 0
        return notifier

    @patch('notifier.subprocess.run')
    @patch('notifier.shutil.which', return_value='/usr/bin/notify-send')
    @patch('notifier.platform.system', return_value='Linux')
    def test_linux_uses_notify_send(self, _system, _which, run):
        sent = self.notifier().send_alert(
            'NSE:TEST', True, '90.0% conditions passed', 100.5)
        self.assertTrue(sent)
        args = run.call_args.args[0]
        self.assertEqual(args[0], '/usr/bin/notify-send')
        self.assertIn('BULLISH', args[-2])
        self.assertIn('100.5', args[-1])
        self.assertEqual(run.call_args.kwargs, {'check': True, 'timeout': 5})

    @patch('notifier.subprocess.run')
    @patch('notifier.shutil.which', return_value=None)
    @patch('notifier.platform.system', return_value='Linux')
    def test_missing_notify_send_keeps_terminal_alert_successful(self, _system, _which, run):
        sent = self.notifier().send_alert(
            'NSE:TEST', False, '90.0% conditions passed', 99.0)
        self.assertTrue(sent)
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
