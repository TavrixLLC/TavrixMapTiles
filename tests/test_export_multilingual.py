import os
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("APP_ROOT", str(ROOT))
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("OUTPUT_DIR", str(ROOT / "output"))
os.environ.setdefault("TMP_DIR", str(ROOT / "tmp"))
os.environ.setdefault("LOG_DIR", str(ROOT / "logs"))
sys.modules.setdefault("psycopg2", types.SimpleNamespace(connect=lambda **_kwargs: None))
sys.path.insert(0, str(ROOT / "scripts"))

import export_layers  # noqa: E402


class FakeSchemaInspector:
    def columns_for_table(self, _table_name):
        return {
            "osm_id": {"data_type": "bigint", "udt_name": "int8"},
            "name": {"data_type": "text", "udt_name": "text"},
            "tags": {"data_type": "USER-DEFINED", "udt_name": "hstore"},
        }

    def describe_table(self, table_name):
        return {"table": table_name, "has_tags": True, "tag_storage": "hstore"}


class ExportMultilingualRegressionTests(unittest.TestCase):
    def test_ogr2ogr_command_includes_source_and_target_srs(self):
        cmd = export_layers.ogr2ogr_command(pathlib.Path("out.fgb"), "SELECT 1", "places")
        self.assertIn("-s_srs", cmd)
        self.assertIn("-t_srs", cmd)
        self.assertEqual(cmd[cmd.index("-s_srs") + 1], "EPSG:4326")
        self.assertEqual(cmd[cmd.index("-t_srs") + 1], "EPSG:4326")
        self.assertLess(cmd.index("-s_srs"), cmd.index("-t_srs"))

    def test_multilingual_sql_keeps_hstore_fields_and_geometry_cast(self):
        layer = {
            "name": "places",
            "multilingual": {
                "enabled": True,
                "source_table": "public.planet_osm_point",
            },
        }
        original_sql = (
            "SELECT osm_id::text AS id, name, "
            "ST_Transform(way, 4326)::geometry(Point, 4326) AS geom "
            "FROM public.planet_osm_point"
        )
        sql = export_layers.enrich_multilingual_sql(original_sql, layer, FakeSchemaInspector())

        for field in ("name_en", "name_ar", "name_ku", "name_int"):
            self.assertIn(field, sql)
        for tag in ("name:en", "name:ar", "name:ku", "name:ckb", "int_name"):
            self.assertIn(f'"tags" -> \'{tag}\'', sql)
        self.assertIn("ST_Transform(way, 4326)::geometry(Point, 4326) AS geom", sql)

    def test_road_multilingual_sql_limits_languages(self):
        layer = {
            "name": "roads",
            "multilingual": {
                "enabled": True,
                "source_table": "public.planet_osm_line",
                "scope": "road",
                "languages": ["ar", "en", "ku"],
            },
        }
        sql = export_layers.enrich_multilingual_sql(
            "SELECT osm_id::text AS id, name, ref, ST_Transform(way, 4326)::geometry(MultiLineString, 4326) AS geom FROM public.planet_osm_line",
            layer,
            FakeSchemaInspector(),
        )
        self.assertIn("name_en", sql)
        self.assertIn("name_ar", sql)
        self.assertIn("name_ku", sql)
        self.assertNotIn("name_fr", sql)


if __name__ == "__main__":
    unittest.main()
