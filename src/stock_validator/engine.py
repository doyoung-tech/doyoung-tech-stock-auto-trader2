"""Deterministic, research-only long equity simulator; this module has no order APIs.

Signals see closed bars and submit a one-bar entry intention. Entries execute at the
following available symbol bar's open. Stops and profit levels trigger simulated market exits (including adverse
slippage), and a daily volume budget is shared by both sides. There is no
synthetic terminal liquidation. All monetary accounting is in KRW.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from typing import Any

from .models import (
    BacktestResult, Bar, Dataset, EngineConfig, EntrySignal, EquityPoint,
    Fill, Strategy, StrategyContext, Trade,
)


@dataclass
class _Position:
    trade_id: int
    symbol: str
    entry_time: datetime
    quantity: int
    original_quantity: int
    original_cost: float
    remaining_cost: float
    entry_price: float
    stop: float
    target: float
    partial_target: float
    partial_remaining: int
    regime: str
    held: int = 1
    pnl: float = 0.0
    high_close: float = 0.0
    pending_exit: str | None = None


def _finite(value: float, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _validate(dataset: Dataset, strategy: Strategy, config: EngineConfig) -> None:
    r, c, e = config.risk, config.costs, config.exits
    for name in ("initial_cash_krw", "planned_loss_per_trade_krw", "daily_loss_limit_krw",
                 "strategy_loss_limit_krw", "account_loss_limit_krw"):
        _finite(getattr(r, name), name, positive=True)
    if isinstance(r.max_positions, bool) or not isinstance(r.max_positions, int) or r.max_positions <= 0:
        raise ValueError("max_positions must be a positive integer")
    _finite(r.volume_participation, "volume_participation", positive=True)
    if r.volume_participation > 1:
        raise ValueError("volume_participation must not exceed one")
    for name in ("commission_bps", "sell_tax_bps", "slippage_bps", "fx_bps"):
        value = getattr(c, name)
        _finite(value, name)
        if not 0 <= value < 10_000:
            raise ValueError(f"{name} must be in [0, 10000)")
    if c.commission_bps + c.sell_tax_bps + c.fx_bps >= 10_000:
        raise ValueError("combined sell costs must be less than 10000 bps")
    if e.stop_mode not in ("fixed", "atr") or e.target_mode not in ("fixed", "reward_risk"):
        raise ValueError("unsupported stop_mode or target_mode")
    for name in ("stop_pct", "atr_multiple", "target_pct", "reward_risk", "partial_at_r"):
        _finite(getattr(e, name), name, positive=True)
    if e.stop_pct >= 1:
        raise ValueError("stop_pct must be less than one")
    _finite(e.partial_take_fraction, "partial_take_fraction")
    if not 0 <= e.partial_take_fraction < 1:
        raise ValueError("partial_take_fraction must be in [0, 1)")
    if e.trailing_pct is not None:
        _finite(e.trailing_pct, "trailing_pct", positive=True)
        if e.trailing_pct >= 1:
            raise ValueError("trailing_pct must be less than one")
    if not isinstance(config.market_filter, bool):
        raise ValueError("market_filter must be a boolean")
    if (isinstance(strategy.max_holding_bars, bool) or
            not isinstance(strategy.max_holding_bars, int) or strategy.max_holding_bars <= 0):
        raise ValueError("max_holding_bars must be a positive integer")
    for stream_name in ("bars", "benchmark_bars", "sector_bars"):
        seen: set[tuple[str, datetime]] = set()
        currencies: dict[str, str] = {}
        for b in getattr(dataset, stream_name, []):
            _aware(b.timestamp, f"{stream_name}.timestamp")
            if (not isinstance(b.symbol, str) or not b.symbol or
                    not isinstance(b.currency, str) or not b.currency or not isinstance(b.tradable, bool)):
                raise ValueError("symbol, currency and boolean tradable are required")
            if b.symbol in currencies and currencies[b.symbol] != b.currency:
                raise ValueError(f"currency changed within symbol {b.symbol}")
            currencies[b.symbol] = b.currency
            for field in ("open", "high", "low", "close", "fx_to_krw"):
                _finite(getattr(b, field), f"{b.symbol}.{field}", positive=True)
            _finite(b.volume, f"{b.symbol}.volume")
            if b.volume < 0 or b.high < max(b.open, b.close) or b.low > min(b.open, b.close) or b.low > b.high:
                raise ValueError(f"invalid OHLCV: {b.symbol} {b.timestamp.isoformat()}")
            if b.currency == "KRW" and not math.isclose(b.fx_to_krw, 1.0):
                raise ValueError("KRW bars must have fx_to_krw=1")
            key = (b.symbol, b.timestamp)
            if key in seen:
                raise ValueError(f"duplicate {stream_name} bar: {key}")
            seen.add(key)
    actual_sessions = {(b.symbol, b.timestamp) for b in dataset.bars}
    for symbol, sessions in dataset.missing_sessions.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("missing session requires a nonempty symbol")
        seen_missing: set[datetime] = set()
        for ts in sessions:
            _aware(ts, "missing_session.timestamp")
            if ts in seen_missing or (symbol, ts) in actual_sessions:
                raise ValueError("duplicate or conflicting missing session")
            seen_missing.add(ts)
    for f in dataset.fundamentals:
        _aware(f.available_at, "fundamental.available_at")
        _aware(f.period_end, "fundamental.period_end")
    for memberships in dataset.sectors.values():
        for start, end, _ in memberships:
            _aware(start, "sector.effective_from")
            if end is not None:
                _aware(end, "sector.effective_to")
                if end <= start:
                    raise ValueError("sector effective_to must follow effective_from")


def _regime(benchmark: tuple[Bar, ...]) -> str:
    if len(benchmark) < 60:
        return "unknown"
    closes = [b.close for b in benchmark[-60:]]
    slow, fast = sum(closes) / 60, sum(closes[-20:]) / 20
    if closes[-1] > slow and fast > slow:
        return "bull"
    if closes[-1] < slow and fast < slow:
        return "bear"
    return "sideways"


def run_backtest(dataset: Dataset, strategy: Strategy,
                 config: EngineConfig = EngineConfig()) -> BacktestResult:
    """Run without mutating the dataset, with conservative OHLC execution.

    ``return_pct`` and ``drawdown`` are fractions. A holding limit of N means
    exit at the next open after N completed symbol bars. UTC calendar dates
    define daily risk buckets, including simultaneous KR/US portfolios. A
    Trade.holding_bars value counts completed bars for an opening exit, and
    includes the current bar for an intrabar exit (minimum one). Unavailable
    provider sessions cancel entries and retain stale marks, with no invented
    trading bar or assumed halt; the holding clock counts observed bars only.
    The previous observed close FX is used for open/intrabar conversion.
    Current close FX only marks closing NAV; intraday FX cannot be inferred.
    """
    _validate(dataset, strategy, config)
    result = BacktestResult(strategy.key, strategy.version, config)
    risk, costs, exits = config.risk, config.costs, config.exits
    cash = float(risk.initial_cash_krw)
    realized = 0.0
    peak = cash
    close_peak = cash
    previous_equity = cash
    daily_start = cash
    current_date = None
    daily_blocked = False
    positions: dict[str, _Position] = {}
    marks: dict[str, tuple[float, float]] = {}
    pending: dict[str, tuple[EntrySignal, str, bool]] = {}
    history: dict[str, list[Bar]] = defaultdict(list)
    groups: dict[datetime, list[Bar]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)
    for b in dataset.bars:
        groups[b.timestamp].append(b)
        counts[b.symbol] += 1
    missing: dict[datetime, list[str]] = defaultdict(list)
    for symbol, sessions in dataset.missing_sessions.items():
        for ts in sessions:
            missing[ts].append(symbol)
            groups.setdefault(ts, [])
    benchmark = tuple(sorted(dataset.benchmark_bars, key=lambda b: (b.timestamp, b.symbol)))
    # One benchmark stream is required. Mixing indices would create invalid MAs.
    if len({b.symbol for b in benchmark}) > 1:
        raise ValueError("benchmark_bars must contain one benchmark symbol per run")
    benchmark_times = [b.timestamp for b in benchmark]
    sector_map: dict[str, list[Bar]] = defaultdict(list)
    for b in sorted(getattr(dataset, "sector_bars", []), key=lambda b: b.timestamp):
        sector_map[b.symbol].append(b)
    sector_times = {key: [b.timestamp for b in bars] for key, bars in sector_map.items()}
    fundamentals: dict[str, list[Any]] = defaultdict(list)
    for f in sorted(dataset.fundamentals, key=lambda f: f.available_at):
        if f.period_end <= f.available_at:
            fundamentals[f.symbol].append(f)
    fundamental_times = {key: [f.available_at for f in rows] for key, rows in fundamentals.items()}
    next_trade_id = 1
    slip = costs.slippage_bps / 10_000

    def context(symbol: str, ts: datetime) -> StrategyContext:
        bm = benchmark[:bisect_right(benchmark_times, ts)]
        fs = fundamentals[symbol][:bisect_right(fundamental_times.get(symbol, []), ts)]
        active = [sector for start, end, sector in dataset.sectors.get(symbol, [])
                  if start <= ts and (end is None or ts < end)]
        if len(set(active)) > 1:
            raise ValueError(f"overlapping point-in-time sector memberships for {symbol}")
        sector = active[0] if active else None
        sb = () if sector is None else tuple(sector_map[sector][:bisect_right(sector_times.get(sector, []), ts)])
        return StrategyContext(ts, tuple(fs), bm, sb, dataset.meta)

    def fee_values(b: Bar, qty: int, fill_price: float, side: str) -> tuple[float, float, float, float]:
        gross = qty * fill_price * b.fx_to_krw
        commission = gross * costs.commission_bps / 10_000
        tax = gross * costs.sell_tax_bps / 10_000 if side == "SELL" else 0.0
        fx_cost = gross * costs.fx_bps / 10_000 if b.currency != "KRW" else 0.0
        return gross, commission, tax, fx_cost

    def totals() -> tuple[float, float, float]:
        holdings = sum(p.quantity * marks[s][0] * marks[s][1] for s, p in positions.items())
        unrealized = sum(p.quantity * marks[s][0] * marks[s][1] - p.remaining_cost for s, p in positions.items())
        equity = cash + holdings
        if not all(math.isfinite(x) for x in (cash, holdings, unrealized, equity, realized)):
            raise ArithmeticError("nonfinite accounting value")
        if cash < -1e-6 or any(p.quantity <= 0 for p in positions.values()):
            raise ArithmeticError("cash/inventory invariant violated")
        if not math.isclose(equity, risk.initial_cash_krw + realized + unrealized, rel_tol=1e-10, abs_tol=1e-6):
            raise ArithmeticError("initial + realized + unrealized != equity")
        return holdings, unrealized, equity

    def update_risk() -> None:
        nonlocal peak, daily_blocked
        _, _, equity = totals()
        peak = max(peak, equity)
        if daily_start - equity >= risk.daily_loss_limit_krw - 1e-8:
            daily_blocked = True
        if result.halted_reason is None:
            if risk.initial_cash_krw - equity >= risk.account_loss_limit_krw - 1e-8:
                result.halted_reason = "account_loss_limit"
            elif peak - equity >= risk.strategy_loss_limit_krw - 1e-8:
                result.halted_reason = "strategy_drawdown_limit"
        if result.halted_reason:
            for p in positions.values():
                p.pending_exit = "risk_halt"

    def reject(b: Bar, side: str, reason: str, requested: int = 0, filled: int = 0) -> None:
        result.rejected_orders.append({"timestamp": b.timestamp.isoformat(), "symbol": b.symbol,
                                       "side": side, "reason": reason,
                                       "requested_quantity": requested, "filled_quantity": filled})

    def sell(b: Bar, wanted: int, reference: float, reason: str,
             capacity: dict[str, int], *, persistent: bool = False, at_open: bool = False) -> int:
        nonlocal cash, realized
        p = positions[b.symbol]
        wanted = min(wanted, p.quantity)
        available = capacity[b.symbol] if b.tradable else 0
        qty = min(wanted, available)
        if qty == 0:
            reject(b, "SELL", "halt_or_zero_volume" if not b.tradable or b.volume == 0 else "volume_limit", wanted)
            if persistent:
                p.pending_exit = reason
            return 0
        fill_price = reference * (1 - slip)
        gross, commission, tax, fx_cost = fee_values(b, qty, fill_price, "SELL")
        allocated = p.remaining_cost * qty / p.quantity
        proceeds = gross - commission - tax - fx_cost
        pnl = proceeds - allocated
        cash += proceeds
        realized += pnl
        p.pnl += pnl
        p.remaining_cost -= allocated
        p.quantity -= qty
        capacity[b.symbol] -= qty
        result.fills.append(Fill(b.timestamp, b.symbol, "SELL", qty, fill_price, b.fx_to_krw,
                                 commission, tax, fx_cost, reason, p.trade_id))
        if qty < wanted:
            reject(b, "SELL", "partial_fill", wanted, qty)
            if persistent:
                p.pending_exit = reason
        if p.quantity == 0:
            result.trades.append(Trade(p.trade_id, p.symbol, p.entry_time, b.timestamp,
                                       p.original_quantity, p.original_cost, p.pnl,
                                       p.pnl / p.original_cost, max(1, p.held - 1) if at_open else p.held, reason, p.regime))
            del positions[b.symbol]
        totals()
        return qty

    for ts in sorted(groups):
        close_bars = sorted(groups[ts], key=lambda b: b.symbol)
        bars = [replace(b, fx_to_krw=history[b.symbol][-1].fx_to_krw)
                if history[b.symbol] else b for b in close_bars]
        date = ts.astimezone(timezone.utc).date()
        if date != current_date:
            current_date, daily_start, daily_blocked = date, previous_equity, False
        for symbol in sorted(missing.get(ts, [])):
            if pending.pop(symbol, None) is not None:
                result.rejected_orders.append({"timestamp": ts.isoformat(), "symbol": symbol,
                    "side": "BUY", "reason": "data_missing",
                    "requested_quantity": 0, "filled_quantity": 0})
        capacity = {b.symbol: (math.floor(b.volume * risk.volume_participation) if b.tradable else 0) for b in bars}
        # Mark at opens first; using today's close here would leak into entries.
        for b in bars:
            marks[b.symbol] = (b.open, b.fx_to_krw)
            if b.symbol in positions:
                positions[b.symbol].held += 1
        update_risk()
        # Opening forced exits and overnight barrier gaps precede new buys.
        for b in bars:
            p = positions.get(b.symbol)
            if p is None:
                continue
            if p.pending_exit or p.held > strategy.max_holding_bars:
                sell(b, p.quantity, b.open, p.pending_exit or "time_exit", capacity, persistent=True, at_open=True)
            elif b.open <= p.stop:
                sell(b, p.quantity, b.open, "gap_stop", capacity, persistent=True, at_open=True)
            elif b.open >= p.target:
                sell(b, p.quantity, max(b.open, p.target), "gap_target", capacity, at_open=True)
            elif p.partial_remaining and b.open >= p.partial_target:
                sold = sell(b, p.partial_remaining, b.open, "partial_target", capacity, at_open=True)
                if b.symbol in positions:
                    positions[b.symbol].partial_remaining -= sold
            update_risk()
        # Entry intention lasts one next symbol bar; a rejection never becomes
        # a stale instruction to enter several days later.
        for b in bars:
            instruction = pending.pop(b.symbol, None)
            if instruction is None:
                continue
            sig, regime, market_blocked = instruction
            if b.symbol in positions:
                reject(b, "BUY", "already_holding")
                continue
            if result.halted_reason or daily_blocked or market_blocked:
                reject(b, "BUY", result.halted_reason or ("daily_loss_limit" if daily_blocked else "market_filter"))
                continue
            if len(positions) >= risk.max_positions:
                reject(b, "BUY", "max_positions")
                continue
            if not b.tradable or capacity[b.symbol] <= 0:
                reject(b, "BUY", "halt_or_zero_volume" if not b.tradable or b.volume == 0 else "volume_limit")
                continue
            price = b.open * (1 + slip)
            distance = price * exits.stop_pct if exits.stop_mode == "fixed" else exits.atr_multiple * sig.atr
            stop = price - distance
            if not (math.isfinite(distance) and 0 < stop < price):
                reject(b, "BUY", "invalid_stop_distance")
                continue
            entry_gross, ec, _, ef = fee_values(b, 1, price, "BUY")
            stop_gross, sc, st, sf = fee_values(b, 1, stop * (1 - slip), "SELL")
            unit_cost = entry_gross + ec + ef
            unit_risk = unit_cost - (stop_gross - sc - st - sf)
            if not math.isfinite(unit_cost) or not math.isfinite(unit_risk) or unit_risk <= 0:
                raise ValueError("invalid unit cost or planned risk")
            intended = min(math.floor((cash + 1e-9) / unit_cost),
                           math.floor((risk.planned_loss_per_trade_krw + 1e-9) / unit_risk),
                           math.floor(history[b.symbol][-1].volume * risk.volume_participation))
            qty = min(intended, capacity[b.symbol])
            if qty <= 0:
                reject(b, "BUY", "cash_or_planned_loss_budget", intended)
                continue
            total = qty * unit_cost
            _, commission, tax, fx_cost = fee_values(b, qty, price, "BUY")
            target = price * (1 + exits.target_pct) if exits.target_mode == "fixed" else price + distance * exits.reward_risk
            partial_qty = math.floor(qty * exits.partial_take_fraction)
            if not math.isfinite(target):
                raise ValueError("target price must be finite")
            p = _Position(next_trade_id, b.symbol, ts, qty, qty, total, total,
                          price, stop, target, price + distance * exits.partial_at_r,
                          partial_qty, regime, high_close=price)
            next_trade_id += 1
            positions[b.symbol] = p
            cash -= total
            capacity[b.symbol] -= qty
            result.fills.append(Fill(ts, b.symbol, "BUY", qty, price, b.fx_to_krw,
                                     commission, tax, fx_cost, sig.reason, p.trade_id))
            if qty < intended:
                reject(b, "BUY", "partial_fill", intended, qty)
            update_risk()
        # Intra-bar ambiguity is resolved against the strategy. A stop touched
        # with a target always wins; today's high cannot tighten today's stop.
        for b in bars:
            p = positions.get(b.symbol)
            if p is None:
                continue
            if p.pending_exit and capacity[b.symbol] > 0:
                # Opening attempts already exhausted this bar's executable
                # volume. A new post-entry risk halt also exits at the open.
                sell(b, p.quantity, b.open, p.pending_exit, capacity, persistent=True, at_open=True)
            elif p.pending_exit:
                continue
            elif b.low <= p.stop:
                sell(b, p.quantity, min(b.open, p.stop), "stop", capacity, persistent=True)
            else:
                if p.partial_remaining and b.high >= p.partial_target and p.partial_target < p.target:
                    sold = sell(b, p.partial_remaining, p.partial_target, "partial_target", capacity)
                    if b.symbol in positions:
                        positions[b.symbol].partial_remaining -= sold
                p = positions.get(b.symbol)
                if p is not None and b.high >= p.target:
                    sell(b, p.quantity, p.target, "target", capacity)
        # Now close marks and new trailing stops are observable. They are used
        # only on the next bar, never against today's already-traversed low.
        for b in close_bars:
            marks[b.symbol] = (b.close, b.fx_to_krw)
            p = positions.get(b.symbol)
            if p is not None:
                p.high_close = max(p.high_close, b.close)
                if exits.trailing_pct is not None:
                    p.stop = max(p.stop, p.high_close * (1 - exits.trailing_pct))
            history[b.symbol].append(b)
        update_risk()
        holdings, unrealized, equity = totals()
        close_peak = max(close_peak, equity)
        result.equity.append(EquityPoint(ts, cash, holdings, equity, realized, unrealized,
                                         equity / close_peak - 1))
        previous_equity = equity
        for b in bars:
            if b.symbol in positions or len(history[b.symbol]) >= counts[b.symbol]:
                continue
            ctx = context(b.symbol, ts)
            sig = strategy.signal(tuple(history[b.symbol]), ctx)
            if sig is None:
                continue
            if not isinstance(sig, EntrySignal):
                raise ValueError("strategy must return EntrySignal or None")
            _finite(sig.atr, "signal.atr", positive=True)
            regime = _regime(ctx.benchmark)
            pending[b.symbol] = (sig, regime, config.market_filter and regime == "bear")
    for symbol, p in sorted(positions.items()):
        mark, fx = marks[symbol]
        result.open_positions.append({"trade_id": p.trade_id, "symbol": symbol, "quantity": p.quantity,
            "entry_time": p.entry_time.isoformat(), "entry_price": p.entry_price,
            "remaining_cost_krw": p.remaining_cost, "market_value_krw": p.quantity * mark * fx,
            "unrealized_pnl_krw": p.quantity * mark * fx - p.remaining_cost,
            "pending_exit": p.pending_exit, "stop": p.stop, "target": p.target})
    if dataset.missing_sessions:
        count = sum(len(rows) for rows in dataset.missing_sessions.values())
        result.warnings.append(f"제공처 OHLCV 미확인 {count}개 시각: 해당 진입은 취소하며 거래정지로 단정하지 않습니다. 보유분은 최종 관측가로 평가하고 보유기간 제한은 실제 경과기간보다 짧게 계산될 수 있습니다.")
    if result.open_positions:
        result.warnings.append("미청산 보유분은 마지막 관측가로 평가했습니다. 향후 매도비용은 아직 차감되지 않았습니다.")
    if any(b.currency != "KRW" for b in dataset.bars):
        result.warnings.append("체결 환율은 직전 관측 종가 환율을 사용하고 현재 종가 환율은 마감 평가에만 사용했습니다. 장중 환전 환율 차이와 갭 위험이 있습니다.")
    result.warnings.append("갭·거래정지·유동성 부족으로 실제 손실은 계획 손절 및 위험 한도를 초과할 수 있습니다.")
    result.warnings.append("봉 전체 거래량에 참여율을 적용한 체결 상한은 시가 유동성의 근사이며 실제 시가 체결량을 보장하지 않습니다.")
    result.warnings.append("손절·목표가는 시장가 청산의 트리거이며 모든 체결에 불리한 슬리피지를 반영합니다. 전략 중단은 시가·종가 관측 고점, 성과 낙폭은 종가 고점 기준입니다.")
    result.accounting_verified = True
    return result
