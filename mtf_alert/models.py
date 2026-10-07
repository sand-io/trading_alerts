from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class TrendState(str, Enum):
    BULLISH = "bullish_trend"
    BEARISH = "bearish_trend"
    CONGESTION = "congestion"


@dataclass(frozen=True)
class Pipeline:
    name: str
    macro_interval: str
    momentum_interval: str
    execution_interval: str
    bias_ttl_minutes: int


@dataclass(frozen=True)
class Alert:
    symbol: str
    pipeline: str
    direction: Direction
    price: float
    candle_time: str
    reasons: tuple[str, ...]
    stop_loss: float
    target: float
