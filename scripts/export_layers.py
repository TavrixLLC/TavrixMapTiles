from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import psycopg2

from common import (
    TMP_DIR,
    ensure_dirs,
    get_group,
    get_region,
    iso_now,
    load_config,
    ogr_pg_dsn,
    pg_conn_kwargs,
    render_sql,
    run_command,
    setup_logging,
    timestamp_id,
    write_json_atomic,
)


LOGGER = setup_logging("export_layers")

DEFAULT_SUPPORTED_LANGUAGES = ["ar", "en", "ku", "fa", "tr", "fr", "de", "es", "ru", "pt", "it", "ur"]
DEFAULT_ROAD_LANGUAGES = ["ar", "en", "ku"]


def _language_config() -> dict[str, Any]:
    try:
        return load_config("languages.json")
    except FileNotFoundError:
        return {
            "supported_languages": DEFAULT_SUPPORTED_LANGUAGES,
            "road_languages": DEFAULT_ROAD_LANGUAGES,
        }


def supported_languages() -> list[str]:
    values = _language_config().get("supported_languages") or DEFAULT_SUPPORTED_LANGUAGES
    return [str(value).strip().lower() for value in values if str(value).strip()]


def road_languages() -> list[str]:
    values = _language_config().get("road_languages") or DEFAULT_ROAD_LANGUAGES
    return [str(value).strip().lower() for value in values if str(value).strip()]


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _split_table_name(value: str) -> tuple[str, str]:
    parts = [part.strip('"') for part in value.split(".")]
    if len(parts) == 1:
        return "public", parts[0]
    return parts[-2], parts[-1]


def _qualified_table(value: str) -> str:
    schema, table = _split_table_name(value)
    return f"{_quote_ident(schema)}.{_quote_ident(table)}"


