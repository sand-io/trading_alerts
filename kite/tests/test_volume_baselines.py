"""Cumulative Kite volume must remain comparable with historical bar volume."""
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from scanner import SymbolData


class VolumeBaselineTests(unittest.TestCase):
    def load(self, interval, rows):
        data = SymbolData(12345, 'NSE:TEST')
        records = [dict(date=stamp, open=99, high=101, low=98,
                        close=100, volume=volume) for stamp, volume in rows]
        with patch('scanner.history_requests.fetch', return_value=records):
            data.check_and_add_intervals(Mock(), {interval})
        return data

    def tick(self, data, stamp, volume):
        data.add_tick(dict(exchange_timestamp=stamp, last_price=101,
                           average_price=100, volume=volume))

    def test_history_to_live_uses_session_sum_for_each_interval(self):
        for interval, times in [('5m', [(9, 15), (9, 20), (9, 25)]),
                                ('1h', [(9, 15), (10, 15), (11, 15)]),
                                ('1d', [(0, 0)])]:
            with self.subTest(interval=interval):
                rows = [(datetime(2026, 10, 14, 0 if interval == '1d' else 15,
                                  0 if interval == '1d' else 15), 9000)]
                rows += [(datetime(2026, 10, 15, h, m), 200) for h, m in times]
                data = self.load(interval, rows)
                h, m = times[-1] if interval != '1d' else (12, 0)
                self.tick(data, datetime(2026, 10, 15, h, m + 1), 200 * len(times) + 50)
                self.assertEqual(data.candles[interval][-1]['volume'], 250)
                self.tick(data, datetime(2026, 10, 15, h, m + 2), 200 * len(times) + 70)
                self.assertEqual(data.candles[interval][-1]['volume'], 270)

    def test_session_reset_counts_first_hour_and_daily_volume(self):
        for interval in ('5m', '1h', '1d'):
            with self.subTest(interval=interval):
                stamp = datetime(2026, 10, 14, 0, 0) if interval == '1d' else datetime(2026, 10, 14, 15, 15)
                data = self.load(interval, [(stamp, 10000)])
                self.tick(data, datetime(2026, 10, 15, 9, 15), 50)
                self.assertEqual(data.candles[interval][-1]['volume'], 50)
                self.tick(data, datetime(2026, 10, 15, 9, 19), 500)
                self.assertEqual(data.candles[interval][-1]['volume'], 500)

    def test_hour_rollover_does_not_inherit_inflated_day_volume(self):
        data = self.load('1h', [(datetime(2026, 10, 15, 9, 15), 9000),
                                 (datetime(2026, 10, 15, 10, 15), 200)])
        self.tick(data, datetime(2026, 10, 15, 10, 30), 9250)
        self.tick(data, datetime(2026, 10, 15, 11, 15), 9260)
        self.tick(data, datetime(2026, 10, 15, 11, 30), 9600)
        self.assertEqual([c['volume'] for c in data.candles['1h']], [9000, 250, 350])

    def test_new_session_midday_without_history_does_not_create_volume_spike(self):
        data = self.load('1h', [(datetime(2026, 10, 14, 15, 15), 10000)])
        self.tick(data, datetime(2026, 10, 15, 12, 20), 5000)
        self.tick(data, datetime(2026, 10, 15, 12, 21), 5050)
        self.assertEqual(data.candles['1h'][-1]['volume'], 50)

    def test_no_history_opening_and_mid_session_baselines(self):
        for interval, stamp, expected in [('1h', datetime(2026, 10, 15, 9, 20), 500),
                                           ('1h', datetime(2026, 10, 15, 12, 20), 0),
                                           ('1d', datetime(2026, 10, 15, 12, 20), 500)]:
            with self.subTest(interval=interval, stamp=stamp):
                data = SymbolData(1, 'NSE:TEST')
                data.intervals = {interval}
                data.candles[interval] = []
                self.tick(data, stamp, 500)
                self.assertEqual(data.candles[interval][-1]['volume'], expected)
