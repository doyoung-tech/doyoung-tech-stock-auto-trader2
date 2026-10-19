"""Six independent, immutable research rules using information observable at t.

A signal is decided at the completed bar. The engine alone sizes and executes
at the next tradable bar and applies costs, risk limits, and exit rules.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import math
from typing import ClassVar, Sequence

from .indicators import atr, rsi, sma
from .models import Bar, EngineConfig, EntrySignal, ExitRules, Fundamental, Strategy, StrategyContext


def _history(history: Sequence[Bar], context: StrategyContext) -> tuple[Bar, ...]:
    # Defence in depth: callers cannot leak future bars into a rule accidentally.
    bars = tuple(bar for bar in history if bar.timestamp <= context.decision_time)
    if not bars:
        return ()
    if any(bar.symbol != bars[0].symbol for bar in bars):
        raise ValueError("a strategy history must contain one symbol")
    if any(left.timestamp >= right.timestamp for left, right in zip(bars, bars[1:])):
        raise ValueError("strategy history must be strictly chronological")
    if not bars[-1].tradable:
        return ()
    return bars


def _entry(bars: Sequence[Bar], reason: str) -> EntrySignal | None:
    value = atr(bars)
    return EntrySignal(reason, value) if value is not None and value > 0 else None


def _mean_volume(bars: Sequence[Bar], period: int = 20) -> float:
    previous = bars[-period - 1:-1]
    return sum(bar.volume for bar in previous) / len(previous) if previous else 0.0


def _fundamental(bars: Sequence[Bar], context: StrategyContext) -> Fundamental | None:
    available = [item for item in context.fundamentals
                 if item.symbol == bars[-1].symbol
                 and item.period_end <= item.available_at <= context.decision_time]
    # Multiple statements at one release choose the newest reporting period.
    return max(available, key=lambda item: (item.available_at, item.period_end)) if available else None


def _known(*values: float | None) -> bool:
    return all(value is not None and math.isfinite(value) for value in values)


class _Parameters:
    """Reject invalid experiment settings before reading any performance data."""

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
            if name.endswith("period") or name in {"minimum_history", "peak_lookback",
                                                   "max_holding_bars", "max_fundamental_age_days"}:
                if not isinstance(value, int) or value < 1:
                    raise ValueError(f"{name} must be a positive integer")
            elif "rsi" in name or name == "oversold_level":
                if not 0 <= value <= 100:
                    raise ValueError(f"{name} must be between 0 and 100")
            elif name == "volume_multiple":
                if value <= 0:
                    raise ValueError("volume_multiple must be positive")
            elif not 0 <= value < 1:
                raise ValueError(f"{name} must be a fraction between 0 and 1")
        if hasattr(self, "fast_period") and self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be smaller than slow_period")
        if self.key == "E":
            if not 252 <= self.max_holding_bars <= 1260:
                raise ValueError("E max_holding_bars must be between 252 and 1260")
            if self.minimum_drawdown >= self.maximum_drawdown:
                raise ValueError("minimum_drawdown must be smaller than maximum_drawdown")
        elif not 1 <= self.max_holding_bars <= 5:
            raise ValueError("short-term max_holding_bars must be between 1 and 5")


@dataclass(frozen=True)
class TrendBreakout(_Parameters):
    key: ClassVar[str] = "A"
    name: ClassVar[str] = "추세 돌파"
    version: ClassVar[str] = "1.0.0"
    requires_fundamentals: ClassVar[bool] = False
    requires_sectors: ClassVar[bool] = False
    fast_period: int = 20
    slow_period: int = 60
    breakout_period: int = 20
    volume_multiple: float = 1.5
    max_holding_bars: int = 5

    def signal(self, history: Sequence[Bar], context: StrategyContext) -> EntrySignal | None:
        bars = _history(history, context)
        if len(bars) < max(self.slow_period, self.breakout_period + 1, 21):
            return None
        closes = [bar.close for bar in bars]
        fast, slow = sma(closes, self.fast_period), sma(closes, self.slow_period)
        previous_high = max(bar.high for bar in bars[-self.breakout_period - 1:-1])
        last = bars[-1]
        baseline = _mean_volume(bars)
        if (fast is not None and slow is not None and last.close > fast > slow
                and last.close > previous_high and last.close > last.open
                and baseline > 0 and last.volume >= self.volume_multiple * baseline):
            return _entry(bars, "20/60일 상승 추세·이전 봉 고가 돌파·거래량 증가")
        return None


@dataclass(frozen=True)
class Pullback(_Parameters):
    key: ClassVar[str] = "B"
    name: ClassVar[str] = "눌림목 매수"
    version: ClassVar[str] = "1.0.0"
    requires_fundamentals: ClassVar[bool] = False
    requires_sectors: ClassVar[bool] = False
    fast_period: int = 20
    slow_period: int = 60
    pullback_period: int = 5
    pullback_fraction: float = 0.02
    ma_tolerance: float = 0.01
    max_holding_bars: int = 5

    def signal(self, history: Sequence[Bar], context: StrategyContext) -> EntrySignal | None:
        bars = _history(history, context)
        if len(bars) < max(self.slow_period + 1, self.pullback_period + 2, 21):
            return None
        closes = [bar.close for bar in bars]
        fast, slow = sma(closes, self.fast_period), sma(closes, self.slow_period)
        previous_fast = sma(closes[:-1], self.fast_period)
        previous_peak = max(bar.high for bar in bars[-self.pullback_period - 2:-2])
        previous, last = bars[-2:]
        if (fast is not None and slow is not None and previous_fast is not None
                and last.close > fast > slow
                and previous.close <= previous_peak * (1.0 - self.pullback_fraction)
                and previous.close <= previous_fast * (1.0 + self.ma_tolerance)
                and last.close > previous.high and last.close > last.open):
            return _entry(bars, "상승 추세 속 2% 이상 조정 후 전일 고가 회복")
        return None


@dataclass(frozen=True)
class OversoldRebound(_Parameters):
    key: ClassVar[str] = "C"
    name: ClassVar[str] = "과매도 반등"
    version: ClassVar[str] = "1.0.0"
    requires_fundamentals: ClassVar[bool] = False
    requires_sectors: ClassVar[bool] = False
    rsi_period: int = 14
    oversold_level: float = 30.0
    maximum_rebound_rsi: float = 45.0
    drawdown_period: int = 20
    minimum_decline: float = 0.10
    support_tolerance: float = 0.10
    volume_multiple: float = 1.2
    max_holding_bars: int = 3

    def signal(self, history: Sequence[Bar], context: StrategyContext) -> EntrySignal | None:
        bars = _history(history, context)
        if len(bars) < max(self.rsi_period + 2, self.drawdown_period + 1, 21):
            return None
        closes = [bar.close for bar in bars]
        previous_rsi = rsi(closes[:-1], self.rsi_period)
        current_rsi = rsi(closes, self.rsi_period)
        prior = bars[-self.drawdown_period - 1:-1]
        peak, support = max(bar.high for bar in prior), min(bar.low for bar in prior)
        last, previous = bars[-1], bars[-2]
        baseline = _mean_volume(bars)
        if (previous_rsi is not None and current_rsi is not None
                and previous_rsi <= self.oversold_level
                and current_rsi <= self.maximum_rebound_rsi
                and previous.close <= peak * (1 - self.minimum_decline)
                and support > 0 and last.close <= support * (1 + self.support_tolerance)
                and last.close > previous.close and last.close > last.open
                and baseline > 0 and last.volume >= baseline * self.volume_multiple):
            return _entry(bars, "RSI 과매도·급락·과거 지지대 인근 거래량 동반 반등")
        return None


@dataclass(frozen=True)
class EarningsImprovement(_Parameters):
    key: ClassVar[str] = "D"
    name: ClassVar[str] = "실적 개선"
    version: ClassVar[str] = "1.0.0"
    requires_fundamentals: ClassVar[bool] = True
    requires_sectors: ClassVar[bool] = False
    fast_period: int = 20
    slow_period: int = 60
    revenue_growth_min: float = 0.05
    earnings_growth_min: float = 0.05
    revision_min: float = 0.02
    max_fundamental_age_days: int = 180
    max_holding_bars: int = 5

    def signal(self, history: Sequence[Bar], context: StrategyContext) -> EntrySignal | None:
        bars = _history(history, context)
        if len(bars) < max(self.slow_period, 21):
            return None
        report = _fundamental(bars, context)
        if (report is None or context.decision_time - report.available_at > timedelta(days=self.max_fundamental_age_days)
                or not _known(report.revenue_growth, report.earnings_growth, report.forecast_revision)):
            return None
        closes = [bar.close for bar in bars]
        fast, slow = sma(closes, self.fast_period), sma(closes, self.slow_period)
        if (report.revenue_growth >= self.revenue_growth_min
                and report.earnings_growth >= self.earnings_growth_min
                and report.forecast_revision >= self.revision_min
                and fast is not None and slow is not None and closes[-1] > fast > slow
                and _mean_volume(bars) > 0 and bars[-1].volume >= _mean_volume(bars)):
            return _entry(bars, "발표 당시 매출·이익 성장 및 전망 상향과 주가 강세")
        return None


@dataclass(frozen=True)
class DeepValueRecovery(_Parameters):
    key: ClassVar[str] = "E"
    name: ClassVar[str] = "폭락 우량주 회복"
    version: ClassVar[str] = "1.0.0"
    requires_fundamentals: ClassVar[bool] = True
    requires_sectors: ClassVar[bool] = False
    minimum_history: int = 252
    peak_lookback: int = 1260
    minimum_drawdown: float = 0.70
    maximum_drawdown: float = 0.95
    debt_to_assets_max: float = 0.60
    earnings_growth_min: float = 0.10
    max_fundamental_age_days: int = 397
    max_holding_bars: int = 1260

    def signal(self, history: Sequence[Bar], context: StrategyContext) -> EntrySignal | None:
        bars = _history(history, context)
        if len(bars) < max(self.minimum_history, 70):
            return None
        report = _fundamental(bars, context)
        if (report is None or context.decision_time - report.available_at > timedelta(days=self.max_fundamental_age_days)
                or report.formerly_large_cap is not True or report.profitable is not True
                or not _known(report.revenue_growth, report.earnings_growth, report.debt_to_assets)):
            return None
        closes = [bar.close for bar in bars]
        peak = max(bar.high for bar in bars[-self.peak_lookback:])
        if peak <= 0:
            return None
        drawdown = 1.0 - closes[-1] / peak
        fast, slow = sma(closes, 20), sma(closes, 60)
        older_fast = sma(closes[:-10], 20)
        previous_high = max(bar.high for bar in bars[-21:-1])
        if (self.minimum_drawdown <= drawdown <= self.maximum_drawdown
                and 0 <= report.debt_to_assets <= self.debt_to_assets_max
                and report.revenue_growth >= 0 and report.earnings_growth >= self.earnings_growth_min
                and fast is not None and slow is not None and older_fast is not None
                and closes[-1] > slow and fast > older_fast and closes[-1] > previous_high):
            return _entry(bars, "과거 대형주·70~95% 하락·재무 개선 확인 후 회복 돌파")
        return None


@dataclass(frozen=True)
class SectorRelativeStrength(_Parameters):
    key: ClassVar[str] = "F"
    name: ClassVar[str] = "업종 상대강도"
    version: ClassVar[str] = "1.0.0"
    requires_fundamentals: ClassVar[bool] = False
    requires_sectors: ClassVar[bool] = True
    comparison_period: int = 60
    relative_strength_min: float = 0.03
    max_holding_bars: int = 5

    def signal(self, history: Sequence[Bar], context: StrategyContext) -> EntrySignal | None:
        bars = _history(history, context)
        if len(bars) < max(60, self.comparison_period + 1):
            return None
        # Engine supplies the stock's historical sector, not today's membership.
        sector = _history(context.sector_bars, context)
        benchmark = _history(context.benchmark, context)
        if not sector or not benchmark:
            return None
        stock_map = {bar.timestamp: bar.close for bar in bars}
        sector_map = {bar.timestamp: bar.close for bar in sector}
        benchmark_map = {bar.timestamp: bar.close for bar in benchmark}
        common = sorted(stock_map.keys() & sector_map.keys() & benchmark_map.keys())
        if len(common) <= self.comparison_period or common[-1] != bars[-1].timestamp:
            return None
        start, end = common[-self.comparison_period - 1], common[-1]
        if min(stock_map[start], sector_map[start], benchmark_map[start]) <= 0:
            return None
        own_return = stock_map[end] / stock_map[start] - 1
        sector_return = sector_map[end] / sector_map[start] - 1
        market_return = benchmark_map[end] / benchmark_map[start] - 1
        closes = [bar.close for bar in bars]
        fast, slow = sma(closes, 20), sma(closes, 60)
        if (sector_return - market_return >= self.relative_strength_min
                and own_return > sector_return
                and fast is not None and slow is not None and closes[-1] > fast > slow):
            return _entry(bars, "동일 과거시점 업종의 시장 초과성과·종목의 업종 초과강도")
        return None


def get_strategies() -> list[Strategy]:
    return [TrendBreakout(), Pullback(), OversoldRebound(), EarningsImprovement(),
            DeepValueRecovery(), SectorRelativeStrength()]


def get_strategy(key: str) -> Strategy:
    for strategy in get_strategies():
        if strategy.key == key.upper():
            return strategy
    raise ValueError(f"unknown strategy: {key}")


def parameter_variants(key: str) -> list[Strategy]:
    """Prespecified immediate neighbours; final holdout must never select among them."""
    strategy = get_strategy(key)
    field, values = {
        "A": ("volume_multiple", (1.3, 1.7)),
        "B": ("pullback_fraction", (0.015, 0.025)),
        "C": ("oversold_level", (25.0, 35.0)),
        "D": ("revision_min", (0.01, 0.03)),
        "E": ("debt_to_assets_max", (0.50, 0.70)),
        "F": ("relative_strength_min", (0.02, 0.04)),
    }[strategy.key]
    return [replace(strategy, **{field: value}) for value in values]


def default_config_for_strategy(key: str) -> EngineConfig:
    """Research defaults, stored before testing; they make no performance claim."""
    strategy = get_strategy(key)
    exits = {
        "A": ExitRules(stop_pct=0.04, reward_risk=2.0, trailing_pct=0.04),
        "B": ExitRules(stop_mode="atr", atr_multiple=2.0, reward_risk=2.0, trailing_pct=0.04),
        "C": ExitRules(stop_pct=0.03, target_mode="fixed", target_pct=0.05,
                       partial_at_r=1.0, trailing_pct=0.03),
        "D": ExitRules(stop_pct=0.05, reward_risk=2.0, trailing_pct=0.05),
        "E": ExitRules(stop_mode="atr", atr_multiple=3.0, reward_risk=3.0,
                       partial_at_r=2.0, trailing_pct=0.15),
        "F": ExitRules(stop_mode="atr", atr_multiple=2.0, reward_risk=2.0, trailing_pct=0.05),
    }[strategy.key]
    return EngineConfig(exits=exits)


def strategy_catalog() -> list[dict]:
    horizons = {"A": "1~5거래일", "B": "1~5거래일", "C": "1~3거래일",
                "D": "1~5거래일", "E": "252~1260거래일 연구, 손절은 조기 실행", "F": "1~5거래일"}
    return [{"key": item.key, "name": item.name, "version": item.version,
             "max_holding_bars": item.max_holding_bars, "horizon": horizons[item.key],
             "requires_fundamentals": item.requires_fundamentals,
             "requires_sectors": item.requires_sectors,
             "parameters": asdict(item),
             "entry_price": "신호 다음 거래 가능 봉 시가 + 불리한 슬리피지",
             "exit_rules": asdict(default_config_for_strategy(item.key).exits)}
            for item in get_strategies()]
