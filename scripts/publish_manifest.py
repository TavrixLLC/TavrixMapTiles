from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import botocore.exceptions

from common import (
    MANIFEST_CACHE_CONTROL,
    OUTPUT_DIR,
    iso_now,
    write_json_atomic,
)
from upload import _s3_client, upload_file


def _manifest_path(region: str) -> Path:
    return OUTPUT_DIR / "manifests" / f"{region}.json"


def _manifest_key(region: str) -> str:
    return f"manifests/{region}.json"


def _load_manifest_from_s3(region: str) -> dict[str, Any] | None:
    bucket = os.getenv("S3_BUCKET", "").strip()
    if not bucket:
        return None
    try:
        response = _s3_client().get_object(Bucket=bucket, Key=_manifest_key(region))
    except botocore.exceptions.ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise
    return json.loads(response["Body"].read().decode("utf-8"))


def load_existing_manifest(region: str) -> dict[str, Any] | None:
    path = _manifest_path(region)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return _load_manifest_from_s3(region)


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


def publish_manifest(
    artifact: dict[str, Any],
    upload_result: dict[str, Any],
    manifest_region: str | None = None,
) -> dict[str, Any]:
    target = artifact["target"]
    region = manifest_region or ("global" if target == "global" else artifact["region"])
    existing = load_existing_manifest(region) or {
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
        global_manifest = load_existing_manifest("global")
        if global_manifest and global_manifest.get("tilesets", {}).get("global"):
            existing["tilesets"]["global"] = global_manifest["tilesets"]["global"]

    manifest_path = _manifest_path(region)
    write_json_atomic(manifest_path, existing)

    result = upload_file(
        manifest_path,
        _manifest_key(region),
        cache_control=MANIFEST_CACHE_CONTROL,
        content_type="application/json",
    )
    existing["manifest_url"] = result["url"]
    existing["manifest_key"] = result["key"]
    return existing


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish or update a short-cached PMTiles manifest.")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--upload-result", required=True)
    parser.add_argument("--manifest-region")
    args = parser.parse_args()
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    upload_result = json.loads(Path(args.upload_result).read_text(encoding="utf-8"))
    manifest = publish_manifest(artifact, upload_result, args.manifest_region)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
