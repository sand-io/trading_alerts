import json
import unittest
from . import KITE_DIR
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from conditions import Condition

class MockSymbolData:
    def __init__(self, df_5m, df_15m, df_1h, df_1d):
        self.dfs = {
            '5m': df_5m,
            '15m': df_15m,
            '1h': df_1h,
            '1d': df_1d,
            '4h': df_1h  # scanner will resample 4h from 1h
        }

    def get_dataframe(self, interval):
        return self.dfs.get(interval, pd.DataFrame())

def create_mock_df(size=200):
    # Generates a realistic rising candle dataset to support technical indicators
    times = [datetime.now() - timedelta(minutes=5 * (size - i)) for i in range(size)]
    np.random.seed(42)
    closes = 100.0 + np.cumsum(np.random.randn(size) * 0.5 + 0.1)  # slightly upward bias
    opens = closes - (np.random.randn(size) * 0.2)
    highs = np.maximum(opens, closes) + np.random.rand(size) * 0.5
    lows = np.minimum(opens, closes) - np.random.rand(size) * 0.5
    volumes = np.random.randint(100, 1000, size=size)
    vwaps = closes + (np.random.randn(size) * 0.1)

    df = pd.DataFrame({
        'time': times,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes,
        'vwap': vwaps
    })
    return df

class AllRulesTests(unittest.TestCase):
    def test_default_rules_parse_and_evaluate(self):
        symbol_data = MockSymbolData(*(create_mock_df(200) for _ in range(4)))
        profile = json.loads((KITE_DIR / 'strategies' / 'default.json').read_text())
        for side in ('bullish', 'bearish'):
            for rule in profile[side]:
                with self.subTest(side=side, rule=rule):
                    condition = Condition.parse(rule)
                    self.assertIsNotNone(condition)
                    result = condition.evaluate(105.0, 104.5, symbol_data)
                    self.assertIsInstance(result, (bool, np.bool_))


if __name__ == '__main__':
    unittest.main()
