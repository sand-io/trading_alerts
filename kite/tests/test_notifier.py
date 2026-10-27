import unittest
import threading
from unittest.mock import patch

from notifier import Notifier, format_alert


class AlertFormatTests(unittest.TestCase):
    def context(self):
        return {'strategy_name': 'BTST Daily-Hourly Confluence', 'strategy_id': 'btst_setup',
                'confirmation_timeframe': '5m', 'threshold': 100,
                'market_time': '2026-10-27T04:30:12+00:00', 'vwap': 99.25,
                'bullish': {'passed': 7, 'total': 7, 'percentage': 100.0},
                'bearish': {'passed': 6, 'total': 7, 'percentage': 600/7},
                'candle': {'time': 1793075400, 'close': 100.0}}

    def test_live_alert_identifies_strategy_and_exchange_time_without_claiming_confirmation(self):
        alert = format_alert('NFO:TEST26OCTFUT', True, 'rules', 100.5, self.context())
        self.assertIn('BULLISH | NFO:TEST26OCTFUT', alert['title'])
        self.assertIn('btst_setup', alert['title'])
        self.assertIn('Strategy: BTST Daily-Hourly Confluence [btst_setup]', alert['message'])
        for detail in ('INR 100.50', 'VWAP: 99.25', '7/7 (100.0%)', 'Required: 100%',
                       '5m confirmation | Live / provisional', '27 Oct 2026 10:00:12 IST', 'Candle start:'):
            self.assertIn(detail, alert['message'])
        self.assertNotIn('Signal close:', alert['message'])

    def test_closed_alert_distinguishes_signal_price_and_close_from_current_ltp(self):
        context = {**self.context(), 'evaluation_mode': 'closed_candle',
                   'as_of': 1793075580, 'confirmation_timeframe': '3m'}
        alert = format_alert('MCX:GOLD26DECFUT', False, 'rules', 101.5, context)
        self.assertIn('BEARISH', alert['title'])
        self.assertIn('6/7 (85.7%)', alert['message'])
        self.assertIn('3m confirmation | Closed candle', alert['message'])
        self.assertIn('Signal close:', alert['message'])
        self.assertIn('LTP: INR 101.50', alert['message'])
        self.assertIn('Signal close price: INR 100.00', alert['message'])

    def test_structured_notification_keeps_stable_strategy_cooldown(self):
        notifier = Notifier()
        notifier.cooldown_seconds = 300
        alert = format_alert('NSE:TEST', True, 'rules', 100, self.context())
        with patch.object(notifier, '_send_desktop') as desktop, patch('builtins.print'):
            self.assertTrue(notifier.send_alert('NSE:TEST', True, 'rules', 100,
                                               cooldown_key='btst:bull', notification=alert))
            self.assertFalse(notifier.send_alert('NSE:TEST', True, 'different rules', 101,
                                                cooldown_key='btst:bull', notification=alert))
            self.assertTrue(notifier.send_alert('NSE:TEST', True, 'rules', 100,
                                               cooldown_key='default:bull', notification=alert))
        self.assertEqual(desktop.call_count, 2)
        desktop.assert_called_with(alert['title'], alert['message'])

    def test_multiline_notification_is_escaped_for_macos(self):
        with patch('notifier.platform.system', return_value='Darwin'), \
                patch('notifier.subprocess.run') as run:
            Notifier()._send_desktop('Strategy "Test"', 'Price: 100\nPath: C:\\Test')
        script = run.call_args.args[0][2]
        self.assertIn('Strategy \\"Test\\"', script)
        self.assertIn('Price: 100\\nPath: C:\\\\Test', script)

    def test_windows_notification_preserves_quotes_and_literal_dollar_text(self):
        with patch('notifier.platform.system', return_value='Windows'), \
                patch('notifier.subprocess.run') as run:
            Notifier()._send_desktop("Trader's strategy", 'Price: $100\nRules: 7/7')
        script = run.call_args.args[0][2]
        self.assertIn("$toast.BalloonTipTitle = 'Trader''s strategy'", script)
        self.assertIn("$toast.BalloonTipText = 'Price: $100\nRules: 7/7'", script)


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

    def test_slow_desktop_delivery_does_not_block_alert_acceptance(self):
        notifier = self.notifier()
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(5)
        with patch.object(notifier, '_send_desktop', side_effect=blocked):
            notifier.start_background()
            try:
                self.assertTrue(notifier.send_alert('FIRST', True, 'match', 100))
                self.assertTrue(entered.wait(2))
                self.assertTrue(notifier.send_alert('SECOND', True, 'match', 100))
                self.assertEqual(notifier._desktop_queue.unfinished_tasks, 2)
            finally:
                release.set()
                notifier.stop_background(3)
        self.assertFalse(notifier._desktop_worker.is_alive())
        self.assertEqual(notifier._desktop_queue.unfinished_tasks, 0)


if __name__ == '__main__':
    unittest.main()
