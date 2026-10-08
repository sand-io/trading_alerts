# MTF Alert

Standalone, alert-only implementation of the supplied Gold/Crude multi-timeframe strategy. It does
not import from, modify, or place orders through the existing `kite/` application.
Both applications use the shared root authentication helper and `.env`.

## Strategy

Two independent pipelines are evaluated using completed candles only:

| Pipeline | Direction | Momentum | Execution |
|---|---|---|---|
| `swing` | 4h EMA 9/50/200 + Stochastic | 1h EMA 9/50 + RSI | 15m HA EMA 9/18 + MACD |
| `fast` | 15m EMA 9/50/200 + Stochastic | 5m EMA 9/50 + RSI + VWAP | 3m HA EMA 9/18 + MACD + OBV EMA 9 |

Only Fast requires the 5m VWAP direction filter. BUY momentum requires RSI(14) above
60 and EMA 9 above EMA 50; its SMA(9) smoothing line is calculated for reference and does
not gate alerts. BUY requires a green Heikin-Ashi execution candle, with no minimum body
percentage. SELL uses the mirrored conditions (RSI below 40, EMA 9 below EMA 50, red HA,
fresh bearish EMA/OBV crossovers, negative falling MACD histogram, and price below VWAP).
This SELL definition is an owner-approved assumption; the PDF does not specify it.

The immediately preceding completed macro candle supplies the established-trend context.
A counter-trend Stochastic crossover from that trend waits for two consecutive closes beyond
EMA 9. Otherwise, a congestion candle uses its single-close rule: close beyond EMA 9 or
EMA 200 in the signal direction with the matching Stochastic crossover. The price itself
need not cross an EMA on that candle; an older trend does not remain active indefinitely.
A countertrend crossover is accepted only when its candle and the preceding completed
candle are both beyond EMA 9; a crossover is never carried into a later candle.
Entering the RSI 40–60 neutral zone clears the active bias and any pending countertrend
confirmation. A bias cannot reappear until a later
macro candle produces a new signal. Otherwise a bias expires exactly 4 hours or 15 minutes
after its macro candle closes. Alerts are deduplicated per symbol, pipeline, and execution candle.

Every alert includes an indicative entry at the raw execution candle close. BUY uses the
raw candle low as stop; SELL uses its high. The target is twice the entry-to-stop distance
in the trade direction (1:2 risk/reward).
These levels are calculated when the candle closes; they are not live fills or
automatically managed exits.

The PDF's Stochastic instructions conflict: it says `(14, 1, 3)`, then says smooth %K by
3 periods and set %D length to 1 so it matches %K. Identical %K and %D cannot cross.
The strategy owner confirmed the `(14, 1, 3)` interpretation: 14-period range, raw %K
(1-period smoothing), and %D as a 3-period average of %K. This keeps crossovers possible.

The PDF gives complete BUY execution conditions but not corresponding SELL details, so
mirrored SELL conditions are explicitly assumed with the owner's approval. The Fast path requires the
fresh 3m HA EMA and OBV/EMA crossovers on its execution candle; the PDF does not explicitly
define a different validity window for the OBV crossover. Keeping alerts-only instead of
market orders and using regular-candle entry/stop/2R levels
are explicit owner-approved choices or additions to the PDF.
Live scanning waits for raw timeframe candles due at a timestamp and processes late-arriving
execution candles in order, separately for Swing and Fast. It resumes unfinished same-day
scans after restart, instead of marking a slot complete before its data arrives.
If a due source candle never appears but a later one does, its scan is skipped with an error
instead of blocking every later scan. One monitor process per state file is enforced.
The event key is saved before log or desktop notification for at-most-once delivery: a crash
at that exact point can lose an alert, but cannot duplicate it after restart.

## Setup

Use Python 3.11 or newer from the parent `trading_alerts` directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The included `config.json` is the single runtime configuration file. Update that file when contracts
roll over or operational settings change.

Set the following credentials in the shared `trading_alerts/.env`:

```ini
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_ACCESS_TOKEN=
```

For automatic login and MTF startup, set your Kite developer app's
Redirect URL once to `http://127.0.0.1:8765/callback`, then run from `trading_alerts`:

```bash
.venv/bin/python -m kite_runtime mtf
```

Complete browser login and the app-code step when prompted. The command captures
the request token, saves the shared access token, and starts only MTF alerts.
A valid saved token skips login. Ctrl+C stops MTF. Use `--login` for a fresh
login, or `--manual-auth` if you keep a different developer-app redirect URL.
Stop any existing MTF monitor before starting the launcher.
To run Kite separately, use `.venv/bin/python -m kite_runtime kite` and open
`http://127.0.0.1:8000`. Both apps share the `kite_runtime/` authentication helper.

To force a fresh login and start MTF alerts:

```bash
.venv/bin/python -m kite_runtime mtf --login
```

Login captures the callback and saves `KITE_ACCESS_TOKEN` to the shared `.env`
without displaying it. Add `--manual-auth` to paste the redirect URL when using
a different developer-app redirect URL. Restart through the launcher whenever
the access token expires.

Keep the contracts in `config.json` updated with currently tradable exact Kite instruments;
commodity futures expire and cannot be hard-coded permanently. The example session is MCX
`09:00`–`23:30`; update `session_close` when the exchange's seasonal closing time changes.

Run:

```bash
python -m mtf_alert
```

Test without credentials or network access:

```bash
python -m unittest discover -s mtf_alert/tests -v
```

Backtest completed-candle alerts over historical data (the warm-up period is replayed but omitted
from the CSV):

```bash
python -m mtf_alert.backtest --from 2026-07-01 --to 2026-09-30 \
  --warmup-days 180 --data-dir mtf_alert/backtest_data \
  --output mtf_alert/backtest_alerts.csv
```

Use `--symbol MCX:GOLD26OCTFUT` one or more times to override the configured symbols. The backtest
warns when a contract has fewer than 202 completed 4h or 15m candles; extending the requested
warm-up period cannot create history for a newly listed or thinly traded contract. The replay
uses in-memory state and never reads or modifies the live `.state.json`. It reports strategy alerts;
the CSV also labels the first later 3m stop/target touch. If both levels are touched in one
bar, the outcome is `ambiguous`; if neither is touched, it is `open`. These are theoretical
level touches, not a P&L or fill simulation: position sizing, fees, liquidity, gaps and slippage
are not modeled. A run ending today is clamped to the actual current time, so a still-forming
Kite candle cannot enter the replay.
When `--data-dir` is supplied, the exact normalized candles and instrument metadata are retained for
independent inspection instead of being discarded after the replay.
Replay that same saved Kite dataset without a network request or access token using
`--input-data-dir mtf_alert/backtest_data`. Historical candles must pass timestamp,
OHLCV, session-completeness, and cross-timeframe aggregation checks before signals are trusted.
Swing is replayed on 15m closes and Fast on 3m closes; a missing 3m bar cannot hide a
valid 15m Swing execution candle.

## Operational notes

- Historical candles are polled because closed-bar evaluation is deterministic and avoids repainting.
- Only complete four-hour blocks are used; the shortened final MCX session block is excluded.
- An initial history load is followed by incremental updates to limit API traffic.
- Each instrument is scanned once per completed 3-minute slot, after a configurable publication delay,
  and only during its configured session.
- The generated `.state.json` preserves active biases and alert deduplication across restarts.
- Runtime settings, including `log_level`, belong in `config.json`; shared credentials belong in `trading_alerts/.env`.
- Set `desktop_notifications` to `true` for optional macOS notifications; logs remain the primary output.
- No API method capable of creating, modifying, or cancelling orders is called anywhere in this package.
