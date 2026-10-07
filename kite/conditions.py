import re
import os
from datetime import datetime
import pandas as pd
import numpy as np
# pyrefly: ignore [missing-import]
import talib

# Technical analysis indicators using TA-Lib
def calculate_rsi(series, period=14):
    if len(series) < period:
        return pd.Series(np.nan, index=series.index)
    rsi_vals = talib.RSI(series.astype(float).values, timeperiod=period)
    return pd.Series(rsi_vals, index=series.index)

def calculate_adx(df, period=14):
    if len(df) < period:
        return pd.Series(np.nan, index=df.index)
    adx_vals = talib.ADX(
        df['high'].astype(float).values,
        df['low'].astype(float).values,
        df['close'].astype(float).values,
        timeperiod=period
    )
    return pd.Series(adx_vals, index=df.index)

def calculate_atr(df, period=14):
    if len(df) < period:
        return pd.Series(np.nan, index=df.index)
    atr_vals = talib.ATR(
        df['high'].astype(float).values,
        df['low'].astype(float).values,
        df['close'].astype(float).values,
        timeperiod=period
    )
    return pd.Series(atr_vals, index=df.index)

def calculate_obv(df):
    if df.empty:
        return pd.Series(np.nan, index=df.index)
    obv_vals = talib.OBV(
        df['close'].astype(float).values,
        df['volume'].astype(float).values
    )
    return pd.Series(obv_vals, index=df.index)

def calculate_stoch(df, k_period=14, k_smooth=3, d_period=3):
    if len(df) < k_period:
        return pd.Series(np.nan, index=df.index), pd.Series(np.nan, index=df.index)
    slowk, slowd = talib.STOCH(
        df['high'].astype(float).values,
        df['low'].astype(float).values,
        df['close'].astype(float).values,
        fastk_period=k_period,
        slowk_period=k_smooth,
        slowk_matype=0,
        slowd_period=d_period,
        slowd_matype=0
    )
    return pd.Series(slowk, index=df.index), pd.Series(slowd, index=df.index)

def calculate_macd(df, fast=12, slow=26, signal=9):
    if len(df) < slow:
        return pd.Series(np.nan, index=df.index), pd.Series(np.nan, index=df.index)
    macd_line, macd_signal, macd_hist = talib.MACD(
        df['close'].astype(float).values,
        fastperiod=fast,
        slowperiod=slow,
        signalperiod=signal
    )
    return pd.Series(macd_line, index=df.index), pd.Series(macd_signal, index=df.index)

def calculate_ema(series, period):
    if len(series) < period:
        return pd.Series(np.nan, index=series.index)
    ema_vals = talib.EMA(series.astype(float).values, timeperiod=period)
    return pd.Series(ema_vals, index=series.index)

def calculate_heikin_ashi(df):
    if df.empty:
        return df
    ha_df = pd.DataFrame(index=df.index)
    ha_df['close'] = (df['open'] + df['high'] + df['low'] + df['close']) / 4.0
    
    ha_open = np.zeros(len(df))
    ha_open[0] = (df['open'].iloc[0] + df['close'].iloc[0]) / 2.0
    for i in range(1, len(df)):
        ha_open[i] = (ha_open[i-1] + ha_df['close'].iloc[i-1]) / 2.0
    ha_df['open'] = ha_open
    
    ha_df['high'] = np.maximum(df['high'], np.maximum(ha_df['open'], ha_df['close']))
    ha_df['low'] = np.minimum(df['low'], np.minimum(ha_df['open'], ha_df['close']))
    ha_df['volume'] = df['volume']
    if 'vwap' in df.columns:
        ha_df['vwap'] = df['vwap']
    return ha_df

