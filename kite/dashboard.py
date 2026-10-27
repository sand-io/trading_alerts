"""Live Kite strategy dashboard.

Run from the kite directory:
    uvicorn dashboard:app --host 127.0.0.1 --port 8000
"""

import asyncio
import json
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

from scanner import RealtimeScanner, strategy_log, history_requests
from strategy_runtime import SUPPORTED_TIMEFRAMES, chart_snapshot, live_study_values, historical_evaluation, profile_chart_snapshot, restore_missing_chart_studies


KITE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = KITE_DIR / 'frontend'


class LiveBridge:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: dict[WebSocket, dict[str, str]] = {}
        self._lock = asyncio.Lock()
        self._subscription_lock = threading.Lock()
        self._subscription_counts: dict[str, int] = {}
        self.latest_status = {"type": "connection_status", "status": "initializing", "error": None}
        self._closing = False
        self._pending = set()
        self._pending_lock = threading.Lock()

    def _schedule(self, coroutine) -> None:
        # Scanner callbacks run on the Kite thread, not the ASGI event loop.
        with self._pending_lock:
            if self._closing or not self.loop or not self.loop.is_running():
                coroutine.close()
                return
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
            except RuntimeError:
                coroutine.close()
                return
            self._pending.add(future)
        future.add_done_callback(self._discard_pending)

    def _discard_pending(self, future) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    async def shutdown(self) -> None:
        with self._pending_lock:
            self._closing = True
            pending = list(self._pending)
        for future in pending:
            future.cancel()
        async with self._lock:
            sockets = list(self.clients)
        for socket in sockets:
            try:
                await asyncio.wait_for(socket.close(code=1001, reason="Server shutting down"), 1)
            except asyncio.CancelledError:
                raise
            except Exception:
                # An already-disconnected socket requires no further handshake.
                pass
            finally:
                await self.unregister(socket)
        self.loop = None

    def publish_from_scanner(self, event: dict) -> None:
        if event.get('type') == 'strategy_changed':
            self._schedule(self.broadcast(event))
            return
        with self._subscription_lock:
            if not self._subscription_counts.get(event["symbol"], 0):
                return
        self._schedule(self.broadcast(event))

    def publish_status_from_scanner(self, status: str, error: str | None) -> None:
        self.latest_status = {
            "type": "connection_status",
            "status": status,
            "error": error,
            "server_time": datetime.now().astimezone().isoformat(),
        }
        self._schedule(self.broadcast_status())

    def add_subscription(self, symbol: str) -> None:
        with self._subscription_lock:
            self._subscription_counts[symbol] = self._subscription_counts.get(symbol, 0) + 1

    def remove_subscription(self, symbol: str) -> None:
        with self._subscription_lock:
            remaining = self._subscription_counts.get(symbol, 0) - 1
            if remaining > 0:
                self._subscription_counts[symbol] = remaining
            else:
                self._subscription_counts.pop(symbol, None)

    async def unregister(self, socket: WebSocket) -> None:
        async with self._lock:
            subscription = self.clients.pop(socket, None)
        if subscription:
            self.remove_subscription(subscription["symbol"])

    async def broadcast(self, event: dict) -> None:
        async with self._lock:
            targets = [(socket, subscription.copy()) for socket, subscription in self.clients.items()]
        dead = []
        study_cache = {}
        for socket, subscription in targets:
            if event.get("type") != "strategy_changed" and subscription["symbol"] != event["symbol"]:
                continue
            try:
                if event.get('type') == 'strategy_changed':
                    await socket.send_json(event)
                    continue
                timeframe = subscription["timeframe"]
                if event.get('type') != 'candle_update' and timeframe not in study_cache:
                    symbol_data = scanner.symbol_data.get(event["instrument_token"])
                    try:
                        study_cache[timeframe] = await asyncio.to_thread(
                            live_study_values, symbol_data, timeframe, getattr(scanner, 'active_strategy', None))
                    except Exception:
                        # A study failure must not unsubscribe a healthy client
                        # or suppress the already-evaluated condition panels.
                        strategy_log.exception('Dashboard study calculation failed: %s %s', event['symbol'], timeframe)
                        study_cache[timeframe] = None
                payload = dict(event)
                if study_cache.get(timeframe) is not None:
                    payload["study_values"] = study_cache[timeframe]
                await socket.send_json(payload)
            except Exception:
                dead.append((socket, subscription["symbol"]))
        if dead:
            for socket, _ in dead:
                await self.unregister(socket)

    async def broadcast_status(self) -> None:
        async with self._lock:
            sockets = [(socket, subscription["symbol"])
                       for socket, subscription in self.clients.items()]
        dead = []
        for socket, symbol in sockets:
            try:
                await socket.send_json(self.latest_status)
            except Exception:
                dead.append((socket, symbol))
        if dead:
            for socket, _ in dead:
                await self.unregister(socket)


