from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from common import (
    center_lonlat,
    get_group,
    get_region,
    lonlat_to_tile,
    run_command,
    setup_logging,
    write_json_atomic,
)
from validate_published import validate_pmtiles_url


LOGGER = setup_logging("validate_pmtiles")


def _json_from_pmtiles(args: list[str]) -> dict[str, Any]:
    result = run_command(args, LOGGER)
    text = result.stdout or "{}"
    return json.loads(text)


def _metadata_layers(metadata: dict[str, Any]) -> set[str]:
    if isinstance(metadata.get("vector_layers"), list):
        return {item.get("id") for item in metadata["vector_layers"] if item.get("id")}

    nested = metadata.get("json")
    if isinstance(nested, str) and nested.strip():
        try:
            parsed = json.loads(nested)
        except json.JSONDecodeError:
            return set()
        if isinstance(parsed.get("vector_layers"), list):
            return {item.get("id") for item in parsed["vector_layers"] if item.get("id")}

    return set()


def _metadata_vector_layers(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(metadata.get("vector_layers"), list):
        return [item for item in metadata["vector_layers"] if isinstance(item, dict)]

    nested = metadata.get("json")
    if isinstance(nested, str) and nested.strip():
        try:
            parsed = json.loads(nested)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed.get("vector_layers"), list):
            return [item for item in parsed["vector_layers"] if isinstance(item, dict)]
    return []


def _metadata_layer_fields(metadata: dict[str, Any]) -> dict[str, set[str]]:
    fields: dict[str, set[str]] = {}
    for layer in _metadata_vector_layers(metadata):
        layer_id = layer.get("id")
        layer_fields = layer.get("fields")
        if isinstance(layer_id, str) and isinstance(layer_fields, dict):
            fields[layer_id] = {str(field) for field in layer_fields}
    return fields


def _configured_multilingual_fields(layer: dict[str, Any]) -> set[str]:
    multilingual = layer.get("multilingual") or {}
    if not multilingual.get("enabled"):
        return set()
    languages = multilingual.get("languages")
    if not languages:
        languages = ["ar", "en", "ku", "fa", "tr", "fr", "de", "es", "ru", "pt", "it", "ur"]
    return {"name_local", "name_int"} | {f"name_{lang}" for lang in languages}


