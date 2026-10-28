"""Cumulative Kite volume must remain comparable with historical bar volume."""
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from scanner import SymbolData
from scanner import normalize_kite_tick, MARKET_TZ
from conditions import Condition
from scanner import EvaluationSnapshot
from strategies import StrategyStore
from strategy_runtime import evaluate_side
from pathlib import Path
import pandas as pd


class VolumeBaselineTests(unittest.TestCase):
    def test_btst_real_indicators_qualify_after_native_volume_update(self):
        data = SymbolData(12345, 'NFO:TEST26NOVFUT')
        daily_times = pd.bdate_range(end='2026-10-28', periods=40)
        hourly_times = [pd.Timestamp(day) + pd.Timedelta(hours=h, minutes=15)
                        for day in daily_times[-10:] for h in range(9, 16)
                        if pd.Timestamp(day) + pd.Timedelta(hours=h, minutes=15) <= pd.Timestamp('2026-10-28 13:15')]
        for interval, times in (('1d', daily_times), ('1h', hourly_times)):
            records = [dict(date=stamp.to_pydatetime(), open=100 * 1.01**i - 1,
                            high=100 * 1.01**i + 1, low=100 * 1.01**i - 2,
                            close=100 * 1.01**i, volume=100)
                       for i, stamp in enumerate(times)]
            records[-1]['volume'] = 20
            data.candles[interval] = data._history_candles(records)
            data.intervals.add(interval)
            data.last_tick_time[interval] = records[-1]['date']
        price = data.candles['1h'][-1]['close'] + 2
        profile = StrategyStore(Path(__file__).resolve().parents[1]).load('btst_setup')
        before = evaluate_side('bullish', profile['bullish'], price, price - 1,
                               EvaluationSnapshot(data), profile['threshold'])
        self.assertFalse(before.triggered)
        self.assertEqual(before.passed, 6)
        data.add_tick({'exchange_timestamp': datetime(2026, 10, 28, 13, 20),
                       'last_price': price, 'volume_traded': 600,
                       'average_traded_price': price - 1})
        after = evaluate_side('bullish', profile['bullish'], price, price - 1,
                              EvaluationSnapshot(data), profile['threshold'])
        self.assertTrue(after.triggered)
        self.assertEqual(after.passed, 7)
        self.assertFalse(any(rule.error for rule in after.rules))

    def test_mcx_session_reset_uses_commodity_open_and_native_fields(self):
        data = SymbolData(1, 'MCX:GOLD26DECFUT')
        data.intervals = {'1h'}
        data.candles['1h'] = data._history_candles([
            dict(date=datetime(2026, 10, 27, 23), open=100, high=102,
                 low=99, close=101, volume=10000)])
        data.last_tick_time['1h'] = datetime(2026, 10, 27, 23)
        data.add_tick({'exchange_timestamp': datetime(2026, 10, 28, 9, 1),
                       'last_price': 102, 'volume_traded': 50,
                       'average_traded_price': 101})
        self.assertEqual(data.candles['1h'][-1]['time'], datetime(2026, 10, 28, 9))
        self.assertEqual(data.candles['1h'][-1]['volume'], 50)
        self.assertEqual(data.candles['1h'][-1]['vwap'], 101)

    def test_real_kite_sdk_packet_preserves_volume_and_vwap(self):
        import struct
        from kiteconnect import KiteTicker
        stamp = datetime(2026, 10, 28, 13, 20, tzinfo=MARKET_TZ)
        packet = bytearray(184)
        for offset, value in ((0, 15768834), (4, 10100), (12, 9950), (16, 600),
                              (60, int(stamp.timestamp()))):
            struct.pack_into('>I', packet, offset, value)
        wire = struct.pack('>HH', 1, len(packet)) + packet
        tick = normalize_kite_tick(KiteTicker('test', 'test')._parse_binary(wire)[0])
        self.assertEqual(tick['volume_traded'], 600)
        self.assertEqual(tick['average_traded_price'], 99.5)
        rows = [(datetime(2026, 10, 28, h, 15), volume)
                for h, volume in ((9, 100), (10, 120), (11, 90), (12, 80), (13, 10))]
        data = self.load('1h', rows)
        data.add_tick(tick)
        self.assertEqual(data.candles['1h'][-1]['volume'], 210)
        self.assertEqual(data.candles['1h'][-1]['vwap'], 99.5)
        volume_rule = Condition.parse('1-hour Volume is greater than each of the previous 3 candles')
        self.assertTrue(volume_rule.evaluate(101, 99.5, data))

    def test_native_sdk_fields_take_precedence_over_legacy_aliases(self):
        data = self.load('1h', [(datetime(2026, 10, 28, 9, 15), 100)])
        data.add_tick({'exchange_timestamp': datetime(2026, 10, 28, 9, 20),
                       'last_price': 101, 'volume_traded': 150, 'volume': 9999,
                       'average_traded_price': 99.5, 'average_price': 88})
        self.assertEqual(data.candles['1h'][-1]['volume'], 150)
        self.assertEqual(data.candles['1h'][-1]['vwap'], 99.5)

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