bridge = LiveBridge()
scanner = RealtimeScanner(
    event_callback=bridge.publish_from_scanner,
    status_callback=bridge.publish_status_from_scanner,
)
scanner_state = {"error": None}


def _run_scanner() -> None:
    try:
        scanner.run(threaded=True)
    except SystemExit as exc:
        scanner_state["error"] = f"Scanner stopped during startup (exit {exc.code})"
    except Exception as exc:
        scanner_state["error"] = str(exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bridge.loop = asyncio.get_running_loop()
    bridge._closing = False
    scanner_thread = threading.Thread(target=_run_scanner, name="kite-scanner", daemon=True)
    scanner_thread.start()
    async def close_session_candles():
        while True:
            await asyncio.to_thread(scanner.finalize_session)
            await asyncio.to_thread(scanner.reconnect_if_stale)
            await asyncio.sleep(1)
    finalizer = asyncio.create_task(close_session_candles())
    try:
        yield
    finally:
        closed = scanner.request_stop()
        finalizer.cancel()
        try:
            await finalizer
        except asyncio.CancelledError:
            pass
        await bridge.shutdown()
        # Both waits are bounded; an upstream outage must not hang ASGI shutdown.
        await asyncio.to_thread(closed.wait, 2)
        await asyncio.to_thread(scanner_thread.join, 2)
        await asyncio.to_thread(scanner.flush_background)


app = FastAPI(title="Kite Strategy Dashboard", version="1.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard_page():
    return FileResponse(FRONTEND_DIR / "dashboard.html", media_type="text/html")


@app.get("/api/health")
async def health():
    status = "error" if scanner_state["error"] else scanner.connection_status
    return {
        "status": status,
        "error": scanner_state["error"] or scanner.connection_error,
        "symbols": len(scanner.token_to_symbol),
        "threshold": scanner.alert_threshold,
        "processing_error": getattr(scanner, 'processing_error', None),
        "history_error": getattr(scanner, 'history_error', None),
        "recording_strategies": list(getattr(scanner, 'recording_profiles', {})),
        "strategy_errors": getattr(scanner, 'profile_errors', {}),
        "instrument_errors": getattr(scanner, 'instrument_errors', {}),
        "historical_requests": history_requests.stats(),
        "pending_tick_batches": scanner._tick_queue.qsize(),
        "pending_evaluations": len(scanner._pending_evaluations),
        "pending_chart_archives": len(getattr(scanner, '_pending_archives', {})),
        "pending_history_events": getattr(getattr(scanner, '_history_queue', None), 'unfinished_tasks', 0),
        "evaluation_lag_seconds": getattr(scanner, '_last_evaluation_lag_seconds', None),
        "feed_age_seconds": scanner.feed_age_seconds(),
    }


@app.get('/api/strategies')
def strategies():
    return {'profiles': scanner.strategy_store.profiles(), 'active': scanner.strategy_info(),
            'error': scanner.strategy_error}


@app.get('/alerts', response_class=HTMLResponse)
def alerts_window():
    return FileResponse(FRONTEND_DIR / 'alerts.html', media_type='text/html')


@app.get('/api/alerts/dates')
def alert_dates():
    return scanner.history.alert_days()


@app.get('/api/alerts')
def daily_alerts(day: str, side: str | None = None, strategy_id: str | None = None,
                 search: str = '', before_id: int | None = None, limit: int = 100):
    try:
        datetime.strptime(day, '%Y-%m-%d')
    except ValueError:
        raise HTTPException(400, 'Date must be YYYY-MM-DD')
    if side not in (None, 'bullish', 'bearish'):
        raise HTTPException(400, 'Direction must be bullish or bearish')
    if not 1 <= limit <= 200 or (before_id is not None and before_id < 1):
        raise HTTPException(400, 'Invalid page size or cursor')
    result = scanner.history.daily_alerts(day, side, strategy_id, search, before_id, limit)
    # Include zero-alert profiles, while keeping deleted historical profiles reviewable.
    profiles = {profile['id']: profile for profile in result['strategies']}
    store = getattr(scanner, 'strategy_store', None)
    for profile in store.profiles() if store is not None else ():
        previous = profiles.get(profile['id'], {})
        profiles[profile['id']] = {**previous, 'id': profile['id'], 'name': profile['name'],
                                   'alert_count': previous.get('alert_count', 0)}
    result['strategies'] = sorted(profiles.values(), key=lambda profile: profile['name'].casefold())
    return result


@app.get('/api/alerts/{alert_id}')
def saved_alert(alert_id: int):
    result = scanner.history.alert(alert_id)
    if result is None:
        raise HTTPException(404, 'Saved alert is unavailable or outside the retention period')
    return result


@app.post('/api/strategies/{strategy_id}/activate')
def activate_strategy(strategy_id: str):
    try:
        return scanner.activate_strategy(strategy_id)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        strategy_log.warning('Activation rejected for [%s]; active strategy unchanged: %s', strategy_id, exc)
        raise HTTPException(400, str(exc))


@app.get("/api/symbols")
async def symbols():
    return [{"token": token, "symbol": symbol}
            for token, symbol in scanner.token_to_symbol.items()
            if token in scanner.symbol_data
            and scanner.profile_applies(scanner.active_strategy, scanner.symbol_data[token])]


@app.get("/api/snapshot/{instrument_token}")
def snapshot(instrument_token: int, timeframe: str = "5m"):
    with getattr(scanner, '_process_lock', nullcontext()), getattr(scanner, '_evaluation_lock', nullcontext()):
        symbol_data = scanner.symbol_data.get(instrument_token)
        if not symbol_data:
            raise HTTPException(404, "Symbol is not subscribed or scanner is still starting")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise HTTPException(400, f"Timeframe must be one of: {', '.join(SUPPORTED_TIMEFRAMES)}")
        payload = chart_snapshot(symbol_data, timeframe=timeframe, limit=600)
        applicable = (not hasattr(scanner, 'active_strategy') or
                      scanner.profile_applies(scanner.active_strategy, symbol_data))
        if hasattr(scanner, 'active_strategy') and applicable:
            payload = profile_chart_snapshot(symbol_data, timeframe, scanner.active_strategy, limit=600, base=payload)
        strategy = scanner.latest_results.get(instrument_token)
        if strategy is not None and hasattr(scanner, 'active_strategy') and strategy.get('strategy_version') is not None and strategy['strategy_version'] != scanner.active_strategy['version']:
            strategy = None
        if strategy is None and applicable:
            with symbol_data._lock:
                strategy = historical_evaluation(symbol_data, scanner.bullish_conditions,
                                                 scanner.bearish_conditions, scanner.alert_threshold,
                                                 getattr(scanner, 'active_strategy', {}).get('confirmation_timeframe', '5m'))
        if strategy is not None and hasattr(scanner, 'active_strategy'):
            strategy = {**strategy, 'strategy_id': scanner.active_strategy['id'],
                        'strategy_name': scanner.active_strategy['name'],
                        'strategy_version': scanner.active_strategy['version']}
        if not applicable:
            strategy = None
        payload.update({
            "symbol": symbol_data.symbol,
            "instrument_token": instrument_token,
            "strategy": strategy,
            "strategy_applicable": applicable,
            "scope_message": ('This strategy applies only to standard MCX Gold and Crude Oil futures.'
                              if getattr(scanner, 'active_strategy', {}).get('instrument_scope') else
                              'This strategy applies to the configured watchlist; this contract is subscribed for NNF.'),
            "market": getattr(scanner, 'latest_market_results', {}).get(instrument_token),
            "signals": scanner.history.events(instrument_token,
                datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat(),
                'confirmed', getattr(scanner, 'active_strategy', {}).get('id', 'default')) if applicable else [],
            "threshold": scanner.alert_threshold,
            "confirmation_timeframe": getattr(scanner, 'active_strategy', {}).get('confirmation_timeframe', '5m'),
        })
        return payload


@app.get("/api/history/{instrument_token}/dates")
def history_dates(instrument_token: int):
    return scanner.history.days(instrument_token)


@app.get('/api/history/{instrument_token}/{day}/strategies')
def history_strategies(instrument_token: int, day: str):
    try:
        datetime.strptime(day, '%Y-%m-%d')
    except ValueError:
        raise HTTPException(400, 'Date must be YYYY-MM-DD')
    return scanner.history.session_strategies(instrument_token, day)


@app.get("/api/history/{instrument_token}/{day}")
def history_day(instrument_token: int, day: str, timeframe: str = "5m", strategy_id: str | None = None):
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(400, "Unsupported timeframe")
    chart = (scanner.history.strategy_chart(instrument_token, day, timeframe, strategy_id)
             if strategy_id else scanner.history.chart(instrument_token, day, timeframe))
    if chart is None:
        raise HTTPException(404, "No recorded chart for this date")
    chart = restore_missing_chart_studies(chart, timeframe)
    evaluations = scanner.history.events(instrument_token, day, 'evaluation', strategy_id)
    return {**chart, "instrument_token": instrument_token, "day": day,
            "strategy": evaluations[-1] if evaluations else None,
            "threshold": evaluations[-1].get('threshold') if evaluations else None,
            "signals": scanner.history.events(instrument_token, day, 'confirmed', strategy_id),
            "alerts": scanner.history.events(instrument_token, day, 'alert', strategy_id),
            "evaluations": evaluations}


@app.websocket("/ws/{instrument_token}")
async def live_updates(websocket: WebSocket, instrument_token: int):
    symbol = scanner.token_to_symbol.get(instrument_token)
    if not symbol:
        await websocket.close(code=4404, reason="Unknown instrument token")
        return
    await websocket.accept()
    await websocket.send_json(bridge.latest_status)
    async with bridge._lock:
        bridge.clients[websocket] = {"symbol": symbol, "timeframe": "5m"}
    bridge.add_subscription(symbol)
    try:
        await websocket.send_json({'type': 'strategy_changed', 'active': scanner.strategy_info(), 'source': 'connection sync'})
        while True:
            # Receive client heartbeats; market events are pushed by the bridge.
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({
                    "type": "feed_heartbeat",
                    "status": scanner.connection_status,
                    "error": scanner.connection_error,
                    "feed_age_seconds": scanner.feed_age_seconds(),
                    "market_open": scanner.market_is_open(),
                })
                continue
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            timeframe = payload.get("timeframe")
            if payload.get("type") == "set_timeframe" and timeframe in SUPPORTED_TIMEFRAMES:
                async with bridge._lock:
                    if websocket in bridge.clients:
                        bridge.clients[websocket]["timeframe"] = timeframe
    except WebSocketDisconnect:
        pass
    finally:
        await bridge.unregister(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
                port=int(os.getenv("DASHBOARD_PORT", "8000")))