def _multilingual_field_check(metadata: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    metadata_fields = _metadata_layer_fields(metadata)
    layers = {}
    for layer in group.get("layers", []):
        expected = _configured_multilingual_fields(layer)
        if not expected:
            continue
        layer_name = layer["name"]
        observed = metadata_fields.get(layer_name, set())
        layers[layer_name] = {
            "expected": sorted(expected),
            "observed": sorted(observed & expected),
            "missing": sorted(expected - observed),
        }
    return {"layers": layers}


def _multilingual_coverage(quality: dict[str, Any] | None) -> dict[str, Any]:
    coverage = {}
    if not quality:
        return coverage
    for layer_name, metrics in quality.get("layers", {}).items():
        feature_count = int(metrics.get("feature_count") or 0)
        if not feature_count:
            continue
        layer_coverage = {}
        for field in ("name_local", "name_en", "name_ar", "name_ku"):
            count_key = f"{field}_count"
            if count_key in metrics:
                count = int(metrics.get(count_key) or 0)
                layer_coverage[field] = {
                    "count": count,
                    "coverage": round(count / feature_count, 4),
                }
        if layer_coverage:
            coverage[layer_name] = layer_coverage
    return coverage


def _header_zoom(header: dict[str, Any], name: str) -> int | None:
    for key in (name, name.replace("zoom", "_zoom")):
        value = header.get(key)
        if value is not None:
            return int(value)
    return None


def _header_bounds(header: dict[str, Any]) -> list[float] | None:
    bounds = header.get("bounds")
    if isinstance(bounds, list) and len(bounds) == 4:
        return [float(value) for value in bounds]
    if isinstance(bounds, str):
        pieces = [piece.strip() for piece in bounds.split(",")]
        if len(pieces) == 4:
            return [float(piece) for piece in pieces]
    return None


def _sample_points(region_config: dict[str, Any]) -> list[tuple[float, float]]:
    min_lon, min_lat, max_lon, max_lat = [float(value) for value in region_config["bbox"]]
    center = center_lonlat(region_config)
    return [
        center,
        ((min_lon + max_lon) / 2, (min_lat * 0.75) + (max_lat * 0.25)),
        ((min_lon + max_lon) / 2, (min_lat * 0.25) + (max_lat * 0.75)),
        ((min_lon * 0.75) + (max_lon * 0.25), (min_lat + max_lat) / 2),
        ((min_lon * 0.25) + (max_lon * 0.75), (min_lat + max_lat) / 2),
    ]


def _tile_readable(pmtiles_path: Path, zoom: int, region_config: dict[str, Any]) -> dict[str, Any]:
    attempts = []
    for lon, lat in _sample_points(region_config):
        x, y = lonlat_to_tile(lon, lat, zoom)
        cmd = ["pmtiles", "tile", str(pmtiles_path), str(zoom), str(x), str(y)]
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        attempts.append({"z": zoom, "x": x, "y": y, "returncode": result.returncode})
        if result.returncode == 0:
            return {"zoom": zoom, "ok": True, "attempts": attempts}
    return {"zoom": zoom, "ok": False, "attempts": attempts}


def _validate_cdn(url: str) -> dict[str, Any]:
    return validate_pmtiles_url(url, timeout=20)


def _validate_quality(quality: dict[str, Any] | None, group: dict[str, Any]) -> list[str]:
    failures = []
    if not quality:
        return failures

    configured_layers = {layer["name"]: layer for layer in group["layers"]}
    for layer_name, metrics in quality.get("layers", {}).items():
        if int(metrics.get("null_geometry_count") or 0) > 0:
            failures.append(f"{layer_name} has null geometries")
        if int(metrics.get("invalid_geometry_count") or 0) > 0:
            failures.append(f"{layer_name} has invalid geometries")

        layer_cfg = configured_layers.get(layer_name, {})
        min_features = layer_cfg.get("min_features")
        max_features = layer_cfg.get("max_features")
        count = int(metrics.get("feature_count") or 0)
        if min_features is not None and count < int(min_features):
            failures.append(f"{layer_name} has {count} features below configured minimum {min_features}")
        if max_features is not None and count > int(max_features):
            failures.append(f"{layer_name} has {count} features above configured maximum {max_features}")
    return failures


def validate_pmtiles(
    artifact: dict[str, Any],
    cdn_url: str | None = None,
    quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pmtiles_path = Path(artifact["path"])
    target = artifact["target"]
    region = artifact["region"]
    group = get_group(target)
    region_config = get_region(region)
    failures: list[str] = []

    if not pmtiles_path.exists():
        failures.append(f"file does not exist: {pmtiles_path}")
    elif pmtiles_path.stat().st_size == 0:
        failures.append(f"file is empty: {pmtiles_path}")

    header: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    multilingual_field_check: dict[str, Any] = {"layers": {}}
    if not failures:
        run_command(["pmtiles", "show", str(pmtiles_path)], LOGGER)
        run_command(["pmtiles", "verify", str(pmtiles_path)], LOGGER)
        header = _json_from_pmtiles(["pmtiles", "show", str(pmtiles_path), "--header-json"])
        metadata = _json_from_pmtiles(["pmtiles", "show", str(pmtiles_path), "--metadata"])

        minzoom = _header_zoom(header, "minzoom")
        maxzoom = _header_zoom(header, "maxzoom")
        if minzoom != group["minzoom"]:
            failures.append(f"minzoom {minzoom} does not match expected {group['minzoom']}")
        if maxzoom != group["maxzoom"]:
            failures.append(f"maxzoom {maxzoom} does not match expected {group['maxzoom']}")

        expected_layers = set(artifact.get("layers") or [layer["name"] for layer in group["layers"]])
        actual_layers = _metadata_layers(metadata)
        missing_layers = expected_layers - actual_layers
        if missing_layers:
            failures.append(f"missing vector layers in metadata: {sorted(missing_layers)}")
        multilingual_field_check = _multilingual_field_check(metadata, group)

        for zoom in group.get("sample_zooms", []):
            sample = _tile_readable(pmtiles_path, int(zoom), region_config)
            if not sample["ok"]:
                failures.append(f"sample tile not readable at z{zoom}: {sample['attempts']}")

    quality_failures = _validate_quality(quality, group)
    failures.extend(quality_failures)

    cdn_check = None
    if cdn_url:
        cdn_check = _validate_cdn(cdn_url)
        if not cdn_check["ok"]:
            failures.append(f"CDN/static server did not return 200/206 for range request: {cdn_check}")

    result = {
        "schema_version": 1,
        "target": target,
        "region": region,
        "path": str(pmtiles_path),
        "ok": not failures,
        "failures": failures,
        "header": header,
        "bounds": _header_bounds(header) if header else None,
        "metadata_layers": sorted(_metadata_layers(metadata)) if metadata else [],
        "multilingual_field_check": multilingual_field_check,
        "multilingual_coverage": _multilingual_coverage(quality),
        "cdn_check": cdn_check,
        "quality_summary": quality,
    }

    build_dir = pmtiles_path.parent
    if artifact.get("build_id"):
        build_dir = Path(artifact["path"]).parent
    write_json_atomic(Path(artifact["path"]).with_suffix(".validation.json"), result)
    if failures:
        raise RuntimeError("PMTiles validation failed: " + "; ".join(failures))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a PMTiles archive.")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--cdn-url")
    parser.add_argument("--quality-json")
    args = parser.parse_args()

    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    quality = None
    if args.quality_json:
        quality = json.loads(Path(args.quality_json).read_text(encoding="utf-8"))
    result = validate_pmtiles(artifact, args.cdn_url, quality)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
