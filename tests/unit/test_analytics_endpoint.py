import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from starlette.testclient import TestClient
    from agy_cli_manager.proxy.fastapi_app import app
    from agy_cli_manager.proxy.db import TelemetryDB
    HAS_PROXY_DEPS = True
except ImportError:
    HAS_PROXY_DEPS = False
    TestClient = None
    app = None
    TelemetryDB = None


@unittest.skipUnless(HAS_PROXY_DEPS, "fastapi/starlette optional dependencies required")
class TestAnalyticsEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp_dir.name) / "test_telemetry.db"
        self.mock_db = TelemetryDB(db_path=self.db_path)
        self.patcher = patch("agy_cli_manager.proxy.fastapi_app.get_telemetry_db", return_value=self.mock_db)
        self.patcher.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.patcher.stop()
        self.mock_db.close()
        try:
            self.tmp_dir.cleanup()
        except OSError:
            pass

    def test_dashboard_contains_daily_analytics_ui(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("Daily Token Consumption", html)
        self.assertIn("barsWrapper", html)
        self.assertIn("dailyTableBody", html)
        self.assertIn("analyticsCard", html)
        self.assertIn("range-selector", html)
        self.assertIn("const maxTokens = Math.max", html)
        self.assertIn("totalHeightPct", html)
        self.assertIn('href="/favicon.svg"', html)
        self.assertIn("header-logo", html)

    def test_favicon_endpoints(self):
        resp_svg = self.client.get("/favicon.svg")
        self.assertEqual(resp_svg.status_code, 200)
        self.assertIn("image/svg+xml", resp_svg.headers["content-type"])
        self.assertIn("public, max-age=86400, immutable", resp_svg.headers.get("cache-control", ""))
        self.assertIn("<polygon", resp_svg.text)
        self.assertIn("clipPath id=\"delta-clip\"", resp_svg.text)

        resp_ico = self.client.get("/favicon.ico")
        self.assertEqual(resp_ico.status_code, 200)
        self.assertIn("image/svg+xml", resp_ico.headers["content-type"])
        self.assertIn("public, max-age=86400, immutable", resp_ico.headers.get("cache-control", ""))

    def test_analytics_tokens_endpoint(self):
        resp = self.client.get("/api/analytics/tokens?range_days=7")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["range_days"], 7)
        self.assertIn("summary", data)
        self.assertIn("timeline", data)
        self.assertIn("by_account", data)
        self.assertIn("by_model", data)
        self.assertEqual(len(data["timeline"]), 7)
        for item in data["timeline"]:
            self.assertIn("date", item)
            self.assertIn("requests", item)
            self.assertIn("prompt_tokens", item)
            self.assertIn("completion_tokens", item)
            self.assertIn("total_tokens", item)
