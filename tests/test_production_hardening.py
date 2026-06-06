import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
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


SAFE_TOKEN = "a" * 32


@contextlib.contextmanager
def patched_server(**values):
    original = {name: getattr(server, name) for name in values}
    try:
        for name, value in values.items():
            setattr(server, name, value)
        server._cache.clear()
        yield
    finally:
        for name, value in original.items():
            setattr(server, name, value)
        server._cache.clear()


def write_tileset(root: pathlib.Path, region: str = "iraq", *, exists: bool = True, validation_ok: bool = True) -> dict:
    filename = f"basemap-{region}-z6-z14-20260602-2254.pmtiles"
    key = f"tiles/{region}/{filename}"
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    if exists:
        path.write_bytes(b"pmtiles")
    validation = {
        "schema_version": 1,
        "target": "basemap",
        "region": region,
        "path": str(path),
        "ok": validation_ok,
        "failures": [] if validation_ok else ["synthetic failure"],
    }
    path.with_suffix(".validation.json").write_text(json.dumps(validation), encoding="utf-8")
    return {
        "url": f"http://localhost:8088/{key}",
        "key": key,
        "filename": filename,
        "minzoom": 6,
        "maxzoom": 14,
    }


def write_manifest(root: pathlib.Path, tileset: dict, region: str = "iraq") -> pathlib.Path:
    path = root / "manifests" / f"{region}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "region": region, "tilesets": {"basemap": tileset}}),
        encoding="utf-8",
    )
    return path


class ProductionHardeningTests(unittest.TestCase):
    def test_production_rejects_dev_token(self):
        with patched_server(ENVIRONMENT="production", MAP_INTERNAL_TOKEN="dev-internal-token"):
            status = server.internal_token_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "unsafe_internal_token")

    def test_production_rejects_missing_token(self):
        with patched_server(ENVIRONMENT="production", MAP_INTERNAL_TOKEN=""):
            status = server.internal_token_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "unsafe_internal_token")

    def test_production_rejects_short_token(self):
        with patched_server(ENVIRONMENT="production", MAP_INTERNAL_TOKEN="short-token"):
            status = server.internal_token_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "unsafe_internal_token")

    def test_production_rejects_wildcard_cors(self):
        with patched_server(ENVIRONMENT="production", API_CORS_ORIGIN="*"):
            status = server.cors_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "unsafe_cors_origin")

    def test_production_accepts_explicit_cors(self):
        with patched_server(ENVIRONMENT="production", API_CORS_ORIGIN="https://maps.example.com,https://app.example.com"):
            status = server.cors_status()
            origin = server.cors_response_origin("https://app.example.com")
        self.assertTrue(status["ok"])
        self.assertEqual(origin, "https://app.example.com")

    def test_production_fails_missing_default_region_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patched_server(ENVIRONMENT="production", OUTPUT_DIR=pathlib.Path(tmp), DEFAULT_REGION="iraq"):
                status = server.manifest_reference_status("iraq")
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "missing_default_region_manifest")

    def test_production_fails_manifest_referencing_missing_pmtiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            tileset = write_tileset(root, exists=False)
            write_manifest(root, tileset)
            with patched_server(ENVIRONMENT="production", OUTPUT_DIR=root, DEFAULT_REGION="iraq"):
                status = server.manifest_reference_status("iraq")
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "invalid_manifest_reference")
        self.assertTrue(any("PMTiles file is missing" in item["message"] for item in status["failures"]))

    def test_production_fails_manifest_referencing_invalid_validation_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            tileset = write_tileset(root, validation_ok=False)
            write_manifest(root, tileset)
            with patched_server(ENVIRONMENT="production", OUTPUT_DIR=root, DEFAULT_REGION="iraq"):
                status = server.manifest_reference_status("iraq")
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "invalid_manifest_reference")
        self.assertTrue(any("validation JSON is not ok=true" in item["message"] for item in status["failures"]))

    def test_direct_tile_endpoints_blocked_when_public_policy_false(self):
        fake = object.__new__(server.Handler)
        fake.headers = {}
        with patched_server(
            ENVIRONMENT="production",
            PUBLIC_DIRECT_TILE_ENDPOINTS=False,
            ALLOW_PUBLIC_DIRECT_TILES_IN_PRODUCTION=False,
            MAP_INTERNAL_TOKEN=SAFE_TOKEN,
        ):
            self.assertFalse(server.direct_tile_access_allowed({}))
            with self.assertRaises(server.APIError) as caught:
                fake.require_direct_tile_access()
        self.assertEqual(caught.exception.code, "direct_tile_endpoints_disabled")
        self.assertEqual(caught.exception.status.value, 403)

    def test_internal_token_can_access_blocked_direct_tile_endpoints(self):
        headers = {"X-Internal-Token": SAFE_TOKEN}
        with patched_server(
            ENVIRONMENT="production",
            PUBLIC_DIRECT_TILE_ENDPOINTS=False,
            MAP_INTERNAL_TOKEN=SAFE_TOKEN,
        ):
            self.assertTrue(server.direct_tile_access_allowed(headers))

    def test_development_remains_easy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patched_server(
                ENVIRONMENT="development",
                MAP_INTERNAL_TOKEN="dev-internal-token",
                API_CORS_ORIGIN="*",
                PUBLIC_DIRECT_TILE_ENDPOINTS=True,
                OUTPUT_DIR=pathlib.Path(tmp),
                DEFAULT_REGION="iraq",
            ):
                status = server.production_hardening_status()
        self.assertTrue(status["production_config_ready"])
        self.assertTrue(status["internal_token_safe"])
        self.assertTrue(status["cors_safe"])
        self.assertTrue(status["default_region_ready"])
        self.assertTrue(status["warnings"])

    def test_oversized_style_validation_body_returns_413(self):
        handler = object.__new__(server.Handler)
        handler.headers = {"Content-Length": "11"}
        handler.rfile = io.BytesIO(b'{"x":"big"}')
        with self.assertRaises(server.APIError) as caught:
            handler.read_json_body(max_bytes=10)
        self.assertEqual(caught.exception.code, "payload_too_large")
        self.assertEqual(caught.exception.status.value, 413)

    def test_docker_compose_has_map_api_healthcheck(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("healthcheck:", compose)
        self.assertIn("/api/health/ready", compose)
        self.assertIn("map-api:", compose)


if __name__ == "__main__":
    unittest.main()
