import json
import os
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("APP_ROOT", str(ROOT))
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("TMP_DIR", str(ROOT / "tmp"))
os.environ.setdefault("LOG_DIR", str(ROOT / "logs"))
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_manifest  # noqa: E402


class RefreshManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = pathlib.Path(self.tmp.name) / "output"
        self.original_output = refresh_manifest.OUTPUT_DIR
        refresh_manifest.OUTPUT_DIR = self.output_dir
        self.original_static = os.environ.get("STATIC_BASE_URL")
        os.environ["STATIC_BASE_URL"] = "http://static.test"

    def tearDown(self):
        refresh_manifest.OUTPUT_DIR = self.original_output
        if self.original_static is None:
            os.environ.pop("STATIC_BASE_URL", None)
        else:
            os.environ["STATIC_BASE_URL"] = self.original_static
        self.tmp.cleanup()

    def write_artifact(self, target, region, stamp, *, ok=True, exists=True):
        if target == "global":
            filename = f"global-z0-z5-{stamp}.pmtiles"
            artifact_region = "global"
            directory = self.output_dir / "tiles" / "global"
            minzoom, maxzoom = 0, 5
        elif target == "basemap":
            filename = f"basemap-{region}-z6-z14-{stamp}.pmtiles"
            artifact_region = region
            directory = self.output_dir / "tiles" / region
            minzoom, maxzoom = 6, 14
        else:
            filename = f"pois-{region}-z10-z16-{stamp}.pmtiles"
            artifact_region = region
            directory = self.output_dir / "tiles" / region
            minzoom, maxzoom = 10, 16

        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        if exists:
            path.write_bytes(f"{target}-{stamp}".encode("ascii"))
        validation = {
            "schema_version": 1,
            "target": target,
            "region": artifact_region,
            "path": str(path),
            "ok": ok,
            "failures": [] if ok else ["synthetic failure"],
            "header": {"minzoom": minzoom, "maxzoom": maxzoom},
        }
        refresh_manifest.validation_path_for(path).write_text(json.dumps(validation), encoding="utf-8")
        return path

    def write_required_regional_artifacts(self, region="iraq"):
        self.write_artifact("basemap", region, "20260602-2254")
        self.write_artifact("pois", region, "20260602-2302")

    def test_regional_manifest_does_not_reference_missing_global_artifact(self):
        self.write_artifact("global", "global", "20260602-2253", exists=False)
        self.write_required_regional_artifacts()

        with self.assertRaisesRegex(RuntimeError, "No valid global PMTiles artifact"):
            refresh_manifest.refresh_region_manifest("iraq", skip_upload=True, no_prune=True)

        self.assertFalse((self.output_dir / "manifests" / "iraq.json").exists())

    def test_manifest_refresh_picks_latest_valid_artifacts(self):
        self.write_artifact("global", "global", "20260529-2005")
        self.write_artifact("global", "global", "20260602-2253")
        self.write_artifact("basemap", "iraq", "20260529-2153")
        self.write_artifact("basemap", "iraq", "20260602-2254")
        self.write_artifact("pois", "iraq", "20260529-2028")
        self.write_artifact("pois", "iraq", "20260602-2302")

        result = refresh_manifest.refresh_region_manifest("iraq", skip_upload=True, no_prune=True)
        tilesets = result["manifest"]["tilesets"]

        self.assertEqual(tilesets["global"]["filename"], "global-z0-z5-20260602-2253.pmtiles")
        self.assertEqual(tilesets["basemap"]["filename"], "basemap-iraq-z6-z14-20260602-2254.pmtiles")
        self.assertEqual(tilesets["pois"]["filename"], "pois-iraq-z10-z16-20260602-2302.pmtiles")

    def test_latest_invalid_artifact_is_not_selected(self):
        self.write_artifact("global", "global", "20260529-2005")
        self.write_artifact("global", "global", "20260602-2253", ok=False)
        self.write_required_regional_artifacts()

        result = refresh_manifest.refresh_region_manifest("iraq", skip_upload=True, no_prune=True)

        self.assertEqual(result["manifest"]["tilesets"]["global"]["filename"], "global-z0-z5-20260529-2005.pmtiles")

    def test_active_manifest_references_versioned_immutable_files(self):
        self.write_artifact("global", "global", "20260602-2253")
        self.write_required_regional_artifacts()

        manifest = refresh_manifest.refresh_region_manifest("iraq", skip_upload=True, no_prune=True)["manifest"]

        for tileset in manifest["tilesets"].values():
            self.assertRegex(tileset["filename"], r"\d{8}-\d{4}\.pmtiles$")
            self.assertEqual(tileset["key"], f"tiles/{'global' if tileset['filename'].startswith('global-') else 'iraq'}/{tileset['filename']}")
            self.assertEqual(tileset["url"], f"http://static.test/{tileset['key']}")

    def test_old_artifacts_are_not_deleted(self):
        old_global = self.write_artifact("global", "global", "20260529-2005")
        old_basemap = self.write_artifact("basemap", "iraq", "20260529-2153")
        old_pois = self.write_artifact("pois", "iraq", "20260529-2028")
        self.write_artifact("global", "global", "20260602-2253")
        self.write_required_regional_artifacts()

        refresh_manifest.refresh_region_manifest("iraq", skip_upload=True, no_prune=True)

        for path in (old_global, old_basemap, old_pois):
            self.assertTrue(path.exists(), f"{path} should not be deleted")


if __name__ == "__main__":
    unittest.main()
