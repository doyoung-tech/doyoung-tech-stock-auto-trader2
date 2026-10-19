"""Auditable daily data import. No broker, credentials, or synthetic fallback.

Provider URLs are fixed HTTPS origins. Public availability does not grant a data
redistribution licence; callers must check their provider's terms before use.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import random
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from .models import Bar, Dataset, DatasetMeta, Fundamental

UTC = timezone.utc
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_DATASET_FILES = 256
MAX_ROWS = 2_000_000
BAR_FIELDS = ("timestamp", "symbol", "open", "high", "low", "close", "volume",
              "currency", "fx_to_krw", "tradable")
FUND_FIELDS = tuple(Fundamental.__dataclass_fields__)
EVIDENCE_FLAGS = ("survivorship_bias_controlled", "point_in_time_universe",
                  "corporate_actions_complete", "point_in_time_fundamentals",
                  "sector_history_complete")


class DataError(ValueError):
    """Invalid, unverified, or unsupported input; never silently repaired."""


def _datetime(value: str, field: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise DataError(f"{field}: ISO 8601 문자열이 필요합니다.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise DataError(f"{field}: ISO 8601 날짜·시각이 필요합니다.") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise DataError(f"{field}: 시간대가 필요합니다.")
    return result.astimezone(UTC)


def _number(value: Any, field: str, optional: bool = False) -> float | None:
    if optional and value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DataError(f"{field}: 숫자가 필요합니다.") from exc
    if not math.isfinite(result):
        raise DataError(f"{field}: 유한한 숫자가 필요합니다.")
    return result


def _boolean(value: Any, field: str, optional: bool = False) -> bool | None:
    if optional and value in (None, ""):
        return None
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise DataError(f"{field}: true/false만 허용합니다.")


def _validate_bars(bars: list[Bar], meta: DatasetMeta, label: str) -> None:
    previous: tuple[datetime, str] | None = None
    for bar in bars:
        if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
            raise DataError(f"{label}: 봉 마감 시각에 시간대가 필요합니다.")
        if not bar.symbol or len(bar.symbol) > 64 or any(c.isspace() for c in bar.symbol):
            raise DataError(f"{label}: 종목코드 형식 오류")
        key = (bar.timestamp, bar.symbol)
        if previous is not None and key <= previous:
            raise DataError(f"{label}: timestamp, symbol 순서와 중복을 확인하세요.")
        previous = key
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(not math.isfinite(v) for v in values):
            raise DataError(f"{label}: OHLCV는 유한해야 합니다.")
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
            raise DataError(f"{label}: 가격은 양수, 거래량은 0 이상이어야 합니다.")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise DataError(f"{label}: OHLC 범위가 일치하지 않습니다.")
        if bar.currency not in {"KRW", "USD"}:
            raise DataError(f"{label}: 지원 통화는 KRW/USD입니다.")
        if bar.currency == "KRW" and bar.fx_to_krw != 1.0:
            raise DataError(f"{label}: KRW의 원화 환산계수는 1이어야 합니다.")
        if not math.isfinite(bar.fx_to_krw):
            if bar.currency != "USD" or meta.fx_complete or not math.isnan(bar.fx_to_krw):
                raise DataError(f"{label}: 환율 결측은 fx_complete=false인 USD만 허용합니다.")
        elif bar.fx_to_krw <= 0:
            raise DataError(f"{label}: 환율은 양수여야 합니다.")


def _validate_dataset(dataset: Dataset) -> None:
    if not dataset.bars:
        raise DataError("실제 가격 봉이 없습니다.")
    retrieved = _datetime(dataset.meta.retrieved_at, "retrieved_at")
    _validate_bars(dataset.bars, dataset.meta, "bars")
    _validate_bars(dataset.benchmark_bars, dataset.meta, "benchmark")
    _validate_bars(dataset.sector_bars, dataset.meta, "sector_bars")
    if not dataset.meta.synthetic and any(bar.timestamp > retrieved for bar in dataset.bars + dataset.benchmark_bars + dataset.sector_bars):
        raise DataError("실제 가격 봉의 시각이 수집 시각보다 미래입니다.")
    last_fundamental = None
    for item in dataset.fundamentals:
        if (item.available_at.tzinfo is None or item.period_end.tzinfo is None
                or item.available_at.utcoffset() is None or item.period_end.utcoffset() is None):
            raise DataError("재무자료의 available_at/period_end에 시간대가 필요합니다.")
        if item.period_end > item.available_at:
            raise DataError("재무 기간 종료 전에 공시된 값으로 처리할 수 없습니다.")
        key = (item.available_at, item.symbol, item.period_end)
        if last_fundamental is not None and key <= last_fundamental:
            raise DataError("재무자료는 available_at, symbol, period_end 순으로 유일해야 합니다.")
        last_fundamental = key
        if not item.source:
            raise DataError("재무자료의 발표 당시 원문 출처가 필요합니다.")
        if item.available_at > retrieved and not dataset.meta.synthetic:
            raise DataError("실제 재무자료의 발표 시각이 수집 시각보다 미래입니다.")
        if any(value is not None and not isinstance(value, bool) for value in (item.profitable, item.formerly_large_cap)):
            raise DataError("재무자료의 boolean 필드 형식 오류")
        for field in ("revenue_growth", "earnings_growth", "forecast_revision",
                      "debt_to_assets", "market_cap_krw"):
            value = getattr(item, field)
            if value is not None and not math.isfinite(value):
                raise DataError(f"재무자료 {field}: 유한한 숫자가 필요합니다.")
        if any(value is not None and value < 0 for value in (item.market_cap_krw, item.debt_to_assets)):
            raise DataError("시가총액과 부채/자산비율은 음수일 수 없습니다.")
    for symbol, intervals in dataset.sectors.items():
        previous_end = None
        for i, (start, end, sector) in enumerate(intervals):
            if start.tzinfo is None or (end is not None and end.tzinfo is None):
                raise DataError("업종 이력은 시간대가 필요합니다.")
            if not symbol or not sector or (end is not None and end <= start):
                raise DataError("업종 이력의 종목/기간/업종 오류")
            if i and (previous_end is None or start < previous_end):
                raise DataError("업종 이력의 유효기간이 겹칩니다.")
            previous_end = end
    bar_keys = {(bar.timestamp, bar.symbol) for bar in dataset.bars + dataset.benchmark_bars + dataset.sector_bars}
    for symbol, timestamps in dataset.missing_sessions.items():
        for timestamp in timestamps:
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise DataError("결측 세션의 시각에 시간대가 필요합니다.")
            if not dataset.meta.synthetic and timestamp > retrieved:
                raise DataError("실제 결측 세션의 시각이 수집 시각보다 미래입니다.")
            if (timestamp, symbol) in bar_keys:
                raise DataError("동일 세션을 실제 봉과 결측으로 동시에 기록할 수 없습니다.")
        if not symbol or timestamps != sorted(set(timestamps)):
            raise DataError("미확인 결측 세션은 종목별 시각 순서대로 유일해야 합니다.")


def _csv_bytes(fields: tuple[str, ...], rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        cooked = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                value = value.astimezone(UTC).isoformat()
            elif isinstance(value, bool):
                value = "true" if value else "false"
            elif value is None or (isinstance(value, float) and math.isnan(value)):
                value = ""
            cooked[key] = value
        writer.writerow(cooked)
    return stream.getvalue().encode("utf-8")


def _content_hash(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        if name.endswith(".csv"):
            digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


def save_dataset(dataset: Dataset, path: str | Path) -> Path:
    """Write a new dataset directory. Existing nonempty directories are preserved."""
    _validate_dataset(dataset)
    target = Path(path).expanduser().absolute()
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise FileExistsError(f"기존 경로를 덮어쓰지 않습니다: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    files = {"prices.csv": _csv_bytes(BAR_FIELDS, [asdict(b) for b in dataset.bars])}
    if dataset.benchmark_bars:
        files["benchmark.csv"] = _csv_bytes(BAR_FIELDS, [asdict(b) for b in dataset.benchmark_bars])
    if dataset.sector_bars:
        files["sector_bars.csv"] = _csv_bytes(BAR_FIELDS, [asdict(b) for b in dataset.sector_bars])
    if dataset.fundamentals:
        files["fundamentals.csv"] = _csv_bytes(FUND_FIELDS, [asdict(f) for f in dataset.fundamentals])
    if dataset.sectors:
        rows = [{"symbol": symbol, "effective_from": start, "effective_to": end, "sector": sector}
                for symbol, spans in sorted(dataset.sectors.items()) for start, end, sector in spans]
        files["sectors.csv"] = _csv_bytes(("symbol", "effective_from", "effective_to", "sector"), rows)
    if dataset.missing_sessions:
        rows = [{"symbol": symbol, "timestamp": timestamp, "reason": "provider_unusable_ohlcv_unknown_session"}
                for symbol, timestamps in sorted(dataset.missing_sessions.items()) for timestamp in timestamps]
        files["missing_sessions.csv"] = _csv_bytes(("symbol", "timestamp", "reason"), rows)
    meta = replace(dataset.meta, sha256=_content_hash(files))
    manifest = {"schema_version": 1, "meta": asdict(meta),
                "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
                "period": {"start": dataset.bars[0].timestamp.isoformat(),
                           "end": dataset.bars[-1].timestamp.isoformat(),
                           "bars": len(dataset.bars), "symbols": sorted({b.symbol for b in dataset.bars})}}
    with tempfile.TemporaryDirectory(prefix=".dataset-", dir=target.parent) as tmp:
        staging = Path(tmp) / "dataset"
        staging.mkdir()
        for name, body in files.items():
            (staging / name).write_bytes(body)
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if target.exists():
            target.rmdir()  # Only the empty directory allowed above.
        staging.rename(target)
    return target


def _safe_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or "\\" in name or PurePosixPath(name).is_absolute():
        raise DataError("manifest 파일 경로 오류")
    parts = PurePosixPath(name).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise DataError("manifest 경로 이동을 허용하지 않습니다.")
    candidate = root.joinpath(*parts)
    if any(root.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1)):
        raise DataError("데이터 파일 심볼릭 링크를 허용하지 않습니다.")
    if not candidate.is_file() or candidate.stat().st_size > MAX_FILE_BYTES:
        raise DataError(f"파일 누락 또는 크기 초과: {name}")
    return candidate


def _rows(body: bytes, required: tuple[str, ...], label: str) -> list[dict[str, str]]:
    try:
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline=""))
        if reader.fieldnames != list(required):
            raise DataError(f"{label}: 열 이름과 순서를 확인하세요. 필요: {','.join(required)}")
        result = []
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise DataError(f"{label}: CSV 열 수가 맞지 않습니다.")
            result.append(row)
            if len(result) > MAX_ROWS:
                raise DataError(f"{label}: 행 수 제한 초과")
        return result
    except (UnicodeDecodeError, csv.Error) as exc:
        raise DataError(f"{label}: UTF-8 CSV 파싱 실패") from exc


def _parse_bars(body: bytes, label: str) -> list[Bar]:
    bars = []
    for row in _rows(body, BAR_FIELDS, label):
        bars.append(Bar(timestamp=_datetime(row["timestamp"]), symbol=row["symbol"],
                        **{key: _number(row[key], key) for key in ("open", "high", "low", "close", "volume")},
                        currency=row["currency"], fx_to_krw=math.nan if row["fx_to_krw"] == "" else _number(row["fx_to_krw"], "fx_to_krw"),
                        tradable=_boolean(row["tradable"], "tradable")))
    return bars


def load_dataset(path: str | Path) -> Dataset:
    root = Path(path).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise DataError("데이터셋 디렉터리가 필요합니다.")
    manifest_path = _safe_file(root, "manifest.json")
    if manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise DataError("manifest 크기 제한 초과")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise DataError("manifest JSON 파싱 실패") from exc
    if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
            or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict)):
        raise DataError("manifest schema_version=1과 files 체크섬이 필요합니다.")
    if len(manifest["files"]) > MAX_DATASET_FILES:
        raise DataError("데이터셋 파일 수 제한 초과")
    files = {}
    total_bytes = 0
    for name, expected in manifest["files"].items():
        file = _safe_file(root, name)
        total_bytes += file.stat().st_size
        if total_bytes > MAX_DATASET_BYTES:
            raise DataError("데이터셋 전체 크기 제한 초과")
        body = file.read_bytes()
        if not isinstance(expected, str) or hashlib.sha256(body).hexdigest() != expected:
            raise DataError(f"SHA-256 무결성 실패: {name}")
        files[name] = body
    if "prices.csv" not in files:
        raise DataError("검증된 prices.csv가 필요합니다.")
    try:
        raw_meta = dict(manifest["meta"])
        for flag in ("synthetic", "fx_complete", *EVIDENCE_FLAGS):
            if flag in raw_meta and not isinstance(raw_meta[flag], bool):
                raise DataError(f"meta.{flag}: JSON boolean이 필요합니다.")
        if not isinstance(raw_meta.get("limitations", []), list):
            raise DataError("limitations는 문자열 목록이어야 합니다.")
        raw_meta["limitations"] = tuple(raw_meta.get("limitations", ()))
        if any(not isinstance(x, str) for x in raw_meta["limitations"]):
            raise DataError("limitations는 문자열 목록이어야 합니다.")
        meta = DatasetMeta(**raw_meta)
    except (KeyError, TypeError) as exc:
        raise DataError("manifest meta 필드를 확인하세요.") from exc
    if any(not isinstance(getattr(meta, name), str) for name in ("source", "retrieved_at", "price_basis", "license_note", "sha256")):
        raise DataError("meta 출처·시각·가격기준·라이선스·체크섬은 문자열이어야 합니다.")
    if not meta.source or not meta.license_note:
        raise DataError("출처와 데이터 이용권한/제한 메모가 필요합니다.")
    _datetime(meta.retrieved_at, "retrieved_at")
    if meta.sha256 != _content_hash(files):
        raise DataError("전체 CSV SHA-256 무결성 실패")
    limitations = list(meta.limitations)
    overrides = {}
    evidence = manifest.get("evidence", {})
    if not isinstance(evidence, dict):
        raise DataError("evidence는 객체여야 합니다.")
    for flag in EVIDENCE_FLAGS:
        if not getattr(meta, flag):
            continue
        audit = evidence.get(flag, {})
        verified = (isinstance(audit, dict) and isinstance(audit.get("files"), list)
                    and bool(audit["files"]) and all(isinstance(name, str) and name in files for name in audit["files"])
                    and bool(audit.get("reviewer")) and bool(audit.get("notes")))
        overrides[flag] = False
        limitations.append(f"{flag}=false: " + ("증빙 해시만 확인했으며 레코드 의미·포함범위·기업행위 회계의 자동 검증기는 미구현입니다." if verified else "감사 증빙이 없으며 사용자 선언만으로 품질을 인정하지 않습니다."))
    meta = replace(meta, limitations=tuple(dict.fromkeys(limitations)), **overrides)
    fundamentals = []
    for row in _rows(files["fundamentals.csv"], FUND_FIELDS, "fundamentals") if "fundamentals.csv" in files else []:
        fundamentals.append(Fundamental(symbol=row["symbol"], available_at=_datetime(row["available_at"], "available_at"),
                                        period_end=_datetime(row["period_end"], "period_end"), source=row["source"],
                                        **{key: _number(row[key], key, optional=True) for key in ("revenue_growth", "earnings_growth", "forecast_revision", "debt_to_assets", "market_cap_krw")},
                                        profitable=_boolean(row["profitable"], "profitable", optional=True),
                                        formerly_large_cap=_boolean(row["formerly_large_cap"], "formerly_large_cap", optional=True)))
    sectors: dict[str, list] = {}
    for row in _rows(files["sectors.csv"], ("symbol", "effective_from", "effective_to", "sector"), "sectors") if "sectors.csv" in files else []:
        sectors.setdefault(row["symbol"], []).append((_datetime(row["effective_from"]),
                           _datetime(row["effective_to"]) if row["effective_to"] else None, row["sector"]))
    dataset = Dataset(_parse_bars(files["prices.csv"], "prices"), meta, fundamentals,
                      _parse_bars(files["benchmark.csv"], "benchmark") if "benchmark.csv" in files else [], sectors)
    dataset.sector_bars = _parse_bars(files["sector_bars.csv"], "sector_bars") if "sector_bars.csv" in files else []
    if "missing_sessions.csv" in files:
        for row in _rows(files["missing_sessions.csv"], ("symbol", "timestamp", "reason"), "missing_sessions"):
            if row["reason"] not in {"provider_all_null_unknown_session", "provider_unusable_ohlcv_unknown_session"}:
                raise DataError("알 수 없는 결측 세션 원인 분류")
            dataset.missing_sessions.setdefault(row["symbol"], []).append(_datetime(row["timestamp"]))
    _validate_dataset(dataset)
    if any(b.currency == "USD" and not math.isfinite(b.fx_to_krw) for b in dataset.bars + dataset.benchmark_bars + dataset.sector_bars):
        dataset.meta = replace(dataset.meta, fx_complete=False, limitations=tuple(dict.fromkeys((*dataset.meta.limitations,
                               "prices_only=true: 실제 시점 환율이 없어 USD 전략 수익률을 계산할 수 없습니다."))))
    return dataset


class _FixedOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hostname: str):
        self.hostname = hostname

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if (parsed.scheme != "https" or parsed.hostname != self.hostname or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment):
            raise DataError("외부 origin으로의 리디렉션을 거부했습니다.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _download(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in {"query1.finance.yahoo.com", "stooq.com", "fred.stlouisfed.org"}
            or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
        raise DataError("지원되는 HTTPS 제공처만 허용합니다.")
    opener = urllib.request.build_opener(_FixedOriginRedirect(parsed.hostname))
    request = urllib.request.Request(url, headers={"User-Agent": "stock-validator-research/0.1", "Accept": "application/json,text/csv;q=0.9,*/*;q=0.1"})
    # The inherited proxy and system CA trust remain enabled.
    with opener.open(request, timeout=15) as response:
        body = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(body) > MAX_DOWNLOAD_BYTES:
        raise DataError("데이터 응답 크기 제한 초과")
    return body


def _session_close(day: date, currency: str) -> datetime:
    # Nominal regular close; using 16:00 on early-close US sessions is conservative.
    zone = ZoneInfo("Asia/Seoul" if currency == "KRW" else "America/New_York")
    close = time(15, 30) if currency == "KRW" else time(16, 0)
    return datetime.combine(day, close, zone).astimezone(UTC)


def _yahoo_bars(body: bytes, symbol: str, start: date, end: date,
                missing_sessions: list[datetime] | None = None,
                issues: list[dict[str, Any]] | None = None) -> list[Bar]:
    try:
        chart = json.loads(body)["chart"]
        if chart.get("error"):
            raise DataError(f"Yahoo provider error: {chart['error'].get('code', 'unknown')}")
        result = chart["result"][0]
        currency = result["meta"]["currency"]
        if currency not in {"KRW", "USD"}:
            raise DataError(f"지원하지 않는 통화: {currency}")
        zone = ZoneInfo(result["meta"]["exchangeTimezoneName"])
        quotes = result["indicators"]["quote"][0]
        bars = []
        for i, timestamp in enumerate(result["timestamp"]):
            day = datetime.fromtimestamp(timestamp, UTC).astimezone(zone).date()
            if not start <= day <= end:
                continue
            values = [quotes[key][i] for key in ("open", "high", "low", "close", "volume")]
            if all(value is None for value in values) and missing_sessions is not None:
                missing_sessions.append(_session_close(day, currency))
                if issues is not None:
                    issues.append({"timestamp": _session_close(day, currency).isoformat(), "reason": "provider_all_null", "ohlcv": values})
                continue
            if any(value is None for value in values):
                raise DataError(f"{symbol} {day}: OHLCV 결측. 임의 보간하지 않습니다.")
            opening, high, low, closing, volume = map(float, values)
            invalid = (any(not math.isfinite(value) for value in (opening, high, low, closing, volume))
                       or min(opening, high, low, closing) <= 0 or volume < 0
                       or not low <= min(opening, closing) <= max(opening, closing) <= high)
            if invalid:
                if missing_sessions is None:
                    raise DataError(f"{symbol} {day}: 원본 OHLCV 범위 오류. 임의 수정하지 않습니다.")
                missing_sessions.append(_session_close(day, currency))
                if issues is not None:
                    issues.append({"timestamp": _session_close(day, currency).isoformat(), "reason": "provider_inconsistent_ohlcv", "ohlcv": values})
                continue
            bars.append(Bar(_session_close(day, currency), symbol, opening, high, low, closing, volume, currency,
                            1.0 if currency == "KRW" else math.nan, volume > 0))
        if not bars:
            raise DataError(f"{symbol}: 요청 기간의 실제 봉이 없습니다.")
        return bars
    except DataError:
        raise
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise DataError(f"Yahoo 응답 스키마 오류: {symbol}") from exc


def _stooq_bars(body: bytes, symbol: str, start: date, end: date) -> list[Bar]:
    if not symbol.lower().endswith(".us") and symbol.upper() not in {"^SPX", "^NDQ", "^DJI"}:
        raise DataError("Stooq 어댑터는 .US 및 미국 대표지수만 지원합니다.")
    try:
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        if not reader.fieldnames or not {"Date", "Open", "High", "Low", "Close"}.issubset(reader.fieldnames):
            raise DataError("Stooq: CSV 가격 응답이 없습니다.")
        bars = []
        for row in reader:
            day = date.fromisoformat(row["Date"])
            if start <= day <= end:
                if not row.get("Volume"):
                    raise DataError("Stooq 거래량 누락: 0으로 대체하지 않습니다.")
                values = [_number(row[key], key) for key in ("Open", "High", "Low", "Close", "Volume")]
                bars.append(Bar(_session_close(day, "USD"), symbol.upper(), *values, "USD", math.nan, values[-1] > 0))
        if not bars:
            raise DataError("Stooq: 요청 기간에 실제 봉이 없습니다.")
        return bars
    except (ValueError, UnicodeError, KeyError) as exc:
        raise DataError("Stooq 응답 파싱 오류") from exc


def _fx_rates(path: str | Path) -> list[tuple[datetime, float]]:
    rows = _rows(Path(path).read_bytes(), ("available_at", "fx_to_krw"), "FX")
    rates = [(_datetime(row["available_at"], "FX available_at"), _number(row["fx_to_krw"], "fx_to_krw")) for row in rows]
    if any(rate <= 0 for _, rate in rates) or any(a[0] >= b[0] for a, b in zip(rates, rates[1:])):
        raise DataError("FX는 양수이며 발표 시각 순서대로 유일해야 합니다.")
    return rates


def collect_daily(symbols: list[str], start: str, end: str, output: str | Path,
                  provider: str = "yahoo", fx_path: str | Path | None = None,
                  benchmark_symbol: str | None = None) -> dict[str, Any]:
    """Collect requested real bars, recording failures. Never produces fake results.

    USD imports remain prices-only until actual timestamped FX is supplied.
    FX available_at must be the release timestamp, not merely observation date.
    """
    if provider not in {"yahoo", "stooq"}:
        raise DataError("provider는 yahoo/stooq 중 하나여야 합니다.")
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or last >= datetime.now(UTC).date():
        raise DataError("기간은 시작<=종료, 종료일은 완료된 UTC 과거 날짜여야 합니다.")
    if not symbols or len(symbols) > 100 or any(not isinstance(symbol, str) for symbol in symbols):
        raise DataError("종목 1~100개가 필요합니다.")
    symbols = [symbol.upper() for symbol in symbols]
    if len(set(symbols)) != len(symbols):
        raise DataError("중복 없는 종목 1~100개가 필요합니다.")
    if benchmark_symbol is not None:
        if not isinstance(benchmark_symbol, str):
            raise DataError("benchmark_symbol은 종목코드 문자열이어야 합니다.")
        benchmark_symbol = benchmark_symbol.upper()
        if not any(symbol != benchmark_symbol for symbol in symbols):
            raise DataError("벤치마크 외에 실제 연구 대상 종목이 필요합니다.")
        if benchmark_symbol not in symbols:
            symbols.append(benchmark_symbol)
    for symbol in symbols:
        if not re.fullmatch(r"[A-Za-z0-9^._=-]{1,32}", symbol):
            raise DataError("종목코드 형식 오류")
    target = Path(output).expanduser().absolute()
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise FileExistsError("수집 결과는 새 디렉터리에 저장하세요.")
    records, bars, raw, missing = [], [], {}, {}
    retrieved = datetime.now(UTC).isoformat()
    for index, symbol in enumerate(symbols):
        raw_name = None
        if provider == "yahoo":
            params = {"period1": int(datetime.combine(first, time(), UTC).timestamp()),
                      "period2": int(datetime.combine(last + timedelta(days=1), time(), UTC).timestamp()),
                      "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true"}
            url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?" + urllib.parse.urlencode(params)
        else:
            params = {"s": symbol.lower(), "i": "d", "d1": first.strftime("%Y%m%d"), "d2": last.strftime("%Y%m%d")}
            url = "https://stooq.com/q/d/l/?" + urllib.parse.urlencode(params)
        try:
            body = _download(url)
            raw_name = f"raw/{index:03d}.{'json' if provider == 'yahoo' else 'csv'}"
            if sum(len(value) for value in raw.values()) + len(body) > MAX_DATASET_BYTES:
                raise DataError("수집 데이터 전체 크기 제한 초과")
            raw[raw_name] = body
            gaps, issues = [], []
            parsed = _yahoo_bars(body, symbol, first, last, gaps, issues) if provider == "yahoo" else _stooq_bars(body, symbol, first, last)
            if gaps:
                missing[symbol] = gaps
            bars.extend(parsed)
            records.append({"symbol": symbol, "status": "downloaded", "bars": len(parsed),
                            "url": url, "raw_file": raw_name, "sha256": hashlib.sha256(body).hexdigest(),
                            "unknown_missing_sessions": [timestamp.isoformat() for timestamp in gaps], "quarantined_observations": issues})
        except (urllib.error.URLError, OSError, DataError) as exc:
            records.append({"symbol": symbol, "status": "failed", "url": url,
                            "error": str(exc)[:500], "error_type": type(exc).__name__,
                            **({"raw_file": raw_name, "sha256": hashlib.sha256(raw[raw_name]).hexdigest()} if raw_name in raw else {})})
    limitations = ["선택한 현재 종목만 수집하여 생존자 편향을 통제하지 못합니다.",
                   "상장폐지·거래정지·종목코드 변경·기업행위·PIT 종목구성·당시 재무/전망/업종 이력은 완전하지 않습니다.",
                   "제공처의 OHLC 조정 방식이 검증되지 않아 분할·배당 영향을 완전히 처리했다고 주장하지 않습니다.",
                   "일봉은 정규장 명목 마감시각 사용. 조기 폐장·과거 장시간 변경을 반영한 체결 시각은 지원하지 않습니다."]
    if bars and any(record["status"] != "downloaded" for record in records):
        limitations.append("부분 수집입니다. 실패 종목이 제외되어 선택 편향 위험이 있으며 전체 요청 모집단을 대표하지 않습니다.")
    if missing:
        limitations.append("제공처 전체 결측·비정상 OHLCV를 원본/수집 이슈에 격리하고 미확인 세션을 missing_sessions.csv에 보존했습니다. 거래정지/휴장으로 단정하거나 가격을 수정·보간하지 않습니다.")
    fx_complete = bool(bars) and all(bar.currency == "KRW" for bar in bars)
    if fx_path is not None:
        if Path(fx_path).stat().st_size > MAX_FILE_BYTES:
            raise DataError("FX 파일 크기 제한 초과")
        rates = _fx_rates(fx_path)
        raw["raw/fx_available_at.csv"] = Path(fx_path).read_bytes()
        updated = []
        for bar in bars:
            candidates = [(available, rate) for available, rate in rates
                          if available <= bar.timestamp and bar.timestamp - available <= timedelta(days=7)]
            updated.append(replace(bar, fx_to_krw=candidates[-1][1]) if bar.currency == "USD" and candidates else bar)
        bars = updated
        fx_complete = bool(bars) and all(math.isfinite(bar.fx_to_krw) for bar in bars)
        limitations.append("환율은 available_at 이전 공개된 실제 자료만 7일 이내에서 사용하며, 체결 가능 환율을 보장하지 않습니다.")
    if not fx_complete:
        limitations.append("prices_only=true: 실제 시점 USD/KRW 환율이 없어 원화 전략 성과 계산은 차단됩니다.")
    trade_bars = [bar for bar in bars if bar.symbol != benchmark_symbol]
    benchmark_bars = [bar for bar in bars if bar.symbol == benchmark_symbol]
    report = {"provider": provider, "requested_period": {"start": start, "end": end}, "retrieved_at": retrieved,
              "status": "downloaded" if trade_bars and all(r["status"] == "downloaded" for r in records) else "partial" if trade_bars else "failed",
              "synthetic": False, "records": records, "fx_complete": fx_complete, "actual_bars": len(bars),
              "actual_tradable_bars": len(trade_bars), "benchmark_bars": len(benchmark_bars), "benchmark_symbol": benchmark_symbol}
    if trade_bars:
        dataset = Dataset(sorted(trade_bars, key=lambda bar: (bar.timestamp, bar.symbol)),
                          DatasetMeta(source=f"{provider} public daily endpoint", retrieved_at=retrieved,
                                      price_basis="vendor_ohlc_adjustments_unverified", fx_complete=fx_complete,
                                      license_note="공개 응답 접근은 이용권한 부여가 아닙니다. 제공처 약관을 확인한 개인 연구에 한정하고 재배포하지 마세요.",
                                      limitations=tuple(limitations)))
        dataset.missing_sessions = missing
        dataset.benchmark_bars = sorted(benchmark_bars, key=lambda bar: (bar.timestamp, bar.symbol))
        save_dataset(dataset, target)
    else:
        target.mkdir(parents=True, exist_ok=True)
    for name, body in raw.items():
        file = target / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(body)
    (target / "collection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if trade_bars:
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"].update({name: hashlib.sha256(body).hexdigest() for name, body in raw.items()})
        manifest["files"]["collection.json"] = hashlib.sha256((target / "collection.json").read_bytes()).hexdigest()
        # Raw Stooq CSV is a provenance artifact, not a canonical data table.
        canonical = {name: (target / name).read_bytes() for name in manifest["files"] if name.endswith(".csv")}
        manifest["meta"]["sha256"] = _content_hash(canonical)
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def download_fred_fx(start: str, end: str, output: str | Path) -> dict[str, Any]:
    """Download official observed FX without inventing release timestamps.

    FRED observations alone cannot be joined as point-in-time executable FX.
    Supply audited available_at FX CSV to collect_daily for KRW performance.
    """
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise DataError("FX 날짜 범위 오류")
    target = Path(output)
    if target.exists():
        raise FileExistsError("기존 FX 파일을 덮어쓰지 않습니다.")
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?" + urllib.parse.urlencode({"id": "DEXKOUS", "cosd": start, "coed": end})
    body = _download(url)
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    if not reader.fieldnames or "DEXKOUS" not in reader.fieldnames:
        raise DataError("FRED DEXKOUS CSV가 아닙니다.")
    rows = list(reader)
    if not rows:
        raise DataError("FRED FX 자료가 없습니다.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return {"source": url, "sha256": hashlib.sha256(body).hexdigest(), "observations": len(rows),
            "usable_for_backtest": False, "limitation": "관측일만 제공되므로 공개 가능 시각 검증 후 사용해야 합니다."}


def make_synthetic_dataset(days: int = 1600, seed: int = 731) -> Dataset:
    """Deterministic fictional market for engine/UI checks, never evidence of profit."""
    if not 10 <= days <= 20_000:
        raise DataError("합성 기간은 10~20,000거래일이어야 합니다.")
    rng = random.Random(seed)
    bars, benchmark = [], []
    day = date(2018, 1, 2)
    prices = {"DEMO-A": 10_000.0, "DEMO-B": 7_000.0, "DEMO-MKT": 2_000.0}
    for index in range(days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        timestamp = _session_close(day, "KRW")
        for n, symbol in enumerate(prices):
            previous = prices[symbol]
            drift = 0.0002 + 0.003 * math.sin(index / (42 + n * 11))
            opening = max(10.0, previous * (1 + rng.gauss(0, 0.004)))
            closing = max(10.0, opening * (1 + drift + rng.gauss(0, 0.012)))
            high = max(opening, closing) * (1 + rng.uniform(0.001, 0.015))
            low = min(opening, closing) * (1 - rng.uniform(0.001, 0.015))
            bar = Bar(timestamp, symbol, opening, high, low, closing, float(rng.randrange(100_000, 2_000_000)))
            (benchmark if symbol == "DEMO-MKT" else bars).append(bar)
            prices[symbol] = closing
        day += timedelta(days=1)
    return Dataset(sorted(bars, key=lambda bar: (bar.timestamp, bar.symbol)),
                   DatasetMeta(source="SYNTHETIC / 실제 시장 데이터 아님", retrieved_at=datetime.now(UTC).isoformat(),
                               synthetic=True, price_basis="fictional", license_note="테스트용 자체 생성 가상 데이터; 투자 판단 금지.",
                               limitations=("모든 가격·거래량·시장 날짜는 가상 테스트 데이터입니다. 실제 거래소 휴장일을 반영하지 않습니다.",
                                            "합성 데이터는 수익·손실·합격 근거로 사용하지 않으며 항상 합격을 차단합니다.")),
                   benchmark_bars=benchmark)
