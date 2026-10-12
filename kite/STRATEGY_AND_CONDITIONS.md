# Kite strategies and conditions

## 1. Live multi-timeframe strategy

Named **Multi-Timeframe Trend Confluence**, stored in `strategies/default.json`
with internal ID `default`. The scanner validates each rule using `Condition.parse`
in `conditions.py`. The original text files have been removed.

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

Each side is scored independently:

```text
Passing percentage = passed conditions / 14 × 100
Alert when passing percentage >= 90%
```

The migrated 90% threshold requires at least 13 of 14 conditions.
The active JSON profile controls the threshold.
If both sides reach the threshold on the same evaluation, only the side with
the higher passing percentage becomes the directional signal. An exact tie is
treated as ambiguous and produces no directional signal. Both raw scores remain
visible on the dashboard.

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


## BTST Daily-Hourly Confluence

Profile: `strategies/btst_setup.json`. Source: the supplied `BTST SETUP.pdf`.
The PDF supplies the bullish setup. A user-requested bearish mirror is included
in the same profile. `threshold: 100` requires all seven rules on each side.

| PDF condition | Implementation |
| --- | --- |
| Daily trend | Daily close strictly above daily EMA(10) |
| Daily momentum | RSI(14) in [40,45), or RSI(14) >= 60 |
| Hourly trend | Hourly close strictly above hourly EMA(10) |
| Hourly momentum | MACD line strictly above signal line |
| Hourly volume trend | OBV strictly above EMA(11) of OBV |
| Hourly volume spike | Current volume strictly exceeds each of the previous three hourly volumes |
| Hourly price confirmation | Close strictly above Open |

The RSI condition uses the explicit formula supplied in the PDF's developer note.
It does not require a fresh RSI crossover. The RSI alternatives remain one rule.
The previous three volumes exclude the trigger candle; this is not an average-volume
comparison. Equality fails strict comparisons. MACD and OBV may already be above
their reference lines, as allowed by the PDF; fresh crossovers are not mandatory.

The PDF does not specify MACD periods. The user confirmed fast 12, slow 26,
signal 9. No extra rule for time of day, entries, exits or stops has been added. The name BTST alone does not add an end-of-day restriction.

Live evaluation uses the current/forming daily and hourly candles, including the
current hourly volume, as permitted by the PDF. These states can change before
close. The existing dashboard's confirmed markers still record at five-minute
rollover; they do not certify that the hourly candle has closed. Live charts retain the standard studies. Saved-session review records the BTST
EMA(10), hourly MACD/OBV/volume studies, and daily RSI band levels. The sidebar
rule results show the exact saved BTST evaluation.

Parser additions: compound RSI bands (`rsi_bands`), volume above every prior candle
(`volume_previous_max`), and Close versus Open (`close_open`). Existing rule
patterns and strategy files retain their behavior. Restart the app after this
parser upgrade, then select **BTST Daily-Hourly Confluence** and activate it.


### User-requested bearish mirror

The PDF has no bearish setup; these rules are the user's requested directional
mirror. Bullish rules remain unchanged.

- Daily close strictly below daily EMA(10).
- Daily RSI(14): **55 < RSI <= 60 OR RSI <= 40**. This is the exact reflection
  of the bullish formula under `bearish RSI = 100 - bullish RSI`, preserving
  strict and inclusive boundaries.
- Hourly close strictly below hourly EMA(10).
- Hourly MACD(12,26,9) strictly below its signal line; held below is sufficient.
- Hourly OBV strictly below its EMA(11); held below is sufficient.
- Hourly volume strictly above each of the previous three hourly volumes.
  Volume is a direction-neutral confirmation and stays elevated on both sides.
- Hourly Close strictly below Open (a red candle; doji fails).

The compound bearish RSI pattern is implemented as `rsi_bearish_bands`.
