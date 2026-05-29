from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg2

from common import (
    TMP_DIR,
    ensure_dirs,
    get_group,
    get_region,
    iso_now,
    ogr_pg_dsn,
    pg_conn_kwargs,
    render_sql,
    run_command,
    setup_logging,
    timestamp_id,
    write_json_atomic,
)


LOGGER = setup_logging("export_layers")


def _quality_query(rendered_sql: str, layer: dict[str, Any]) -> str:
    select_parts = [
        "COUNT(*)::bigint AS feature_count",
        "COUNT(*) FILTER (WHERE geom IS NULL)::bigint AS null_geometry_count",
        "COUNT(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsValid(geom))::bigint AS invalid_geometry_count",
        "ST_Extent(geom)::text AS bbox_extent",
    ]

    if layer.get("name_field"):
        field = layer["name_field"]
        select_parts.append(
            f"COUNT(*) FILTER (WHERE NULLIF(BTRIM(COALESCE({field}::text, '')), '') IS NULL)::bigint AS empty_name_count"
        )

    if layer.get("category_field"):
        field = layer["category_field"]
        select_parts.append(
            f"COUNT(*) FILTER (WHERE NULLIF(BTRIM(COALESCE({field}::text, '')), '') IS NULL)::bigint AS missing_category_count"
        )

    return f"""
        WITH layer_data AS (
            {rendered_sql}
        )
        SELECT {", ".join(select_parts)}
        FROM layer_data
    """


def _geometry_type_query(rendered_sql: str) -> str:
    return f"""
        WITH layer_data AS (
            {rendered_sql}
        )
        SELECT COALESCE(GeometryType(geom), 'NULL') AS geometry_type, COUNT(*)::bigint AS count
        FROM layer_data
        GROUP BY 1
        ORDER BY 1
    """


def collect_quality(rendered_sql: str, layer: dict[str, Any]) -> dict[str, Any]:
    with psycopg2.connect(**pg_conn_kwargs()) as conn:
        with conn.cursor() as cur:
            cur.execute(_quality_query(rendered_sql, layer))
            columns = [desc[0] for desc in cur.description]
            values = cur.fetchone()
            quality = dict(zip(columns, values))

            cur.execute(_geometry_type_query(rendered_sql))
            quality["geometry_type_check"] = {
                row[0]: int(row[1])
                for row in cur.fetchall()
            }
    return quality


def export_layer(
    layer: dict[str, Any],
    region_name: str,
    region_config: dict[str, Any],
    exports_dir: Path,
) -> dict[str, Any]:
    layer_name = layer["name"]
    rendered_sql = render_sql(layer["sql"], region_name, region_config)
    output_path = exports_dir / f"{layer_name}.fgb"
    if output_path.exists():
        output_path.unlink()

    quality = collect_quality(rendered_sql, layer)
    LOGGER.info(
        "Layer %s quality: %s features, %s invalid geometries",
        layer_name,
        quality.get("feature_count"),
        quality.get("invalid_geometry_count"),
    )

    cmd = [
        "ogr2ogr",
        "-f",
        "FlatGeobuf",
        str(output_path),
        ogr_pg_dsn(),
        "-dialect",
        "PostgreSQL",
        "-sql",
        rendered_sql,
        "-nln",
        layer_name,
        "-t_srs",
        "EPSG:4326",
    ]
    run_command(cmd, LOGGER)

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Export produced an empty file for layer {layer_name}: {output_path}")

    return {
        "name": layer_name,
        "description": layer.get("description", ""),
        "path": str(output_path),
        "quality": quality,
    }


def export_layers(target: str, region_name: str, build_id: str | None = None) -> dict[str, Any]:
    ensure_dirs()
    group = get_group(target)
    region_config = get_region(region_name)
    build_id = build_id or f"{target}-{region_name}-{timestamp_id()}"
    build_dir = TMP_DIR / build_id
    exports_dir = build_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    exports = []
    for layer in group["layers"]:
        exports.append(export_layer(layer, region_name, region_config, exports_dir))

    manifest = {
        "schema_version": 1,
        "build_id": build_id,
        "target": target,
        "region": region_name,
        "created_at": iso_now(),
        "exports_dir": str(exports_dir),
        "layers": exports,
    }
    write_json_atomic(build_dir / "exports-manifest.json", manifest)
    write_json_atomic(
        build_dir / "quality.json",
        {
            "schema_version": 1,
            "build_id": build_id,
            "target": target,
            "region": region_name,
            "layers": {item["name"]: item["quality"] for item in exports},
        },
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export configured PostGIS layers to GeoJSON.")
    parser.add_argument("--target", required=True, choices=["global", "basemap", "pois"])
    parser.add_argument("--region", required=True)
    parser.add_argument("--build-id")
    args = parser.parse_args()
    result = export_layers(args.target, args.region, args.build_id)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
