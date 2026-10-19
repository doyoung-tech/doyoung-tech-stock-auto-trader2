"""Independent round-two adversarial checks; every fixture is fictional.

These tests inspect economic outcomes and tampered reports rather than copying
engine helpers. They do not establish the profitability of a market strategy.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from random import Random

import pytest

from stock_validator.engine import run_backtest
from stock_validator.metrics import independent_audit, performance_metrics
from stock_validator.models import (
    Bar, CostModel, Dataset, DatasetMeta, EngineConfig, EntrySignal, ExitRules, RiskLimits,
)

BASE = datetime(2020, 1, 1, 21, tzinfo=timezone.utc)
META = DatasetMeta("SYNTHETIC_ROUND_TWO_REVIEW", "2026-10-19", synthetic=True)


class BuyOnce:
    key, name, version = "review", "독립 회계검사 가상 전략", "1"
    max_holding_bars = 5
    requires_fundamentals = requires_sectors = False

    def signal(self, history, context):
        return EntrySignal("synthetic review only", 2) if len(history) == 1 else None


class Never(BuyOnce):
    def signal(self, history, context):
        return None


def bar(day, open=100, high=None, low=None, close=None, volume=100000, symbol="SYNTHETIC_X"):
    close = open if close is None else close
    return Bar(BASE + timedelta(days=day), symbol, open,
               max(open, close) if high is None else high,
               min(open, close) if low is None else low, close, volume)


def config():
    return EngineConfig(
        costs=CostModel(commission_bps=100, sell_tax_bps=200, slippage_bps=0, fx_bps=0),
        exits=ExitRules(stop_pct=.1, target_mode="fixed", target_pct=.2,
                        partial_take_fraction=.5, trailing_pct=None),
        risk=RiskLimits(initial_cash_krw=10000, planned_loss_per_trade_krw=10000,
                        daily_loss_limit_krw=100000, strategy_loss_limit_krw=100000,
                        account_loss_limit_krw=100000, volume_participation=1),
        market_filter=False,
    )


def test_independent_hand_ledger_with_partial_sale_and_both_side_costs():
    # Buy 99 shares at 100 +1% commission: cost 9999, cash1.
    # Sell49 at110 less1% commission+2% tax: proceeds5228.30, P/L279.30.
    # Sell50 at120 less3%: proceeds5820, P/L770. Total P/L1049.30.
    dataset = Dataset([bar(0), bar(1), bar(2, high=115, close=110), bar(3, open=120)], META)
    result = run_backtest(dataset, BuyOnce(), config())
    assert [fill.quantity for fill in result.fills] == [99, 49, 50]
    assert [fill.price for fill in result.fills] == pytest.approx([100, 110, 120])
    expected = [
        (10000, 0, 10000, 0, 0),
        (1, 9900, 9901, 0, -99),
        (5229.3, 5500, 10729.3, 279.3, 450),
        (11049.3, 0, 11049.3, 1049.3, 0),
    ]
    for point, manual in zip(result.equity, expected):
        assert (point.cash_krw, point.holdings_krw, point.equity_krw,
                point.realized_pnl_krw, point.unrealized_pnl_krw) == pytest.approx(manual)
    assert result.trades[0].net_pnl_krw == pytest.approx(1049.3)
    assert independent_audit(result, dataset)["passed"]
    metrics = performance_metrics(result)
    assert metrics["net_return"] == pytest.approx(.10493)
    assert metrics["max_drawdown"] == pytest.approx(-.0099)


@pytest.mark.parametrize("tamper", ["invented_mark", "cash_inventory_offset"])
def test_auditor_rejects_intermediate_curve_even_when_identities_and_terminal_are_consistent(tamper):
    dataset = Dataset([bar(0), bar(1), bar(2)], META)
    result = run_backtest(dataset, Never(), config())
    if tamper == "invented_mark":
        # Arithmetic identities and DD still match, but no shares ever existed.
        result.equity[1] = replace(result.equity[1], holdings_krw=1000,
                                   unrealized_pnl_krw=1000, equity_krw=11000)
        result.equity[2] = replace(result.equity[2], drawdown=10000 / 11000 - 1)
    else:
        # Equity is unchanged but this impossible intermediate cash is hidden
        # by offsetting a negative inventory mark.
        result.equity[1] = replace(result.equity[1], cash_krw=11000, holdings_krw=-1000)
    audit = independent_audit(result, dataset)
    assert not audit["passed"], audit


def test_future_price_changes_leave_completed_prefix_fills_and_equity_unchanged():
    original = [bar(0), bar(1), bar(2, high=115, close=110), bar(3, open=120), bar(4)]
    changed = original[:3] + [bar(3, open=10, high=20000, low=1, close=15000), bar(4, open=30000)]
    one = run_backtest(Dataset(original, META), BuyOnce(), config())
    two = run_backtest(Dataset(changed, META), BuyOnce(), config())
    cutoff = original[2].timestamp
    assert [fill for fill in one.fills if fill.timestamp <= cutoff] == [
        fill for fill in two.fills if fill.timestamp <= cutoff]
    assert [point for point in one.equity if point.timestamp <= cutoff] == [
        point for point in two.equity if point.timestamp <= cutoff]


def test_independent_ledger_survives_random_multiasset_gaps_halts_and_partial_volume():
    # Reproducible adversarial paths: this checks ledger consistency, not returns.
    for seed in range(20):
        random = Random(seed)
        rows = []
        prices = {"SYNTHETIC_X": 100., "SYNTHETIC_Y": 70.}
        for day in range(25):
            for symbol, previous in list(prices.items()):
                opening = max(1., previous * random.uniform(.65, 1.35))
                closing = max(1., opening * random.uniform(.8, 1.2))
                rows.append(bar(day, opening, max(opening, closing) * 1.1,
                                min(opening, closing) * .9, closing,
                                random.choice([0, 5, 20, 100]), symbol))
                prices[symbol] = closing
        dataset = Dataset(rows, META)
        result = run_backtest(dataset, BuyOnce(), config())
        audit = independent_audit(result, dataset)
        assert audit["passed"], (seed, audit)
        assert all(point.cash_krw >= -.01 for point in result.equity)