class Condition:
    def __init__(self, raw_text, interval, rule_type, params):
        self.raw_text = raw_text
        self.interval = interval    # e.g., '5m', '1h', '4h', '1d', 'tick'
        self.rule_type = rule_type  # 'adx', 'stochastic', 'rsi', 'macd', etc.
        self.params = params        # dict of parameters specific to the rule

    def __repr__(self):
        return f"Condition(interval={self.interval}, rule_type={self.rule_type}, params={self.params}, raw='{self.raw_text}')"

    @classmethod
    def parse(cls, text):
        text = text.strip()
        if not text or text.startswith("#") or text.startswith("//"):
            return None
        
        # Remove trailing period if present
        if text.endswith("."):
            text = text[:-1].strip()

        norm = " ".join(text.lower().split())
        
        # Parse interval first
        interval = 'tick'
        interval_match = re.search(r'\b(\d+)\s*-?\s*(?:minute|min|m|hour|h|day|d)\b', norm)
        if interval_match:
            val = interval_match.group(1)
            unit = interval_match.group(0)
            if 'min' in unit or 'm' in unit:
                interval = f"{val}m"
            elif 'hour' in unit or 'h' in unit:
                interval = f"{val}h"
            elif 'day' in unit or 'd' in unit:
                interval = f"{val}d"
        elif 'daily' in norm:
            interval = '1d'

        # 1. ADX, optionally requiring the latest ADX value to be rising.
        m = re.search(r'\badx\s*\(?(\d+)\)?\s+is\s+(greater than|less than|above|below|>|<)\s+(\d+(?:\.\d+)?)(?:\s+and\s+rising)?', norm)
        if m:
            op = '>' if m.group(2) in ['greater than', 'above', '>'] else '<'
            return cls(text, interval, 'adx', {
                'period': int(m.group(1)),
                'op': op,
                'value': float(m.group(3)),
                'rising': bool(re.search(r'\band\s+rising\b', norm))
            })

        # 2. Opening-range/previous-day breakout. The parenthesized alternative is
        # deliberately one condition so its OR semantics cannot be changed by the
        # scanner's percentage threshold.
        m = re.search(
            r'\bclose\s+is\s+(greater than|less than|above|below|>|<)\s+(?:the\s+)?first\s+' 
            r'(\d+)\s*-?\s*(?:minute|min|m)\s+(high|low)\s*\(\s*or\s+(?:the\s+)?previous\s+day\s+(high|low)\s*\)',
            norm
        )
        if m:
            op = '>' if m.group(1) in ['greater than', 'above', '>'] else '<'
            return cls(text, interval, 'opening_or_previous_day', {
                'op': op,
                'opening_minutes': int(m.group(2)),
                'opening_field': m.group(3),
                'previous_day_field': m.group(4)
            })

        # 3. OBV relative to an EMA of OBV.
        m = re.search(
            r'\bobv\s+is\s+(above|below|greater than|less than|>|<)\s+(?:its\s+)?(?:\d+\s*-?\s*(?:minute|min|m)\s+)?ema\s*\((\d+)\)',
            norm
        )
        if m:
            op = '>' if m.group(1) in ['above', 'greater than', '>'] else '<'
            return cls(text, interval, 'obv_ema', {
                'op': op,
                'ema_period': int(m.group(2))
            })

        # 4. Range across the latest N candles relative to current ATR.
        m = re.search(
            r'\blast\s+(\d+)\s*-?\s*candle\s+high\s*[-–—]\s*low\s+range\s+is\s+' 
            r'(less than|greater than|below|above|<|>)\s+(\d+(?:\.\d+)?)\s*[x×]\s*atr\s*\(?(\d+)\)?',
            norm
        )
        if m:
            op = '<' if m.group(2) in ['less than', 'below', '<'] else '>'
            return cls(text, '5m' if interval == 'tick' else interval, 'range_atr', {
                'candles': int(m.group(1)),
                'op': op,
                'multiplier': float(m.group(3)),
                'atr_period': int(m.group(4))
            })

        # 5. First candle comparison: "Current 5-minute Low is lower than the first 5-minute Low."
        m = re.search(r'\b(low|high)\s+is\s+(lower than|higher than)\s+(?:the\s+)?first\s+\d+-?(?:minute|min|m)\s+(low|high)', norm)
        if m:
            field = m.group(1)
            op = '<' if 'lower' in m.group(2) else '>'
            return cls(text, interval, 'first_candle', {
                'field': field,
                'op': op
            })

        # 3. Lowest/Highest of last N candles:
        # "5-minute Close is less than or equal to the lowest Low of the last 18 candles."
        m = re.search(r'\b(close|price|ltp)\s+is\s+(less than or equal to|greater than or equal to|below|above|<=|>=|<|>)\s+(?:the\s+)?(lowest low|highest high)\s+of\s+(?:the\s+)?last\s+(\d+)\s+candles', norm)
        if m:
            field = 'close'
            op_str = m.group(2)
            op = '<=' if 'less' in op_str or '<=' in op_str else '>='
            ref = 'lowest_low' if 'lowest' in m.group(3) else 'highest_high'
            return cls(text, interval, 'rolling_extreme', {
                'field': field,
                'op': op,
                'ref': ref,
                'period': int(m.group(4))
            })

        # 4. Yesterday's compare:
        # "5 min close is less than yesterday's low" or "5 min close is greater than yesterday's high"
        m = re.search(r'\b(close|price|ltp)\s+is\s+(less than|greater than|below|above|<|>)\s+yesterday\'s\s+(high|low)', norm)
        if m:
            op = '<' if m.group(2) in ['less than', 'below', '<'] else '>'
            ref_field = m.group(3)
            return cls(text, interval, 'yesterday_compare', {
                'op': op,
                'ref_field': ref_field
            })

        # 5. Stochastic:
        # "4-hour Fast Stochastic %K (14,3) is less than Slow Stochastic %D (14,3)."
        m = re.search(r'\bfast\s+stochastic\s+%k\s*\((\d+),(\d+)\)\s+is\s+(less than|greater than|below|above|<|>)\s+slow\s+stochastic\s+%d\s*\((\d+),(\d+)\)', norm)
        if m:
            op = '<' if m.group(3) in ['less than', 'below', '<'] else '>'
            return cls(text, interval, 'stochastic_cross', {
                'k_period': int(m.group(1)),
                'k_smooth': int(m.group(2)),
                'op': op,
                'd_period': int(m.group(4)),
                'd_smooth': int(m.group(5))
            })

        # 6. Heikin-Ashi EMA relation:
        # "15-minute EMA (5) of Heikin-Ashi Close is below EMA (9) of Heikin-Ashi Close."
        m = re.search(r'\bema\s*\((\d+)\)\s+of\s+heikin-ashi\s+close\s+is\s+(below|above|less than|greater than|<|>)\s+ema\s*\((\d+)\)\s+of\s+heikin-ashi\s+close', norm)
        if m:
            op = '<' if m.group(2) in ['below', 'less than', '<'] else '>'
            return cls(text, interval, 'ha_ema_ema', {
                'ema1': int(m.group(1)),
                'ema2': int(m.group(3)),
                'op': op
            })

        # 7. Previous Close relative to EMA:
        # "Previous 4-hour Close was also below 4-hour EMA (9)."
        m = re.search(r'\bprevious\s+.*close\s+was\s+(?:also\s+)?(below|above)\s+.*ema\s*\((\d+)\)', norm)
        if m:
            op = '<' if m.group(1) == 'below' else '>'
            return cls(text, interval, 'prev_close_ema', {
                'op': op,
                'ema_period': int(m.group(2))
            })

        # 8. Close relative to EMA:
        # "Current 4-hour Close is below 4-hour EMA (9)." or "4-hour Close is below 4-hour EMA (50)."
        m = re.search(r'\bclose\s+is\s+(below|above|less than|greater than|<|>)\s+.*ema\s*\((\d+)\)', norm)
        if m:
            op = '<' if m.group(1) in ['below', 'less than', '<'] else '>'
            return cls(text, interval, 'close_ema', {
                'op': op,
                'ema_period': int(m.group(2))
            })

        # 9. RSI:
        # "1-hour RSI (14) is less than 40."
        m = re.search(r'\brsi\s*\(?(\d+)\)?\s+is\s+(less than|greater than|below|above|>|<)\s+(\d+(?:\.\d+)?)', norm)
        if m:
            op = '>' if m.group(2) in ['greater than', 'above', '>'] else '<'
            return cls(text, interval, 'rsi', {
                'period': int(m.group(1)),
                'op': op,
                'value': float(m.group(3))
            })

        # 10. EMA relation (EMA crossover/position):
        # "1-hour EMA (9) is below 1-hour EMA (50)."
        m = re.search(r'\bema\s*\((\d+)\)\s+is\s+(below|above|less than|greater than|<|>)\s+.*ema\s*\((\d+)\)', norm)
        if m:
            op = '<' if m.group(2) in ['below', 'less than', '<'] else '>'
            return cls(text, interval, 'ema_ema', {
                'ema1': int(m.group(1)),
                'ema2': int(m.group(3)),
                'op': op
            })

        # 11. MACD relation:
        # "15-minute MACD line (26,12,9) is below MACD signal line."
        m = re.search(r'\bmacd\s+line\s*\((\d+),(\d+),(\d+)\)\s+is\s+(below|above|less than|greater than|<|>)\s+macd\s+signal\s+line', norm)
        if m:
            p1 = int(m.group(1))
            p2 = int(m.group(2))
            fast = min(p1, p2)
            slow = max(p1, p2)
            signal = int(m.group(3))
            op = '<' if m.group(4) in ['below', 'less than', '<'] else '>'
            return cls(text, interval, 'macd', {
                'fast': fast,
                'slow': slow,
                'signal': signal,
                'op': op
            })

        # 12. Close relative to VWAP:
        # "5-minute Close is below VWAP."
        m = re.search(r'\bclose\s+is\s+(below|above|less than|greater than|<|>)\s+vwap', norm)
        if m:
            op = '<' if m.group(1) in ['below', 'less than', '<'] else '>'
            return cls(text, interval, 'close_vwap', {
                'op': op
            })

        # 13. Absolute candle body size:
        # "Absolute candle body size (|Open − Close|) is greater than 55% of the candle’s High–Low range."
        m = re.search(r'\babsolute\s+candle\s+body\s+size\s*\(.*(?:open.*close|close.*open).*\)\s+is\s+(greater|less)\s+than\s+(\d+(?:\.\d+)?)\%\s+of\s+(?:the\s+)?candle.*high.*low', norm)
        if m:
            op = '>' if m.group(1) == 'greater' else '<'
            return cls(text, '5m' if interval == 'tick' else interval, 'candle_body', {
                'pct': float(m.group(2)) / 100.0,
                'op': op
            })

        # 14. Volume average:
        # "current 5-minute volume is greater than average volume of last 20 candles"
        m = re.search(r'\bvolume\s+is\s+(greater|less)\s+than\s+average\s+volume\s+of\s+last\s+(\d+)\s+candles', norm)
        if m:
            op = '>' if m.group(1) == 'greater' else '<'
            return cls(text, interval, 'volume_avg', {
                'period': int(m.group(2)),
                'op': op
            })

        # 15. Multi-day close comparison:
        # "5 min close greater than last 4 days close"
        m = re.search(r'\bclose\s+(?:is\s+)?(greater|less)\s+than\s+last\s+(\d+)\s+days\s+close', norm)
        if m:
            op = '>' if m.group(1) == 'greater' else '<'
            return cls(text, interval, 'multi_day_close', {
                'period': int(m.group(2)),
                'op': op
            })

        # 16. Yesterday's high/low (alternate format):
        # "5 min close is greater than yesterday's high"
        m = re.search(r'\bclose\s+is\s+(greater|less|above|below)\s+than\s+yesterday\'s\s+(high|low)', norm)
        if m:
            op = '>' if m.group(1) in ['greater', 'above'] else '<'
            ref_field = m.group(2)
            return cls(text, interval, 'yesterday_compare', {
                'op': op,
                'ref_field': ref_field
            })

        # Generic parser fallback
        return cls._parse_generic(text, norm, interval)

    @classmethod
    def _parse_generic(cls, text, norm, interval):
        operator_map = {
            "crosses above": "crosses_above",
            "crosses over": "crosses_above",
            "crosses below": "crosses_below",
            "crosses under": "crosses_below",
            "is above": ">",
            "greater than": ">",
            "above": ">",
            ">": ">",
            "is below": "<",
            "less than": "<",
            "below": "<",
            "<": "<",
            "is equal to": "==",
            "equal to": "==",
            "==": "=="
        }

        found_op = None
        found_op_raw = None
        for op_raw, op_code in operator_map.items():
            pattern = rf"\b{re.escape(op_raw)}\b" if op_raw.replace(" ", "").isalpha() else re.escape(op_raw)
            if re.search(pattern, norm):
                found_op = op_code
                found_op_raw = op_raw
                break

        if not found_op:
            return None

        parts = re.split(rf"\b{re.escape(found_op_raw)}\b" if found_op_raw.replace(" ", "").isalpha() else re.escape(found_op_raw), norm, maxsplit=1)
        if len(parts) != 2:
            return None

        left_str = parts[0].strip()
        right_str = parts[1].strip()

        left = "close"
        if "open" in left_str:
            left = "open"
        elif "high" in left_str:
            left = "high"
        elif "low" in left_str:
            left = "low"
        elif "volume" in left_str:
            left = "volume"

        right = None
        try:
            right = float(right_str)
        except ValueError:
            if "vwap" in right_str:
                right = "vwap"
            else:
                ema_match = re.match(r'\bema\s*\(?\s*(\d+)\s*\)?', right_str)
                sma_match = re.match(r'\bsma\s*\(?\s*(\d+)\s*\)?', right_str)
                if ema_match:
                    right = f"EMA({ema_match.group(1)})"
                elif sma_match:
                    right = f"SMA({sma_match.group(1)})"
                else:
                    right = right_str.upper()

        return cls(text, interval, 'generic', {
            'left': left,
            'op': found_op,
            'right': right
        })

    def evaluate(self, ltp, vwap, symbol_data):
        if symbol_data is None:
            return False

        df = symbol_data.get_dataframe(self.interval)
        
        # If interval is tick or df is empty, evaluate basic mathematical logic on ticks
        if self.interval == 'tick' or df.empty:
            if self.rule_type == 'close_vwap':
                return ltp > vwap if self.params['op'] == '>' else ltp < vwap
            elif self.rule_type == 'generic':
                left = self.params['left']
                op = self.params['op']
                right = self.params['right']
                
                val_left = ltp # for tick/empty candles, left side defaults to LTP
                if isinstance(right, float):
                    val_right = right
                elif right == 'vwap':
                    val_right = vwap
                else:
                    return False
                    
                if op == '>': return val_left > val_right
                if op == '<': return val_left < val_right
                if op == '==': return abs(val_left - val_right) < 1e-5
            return False

        # Make copy and update the last row with real-time live price (LTP) and tick VWAP
        df = df.copy()
        latest_idx = df.index[-1]
        df.loc[latest_idx, 'close'] = ltp
        if ltp > df.loc[latest_idx, 'high']:
            df.loc[latest_idx, 'high'] = ltp
        if ltp < df.loc[latest_idx, 'low']:
            df.loc[latest_idx, 'low'] = ltp
        df.loc[latest_idx, 'vwap'] = vwap

        # Route to logic
        if self.rule_type == 'adx':
            period = self.params['period']
            op = self.params['op']
            val = self.params['value']
            
            adx_series = calculate_adx(df, period)
            if len(adx_series) < 2 or pd.isna(adx_series.iloc[-1]):
                return False
            threshold_passed = adx_series.iloc[-1] > val if op == '>' else adx_series.iloc[-1] < val
            rising_passed = (
                not self.params.get('rising', False)
                or (not pd.isna(adx_series.iloc[-2]) and adx_series.iloc[-1] > adx_series.iloc[-2])
            )
            return threshold_passed and rising_passed

        elif self.rule_type == 'opening_or_previous_day':
            op = self.params['op']
            opening_minutes = self.params['opening_minutes']
            opening_field = self.params['opening_field']
            previous_day_field = self.params['previous_day_field']

            times = pd.to_datetime(df['time'])
            current_date = times.iloc[-1].date()
            df_today = df[times.dt.date == current_date].sort_values('time')
            # The strategy uses 5-minute data, so the first 15-minute range is
            # composed from the first three completed/available 5-minute bars.
            bars_needed = max(1, opening_minutes // 5)
            opening_passed = False
            if len(df_today) >= bars_needed:
                opening_bars = df_today.iloc[:bars_needed]
                opening_value = (
                    opening_bars['high'].max()
                    if opening_field == 'high'
                    else opening_bars['low'].min()
                )
                opening_passed = ltp > opening_value if op == '>' else ltp < opening_value

            daily = symbol_data.get_dataframe('1d')
            previous_day_passed = False
            if not daily.empty and 'time' in daily.columns:
                daily_times = pd.to_datetime(daily['time'])
                completed_daily = daily[daily_times.dt.date < current_date]
                if not completed_daily.empty:
                    previous_value = completed_daily.iloc[-1][previous_day_field]
                    previous_day_passed = ltp > previous_value if op == '>' else ltp < previous_value

            return opening_passed or previous_day_passed

        elif self.rule_type == 'obv_ema':
            period = self.params['ema_period']
            if len(df) < period:
                return False
            obv = calculate_obv(df)
            obv_ema = calculate_ema(obv, period)
            if pd.isna(obv.iloc[-1]) or pd.isna(obv_ema.iloc[-1]):
                return False
            return obv.iloc[-1] > obv_ema.iloc[-1] if self.params['op'] == '>' else obv.iloc[-1] < obv_ema.iloc[-1]

        elif self.rule_type == 'range_atr':
            candles = self.params['candles']
            atr_period = self.params['atr_period']
            if len(df) < max(candles, atr_period + 1):
                return False
            recent = df.iloc[-candles:]
            price_range = recent['high'].max() - recent['low'].min()
            atr = calculate_atr(df, atr_period).iloc[-1]
            if pd.isna(atr):
                return False
            limit = self.params['multiplier'] * atr
            return price_range < limit if self.params['op'] == '<' else price_range > limit

        elif self.rule_type == 'first_candle':
            field = self.params['field']
            op = self.params['op']
            
            today = datetime.now().date()
            df_today = df[pd.to_datetime(df['time']).dt.date == today]
            if df_today.empty:
                return False
            
            first_candle = df_today.iloc[0]
            val_curr = ltp if field == 'close' else df_today.iloc[-1][field]
            val_first = first_candle[field]
            
            return val_curr > val_first if op == '>' else val_curr < val_first

        elif self.rule_type == 'rolling_extreme':
            field = self.params['field']
            op = self.params['op']
            ref = self.params['ref']
            period = self.params['period']
            
            if len(df) < period + 1:
                return False
                
            hist_df = df.iloc[:-1] # exclude current candle
            if ref == 'lowest_low':
                limit_val = hist_df['low'].rolling(window=period).min().iloc[-1]
                val_curr = ltp if field == 'close' else df.iloc[-1][field]
                return val_curr <= limit_val if op == '<=' else val_curr >= limit_val
            else:
                limit_val = hist_df['high'].rolling(window=period).max().iloc[-1]
                val_curr = ltp if field == 'close' else df.iloc[-1][field]
                return val_curr >= limit_val if op == '>=' else val_curr <= limit_val

        elif self.rule_type == 'yesterday_compare':
            op = self.params['op']
            ref_field = self.params['ref_field']
            
            df_daily = symbol_data.get_dataframe('1d')
            if df_daily.empty:
                return False
                
            today = datetime.now().date()
            hist_daily = df_daily[pd.to_datetime(df_daily['time']).dt.date < today]
            if hist_daily.empty:
                return False
                
            yesterday_val = hist_daily.iloc[-1][ref_field]
            return ltp > yesterday_val if op == '>' else ltp < yesterday_val

        elif self.rule_type == 'stochastic_cross':
            k_period = self.params['k_period']
            k_smooth = self.params['k_smooth']
            op = self.params['op']
            d_smooth = self.params['d_smooth']
            
            k_series, d_series = calculate_stoch(df, k_period, k_smooth, d_smooth)
            if k_series.empty or pd.isna(k_series.iloc[-1]) or pd.isna(d_series.iloc[-1]):
                return False
                
            return k_series.iloc[-1] > d_series.iloc[-1] if op == '>' else k_series.iloc[-1] < d_series.iloc[-1]

        elif self.rule_type == 'prev_close_ema':
            op = self.params['op']
            ema_period = self.params['ema_period']
            
            if len(df) < ema_period + 1:
                return False
                
            ema_series = calculate_ema(df['close'], ema_period)
            val_prev = df['close'].iloc[-2]
            ema_prev = ema_series.iloc[-2]
            
            return val_prev > ema_prev if op == '>' else val_prev < ema_prev

        elif self.rule_type == 'close_ema':
            op = self.params['op']
            ema_period = self.params['ema_period']
            
            if len(df) < ema_period:
                return False
                
            ema_series = calculate_ema(df['close'], ema_period)
            return ltp > ema_series.iloc[-1] if op == '>' else ltp < ema_series.iloc[-1]

        elif self.rule_type == 'rsi':
            period = self.params['period']
            op = self.params['op']
            val = self.params['value']
            
            rsi_series = calculate_rsi(df['close'], period)
            if rsi_series.empty or pd.isna(rsi_series.iloc[-1]):
                return False
            return rsi_series.iloc[-1] > val if op == '>' else rsi_series.iloc[-1] < val

        elif self.rule_type == 'ema_ema':
            ema1 = self.params['ema1']
            ema2 = self.params['ema2']
            op = self.params['op']
            
            max_ema = max(ema1, ema2)
            if len(df) < max_ema:
                return False
                
            ema1_series = calculate_ema(df['close'], ema1)
            ema2_series = calculate_ema(df['close'], ema2)
            
            return ema1_series.iloc[-1] > ema2_series.iloc[-1] if op == '>' else ema1_series.iloc[-1] < ema2_series.iloc[-1]

        elif self.rule_type == 'macd':
            fast = self.params['fast']
            slow = self.params['slow']
            signal = self.params['signal']
            op = self.params['op']
            
            macd_series, signal_series = calculate_macd(df, fast, slow, signal)
            if macd_series.empty or pd.isna(macd_series.iloc[-1]) or pd.isna(signal_series.iloc[-1]):
                return False
            return macd_series.iloc[-1] > signal_series.iloc[-1] if op == '>' else macd_series.iloc[-1] < signal_series.iloc[-1]

        elif self.rule_type == 'ha_ema_ema':
            ema1 = self.params['ema1']
            ema2 = self.params['ema2']
            op = self.params['op']
            
            ha_df = calculate_heikin_ashi(df)
            max_ema = max(ema1, ema2)
            if len(ha_df) < max_ema:
                return False
                
            ema1_series = calculate_ema(ha_df['close'], ema1)
            ema2_series = calculate_ema(ha_df['close'], ema2)
            
            return ema1_series.iloc[-1] > ema2_series.iloc[-1] if op == '>' else ema1_series.iloc[-1] < ema2_series.iloc[-1]

        elif self.rule_type == 'close_vwap':
            op = self.params['op']
            return ltp > vwap if op == '>' else ltp < vwap

        elif self.rule_type == 'candle_body':
            pct = self.params['pct']
            op = self.params['op']
            
            current_candle = df.iloc[-1]
            body_size = abs(current_candle['open'] - ltp)
            hl_range = current_candle['high'] - current_candle['low']
            
            if hl_range == 0:
                return False
                
            body_ratio = body_size / hl_range
            return body_ratio > pct if op == '>' else body_ratio < pct

        elif self.rule_type == 'volume_avg':
            period = self.params['period']
            op = self.params['op']
            
            if len(df) < period + 1:
                return False
                
            avg_vol = df['volume'].iloc[:-1].rolling(window=period).mean().iloc[-1]
            curr_vol = df['volume'].iloc[-1]
            return curr_vol > avg_vol if op == '>' else curr_vol < avg_vol

        elif self.rule_type == 'multi_day_close':
            period = self.params['period']
            op = self.params['op']
            
            df_daily = symbol_data.get_dataframe('1d')
            if df_daily.empty:
                return False
                
            today = datetime.now().date()
            hist_daily = df_daily[pd.to_datetime(df_daily['time']).dt.date < today]
            if len(hist_daily) < period:
                return False
                
            last_n_closes = hist_daily['close'].iloc[-period:]
            return ltp > last_n_closes.max() if op == '>' else ltp < last_n_closes.min()

        elif self.rule_type == 'generic':
            left = self.params['left']
            op = self.params['op']
            right = self.params['right']
            
            val_left = ltp if left == 'close' else df.iloc[-1][left]
            
            if isinstance(right, float):
                val_right = right
            elif right == 'vwap':
                val_right = vwap
            elif isinstance(right, str) and (right.startswith("EMA") or right.startswith("SMA")):
                period = int(re.search(r'\d+', right).group())
                if len(df) < period:
                    return False
                if right.startswith("EMA"):
                    val_right = calculate_ema(df['close'], period).iloc[-1]
                else:
                    val_right = df['close'].rolling(window=period).mean().iloc[-1]
            else:
                return False
                
            if op == '>':
                return val_left > val_right
            elif op == '<':
                return val_left < val_right
            elif op == '==':
                return abs(val_left - val_right) < 1e-5

        return False

def load_conditions_from_file(filename):
    conditions = []
    if not os.path.exists(filename):
        return conditions
    with open(filename, "r") as f:
        for line in f:
            line_str = line.strip()
            if not line_str or line_str.startswith("#") or line_str.startswith("//"):
                continue
            cond = Condition.parse(line_str)
            if cond:
                conditions.append(cond)
            else:
                print(f"[WARNING] Could not parse condition: '{line_str}'")
    return conditions
