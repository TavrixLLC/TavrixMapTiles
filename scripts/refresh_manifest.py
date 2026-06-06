from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import (
    MANIFEST_CACHE_CONTROL,
    MANIFEST_CONTENT_TYPE,
    OUTPUT_DIR,
    iso_now,
    setup_logging,
    sha256_file,
    storage_key_for,
    url_for_key,
    write_json_atomic,
)


LOGGER = setup_logging("refresh_manifest")

TARGET_ORDER = ("global", "basemap", "pois")
REGION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class ManifestArtifact:
    target: str
    region: str
    filename: str
    path: Path
    validation_path: Path
    stamp: str
    minzoom: int
    maxzoom: int
    size_bytes: int
    sha256: str


def safe_region(value: str) -> str:
    region = value.strip().lower()
    if not REGION_RE.fullmatch(region):
        raise ValueError(f"Invalid region id: {value!r}")
    if region == "global":
        raise ValueError("Use a regional id such as 'iraq'; global manifests are published by global builds.")
    return region


def manifest_path(region: str) -> Path:
    return OUTPUT_DIR / "manifests" / f"{region}.json"


def manifest_key(region: str) -> str:
    return f"manifests/{region}.json"


def manifest_stage_path(region: str) -> Path:
    return OUTPUT_DIR / "manifests" / f".{region}.refresh.json"


def validation_path_for(pmtiles_path: Path) -> Path:
    return pmtiles_path.with_suffix(".validation.json")


def artifact_pattern(target: str, region: str) -> re.Pattern[str]:
    if target == "global":
        return re.compile(r"^global-z(?P<minzoom>\d+)-z(?P<maxzoom>\d+)-(?P<stamp>\d{8}-\d{4})\.pmtiles$")
    return re.compile(
        rf"^{re.escape(target)}-{re.escape(region)}-z(?P<minzoom>\d+)-z(?P<maxzoom>\d+)-(?P<stamp>\d{{8}}-\d{{4}})\.pmtiles$"
    )


def output_dir_for(target: str, region: str) -> Path:
    return OUTPUT_DIR / "tiles" / ("global" if target == "global" else region)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _header_zoom(validation: dict[str, Any], name: str, fallback: int) -> int:
    header = validation.get("header") or {}
    for key in (name, name.replace("zoom", "_zoom")):
        value = header.get(key)
        if value is not None:
            return int(value)
    return fallback


def _validated_artifact(path: Path, target: str, region: str, match: re.Match[str]) -> ManifestArtifact | None:
    if not path.exists() or path.stat().st_size <= 0:
        return None

    validation_path = validation_path_for(path)
    if not validation_path.exists():
        return None

    validation = _json(validation_path)
    expected_region = "global" if target == "global" else region
    if validation.get("ok") is not True:
        return None
    if validation.get("target") != target or validation.get("region") != expected_region:
        return None

    fallback_minzoom = int(match.group("minzoom"))
    fallback_maxzoom = int(match.group("maxzoom"))
    return ManifestArtifact(
        target=target,
        region=expected_region,
        filename=path.name,
        path=path,
        validation_path=validation_path,
        stamp=match.group("stamp"),
        minzoom=_header_zoom(validation, "minzoom", fallback_minzoom),
        maxzoom=_header_zoom(validation, "maxzoom", fallback_maxzoom),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )


def valid_artifacts(target: str, region: str) -> list[ManifestArtifact]:
    pattern = artifact_pattern(target, region)
    candidates: list[ManifestArtifact] = []
    for path in output_dir_for(target, region).glob("*.pmtiles"):
        match = pattern.fullmatch(path.name)
        if not match:
            continue
        artifact = _validated_artifact(path, target, region, match)
        if artifact:
            candidates.append(artifact)
    return sorted(candidates, key=lambda artifact: (artifact.stamp, artifact.filename))


def latest_valid_artifact(target: str, region: str) -> ManifestArtifact:
    artifacts = valid_artifacts(target, region)
    if not artifacts:
        expected_dir = output_dir_for(target, region)
        raise RuntimeError(f"No valid {target} PMTiles artifact found in {expected_dir}")
    return artifacts[-1]


def tileset_entry(artifact: ManifestArtifact) -> dict[str, Any]:
    key = storage_key_for(artifact.region, artifact.target, artifact.filename)
    return {
        "url": url_for_key(key),
        "key": key,
        "filename": artifact.filename,
        "minzoom": artifact.minzoom,
        "maxzoom": artifact.maxzoom,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
    }


def build_regional_manifest(region: str) -> tuple[dict[str, Any], dict[str, ManifestArtifact]]:
    selected = {target: latest_valid_artifact(target, region) for target in TARGET_ORDER}
    manifest = {
        "schema_version": 1,
        "region": region,
        "tilesets": {target: tileset_entry(selected[target]) for target in TARGET_ORDER},
        "environment": os.getenv("ENVIRONMENT", "local"),
        "last_updated": iso_now(),
    }
    return manifest, selected


def _local_manifest_upload(region: str, path: Path) -> dict[str, Any]:
    key = manifest_key(region)
    return {
        "bucket": None,
        "key": key,
        "url": url_for_key(key),
        "local_path": str(path),
        "uploaded": False,
    }


def refresh_region_manifest(region: str, *, skip_upload: bool = False, no_prune: bool = False) -> dict[str, Any]:
    region = safe_region(region)
    manifest, selected = build_regional_manifest(region)

    path = manifest_path(region)
    stage_path = manifest_stage_path(region)
    write_json_atomic(stage_path, manifest)

    try:
        if skip_upload:
            upload_result = _local_manifest_upload(region, stage_path)
        else:
            from upload import upload_file

            upload_result = upload_file(
                stage_path,
                manifest_key(region),
                cache_control=MANIFEST_CACHE_CONTROL,
                content_type=MANIFEST_CONTENT_TYPE,
            )
        write_json_atomic(path, manifest)
    finally:
        try:
            stage_path.unlink()
        except FileNotFoundError:
            pass

    if not no_prune:
        LOGGER.info("Manifest refresh never prunes PMTiles artifacts; --no-prune is accepted for build command parity.")

    result = {
        "schema_version": 1,
        "region": region,
        "manifest": {
            "path": str(path),
            "url": upload_result["url"],
            "key": upload_result["key"],
            "tilesets": manifest["tilesets"],
        },
        "selected_artifacts": {
            target: {
                "path": str(artifact.path),
                "validation_path": str(artifact.validation_path),
                "filename": artifact.filename,
            }
            for target, artifact in selected.items()
        },
        "upload": upload_result,
        "prune": None,
    }
    LOGGER.info("Refreshed manifest for %s with %s", region, ", ".join(a.filename for a in selected.values()))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh a regional manifest from latest locally validated PMTiles artifacts.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--skip-upload", action="store_true", help="Write the local manifest but do not upload it.")
    parser.add_argument("--no-prune", action="store_true", help="Accepted for build command parity; refresh never prunes PMTiles.")
    args = parser.parse_args()

    result = refresh_region_manifest(args.region, skip_upload=args.skip_upload, no_prune=args.no_prune)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
