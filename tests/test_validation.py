"""Hand calculations, tamper detection, and irreversible holdout boundaries."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math

import pytest

from stock_validator import validation as v
from stock_validator.metrics import (block_bootstrap_expectancy, independent_audit,
                                     performance_metrics, trade_statistics)
from stock_validator.models import (BacktestResult, Bar, Dataset, DatasetMeta,
                                    EngineConfig, EquityPoint, Fill, Trade)


T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)


def example_data(n=100, *, fx=1.0):
    return Dataset([Bar(T0 + timedelta(days=i), "TEST", 100, 101, 99, 100, 1_000_000,
                        "KRW", fx) for i in range(n)],
                   DatasetMeta("SYNTHETIC HAND EXAMPLE", T0.isoformat(), synthetic=True))


def trade(pnl, i=0, regime="bull"):
    return Trade(i, "TEST", T0 + timedelta(days=i), T0 + timedelta(days=i + 1),
                 10, 1000, pnl, pnl / 1000, 1, "time", regime)


def flat_result(dataset, strategy=None, config=None):
    config = config or EngineConfig()
    cash = config.risk.initial_cash_krw
    result = BacktestResult(getattr(strategy, "key", "A"), "1", config)
    result.equity = [EquityPoint(d, cash, 0, cash, 0, 0, 0)
                     for d in sorted({b.timestamp for b in dataset.bars})]
    result.accounting_verified = True
    return result


def test_hand_statistics_and_undefined_ratios():
    result = trade_statistics([trade(100), trade(-40, 1), trade(0, 2)])
    assert result["profit_factor"] == 2.5
    assert result["payoff_ratio"] == 2.5
    assert result["expectancy"] == 20
    assert result["win_rate"] == pytest.approx(1 / 3)
    assert trade_statistics([trade(100)])["profit_factor"] is None
    assert trade_statistics([])["win_rate"] is None


def test_hand_equity_metrics_and_cagr_short_period_not_annualized():
    result = flat_result(example_data(3))
    result.equity[1] = EquityPoint(T0 + timedelta(days=1), 900000, 0, 900000, -100000, 0, -.1)
    result.equity[2] = EquityPoint(T0 + timedelta(days=2), 950000, 0, 950000, -50000, 0, -.05)
    stats = performance_metrics(result)
    assert stats["net_return"] == pytest.approx(-.05)
    assert stats["max_drawdown"] == pytest.approx(-.1)
    assert stats["cagr"] is None


def test_cagr_exact_one_year_and_loss_curve():
    result = flat_result(example_data(1))
    end = T0 + timedelta(days=365.2425)
    result.equity.append(EquityPoint(end, 1100000, 0, 1100000, 100000, 0, 0))
    assert performance_metrics(result)["cagr"] == pytest.approx(.1)


def test_independent_fill_ledger_partial_sell_costs_and_fx():
    config = EngineConfig()
    t1, t2 = T0 + timedelta(days=1), T0 + timedelta(days=2)
    # Buy KRW 1000 + fees 12, sell two partials: net 656 + 432 = 1088.
    fills = [Fill(T0, "TEST", "BUY", 10, 10, 10, 10, 0, 2, "entry", 1),
             Fill(t1, "TEST", "SELL", 6, 11, 10, 2, 1, 1, "partial", 1),
             Fill(t2, "TEST", "SELL", 4, 11, 10, 3, 3, 2, "close", 1)]
    result = BacktestResult("A", "1", config, fills=fills,
                            trades=[Trade(1, "TEST", T0, t2, 10, 1012, 76, 76 / 1012, 2, "close", "bull")])
    result.equity = [EquityPoint(T0, 998988, 1000, 999988, 0, -12, -.000012),
                     EquityPoint(t1, 999644, 440, 1000084, 48.8, 35.2, 0),
                     EquityPoint(t2, 1000076, 0, 1000076, 76, 0, 1000076 / 1000084 - 1)]
    data = Dataset([Bar(T0, "TEST", 10, 10, 10, 10, 100, "USD", 10),
                    Bar(t1, "TEST", 11, 11, 11, 11, 100, "USD", 10),
                    Bar(t2, "TEST", 11, 11, 11, 11, 100, "USD", 10)], example_data().meta)
    assert independent_audit(result, data)["passed"]
    result.trades[0] = replace(result.trades[0], net_pnl_krw=77)
    assert not independent_audit(result, data)["passed"]


def test_independent_audit_catches_intermediate_fictitious_equity():
    data = example_data(3)
    result = flat_result(data)
    result.equity[1] = EquityPoint(T0 + timedelta(days=1), 1000000, 100000, 1100000, 0, 100000, 0)
    result.equity[2] = replace(result.equity[2], drawdown=1000000 / 1100000 - 1)
    assert not independent_audit(result, data)["passed"]


def test_independent_audit_rejects_sale_without_shares_and_missing_equity():
    result = flat_result(example_data(2))
    result.fills = [Fill(T0, "TEST", "SELL", 1, 100, 1, 0, 0, 0, "impossible", 1)]
    assert "보유량 초과 매도" in independent_audit(result)["errors"]
    result.equity = []
    assert not independent_audit(result)["passed"]


def test_independent_audit_rejects_nonfinite_trade_pnl_and_hidden_open_positions():
    result = flat_result(example_data(2))
    result.trades = [trade(math.nan)]
    assert not independent_audit(result)["passed"]
    result.trades = []
    result.fills = [Fill(T0, "TEST", "BUY", 1, 100, 1, 0, 0, 0, "entry", 1)]
    result.equity = [EquityPoint(b.timestamp, 999900, 100, 1000000, 0, 0, 0) for b in example_data(2).bars]
    assert "미청산 포지션 기록 누락 또는 허위 기록" in independent_audit(result, example_data(2))["errors"]


def test_independent_audit_rejects_fractional_share_fill():
    result = flat_result(example_data(2))
    result.fills = [Fill(T0, "TEST", "BUY", 1.5, 100, 1, 0, 0, 0, "entry", 1)]
    assert "유효하지 않은 체결 수치" in independent_audit(result)["errors"]


def test_bootstrap_reproducible_clustered_and_adjusted_resolution():
    trades = [trade(100 if i % 3 else -20, i) for i in range(100)]
    a = block_bootstrap_expectancy(trades, samples=999, seed=42)
    b = block_bootstrap_expectancy(trades, samples=999, seed=42)
    assert a == b
    assert a["lower"] > 0 and a["usable"]
    corrected = block_bootstrap_expectancy(trades, samples=99, alpha=.001)
    assert not corrected["usable"]
    assert not block_bootstrap_expectancy(trades[:1])["usable"]


def test_split_purges_complete_holding_period_from_every_boundary():
    dates = [T0 + timedelta(days=i) for i in range(1000)]
    policy = json.loads(v.DEFAULT_POLICY.read_text())
    split = v.chronological_split(dates, policy, 5)
    assert split["development"] == (0, 450)
    assert split["walk_forward"][0][0] == 455
    assert split["holdout"][0] == 805
    ranges = [split["development"], *split["walk_forward"], split["holdout"]]
    for before, after in zip(ranges, ranges[1:]):
        assert after[0] - before[1] >= 5


def test_policy_rejects_overlapping_split_and_cost_relaxation():
    policy = json.loads(v.DEFAULT_POLICY.read_text())
    with pytest.raises(ValueError):
        v._validate_policy({**policy, "development_fraction": .8})
    with pytest.raises(ValueError):
        v._validate_policy({**policy, "cost_multiplier": 1})


@pytest.mark.parametrize("key,value", [("maximum_drawdown", math.nan),
                                      ("cost_multiplier", math.nan),
                                      ("minimum_neighbor_profitable_fraction", math.inf),
                                      ("bootstrap_samples", 1.5),
                                      ("bootstrap_samples", True),
                                      ("required_regimes", []),
                                      ("seed", 1.5)])
def test_policy_rejects_nonfinite_and_noninteger_values(key, value):
    policy = json.loads(v.DEFAULT_POLICY.read_text())
    with pytest.raises(ValueError):
        v._validate_policy({**policy, key: value})


def test_long_horizon_policy_rejects_nonfinite_criteria():
    policy = json.loads(v.DEFAULT_POLICY.read_text())
    policy["long_horizon"]["maximum_drawdown"] = math.nan
    with pytest.raises(ValueError):
        v._validate_policy(policy)


def test_mixed_exchange_purge_counts_each_symbols_bars():
    dates = [T0 + timedelta(hours=12 * i) for i in range(1000)]
    symbols = {"KR": set(dates[::2]), "US": set(dates[1::2])}
    split = v.chronological_split(dates, json.loads(v.DEFAULT_POLICY.read_text()), 5, symbols)
    assert split["walk_forward"][0][0] == 460
    assert split["holdout"][0] == 810


def test_candidate_grid_includes_stops_atr_partial_trailing_and_holdings():
    candidates = v.research_candidates(v.get_strategies()[0])
    assert {.03, .04, .05} <= {c.exits.stop_pct for _, c in candidates}
    assert {"fixed", "atr"} <= {c.exits.stop_mode for _, c in candidates}
    assert {1, 3, 5} <= {s.max_holding_bars for s, _ in candidates}
    assert any(c.exits.partial_take_fraction == 0 for _, c in candidates)
    assert any(c.exits.trailing_pct is None for _, c in candidates)


def test_robustness_never_counts_selected_parameter_as_its_own_neighbor():
    base = v.get_strategies()[0]
    selected = replace(v.parameter_variants("A")[0], max_holding_bars=3)
    config = v.default_config_for_strategy("A")
    pairs = v.robustness_neighbors(base, selected, config)
    assert all((asdict(rule), asdict(conf)) != (asdict(selected), asdict(config)) for rule, conf in pairs)
    assert any(rule.volume_multiple == base.volume_multiple for rule, _ in pairs)


def test_data_fingerprint_ignores_retrieval_labels_not_actual_observations():
    data = example_data()
    fingerprint = v.dataset_fingerprint(data)
    data.meta = replace(data.meta, retrieved_at="changed", source="relabeled", synthetic=False)
    assert v.dataset_fingerprint(data) == fingerprint
    data.bars[-1] = replace(data.bars[-1], close=100.1)
    assert v.dataset_fingerprint(data) != fingerprint


def test_subset_preserves_missing_sessions_and_fingerprint_binds_them():
    data = example_data()
    first_hash = v.dataset_fingerprint(data)
    data.missing_sessions = {"TEST": [T0 + timedelta(days=3), T0 + timedelta(days=99)]}
    assert v.dataset_fingerprint(data) != first_hash
    subset = v._subset(data, [b.timestamp for b in data.bars], (0, 10))
    assert subset.missing_sessions == {"TEST": [T0 + timedelta(days=3)]}


def test_missing_certificate_prevents_any_strategy_backtest(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "REGISTRY_ROOT", tmp_path / "registry")
    def forbidden(*args):
        raise AssertionError("uncertified engine must not evaluate strategies")
    monkeypatch.setattr(v, "run_backtest", forbidden)
    report = v.validate_dataset(example_data(1500), tmp_path / "output")
    assert not report["engine_verified"] and not report["holdout_consumed"]
    assert all(s["status"] == "DATA_INSUFFICIENT" for s in report["strategies"])
    assert all(not s["metrics"] for s in report["strategies"])
    json.dumps(report, allow_nan=False)
    assert (tmp_path / "output" / "acceptance.lock.json").exists()


def test_missing_fx_is_reported_without_calculating_fictitious_returns(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "REGISTRY_ROOT", tmp_path / "registry")
    monkeypatch.setattr(v, "verify_engine_certificate", lambda path: {"passed": True, "reasons": []})
    monkeypatch.setattr(v, "run_backtest", lambda *args: pytest.fail("NaN FX must block backtest"))
    report = v.validate_dataset(example_data(1500, fx=math.nan), tmp_path / "output")
    assert all(s["metrics"] == {} for s in report["strategies"])
    assert "환율 누락" in report["strategies"][0]["reason"]
    assert not report["holdout_consumed"]
    json.dumps(report, allow_nan=False)


def _small_policy(tmp_path):
    policy = json.loads(v.DEFAULT_POLICY.read_text())
    policy.update(min_development_bars=4, min_fold_bars=2, min_holdout_bars=2,
                  bootstrap_samples=20, minimum_trades_walk_forward=1,
                  minimum_trades_holdout=1, minimum_regime_trades=1)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return path


def test_holdout_freezes_selections_and_replays_without_reopening(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "REGISTRY_ROOT", tmp_path / "registry")
    monkeypatch.setattr(v, "verify_engine_certificate", lambda path: {"passed": True, "reasons": []})
    calls = []
    def recording(data, strategy, config):
        calls.append((strategy.key, min(b.timestamp for b in data.bars), max(b.timestamp for b in data.bars)))
        # The selection lock must exist before any final-period observations.
        if min(b.timestamp for b in data.bars) >= T0 + timedelta(days=85):
            assert (tmp_path / "one" / "selection.lock.json").is_file()
        return flat_result(data, strategy, config)
    monkeypatch.setattr(v, "run_backtest", recording)
    data, policy = example_data(), _small_policy(tmp_path)
    first = v.validate_dataset(data, tmp_path / "one", policy)
    assert first["holdout_consumed"]
    count = len(calls)
    second = v.validate_dataset(data, tmp_path / "two", policy)
    assert first == second and len(calls) == count
    changed = json.loads(policy.read_text())
    changed["seed"] += 1
    policy.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="이미 개봉"):
        v.validate_dataset(data, tmp_path / "three", policy)
    assert len(calls) == count


def test_cached_report_downgrades_when_current_certificate_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "REGISTRY_ROOT", tmp_path / "registry")
    monkeypatch.setattr(v, "verify_engine_certificate", lambda path: {"passed": True, "reasons": []})
    monkeypatch.setattr(v, "run_backtest", flat_result)
    data, policy = example_data(), _small_policy(tmp_path)
    v.validate_dataset(data, tmp_path / "one", policy)
    original = next((tmp_path / "registry").glob("*/report.json")).read_bytes()
    monkeypatch.setattr(v, "verify_engine_certificate", lambda path: {"passed": False, "reasons": ["stale certificate"]})
    report = v.validate_dataset(data, tmp_path / "two", policy)
    assert not report["engine_verified"] and report["archived_engine_verified"]
    assert "stale certificate" in report["strategies"][0]["reason"]
    assert next((tmp_path / "registry").glob("*/report.json")).read_bytes() == original


def test_meta_true_cannot_bypass_unimplemented_semantic_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "REGISTRY_ROOT", tmp_path / "registry")
    monkeypatch.setattr(v, "verify_engine_certificate", lambda path: {"passed": True, "reasons": []})
    monkeypatch.setattr(v, "run_backtest", flat_result)
    data = example_data()
    data.meta = replace(data.meta, synthetic=False, survivorship_bias_controlled=True,
                        point_in_time_universe=True, corporate_actions_complete=True,
                        point_in_time_fundamentals=True, sector_history_complete=True, fx_complete=True)
    report = v.validate_dataset(data, tmp_path / "output", _small_policy(tmp_path))
    assert all(s["status"] == "DATA_INSUFFICIENT" for s in report["strategies"])
    assert all(v.EVIDENCE_LIMITATION in s["reason"] for s in report["strategies"])


def test_forged_or_stale_certificate_does_not_pass(tmp_path):
    path = tmp_path / "certificate.json"
    path.write_text(json.dumps({"passed": True, "exit_code": 0, "tests_passed": 100,
                                "source_hashes": {}, "test_hashes": {}}))
    result = v.verify_engine_certificate(path)
    assert not result["passed"]
    assert any("소스 변경" in r for r in result["reasons"])


def test_json_safety_does_not_emit_nonstandard_nan_or_infinity():
    cleaned = v._safe({"a": math.nan, "b": [math.inf, -math.inf, 1.0]})
    assert cleaned == {"a": None, "b": [None, None, 1.0]}
    json.dumps(cleaned, allow_nan=False)
