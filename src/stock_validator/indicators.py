"""Causal end-of-bar indicators; no padding, forward fill, or future observations.

Periods count observations, not calendar days. RSI and ATR use Wilder smoothing;
ATR's first true range is the first bar's high minus low. EMA starts at the SMA
of its first complete period. Missing warm-up values are ``None``, never zero.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from .models import Bar


def _period(period: int) -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("period must be a positive integer")


def _values(values: Sequence[float]) -> list[float]:
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise ValueError("indicator inputs must be finite")
    return result


def sma(values: Sequence[float], period: int) -> float | None:
    _period(period)
    values = _values(values)
    return sum(values[-period:]) / period if len(values) >= period else None


def ema_series(values: Sequence[float], period: int) -> list[float | None]:
    _period(period)
    values = _values(values)
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1)
    for index in range(period, len(values)):
        current += alpha * (values[index] - current)
        result[index] = current
    return result


def ema(values: Sequence[float], period: int) -> float | None:
    series = ema_series(values, period)
    return series[-1] if series else None


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    _period(period)
    values = _values(values)
    if len(values) < period + 1:
        return None
    changes = [right - left for left, right in zip(values, values[1:])]
    gain = sum(max(change, 0.0) for change in changes[:period]) / period
    loss = sum(max(-change, 0.0) for change in changes[:period]) / period
    for change in changes[period:]:
        gain = ((period - 1) * gain + max(change, 0.0)) / period
        loss = ((period - 1) * loss + max(-change, 0.0)) / period
    if loss == 0:
        return 50.0 if gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    result: list[float] = []
    previous: float | None = None
    for bar in bars:
        if not all(math.isfinite(value) for value in (bar.high, bar.low, bar.close)):
            raise ValueError("ATR inputs must be finite")
        if bar.high < bar.low:
            raise ValueError("bar high must be at least its low")
        result.append(bar.high - bar.low if previous is None else max(
            bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
        previous = bar.close
    return result


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    _period(period)
    ranges = true_ranges(bars)
    if len(ranges) < period:
        return None
    current = sum(ranges[:period]) / period
    for value in ranges[period:]:
        current = ((period - 1) * current + value) / period
    return current


@dataclass(frozen=True)
class MACD:
    line: float
    signal: float
    histogram: float


def macd(values: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> MACD | None:
    for period in (fast, slow, signal):
        _period(period)
    if fast >= slow:
        raise ValueError("MACD fast period must be smaller than slow period")
    fast_values, slow_values = ema_series(values, fast), ema_series(values, slow)
    lines = [left - right for left, right in zip(fast_values, slow_values)
             if left is not None and right is not None]
    signal_value = ema(lines, signal)
    if signal_value is None:
        return None
    return MACD(lines[-1], signal_value, lines[-1] - signal_value)
