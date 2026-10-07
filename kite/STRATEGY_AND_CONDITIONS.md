# Kite strategies and conditions

## 1. Live multi-timeframe strategy

Used by the Kite scanner and dashboard.

### Bullish conditions

1. 5-minute ADX (14) is greater than 22 and rising
2. 5-minute Close is greater than First 15-minute High (or Previous Day High)
3. 1-hour RSI (14) is greater than 55
4. 1-hour EMA (9) is above 1-hour EMA (50).
5. 15-minute MACD line (26,12,9) is above MACD signal line.
6. 15-minute EMA (5) of Heikin-Ashi Close is above EMA (9) of Heikin-Ashi Close.
7. 5-minute Close is above VWAP.
8. 5-minute EMA (20) is above 5-minute EMA (50).
9. 5-minute OBV is above its 5-minute EMA (5)
10. Last 10-candle High–Low range is less than 5 × ATR (14)
11. Absolute candle body size (Close − Open) is greater than 60% of the candle’s High–Low range
12. 1 day Close is above 1 day EMA (50)
13. 1 day Close is above 1 day EMA (9)
14. 1 day RSI (14) is greater than 55

### Bearish conditions

1. 5-minute ADX (14) is greater than 22 and rising
2. 5-minute Close is less than First 15-minute Low (or Previous Day Low)
3. 1-hour RSI (14) is less than 45
4. 1-hour EMA (9) is below 1-hour EMA (50).
5. 15-minute MACD line (26,12,9) is below MACD signal line.
6. 15-minute EMA (5) of Heikin-Ashi Close is below EMA (9) of Heikin-Ashi Close.
7. 5-minute Close is below VWAP.
8. 5-minute EMA (20) is below 5-minute EMA (50).
9. 5-minute OBV is below its 5-minute EMA (5)
10. Last 10-candle High–Low range is less than 5 × ATR (14)
11. Absolute candle body size (Open − Close) is greater than 55% of the candle’s High–Low range
12. 1 day Close is less than 1 day EMA (50)
13. 1 day Close is less than 1 day EMA (9)
14. 1 day RSI (14) is less than 45

### Combined alert condition

Each side is evaluated independently:

```text
Passing percentage = passed conditions / 14 × 100
Alert when passing percentage >= 30%
```

The current threshold requires at least 5 of 14 conditions, not all 14.
Both sides can qualify simultaneously.

### Condition meanings

- Comparisons are strict: equality does not pass.
- “Rising” means latest ADX is greater than the previous ADX.
- Opening-range and previous-day comparisons are alternatives joined by OR.
- Opening range uses the first three available 5-minute candles for the day.
- Previous day means the latest daily row dated before the current candle date.
- MACD text `(26,12,9)` is evaluated as fast 12, slow 26, signal 9.
- Heikin-Ashi close is `(Open + High + Low + Close) / 4`.
- Ten-candle range is `maximum High − minimum Low` across the latest ten 5-minute candles.
- Body size is absolute; no bullish/bearish candle-colour requirement is imposed. A zero High–Low range fails.
- Rules use current live/forming candles, including hourly and daily indicators.
- EMA, MACD and OBV comparisons require the stated relationship, not a new crossover.

## 2. Separate historical chart-pattern strategies

These are in the separate pattern scanner, not additional conditions in the live strategy.

### Shared detection thresholds

```text
A = latest ATR(14)
C = latest Close
N = number of historical candles

Tolerance = max(0.015, A / C)
Peak prominence = max(C × 0.03, A × 1.5)
Minimum peak distance = max(10, N // 20)
```

### Double Top — bearish

- At least two detected high peaks.
- Checks adjacent detected peak pairs, most recent first.
- `abs(Peak1 − Peak2) / max(Peak1, Peak2) <= Tolerance`.
- Neckline is the minimum Low from the first peak up to, but excluding, the second peak.
- Latest Close is below the neckline.

### Double Bottom — bullish

- At least two detected low troughs.
- Checks adjacent detected trough pairs, most recent first.
- `abs(Trough1 − Trough2) / max(Trough1, Trough2) <= Tolerance`.
- Neckline is the maximum High from the first trough up to, but excluding, the second trough.
- Latest Close is above the neckline.

### Head & Shoulders — bearish

- Pivot highs must be strictly higher than the three neighbouring highs on each side.
- Left shoulder occurs before the head; right shoulder occurs after the head.
- Head High is greater than each shoulder High plus `0.5 × ATR(14)`.
- `abs(Left shoulder High − Right shoulder High) / max(Shoulder Highs) <= Tolerance`.
- Neckline is the smaller of the minimum Lows between left shoulder/head and head/right shoulder; each slice excludes its ending peak.
- Latest Close is below the neckline for confirmation.
- Only confirmed patterns trigger an alert; forming patterns do not.
- No minimum pattern-score condition is required.

### Bullish Rectangle — bullish

- Uses the latest 20 candles.
- Resistance is their maximum High; support is their minimum Low.
- `(Resistance − Support) / Support < 0.05`.
- Latest Close is above resistance.

### Bearish Rectangle — bearish

- Uses the latest 20 candles.
- Resistance is their maximum High; support is their minimum Low.
- `(Resistance − Support) / Support < 0.05`.
- Latest Close is below support.

**Rectangle limitation:** the current calculations include the latest candle in the range. For valid OHLC data, these strict breakout conditions cannot normally pass.
