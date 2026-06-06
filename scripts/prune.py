from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from common import OUTPUT_DIR, setup_logging
from upload import _s3_client


LOGGER = setup_logging("prune")

VERSIONED_PMTILES_RE = re.compile(
    r"^(?:global-z\d+-z\d+-\d{8}-\d{4}|(?:basemap|pois)-[a-z0-9][a-z0-9_-]*-z\d+-z\d+-\d{8}-\d{4})\.pmtiles$"
)


def is_versioned_pmtiles_name(name: str) -> bool:
    return bool(VERSIONED_PMTILES_RE.fullmatch(name))


def _keys_from_manifest(manifest: dict[str, Any]) -> set[str]:
    keys = set()
    for tileset in manifest.get("tilesets", {}).values():
        key = tileset.get("key")
        if key:
            keys.add(key)
    return keys


def active_keys_from_local_manifests() -> set[str]:
    keys: set[str] = set()
    manifest_dir = OUTPUT_DIR / "manifests"
    if not manifest_dir.exists():
        return keys
    for path in manifest_dir.glob("*.json"):
        try:
            keys.update(_keys_from_manifest(json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            LOGGER.warning("Skipping invalid manifest JSON: %s", path)
    return keys


def active_keys_from_s3_manifests() -> set[str]:
    bucket = os.getenv("S3_BUCKET", "").strip()
    if not bucket:
        return set()
    client = _s3_client()
    keys: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="manifests/"):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.endswith(".json"):
                continue
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            keys.update(_keys_from_manifest(json.loads(body)))
    return keys


def prune_local(retention_days: int, active_keys: set[str]) -> dict[str, list[str]]:
    deleted: list[str] = []
    skipped: list[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    tiles_root = OUTPUT_DIR / "tiles"
    if not tiles_root.exists():
        return {"deleted": deleted, "skipped": skipped}

    for path in tiles_root.rglob("*.pmtiles"):
        key = path.relative_to(OUTPUT_DIR).as_posix()
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if key in active_keys:
            continue
        if not is_versioned_pmtiles_name(path.name):
            skipped.append(key)
            LOGGER.warning("Skipping local PMTiles without clear versioned filename: %s", path)
            continue
        if mtime >= cutoff:
            continue
        LOGGER.info("Pruning local old PMTiles: %s", path)
        path.unlink()
        deleted.append(key)
    return {"deleted": deleted, "skipped": skipped}


def prune_s3(retention_days: int, active_keys: set[str]) -> dict[str, list[str]]:
    bucket = os.getenv("S3_BUCKET", "").strip()
    if not bucket:
        return {"deleted": [], "skipped": []}

    client = _s3_client()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted: list[str] = []
    skipped: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="tiles/"):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.endswith(".pmtiles"):
                continue
            if key in active_keys:
                continue
            if not is_versioned_pmtiles_name(Path(key).name):
                skipped.append(key)
                LOGGER.warning("Skipping S3 PMTiles without clear versioned filename: s3://%s/%s", bucket, key)
                continue
            if item["LastModified"] >= cutoff:
                continue
            LOGGER.info("Pruning S3 old PMTiles: s3://%s/%s", bucket, key)
            client.delete_object(Bucket=bucket, Key=key)
            deleted.append(key)
    return {"deleted": deleted, "skipped": skipped}


def prune(retention_days: int) -> dict[str, Any]:
    active_keys = active_keys_from_local_manifests() | active_keys_from_s3_manifests()
    local = prune_local(retention_days, active_keys)
    s3 = prune_s3(retention_days, active_keys)
    return {
        "retention_days": retention_days,
        "active_keys": sorted(active_keys),
        "local_deleted": local["deleted"],
        "local_skipped": local["skipped"],
        "s3_deleted": s3["deleted"],
        "s3_skipped": s3["skipped"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune old PMTiles versions that are not referenced by active manifests.")
    parser.add_argument("--retention-days", type=int, default=int(os.getenv("RETENTION_DAYS", "30")))
    args = parser.parse_args()
    result = prune(args.retention_days)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
