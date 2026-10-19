"""Small manual ledgers and adversarial execution cases (synthetic fixtures only)."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import math
import unittest

from stock_validator.engine import run_backtest
from stock_validator.models import (
    Bar, CostModel, Dataset, DatasetMeta, EngineConfig, EntrySignal, ExitRules,
    Fundamental, RiskLimits,
)

BASE = datetime(2020, 1, 1, 21, tzinfo=timezone.utc)
META = DatasetMeta("unit-test-only synthetic fixture", "2026-10-19", synthetic=True)


def bar(day, open=100, high=None, low=None, close=None, volume=10000, symbol="X", **kw):
    close = open if close is None else close
    return Bar(BASE + timedelta(days=day), symbol, open,
               max(open, close) if high is None else high,
               min(open, close) if low is None else low, close, volume, **kw)


class Scripted:
    key, name, version = "test", "Synthetic test", "1"
    max_holding_bars = 50
    requires_fundamentals = requires_sectors = False

    def __init__(self, signal_lengths=(1,)):
        self.signal_lengths = signal_lengths
        self.seen = []

    def signal(self, history, context):
        self.seen.append((history, context))
        return EntrySignal("explicit fixture", 2) if len(history) in self.signal_lengths else None


def cfg(**risk_kw):
    r = RiskLimits(initial_cash_krw=10000, planned_loss_per_trade_krw=1000,
                   daily_loss_limit_krw=100000, strategy_loss_limit_krw=100000,
                   account_loss_limit_krw=100000, max_positions=5, volume_participation=1)
    return EngineConfig(
        exits=ExitRules(stop_pct=.1, target_mode="fixed", target_pct=.2,
                        partial_take_fraction=0, trailing_pct=None),
        costs=CostModel(0, 0, 0, 0), risk=replace(r, **risk_kw), market_filter=False)


def run(bars, config=None, strategy=None, **dataset_kw):
    return run_backtest(Dataset(bars, META, **dataset_kw), strategy or Scripted(), config or cfg())


class EngineLedgerTests(unittest.TestCase):
    def test_hand_calculated_fees_tax_slippage_and_fx(self):
        config = cfg(initial_cash_krw=100000, planned_loss_per_trade_krw=100000)
        config = replace(config, costs=CostModel(100, 200, 100, 100),
                         exits=replace(config.exits, stop_pct=.04, target_pct=.10))
        result = run([
            bar(0, 10, currency="USD", fx_to_krw=1000),
            bar(1, 10, high=11, close=11, currency="USD", fx_to_krw=1200),
            bar(2, 12, currency="USD", fx_to_krw=1300),
        ], config)
        buy, sell = result.fills
        self.assertEqual(buy.quantity, 9)
        self.assertAlmostEqual(buy.price, 10.1)
        self.assertEqual(buy.fx_to_krw, 1000)
        self.assertAlmostEqual(buy.commission_krw, 909)
        self.assertAlmostEqual(buy.fx_cost_krw, 909)
        self.assertEqual(buy.tax_krw, 0)
        self.assertEqual(sell.fx_to_krw, 1200)
        self.assertAlmostEqual(sell.price, 11.88)
        self.assertAlmostEqual(sell.commission_krw, 1283.04)
        self.assertAlmostEqual(sell.tax_krw, 2566.08)
        self.assertAlmostEqual(sell.fx_cost_krw, 1283.04)
        self.assertAlmostEqual(result.trades[0].entry_cost_krw, 92718)
        self.assertAlmostEqual(result.trades[0].net_pnl_krw, 30453.84)
        self.assertAlmostEqual(result.equity[-1].equity_krw, 130453.84)
        self.assertAlmostEqual(result.equity[1].cash_krw, 7282)
        self.assertAlmostEqual(result.equity[1].holdings_krw, 118800)
        self.assertTrue(result.accounting_verified)

    def test_same_bar_ambiguous_stop_wins(self):
        result = run([bar(0), bar(1, high=130, low=80, close=110)])
        self.assertEqual([f.side for f in result.fills], ["BUY", "SELL"])
        self.assertEqual(result.fills[1].price, 90)
        self.assertEqual(result.trades[0].net_pnl_krw, -1000)
        self.assertEqual(result.trades[0].exit_reason, "stop")

    def test_risk_budget_includes_entry_and_exit_costs(self):
        config = replace(cfg(), costs=CostModel(50, 20, 100, 10))
        result = run([bar(0), bar(1)], config)
        buy = result.fills[0]
        stop = buy.price * .9
        buy_allin = buy.quantity * buy.price * 1.005
        stop_net = buy.quantity * stop * .99 * (1 - .005 - .002)
        self.assertLessEqual(buy_allin - stop_net, 1000 + 1e-8)
        self.assertGreater((buy_allin - stop_net) / buy.quantity * (buy.quantity + 1), 1000)
        self.assertGreaterEqual(result.equity[-1].cash_krw, 0)

    def test_entry_gap_recalculates_risk_and_cash(self):
        result = run([bar(0), bar(1, open=1000)], cfg())
        self.assertEqual(result.fills[0].quantity, 10)
        self.assertEqual(result.fills[0].price, 1000)
        self.assertEqual(result.equity[-1].cash_krw, 0)

    def test_gap_loss_exceeds_planned_stop_and_halt_is_sticky(self):
        config = cfg(strategy_loss_limit_krw=1000)
        result = run([bar(0), bar(1), bar(2, open=50), bar(3, open=200), bar(4)], config,
                     Scripted((1, 3, 4)))
        self.assertEqual(result.fills[1].price, 50)
        self.assertEqual(result.trades[0].net_pnl_krw, -5000)
        self.assertEqual(result.halted_reason, "strategy_drawdown_limit")
        self.assertEqual(sum(f.side == "BUY" for f in result.fills), 1)

    def test_absolute_account_limit_is_independent_from_peak_drawdown(self):
        result = run([bar(0), bar(1), bar(2, open=50)],
                     cfg(strategy_loss_limit_krw=100000, account_loss_limit_krw=2000))
        self.assertEqual(result.halted_reason, "account_loss_limit")

    def test_partial_fill_shared_volume_and_persistent_gap_stop(self):
        config = cfg(volume_participation=.1)
        result = run([
            bar(0, volume=100), bar(1, volume=100),
            bar(2, open=50, volume=30),
            bar(3, open=60, volume=0, tradable=False),
            bar(4, open=70, volume=100),
        ], config)
        self.assertEqual([f.quantity for f in result.fills], [10, 3, 7])
        self.assertEqual([f.price for f in result.fills], [100, 50, 70])
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].quantity, 10)
        self.assertEqual(result.trades[0].net_pnl_krw, -360)
        for day in range(5):
            self.assertLessEqual(sum(f.quantity for f in result.fills if f.timestamp == BASE + timedelta(days=day)),
                                 [10, 10, 3, 0, 10][day])
        self.assertTrue(any(o["reason"] == "partial_fill" for o in result.rejected_orders))

    def test_halted_terminal_position_not_fabricated_closed(self):
        result = run([bar(0), bar(1), bar(2, open=50, tradable=False, volume=0)])
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(len(result.trades), 0)
        self.assertEqual(result.open_positions[0]["quantity"], 100)
        self.assertEqual(result.equity[-1].equity_krw, 5000)
        self.assertEqual(result.equity[-1].unrealized_pnl_krw, -5000)

    def test_entry_rejection_is_not_retried_days_later(self):
        result = run([bar(0), bar(1, volume=0), bar(2)])
        self.assertEqual(result.fills, [])
        self.assertEqual(result.rejected_orders[0]["side"], "BUY")

    def test_partial_take_profits_and_full_trade_pnl(self):
        config = replace(cfg(), exits=replace(cfg().exits, partial_take_fraction=.5))
        result = run([bar(0), bar(1), bar(2, open=100, high=125, close=120)], config)
        self.assertEqual([f.quantity for f in result.fills], [100, 50, 50])
        self.assertEqual([f.price for f in result.fills], [100, 110, 120])
        self.assertAlmostEqual(result.trades[0].net_pnl_krw, 1500)
        self.assertAlmostEqual(result.equity[-1].realized_pnl_krw, 1500)

    def test_trailing_stop_uses_prior_close_only(self):
        config = replace(cfg(), exits=replace(cfg().exits, target_pct=2, trailing_pct=.1))
        result = run([bar(0), bar(1, high=150, low=95, close=110),
                      bar(2, open=110, high=111, low=98, close=100)], config)
        self.assertEqual(result.fills[1].timestamp, BASE + timedelta(days=2))
        self.assertEqual(result.fills[1].price, 99)

    def test_time_exit_counts_symbol_bars_not_market_union(self):
        strategy = Scripted()
        strategy.max_holding_bars = 2
        result = run([bar(0), bar(2), bar(3, symbol="Y"), bar(4),
                      bar(5, symbol="Y"), bar(6)], strategy=strategy)
        x_sells = [f for f in result.fills if f.side == "SELL" and f.symbol == "X"]
        self.assertEqual(x_sells[0].timestamp, BASE + timedelta(days=6))
        self.assertEqual(result.trades[0].exit_reason, "time_exit")
        self.assertEqual(result.trades[0].holding_bars, 2)

    def test_one_completed_bar_time_limit_displays_one(self):
        strategy = Scripted()
        strategy.max_holding_bars = 1
        result = run([bar(0), bar(1), bar(2)], strategy=strategy)
        self.assertEqual(result.trades[0].holding_bars, 1)
        self.assertEqual(result.trades[0].exit_time, BASE + timedelta(days=2))

    def test_intrabar_exit_includes_current_observed_bar(self):
        result = run([bar(0), bar(1), bar(2, high=130)])
        self.assertEqual(result.trades[0].holding_bars, 2)

    def test_missing_session_does_not_advance_observed_holding_clock(self):
        strategy = Scripted()
        strategy.max_holding_bars = 2
        result = run([bar(0), bar(1), bar(3), bar(4)], strategy=strategy,
                     missing_sessions={"X": [BASE + timedelta(days=2)]})
        self.assertEqual(result.trades[0].exit_time, BASE + timedelta(days=4))
        self.assertEqual(result.trades[0].holding_bars, 2)

    def test_missing_unknown_session_cancels_next_entry(self):
        strategy = Scripted()
        result = run([bar(0), bar(2)], strategy=strategy,
                     missing_sessions={"X": [BASE + timedelta(days=1)]})
        self.assertEqual(result.fills, [])
        self.assertEqual(result.rejected_orders[0]["reason"], "data_missing")
        self.assertEqual(result.rejected_orders[0]["timestamp"], (BASE + timedelta(days=1)).isoformat())
        self.assertEqual(len(result.equity), 3)
        self.assertEqual(len(strategy.seen), 1)

    def test_missing_terminal_session_retains_actual_last_mark(self):
        from stock_validator.metrics import independent_audit
        data = Dataset([bar(0), bar(1)], META,
                       missing_sessions={"X": [BASE + timedelta(days=2)]})
        result = run_backtest(data, Scripted(), cfg())
        self.assertEqual(result.equity[-1].timestamp, BASE + timedelta(days=2))
        self.assertEqual(result.equity[-1].holdings_krw, result.equity[-2].holdings_krw)
        self.assertEqual(result.equity[-1].equity_krw, 10000)
        self.assertEqual(result.open_positions[0]["quantity"], 100)
        self.assertEqual(len(result.fills), 1)
        self.assertEqual(len(result.trades), 0)
        self.assertTrue(independent_audit(result, data)["passed"])
        self.assertTrue(any("OHLCV 미확인" in w for w in result.warnings))

    def test_missing_symbol_session_does_not_block_other_symbol(self):
        result = run([bar(0), bar(0, symbol="Y"), bar(1, symbol="Y"),
                      bar(2, symbol="Y"), bar(2)], cfg(initial_cash_krw=20000),
                     missing_sessions={"X": [BASE + timedelta(days=1)]})
        self.assertEqual([f.symbol for f in result.fills if f.side == "BUY"], ["Y"])
        self.assertEqual(result.rejected_orders[0]["reason"], "data_missing")

    def test_atr_stop_uses_signal_information(self):
        config = replace(cfg(), exits=replace(cfg().exits, stop_mode="atr", atr_multiple=2))
        result = run([bar(0), bar(1, low=95)], config)
        self.assertEqual(result.fills[1].price, 96)
        self.assertEqual(result.fills[0].quantity, 100)  # cash, not risk, binds

    def test_daily_loss_blocks_entries_without_global_halt(self):
        config = cfg(daily_loss_limit_krw=1000)
        # X is held. Y signals on day 1, then X's observed day-2 opening gap
        # blocks Y before any day-2 close is visible.
        result = run([bar(0), bar(1), bar(1, symbol="Y"), bar(2, open=80),
                      bar(2, symbol="Y")], config)
        self.assertFalse(any(f.side == "BUY" and f.symbol == "Y" for f in result.fills))
        self.assertIsNone(result.halted_reason)
        self.assertTrue(any(o["reason"] == "daily_loss_limit" for o in result.rejected_orders))

    def test_max_positions_and_no_leverage_in_multiasset_account(self):
        result = run([bar(0, symbol=s) for s in ("X", "Y")]
                     + [bar(1, symbol=s) for s in ("X", "Y")], cfg(max_positions=1))
        self.assertEqual(sum(f.side == "BUY" for f in result.fills), 1)
        self.assertEqual(result.rejected_orders[-1]["reason"], "max_positions")
        for point in result.equity:
            self.assertGreaterEqual(point.cash_krw, 0)
            self.assertAlmostEqual(point.cash_krw + point.holdings_krw, point.equity_krw)
            self.assertAlmostEqual(10000 + point.realized_pnl_krw + point.unrealized_pnl_krw, point.equity_krw)


class InformationBoundaryTests(unittest.TestCase):
    def test_close_fx_and_future_prices_do_not_change_open_entry(self):
        common = [bar(0, 10, currency="USD", fx_to_krw=1000),
                  bar(1, 10, high=11, close=11, currency="USD", fx_to_krw=1000),
                  bar(2, 10, currency="USD", fx_to_krw=1000)]
        mutated = list(common)
        mutated[1] = replace(common[1], fx_to_krw=100000)
        mutated[2] = bar(2, 10000, currency="USD", fx_to_krw=200000)
        c = cfg(initial_cash_krw=100000, planned_loss_per_trade_krw=100000,
                strategy_loss_limit_krw=1e20, daily_loss_limit_krw=1e20)
        a, b = run(common, c), run(mutated, c)
        self.assertEqual(a.fills[0], b.fills[0])
        self.assertEqual(a.fills[0].fx_to_krw, 1000)

    def test_signal_context_is_immutable_and_point_in_time(self):
        early = Fundamental("X", BASE, BASE - timedelta(days=1), earnings_growth=.1)
        future = Fundamental("X", BASE + timedelta(days=10), BASE, earnings_growth=.9)
        invalid_period = Fundamental("X", BASE, BASE + timedelta(days=1), earnings_growth=10)
        strategy = Scripted(())
        sectors = {"X": [(BASE, BASE + timedelta(days=1), "tech"),
                         (BASE + timedelta(days=1), None, "finance")]}
        run([bar(0), bar(1), bar(2)], strategy=strategy,
            fundamentals=[future, early, invalid_period],
            benchmark_bars=[bar(0, symbol="INDEX"), bar(10, symbol="INDEX")],
            sectors=sectors, sector_bars=[bar(0, symbol="tech"), bar(0, symbol="finance"),
                                        bar(10, symbol="finance")])
        self.assertEqual(len(strategy.seen), 2)  # no final-bar signal
        for history, ctx in strategy.seen:
            self.assertIsInstance(history, tuple)
            self.assertTrue(all(b.timestamp <= ctx.decision_time for b in history))
            self.assertEqual(ctx.fundamentals, (early,))
            self.assertTrue(all(b.timestamp <= ctx.decision_time for b in ctx.benchmark + ctx.sector_bars))
        self.assertEqual(strategy.seen[0][1].sector_bars[0].symbol, "tech")
        self.assertEqual(strategy.seen[1][1].sector_bars[0].symbol, "finance")

    def test_market_filter_uses_benchmark_known_at_decision(self):
        benchmark = [bar(i - 60, open=200 - i, symbol="INDEX") for i in range(61)]
        config = replace(cfg(), market_filter=True)
        result = run([bar(0), bar(1)], config, benchmark_bars=benchmark)
        self.assertEqual(result.fills, [])
        self.assertEqual(result.rejected_orders[0]["reason"], "market_filter")

    def test_invalid_data_and_configuration_fail_closed(self):
        for bad in [replace(bar(0), fx_to_krw=math.nan), replace(bar(0), volume=-1),
                    replace(bar(0), low=200), replace(bar(0), timestamp=datetime(2020, 1, 1))]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                run([bad])
        with self.assertRaises(ValueError):
            run([bar(0), bar(0)])
        with self.assertRaises(ValueError):
            run([bar(0), bar(1, currency="USD", fx_to_krw=1000)])
        with self.assertRaises(ValueError):
            run([bar(0)], missing_sessions={"X": [BASE]})
        with self.assertRaises(ValueError):
            run([bar(0)], missing_sessions={"X": [datetime(2020, 1, 2)]})
        for invalid in [replace(cfg(), costs=CostModel(slippage_bps=10000)),
                        replace(cfg(), costs=CostModel(commission_bps=9000, sell_tax_bps=2000)),
                        replace(cfg(), risk=replace(cfg().risk, volume_participation=2)),
                        replace(cfg(), exits=replace(cfg().exits, stop_pct=1))]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                run([bar(0)], invalid)

    def test_deterministic_input_order_does_not_mutate_dataset(self):
        data = Dataset([bar(2, open=120), bar(0), bar(1)], META)
        original = list(data.bars)
        a = run_backtest(data, Scripted(), cfg())
        b = run_backtest(data, Scripted(), cfg())
        self.assertEqual(a, b)
        self.assertEqual(data.bars, original)

    def test_close_drawdown_does_not_use_unrecorded_open_peak(self):
        config = replace(cfg(), exits=replace(cfg().exits, target_pct=10))
        result = run([bar(0), bar(1), bar(2, open=200, low=100, close=100)], config)
        self.assertEqual([p.drawdown for p in result.equity], [0, 0, 0])

    def test_empty_data_has_no_invented_equity_or_profit(self):
        result = run([])
        self.assertEqual(result.equity, [])
        self.assertEqual(result.fills, [])
        self.assertTrue(result.accounting_verified)


if __name__ == "__main__":
    unittest.main()
