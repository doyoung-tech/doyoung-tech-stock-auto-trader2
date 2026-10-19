"""Synthetic examples verify rule logic, not achievable market performance."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from stock_validator.indicators import atr, ema, ema_series, macd, rsi, sma, true_ranges
from stock_validator.models import Bar, Fundamental, StrategyContext
from stock_validator.strategies import (
    DeepValueRecovery, EarningsImprovement, OversoldRebound, Pullback,
    SectorRelativeStrength, TrendBreakout, default_config_for_strategy,
    get_strategies, get_strategy, parameter_variants, strategy_catalog,
)


START = datetime(2000, 1, 1, tzinfo=timezone.utc)


def bars(closes, symbol="SYNTHETIC_TEST", volume=100):
    return [Bar(START + timedelta(days=index), symbol, value - 0.1,
                value + 0.2, value - 0.3, value, volume)
            for index, value in enumerate(closes)]


def statement(at, **kwargs):
    fields = dict(symbol="SYNTHETIC_TEST", available_at=at,
                  period_end=at - timedelta(days=30), revenue_growth=0.1,
                  earnings_growth=0.2, forecast_revision=0.1,
                  debt_to_assets=0.3, profitable=True, formerly_large_cap=True,
                  source="SYNTHETIC_UNIT_TEST_ONLY")
    fields.update(kwargs)
    return Fundamental(**fields)


def example(key):
    if key == "A":
        history = bars([100 + 0.2 * index for index in range(60)] + [115])
        history[-1] = replace(history[-1], volume=200)
    elif key == "B":
        history = bars([100 + 0.3 * index for index in range(70)] + [116, 120.5])
    elif key == "C":
        history = bars([120 - index for index in range(40)] + [82])
        history[-1] = replace(history[-1], volume=200)
    elif key == "E":
        history = bars([100] * 180 + [100 - 1.2 * index for index in range(70)]
                       + [10 + 0.15 * index for index in range(70)] + [22])
    else:
        history = bars([100 + 0.5 * index for index in range(80)])
    context = StrategyContext(history[-1].timestamp)
    if key in {"D", "E"}:
        context = replace(context, fundamentals=(statement(history[-1].timestamp),))
    if key == "F":
        context = replace(context,
                          sector_bars=tuple(bars([100 + 0.2 * index for index in range(80)], "SECTOR")),
                          benchmark=tuple(bars([100 + 0.05 * index for index in range(80)], "MARKET")))
    return history, context


def test_wilder_rsi_hand_calculation_and_edge_cases():
    # Changes +1,-1,+2; period2 mean gain .5 ->1.25, mean loss .5 ->.25.
    assert rsi([1, 2, 1, 3], 2) == pytest.approx(100 - 100 / 6)
    assert rsi([1, 2, 3], 2) == 100
    assert rsi([3, 2, 1], 2) == 0
    assert rsi([2, 2, 2], 2) == 50
    assert rsi([1, 2], 2) is None


def test_atr_gaps_and_wilder_average_hand_calculation():
    values = [Bar(START, "S", 10, 11, 9, 10, 1),
              Bar(START + timedelta(days=1), "S", 14, 15, 13, 14, 1),
              Bar(START + timedelta(days=2), "S", 10, 11, 9, 10, 1)]
    assert true_ranges(values) == [2, 5, 5]
    # First two TRs seed3.5; (3.5+5)/2=4.25.
    assert atr(values, 2) == 4.25
    assert atr(values[:1], 2) is None


def test_ema_sma_and_macd_defined_warmup():
    assert sma([1, 2, 3, 4], 3) == 3
    assert sma([1], 2) is None
    assert ema_series([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]
    assert ema([], 3) is None
    assert macd(list(range(30))) is None
    result = macd(list(range(1, 41)))
    assert result is not None
    assert result.line == pytest.approx(7)
    assert result.signal == pytest.approx(7)
    assert result.histogram == pytest.approx(0)


@pytest.mark.parametrize("function", [sma, ema, rsi])
def test_invalid_indicator_inputs_rejected(function):
    with pytest.raises(ValueError):
        function([1, float("nan")], 2)
    with pytest.raises(ValueError):
        function([1, 2], 0)


@pytest.mark.parametrize("key", list("ABCDEF"))
def test_each_strategy_has_positive_crafted_example_and_no_empty_signal(key):
    history, context = example(key)
    strategy = get_strategy(key)
    signal = strategy.signal(history, context)
    assert signal is not None, key
    assert signal.atr > 0
    assert strategy.signal([], context) is None
    assert strategy.signal(history[:5], context) is None


@pytest.mark.parametrize("key", list("ABCDEF"))
def test_future_prices_and_reports_cannot_change_prior_decision(key):
    history, context = example(key)
    strategy = get_strategy(key)
    before = strategy.signal(history, context)
    future = replace(history[-1], timestamp=context.decision_time + timedelta(days=1),
                     close=1_000_000, high=1_000_001, low=1, volume=1_000_000)
    future_report = statement(future.timestamp, revenue_growth=-1, formerly_large_cap=False)
    context_with_future = replace(context, fundamentals=context.fundamentals + (future_report,),
                                  sector_bars=context.sector_bars + ((replace(future, symbol="SECTOR"),)
                                      if context.sector_bars else ()),
                                  benchmark=context.benchmark + ((replace(future, symbol="MARKET"),)
                                      if context.benchmark else ()))
    assert strategy.signal(history + [future], context_with_future) == before


def test_breakout_resistance_excludes_signal_bar_and_volume_must_expand():
    history, context = example("A")
    assert history[-1].high > history[-1].close
    assert TrendBreakout().signal(history, context) is not None
    history[-1] = replace(history[-1], volume=100)
    assert TrendBreakout().signal(history, context) is None


def test_pullback_requires_prior_correction():
    history = bars([100 + 0.3 * index for index in range(80)])
    assert Pullback().signal(history, StrategyContext(history[-1].timestamp)) is None


def test_oversold_does_not_buy_during_continued_decline():
    history, context = example("C")
    history[-1] = replace(history[-1], close=79, low=78, open=80)
    assert OversoldRebound().signal(history, context) is None


@pytest.mark.parametrize("key", ["D", "E"])
def test_missing_fundamentals_are_not_invented(key):
    history, context = example(key)
    assert get_strategy(key).signal(history, replace(context, fundamentals=())) is None
    future = replace(context.fundamentals[0], available_at=context.decision_time + timedelta(seconds=1))
    assert get_strategy(key).signal(history, replace(context, fundamentals=(future,))) is None
    invalid_period = replace(context.fundamentals[0], period_end=context.decision_time + timedelta(days=1))
    assert get_strategy(key).signal(history, replace(context, fundamentals=(invalid_period,))) is None


def test_earnings_needs_genuine_forecast_revision():
    history, context = example("D")
    for revision in (None, -0.1, 0):
        report = replace(context.fundamentals[0], forecast_revision=revision)
        assert EarningsImprovement().signal(history, replace(context, fundamentals=(report,))) is None


@pytest.mark.parametrize("key,age", [("D", 181), ("E", 398)])
def test_stale_reports_do_not_count_as_current_quality_or_revision(key, age):
    history, context = example(key)
    report = statement(context.decision_time - timedelta(days=age))
    assert get_strategy(key).signal(history, replace(context, fundamentals=(report,))) is None


def test_recovery_needs_past_large_cap_profitable_solvent_business():
    history, context = example("E")
    for change in ({"formerly_large_cap": None}, {"formerly_large_cap": False},
                   {"profitable": False}, {"debt_to_assets": 0.9}, {"earnings_growth": -0.1}):
        report = replace(context.fundamentals[0], **change)
        assert DeepValueRecovery().signal(history, replace(context, fundamentals=(report,))) is None
    assert DeepValueRecovery().signal(history[-200:], context) is None


def test_sector_requires_matching_past_index_and_no_stale_benchmark():
    history, context = example("F")
    strategy = SectorRelativeStrength()
    assert strategy.signal(history, replace(context, sector_bars=())) is None
    assert strategy.signal(history, replace(context, benchmark=context.benchmark[:-1])) is None
    # Identical sector and market produces no excess return.
    assert strategy.signal(history, replace(context, sector_bars=context.benchmark)) is None


def test_catalog_neighbours_and_independent_exit_defaults():
    assert [item.key for item in get_strategies()] == list("ABCDEF")
    assert [row["key"] for row in strategy_catalog()] == list("ABCDEF")
    for strategy in get_strategies():
        assert len(parameter_variants(strategy.key)) == 2
        assert all(variant != strategy for variant in parameter_variants(strategy.key))
        assert strategy.max_holding_bars <= 5 or strategy.key == "E"
        with pytest.raises(FrozenInstanceError):
            strategy.max_holding_bars = 1
    assert default_config_for_strategy("E").exits.trailing_pct == 0.15
    assert default_config_for_strategy("C").exits.target_mode == "fixed"
    with pytest.raises(ValueError):
        get_strategy("Z")


def test_mixed_or_reordered_history_rejected_instead_of_silently_sorted():
    history, context = example("A")
    with pytest.raises(ValueError):
        TrendBreakout().signal(history[::-1], context)
    history[-1] = replace(history[-1], symbol="WRONG")
    with pytest.raises(ValueError):
        TrendBreakout().signal(history, context)


@pytest.mark.parametrize("constructor,kwargs", [
    (TrendBreakout, {"fast_period": 60}),
    (TrendBreakout, {"volume_multiple": 0}),
    (Pullback, {"max_holding_bars": 6}),
    (OversoldRebound, {"oversold_level": 101}),
    (EarningsImprovement, {"revision_min": float("nan")}),
    (DeepValueRecovery, {"max_holding_bars": 10}),
    (DeepValueRecovery, {"minimum_drawdown": 0.96}),
    (SectorRelativeStrength, {"comparison_period": 0}),
])
def test_invalid_strategy_parameters_rejected_before_experiments(constructor, kwargs):
    with pytest.raises(ValueError):
        constructor(**kwargs)
