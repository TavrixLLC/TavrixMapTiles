from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from common import (
    MANIFEST_CACHE_CONTROL,
    MANIFEST_CONTENT_TYPE,
    OUTPUT_DIR,
    iso_now,
    storage_key_for,
    url_for_key,
    write_json_atomic,
)
from upload import _s3_client, upload_file


def _manifest_path(region: str) -> Path:
    return OUTPUT_DIR / "manifests" / f"{region}.json"


def _manifest_key(region: str) -> str:
    return f"manifests/{region}.json"


def _manifest_stage_path(region: str) -> Path:
    return OUTPUT_DIR / "manifests" / f".{region}.publish.json"


def _load_manifest_from_s3(region: str) -> dict[str, Any] | None:
    bucket = os.getenv("S3_BUCKET", "").strip()
    if not bucket:
        return None
    try:
        response = _s3_client().get_object(Bucket=bucket, Key=_manifest_key(region))
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return json.loads(response["Body"].read().decode("utf-8"))


def load_existing_manifest(region: str, *, include_s3: bool = True) -> dict[str, Any] | None:
    path = _manifest_path(region)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return _load_manifest_from_s3(region) if include_s3 else None


def _tileset_name(target: str) -> str:
    return "global" if target == "global" else target


def _tileset_entry(artifact: dict[str, Any], upload_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": upload_result["url"],
        "key": upload_result["key"],
        "filename": artifact["filename"],
        "minzoom": artifact["minzoom"],
        "maxzoom": artifact["maxzoom"],
        "sha256": artifact.get("sha256"),
        "size_bytes": artifact.get("size_bytes"),
    }


def _validation_path_for(artifact: dict[str, Any]) -> Path:
    return Path(artifact["path"]).with_suffix(".validation.json")


def assert_artifact_publishable(artifact: dict[str, Any], upload_result: dict[str, Any], *, skip_upload: bool = False) -> None:
    local_path = Path(artifact["path"])
    if not local_path.exists() or local_path.stat().st_size <= 0:
        raise RuntimeError(f"Cannot publish manifest; PMTiles file is missing or empty: {local_path}")

    validation_path = _validation_path_for(artifact)
    if not validation_path.exists():
        raise RuntimeError(f"Cannot publish manifest; validation JSON is missing: {validation_path}")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("ok") is not True:
        raise RuntimeError(f"Cannot publish manifest; validation JSON is not ok=true: {validation_path}")
    if validation.get("target") != artifact.get("target") or validation.get("region") != artifact.get("region"):
        raise RuntimeError(f"Cannot publish manifest; validation JSON target/region does not match artifact: {validation_path}")

    expected_key = storage_key_for(artifact["region"], artifact["target"], artifact["filename"])
    if upload_result.get("key") != expected_key:
        raise RuntimeError(f"Cannot publish manifest; upload key {upload_result.get('key')!r} does not match expected {expected_key!r}")

    upload_enabled = bool(os.getenv("S3_BUCKET", "").strip())
    if upload_enabled and not skip_upload and not upload_result.get("uploaded"):
        raise RuntimeError("Cannot publish manifest; PMTiles upload did not complete while S3_BUCKET is configured.")


def publish_manifest(
    artifact: dict[str, Any],
    upload_result: dict[str, Any],
    manifest_region: str | None = None,
    skip_upload: bool = False,
) -> dict[str, Any]:
    assert_artifact_publishable(artifact, upload_result, skip_upload=skip_upload)

    target = artifact["target"]
    region = manifest_region or ("global" if target == "global" else artifact["region"])
    existing = load_existing_manifest(region, include_s3=not skip_upload) or {
        "schema_version": 1,
        "region": region,
        "tilesets": {},
    }

    existing["schema_version"] = 1
    existing["region"] = region
    existing["environment"] = os.getenv("ENVIRONMENT", "local")
    existing["last_updated"] = iso_now()
    existing["tilesets"] = existing.get("tilesets", {})
    existing["tilesets"][_tileset_name(target)] = _tileset_entry(artifact, upload_result)

    if region != "global" and "global" not in existing["tilesets"]:
        global_manifest = load_existing_manifest("global", include_s3=not skip_upload)
        if global_manifest and global_manifest.get("tilesets", {}).get("global"):
            existing["tilesets"]["global"] = global_manifest["tilesets"]["global"]

    manifest_path = _manifest_path(region)
    stage_path = _manifest_stage_path(region)
    write_json_atomic(stage_path, existing)
    try:
        if skip_upload:
            result = {
                "bucket": None,
                "key": _manifest_key(region),
                "url": url_for_key(_manifest_key(region)),
                "local_path": str(stage_path),
                "uploaded": False,
            }
        else:
            result = upload_file(
                stage_path,
                _manifest_key(region),
                cache_control=MANIFEST_CACHE_CONTROL,
                content_type=MANIFEST_CONTENT_TYPE,
            )
        write_json_atomic(manifest_path, existing)
    finally:
        try:
            stage_path.unlink()
        except FileNotFoundError:
            pass
    existing["manifest_url"] = result["url"]
    existing["manifest_key"] = result["key"]
    return existing


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish or update a short-cached PMTiles manifest.")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--upload-result", required=True)
    parser.add_argument("--manifest-region")
    parser.add_argument("--skip-upload", action="store_true")
    args = parser.parse_args()
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    upload_result = json.loads(Path(args.upload_result).read_text(encoding="utf-8"))
    manifest = publish_manifest(artifact, upload_result, args.manifest_region, skip_upload=args.skip_upload)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
