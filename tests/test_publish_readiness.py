import contextlib
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("APP_ROOT", str(ROOT))
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))
os.environ.setdefault("TMP_DIR", str(ROOT / "tmp"))
os.environ.setdefault("LOG_DIR", str(ROOT / "logs"))
sys.path.insert(0, str(ROOT / "scripts"))

import prune  # noqa: E402
import publish_manifest  # noqa: E402
import refresh_manifest  # noqa: E402
import rollback_manifest  # noqa: E402
import upload  # noqa: E402
import validate_published  # noqa: E402
from common import (  # noqa: E402
    MANIFEST_CACHE_CONTROL,
    MANIFEST_CONTENT_TYPE,
    PMTILES_CACHE_CONTROL,
    PMTILES_CONTENT_TYPE,
    assert_verify_url_runtime_safe,
)


@contextlib.contextmanager
def patched_env(**values):
    original = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class FakeS3Client:
    def __init__(self):
        self.uploads = []

    def upload_file(self, local_path, bucket, key, ExtraArgs=None):
        self.uploads.append({
            "local_path": local_path,
            "bucket": bucket,
            "key": key,
            "ExtraArgs": ExtraArgs or {},
        })


class FakeResponse:
    def __init__(self, status_code=200, headers=None, content=b"body", payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"No fake response left for {url}")
        return self.responses.pop(0)


class PublishReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = pathlib.Path(self.tmp.name) / "output"
        self.old_outputs = {
            "publish": publish_manifest.OUTPUT_DIR,
            "refresh": refresh_manifest.OUTPUT_DIR,
            "prune": prune.OUTPUT_DIR,
        }
        publish_manifest.OUTPUT_DIR = self.output_dir
        refresh_manifest.OUTPUT_DIR = self.output_dir
        prune.OUTPUT_DIR = self.output_dir

    def tearDown(self):
        publish_manifest.OUTPUT_DIR = self.old_outputs["publish"]
        refresh_manifest.OUTPUT_DIR = self.old_outputs["refresh"]
        prune.OUTPUT_DIR = self.old_outputs["prune"]
        self.tmp.cleanup()

    def write_artifact(self, target="basemap", region="iraq", stamp="20260602-2254", *, ok=True):
        if target == "global":
            filename = f"global-z0-z5-{stamp}.pmtiles"
            artifact_region = "global"
            minzoom, maxzoom = 0, 5
            directory = self.output_dir / "tiles" / "global"
        elif target == "basemap":
            filename = f"basemap-{region}-z6-z14-{stamp}.pmtiles"
            artifact_region = region
            minzoom, maxzoom = 6, 14
            directory = self.output_dir / "tiles" / region
        else:
            filename = f"pois-{region}-z10-z16-{stamp}.pmtiles"
            artifact_region = region
            minzoom, maxzoom = 10, 16
            directory = self.output_dir / "tiles" / region
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_bytes(b"pmtiles")
        validation = {
            "schema_version": 1,
            "target": target,
            "region": artifact_region,
            "path": str(path),
            "ok": ok,
            "failures": [] if ok else ["not ok"],
            "header": {"minzoom": minzoom, "maxzoom": maxzoom},
        }
        path.with_suffix(".validation.json").write_text(json.dumps(validation), encoding="utf-8")
        return {
            "target": target,
            "region": artifact_region,
            "filename": filename,
            "path": str(path),
            "minzoom": minzoom,
            "maxzoom": maxzoom,
            "sha256": "abc",
            "size_bytes": path.stat().st_size,
        }

    def test_pmtiles_and_manifest_upload_cache_headers(self):
        fake = FakeS3Client()
        original_client = upload._s3_client
        upload._s3_client = lambda: fake
        try:
            with patched_env(
                S3_BUCKET="tiles",
                S3_REGION="auto",
                S3_ACCESS_KEY_ID="key",
                S3_SECRET_ACCESS_KEY="secret",
                S3_PUBLIC_BASE_URL="https://cdn.example.com",
            ):
                path = pathlib.Path(self.tmp.name) / "file.pmtiles"
                path.write_bytes(b"pmtiles")
                upload.upload_file(path, "tiles/global/file.pmtiles")
                upload.upload_file(path, "manifests/iraq.json", cache_control=MANIFEST_CACHE_CONTROL, content_type=MANIFEST_CONTENT_TYPE)
        finally:
            upload._s3_client = original_client

        self.assertEqual(fake.uploads[0]["ExtraArgs"]["CacheControl"], PMTILES_CACHE_CONTROL)
        self.assertEqual(fake.uploads[0]["ExtraArgs"]["ContentType"], PMTILES_CONTENT_TYPE)
        self.assertEqual(fake.uploads[1]["ExtraArgs"]["CacheControl"], MANIFEST_CACHE_CONTROL)
        self.assertEqual(fake.uploads[1]["ExtraArgs"]["ContentType"], MANIFEST_CONTENT_TYPE)

    def test_missing_s3_config_handling(self):
        with patched_env(
            S3_BUCKET="tiles",
            S3_REGION=None,
            AWS_DEFAULT_REGION=None,
            S3_ACCESS_KEY_ID=None,
            AWS_ACCESS_KEY_ID=None,
            S3_SECRET_ACCESS_KEY=None,
            AWS_SECRET_ACCESS_KEY=None,
            S3_PUBLIC_BASE_URL=None,
            CDN_BASE_URL=None,
            STATIC_BASE_URL=None,
        ):
            status = upload.object_storage_config_status()
            self.assertFalse(status["ok"])
            self.assertIn("S3_REGION or AWS_DEFAULT_REGION", status["missing"])
            self.assertIn("S3_ACCESS_KEY_ID or AWS_ACCESS_KEY_ID", status["missing"])
            self.assertIn("S3_SECRET_ACCESS_KEY or AWS_SECRET_ACCESS_KEY", status["missing"])
            self.assertIn("S3_PUBLIC_BASE_URL or CDN_BASE_URL", status["missing"])

    def test_localhost_verify_url_misconfiguration_inside_container_fails_early(self):
        with patched_env(
            TAVRIX_IN_DOCKER="true",
            S3_PUBLIC_BASE_URL=None,
            CDN_BASE_URL="http://localhost:8088",
            STATIC_BASE_URL=None,
        ):
            with self.assertRaisesRegex(RuntimeError, "Use http://static:80 inside docker compose"):
                assert_verify_url_runtime_safe(True)

        with patched_env(
            TAVRIX_IN_DOCKER="true",
            S3_PUBLIC_BASE_URL=None,
            CDN_BASE_URL="http://static:80",
            STATIC_BASE_URL=None,
        ):
            assert_verify_url_runtime_safe(True)

    def test_publish_does_not_happen_before_validation_ok_true(self):
        artifact = self.write_artifact(ok=False)
        old_manifest = {"schema_version": 1, "region": "iraq", "tilesets": {"basemap": {"filename": "old.pmtiles"}}}
        manifest_path = self.output_dir / "manifests" / "iraq.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps(old_manifest), encoding="utf-8")
        upload_result = {"key": f"tiles/iraq/{artifact['filename']}", "url": "http://static.test/file.pmtiles", "uploaded": False}

        with patched_env(S3_BUCKET=None):
            with self.assertRaisesRegex(RuntimeError, "validation JSON is not ok=true"):
                publish_manifest.publish_manifest(artifact, upload_result, skip_upload=True)

        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), old_manifest)

    def test_publish_leaves_old_manifest_active_on_failure(self):
        artifact = self.write_artifact()
        old_manifest = {"schema_version": 1, "region": "iraq", "tilesets": {"basemap": {"filename": "old.pmtiles"}}}
        manifest_path = self.output_dir / "manifests" / "iraq.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text(json.dumps(old_manifest), encoding="utf-8")
        upload_result = {"key": f"tiles/iraq/{artifact['filename']}", "url": "http://static.test/file.pmtiles", "uploaded": False}
        original_upload_file = publish_manifest.upload_file
        publish_manifest.upload_file = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("remote failed"))
        try:
            with patched_env(S3_BUCKET=None):
                with self.assertRaisesRegex(RuntimeError, "remote failed"):
                    publish_manifest.publish_manifest(artifact, upload_result)
        finally:
            publish_manifest.upload_file = original_upload_file

        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), old_manifest)

    def test_range_validation_behavior(self):
        good = validate_published.validate_pmtiles_url(
            "https://cdn.example.com/file.pmtiles",
            session=FakeSession([
                FakeResponse(
                    206,
                    {
                        "Content-Range": "bytes 0-1023/2048",
                        "Content-Length": "1024",
                        "Cache-Control": PMTILES_CACHE_CONTROL,
                        "Content-Type": PMTILES_CONTENT_TYPE,
                    },
                    b"x" * 1024,
                )
            ]),
            strict_cache=True,
        )
        bad = validate_published.validate_pmtiles_url(
            "https://cdn.example.com/file.pmtiles",
            session=FakeSession([FakeResponse(206, {"Content-Length": "1024"}, b"x" * 1024)]),
        )

        self.assertTrue(good["ok"])
        self.assertFalse(bad["ok"])
        self.assertTrue(any("Content-Range" in failure for failure in bad["failures"]))

    def test_prune_does_not_delete_active_or_unversioned_files(self):
        active = self.write_artifact(target="basemap", stamp="20260602-2254")
        old = self.write_artifact(target="basemap", stamp="20260501-0000")
        unversioned = self.output_dir / "tiles" / "iraq" / "basemap-current.pmtiles"
        unversioned.write_bytes(b"do-not-delete")
        old_time = time.time() - (90 * 24 * 3600)
        for path in (pathlib.Path(active["path"]), pathlib.Path(old["path"]), unversioned):
            os.utime(path, (old_time, old_time))

        result = prune.prune_local(30, {f"tiles/iraq/{active['filename']}"})

        self.assertTrue(pathlib.Path(active["path"]).exists())
        self.assertFalse(pathlib.Path(old["path"]).exists())
        self.assertTrue(unversioned.exists())
        self.assertIn("tiles/iraq/basemap-current.pmtiles", result["skipped"])

    def test_rollback_dry_run_selects_valid_previous_versions(self):
        with patched_env(STATIC_BASE_URL="http://static.test"):
            self.write_artifact("global", "global", "20260501-0000")
            self.write_artifact("global", "global", "20260602-2253")
            self.write_artifact("basemap", "iraq", "20260501-0000")
            self.write_artifact("basemap", "iraq", "20260602-2254")
            self.write_artifact("pois", "iraq", "20260501-0000")
            self.write_artifact("pois", "iraq", "20260602-2302")
            refresh_manifest.refresh_region_manifest("iraq", skip_upload=True, no_prune=True)

            result = rollback_manifest.rollback_region_manifest("iraq", dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["selected_artifacts"]["global"]["filename"], "global-z0-z5-20260501-0000.pmtiles")
        self.assertEqual(result["selected_artifacts"]["basemap"]["filename"], "basemap-iraq-z6-z14-20260501-0000.pmtiles")
        self.assertEqual(result["selected_artifacts"]["pois"]["filename"], "pois-iraq-z10-z16-20260501-0000.pmtiles")


if __name__ == "__main__":
    unittest.main()
