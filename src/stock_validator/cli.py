"""Research commands only. No brokerage credentials or real orders exist here."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


CORE_SOURCES = ("engine.py", "models.py", "metrics.py", "strategies.py", "indicators.py", "data.py", "validation.py", "cli.py")
CERT_TESTS = (
    "tests/test_engine.py", "tests/test_strategies.py", "tests/test_data.py",
    "tests/test_validation.py", "tests/test_web.py", "tests/test_cli.py", "tests/test_review_engine.py",
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _hashes(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    package = Path(__file__).parent
    source_hashes = {name: hashlib.sha256((package / name).read_bytes()).hexdigest() for name in CORE_SOURCES}
    for name in CERT_TESTS:
        if not (root / name).is_file():
            raise FileNotFoundError(f"필수 테스트 파일 누락: {name}")
    test_hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted((root / "tests").rglob("*.py"))}
    return source_hashes, test_hashes


def certify(root: Path, output: Path) -> int:
    """Execute actual invariant tests; never certify a zero/skipped/stale run."""
    root, output = root.resolve(), output.resolve()
    before = _hashes(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    junit = output.with_suffix(".tests.xml")
    junit.unlink(missing_ok=True)
    command = [sys.executable, "-m", "pytest", "tests", "-q", f"--junitxml={junit}"]
    result = subprocess.run(command, cwd=root, check=False)
    tests = failures = errors = skipped = 0
    if junit.is_file():
        suites = ET.parse(junit).getroot()
        for case in suites.iter("testcase"):
            tests += 1
            failures += case.find("failure") is not None
            errors += case.find("error") is not None
            skipped += case.find("skipped") is not None
    after = _hashes(root)
    passed = result.returncode == 0 and tests > 0 and failures == errors == skipped == 0 and before == after
    document = {
        "passed": passed, "exit_code": result.returncode,
        "tests_passed": tests - failures - errors - skipped,
        "tests_total": tests, "failures": failures, "errors": errors, "skipped": skipped,
        "source_hashes": after[0], "test_hashes": after[1],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": command, "sources_unchanged_during_tests": before == after,
        "scope": "합성 수기 예제와 자동 테스트의 엔진 무결성. 실제 시장 수익성 인증이 아님.",
    }
    _write_json(output, document)
    print(f"엔진 테스트 증거 저장: {output}; passed={passed}; tests={tests}; skipped={skipped}")
    return 0 if passed else (result.returncode or 1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="주문 없는 독립 주식 전략 검증 연구 플랫폼")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="실제 공개 일봉 수집. 실패 시 합성으로 대체하지 않음")
    collect.add_argument("--symbols", nargs="+", required=True)
    collect.add_argument("--start", default="2010-01-01")
    collect.add_argument("--end", default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
    collect.add_argument("--provider", choices=["yahoo", "stooq"], default="yahoo")
    collect.add_argument("--output", type=Path, default=Path("data/real"))
    collect.add_argument("--fx-csv", type=Path)
    collect.add_argument("--benchmark", help="같은 기간에 수집할 단일 벤치마크 (매매 종목에서 제외)")
    fx = sub.add_parser("collect-fx", help="공식 FRED 관측 환율 수집 (공개 시각 검증 별도)")
    fx.add_argument("--start", default="2010-01-01")
    fx.add_argument("--end", default=(datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat())
    fx.add_argument("--output", type=Path, default=Path("data/fred-observed-fx.csv"))
    demo = sub.add_parser("demo", help="명시적으로 합성 데이터 생성. 시장 수익률이 아님")
    demo.add_argument("--output", type=Path, default=Path("data/synthetic"))
    demo.add_argument("--days", type=int, default=1600)
    inspect = sub.add_parser("inspect-data", help="데이터 무결성·출처 검사")
    inspect.add_argument("--data", type=Path, required=True)
    cert = sub.add_parser("certify", help="필수 테스트 실제 실행 후 해시 결합 인증")
    cert.add_argument("--root", type=Path, default=Path.cwd())
    cert.add_argument("--output", type=Path, default=Path("reports/engine-certificate.json"))
    validate = sub.add_parser("validate", help="사전 기준·개발·워크포워드·최종 독립 검증")
    validate.add_argument("--data", type=Path, required=True)
    validate.add_argument("--output", type=Path, default=Path("reports/latest"))
    validate.add_argument("--policy", type=Path, default=Path("config/acceptance.json"))
    validate.add_argument("--certificate", type=Path, default=Path("reports/engine-certificate.json"))
    server = sub.add_parser("serve", help="읽기 전용 한국어 모바일 대시보드")
    server.add_argument("--reports", type=Path, default=Path("reports/latest"))
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "certify":
            return certify(args.root, args.output)
        if args.command == "serve":
            from .web import serve
            serve(args.reports, args.host, args.port)
            return 0
        if args.command == "collect-fx":
            from .data import download_fred_fx
            print(json.dumps(download_fred_fx(args.start, args.end, args.output), ensure_ascii=False, indent=2))
            return 0
        from .data import collect_daily, load_dataset, make_synthetic_dataset, save_dataset
        if args.command == "collect":
            # Preserve exactly the actual success/failure information from provider.
            result = collect_daily(args.symbols, args.start, args.end, args.output,
                                   provider=args.provider, fx_path=args.fx_csv,
                                   benchmark_symbol=args.benchmark)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0 if result.get("status") in {"downloaded", "partial"} else 1
        if args.command == "demo":
            path = save_dataset(make_synthetic_dataset(days=args.days), args.output)
            print(f"합성 테스트 데이터 저장: {path}. 실제 시장 데이터·수익률이 아닙니다.")
            return 0
        dataset = load_dataset(args.data)
        if args.command == "inspect-data":
            summary = {"meta": asdict(dataset.meta), "bar_count": len(dataset.bars),
                       "symbols": sorted({b.symbol for b in dataset.bars}),
                       "start": dataset.bars[0].timestamp.isoformat(),
                       "end": dataset.bars[-1].timestamp.isoformat()}
            print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
            return 0
        if args.command == "validate":
            from .validation import validate_dataset
            report = validate_dataset(dataset, args.output, args.policy, args.certificate)
            print(json.dumps({"decision": report.get("decision"), "report": str(args.output / "report.json"),
                              "engine_verified": report.get("engine_verified"),
                              "strategies": [{"key": r.get("key"), "status": r.get("status")}
                                             for r in report.get("strategies", [])]}, ensure_ascii=False, indent=2))
            # A completed honest DATA_INSUFFICIENT or FAIL report is a successful research command.
            return 0
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"작업 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
