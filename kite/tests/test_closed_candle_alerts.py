"""Alerts must use the same final snapshot as chart confirmations."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from scanner import MARKET_TZ
from . import test_confirmation_timeframes as fixtures


class ClosedCandleAlertTests(unittest.TestCase):
    setUp = fixtures.ConfirmationTimeframeTests.setUp
    profile = fixtures.ConfirmationTimeframeTests.profile
    scanner = fixtures.ConfirmationTimeframeTests.scanner
    data = fixtures.ConfirmationTimeframeTests.data

    def setup_scanner(self, interval='5m', **extra):
        profile = self.profile(interval, **extra)
        scanner = self.scanner(profile)
        scanner.event_callback = Mock()
        stamp = datetime(2026, 10, 12, 10, 15)
        data = self.data(stamp, {'5m', interval})
        scanner.symbol_data = {1: data}
        scanner.get_required_intervals = Mock(return_value=data.intervals)
        clock = patch('scanner.datetime', wraps=datetime)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.return_value = stamp.replace(tzinfo=MARKET_TZ)
        return scanner, data, stamp

    def tick(self, scanner, stamp, price, defer=True):
        scanner._process_tick_locked({'instrument_token': 1, 'last_price': price,
            'timestamp': stamp, 'volume': 250}, defer_evaluation=defer)

    def drain(self, scanner, allow_alerts=True):
        tasks = list(scanner._pending_evaluations.values())
        scanner._pending_evaluations.clear()
        for task in tasks:
            scanner._evaluate_all_snapshot(*task[:4], profiles=task[8], allow_alerts=allow_alerts)

    def test_intrabar_match_that_fails_at_close_never_alerts(self):
        scanner, data, stamp = self.setup_scanner()
        with patch('scanner.notifier.send_alert', return_value=True) as notify:
            self.tick(scanner, stamp + timedelta(seconds=1), 200)
            self.drain(scanner)
            self.assertTrue(scanner.latest_results[1]['bullish']['triggered'])
            notify.assert_not_called()
            # Same monotonic time ensures this final tick is throttled.
            with patch('scanner.time.monotonic', return_value=100):
                scanner._last_evaluation_at[1] = 100
                self.tick(scanner, stamp + timedelta(minutes=4, seconds=59), 100)
            self.clock.now.return_value = (stamp + timedelta(minutes=5)).replace(tzinfo=MARKET_TZ)
            self.tick(scanner, stamp + timedelta(minutes=5), 220)
            self.drain(scanner)
            notify.assert_not_called()
        signal = scanner.latest_results[1]['confirmed_signal']
        self.assertFalse(signal['bullish']['triggered'])
        self.assertEqual(signal['candle']['close'], 100)
        self.assertEqual(scanner.history.events(1, stamp.date().isoformat(), 'alert'), [])

    def test_final_match_alerts_once_with_confirmed_price_and_rules(self):
        for defer in (True, False):
            with self.subTest(defer=defer):
                scanner, data, stamp = self.setup_scanner()
                with patch('scanner.notifier.send_alert', return_value=True) as notify:
                    self.tick(scanner, stamp + timedelta(seconds=1), 100, defer)
                    if defer:
                        self.drain(scanner)
                    with patch('scanner.time.monotonic', return_value=100):
                        scanner._last_evaluation_at[1] = 100
                        self.tick(scanner, stamp + timedelta(minutes=4, seconds=59), 200, defer)
                    notify.assert_not_called()
                    self.clock.now.return_value = (stamp + timedelta(minutes=5)).replace(tzinfo=MARKET_TZ)
                    self.tick(scanner, stamp + timedelta(minutes=5), 90, defer)
                    if defer:
                        self.drain(scanner)
                    notify.assert_called_once()
                    self.assertEqual(notify.call_args.kwargs['price'], 200)
                    self.tick(scanner, stamp + timedelta(minutes=5, seconds=1), 90, defer)
                    if defer:
                        self.drain(scanner)
                    notify.assert_called_once()
                day = stamp.date().isoformat()
                alert = scanner.history.events(1, day, 'alert')[0]
                confirmed = scanner.history.events(1, day, 'confirmed')[0]
                for key in ('candle', 'bullish', 'bearish', 'price', 'strategy_version'):
                    self.assertEqual(alert[key], confirmed[key])

    def test_session_close_refreshes_throttled_tick_and_notifies_once(self):
        scanner, data, stamp = self.setup_scanner()
        with patch('scanner.notifier.send_alert', return_value=True) as notify:
            self.tick(scanner, stamp.replace(hour=15, minute=25), 100)
            self.drain(scanner)
            with patch('scanner.time.monotonic', return_value=100):
                scanner._last_evaluation_at[1] = 100
                self.tick(scanner, stamp.replace(hour=15, minute=29, second=59), 200)
            notify.assert_not_called()
            close = stamp.replace(hour=15, minute=30)
            self.clock.now.return_value = close.replace(tzinfo=MARKET_TZ)
            scanner.finalize_session(close)
            scanner.finalize_session(close + timedelta(seconds=1))
            notify.assert_called_once()
            self.assertEqual(notify.call_args.kwargs['price'], 200)
            self.assertTrue(scanner.event_callback.call_args.args[0]['confirmed_signal']['bullish']['triggered'])

    def test_stale_confirmation_and_shutdown_do_not_notify(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                scanner, data, stamp = self.setup_scanner()
                self.tick(scanner, stamp, 200)
                self.drain(scanner)
                self.clock.now.return_value = (stamp + timedelta(minutes=5 if shutdown else 6)).replace(tzinfo=MARKET_TZ)
                self.tick(scanner, stamp + timedelta(minutes=5), 200)
                if shutdown:
                    scanner._stop_requested.set()
                with patch('scanner.notifier.send_alert') as notify:
                    self.drain(scanner)
                    notify.assert_not_called()
                self.assertEqual(len(scanner.history.events(1, stamp.date().isoformat(), 'confirmed')), 1)

    def test_confirmation_uses_configured_timeframe_and_cooldown_rejection(self):
        scanner, data, stamp = self.setup_scanner('15m')
        with patch('scanner.notifier.send_alert', return_value=False) as notify:
            self.tick(scanner, stamp, 200)
            self.drain(scanner)
            self.clock.now.return_value = (stamp + timedelta(minutes=5)).replace(tzinfo=MARKET_TZ)
            self.tick(scanner, stamp + timedelta(minutes=5), 200)
            self.drain(scanner)
            notify.assert_not_called()
            self.clock.now.return_value = (stamp + timedelta(minutes=15)).replace(tzinfo=MARKET_TZ)
            self.tick(scanner, stamp + timedelta(minutes=15), 200)
            self.drain(scanner)
            notify.assert_called_once()
        day = stamp.date().isoformat()
        self.assertEqual(scanner.history.events(1, day, 'alert'), [])
        self.assertEqual(len(scanner.history.events(1, day, 'confirmed')), 1)

    def test_bearish_alert_uses_closed_candle_even_if_new_candle_is_bullish(self):
        scanner, data, stamp = self.setup_scanner(bullish=[],
            bearish=['5-minute Close is less than Open'])
        with patch('scanner.notifier.send_alert', return_value=True) as notify:
            self.tick(scanner, stamp, 100)
            self.drain(scanner)
            notify.assert_not_called()
            self.clock.now.return_value = (stamp + timedelta(minutes=5)).replace(tzinfo=MARKET_TZ)
            self.tick(scanner, stamp + timedelta(minutes=5), 220)
            self.drain(scanner)
            notify.assert_called_once()
            self.assertFalse(notify.call_args.kwargs['is_bullish'])
            self.assertEqual(notify.call_args.kwargs['price'], 100)
        signal = scanner.latest_results[1]['confirmed_signal']
        self.assertTrue(signal['bearish']['triggered'])

    def test_daily_confirmation_freshness_is_measured_from_session_close(self):
        scanner, data, stamp = self.setup_scanner('1d')
        with patch('scanner.notifier.send_alert', return_value=True) as notify:
            # Daily close can be fresh even when its last trade was minutes ago.
            self.tick(scanner, stamp.replace(hour=15, minute=28), 200)
            self.drain(scanner)
            notify.assert_not_called()
            close = stamp.replace(hour=15, minute=30)
            self.clock.now.return_value = close.replace(tzinfo=MARKET_TZ)
            scanner.finalize_session(close)
            scanner.finalize_session(close)
            notify.assert_called_once()
        alert = scanner.history.events(1, stamp.date().isoformat(), 'alert')[0]
        self.assertEqual(alert['confirmation_timeframe'], '1d')
        self.assertEqual(alert['confirmed_at'], '2026-10-12T15:30:00+05:30')

    def test_missing_timeframe_prevents_closed_candle_alert(self):
        scanner, data, stamp = self.setup_scanner(bullish=[
            '5-minute Close is greater than Open', '1-hour Close is greater than Open'])
        # Deliberately keep the hourly history unavailable.
        with patch('scanner.notifier.send_alert') as notify:
            self.tick(scanner, stamp, 200)
            self.drain(scanner)
            self.clock.now.return_value = (stamp + timedelta(minutes=5)).replace(tzinfo=MARKET_TZ)
            self.tick(scanner, stamp + timedelta(minutes=5), 200)
            self.drain(scanner)
            notify.assert_not_called()
        self.assertFalse(scanner.latest_results[1]['confirmed_signal']['data_ready'])
