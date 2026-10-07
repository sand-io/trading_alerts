"""
Alert System
Sends alerts when bullish/bearish conditions are met.
Supports: console print, desktop notification (optional), sound (optional).
Threshold: configurable % of rules that must pass to trigger alert.
"""

import time
import datetime
from evaluator import score_results


# ─── Config ──────────────────────────────────────────────────────────────────

ALERT_THRESHOLD_PCT = 70   # % of rules that must pass to trigger alert
COOLDOWN_SECONDS    = 300  # Don't re-alert same signal within 5 minutes


class AlertManager:
    def __init__(self, threshold_pct: float = ALERT_THRESHOLD_PCT,
                 cooldown_sec: int = COOLDOWN_SECONDS):
        self.threshold_pct = threshold_pct
        self.cooldown_sec = cooldown_sec
        self._last_alert: dict[str, float] = {}   # signal_type -> timestamp

    def _can_alert(self, signal_type: str) -> bool:
        last = self._last_alert.get(signal_type, 0)
        return (time.time() - last) >= self.cooldown_sec

    def _record_alert(self, signal_type: str):
        self._last_alert[signal_type] = time.time()

    def check_and_alert(self, symbol: str, bullish_rules, bearish_rules,
                        data: dict, refs: dict):
        from evaluator import evaluate_all_rules

        bull_results = evaluate_all_rules(bullish_rules, data, refs)
        bear_results = evaluate_all_rules(bearish_rules, data, refs)

        bull_score = score_results(bull_results)
        bear_score = score_results(bear_results)

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        triggered = []

        if bull_score['score_pct'] >= self.threshold_pct and self._can_alert(f"{symbol}_BULL"):
            triggered.append(("BULLISH", bull_score))
            self._record_alert(f"{symbol}_BULL")

        if bear_score['score_pct'] >= self.threshold_pct and self._can_alert(f"{symbol}_BEAR"):
            triggered.append(("BEARISH", bear_score))
            self._record_alert(f"{symbol}_BEAR")

        # Always print live status
        # print(f"\n{'='*60}")
        # print(f"  [{now}]  {symbol}")
        # print(f"  🟢 BULLISH: {bull_score['passed']}/{bull_score['total']} rules = {bull_score['score_pct']}%")
        # print(f"  🔴 BEARISH: {bear_score['passed']}/{bear_score['total']} rules = {bear_score['score_pct']}%")

        for signal_type, score in triggered:
            emoji = "🚀" if signal_type == "BULLISH" else "💥"
            print(f"\n  {emoji}  ALERT: {signal_type} SIGNAL on {symbol}!")
            print(f"  {score['passed']}/{score['total']} conditions met ({score['score_pct']}%)")
            print(f"\n  Passed conditions:")
            for r in score['results']:
                if r.passed:
                    print(f"    ✅ {r.rule.raw}")
            print(f"\n  Failed conditions:")
            for r in score['results']:
                if not r.passed:
                    print(f"    ❌ {r.rule.raw}  [{r.reason}]")

            # Optional: desktop notification (macOS)
            try:
                import subprocess
                msg = f"{signal_type} signal on {symbol} — {score['score_pct']}% conditions met"
                subprocess.run([
                    'osascript', '-e',
                    f'display notification "{msg}" with title "Trading Alert 📊"'
                ], check=False, capture_output=True)
            except Exception:
                pass

        return triggered
