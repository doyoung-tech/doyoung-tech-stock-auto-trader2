"""Independent importer/security tests; all inline prices are fictional fixtures."""
import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from stock_validator.data import (DataError, _download, _fx_rates, _yahoo_bars,
                                  collect_daily, load_dataset, make_synthetic_dataset,
                                  save_dataset)
from stock_validator.models import Dataset, Fundamental


def test_synthetic_is_explicit_and_roundtrips_without_real_data_claim(tmp_path):
    dataset = make_synthetic_dataset(20)
    assert dataset.meta.synthetic and "SYNTHETIC" in dataset.meta.source
    assert not dataset.meta.corporate_actions_complete
    loaded = load_dataset(save_dataset(dataset, tmp_path / "fictional"))
    assert loaded.bars == dataset.bars
    assert loaded.meta.synthetic
    assert len(loaded.meta.sha256) == 64


def test_import_rejects_tampered_csv(tmp_path):
    path = save_dataset(make_synthetic_dataset(10), tmp_path / "tampered")
    with (path / "prices.csv").open("ab") as output:
        output.write(b"fake record\n")
    with pytest.raises(DataError, match="SHA-256"):
        load_dataset(path)


@pytest.mark.parametrize("change", [
    {"timestamp": datetime(2020, 1, 1)}, {"open": -1}, {"high": 1},
    {"volume": -1}, {"close": math.nan}, {"high": math.inf},
    {"fx_to_krw": 0}, {"fx_to_krw": math.inf}, {"currency": "EUR"},
])
def test_import_rejects_invalid_prices(tmp_path, change):
    dataset = make_synthetic_dataset(10)
    dataset.bars[0] = replace(dataset.bars[0], **change)
    with pytest.raises(DataError):
        save_dataset(dataset, tmp_path / "bad")


def test_duplicate_and_unsorted_bars_rejected(tmp_path):
    dataset = make_synthetic_dataset(10)
    dataset.bars.insert(1, dataset.bars[0])
    with pytest.raises(DataError, match="중복"):
        save_dataset(dataset, tmp_path / "duplicate")
    dataset = make_synthetic_dataset(10)
    dataset.bars.reverse()
    with pytest.raises(DataError, match="순서"):
        save_dataset(dataset, tmp_path / "unsorted")


def test_missing_usd_fx_is_prices_only_not_one(tmp_path):
    dataset = make_synthetic_dataset(10)
    dataset.bars = [replace(bar, currency="USD", fx_to_krw=math.nan) for bar in dataset.bars]
    result = load_dataset(save_dataset(dataset, tmp_path / "usd"))
    assert math.isnan(result.bars[0].fx_to_krw)
    assert not result.meta.fx_complete
    assert any("prices_only=true" in reason for reason in result.meta.limitations)
    dataset.meta = replace(dataset.meta, fx_complete=True)
    with pytest.raises(DataError, match="환율 결측"):
        save_dataset(dataset, tmp_path / "false-claim")


def test_known_fundamental_requires_actual_available_timestamp(tmp_path):
    dataset = make_synthetic_dataset(10)
    dataset.fundamentals = [Fundamental("DEMO-A", datetime(2020, 1, 1, tzinfo=timezone.utc),
                                      datetime(2020, 2, 1, tzinfo=timezone.utc), source="fictional test")]
    with pytest.raises(DataError, match="종료 전"):
        save_dataset(dataset, tmp_path / "future")


def test_metadata_declaration_and_hashed_evidence_cannot_authorize_pass(tmp_path):
    dataset = make_synthetic_dataset(10)
    dataset.meta = replace(dataset.meta, synthetic=False, corporate_actions_complete=True,
                           point_in_time_universe=True)
    path = save_dataset(dataset, tmp_path / "declaration")
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["evidence"] = {"corporate_actions_complete": {
        "files": ["prices.csv"], "reviewer": "fictional reviewer", "notes": "unverified claim"}}
    (path / "manifest.json").write_text(json.dumps(manifest))
    result = load_dataset(path)
    assert not result.meta.corporate_actions_complete
    assert not result.meta.point_in_time_universe
    assert any("미구현" in reason for reason in result.meta.limitations)


def test_manifest_cannot_read_parent_or_symlink(tmp_path):
    path = save_dataset(make_synthetic_dataset(10), tmp_path / "escape")
    outside = tmp_path / "outside.csv"
    outside.write_text("outside")
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["files"]["../outside.csv"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DataError, match="経|경로"):
        load_dataset(path)
    del manifest["files"]["../outside.csv"]
    (path / "prices.csv").unlink()
    (path / "prices.csv").symlink_to(outside)
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DataError, match="심볼릭"):
        load_dataset(path)


def test_saving_preserves_existing_user_files(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "notes").write_text("preserve")
    with pytest.raises(FileExistsError):
        save_dataset(make_synthetic_dataset(10), existing)
    assert (existing / "notes").read_text() == "preserve"


def test_network_failure_has_no_fake_dataset(monkeypatch, tmp_path):
    import urllib.error
    import stock_validator.data as data

    def fail(_url):
        raise urllib.error.URLError("Tunnel connection failed: 403 Forbidden")

    monkeypatch.setattr(data, "_download", fail)
    output = tmp_path / "blocked"
    report = collect_daily(["005930.KS", "SPY"], "2015-01-01", "2020-01-01", output)
    assert report["status"] == "failed" and report["actual_bars"] == 0
    assert not report["synthetic"]
    assert all(record["status"] == "failed" for record in report["records"])
    assert (output / "collection.json").exists()
    assert not (output / "prices.csv").exists()
    assert not (output / "manifest.json").exists()


