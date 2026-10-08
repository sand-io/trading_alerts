# Kite Connect runtime

This package contains the Kite Connect login and launcher shared exclusively by
`kite/` and `mtf_alert/`. TrueData manages its own credentials and startup.
The root `.env` holds shared credentials; each application's code and
configuration stay in `kite/` and `mtf_alert/`.

Run from the `trading_alerts` project directory:

```bash
# Kite dashboard and Kite alerts only
.venv/bin/python -m kite_runtime kite

# MTF alerts only
.venv/bin/python -m kite_runtime mtf
```

Set the Kite developer app Redirect URL once to
`http://127.0.0.1:8765/callback`. Each command validates the saved token,
opens browser login when needed, captures the callback, and saves the new token.
Complete Zerodha credentials and app-code entry yourself. Open the browser on
the same computer as the launcher. Kite's dashboard is at
`http://127.0.0.1:8000`. Ctrl+C stops the selected application.

Options: `--login` forces login; `--manual-auth` accepts a pasted redirect URL
when a different developer-app redirect URL is configured; `--port` sets the
Kite dashboard port. Restart the selected application when its token expires.

- `auth.py`: shared token validation, login callback, and token persistence.
- `launcher.py`: starts and supervises the selected application.
- `__main__.py`: entry point for `python -m kite_runtime`.
- `tests/`: authentication and launcher regression checks.

Run checks without broker credentials or network access:

```bash
.venv/bin/python -m unittest discover -s kite_runtime/tests -v
```
