# Trading Alert System

Dynamic rule-based alert system using TrueData live feed.

## Setup

From the parent `trading_alerts` directory:

```bash
pip install -r requirements.txt
```

Set the TrueData credentials in the shared `trading_alerts/.env` file:

```ini
USERNAME=your_truedata_username
PASSWORD=your_truedata_password
```

Place your rule files in the same folder:
- `bullish.txt`
- `bearish.txt`

## Run

```bash
python main.py
```

## File Structure

```
trading_alert_system/
├── main.py          # Entry point — run this
├── rule_parser.py   # Converts .txt rules into Rule objects
├── indicators.py    # Computes ADX, RSI, EMA, MACD, VWAP etc.
├── evaluator.py     # Evaluates rules against live indicator data
├── alerts.py        # Triggers alerts + macOS desktop notifications
├── bullish.txt      # Your bullish conditions (edit freely)
└── bearish.txt      # Your bearish conditions (edit freely)
```

## How it works

```
bullish.txt / bearish.txt
        ↓ rule_parser.py  (plain English → Rule objects)
        ↓ indicators.py   (OHLCV → ADX, EMA, RSI, MACD ...)
        ↓ evaluator.py    (Rule vs Indicator → pass/fail)
        ↓ alerts.py       (score ≥ threshold → ALERT 🚀)
```

## Updating Rules

Just edit `bullish.txt` or `bearish.txt` — **no restart needed**.
Rules are re-parsed on every scan cycle automatically.

## Config (in main.py)

| Setting | Default | Description |
|---|---|---|
| `SYMBOLS` | `['NIFTY-I', 'RELIANCE-I']` | Symbols to watch |
| `SCAN_INTERVAL_SECONDS` | `30` | How often to scan |
| `ALERT_THRESHOLD_PCT` | `70` | % rules needed to trigger alert |
| `COOLDOWN_SECONDS` | `300` | Min gap between same alerts |

## Alert Output

```
============================================================
  [2026-03-10 09:32:00]  NIFTY-I
  🟢 BULLISH: 17/22 rules = 77.3%
  🔴 BEARISH: 4/22 rules = 18.2%

  🚀 ALERT: BULLISH SIGNAL on NIFTY-I!
  17/22 conditions met (77.3%)

  Passed conditions:
    ✅ 5-minute ADX (14) is greater than 30
    ✅ 5-minute Close is above VWAP
    ...

  Failed conditions:
    ❌ 1-hour RSI (14) is greater than 60  [54.3 gt 60 → ❌]
    ...
```
