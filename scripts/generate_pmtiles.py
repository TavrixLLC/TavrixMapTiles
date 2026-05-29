from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    get_group,
    local_output_path_for,
    run_command,
    setup_logging,
    sha256_file,
    timestamp_id,
    write_json_atomic,
)


LOGGER = setup_logging("generate_pmtiles")


def filename_for(target: str, region: str, timestamp: str) -> str:
    group = get_group(target)
    return group["filename_template"].format(region=region, timestamp=timestamp)


def generate_pmtiles(exports_manifest: dict[str, Any], timestamp: str | None = None) -> dict[str, Any]:
    target = exports_manifest["target"]
    region = exports_manifest["region"]
    timestamp = timestamp or timestamp_id()
    group = get_group(target)
    filename = filename_for(target, region, timestamp)
    output_path = local_output_path_for(region, target, filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    cmd = [
        "tippecanoe",
        "--force",
        "--output",
        str(output_path),
        "--minimum-zoom",
        str(group["minzoom"]),
        "--maximum-zoom",
        str(group["maxzoom"]),
        "--name",
        filename.removesuffix(".pmtiles"),
        "--description",
        group.get("description", ""),
    ]
    cmd.extend(group.get("tippecanoe_options", []))

    for layer in exports_manifest["layers"]:
        layer_spec = {
            "file": layer["path"],
            "layer": layer["name"],
            "description": layer.get("description", ""),
        }
        cmd.append(f"--named-layer={json.dumps(layer_spec, separators=(',', ':'))}")

    run_command(cmd, LOGGER)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Tippecanoe did not create a valid PMTiles file: {output_path}")

    artifact = {
        "schema_version": 1,
        "build_id": exports_manifest["build_id"],
        "target": target,
        "region": region,
        "filename": filename,
        "path": str(output_path),
        "minzoom": group["minzoom"],
        "maxzoom": group["maxzoom"],
        "layers": [layer["name"] for layer in exports_manifest["layers"]],
        "size_bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
    }
    build_dir = Path(exports_manifest["exports_dir"]).parent
    write_json_atomic(build_dir / "pmtiles-artifact.json", artifact)
    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a PMTiles archive with Tippecanoe.")
    parser.add_argument("--exports-manifest", required=True)
    parser.add_argument("--timestamp")
    args = parser.parse_args()

    with Path(args.exports_manifest).open("r", encoding="utf-8") as handle:
        exports_manifest = json.load(handle)
    artifact = generate_pmtiles(exports_manifest, args.timestamp)
    print(json.dumps(artifact, indent=2))


if __name__ == "__main__":
    main()
