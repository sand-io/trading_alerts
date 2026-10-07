import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from conditions import Condition, load_conditions_from_file

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

def test_rules():
    print("=" * 80)
    print("RUNNING END-TO-END CONDITION PARSING & EVALUATION TEST")
    print("=" * 80)
    
    # 1. Create mock database
    df_5m = create_mock_df(200)
    df_15m = create_mock_df(200)
    df_1h = create_mock_df(200)
    df_1d = create_mock_df(200)
    
    symbol_data = MockSymbolData(df_5m, df_15m, df_1h, df_1d)
    
    # Let's check parse & evaluation
    files = {
        "Bullish": "bullish.txt",
        "Bearish": "bearish.txt"
    }
    
    has_errors = False
    
    for category, file_path in files.items():
        print(f"\n--- Testing {category} Rules from '{file_path}' ---")
        if not os.path.exists(file_path):
            print(f"[ERROR] File '{file_path}' not found!")
            has_errors = True
            continue
            
        with open(file_path, "r") as f:
            lines = [l.strip() for l in f if l.strip() and not l.strip().startswith(("#", "//"))]
            
        for i, raw_text in enumerate(lines, 1):
            print(f"Rule #{i}: \"{raw_text}\"")
            
            # Parse rule
            try:
                cond = Condition.parse(raw_text)
            except Exception as pe:
                print(f"  └─ ❌ PARSE ERROR: {pe}")
                has_errors = True
                continue
                
            if cond is None:
                print("  └─ ❌ PARSE FAILED (Returned None)")
                has_errors = True
                continue
                
            print(f"  └─ Parsed as: rule_type={cond.rule_type}, interval={cond.interval}, params={cond.params}")
            
            # Evaluate rule
            try:
                # LTP = 105.0, VWAP = 104.5
                result = cond.evaluate(105.0, 104.5, symbol_data)
                print(f"  └─ Evaluation Success: Result = {result}")
            except Exception as ee:
                print(f"  └─ ❌ EVALUATION ERROR: {ee}")
                has_errors = True
                
    print("\n" + "=" * 80)
    if has_errors:
        print("RESULT: ❌ Some conditions failed to parse or evaluate correctly.")
        sys.exit(1)
    else:
        print("RESULT: ✅ All conditions parsed and evaluated successfully without error!")
        sys.exit(0)

if __name__ == "__main__":
    test_rules()