class SchemaInspector:
    def __init__(self) -> None:
        self._cache: dict[str, dict[str, dict[str, str]]] = {}

    def columns_for_table(self, table_name: str) -> dict[str, dict[str, str]]:
        if table_name in self._cache:
            return self._cache[table_name]

        schema, table = _split_table_name(table_name)
        with psycopg2.connect(**pg_conn_kwargs()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, data_type, udt_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    """,
                    (schema, table),
                )
                columns = {
                    str(name): {"data_type": str(data_type), "udt_name": str(udt_name)}
                    for name, data_type, udt_name in cur.fetchall()
                }
        self._cache[table_name] = columns
        return columns

    def describe_table(self, table_name: str) -> dict[str, Any]:
        columns = self.columns_for_table(table_name)
        tags = columns.get("tags")
        tag_storage = None
        if tags:
            udt_name = tags.get("udt_name", "").lower()
            data_type = tags.get("data_type", "").lower()
            if udt_name == "hstore":
                tag_storage = "hstore"
            elif udt_name in {"json", "jsonb"} or data_type in {"json", "jsonb"}:
                tag_storage = udt_name or data_type
            else:
                tag_storage = data_type or udt_name
        return {
            "table": table_name,
            "has_tags": bool(tags),
            "tag_storage": tag_storage,
            "explicit_multilingual_columns": sorted(
                name for name in columns if name.startswith("name:") or re.fullmatch(r"name_[a-z]{2,3}", name)
            ),
            "has_int_name": "int_name" in columns,
            "has_wikidata": "wikidata" in columns,
        }


def _tag_expr(alias: str, columns: dict[str, dict[str, str]], tag_name: str) -> str | None:
    info = columns.get("tags")
    if not info:
        return None
    udt_name = info.get("udt_name", "").lower()
    data_type = info.get("data_type", "").lower()
    tag_literal = _quote_literal(tag_name)
    if udt_name == "hstore":
        return f"{alias}.{_quote_ident('tags')} -> {tag_literal}"
    if udt_name in {"json", "jsonb"} or data_type in {"json", "jsonb"}:
        return f"{alias}.{_quote_ident('tags')} ->> {tag_literal}"
    return None


def _column_expr(alias: str, columns: dict[str, dict[str, str]], column_name: str) -> str | None:
    if column_name in columns:
        return f"{alias}.{_quote_ident(column_name)}::text"
    return None


def _clean_expr(expr: str | None) -> str:
    return f"NULLIF(BTRIM(({expr})::text), '')" if expr else "NULL::text"


def _coalesced_clean_expr(exprs: list[str | None]) -> str:
    usable = [expr for expr in exprs if expr]
    if not usable:
        return "NULL::text"
    return f"NULLIF(BTRIM(COALESCE({', '.join(f'({expr})::text' for expr in usable)})), '')"


def _osm_tag_expr(alias: str, columns: dict[str, dict[str, str]], tag_name: str) -> str | None:
    return _column_expr(alias, columns, tag_name) or _tag_expr(alias, columns, tag_name)


def _language_name_expr(alias: str, columns: dict[str, dict[str, str]], lang: str) -> str:
    if lang == "ku":
        candidates = ["name:ku", "name_ku", "name:ckb", "name_ckb"]
    else:
        candidates = [f"name:{lang}", f"name_{lang}"]
    return _coalesced_clean_expr([_osm_tag_expr(alias, columns, candidate) for candidate in candidates])


def _int_name_expr(alias: str, columns: dict[str, dict[str, str]]) -> str:
    return _coalesced_clean_expr([
        _column_expr(alias, columns, "int_name"),
        _tag_expr(alias, columns, "int_name"),
    ])


def _wikidata_expr(alias: str, columns: dict[str, dict[str, str]]) -> str:
    return _coalesced_clean_expr([
        _column_expr(alias, columns, "wikidata"),
        _tag_expr(alias, columns, "wikidata"),
    ])


def _multilingual_languages(layer: dict[str, Any]) -> list[str]:
    multilingual = layer.get("multilingual") or {}
    values = multilingual.get("languages")
    if values:
        return [str(value).strip().lower() for value in values if str(value).strip()]
    if multilingual.get("scope") == "road":
        return road_languages()
    return supported_languages()


def enrich_multilingual_sql(rendered_sql: str, layer: dict[str, Any], inspector: SchemaInspector) -> str:
    multilingual = layer.get("multilingual") or {}
    if not multilingual.get("enabled"):
        return rendered_sql

    source_table = multilingual.get("source_table")
    if not source_table:
        raise RuntimeError(f"Layer {layer['name']} enables multilingual export but has no source_table.")

    columns = inspector.columns_for_table(source_table)
    source_alias = "ml_src"
    select_parts = [
        "base.*",
        "NULLIF(BTRIM(base.name::text), '') AS name_local",
        f"{_int_name_expr(source_alias, columns)} AS name_int",
    ]

    selected_languages = set(_multilingual_languages(layer))
    for lang in supported_languages():
        if lang in selected_languages:
            select_parts.append(f"{_language_name_expr(source_alias, columns, lang)} AS name_{lang}")

    if multilingual.get("include_wikidata"):
        select_parts.append(f"{_wikidata_expr(source_alias, columns)} AS wikidata")

    LOGGER.info(
        "Layer %s multilingual extraction: %s",
        layer["name"],
        json.dumps(inspector.describe_table(source_table), sort_keys=True),
    )

    return f"""
        WITH base AS (
            {rendered_sql}
        )
        SELECT {", ".join(select_parts)}
        FROM base
        LEFT JOIN {_qualified_table(source_table)} AS {source_alias}
            ON {source_alias}.{_quote_ident("osm_id")}::text = base.id::text
    """


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

    if (layer.get("multilingual") or {}).get("enabled"):
        for field in ("name_local", "name_en", "name_ar", "name_ku"):
            select_parts.append(
                f"COUNT(*) FILTER (WHERE NULLIF(BTRIM(COALESCE({field}::text, '')), '') IS NOT NULL)::bigint AS {field}_count"
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


def ogr2ogr_command(output_path: Path, rendered_sql: str, layer_name: str) -> list[str]:
    return [
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
        "-s_srs",
        "EPSG:4326",
        "-t_srs",
        "EPSG:4326",
    ]


def export_layer(
    layer: dict[str, Any],
    region_name: str,
    region_config: dict[str, Any],
    exports_dir: Path,
    inspector: SchemaInspector,
) -> dict[str, Any]:
    layer_name = layer["name"]
    rendered_sql = enrich_multilingual_sql(render_sql(layer["sql"], region_name, region_config), layer, inspector)
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

    cmd = ogr2ogr_command(output_path, rendered_sql, layer_name)
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
    inspector = SchemaInspector()
    for layer in group["layers"]:
        exports.append(export_layer(layer, region_name, region_config, exports_dir, inspector))

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
