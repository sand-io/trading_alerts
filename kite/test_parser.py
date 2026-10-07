import unittest
import pandas as pd
from conditions import Condition, load_conditions_from_file

class MockSymbolData:
    def __init__(self, df_candles, df_daily=None):
        self.df = df_candles
        self.df_daily = df_daily if df_daily is not None else pd.DataFrame()

    def get_dataframe(self, interval):
        if interval == '1d':
            return self.df_daily
        return self.df

class TestConditionParser(unittest.TestCase):
    def test_basic_parsing(self):
        # 1. Test exact match of user's condition
        c = Condition.parse("5-minute Close is above VWAP")
        self.assertIsNotNone(c)
        self.assertEqual(c.interval, "5m")
        self.assertEqual(c.rule_type, "close_vwap")
        self.assertEqual(c.params['op'], ">")

        # 2. Test EMA relation
        c2 = Condition.parse("5-minute EMA (20) is above 5-minute EMA (50)")
        self.assertIsNotNone(c2)
        self.assertEqual(c2.interval, "5m")
        self.assertEqual(c2.rule_type, "ema_ema")
        self.assertEqual(c2.params['ema1'], 20)
        self.assertEqual(c2.params['ema2'], 50)
        self.assertEqual(c2.params['op'], ">")

        # 3. Test Heikin-Ashi Close EMA
        c3 = Condition.parse("15-minute EMA (5) of Heikin-Ashi Close is below EMA (9) of Heikin-Ashi Close")
        self.assertIsNotNone(c3)
        self.assertEqual(c3.interval, "15m")
        self.assertEqual(c3.rule_type, "ha_ema_ema")
        self.assertEqual(c3.params['ema1'], 5)
        self.assertEqual(c3.params['ema2'], 9)
        self.assertEqual(c3.params['op'], "<")

        # 4. Test Stochastic crossover
        c4 = Condition.parse("4-hour Fast Stochastic %K (14,3) is less than Slow Stochastic %D (14,3)")
        self.assertIsNotNone(c4)
        self.assertEqual(c4.interval, "4h")
        self.assertEqual(c4.rule_type, "stochastic_cross")
        self.assertEqual(c4.params['k_period'], 14)
        self.assertEqual(c4.params['k_smooth'], 3)
        self.assertEqual(c4.params['op'], "<")

        # 5. Test ignore comments
        self.assertIsNone(Condition.parse("# this is a comment"))
        self.assertIsNone(Condition.parse("// another comment"))
        self.assertIsNone(Condition.parse("   "))

    def test_evaluation_tick(self):
        c = Condition.parse("LTP is above 100")
        mock_sd = MockSymbolData(pd.DataFrame())
        self.assertTrue(c.evaluate(105, 95, mock_sd))
        self.assertFalse(c.evaluate(95, 95, mock_sd))

        c2 = Condition.parse("LTP is below VWAP")
        self.assertTrue(c2.evaluate(95, 100, mock_sd))
        self.assertFalse(c2.evaluate(105, 100, mock_sd))

    def test_evaluation_rsi(self):
        c = Condition.parse("5-minute RSI (14) is greater than 60")
        
        # Create a series of 20 candles
        data = {
            'time': pd.date_range('2026-07-11 11:00:00', periods=20, freq='5min'),
            'open': [100.0] * 20,
            'high': [102.0] * 20,
            'low': [98.0] * 20,
            'close': [100.0 + i for i in range(20)], # steadily rising close
            'volume': [1000] * 20
        }
        df = pd.DataFrame(data)
        mock_sd = MockSymbolData(df)
        
        # For a steadily rising series, RSI should be very high (>60)
        self.assertTrue(c.evaluate(120.0, 100.0, mock_sd))

    def test_image_strategy_rules_parse_exactly(self):
        expected_bullish = [
            ('adx', '5m', {'period': 14, 'value': 22.0, 'rising': True}),
            ('opening_or_previous_day', '5m', {'op': '>', 'opening_minutes': 15}),
            ('rsi', '1h', {'value': 55.0}),
            ('ema_ema', '1h', {'ema1': 9, 'ema2': 50}),
            ('macd', '15m', {'fast': 12, 'slow': 26, 'signal': 9}),
            ('ha_ema_ema', '15m', {'ema1': 5, 'ema2': 9}),
            ('close_vwap', '5m', {'op': '>'}),
            ('ema_ema', '5m', {'ema1': 20, 'ema2': 50}),
            ('obv_ema', '5m', {'ema_period': 5, 'op': '>'}),
            ('range_atr', '5m', {'candles': 10, 'multiplier': 5.0, 'atr_period': 14}),
            ('candle_body', '5m', {'pct': 0.60}),
            ('close_ema', '1d', {'ema_period': 50, 'op': '>'}),
            ('close_ema', '1d', {'ema_period': 9, 'op': '>'}),
            ('rsi', '1d', {'value': 55.0, 'op': '>'}),
        ]
        bullish = load_conditions_from_file('bullish.txt')
        self.assertEqual(len(bullish), len(expected_bullish))
        for condition, (rule_type, interval, params) in zip(bullish, expected_bullish):
            self.assertEqual(condition.rule_type, rule_type)
            self.assertEqual(condition.interval, interval)
            for key, value in params.items():
                self.assertEqual(condition.params[key], value)

        bearish = load_conditions_from_file('bearish.txt')
        self.assertEqual(len(bearish), 14)
        self.assertEqual(bearish[1].rule_type, 'opening_or_previous_day')
        self.assertEqual(bearish[1].params['op'], '<')
        self.assertEqual(bearish[8].rule_type, 'obv_ema')
        self.assertEqual(bearish[8].params['op'], '<')
        self.assertEqual(bearish[10].rule_type, 'candle_body')
        self.assertEqual(bearish[10].params['pct'], 0.55)
        self.assertEqual(bearish[13].params['value'], 45.0)

    def test_opening_range_or_previous_day_semantics(self):
        intraday = pd.DataFrame({
            'time': pd.date_range('2026-10-07 09:15:00', periods=4, freq='5min'),
            'open': [100, 101, 102, 103],
            'high': [102, 103, 104, 106],
            'low': [99, 100, 101, 102],
            'close': [101, 102, 103, 105],
            'volume': [1000] * 4,
            'vwap': [101] * 4,
        })
        daily = pd.DataFrame({
            'time': pd.to_datetime(['2026-10-06']),
            'open': [100], 'high': [110], 'low': [90], 'close': [105],
            'volume': [10000], 'vwap': [102],
        })
        mock_sd = MockSymbolData(intraday, daily)
        bullish = Condition.parse(
            '5-minute Close is greater than First 15-minute High (or Previous Day High)'
        )
        # 105 breaks the opening high (104), even though it does not break PDH (110).
        self.assertTrue(bullish.evaluate(105, 101, mock_sd))
        self.assertFalse(bullish.evaluate(103, 101, mock_sd))

if __name__ == "__main__":
    unittest.main()
