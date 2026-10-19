"""Shared immutable data contracts. All decision timestamps must be timezone aware."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    currency: str = "KRW"
    fx_to_krw: float = 1.0
    tradable: bool = True


@dataclass(frozen=True)
class Fundamental:
    symbol: str
    available_at: datetime
    period_end: datetime
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    forecast_revision: float | None = None
    debt_to_assets: float | None = None
    profitable: bool | None = None
    market_cap_krw: float | None = None
    formerly_large_cap: bool | None = None
    source: str = ""


@dataclass(frozen=True)
class DatasetMeta:
    source: str
    retrieved_at: str
    synthetic: bool = False
    price_basis: str = "raw"
    survivorship_bias_controlled: bool = False
    point_in_time_universe: bool = False
    corporate_actions_complete: bool = False
    point_in_time_fundamentals: bool = False
    sector_history_complete: bool = False
    fx_complete: bool = False
    license_note: str = ""
    sha256: str = ""
    limitations: tuple[str, ...] = ()


@dataclass
class Dataset:
    bars: list[Bar]
    meta: DatasetMeta
    fundamentals: list[Fundamental] = field(default_factory=list)
    benchmark_bars: list[Bar] = field(default_factory=list)
    # symbol -> [(effective_from, effective_to, sector)] ; no present-day backfill.
    sectors: dict[str, list[tuple[datetime, datetime | None, str]]] = field(default_factory=dict)
    sector_bars: list[Bar] = field(default_factory=list)
    # Provider returned a session with unavailable OHLCV. Never manufacture a price.
    missing_sessions: dict[str, list[datetime]] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyContext:
    decision_time: datetime
    fundamentals: tuple[Fundamental, ...] = ()
    benchmark: tuple[Bar, ...] = ()
    sector_bars: tuple[Bar, ...] = ()
    metadata: DatasetMeta | None = None


@dataclass(frozen=True)
class EntrySignal:
    reason: str
    atr: float


class Strategy(Protocol):
    key: str
    name: str
    version: str
    max_holding_bars: int
    requires_fundamentals: bool
    requires_sectors: bool

    def signal(self, history: Sequence[Bar], context: StrategyContext) -> EntrySignal | None: ...


@dataclass(frozen=True)
class ExitRules:
    stop_mode: str = "fixed"
    stop_pct: float = 0.04
    atr_multiple: float = 2.0
    target_mode: str = "reward_risk"
    target_pct: float = 0.08
    reward_risk: float = 2.0
    partial_take_fraction: float = 0.5
    partial_at_r: float = 1.0
    trailing_pct: float | None = 0.04


@dataclass(frozen=True)
class CostModel:
    # Conservative research assumptions, NOT a statement of any current tax law.
    commission_bps: float = 5.0
    sell_tax_bps: float = 20.0
    slippage_bps: float = 10.0
    fx_bps: float = 10.0


@dataclass(frozen=True)
class RiskLimits:
    initial_cash_krw: float = 1_000_000.0
    planned_loss_per_trade_krw: float = 10_000.0
    daily_loss_limit_krw: float = 20_000.0
    strategy_loss_limit_krw: float = 50_000.0
    account_loss_limit_krw: float = 1_000_000.0
    max_positions: int = 5
    volume_participation: float = 0.01


@dataclass(frozen=True)
class EngineConfig:
    exits: ExitRules = field(default_factory=ExitRules)
    costs: CostModel = field(default_factory=CostModel)
    risk: RiskLimits = field(default_factory=RiskLimits)
    market_filter: bool = True


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    symbol: str
    side: str
    quantity: int
    price: float
    fx_to_krw: float
    commission_krw: float
    tax_krw: float
    fx_cost_krw: float
    reason: str
    trade_id: int


@dataclass(frozen=True)
class Trade:
    trade_id: int
    symbol: str
    entry_time: datetime
    exit_time: datetime
    quantity: int
    entry_cost_krw: float
    net_pnl_krw: float
    return_pct: float
    holding_bars: int
    exit_reason: str
    regime: str


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    cash_krw: float
    holdings_krw: float
    equity_krw: float
    realized_pnl_krw: float
    unrealized_pnl_krw: float
    drawdown: float


@dataclass
class BacktestResult:
    strategy_key: str
    strategy_version: str
    config: EngineConfig
    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected_orders: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    halted_reason: str | None = None
    accounting_verified: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)
