"""Local, read-only Korean research dashboard (no broker or order endpoint)."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_REPORT_BYTES = 8 * 1024 * 1024
_ASSETS = Path(__file__).with_name("static")
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; font-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _read_report(report_dir: Path) -> tuple[dict, int]:
    """Read a fixed report location; a missing report is an honest empty state."""
    report_path = report_dir / "report.json"
    if not report_path.is_file():
        report_path = report_dir / "latest" / "report.json"
    if not report_path.is_file():
        return {
            "available": False,
            "engine_verified": False,
            "decision": "검증 결과 없음: 투자하지 않음",
            "strategies": [],
        }, HTTPStatus.OK
    try:
        if report_path.is_symlink() or report_path.parent.is_symlink():
            raise ValueError("symlink report rejected")
        report_path.resolve().relative_to(report_dir.resolve())
        with report_path.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError("report exceeds limit")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("report must be an object")
        # Reject NaN/Infinity (including 1e999) before writing any response headers.
        json.dumps(value, allow_nan=False)
        value["available"] = True
        return value, HTTPStatus.OK
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {
            "available": False,
            "engine_verified": False,
            "decision": "보고서 읽기 실패: 투자하지 않음",
            "error": "보고서 형식과 생성 작업을 확인하세요.",
            "strategies": [],
        }, HTTPStatus.SERVICE_UNAVAILABLE


def make_server(
    report_dir: Path, host: str = "127.0.0.1", port: int = 8000
) -> ThreadingHTTPServer:
    """Create a server serving only fixed assets and an existing report.

    ``report_dir`` can be the latest report folder or its parent reports folder.
    Construction does not run experiments, execute trades, or mutate files.
    """
    report_dir = Path(report_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "StrategyResearch"
        sys_version = ""

        def log_message(self, format: str, *args: object) -> None:
            # Request paths may contain user input; keep local logs quiet.
            return

        def _respond(
            self, status: int, payload: bytes, content_type: str, head: bool = False
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Language", "ko")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            if not head:
                self.wfile.write(payload)

        def _get(self, head: bool = False) -> None:
            # BaseHTTPRequestHandler normalizes a leading //; accept exact targets only.
            if self.requestline.split()[1] != self.path:
                self._respond(HTTPStatus.NOT_FOUND, b"Not found", "text/plain", head)
                return
            if self.path == "/api/report":
                report, status = _read_report(report_dir)
                payload = json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self._respond(status, payload, "application/json; charset=utf-8", head)
                return
            routes = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if self.path in routes:
                name, content_type = routes[self.path]
                try:
                    payload = (_ASSETS / name).read_bytes()
                except OSError:
                    self._respond(503, b"Dashboard asset unavailable", "text/plain", head)
                    return
                self._respond(HTTPStatus.OK, payload, content_type, head)
                return
            self._respond(HTTPStatus.NOT_FOUND, b"Not found", "text/plain", head)

        def do_GET(self) -> None:
            self._get()

        def do_HEAD(self) -> None:
            self._get(head=True)

        def _reject_write(self) -> None:
            self._respond(HTTPStatus.METHOD_NOT_ALLOWED, b"Read-only dashboard", "text/plain")

        do_POST = _reject_write
        do_PUT = _reject_write
        do_PATCH = _reject_write
        do_DELETE = _reject_write
        do_OPTIONS = _reject_write
        do_CONNECT = _reject_write
        do_TRACE = _reject_write

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve(report_dir: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Serve existing research results until interrupted."""
    server = make_server(report_dir, host, port)
    print(f"전략 연구 대시보드 실행: {host}:{server.server_port} (읽기 전용)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
