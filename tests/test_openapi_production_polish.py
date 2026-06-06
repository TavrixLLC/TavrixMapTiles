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


DIRECT_TILE_PATHS = (
    "/api/vector/{z}/{x}/{y}.pbf",
    "/api/vector/{region}/{z}/{x}/{y}.pbf",
    "/api/vector/{tileset}/{z}/{x}/{y}.pbf",
    "/api/vector/{region}/{tileset}/{z}/{x}/{y}.pbf",
    "/api/raster/{z}/{x}/{y}.{format}",
    "/api/raster/{region}/{z}/{x}/{y}.{format}",
)


def query_param(operation: dict, name: str) -> dict:
    for param in operation.get("parameters", []):
        if param.get("in") == "query" and param.get("name") == name:
            return param
    raise AssertionError(f"Missing query parameter {name!r}")


class OpenApiProductionPolishTests(unittest.TestCase):
    def setUp(self):
        self.spec = server.openapi_spec()

    def test_direct_tile_endpoints_document_403_policy_response(self):
        for path in DIRECT_TILE_PATHS:
            operation = self.spec["paths"][path]["get"]
            responses = operation["responses"]
            self.assertIn("403", responses, path)
            description = responses["403"]["description"]
            self.assertIn("direct_tile_endpoints_disabled", description)
            self.assertIn("PUBLIC_DIRECT_TILE_ENDPOINTS=false", description)
            self.assertIn("PMTiles CDN URLs", description)
            example = responses["403"]["content"]["application/json"]["examples"]["disabled"]["value"]
            self.assertEqual(example["error"]["code"], "direct_tile_endpoints_disabled")

    def test_direct_tile_descriptions_point_to_pmtiles_cdn_public_serving(self):
        for path in DIRECT_TILE_PATHS:
            description = self.spec["paths"][path]["get"]["description"]
            self.assertIn("diagnostic/development", description)
            self.assertIn("explicitly-enabled production", description)
            self.assertIn("PMTiles via CDN/object storage", description)

    def test_internal_debug_cache_dependency_endpoints_have_security(self):
        secured_operations = (
            ("/api/health/dependencies", "get"),
            ("/api/cache/clear", "get"),
            ("/api/cache/clear", "post"),
            ("/api/cache/warm", "post"),
            ("/api/tiles/inspect/{region}/{z}/{x}/{y}", "get"),
        )
        for path, method in secured_operations:
            self.assertIn("security", self.spec["paths"][path][method], f"{method.upper()} {path}")
            self.assertTrue(self.spec["paths"][path][method]["security"], f"{method.upper()} {path}")

    def test_style_and_raster_endpoints_expose_lang_enum(self):
        paths = (
            "/api/style.json",
            "/api/style/{region}.json",
            "/api/raster/tilejson.json",
            "/api/raster/{z}/{x}/{y}.{format}",
            "/api/raster/{region}/{z}/{x}/{y}.{format}",
        )
        expected = set(server.SUPPORTED_LANGUAGES)
        for path in paths:
            enum = set(query_param(self.spec["paths"][path]["get"], "lang")["schema"]["enum"])
            self.assertEqual(enum, expected, path)
            self.assertIn("en", enum)
            self.assertIn("ar", enum)
            self.assertIn("ku", enum)

    def test_metrics_document_production_protection_note(self):
        for path in ("/api/metrics", "/api/health/metrics"):
            description = self.spec["paths"][path]["get"]["description"]
            self.assertIn("secret-free", description)
            self.assertIn("reverse proxy", description)
            self.assertIn("IP allowlist", description)

    def test_api_manifest_endpoints_document_short_active_manifest_cache(self):
        for path in ("/api/manifest.json", "/api/manifest/{region}.json"):
            operation = self.spec["paths"][path]["get"]
            self.assertIn("max-age=60", operation["description"])
            self.assertIn("must-revalidate", operation["description"])
            self.assertIn("Static CDN manifests", operation["description"])
            self.assertIn("max-age=60", operation["responses"]["200"]["description"])


if __name__ == "__main__":
    unittest.main()
