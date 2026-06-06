import copy
import json
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

import raster_tiles  # noqa: E402
import server  # noqa: E402


class MultilingualLabelTests(unittest.TestCase):
    def test_supported_language_is_accepted(self):
        self.assertEqual(server.resolve_language({"lang": ["en"]}), "en")
        self.assertEqual(server.resolve_language({"lang": ["AR"]}), "ar")

    def test_unsupported_language_is_rejected(self):
        for lang in ("hi", "zh", "ja", "xx"):
            with self.subTest(lang=lang):
                with self.assertRaises(server.APIError) as caught:
                    server.resolve_language({"lang": [lang]})
                self.assertEqual(caught.exception.code, "unsupported_language")
                self.assertEqual(caught.exception.status.value, 400)

    def test_omitted_language_is_noop(self):
        style = {"layers": [{"type": "symbol", "layout": {"text-field": ["get", "name"]}}]}
        original = copy.deepcopy(style)
        self.assertIs(server.apply_language_to_style(style, None), style)
        self.assertEqual(style, original)

    def test_symbol_text_field_is_rewritten(self):
        style = {"layers": [{"type": "symbol", "layout": {"text-field": ["get", "name"]}}]}
        server.apply_language_to_style(style, "en")
        self.assertEqual(
            style["layers"][0]["layout"]["text-field"],
            ["coalesce", ["get", "name_en"], ["get", "name_int"], ["get", "name_local"], ["get", "name_ar"], ["get", "name"]],
        )

    def test_road_ref_priority_is_preserved(self):
        original = ["coalesce", ["get", "ref"], ["get", "name"]]
        expr = server.language_text_expression("ku", original)
        self.assertEqual(expr[0:2], ["coalesce", ["get", "ref"]])
        self.assertIn(["get", "name_ku"], expr)
        self.assertIn(["get", "name_ar"], expr)

    def test_style_template_is_not_mutated(self):
        template = server.load_style_template("light")
        before = json.dumps(template, sort_keys=True)
        style = copy.deepcopy(template)
        server.apply_language_to_style(style, "ar")
        self.assertEqual(json.dumps(template, sort_keys=True), before)

    def test_raster_label_fallback_helper(self):
        props = {"name": "local", "name_ar": "arabic", "name_en": "english"}
        self.assertEqual(raster_tiles.label_from_properties(props), "local")
        self.assertEqual(raster_tiles.label_from_properties(props, "en"), "english")
        self.assertEqual(raster_tiles.label_from_properties(props, "ku"), "arabic")
        self.assertIsNone(raster_tiles.label_from_properties({}, "en"))


if __name__ == "__main__":
    unittest.main()
