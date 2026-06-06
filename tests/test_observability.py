import io
import json
import logging
import os
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("APP_ROOT", str(ROOT))
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("OUTPUT_DIR", str(ROOT / "output"))
os.environ.setdefault("STYLES_DIR", str(ROOT / "config" / "styles"))
os.environ.setdefault("GLYPHS_DIR", str(ROOT / "config" / "glyphs"))
os.environ.setdefault("SPRITES_DIR", str(ROOT / "config" / "sprites"))
sys.path.insert(0, str(ROOT / "api"))

import server  # noqa: E402


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        server._metrics.reset()

    def test_metrics_increment_after_requests(self):
        server._metrics.record_request("style", 200, 12.5)
        server._metrics.record_request("manifest", 404, 4.0)
        snapshot = server._metrics.snapshot()

        self.assertEqual(snapshot["total_requests"], 2)
        self.assertEqual(snapshot["requests_by_status"]["200"], 1)
        self.assertEqual(snapshot["requests_by_status"]["404"], 1)
        self.assertEqual(snapshot["requests_by_route_group"]["style"], 1)
        self.assertEqual(snapshot["style_requests_total"], 1)
        self.assertEqual(snapshot["manifest_requests_total"], 1)
        self.assertEqual(snapshot["error_count"], 1)
        self.assertGreaterEqual(snapshot["latency_ms"]["p95"], 4.0)

    def test_direct_tile_blocked_counter_increments(self):
        server._metrics.record_request("vector_tile", 403, 1.2, direct_tile_blocked=True)
        snapshot = server._metrics.snapshot()

        self.assertEqual(snapshot["direct_tile_requests_total"], 1)
        self.assertEqual(snapshot["direct_tile_blocked_total"], 1)
        self.assertEqual(snapshot["vector_requests_total"], 1)

    def test_prometheus_metrics_format_contains_core_counters(self):
        server._metrics.record_request("raster_tile", 200, 3.3)
        text = server.prometheus_metrics(server._metrics.snapshot())

        self.assertIn("tavrix_tiles_total_requests 1", text)
        self.assertIn('tavrix_tiles_requests_by_route_group{route_group="raster_tile"} 1', text)
        self.assertIn("tavrix_tiles_cache_hits", text)

    def test_openapi_documents_metrics_endpoint(self):
        spec = server.openapi_spec()

        self.assertIn("/api/metrics", spec["paths"])
        self.assertIn("/api/health/metrics", spec["paths"])
        self.assertIn("MetricsResponse", spec["components"]["schemas"])

    def test_logs_do_not_include_internal_token_in_query(self):
        token = "replace-with-very-long-random-token-123456789"
        redacted = server.redact_path(f"/api/style/iraq.json?token={token}&lang=en&api_key={token}")

        self.assertNotIn(token, redacted)
        self.assertIn("token=[REDACTED]", redacted)
        self.assertIn("api_key=[REDACTED]", redacted)

    def test_request_log_fields_are_structured_and_redacted(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        server.log.addHandler(handler)
        try:
            server._metrics.record_request("style", 200, 2.0)
            payload = {
                "event": "request",
                "request_id": "abc",
                "method": "GET",
                "path": server.redact_path("/api/style/iraq.json?token=secret"),
                "route_group": server.route_group_for_path("/api/style/iraq.json?token=secret"),
                "status": 200,
                "duration_ms": 2.0,
                "environment": server.ENVIRONMENT,
                "direct_tile_blocked": False,
                "user_agent": "unit-test",
            }
            server.log.info(json.dumps(payload))
        finally:
            server.log.removeHandler(handler)

        output = stream.getvalue()
        self.assertIn('"route_group": "style"', output)
        self.assertIn('"status": 200', output)
        self.assertNotIn("secret", output)


if __name__ == "__main__":
    unittest.main()
