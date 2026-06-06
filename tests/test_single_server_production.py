import contextlib
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
sys.path.insert(0, str(ROOT / "scripts"))

import estimate_static_cost  # noqa: E402
import publish_static_local  # noqa: E402
import smoke_single_server_production  # noqa: E402
import server  # noqa: E402


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


@contextlib.contextmanager
def patched_script_paths(*values):
    originals = {}
    try:
        for module, attrs in values:
            originals[module] = {name: getattr(module, name) for name in attrs}
            for name, value in attrs.items():
                setattr(module, name, value)
        yield
    finally:
        for module, attrs in originals.items():
            for name, value in attrs.items():
                setattr(module, name, value)


def write_manifest_tree(root: pathlib.Path, *, url_base: str = "https://tiles.tavrix.com") -> dict:
    filename = "basemap-iraq-z6-z14-20260602-2254.pmtiles"
    key = f"tiles/iraq/{filename}"
    pmtiles = root / key
    pmtiles.parent.mkdir(parents=True, exist_ok=True)
    pmtiles.write_bytes(b"pmtiles")
    pmtiles.with_suffix(".validation.json").write_text(
        json.dumps({
            "ok": True,
            "header": {"minzoom": 6, "maxzoom": 14},
            "metadata_layers": ["roads", "buildings", "pois"],
        }),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "region": "iraq",
        "tilesets": {
            "basemap": {
                "url": f"{url_base}/{key}",
                "key": key,
                "filename": filename,
                "minzoom": 6,
                "maxzoom": 14,
            }
        },
    }
    manifest_path = root / "manifests" / "iraq.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


