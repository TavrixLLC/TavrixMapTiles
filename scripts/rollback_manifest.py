from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from common import MANIFEST_CACHE_CONTROL, MANIFEST_CONTENT_TYPE, iso_now, url_for_key, write_json_atomic
from refresh_manifest import (
    TARGET_ORDER,
    ManifestArtifact,
    manifest_key,
    manifest_path,
    manifest_stage_path,
    safe_region,
    tileset_entry,
    valid_artifacts,
)


def load_active_manifest(region: str) -> dict[str, Any]:
    path = manifest_path(region)
    if not path.exists():
        raise RuntimeError(f"Active manifest does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def active_filenames(region: str) -> dict[str, str]:
    manifest = load_active_manifest(region)
    tilesets = manifest.get("tilesets")
    if not isinstance(tilesets, dict):
        raise RuntimeError(f"Active manifest has no tilesets object: {manifest_path(region)}")
    filenames = {}
    for target in TARGET_ORDER:
        tileset = tilesets.get(target)
        if not isinstance(tileset, dict) or not tileset.get("filename"):
            raise RuntimeError(f"Active manifest is missing tileset '{target}'.")
        filenames[target] = str(tileset["filename"])
    return filenames


def active_public_base_url(region: str) -> str | None:
    manifest = load_active_manifest(region)
    for tileset in manifest.get("tilesets", {}).values():
        if not isinstance(tileset, dict):
            continue
        url = str(tileset.get("url") or "")
        marker = "/tiles/"
        if url.startswith(("http://", "https://")) and marker in url:
            return url.split(marker, 1)[0]
    return None


def list_versions(region: str) -> dict[str, Any]:
    region = safe_region(region)
    active = {}
    try:
        active = active_filenames(region)
    except RuntimeError:
        active = {}
    versions = {}
    for target in TARGET_ORDER:
        versions[target] = [
            {
                "filename": artifact.filename,
                "path": str(artifact.path),
                "validation_path": str(artifact.validation_path),
                "stamp": artifact.stamp,
                "active": active.get(target) == artifact.filename,
            }
            for artifact in valid_artifacts(target, region)
        ]
    return {"region": region, "active": active, "versions": versions}


def _artifact_by_filename(target: str, region: str, filename: str) -> ManifestArtifact:
    for artifact in valid_artifacts(target, region):
        if artifact.filename == filename:
            return artifact
    raise RuntimeError(f"No valid {target} artifact named {filename!r}.")


def select_previous_artifacts(region: str) -> dict[str, ManifestArtifact]:
    active = active_filenames(region)
    selected = {}
    for target in TARGET_ORDER:
        artifacts = valid_artifacts(target, region)
        names = [artifact.filename for artifact in artifacts]
        active_name = active[target]
        if active_name not in names:
            raise RuntimeError(f"Active {target} artifact {active_name!r} is not a valid rollback source.")
        active_index = names.index(active_name)
        if active_index == 0:
            raise RuntimeError(f"No previous valid {target} artifact exists before {active_name!r}.")
        selected[target] = artifacts[active_index - 1]
    return selected


def select_explicit_artifacts(region: str, filenames: dict[str, str | None]) -> dict[str, ManifestArtifact]:
    active = active_filenames(region)
    selected = {}
    for target in TARGET_ORDER:
        filename = filenames.get(target) or active[target]
        selected[target] = _artifact_by_filename(target, region, filename)
    return selected


def _tileset_entry_with_base(artifact: ManifestArtifact, public_base_url: str | None) -> dict[str, Any]:
    entry = tileset_entry(artifact)
    if public_base_url:
        entry["url"] = public_base_url.rstrip("/") + "/" + entry["key"]
    return entry


def manifest_from_selection(region: str, selected: dict[str, ManifestArtifact], public_base_url: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "region": region,
        "tilesets": {target: _tileset_entry_with_base(selected[target], public_base_url) for target in TARGET_ORDER},
        "environment": "rollback",
        "last_updated": iso_now(),
    }


def rollback_region_manifest(
    region: str,
    *,
    dry_run: bool = True,
    skip_upload: bool = False,
    explicit_filenames: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    region = safe_region(region)
    selected = (
        select_explicit_artifacts(region, explicit_filenames)
        if explicit_filenames and any(explicit_filenames.values())
        else select_previous_artifacts(region)
    )
    configured_base = os.getenv("S3_PUBLIC_BASE_URL") or os.getenv("CDN_BASE_URL") or os.getenv("STATIC_BASE_URL")
    manifest = manifest_from_selection(region, selected, configured_base or active_public_base_url(region))
    result = {
        "schema_version": 1,
        "region": region,
        "dry_run": dry_run,
        "manifest_path": str(manifest_path(region)),
        "manifest_key": manifest_key(region),
        "manifest": manifest,
        "selected_artifacts": {
            target: {
                "filename": artifact.filename,
                "path": str(artifact.path),
                "validation_path": str(artifact.validation_path),
            }
            for target, artifact in selected.items()
        },
        "upload": None,
    }
    if dry_run:
        return result

    stage_path = manifest_stage_path(region)
    write_json_atomic(stage_path, manifest)
    try:
        if skip_upload:
            upload_result = {
                "bucket": None,
                "key": manifest_key(region),
                "url": url_for_key(manifest_key(region)),
                "local_path": str(stage_path),
                "uploaded": False,
            }
        else:
            from upload import upload_file

            upload_result = upload_file(
                stage_path,
                manifest_key(region),
                cache_control=MANIFEST_CACHE_CONTROL,
                content_type=MANIFEST_CONTENT_TYPE,
            )
        write_json_atomic(manifest_path(region), manifest)
    finally:
        try:
            stage_path.unlink()
        except FileNotFoundError:
            pass
    result["upload"] = upload_result
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely rollback a regional manifest to previous valid immutable PMTiles versions.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--list", action="store_true", help="List valid rollback candidates and active versions.")
    parser.add_argument("--dry-run", action="store_true", help="Show the rollback manifest without writing or uploading.")
    parser.add_argument("--apply", action="store_true", help="Apply the rollback. Without this flag the command is dry-run only.")
    parser.add_argument("--skip-upload", action="store_true", help="Do not upload the manifest, even if S3_BUCKET is configured.")
    parser.add_argument("--global-filename")
    parser.add_argument("--basemap-filename")
    parser.add_argument("--pois-filename")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.list:
        print(json.dumps(list_versions(args.region), indent=2))
        return
    explicit = {
        "global": args.global_filename,
        "basemap": args.basemap_filename,
        "pois": args.pois_filename,
    }
    result = rollback_region_manifest(
        args.region,
        dry_run=not args.apply or args.dry_run,
        skip_upload=args.skip_upload,
        explicit_filenames=explicit,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
