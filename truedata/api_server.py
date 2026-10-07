"""
Strategy API Server
FastAPI with REST + WebSocket endpoints + embedded Web UI.

Run with:
    uvicorn api_server:app --reload --port 8000

UI available at: http://localhost:8000
"""

import json
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from strategy_db import (
    init_db, create_strategy, get_strategy, get_all_strategies,
    update_strategy, delete_strategy, save_alert, get_alerts
)
from strategy_evaluator import evaluate_strategy, INDICATORS, TIMEFRAMES, CONDITIONS

# ─── App Setup ───────────────────────────────────────────────────────────────

app = FastAPI(title="Strategy Builder API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global reference to the scanner (set from main.py when running together)
scanner_ref = None
active_ws_clients: list[WebSocket] = []


@app.on_event("startup")
async def startup():
    init_db()


# ─── Pydantic Models ─────────────────────────────────────────────────────────

class ConditionModel(BaseModel):
    timeframe: str
    indicator: str
    indicator_param: str = ""
    condition: str
    compare_to: str
    compare_param: str = ""


class StrategyModel(BaseModel):
    name: str
    description: str = ""
    signal_type: str = "CUSTOM"
    conditions: list[ConditionModel]
    match_type: str = "ALL"
    symbols: list[str]
    mode: str = "LIVE"
    threshold_pct: float = 100.0


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    signal_type: Optional[str] = None
    conditions: Optional[list[ConditionModel]] = None
    match_type: Optional[str] = None
    symbols: Optional[list[str]] = None
    mode: Optional[str] = None
    threshold_pct: Optional[float] = None
    is_active: Optional[bool] = None


# ─── REST: Catalog ───────────────────────────────────────────────────────────

@app.get("/api/catalog")
def get_catalog():
    """Returns all available indicators, timeframes, conditions for UI dropdowns."""
    return {
        "indicators": INDICATORS,
        "timeframes": TIMEFRAMES,
        "conditions": CONDITIONS,
    }

@app.get("/api/symbols")
def get_symbols():
    """Returns all currently subscribed symbols."""
    from nifty200 import NIFTY200_SYMBOLS
    return {"symbols": NIFTY200_SYMBOLS}


# ─── REST: Strategy CRUD ─────────────────────────────────────────────────────

@app.post("/api/strategies", status_code=201)
def create(body: StrategyModel):
    data = body.model_dump()
    data['conditions'] = [c if isinstance(c, dict) else c.model_dump() for c in body.conditions]
    strategy_id = create_strategy(data)
    return {"id": strategy_id, "message": "Strategy created"}


@app.get("/api/strategies")
def list_strategies(active_only: bool = False):
    return get_all_strategies(active_only)


@app.get("/api/strategies/{strategy_id}")
def get(strategy_id: int):
    s = get_strategy(strategy_id)
    if not s:
        raise HTTPException(404, "Strategy not found")
    return s


@app.put("/api/strategies/{strategy_id}")
def update(strategy_id: int, body: StrategyUpdate):
    if not get_strategy(strategy_id):
        raise HTTPException(404, "Strategy not found")
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    if 'conditions' in data:
        data['conditions'] = [c if isinstance(c, dict) else c for c in data['conditions']]
    update_strategy(strategy_id, data)
    return {"message": "Updated"}


@app.delete("/api/strategies/{strategy_id}")
def delete(strategy_id: int):
    if not get_strategy(strategy_id):
        raise HTTPException(404, "Strategy not found")
    delete_strategy(strategy_id)
    return {"message": "Deleted"}


# ─── REST: One-time Check ────────────────────────────────────────────────────

@app.post("/api/strategies/{strategy_id}/run")
def run_once(strategy_id: int):
    """
    One-time evaluation of strategy against current live data.
    Requires scanner to be running (started from main.py).
    """
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(404, "Strategy not found")

    if scanner_ref is None:
        raise HTTPException(503, "Scanner not running. Start main.py first.")

    results = []
    for symbol in strategy['symbols']:
        store = scanner_ref.stores.get(symbol)
        if not store or store.tick_count() < 5:
            results.append({"symbol": symbol, "error": "No live data yet"})
            continue

        data = store.get_all_timeframes()
        from indicators import get_reference_values
        refs = get_reference_values(data.get('1day'), data.get('1week'))
        result = evaluate_strategy(strategy, data, refs)
        result['symbol'] = symbol
        results.append(result)

        if result['triggered']:
            passed_labels = [r['reason'] for r in result['passed']]
            failed_labels = [r['reason'] for r in result['failed']]
            save_alert(strategy_id, symbol, strategy['signal_type'],
                       result['score_pct'], passed_labels, failed_labels)

    return {"strategy": strategy['name'], "results": results, "ran_at": datetime.now().isoformat()}


# ─── REST: Alerts History ────────────────────────────────────────────────────

@app.get("/api/alerts")
def list_alerts(strategy_id: int = None, limit: int = 50):
    return get_alerts(strategy_id, limit)


# ─── WebSocket: Live Strategy Alerts ─────────────────────────────────────────

@app.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    """
    Connect to receive live alerts for all active strategies.
    Messages are pushed whenever a strategy triggers.
    """
    await websocket.accept()
    active_ws_clients.append(websocket)
    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        active_ws_clients.remove(websocket)


async def broadcast_alert(alert: dict):
    """Called from scanner when a user strategy triggers."""
    dead = []
    for ws in active_ws_clients:
        try:
            await ws.send_json(alert)
        except Exception:
            dead.append(ws)
    for ws in dead:
        active_ws_clients.remove(ws)


# ─── Web UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def ui():
    return HTMLResponse(open("strategy_ui.html").read())


# ─── Run directly ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
