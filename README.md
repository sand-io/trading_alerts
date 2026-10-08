# Trading alerts

The repository contains two Kite Connect applications and a separate TrueData
application. Provider-specific authentication and startup live with their provider.

```text
trading_alerts/
├── kite_runtime/       # Shared Kite Connect login and application launcher
│   ├── auth.py        # Token validation, browser callback, persistence
│   ├── launcher.py    # Start and supervise one selected Kite application
│   ├── __main__.py    # python -m kite_runtime entry point
│   └── tests/         # Shared login and startup checks
├── kite/              # Live scanner, alerts, and dashboard
├── mtf_alert/         # Completed-candle MTF strategy, alerts, and backtests
├── truedata/          # Independent TrueData integration
├── .env               # Local credentials (git-ignored)
└── requirements.txt   # Shared Python dependencies
```

## Start a Kite Connect application

Run from this repository's root directory:

```bash
# Kite dashboard and its alert scanner
.venv/bin/python -m kite_runtime kite

# MTF alerts
.venv/bin/python -m kite_runtime mtf
```

Each command starts only the selected application. Both commands validate and
reuse the same Kite session. If needed, browser login opens, the request token
is captured, and the new access token is saved to the root `.env`. Complete
Zerodha login and the app-code step yourself. Ctrl+C stops the selected app.
The Kite dashboard is available at `http://127.0.0.1:8000`.

Set your Kite developer app's Redirect URL once to
`http://127.0.0.1:8765/callback`, and set `KITE_API_KEY` and `KITE_API_SECRET`
in `.env`. Run the launcher and browser on the same computer.
Use `--login` to force a new login, `--manual-auth` for a pasted redirect URL,
or `--port 8001` to change the Kite dashboard port.

## Package boundaries

- `kite_runtime` owns Kite Connect authentication and process startup. It does
  not import strategy code or manage TrueData sessions.
- `kite` owns the live dashboard and scanner, including its rules and config.
- `mtf_alert` owns its strategy, configuration, alert state, and backtest data.
- `truedata` owns the TrueData integration and its existing startup flow.
- Both Kite applications use `kite_runtime.auth` as their single authentication
  implementation, including the optional manual redirect workflow.

Configuration, historical datasets, and alert state stay in their existing
application directories. Keeping these paths stable preserves existing commands
and stored data while shared Kite infrastructure has an explicit provider scope.

See [Kite setup](kite/README.md), [MTF setup](mtf_alert/README.md),
[shared Kite runtime](kite_runtime/README.md), and [TrueData setup](truedata/README.md).

## Verify shared authentication and startup

```bash
.venv/bin/python -m unittest discover -s kite_runtime/tests -v
.venv/bin/python -m unittest mtf_alert.tests.test_auth -v
```
