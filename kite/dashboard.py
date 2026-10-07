"""Live Kite strategy dashboard.

Run from the kite directory:
    uvicorn dashboard:app --host 127.0.0.1 --port 8000
"""

import asyncio
import json
import os
import threading
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse

from scanner import RealtimeScanner
from strategy_runtime import SUPPORTED_TIMEFRAMES, chart_snapshot, live_study_values, historical_evaluation


KITE_DIR = Path(__file__).resolve().parent


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
            if subscription["symbol"] != event["symbol"]:
                continue
            try:
                timeframe = subscription["timeframe"]
                if timeframe not in study_cache:
                    symbol_data = scanner.symbol_data.get(event["instrument_token"])
                    study_cache[timeframe] = live_study_values(symbol_data, timeframe)
                payload = dict(event)
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


app = FastAPI(title="Kite Strategy Dashboard", version="1.0.0", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard_page():
    return FileResponse(KITE_DIR / "dashboard.html", media_type="text/html")


@app.get('/strategy-indicator.js')
def strategy_indicator_script():
    return FileResponse(KITE_DIR / 'strategy_indicator.js', media_type='text/javascript')


@app.get("/api/health")
async def health():
    status = "error" if scanner_state["error"] else scanner.connection_status
    return {
        "status": status,
        "error": scanner_state["error"] or scanner.connection_error,
        "symbols": len(scanner.token_to_symbol),
        "threshold": scanner.alert_threshold,
    }


@app.get("/api/symbols")
async def symbols():
    return [{"token": token, "symbol": symbol}
            for token, symbol in scanner.token_to_symbol.items()]


@app.get("/api/snapshot/{instrument_token}")
def snapshot(instrument_token: int, timeframe: str = "5m"):
    symbol_data = scanner.symbol_data.get(instrument_token)
    if not symbol_data:
        raise HTTPException(404, "Symbol is not subscribed or scanner is still starting")
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(400, f"Timeframe must be one of: {', '.join(SUPPORTED_TIMEFRAMES)}")
    payload = chart_snapshot(symbol_data, timeframe=timeframe)
    strategy = scanner.latest_results.get(instrument_token)
    if strategy is None:
        with symbol_data._lock:
            strategy = historical_evaluation(symbol_data, scanner.bullish_conditions,
                                             scanner.bearish_conditions, scanner.alert_threshold)
    payload.update({
        "symbol": symbol_data.symbol,
        "instrument_token": instrument_token,
        "strategy": strategy,
        "signals": scanner.confirmed_signals.get(instrument_token, []),
        "threshold": scanner.alert_threshold,
    })
    return payload


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
        while True:
            # Receive client heartbeats; market events are pushed by the bridge.
            message = await websocket.receive_text()
            if message == "ping":
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