def test_untrusted_url_rejected_without_network():
    with pytest.raises(DataError, match="HTTPS"):
        _download("http://query1.finance.yahoo.com/v8/finance/chart/SPY")
    with pytest.raises(DataError, match="HTTPS"):
        _download("https://127.0.0.1/admin")


def test_bad_symbols_and_date_ranges_never_reach_network(tmp_path):
    for symbols, start, end in [(["../../etc"], "2020-01-01", "2020-01-02"),
                                (["SPY"], "2020-02-01", "2020-01-01"),
                                (["SPY", "SPY"], "2020-01-01", "2020-01-02")]:
        with pytest.raises(DataError):
            collect_daily(symbols, start, end, tmp_path / "unused")
    assert not (tmp_path / "unused").exists()


def _fictional_yahoo_response():
    return json.dumps({"chart": {"result": [{"meta": {"currency": "USD", "exchangeTimezoneName": "America/New_York"},
                   "timestamp": [1577975400], "indicators": {"quote": [{"open": [10.0], "high": [12.0],
                   "low": [9.0], "close": [11.0], "volume": [1000]}]}}], "error": None}}).encode()


def test_yahoo_fixture_uses_market_close_aware_and_no_fake_fx():
    from datetime import date
    bars = _yahoo_bars(_fictional_yahoo_response(), "FICTIONAL", date(2020, 1, 2), date(2020, 1, 2))
    assert bars[0].timestamp == datetime(2020, 1, 2, 21, tzinfo=timezone.utc)
    assert math.isnan(bars[0].fx_to_krw)


def test_successful_fixture_collection_preserves_raw_and_fx_provenance(monkeypatch, tmp_path):
    import stock_validator.data as data
    monkeypatch.setattr(data, "_download", lambda _url: _fictional_yahoo_response())
    fx = tmp_path / "audited-fx.csv"
    fx.write_text("available_at,fx_to_krw\n2020-01-01T20:00:00Z,1150\n2020-01-02T22:00:00Z,9999\n")
    output = tmp_path / "fixture-download"
    report = collect_daily(["FICTIONAL"], "2020-01-02", "2020-01-02", output, fx_path=fx)
    loaded = load_dataset(output)
    assert report["status"] == "downloaded"
    assert loaded.bars[0].fx_to_krw == 1150  # Later release must not leak.
    assert (output / "raw" / "000.json").exists()
    assert (output / "raw" / "fx_available_at.csv").exists()
    assert not loaded.meta.corporate_actions_complete
    # This fixture is only an importer test; the provider monkeypatch is never used by normal collection.


def test_fx_requires_aware_unique_positive_publication_times(tmp_path):
    path = tmp_path / "fx.csv"
    for text in ["2020-01-01,1000", "2020-01-01T00:00:00Z,-1",
                 "2020-01-01T00:00:00Z,1000\n2020-01-01T00:00:00Z,1100"]:
        path.write_text("available_at,fx_to_krw\n" + text + "\n")
        with pytest.raises(DataError):
            _fx_rates(path)


@pytest.mark.parametrize("body", ["[]", "null", "0", '"string"', "[" * 1100 + "]" * 1100])
def test_malformed_manifest_is_controlled_data_error(tmp_path, body):
    (tmp_path / "manifest.json").write_text(body)
    with pytest.raises(DataError):
        load_dataset(tmp_path)


def test_provider_invalid_and_missing_bars_quarantined_without_price_edits(tmp_path):
    from datetime import date
    payload = json.loads(_fictional_yahoo_response())
    result = payload["chart"]["result"][0]
    result["timestamp"] = [1577975400, 1578061800, 1578321000]
    result["indicators"]["quote"][0] = {"open": [10, None, 10], "high": [12, None, 12],
           "low": [9, None, 9], "close": [11, None, 8], "volume": [1000, None, 1000]}
    gaps, issues = [], []
    bars = _yahoo_bars(json.dumps(payload).encode(), "FICTIONAL", date(2020, 1, 2), date(2020, 1, 6), gaps, issues)
    assert len(bars) == 1 and bars[0].close == 11
    assert len(gaps) == 2
    assert [item["reason"] for item in issues] == ["provider_all_null", "provider_inconsistent_ohlcv"]
    assert issues[-1]["ohlcv"][3] == 8  # Original inconsistent value preserved, never fixed.
    dataset = make_synthetic_dataset(10)
    dataset.bars = [replace(bars[0], currency="KRW", fx_to_krw=1)]
    dataset.missing_sessions = {"FICTIONAL": gaps}
    loaded = load_dataset(save_dataset(dataset, tmp_path / "gaps"))
    assert loaded.missing_sessions == dataset.missing_sessions
    assert len(loaded.bars) == 1


def test_partial_missing_provider_row_still_rejected():
    from datetime import date
    payload = json.loads(_fictional_yahoo_response())
    payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"] = [None]
    with pytest.raises(DataError, match="OHLCV 결측"):
        _yahoo_bars(json.dumps(payload).encode(), "FICTIONAL", date(2020, 1, 2), date(2020, 1, 2), [], [])


def test_benchmark_separate_and_requested_once(monkeypatch, tmp_path):
    import stock_validator.data as data
    calls = []
    def fake(url):
        calls.append(url)
        return _fictional_yahoo_response()
    monkeypatch.setattr(data, "_download", fake)
    output = tmp_path / "benchmark"
    report = collect_daily(["FICTIONAL", "^GSPC"], "2020-01-02", "2020-01-02", output,
                           benchmark_symbol="^GSPC")
    dataset = load_dataset(output)
    assert len(calls) == 2
    assert {bar.symbol for bar in dataset.bars} == {"FICTIONAL"}
    assert {bar.symbol for bar in dataset.benchmark_bars} == {"^GSPC"}
    assert report["actual_tradable_bars"] == report["benchmark_bars"] == 1
