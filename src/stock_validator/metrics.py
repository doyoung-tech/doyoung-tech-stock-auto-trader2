"""Performance math and a second, fill-based accounting implementation.

Returns and drawdowns are fractions. Monetary statistics are KRW. Undefined
ratios are None, never infinity or a misleading zero.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import ceil, isfinite, sqrt
from random import Random
from statistics import mean
from typing import Any, Sequence

from .models import BacktestResult, Dataset, Trade


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def trade_statistics(trades: Sequence[Trade]) -> dict[str, Any]:
    pnls = [t.net_pnl_krw for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = mean(wins) if wins else None
    avg_loss = mean(losses) if losses else None
    return {
        "trade_count": len(trades), "win_rate": _ratio(len(wins), len(trades)),
        "avg_win": avg_win, "avg_loss": avg_loss,
        "average_pnl": mean(pnls) if pnls else None,
        "expectancy": mean(pnls) if pnls else None,
        "profit_factor": _ratio(sum(wins), abs(sum(losses))),
        "payoff_ratio": _ratio(avg_win, abs(avg_loss)) if avg_win is not None and avg_loss is not None else None,
        "realized_pnl": sum(pnls), "monetary_unit": "KRW",
        "average_return": mean(t.return_pct for t in trades) if trades else None,
        "wins": len(wins), "losses": len(losses), "breakeven": len(pnls) - len(wins) - len(losses),
    }


def performance_metrics(result: BacktestResult, dataset: Dataset | None = None) -> dict[str, Any]:
    stats = trade_statistics(result.trades)
    initial = result.config.risk.initial_cash_krw
    end = result.equity[-1].equity_krw if result.equity else initial
    years = ((result.equity[-1].timestamp - result.equity[0].timestamp).total_seconds() / 86400 / 365.2425) if len(result.equity) > 1 else 0
    peak, mdd = initial, 0.0
    for point in result.equity:
        peak = max(peak, point.equity_krw)
        mdd = min(mdd, point.equity_krw / peak - 1 if peak else 0)
    cagr = ((end / initial) ** (1 / years) - 1) if years >= 1 and end > 0 else None
    stats.update({
        "net_return": end / initial - 1, "cagr": cagr,
        "cagr_note": None if years >= 1 else "1년 미만 기간은 CAGR을 표시하지 않습니다.",
        "max_drawdown": mdd, "initial_equity": initial, "final_equity": end,
        "period_years": years, "open_position_count": len(result.open_positions),
        "period_start": result.equity[0].timestamp.isoformat() if result.equity else None,
        "period_end": result.equity[-1].timestamp.isoformat() if result.equity else None,
        "return_to_drawdown": _ratio(end / initial - 1, abs(mdd)),
        "calmar_ratio": _ratio(cagr, abs(mdd)) if cagr is not None else None,
        "open_exposure_krw": result.equity[-1].holdings_krw if result.equity else 0,
        "halted_reason": result.halted_reason,
    })
    if dataset is not None:
        stats["benchmark"] = benchmark_comparison(result, dataset)
    return stats


def benchmark_comparison(result: BacktestResult, dataset: Dataset) -> dict[str, Any]:
    if not result.equity:
        return {}
    start, end = result.equity[0].timestamp, result.equity[-1].timestamp
    grouped: dict[str, list] = defaultdict(list)
    for bar in dataset.benchmark_bars:
        if start <= bar.timestamp <= end:
            grouped[bar.symbol].append(bar)
    strategy_return = result.equity[-1].equity_krw / result.config.risk.initial_cash_krw - 1
    out = {}
    for symbol, bars in grouped.items():
        bars.sort(key=lambda b: b.timestamp)
        if len(bars) < 2:
            continue
        first, last = bars[0], bars[-1]
        # Comparisons must share endpoints; no silent shorter-period benchmark.
        if first.timestamp != start or last.timestamp != end:
            out[symbol] = {"net_return": None, "excess_return": None, "reason": "전략과 벤치마크 기간 경계 불일치"}
            continue
        value = last.close * last.fx_to_krw / (first.close * first.fx_to_krw) - 1
        out[symbol] = {"net_return": value, "excess_return": strategy_return - value,
                       "start": start.isoformat(), "end": end.isoformat(),
                       "basis": "KRW buy-and-hold; benchmark costs and unprovided distributions excluded"}
    return out


def regime_statistics(trades: Sequence[Trade]) -> dict[str, Any]:
    return {regime: trade_statistics([t for t in trades if t.regime == regime])
            for regime in ("bull", "bear", "sideways", "unknown")}


def block_bootstrap_expectancy(trades: Sequence[Trade], *, alpha: float = .05,
                               samples: int = 4999, seed: int = 1729,
                               max_holding_bars: int = 5,
                               trading_dates: Sequence[datetime] = ()) -> dict[str, Any]:
    """Cluster same-day exits and resample contiguous date blocks.

    The interval describes realized trade P/L, not future returns. It preserves
    nearby exit-day dependence approximately; structural market shifts remain.
    Insufficient tail resolution is explicitly unusable for a PASS decision.
    """
    if not 0 < alpha < 1:
        raise ValueError("alpha must lie in (0, 1)")
    dates = sorted({d.date() for d in trading_dates} | {t.exit_time.date() for t in trades})
    block_days = max(5, max_holding_bars + 1)
    base = {"method": "circular moving-block bootstrap of exit-day clusters",
            "alpha": alpha, "samples": samples, "seed": seed,
            "block_days": block_days, "trade_count": len(trades),
            "limitations": "과거 의존성을 근사하며 미래 수익과 구조 변화에 대한 보장이 아닙니다."}
    if len(trades) < 2 or len(dates) < 2 * block_days:
        return {**base, "lower": None, "upper": None, "usable": False,
                "reason": "독립 시간 블록 또는 거래 표본 부족"}
    clusters: dict[Any, list[float]] = defaultdict(list)
    for trade in trades:
        clusters[trade.exit_time.date()].append(trade.net_pnl_krw)
    n = len(dates)
    pnls = [sum(clusters[d]) for d in dates]
    counts = [len(clusters[d]) for d in dates]
    # Prefix sums over a doubled circular series keep resampling inexpensive.
    ps, cs = [0.0], [0]
    for pnl, count in zip(pnls * 2, counts * 2):
        ps.append(ps[-1] + pnl)
        cs.append(cs[-1] + count)
    random = Random(seed)
    values = []
    full_blocks, remainder = divmod(n, block_days)
    for _ in range(samples):
        total, count = 0.0, 0
        for _ in range(full_blocks):
            offset = random.randrange(n)
            total += ps[offset + block_days] - ps[offset]
            count += cs[offset + block_days] - cs[offset]
        if remainder:
            offset = random.randrange(n)
            total += ps[offset + remainder] - ps[offset]
            count += cs[offset + remainder] - cs[offset]
        if count:
            values.append(total / count)
    values.sort()
    if not values:
        return {**base, "lower": None, "upper": None, "usable": False, "reason": "재표집 유효 거래 없음"}
    tail = alpha / 2
    low = values[max(0, int(tail * len(values)))]
    high = values[min(len(values) - 1, ceil((1 - tail) * len(values)) - 1)]
    usable = tail * len(values) >= 5
    return {**base, "lower": low, "upper": high, "usable": usable,
            "reason": None if usable else "다중검정 보정 후 bootstrap 꼬리 해상도 부족",
            "effective_samples": len(values)}


def independent_audit(result: BacktestResult, dataset: Dataset | None = None) -> dict[str, Any]:
    """Reconstruct cash, cost basis, closed trades, and terminal valuation.

    This code deliberately shares no accounting helpers with the engine.
    """
    errors: list[str] = []
    cash = result.config.risk.initial_cash_krw
    inventory: dict[tuple[str, int], dict[str, float]] = {}
    all_cost: dict[int, float] = defaultdict(float)
    all_proceeds: dict[int, float] = defaultdict(float)
    total_bought: dict[int, int] = defaultdict(int)
    realized = 0.0
    previous_time = None
    snapshots = []
    for fill in result.fills:
        if previous_time is not None and fill.timestamp < previous_time:
            errors.append("체결 시각 역전")
        previous_time = fill.timestamp
        nums = (fill.quantity, fill.price, fill.fx_to_krw, fill.commission_krw, fill.tax_krw, fill.fx_cost_krw)
        if any(not isfinite(x) for x in nums) or not isinstance(fill.quantity, int) or isinstance(fill.quantity, bool) or fill.quantity <= 0 or fill.price <= 0 or fill.fx_to_krw <= 0 or any(x < 0 for x in nums[3:]):
            errors.append("유효하지 않은 체결 수치")
            continue
        key = (fill.symbol, fill.trade_id)
        notional = fill.quantity * fill.price * fill.fx_to_krw
        fees = fill.commission_krw + fill.tax_krw + fill.fx_cost_krw
        if fill.side == "BUY":
            entry = inventory.setdefault(key, {"quantity": 0, "cost": 0.0})
            entry["quantity"] += fill.quantity
            entry["cost"] += notional + fees
            cash -= notional + fees
            all_cost[fill.trade_id] += notional + fees
            total_bought[fill.trade_id] += fill.quantity
        elif fill.side == "SELL":
            entry = inventory.get(key)
            if entry is None or fill.quantity > entry["quantity"]:
                errors.append("보유량 초과 매도")
                continue
            allocated = entry["cost"] * fill.quantity / entry["quantity"]
            proceeds = notional - fees
            cash += proceeds
            realized += proceeds - allocated
            entry["quantity"] -= fill.quantity
            entry["cost"] -= allocated
            all_proceeds[fill.trade_id] += proceeds
        else:
            errors.append("알 수 없는 체결 방향")
        if cash < -.01:
            errors.append("음수 현금: 레버리지 발생")
        snapshots.append((fill.timestamp, cash, realized,
                          {key: dict(position) for key, position in inventory.items() if position["quantity"]}))
    seen = set()
    for trade in result.trades:
        if any(not isfinite(x) for x in (trade.entry_cost_krw, trade.net_pnl_krw, trade.return_pct)) or trade.entry_cost_krw <= 0:
            errors.append("유효하지 않은 거래 손익 수치")
        if trade.entry_time > trade.exit_time:
            errors.append("거래 진입·청산 시각 역전")
        if trade.trade_id in seen:
            errors.append("중복 완결 거래")
        seen.add(trade.trade_id)
        if any(k[1] == trade.trade_id and p["quantity"] > 0 for k, p in inventory.items()):
            errors.append("미청산 거래가 완결 거래로 표시됨")
        expected = all_proceeds[trade.trade_id] - all_cost[trade.trade_id]
        if abs(expected - trade.net_pnl_krw) > .01:
            errors.append("체결과 거래 손익 불일치")
        if abs(all_cost[trade.trade_id] - trade.entry_cost_krw) > .01 or total_bought[trade.trade_id] != trade.quantity:
            errors.append("거래 진입 원가 또는 수량 불일치")
        if trade.entry_cost_krw > 0 and abs(expected / trade.entry_cost_krw - trade.return_pct) > 1e-8:
            errors.append("거래 수익률 불일치")
    for trade_id in all_cost:
        if not any(k[1] == trade_id and p["quantity"] > 0 for k, p in inventory.items()) and trade_id not in seen:
            errors.append("완결 거래 기록 누락")
    declared_open = {}
    for position in result.open_positions:
        key = (position.get("symbol"), position.get("trade_id"))
        if key in declared_open:
            errors.append("중복 미청산 포지션")
        declared_open[key] = position
    actual_open = {key: position for key, position in inventory.items() if position["quantity"]}
    if set(declared_open) != set(actual_open):
        errors.append("미청산 포지션 기록 누락 또는 허위 기록")
    for key in set(declared_open) & set(actual_open):
        declared, actual = declared_open[key], actual_open[key]
        if declared.get("quantity") != actual["quantity"] or abs(declared.get("remaining_cost_krw", float("inf")) - actual["cost"]) > .01:
            errors.append("미청산 포지션 수량·원가 불일치")
    initial = result.config.risk.initial_cash_krw
    peak = initial
    snapshot_index, mark_index = 0, 0
    ledger_cash, ledger_realized, ledger_inventory = initial, 0.0, {}
    ordered_bars = sorted(dataset.bars, key=lambda b: b.timestamp) if dataset is not None else []
    marks_at_time = {}
    previous_equity_time = None
    for point in result.equity:
        if previous_equity_time is not None and point.timestamp <= previous_equity_time:
            errors.append("순자산 시각 중복 또는 역전")
        previous_equity_time = point.timestamp
        while snapshot_index < len(snapshots) and snapshots[snapshot_index][0] <= point.timestamp:
            _, ledger_cash, ledger_realized, ledger_inventory = snapshots[snapshot_index]
            snapshot_index += 1
        if abs(point.cash_krw - ledger_cash) > .01 or abs(point.realized_pnl_krw - ledger_realized) > .01:
            errors.append("중간 시점 체결 재계산 현금·실현손익 불일치")
        if dataset is not None:
            while mark_index < len(ordered_bars) and ordered_bars[mark_index].timestamp <= point.timestamp:
                bar = ordered_bars[mark_index]
                marks_at_time[bar.symbol] = bar.close * bar.fx_to_krw
                mark_index += 1
            expected_holdings, expected_basis = 0.0, 0.0
            for (symbol, _), position in ledger_inventory.items():
                if symbol not in marks_at_time:
                    errors.append("중간 시점 평가 가격 누락")
                else:
                    expected_holdings += position["quantity"] * marks_at_time[symbol]
                expected_basis += position["cost"]
            if abs(point.holdings_krw - expected_holdings) > .01 or abs(point.unrealized_pnl_krw - (expected_holdings - expected_basis)) > .01:
                errors.append("중간 시점 체결 재계산 보유평가·미실현손익 불일치")
        if any(not isfinite(x) for x in (point.cash_krw, point.holdings_krw, point.equity_krw, point.realized_pnl_krw, point.unrealized_pnl_krw)):
            errors.append("유효하지 않은 순자산 수치")
        if abs(point.cash_krw + point.holdings_krw - point.equity_krw) > .01:
            errors.append("현금+보유평가액 회계 불일치")
        if abs(initial + point.realized_pnl_krw + point.unrealized_pnl_krw - point.equity_krw) > .01:
            errors.append("실현+미실현손익 회계 불일치")
        peak = max(peak, point.equity_krw)
        expected_dd = point.equity_krw / peak - 1 if peak else 0
        if abs(expected_dd - point.drawdown) > 1e-8:
            errors.append("낙폭 계산 불일치")
    if result.equity:
        final = result.equity[-1]
        if any(fill.timestamp < result.equity[0].timestamp or fill.timestamp > final.timestamp for fill in result.fills):
            errors.append("순자산 관찰기간 밖 체결 기록")
        if abs(final.cash_krw - cash) > .01:
            errors.append("체결 재계산 현금 불일치")
        if abs(final.realized_pnl_krw - realized) > .01:
            errors.append("체결 재계산 실현손익 불일치")
        if dataset is not None:
            marks = {}
            for bar in sorted(dataset.bars, key=lambda b: b.timestamp):
                if bar.timestamp <= final.timestamp:
                    marks[bar.symbol] = bar.close * bar.fx_to_krw
            holdings = 0.0
            for (symbol, _), position in inventory.items():
                if position["quantity"]:
                    if symbol not in marks:
                        errors.append("최종 평가 가격 누락")
                    else:
                        holdings += position["quantity"] * marks[symbol]
            if abs(holdings - final.holdings_krw) > .01:
                errors.append("독립 최종 보유평가 불일치")
    elif result.fills:
        errors.append("체결이 있으나 순자산 기록 없음")
    return {"passed": not errors, "errors": sorted(set(errors)),
            "recomputed_final_cash": cash, "recomputed_realized_pnl": realized,
            "method": "independent fill-ledger reconstruction"}
