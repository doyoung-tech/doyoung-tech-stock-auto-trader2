"""Exercise the real local read-only HTTP boundary without external services."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from stock_validator.web import MAX_REPORT_BYTES, make_server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.report_dir = Path(self.temp.name)
        self.server = make_server(self.report_dir, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        conn.request(method, path, body=body)
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return result

    def write_report(self, value):
        (self.report_dir / "report.json").write_text(json.dumps(value), encoding="utf-8")

    def test_empty_report_never_fabricates_performance(self):
        status, headers, body = self.request("/api/report")
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertFalse(value["available"])
        self.assertFalse(value["engine_verified"])
        self.assertEqual(value["strategies"], [])
        self.assertIn("투자하지 않음", value["decision"])
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_real_http_report_and_nested_reports_directory(self):
        (self.report_dir / "latest").mkdir()
        value = {"engine_verified": True, "data": {"synthetic": False}, "strategies": []}
        (self.report_dir / "latest" / "report.json").write_text(json.dumps(value))
        status, headers, body = self.request("/api/report")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["available"])
        self.assertIn("no-store", headers["Cache-Control"])

    def test_untrusted_report_text_cannot_be_interpolated_into_html(self):
        attack = '</script><img src=x onerror="alert(1)"><script>alert(2)</script>'
        self.write_report({"decision": attack, "strategies": [{"id": "A", "name": attack}]})
        status, headers, raw = self.request("/api/report")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["decision"], attack)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        _, _, html = self.request("/")
        self.assertNotIn(attack.encode(), html)
        _, _, script = self.request("/assets/app.js")
        self.assertNotIn(b".innerHTML", script)
        self.assertNotIn(b"document.write", script)
        self.assertIn(b".textContent", script)

    def test_mobile_accessible_dashboard_and_local_assets(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        html = body.decode()
        self.assertIn('lang="ko"', html)
        self.assertIn('name="viewport"', html)
        self.assertIn('width=device-width', html)
        self.assertIn('aria-label="기간별 자산 수익곡선"', html)
        self.assertIn("차트 데이터 표로 보기", html)
        self.assertIn("실제 주문 기능 없음", html)
        self.assertIn("가상 데이터", html)
        csp = headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("https://", html)
        for asset in ("app.js", "style.css"):
            self.assertEqual(self.request(f"/assets/{asset}")[0], 200)
        css = self.request("/assets/style.css")[2]
        self.assertIn(b"max-width:620px", css)
        self.assertIn(b"prefers-reduced-motion", css)

    def test_only_fixed_routes_no_filesystem_access(self):
        (self.report_dir / "secret.txt").write_text("do not serve")
        for path in ("/secret.txt", "/../secret.txt", "/assets/../web.py", "/%2e%2e/secret.txt", "/api/report?path=secret.txt", "/api/report/", "/assets/web.py", "//api/report"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_writes_are_rejected_without_creating_report(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE", "OPTIONS"):
            with self.subTest(method=method):
                status, _, _ = self.request("/api/report", method, b'{"trade":true}')
                self.assertEqual(status, 405)
        self.assertEqual(list(self.report_dir.iterdir()), [])

    def test_head_has_headers_but_no_body(self):
        status, headers, body = self.request("/", "HEAD")
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(body, b"")

    def test_invalid_reports_fail_closed_without_path_disclosure(self):
        for raw in (b"not-json", b"[]", b'{"value":NaN}', b'{"value":1e999}', b"\xff", b"[" * 1100):
            with self.subTest(raw=raw[:30]):
                (self.report_dir / "report.json").write_bytes(raw)
                status, _, body = self.request("/api/report")
                self.assertEqual(status, 503)
                value = json.loads(body)
                self.assertFalse(value["available"])
                self.assertIn("투자하지 않음", value["decision"])
                self.assertNotIn(str(self.report_dir), body.decode())

    def test_oversized_report_is_bounded(self):
        with (self.report_dir / "report.json").open("wb") as stream:
            stream.write(b" " * (MAX_REPORT_BYTES + 1))
        self.assertEqual(self.request("/api/report")[0], 503)

    def test_report_and_latest_symlinks_cannot_expose_other_json(self):
        with tempfile.TemporaryDirectory() as other:
            external = Path(other)
            (external / "report.json").write_text('{"private":"test-only marker"}')
            (self.report_dir / "report.json").symlink_to(external / "report.json")
            self.assertEqual(self.request("/api/report")[0], 503)
            (self.report_dir / "report.json").unlink()
            (self.report_dir / "latest").symlink_to(external, target_is_directory=True)
            status, _, body = self.request("/api/report")
            self.assertEqual(status, 503)
            self.assertNotIn(b"test-only marker", body)


if __name__ == "__main__":
    unittest.main()