class SingleServerProductionTests(unittest.TestCase):
    def test_sprite_url_uses_configured_static_base(self):
        with patched_server(SPRITES_BASE_URL="https://tiles.tavrix.com/sprites"):
            self.assertEqual(server.style_sprite_url("light"), "https://tiles.tavrix.com/sprites/light/sprite")

    def test_gateway_readiness_accepts_https_static_single_server_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "output"
            static_root = pathlib.Path(tmp) / "static"
            write_manifest_tree(output)
            (static_root / "tiles" / "iraq").mkdir(parents=True)
            (static_root / "manifests").mkdir(parents=True)
            (static_root / "fonts").mkdir(parents=True)
            (static_root / "sprites").mkdir(parents=True)
            src = output / "tiles" / "iraq" / "basemap-iraq-z6-z14-20260602-2254.pmtiles"
            (static_root / "tiles" / "iraq" / src.name).write_bytes(src.read_bytes())
            with patched_server(
                ENVIRONMENT="production",
                TILES_BEHIND_GATEWAY=True,
                PUBLIC_DIRECT_TILE_ENDPOINTS=False,
                PUBLIC_API_BASE_URL="https://api.tavrix.com/maps",
                PUBLIC_STATIC_BASE_URL="https://tiles.tavrix.com",
                CDN_BASE_URL="https://tiles.tavrix.com",
                S3_PUBLIC_BASE_URL="https://tiles.tavrix.com",
                GLYPHS_URL="https://tiles.tavrix.com/fonts/{fontstack}/{range}.pbf",
                GLYPHS_URL_EXPLICIT=True,
                SPRITES_BASE_URL="https://tiles.tavrix.com/sprites",
                STATIC_PUBLISH_ROOT=static_root,
                OUTPUT_DIR=output,
                DEFAULT_REGION="iraq",
                DEFAULT_STYLE="light",
            ):
                status = server.gateway_deployment_status()

        self.assertTrue(status["ok"], status)
        self.assertTrue(status["gateway_contract_documented"])
        self.assertEqual(status["gateway_contract"]["path"], "docs/gateway-integration-contract.md")
        self.assertNotIn("billing", status)

    def test_health_readiness_does_not_claim_billing_enabled(self):
        with patched_server(ENVIRONMENT="development", TILES_BEHIND_GATEWAY=False):
            details = server.health_details()
        self.assertTrue(details["gateway_contract_documented"])
        self.assertNotIn("billing", details)

    def test_trusted_gateway_header_can_authorize_internal_access(self):
        with patched_server(
            ENVIRONMENT="production",
            MAP_INTERNAL_TOKEN="",
            TRUSTED_GATEWAY_HEADER="X-Tavrix-Gateway-Token",
            TRUSTED_GATEWAY_HEADER_VALUE="g" * 32,
        ):
            self.assertTrue(server.internal_auth_allowed({"X-Tavrix-Gateway-Token": "g" * 32}))
            self.assertFalse(server.internal_auth_allowed({"X-Tavrix-Gateway-Token": "wrong"}))

    def test_gateway_readiness_rejects_internal_style_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "output"
            write_manifest_tree(output, url_base="http://localhost:8088")
            with patched_server(
                ENVIRONMENT="production",
                TILES_BEHIND_GATEWAY=True,
                PUBLIC_DIRECT_TILE_ENDPOINTS=False,
                PUBLIC_API_BASE_URL="https://api.tavrix.com/maps",
                PUBLIC_STATIC_BASE_URL="https://tiles.tavrix.com",
                CDN_BASE_URL="https://tiles.tavrix.com",
                S3_PUBLIC_BASE_URL="https://tiles.tavrix.com",
                GLYPHS_URL="https://tiles.tavrix.com/fonts/{fontstack}/{range}.pbf",
                GLYPHS_URL_EXPLICIT=True,
                SPRITES_BASE_URL="https://tiles.tavrix.com/sprites",
                STATIC_PUBLISH_ROOT=None,
                OUTPUT_DIR=output,
                DEFAULT_REGION="iraq",
                DEFAULT_STYLE="light",
            ):
                status = server.gateway_deployment_status()

        self.assertFalse(status["ok"])
        self.assertTrue(any(failure["code"] == "unsafe_public_url" for failure in status["failures"]))

    def test_nginx_production_config_contains_required_static_policy(self):
        config = (ROOT / "nginx" / "tavrix-tiles.conf").read_text(encoding="utf-8")
        self.assertIn("root /var/www/tavrix-tiles", config)
        self.assertIn("location /tiles/", config)
        self.assertIn("application/vnd.pmtiles pmtiles", config)
        self.assertIn("Accept-Ranges", config)
        self.assertIn("max-age=31536000, immutable", config)
        self.assertIn("location /manifests/", config)
        self.assertIn("max-age=60, must-revalidate", config)
        self.assertIn("gzip off", config)

    def test_gateway_contract_doc_exists_without_tiles_owned_schema(self):
        path = ROOT / "docs" / "gateway-integration-contract.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("Gateway is the public entry point", text)
        self.assertIn("Tiles does not count, store, price, or roll up customer usage", text)
        for forbidden in ("CREATE TABLE api_keys", "CREATE TABLE usage_events", "CREATE TABLE daily_usage_rollups", "CREATE TABLE billing_rules"):
            self.assertNotIn(forbidden, text)

    def test_tiles_openapi_exposes_no_billing_endpoints(self):
        spec = server.openapi_spec()
        billing_paths = [path for path in spec["paths"] if "billing" in path.lower() or "usage" in path.lower()]
        self.assertEqual(billing_paths, [])

    def test_static_publish_refuses_pmtiles_overwrite_with_different_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            output = root / "output"
            config = root / "config"
            src = output / "tiles" / "iraq" / "basemap-iraq-z6-z14-20260602-2254.pmtiles"
            src.parent.mkdir(parents=True)
            src.write_bytes(b"new-content")
            destination_root = root / "static"
            dst = destination_root / "tiles" / "iraq" / src.name
            dst.parent.mkdir(parents=True)
            dst.write_bytes(b"old-content")
            with patched_script_paths((
                publish_static_local,
                {
                    "OUTPUT_DIR": output,
                    "CONFIG_DIR": config,
                    "COPY_SETS": (
                        ("tiles", output / "tiles", "tiles"),
                    ),
                },
            )):
                with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite immutable PMTiles"):
                    publish_static_local.publish_static(destination_root)

    def test_smoke_style_validation_rejects_internal_sources(self):
        style = {
            "sources": {
                "basemap": {"url": "pmtiles://http://localhost:8088/tiles/iraq/file.pmtiles"},
            },
            "glyphs": "https://tiles.tavrix.com/fonts/{fontstack}/{range}.pbf",
        }
        ok, detail = smoke_single_server_production.validate_style_payload(style, "https://tiles.tavrix.com")
        self.assertFalse(ok)
        self.assertIn("localhost", detail)

    def test_smoke_gateway_usage_check_is_optional(self):
        args = smoke_single_server_production.parse_args([
            "--valid-token",
            "token",
        ])
        self.assertIsNone(args.usage_url)

    def test_static_cost_estimator_parses_nginx_json_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = pathlib.Path(tmp) / "access.log"
            log_path.write_text(
                "\n".join([
                    json.dumps({"uri": "/tiles/iraq/a.pmtiles", "status": 206, "bytes_sent": 1024, "http_range": "bytes=0-1023", "request_time": 0.01}),
                    json.dumps({"uri": "/manifests/iraq.json", "status": 200, "bytes_sent": 512, "http_range": "", "request_time": 0.02}),
                ]),
                encoding="utf-8",
            )
            result = estimate_static_cost.estimate_static_cost(
                log_path,
                provider_profile="vps",
                days_sampled=1,
            )

        self.assertEqual(result["total_requests"], 2)
        self.assertEqual(result["pmtiles_requests"], 1)
        self.assertEqual(result["range_requests"], 1)
        self.assertIn("cost_note", result)
        self.assertNotIn("requests_per_map_load", result)
        self.assertNotIn("bytes_per_map_load", result)


if __name__ == "__main__":
    unittest.main()
