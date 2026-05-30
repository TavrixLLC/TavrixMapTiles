import json
import os
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE_URL = os.getenv("MAP_API_BASE_URL", "http://localhost:8090").rstrip("/")
INTERNAL_TOKEN = os.getenv("MAP_INTERNAL_TOKEN", "dev-internal-token")


def request(path, method="GET", payload=None, headers=None):
    data = None
    headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def json_request(path, method="GET", payload=None, headers=None):
    status, headers, body = request(path, method, payload, headers=headers)
    return status, headers, json.loads(body.decode("utf-8"))


def auth_headers(request_id=None):
    headers = {"X-Internal-Token": INTERNAL_TOKEN}
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


class MapApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            status, _headers, _body = request("/api/health/live")
        except URLError as exc:
            raise unittest.SkipTest(f"map-api is not reachable at {BASE_URL}: {exc}") from exc
        if status != 200:
            raise unittest.SkipTest(f"map-api health returned {status}")

    def assert_error(self, payload, code=None):
        self.assertIn("error", payload)
        self.assertIsInstance(payload["error"], dict)
        self.assertIn("code", payload["error"])
        self.assertIn("status", payload["error"])
        self.assertIn("message", payload["error"])
        self.assertIn("request_id", payload["error"])
        self.assertIn("details", payload["error"])
        if code:
            self.assertEqual(payload["error"]["code"], code)

    def test_request_id_propagation_and_error_body(self):
        request_id = "test-request-id-123"
        status, headers, payload = json_request("/api/vector/not-a-region/12/2553/1645.pbf", headers={"X-Request-ID": request_id})
        self.assertEqual(status, 404)
        self.assertEqual(headers["X-Request-ID"], request_id)
        self.assert_error(payload, "not_found")
        self.assertEqual(payload["error"]["request_id"], request_id)
        status, headers, payload = json_request("/api/vector/not-a-region/12/2553/1645.pbf")
        self.assertEqual(status, 404)
        self.assertTrue(headers["X-Request-ID"])
        self.assertEqual(payload["error"]["request_id"], headers["X-Request-ID"])

    def test_vector_tile_response(self):
        status, headers, body = request("/api/vector/iraq/12/2553/1645.pbf")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/vnd.mapbox-vector-tile")
        self.assertIn("ETag", headers)
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertGreater(len(body), 0)

    def test_vector_tilejson(self):
        status, headers, payload = json_request("/api/vector/iraq/tilejson.json")
        self.assertEqual(status, 200)
        self.assertIn("ETag", headers)
        self.assertIn("max-age=3600", headers["Cache-Control"])
        self.assertEqual(payload["tilejson"], "3.0.0")
        self.assertEqual(payload["scheme"], "xyz")
        self.assertTrue(payload["tiles"])
        self.assertIn("vector_layers", payload)
        for key in ("minzoom", "maxzoom", "bounds", "center", "attribution", "description"):
            self.assertIn(key, payload)

    def test_glyph_endpoint_uses_local_assets(self):
        fontstack = quote("Noto Sans Regular")
        status, headers, body = request(f"/api/fonts/{fontstack}/0-255.pbf")
        if status == 200:
            self.assertEqual(headers.get_content_type(), "application/x-protobuf")
            self.assertIn("ETag", headers)
            self.assertIn("immutable", headers["Cache-Control"])
            self.assertGreater(len(body), 0)
        else:
            self.assertEqual(status, 404)
            self.assert_error(json.loads(body.decode("utf-8")), "not_found")

    def test_sprite_serving(self):
        status, headers, payload = json_request("/api/sprites/light/sprite.json")
        self.assertEqual(status, 200)
        self.assertIn("ETag", headers)
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertIsInstance(payload, dict)
        status, headers, body = request("/api/sprites/light/sprite.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/png")
        self.assertIn("ETag", headers)
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertGreater(len(body), 0)

    def test_missing_sprite_and_glyph_return_404(self):
        status, _headers, payload = json_request("/api/sprites/not-a-style/sprite.json")
        self.assertEqual(status, 404)
        self.assert_error(payload, "not_found")
        status, _headers, payload = json_request("/api/glyphs/NoSuchFont/0-255.pbf")
        self.assertEqual(status, 404)
        self.assert_error(payload, "not_found")

    def test_style_metadata(self):
        status, headers, payload = json_request("/api/styles/light")
        self.assertEqual(status, 200)
        self.assertIn("max-age=300", headers["Cache-Control"])
        self.assertEqual(payload["id"], "light")
        self.assertIn("supports_3d", payload)
        self.assertGreater(payload["layer_count"], 0)
        for key in (
            "minzoom",
            "maxzoom",
            "default_pitch",
            "default_bearing",
            "sources",
            "tilesets",
            "glyphs_required",
            "sprite_required",
        ):
            self.assertIn(key, payload)
        self.assertIsInstance(payload["sources"], list)
        self.assertIsInstance(payload["tilesets"], list)

    def test_style_json_cache_headers(self):
        status, headers, payload = json_request("/api/style/iraq.json?style=light")
        self.assertEqual(status, 200)
        self.assertIn("ETag", headers)
        self.assertIn("max-age=300", headers["Cache-Control"])
        self.assertIn("sources", payload)

    def test_style_validation(self):
        style = {
            "version": 8,
            "sources": {"basemap": {"type": "vector", "tiles": []}},
            "layers": [{"id": "background", "type": "background"}],
        }
        status, _headers, payload = json_request("/api/styles/validate", "POST", style)
        self.assertEqual(status, 200)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], [])

    def test_style_validation_errors_and_warnings(self):
        style = {
            "version": 7,
            "sources": {"basemap": {"type": "vector", "tiles": []}},
            "layers": [{"id": "roads", "type": "line", "source": "basemap"}],
        }
        status, _headers, payload = json_request("/api/styles/validate", "POST", style)
        self.assertEqual(status, 200)
        self.assertFalse(payload["valid"])
        for issue in payload["errors"] + payload["warnings"]:
            self.assertIsInstance(issue, dict)
            self.assertIn("code", issue)
            self.assertIn("message", issue)
            self.assertIn("path", issue)
        self.assertTrue(any(error["code"] == "invalid_version" for error in payload["errors"]))
        self.assertTrue(any(error["code"] == "missing_source_layer" for error in payload["errors"]))

    def test_coverage(self):
        status, _headers, payload = json_request("/api/coverage/iraq")
        self.assertEqual(status, 200)
        self.assertEqual(payload["region"], "iraq")
        self.assertTrue(payload["tilesets"])
        for key in ("bounds", "center", "minzoom", "maxzoom", "last_updated"):
            self.assertIn(key, payload)
        status, _headers, all_payload = json_request("/api/coverage")
        self.assertEqual(status, 200)
        self.assertIn("regions", all_payload)
        self.assertIsInstance(all_payload["regions"], list)
        status, headers, payload = json_request("/api/manifest/iraq.json")
        self.assertEqual(status, 200)
        self.assertIn("ETag", headers)
        self.assertIn("max-age=3600", headers["Cache-Control"])
        self.assertIn("tilesets", payload)

    def test_raster_tile_and_tilejson_cache_headers(self):
        status, headers, payload = json_request("/api/raster/iraq/tilejson.json?style=light&format=webp")
        self.assertEqual(status, 200)
        self.assertIn("ETag", headers)
        self.assertIn("max-age=3600", headers["Cache-Control"])
        self.assertEqual(payload["tilejson"], "3.0.0")
        status, headers, body = request("/api/raster/iraq/12/2553/1645.webp?style=light")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/webp")
        self.assertIn("ETag", headers)
        self.assertIn("immutable", headers["Cache-Control"])
        self.assertGreater(len(body), 0)

    def test_cache_status_clear_warm(self):
        status, _headers, payload = json_request("/api/cache/status")
        self.assertEqual(status, 200)
        self.assertIn("cache", payload)
        for key in ("enabled", "ttl_seconds", "items", "size_bytes", "hits", "misses", "hit_rate"):
            self.assertIn(key, payload["cache"])
        status, _headers, payload = json_request("/api/cache/clear", "POST", {}, headers=auth_headers())
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        status, _headers, payload = json_request(
            "/api/cache/warm",
            "POST",
            {"regions": ["iraq"], "styles": ["light"]},
            headers=auth_headers(),
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_internal_endpoints_require_token(self):
        status, _headers, payload = json_request("/api/cache/clear", "POST", {})
        self.assertEqual(status, 401)
        self.assert_error(payload, "unauthorized")
        status, _headers, payload = json_request("/api/cache/clear")
        self.assertEqual(status, 401)
        self.assert_error(payload, "unauthorized")
        status, _headers, payload = json_request("/api/tiles/inspect/iraq/12/2553/1645")
        self.assertEqual(status, 401)
        self.assert_error(payload, "unauthorized")
        status, _headers, payload = json_request("/api/cache/warm", "POST", {})
        self.assertEqual(status, 401)
        self.assert_error(payload, "unauthorized")
        status, _headers, payload = json_request("/api/health/dependencies")
        self.assertEqual(status, 401)
        self.assert_error(payload, "unauthorized")

    def test_tile_inspect_with_token(self):
        status, _headers, payload = json_request("/api/tiles/inspect/iraq/12/2553/1645", headers=auth_headers())
        self.assertEqual(status, 200)
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["z"], 12)
        self.assertEqual(payload["x"], 2553)
        self.assertEqual(payload["y"], 1645)
        self.assertEqual(payload["tileset"], "basemap")
        self.assertEqual(payload["content_type"], "application/vnd.mapbox-vector-tile")
        self.assertIn("content_encoding", payload)
        self.assertIn("etag", payload)
        self.assertIn(payload["cache_status"], ("hit", "miss"))

    def test_health_endpoints(self):
        for path in ("/api/health/live", "/api/health/ready"):
            status, _headers, payload = json_request(path)
            self.assertIn(status, (200, 503))
            self.assertIn("ok", payload)
        status, _headers, payload = json_request("/api/health/dependencies", headers=auth_headers())
        self.assertIn(status, (200, 503))
        self.assertIn("checks", payload)
        self.assertIsInstance(payload["checks"], dict)
        for key in (
            "manifests_dir",
            "styles_dir",
            "pmtiles_files",
            "glyphs_dir",
            "sprites_dir",
            "default_region",
            "default_style",
            "cache",
        ):
            self.assertIn(key, payload["checks"])
        self.assertIn("missing", payload)
        self.assertIn("warnings", payload)
        self.assertEqual(json_request("/api/health/live")[2]["ok"], True)

    def test_missing_region(self):
        status, _headers, payload = json_request("/api/vector/not-a-region/12/2553/1645.pbf")
        self.assertEqual(status, 404)
        self.assert_error(payload, "not_found")

    def test_missing_tile(self):
        status, _headers, payload = json_request("/api/vector/iraq/basemap/6/0/0.pbf")
        self.assertEqual(status, 404)
        self.assert_error(payload, "not_found")

    def test_invalid_zxy(self):
        status, _headers, payload = json_request("/api/vector/iraq/19/0/0.pbf")
        self.assertEqual(status, 400)
        self.assert_error(payload, "invalid_tile_coordinate")
        status, _headers, payload = json_request("/api/vector/iraq/2/4/0.pbf")
        self.assertEqual(status, 400)
        self.assert_error(payload, "invalid_tile_coordinate")
        status, _headers, payload = json_request("/api/vector/iraq/2/0/4.pbf")
        self.assertEqual(status, 400)
        self.assert_error(payload, "invalid_tile_coordinate")

    def test_tileset_metadata_fields(self):
        status, _headers, payload = json_request("/api/tilesets/iraq/basemap")
        self.assertEqual(status, 200)
        for key in ("id", "region", "bounds", "center", "vector_layers", "format", "tile_count", "last_updated", "etag"):
            self.assertIn(key, payload)

    def test_openapi_internal_security_and_asset_errors(self):
        status, _headers, payload = json_request("/api/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("securitySchemes", payload["components"])
        self.assertIn("internalToken", payload["components"]["securitySchemes"])
        self.assertIn("400", payload["paths"]["/api/glyphs/{fontstack}/{range}.pbf"]["get"]["responses"])
        self.assertIn("404", payload["paths"]["/api/sprites/{style}/sprite.png"]["get"]["responses"])
        schemas = payload["components"]["schemas"]
        for name in (
            "ErrorResponse",
            "CacheStatus",
            "Coverage",
            "CoverageResponse",
            "VectorTileJson",
            "Tileset",
            "HealthDetailed",
            "TileInspect",
            "StyleMetadata",
            "StyleValidation",
        ):
            self.assertIn("required", schemas[name])
        self.assertEqual(
            schemas["StyleValidation"]["properties"]["errors"]["items"]["$ref"],
            "#/components/schemas/ValidationIssue",
        )
        checks = schemas["HealthDetailed"]["properties"]["checks"]
        self.assertIn("manifests_dir", checks["properties"])
        self.assertIn("Cache-Control", payload["paths"]["/api/vector/{region}/{z}/{x}/{y}.pbf"]["get"]["responses"]["200"]["headers"])
        self.assertIn("ETag", payload["paths"]["/api/raster/{region}/{z}/{x}/{y}.{format}"]["get"]["responses"]["200"]["headers"])


if __name__ == "__main__":
    unittest.main()
