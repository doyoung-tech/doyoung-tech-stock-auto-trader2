"""Precommitted chronological research, never an execution authorization.

The final segment is consumed once per exact dataset in a repository-wide
registry, independent of output directory, seed, or changed acceptance policy.
Current data ingestion cannot establish audited corporate-action accounting or
point-in-time universe evidence: real strategy PASS is therefore unavailable.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil, isfinite
from pathlib import Path
from typing import Any

from .engine import run_backtest
from .metrics import (block_bootstrap_expectancy, independent_audit, performance_metrics,
                      regime_statistics, trade_statistics)
from .models import Dataset, EngineConfig
from .strategies import default_config_for_strategy, get_strategies, parameter_variants


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY = REPO_ROOT / "config" / "acceptance.json"
REGISTRY_ROOT = REPO_ROOT / ".research_state" / "holdouts"
SOURCE_FILES = ("engine.py", "models.py", "metrics.py", "strategies.py", "indicators.py", "validation.py", "data.py", "cli.py")
REQUIRED_TESTS = ("test_engine.py", "test_strategies.py", "test_data.py", "test_validation.py", "test_web.py", "test_cli.py", "test_review_engine.py")
EVIDENCE_LIMITATION = "기업행동(분할·배당·상폐) 회계와 PIT 구성 증거 검증 미구현: 실제 전략 자동합격 불가"


def _safe(value: Any) -> Any:
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (datetime, Path)):
        return str(value)
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode()).hexdigest()


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def engine_code_hash() -> str:
    return _digest({name: sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                    for name in SOURCE_FILES})


def verify_engine_certificate(path: str | Path | None) -> dict[str, Any]:
    if path is None or not Path(path).is_file():
        return {"passed": False, "reasons": ["현재 소스에 대한 실행 테스트 인증서 없음"]}
    reasons = []
    try:
        cert = json.loads(Path(path).read_text(encoding="utf-8"))
        if cert.get("passed") is not True or cert.get("exit_code") != 0 or cert.get("tests_passed", 0) < 8:
            reasons.append("실행 테스트 미통과 또는 표본 부족")
        hashes = cert.get("source_hashes", {})
        for name in SOURCE_FILES:
            declared = hashes.get(name, hashes.get(f"src/stock_validator/{name}"))
            actual = sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            if declared != actual:
                reasons.append(f"테스트 이후 소스 변경 또는 해시 누락: {name}")
        test_hashes = cert.get("test_hashes", {})
        names = sorted(set(REQUIRED_TESTS) | {path.name for path in (REPO_ROOT / "tests").glob("test_*.py")})
        for name in names:
            declared = test_hashes.get(f"tests/{name}", test_hashes.get(name))
            test_path = REPO_ROOT / "tests" / name
            if not test_path.is_file() or declared != sha256(test_path.read_bytes()).hexdigest():
                reasons.append(f"실행 테스트 증거 해시 불일치: {name}")
        return {"passed": not reasons, "reasons": reasons,
                "tests_passed": cert.get("tests_passed"), "created_at": cert.get("created_at"),
                "scope": "로컬 실행 테스트와 현재 파일 해시의 일치; 악의적인 인증서 위조를 막는 전자서명은 아님"}
    except (OSError, ValueError, TypeError, AttributeError) as error:
        return {"passed": False, "reasons": [f"인증서 형식 오류: {type(error).__name__}"]}


def dataset_fingerprint(dataset: Dataset) -> str:
    # Retrieval timestamp, labels, source metadata, policy and random seed do not
    # create a fresh holdout. Actual observations and availability times do.
    return _digest({"bars": [asdict(b) for b in sorted(dataset.bars, key=lambda b: (b.timestamp, b.symbol))],
                    "fundamentals": [asdict(f) for f in sorted(dataset.fundamentals, key=lambda f: (f.symbol, f.available_at, f.period_end))],
                    "benchmark": [asdict(b) for b in sorted(dataset.benchmark_bars, key=lambda b: (b.timestamp, b.symbol))],
                    "sector_bars": [asdict(b) for b in sorted(dataset.sector_bars, key=lambda b: (b.timestamp, b.symbol))],
                    "sectors": dataset.sectors, "missing_sessions": dataset.missing_sessions})


def chronological_split(dates: list[datetime], policy: dict[str, Any], purge_bars: int,
                        symbol_dates: dict[str, set[datetime]] | None = None) -> dict[str, Any]:
    """Disjoint dev/WF/final segments; a full holding period precedes each test."""
    n = len(dates)
    dev_end = int(n * policy["development_fraction"])
    wf_end = int(n * (policy["development_fraction"] + policy["walk_forward_fraction"]))
    folds = policy["walk_forward_folds"]
    bounds = [dev_end + (wf_end - dev_end) * i // folds for i in range(folds + 1)]
    def after_purge(boundary: int, stop: int) -> int:
        if symbol_dates is None:
            return boundary + purge_bars
        # A KR/US mixed dataset has multiple exchange timestamps per day.
        # Count observations for each active symbol, never union timestamps.
        active = {symbol: times for symbol, times in symbol_dates.items()
                  if any(date in times for date in dates[boundary:stop])}
        if not active:
            return stop
        counts = {symbol: 0 for symbol in active}
        for index in range(boundary, stop):
            for symbol, times in active.items():
                counts[symbol] += dates[index] in times
            if min(counts.values()) >= purge_bars:
                return index + 1
        return stop
    ranges = [(after_purge(bounds[i], bounds[i + 1]), bounds[i + 1]) for i in range(folds)]
    holdout = (after_purge(wf_end, n), n)
    return {"development": (0, dev_end), "walk_forward": ranges,
            "holdout": holdout, "purge_bars": purge_bars,
            "warmup": "각 독립 구간 내부에서만 지표 준비; 이전 구간 포지션을 넘기지 않음"}


def _subset(dataset: Dataset, dates: list[datetime], bounds: tuple[int, int]) -> Dataset:
    start, stop = bounds
    selected = set(dates[max(0, start):max(0, stop)]) if start < stop else set()
    if not selected:
        return Dataset([], dataset.meta)
    first, last = min(selected), max(selected)
    return Dataset([b for b in dataset.bars if b.timestamp in selected], dataset.meta,
                   [f for f in dataset.fundamentals if f.available_at <= last],
                   [b for b in dataset.benchmark_bars if first <= b.timestamp <= last],
                   dataset.sectors,
                   [b for b in dataset.sector_bars if first <= b.timestamp <= last],
                   {symbol: [timestamp for timestamp in timestamps if first <= timestamp <= last]
                    for symbol, timestamps in dataset.missing_sessions.items()})


def research_candidates(strategy: Any) -> list[tuple[Any, EngineConfig]]:
    """One-factor alternatives declared before observations; no holdout tuning."""
    base = default_config_for_strategy(strategy.key)
    candidates = [(strategy, base)]
    candidates += [(variant, base) for variant in parameter_variants(strategy.key)]
    for stop in (.03, .04, .05):
        candidates.append((strategy, replace(base, exits=replace(base.exits, stop_mode="fixed", stop_pct=stop))))
    for multiple in (2.0, 3.0):
        candidates.append((strategy, replace(base, exits=replace(base.exits, stop_mode="atr", atr_multiple=multiple))))
    candidates.append((strategy, replace(base, exits=replace(base.exits, target_mode="fixed", target_pct=.08))))
    candidates.append((strategy, replace(base, exits=replace(base.exits, partial_take_fraction=0.0))))
    candidates.append((strategy, replace(base, exits=replace(base.exits, trailing_pct=None))))
    holdings = (252, 756, 1260) if strategy.key == "E" else (1, 3, 5)
    candidates += [(replace(strategy, max_holding_bars=holding), base) for holding in holdings]
    unique = {}
    for rule, config in candidates:
        unique[_digest({"strategy": asdict(rule), "config": asdict(config)})] = (rule, config)
    return list(unique.values())


def robustness_neighbors(strategy: Any, selected: Any, config: EngineConfig) -> list[tuple[Any, EngineConfig]]:
    """Compare distinct neighbours of the chosen rule, never count itself."""
    rules = [replace(rule, max_holding_bars=selected.max_holding_bars)
             for rule in [strategy, *parameter_variants(strategy.key)]]
    pairs = [(rule, config) for rule in rules if asdict(rule) != asdict(selected)]
    pairs += [(selected, replace(config, exits=replace(config.exits, stop_mode="fixed", stop_pct=stop)))
              for stop in (.03, .04, .05)
              if stop != config.exits.stop_pct or config.exits.stop_mode != "fixed"]
    return pairs


def _stage(result: Any, data: Dataset) -> dict[str, Any]:
    audit = independent_audit(result, data)
    metrics = performance_metrics(result, data)
    metrics["trusted"] = False
    return {"executed": True, "metrics": metrics, "audit": audit,
            "warnings": result.warnings, "open_positions": result.open_positions,
            "rejected_orders": result.rejected_orders}


def _data_reasons(dataset: Dataset, strategy: Any) -> list[str]:
    reasons = [EVIDENCE_LIMITATION]
    meta = dataset.meta
    if meta.synthetic:
        reasons.append("합성 테스트 데이터는 실제 성과 검증 및 합격에 사용할 수 없음")
    for field, label in (("survivorship_bias_controlled", "생존자 편향 통제 증거 없음"),
                         ("point_in_time_universe", "과거 시점 구성종목 미확보"),
                         ("corporate_actions_complete", "주식분할·배당·상장폐지 이력 미확보"),
                         ("fx_complete", "시점별 환율 검증 미완료")):
        if not getattr(meta, field):
            reasons.append(label)
    if strategy.requires_fundamentals and not meta.point_in_time_fundamentals:
        reasons.append("발표시각이 검증된 당시 재무·전망 자료 없음")
    if strategy.requires_sectors and not meta.sector_history_complete:
        reasons.append("당시 업종 구성과 업종지수 이력 없음")
    if meta.price_basis != "raw":
        reasons.append("조정 가격의 사후 기업행동 재작성 및 실행 가격 정합성 미검증")
    if any(dataset.missing_sessions.values()):
        reasons.append("공급처 관측 누락 세션 존재: 미확인 가격을 체결 또는 보간하지 않음")
    return reasons


def _validate_policy(policy: dict[str, Any]) -> None:
    def number(key: str, low: float, high: float, *, integer: bool = False,
               source: dict[str, Any] | None = None, low_inclusive: bool = True) -> float:
        value = (policy if source is None else source).get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"policy {key} must be finite numeric")
        if integer and not isinstance(value, int):
            raise ValueError(f"policy {key} must be integer")
        if value < low or (not low_inclusive and value == low) or value > high:
            raise ValueError(f"policy {key} out of range")
        return value
    dev = number("development_fraction", 0, 1, low_inclusive=False)
    wf = number("walk_forward_fraction", 0, 1, low_inclusive=False)
    if not 0 < dev < 1 or not 0 < wf < 1 or dev + wf >= 1:
        raise ValueError("development + walk-forward fractions must leave a holdout")
    number("walk_forward_folds", 2, 20, integer=True)
    for key in ("min_development_bars", "min_fold_bars", "min_holdout_bars", "bootstrap_samples",
                "minimum_trades_walk_forward", "minimum_trades_holdout", "minimum_regime_trades"):
        number(key, 1, 100_000 if key == "bootstrap_samples" else 10_000_000, integer=True)
    if not 0 < number("family_wise_alpha", 0, 1, low_inclusive=False) < 1:
        raise ValueError("invalid family-wise alpha")
    number("seed", 0, 2**32 - 1, integer=True)
    number("cost_multiplier", 2, 100)
    number("maximum_drawdown", 0, 1, low_inclusive=False)
    number("minimum_profitable_fold_fraction", 0, 1)
    number("minimum_neighbor_profitable_fraction", 0, 1)
    number("minimum_neighbor_return", -1, 1)
    regimes = policy.get("required_regimes")
    if not isinstance(regimes, list) or not regimes or len(regimes) != len(set(regimes)) or any(r not in ("bull", "bear", "sideways") for r in regimes):
        raise ValueError("required_regimes must be a nonempty unique known regime list")
    if not isinstance(policy.get("require_benchmark_outperformance"), bool):
        raise ValueError("require_benchmark_outperformance must be boolean")
    long_horizon = policy.get("long_horizon")
    if not isinstance(long_horizon, dict):
        raise ValueError("missing long_horizon policy")
    for key in ("minimum_trades_walk_forward", "minimum_trades_holdout", "minimum_regime_trades"):
        number(key, 1, 10_000_000, integer=True, source=long_horizon)
    number("maximum_drawdown", 0, 1, source=long_horizon, low_inclusive=False)


def validate_dataset(dataset: Dataset, output_dir: str | Path,
                     policy_path: str | Path | None = None,
                     engine_certificate_path: str | Path | None = None) -> dict[str, Any]:
    policy = json.loads(Path(policy_path or DEFAULT_POLICY).read_text(encoding="utf-8"))
    _validate_policy(policy)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    dates = sorted({b.timestamp for b in dataset.bars})
    fingerprint = dataset_fingerprint(dataset)
    # Gap declarations, benchmark additions or financial snapshot changes do
    # not create a new final sample for already observed stock prices.
    registry_fingerprint = _digest({"bars": [asdict(b) for b in sorted(dataset.bars, key=lambda b: (b.timestamp, b.symbol))]})
    policy_hash, code_hash = _digest(policy), engine_code_hash()
    certificate = verify_engine_certificate(engine_certificate_path)
    registry = REGISTRY_ROOT / registry_fingerprint
    if registry.exists():
        registration = json.loads((registry / "precommit.json").read_text(encoding="utf-8"))
        previous = registry / "report.json"
        if registration["policy_hash"] != policy_hash or registration["code_hash"] != code_hash or registration["dataset_hash"] != fingerprint:
            raise ValueError("최종 독립기간은 이미 개봉되었습니다. 정책·코드·난수 변경으로 같은 데이터를 재검증할 수 없습니다.")
        if not previous.is_file():
            raise ValueError("이 데이터의 최종 독립기간이 이미 개봉되었으나 실행이 완료되지 않았습니다. 재최적화/재개봉 금지.")
        report = json.loads(previous.read_text(encoding="utf-8"))
        # Immutable historical evidence stays in the registry. Its presentation
        # must not claim the current test suite is certified after test changes.
        if not certificate["passed"]:
            report["archived_engine_verified"] = report["engine_verified"]
            report["engine_verified"] = False
            report["engine_certificate"] = certificate
            report["archived_report_sha256"] = sha256(previous.read_bytes()).hexdigest()
            for entry in report["strategies"]:
                entry["archived_status"] = entry["status"]
                entry["status"], entry["status_label"] = "DATA_INSUFFICIENT", "데이터 부족"
                entry["insufficient_data_reasons"] = sorted(set(entry["insufficient_data_reasons"] + certificate["reasons"]))
                entry["reason"] = " / ".join(entry["insufficient_data_reasons"] + entry["failed_criteria"])
            report["approved_strategies"] = []
            report["decision"] = "현재 엔진 테스트 인증 없음: 투자하지 않음"
        _write(output / "report.json", report)
        return report
    plan = {"created_at": datetime.now(timezone.utc).isoformat(), "policy": policy,
            "policy_hash": policy_hash, "code_hash": code_hash, "dataset_hash": fingerprint,
            "acceptance_frozen_before_execution": True,
            "test_results_verified_before_execution": certificate}
    _write(output / "acceptance.lock.json", plan)
    strategies = get_strategies()
    total_candidates = sum(len(research_candidates(strategy)) for strategy in strategies)
    adjusted_alpha = policy["family_wise_alpha"] / total_candidates
    bootstrap_samples = (100_000 if adjusted_alpha < 12 / 100_000
                         else min(100_000, max(policy["bootstrap_samples"], ceil(12 / adjusted_alpha))))
    report: dict[str, Any] = {
        "schema_version": 1, "generated_at": plan["created_at"],
        "engine_verified": certificate["passed"], "engine_certificate": certificate,
        "data_evidence_verified": False, "research_only": True,
        "policy_hash": policy_hash, "code_version": code_hash, "acceptance_policy": policy,
        "multiple_testing": {"method": "Bonferroni family-wise correction", "candidate_count": total_candidates,
                             "family_wise_alpha": policy["family_wise_alpha"], "per_candidate_alpha": adjusted_alpha,
                             "limitations": "보정 후 구간도 수익 보장이 아님. 시도한 모든 후보 수를 보수적으로 포함."},
        "data": {"source": dataset.meta.source, "start": dates[0].isoformat() if dates else None,
                 "end": dates[-1].isoformat() if dates else None, "bars": len(dataset.bars),
                 "trading_dates": len(dates), "synthetic": dataset.meta.synthetic, "sha256": fingerprint,
                 "retrieved_at": dataset.meta.retrieved_at, "metadata": asdict(dataset.meta),
                 "missing_sessions": dataset.missing_sessions,
                 "limitations": list(dataset.meta.limitations) + [EVIDENCE_LIMITATION]},
        "risk": asdict(EngineConfig().risk), "costs": asdict(EngineConfig().costs),
        "execution_assumptions": ["봉 마감 신호→다음 봉 시가, 불리한 슬리피지", "동일 봉 손절·익절은 손절 우선",
                                  "거래량 제한·거래정지 시 미체결/부분체결", "미청산 포지션 평가에는 미래 매도비용 미포함",
                                  "갭·유동성 부족 시 손실 한도 초과 가능", "세금·수수료는 연구용 가정, 현재 법정세율 아님"],
        "strategies": [], "real_orders_enabled": False,
    }
    work = []
    selections = []
    any_holdout = False
    development_audits_passed = True
    executable_prices = all(isfinite(b.fx_to_krw) and b.fx_to_krw > 0 for b in dataset.bars)
    symbol_dates: dict[str, set[datetime]] = {}
    for bar in dataset.bars:
        symbol_dates.setdefault(bar.symbol, set()).add(bar.timestamp)
    def observation_count(data: Dataset) -> int:
        counts: dict[str, int] = {}
        for bar in data.bars:
            counts[bar.symbol] = counts.get(bar.symbol, 0) + 1
        return min(counts.values()) if counts else 0
    # Phase 1: development selection for EVERY strategy is frozen before any
    # final-period bar is passed to the engine.
    for strategy in strategies:
        candidates = research_candidates(strategy)
        purge = max(rule.max_holding_bars for rule, _ in candidates)
        split = chronological_split(dates, policy, purge, symbol_dates)
        dev_data = _subset(dataset, dates, split["development"])
        insuff = _data_reasons(dataset, strategy)
        insuff += certificate["reasons"]
        if not executable_prices:
            insuff.append("체결시점 환율 누락: 가격 관찰만 가능하며 원화 포트폴리오 수익률 계산 중단")
        folds = [_subset(dataset, dates, bound) for bound in split["walk_forward"]]
        final_data = _subset(dataset, dates, split["holdout"])
        min_stage = strategy.max_holding_bars + 252 if strategy.key == "E" else policy["min_fold_bars"]
        if observation_count(dev_data) < max(policy["min_development_bars"], min_stage if strategy.key == "E" else 0):
            insuff.append("전략 개발기간 부족")
        for number, fold in enumerate(folds, 1):
            if observation_count(fold) < min_stage:
                insuff.append(f"워크포워드 {number}구간 기간 부족 (보유기간 purge 포함)")
        if observation_count(final_data) < max(policy["min_holdout_bars"], min_stage):
            insuff.append("최종 독립기간 부족 (보유기간 purge 포함)")
        evaluated = []
        if dev_data.bars and executable_prices and certificate["passed"]:
            # Long-horizon data insufficiency does not justify a costly grid.
            for rule, config in candidates if strategy.key != "E" or not any("기간 부족" in r for r in insuff) else candidates[:1]:
                result = run_backtest(dev_data, rule, config)
                stats = performance_metrics(result, dev_data)
                audit = independent_audit(result, dev_data)
                if not audit["passed"]:
                    insuff.append("개발기간 독립 회계 검증 실패")
                    development_audits_passed = False
                # A risk-adjusted score fixed in code; zero-trade alternatives
                # cannot win against a genuinely observed profitable result.
                score = (stats["net_return"] / max(.01, abs(stats["max_drawdown"]))) if stats["trade_count"] else float("-inf")
                evaluated.append((score, rule, config, result, stats))
        if evaluated:
            evaluated.sort(key=lambda item: item[0], reverse=True)
            _, selected, config, dev_result, dev_stats = evaluated[0]
            dev_stage = _stage(dev_result, dev_data)
        else:
            selected, config = candidates[0]
            dev_result, dev_stats = None, {}
            dev_stage = {"executed": False, "reason": "개발 데이터·검증된 환율·현재 엔진 테스트 인증 중 필수 요건 미충족"}
        selection = {"id": strategy.key, "parameters": asdict(selected), "config": asdict(config),
                     "development_candidates_evaluated": len(evaluated),
                     "development_candidates": [{"parameters": asdict(rule), "config": asdict(conf), "metrics": stats}
                                                for _, rule, conf, _, stats in evaluated]}
        selections.append(selection)
        stage_ready = (not any("기간 부족" in r for r in insuff) and bool(final_data.bars)
                       and executable_prices and certificate["passed"])
        any_holdout |= stage_ready
        work.append((strategy, selected, config, split, dev_result, dev_stage, folds, final_data, insuff, stage_ready))
    selection_lock = {"policy_hash": policy_hash, "dataset_hash": fingerprint,
                      "selections": selections, "selected_before_holdout": True}
    selection_lock["selection_hash"] = _digest(selection_lock)
    _write(output / "selection.lock.json", selection_lock)
    if any_holdout:
        registry.parent.mkdir(parents=True, exist_ok=True)
        try:
            registry.mkdir()
        except FileExistsError as error:
            raise ValueError("동일 데이터 최종 독립 검증이 이미 실행 중이거나 완료되었습니다.") from error
        _write(registry / "precommit.json", {**plan, "selection_hash": selection_lock["selection_hash"],
                                            "final_period_consumed": True})
    all_audits_passed = development_audits_passed
    for strategy, selected, config, split, dev_result, dev_stage, folds, final_data, insuff, ready in work:
        entry: dict[str, Any] = {"id": strategy.key, "key": strategy.key, "name": strategy.name,
                                "version": strategy.version, "parameters": asdict(selected),
                                "config": asdict(config), "split": split, "data_period": report["data"],
                                "stages": {"development": dev_stage, "walk_forward": {"executed": False},
                                           "holdout": {"executed": False}},
                                "failed_criteria": [], "insufficient_data_reasons": [],
                                "metrics": {}, "equity_curve": [], "drawdown_curve": [],
                                "regimes": {}, "confidence_interval": {}, "benchmark": {},
                                "cost_sensitivity": {"executed": False}, "parameter_stability": {"executed": False}}
        fails = entry["failed_criteria"]
        display_result = dev_result
        if ready:
            wf_results = [run_backtest(data, selected, config) for data in folds]
            wf_stages = [_stage(result, data) for result, data in zip(wf_results, folds)]
            wf_trades = [trade for result in wf_results for trade in result.trades]
            entry["stages"]["walk_forward"] = {"executed": True, "folds": wf_stages,
                                                      "aggregate_trade_metrics": trade_statistics(wf_trades),
                                                      "configuration_reselected": False}
            wf_ci = block_bootstrap_expectancy(
                wf_trades, alpha=adjusted_alpha, samples=bootstrap_samples,
                seed=policy["seed"], max_holding_bars=selected.max_holding_bars,
                trading_dates=[p.timestamp for result in wf_results for p in result.equity])
            entry["stages"]["walk_forward"]["confidence_interval"] = wf_ci
            if not wf_ci["usable"]:
                insuff.append("워크포워드 기대수익 불확실성 평가 표본/해상도 부족")
            elif wf_ci["lower"] <= 0:
                fails.append("워크포워드 다중검정 보정 기대수익 구간 하한 비양수")
            final = run_backtest(final_data, selected, config)
            display_result = final
            entry["stages"]["holdout"] = _stage(final, final_data)
            all_audits_passed &= all(stage["audit"]["passed"] for stage in wf_stages) and entry["stages"]["holdout"]["audit"]["passed"]
            if any(not stage["audit"]["passed"] for stage in wf_stages + [entry["stages"]["holdout"]]):
                insuff.append("독립 체결·회계 교차 검증 실패")
            criteria = {**policy, **(policy["long_horizon"] if strategy.key == "E" else {})}
            if len(wf_trades) < criteria["minimum_trades_walk_forward"]:
                insuff.append("워크포워드 거래 표본 부족")
            if len(final.trades) < criteria["minimum_trades_holdout"]:
                insuff.append("최종 독립 거래 표본 부족")
            stats = performance_metrics(final, final_data)
            if stats["net_return"] <= 0:
                fails.append("최종 독립기간 비용 차감 수익성 미달")
            if stats["max_drawdown"] < -criteria["maximum_drawdown"]:
                fails.append("최대 낙폭 기준 초과")
            profitable_fraction = sum(stage["metrics"]["net_return"] > 0 for stage in wf_stages) / len(wf_stages)
            if profitable_fraction < criteria["minimum_profitable_fold_fraction"]:
                fails.append("반복 워크포워드 수익 안정성 부족")
            if sum(t.net_pnl_krw for t in wf_trades) <= 0:
                fails.append("워크포워드 순실현손익 비양수")
            ci = block_bootstrap_expectancy(final.trades, alpha=adjusted_alpha, samples=bootstrap_samples,
                                           seed=policy["seed"], max_holding_bars=selected.max_holding_bars,
                                           trading_dates=[p.timestamp for p in final.equity])
            entry["confidence_interval"] = ci
            if not ci["usable"]:
                insuff.append(ci["reason"])
            elif ci["lower"] <= 0:
                fails.append("다중검정 보정 거래 기대수익 구간 하한 비양수")
            regimes = regime_statistics(final.trades)
            for regime in criteria["required_regimes"]:
                if regimes[regime]["trade_count"] < criteria["minimum_regime_trades"]:
                    insuff.append(f"{regime} 시장 국면 거래 표본 부족")
                elif regimes[regime]["expectancy"] <= 0:
                    fails.append(f"{regime} 시장 국면 기대손익 비양수")
            benchmarks = stats.get("benchmark", {})
            valid_benchmarks = [b for b in benchmarks.values() if b.get("excess_return") is not None]
            if not valid_benchmarks:
                insuff.append("동일기간 적절한 벤치마크 미확보")
            elif policy["require_benchmark_outperformance"] and any(b["excess_return"] < 0 for b in valid_benchmarks):
                fails.append("벤치마크 대비 초과성과 미달")
            if final.open_positions or any(result.open_positions for result in wf_results):
                insuff.append("구간말 미청산 포지션: 매도비용·최종 체결 불확실")
            if final.halted_reason or any(result.halted_reason for result in wf_results):
                fails.append("사전 위험 한도 도달로 전략 중단")
            multiplier = policy["cost_multiplier"]
            stressed_config = replace(config, costs=replace(config.costs, **{key: value * multiplier for key, value in asdict(config.costs).items()}))
            stressed = run_backtest(final_data, selected, stressed_config)
            stressed_stage = _stage(stressed, final_data)
            entry["cost_sensitivity"] = {"executed": True, "multiplier": multiplier, **stressed_stage,
                                          "retuned": False}
            if stressed_stage["metrics"]["net_return"] <= 0:
                fails.append("거래비용 2배 민감도 검사 수익성 미달")
            if not stressed_stage["audit"]["passed"]:
                insuff.append("비용 민감도 독립 회계 검증 실패")
                all_audits_passed = False
            neighbors = []
            # Neighbours are diagnostics only. Their results never replace the
            # dev-selected configuration or change the fixed pass thresholds.
            for rule, conf in robustness_neighbors(strategy, selected, config):
                neighbor_result = run_backtest(final_data, rule, conf)
                stage = _stage(neighbor_result, final_data)
                neighbors.append({"parameters": asdict(rule), "config": asdict(conf), **stage})
                if not stage["audit"]["passed"]:
                    insuff.append("인접 파라미터 독립 회계 검증 실패")
                    all_audits_passed = False
            neighbor_fraction = sum(n["metrics"]["net_return"] > 0 for n in neighbors) / len(neighbors)
            entry["parameter_stability"] = {"executed": True, "profitable_fraction": neighbor_fraction,
                                              "neighbors": neighbors, "retuned": False}
            if neighbor_fraction < policy["minimum_neighbor_profitable_fraction"] or any(n["metrics"]["net_return"] < policy["minimum_neighbor_return"] for n in neighbors):
                fails.append("인접 파라미터 안정성 미달")
        else:
            entry["stages"]["walk_forward"]["reason"] = "기간·환율·현재 엔진 테스트 인증 필수 요건 부족: 미실행"
            entry["stages"]["holdout"]["reason"] = "기간·환율·현재 엔진 테스트 인증 필수 요건 부족: 미개봉"
        if display_result is not None:
            display_data = final_data if ready else _subset(dataset, dates, split["development"])
            entry["metrics"] = performance_metrics(display_result, display_data)
            entry["metrics"]["trusted"] = False
            entry["metrics"]["period_role"] = "holdout" if ready else "development_only"
            entry["benchmark"] = entry["metrics"].get("benchmark", {})
            entry["regimes"] = regime_statistics(display_result.trades)
            entry["equity_curve"] = [{"date": p.timestamp.isoformat(), "equity": p.equity_krw} for p in display_result.equity]
            entry["drawdown_curve"] = [{"date": p.timestamp.isoformat(), "drawdown": p.drawdown} for p in display_result.equity]
            entry["warnings"] = display_result.warnings
        if dev_stage.get("executed") and not dev_stage["audit"]["passed"]:
            all_audits_passed = False
        entry["insufficient_data_reasons"] = sorted(set(insuff))
        entry["status"] = "DATA_INSUFFICIENT" if insuff else "FAIL" if fails else "PASS"
        entry["status_label"] = {"PASS": "합격", "FAIL": "불합격", "DATA_INSUFFICIENT": "데이터 부족"}[entry["status"]]
        entry["reason"] = " / ".join(entry["insufficient_data_reasons"] + fails)
        report["strategies"].append(entry)
    report["engine_verified"] = certificate["passed"] and all_audits_passed
    report["independent_accounting_audits_passed"] = all_audits_passed
    report["selection_hash"] = selection_lock["selection_hash"]
    report["holdout_consumed"] = any_holdout
    passed = [entry["id"] for entry in report["strategies"] if entry["status"] == "PASS"]
    report["decision"] = "연구 기준 합격: 실전 주문 활성화 없음" if passed else "합격 전략 없음: 투자하지 않음"
    report["approved_strategies"] = passed
    report = _safe(report)
    _write(output / "report.json", report)
    if any_holdout:
        _write(registry / "report.json", report)
    return report
