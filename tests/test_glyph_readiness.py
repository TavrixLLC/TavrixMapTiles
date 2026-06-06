import contextlib
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


STYLE_WITH_TEXT = {
    "layers": [
        {
            "id": "labels",
            "type": "symbol",
            "layout": {"text-field": ["get", "name"], "text-font": ["Noto Sans Regular"]},
        }
    ]
}


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


class GlyphReadinessTests(unittest.TestCase):
    def test_production_rejects_demo_glyph_url(self):
        with patched_server(
            ENVIRONMENT="production",
            GLYPHS_URL=server.DEMO_GLYPHS_URL,
            GLYPHS_URL_EXPLICIT=True,
        ):
            result = server.glyph_readiness([STYLE_WITH_TEXT])

        self.assertFalse(result["ok"])
        self.assertIn("Demo MapLibre glyphs are not allowed in production.", result["failures"])

    def test_production_missing_glyphs_url_fails_readiness(self):
        with patched_server(
            ENVIRONMENT="production",
            GLYPHS_URL=server.DEMO_GLYPHS_URL,
            GLYPHS_URL_EXPLICIT=False,
        ):
            result = server.glyph_readiness([STYLE_WITH_TEXT])

        self.assertFalse(result["ok"])
        self.assertIn("GLYPHS_URL must be explicitly configured in production.", result["failures"])

    def test_development_allows_demo_glyph_url_with_warning(self):
        with patched_server(
            ENVIRONMENT="local",
            GLYPHS_URL=server.DEMO_GLYPHS_URL,
            GLYPHS_URL_EXPLICIT=False,
        ):
            result = server.glyph_readiness([STYLE_WITH_TEXT])

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "demo")
        self.assertTrue(any("development" in warning for warning in result["warnings"]))

    def test_style_json_uses_configured_glyphs_url(self):
        glyphs_url = "https://glyphs.example.com/fonts/{fontstack}/{range}.pbf"
        with patched_server(GLYPHS_URL=glyphs_url, GLYPHS_URL_EXPLICIT=True):
            style = server.load_style_template("light")

        self.assertEqual(style["glyphs"], glyphs_url)

    def test_production_accepts_explicit_external_glyph_url(self):
        glyphs_url = "https://cdn.example.com/fonts/{fontstack}/{range}.pbf"
        with patched_server(
            ENVIRONMENT="production",
            GLYPHS_URL=glyphs_url,
            GLYPHS_URL_EXPLICIT=True,
        ):
            result = server.glyph_readiness([STYLE_WITH_TEXT])

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "external")
        self.assertEqual(len(result["checks"]), len(server.REQUIRED_GLYPH_RANGES))
        self.assertTrue(all(check["ok"] and check["url"].startswith("https://cdn.example.com/") for check in result["checks"]))

    def test_local_glyph_readiness_checks_expected_pbf_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            glyph_dir = pathlib.Path(tmp) / "glyphs"
            font_dir = glyph_dir / "Noto Sans Regular"
            font_dir.mkdir(parents=True)
            for spec in server.REQUIRED_GLYPH_RANGES:
                (font_dir / f"{spec['range']}.pbf").write_bytes(b"pbf")

            with patched_server(
                ENVIRONMENT="production",
                GLYPHS_URL="/api/fonts/{fontstack}/{range}.pbf",
                GLYPHS_URL_EXPLICIT=True,
                GLYPHS_DIR=glyph_dir,
            ):
                result = server.glyph_readiness([STYLE_WITH_TEXT])

            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "local")
            self.assertEqual(len(result["checks"]), len(server.REQUIRED_GLYPH_RANGES))

            (font_dir / "1536-1791.pbf").unlink()
            with patched_server(
                ENVIRONMENT="production",
                GLYPHS_URL="/api/fonts/{fontstack}/{range}.pbf",
                GLYPHS_URL_EXPLICIT=True,
                GLYPHS_DIR=glyph_dir,
            ):
                result = server.glyph_readiness([STYLE_WITH_TEXT])

            self.assertFalse(result["ok"])
            self.assertTrue(any("1536-1791" in failure for failure in result["failures"]))

    def test_readiness_includes_glyph_status(self):
        with patched_server(
            ENVIRONMENT="local",
            GLYPHS_URL=server.DEMO_GLYPHS_URL,
            GLYPHS_URL_EXPLICIT=False,
        ):
            details = server.health_details()

        self.assertIn("glyphs_ready", details["checks"])
        self.assertIn("glyphs", details)
        self.assertIn("required_ranges", details["glyphs"])


if __name__ == "__main__":
    unittest.main()
