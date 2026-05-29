from __future__ import annotations

import json
import os
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


APP_ROOT = Path(os.getenv("APP_ROOT", "/app"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", APP_ROOT / "output"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", APP_ROOT / "config"))
STYLES_DIR = Path(os.getenv("STYLES_DIR", CONFIG_DIR / "styles"))
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8090"))
DEFAULT_REGION = os.getenv("REGION", "saudi")
DEFAULT_STYLE = os.getenv("MAP_STYLE", "light")
API_CORS_ORIGIN = os.getenv("API_CORS_ORIGIN", "*")
GLYPHS_URL = os.getenv("GLYPHS_URL", "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf")


def openapi_spec() -> dict:
    style_param = {
        "name": "style",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "default": DEFAULT_STYLE},
        "description": "Style id from /api/styles, for example light, dark, or navigation.",
    }
    lon_param = {
        "name": "lon",
        "in": "query",
        "required": False,
        "schema": {"type": "number", "format": "double", "example": 50.6},
        "description": "Longitude used to resolve the active region from configured region bounding boxes.",
    }
    lat_param = {
        "name": "lat",
        "in": "query",
        "required": False,
        "schema": {"type": "number", "format": "double", "example": 26.2},
        "description": "Latitude used to resolve the active region from configured region bounding boxes.",
    }
    region_path_param = {
        "name": "region",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "example": DEFAULT_REGION},
        "description": "Region id with an active manifest, such as saudi, iraq, uae, or global.",
    }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "PMTiles Map API",
            "version": "1.0.0",
            "description": (
                "Local API that exposes active PMTiles manifests and MapLibre style JSON. "
                "It does not serve live vector tiles; MapLibre reads immutable PMTiles through pmtiles.js."
            ),
        },
        "servers": [{"url": "/", "description": "Current API host"}],
        "tags": [
            {"name": "Docs", "description": "Swagger/OpenAPI metadata."},
            {"name": "Health", "description": "Runtime health and local configuration."},
            {"name": "Catalog", "description": "Available regions and styles."},
            {"name": "MapLibre", "description": "Manifest and style endpoints consumed by the frontend."},
            {"name": "Demo", "description": "Local browser demo."},
        ],
        "paths": {
            "/": {
                "get": {
                    "tags": ["Demo"],
                    "summary": "Open the local MapLibre demo",
                    "responses": {
                        "200": {
                            "description": "HTML demo page.",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/demo": {
                "get": {
                    "tags": ["Demo"],
                    "summary": "Open the local MapLibre demo",
                    "responses": {
                        "200": {
                            "description": "HTML demo page.",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/docs": {
                "get": {
                    "tags": ["Docs"],
                    "summary": "Open Swagger UI",
                    "responses": {
                        "200": {
                            "description": "Swagger UI HTML page.",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/api/docs": {
                "get": {
                    "tags": ["Docs"],
                    "summary": "Open Swagger UI",
                    "responses": {
                        "200": {
                            "description": "Swagger UI HTML page.",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/api/openapi.json": {
                "get": {
                    "tags": ["Docs"],
                    "summary": "Get the OpenAPI document",
                    "responses": {
                        "200": {
                            "description": "OpenAPI 3 document.",
                            "content": {
                                "application/json": {
                                    "schema": {"type": "object"},
                                    "example": {"openapi": "3.0.3", "info": {"title": "PMTiles Map API"}},
                                }
                            },
                        }
                    },
                }
            },
            "/api/health": {
                "get": {
                    "tags": ["Health"],
                    "summary": "Health check",
                    "responses": {
                        "200": {
                            "description": "API runtime status.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/HealthResponse"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/regions": {
                "get": {
                    "tags": ["Catalog"],
                    "summary": "List configured and published regions",
                    "responses": {
                        "200": {
                            "description": "Region list.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/RegionsResponse"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/styles": {
                "get": {
                    "tags": ["Catalog"],
                    "summary": "List available MapLibre style templates",
                    "responses": {
                        "200": {
                            "description": "Style list.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/StylesResponse"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/resolve": {
                "get": {
                    "tags": ["MapLibre"],
                    "summary": "Resolve lon/lat to the active PMTiles region",
                    "parameters": [lon_param, lat_param],
                    "responses": {
                        "200": {
                            "description": "Resolved region and manifest URL.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ResolveResponse"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/manifest.json": {
                "get": {
                    "tags": ["MapLibre"],
                    "summary": "Get the active manifest for the resolved region",
                    "description": "If lon/lat are omitted, the API uses REGION from the environment or the first published manifest.",
                    "parameters": [lon_param, lat_param],
                    "responses": {
                        "200": {
                            "description": "Active manifest.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Manifest"}
                                }
                            },
                        },
                        "404": {"$ref": "#/components/responses/NotFound"},
                    },
                }
            },
            "/api/manifest/{region}.json": {
                "get": {
                    "tags": ["MapLibre"],
                    "summary": "Get the active manifest for a specific region",
                    "parameters": [region_path_param],
                    "responses": {
                        "200": {
                            "description": "Active manifest.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Manifest"}
                                }
                            },
                        },
                        "404": {"$ref": "#/components/responses/NotFound"},
                    },
                }
            },
            "/api/style.json": {
                "get": {
                    "tags": ["MapLibre"],
                    "summary": "Get a MapLibre style for the resolved region",
                    "description": "The response injects PMTiles sources from the active manifest and filters layers that are not present in the active tilesets.",
                    "parameters": [lon_param, lat_param, style_param],
                    "responses": {
                        "200": {
                            "description": "MapLibre style JSON.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/MapLibreStyle"}
                                }
                            },
                        },
                        "404": {"$ref": "#/components/responses/NotFound"},
                    },
                }
            },
            "/api/style/{region}.json": {
                "get": {
                    "tags": ["MapLibre"],
                    "summary": "Get a MapLibre style for a specific region",
                    "parameters": [region_path_param, style_param],
                    "responses": {
                        "200": {
                            "description": "MapLibre style JSON.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/MapLibreStyle"}
                                }
                            },
                        },
                        "404": {"$ref": "#/components/responses/NotFound"},
                    },
                }
            },
        },
        "components": {
            "responses": {
                "NotFound": {
                    "description": "Requested manifest or style was not found.",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                        }
                    },
                }
            },
            "schemas": {
                "ErrorResponse": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                    "required": ["error"],
                },
                "HealthResponse": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "default_region": {"type": "string"},
                        "default_style": {"type": "string"},
                        "output_dir": {"type": "string"},
                        "manifests_dir_exists": {"type": "boolean"},
                        "styles_dir_exists": {"type": "boolean"},
                        "docs_url": {"type": "string"},
                        "openapi_url": {"type": "string"},
                    },
                    "required": ["ok", "default_region", "default_style"],
                },
                "Region": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "has_manifest": {"type": "boolean"},
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "center": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "required": ["id", "name", "has_manifest"],
                },
                "RegionsResponse": {
                    "type": "object",
                    "properties": {
                        "regions": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Region"},
                        }
                    },
                    "required": ["regions"],
                },
                "StyleSummary": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "layer_count": {"type": "integer"},
                        "has_labels": {"type": "boolean"},
                    },
                    "required": ["id", "name", "layer_count", "has_labels"],
                },
                "StylesResponse": {
                    "type": "object",
                    "properties": {
                        "styles": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/StyleSummary"},
                        },
                        "default_style": {"type": "string"},
                    },
                    "required": ["styles", "default_style"],
                },
                "ResolveResponse": {
                    "type": "object",
                    "properties": {
                        "region": {"type": "string"},
                        "manifest_url": {"type": "string"},
                    },
                    "required": ["region", "manifest_url"],
                },
                "Tileset": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "key": {"type": "string"},
                        "filename": {"type": "string"},
                        "minzoom": {"type": "integer"},
                        "maxzoom": {"type": "integer"},
                        "sha256": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                    },
                    "required": ["url", "minzoom", "maxzoom"],
                },
                "Manifest": {
                    "type": "object",
                    "properties": {
                        "schema_version": {"type": "integer"},
                        "region": {"type": "string"},
                        "last_updated": {"type": "string", "format": "date-time"},
                        "environment": {"type": "string"},
                        "tilesets": {
                            "type": "object",
                            "additionalProperties": {"$ref": "#/components/schemas/Tileset"},
                        },
                    },
                    "required": ["schema_version", "region", "tilesets"],
                },
                "MapLibreStyle": {
                    "type": "object",
                    "description": "MapLibre GL style specification with PMTiles vector sources injected from the active manifest.",
                    "properties": {
                        "version": {"type": "integer", "example": 8},
                        "name": {"type": "string"},
                        "glyphs": {"type": "string"},
                        "sources": {"type": "object"},
                        "layers": {"type": "array", "items": {"type": "object"}},
                        "center": {"type": "array", "items": {"type": "number"}},
                        "zoom": {"type": "number"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["version", "sources", "layers"],
                },
            },
        },
    }


def swagger_ui_html() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>PMTiles Map API Docs</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
    <style>
      body { margin: 0; background: #f7f8fa; }
      .swagger-ui .topbar { display: none; }
    </style>
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
      window.addEventListener("load", () => {
        SwaggerUIBundle({
          url: "/api/openapi.json",
          dom_id: "#swagger-ui",
          deepLinking: true,
          presets: [SwaggerUIBundle.presets.apis],
          layout: "BaseLayout"
        });
      });
    </script>
  </body>
</html>
"""


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def manifest_path(region: str) -> Path:
    safe_region = region.replace("/", "").replace("\\", "")
    return OUTPUT_DIR / "manifests" / f"{safe_region}.json"


def load_manifest(region: str) -> dict:
    path = manifest_path(region)
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found for region '{region}'")
    return read_json(path)


def load_regions() -> dict:
    path = CONFIG_DIR / "regions.json"
    if not path.exists():
        return {"regions": {}}
    return read_json(path)


def region_center(region: str) -> list[float]:
    regions = load_regions().get("regions", {})
    if region in regions and "center" in regions[region]:
        return regions[region]["center"]
    return [45.0, 24.0]


def listed_regions() -> list[dict]:
    configured = load_regions().get("regions", {})
    manifests_dir = OUTPUT_DIR / "manifests"
    manifest_regions = set()
    if manifests_dir.exists():
        manifest_regions = {path.stem for path in manifests_dir.glob("*.json")}

    names = sorted(set(configured) | manifest_regions)
    return [
        {
            "id": name,
            "name": configured.get(name, {}).get("name", name),
            "has_manifest": name in manifest_regions,
            "bbox": configured.get(name, {}).get("bbox"),
            "center": configured.get(name, {}).get("center"),
        }
        for name in names
    ]


def safe_id(value: str) -> str:
    return value.replace("/", "").replace("\\", "").removesuffix(".json")


def style_path(style_id: str) -> Path:
    return STYLES_DIR / f"{safe_id(style_id)}.json"


def list_styles() -> list[dict]:
    if not STYLES_DIR.exists():
        return []

    styles = []
    for path in sorted(STYLES_DIR.glob("*.json")):
        try:
            style = read_json(path)
        except json.JSONDecodeError:
            continue
        styles.append(
            {
                "id": path.stem,
                "name": style.get("name", path.stem),
                "layer_count": len(style.get("layers", [])),
                "has_labels": any(layer.get("type") == "symbol" for layer in style.get("layers", [])),
            }
        )
    return styles


def resolve_style_id(query: dict[str, list[str]]) -> str:
    requested = query.get("style", [DEFAULT_STYLE])[0] or DEFAULT_STYLE
    if style_path(requested).exists():
        return safe_id(requested)
    if style_path(DEFAULT_STYLE).exists():
        return safe_id(DEFAULT_STYLE)
    styles = list_styles()
    if styles:
        return styles[0]["id"]
    raise FileNotFoundError(f"No style files found in {STYLES_DIR}")


def load_style_template(style_id: str) -> dict:
    path = style_path(style_id)
    if not path.exists():
        raise FileNotFoundError(f"Style not found: {style_id}")
    style = read_json(path)
    glyphs = style.get("glyphs")
    if not glyphs or glyphs == "{GLYPHS_URL}":
        style["glyphs"] = GLYPHS_URL
    return style


def manifest_region_ids() -> set[str]:
    manifests_dir = OUTPUT_DIR / "manifests"
    if not manifests_dir.exists():
        return set()
    return {path.stem for path in manifests_dir.glob("*.json")}


def bbox_contains(region_config: dict, lon: float, lat: float) -> bool:
    bbox = region_config.get("bbox")
    if not bbox or len(bbox) != 4:
        return False
    min_lon, min_lat, max_lon, max_lat = [float(value) for value in bbox]
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def resolve_region(query: dict[str, list[str]]) -> str:
    manifests = manifest_region_ids()
    configured = load_regions().get("regions", {})

    try:
        lon = float(query.get("lon", [""])[0])
        lat = float(query.get("lat", [""])[0])
    except ValueError:
        lon = lat = None

    if lon is not None and lat is not None:
        for region_id, region_config in configured.items():
            if region_id in manifests and bbox_contains(region_config, lon, lat):
                return region_id

    if DEFAULT_REGION in manifests:
        return DEFAULT_REGION
    if manifests:
        return sorted(manifests)[0]
    return DEFAULT_REGION


def pmtiles_url(raw_url: str) -> str:
    return raw_url if raw_url.startswith("pmtiles://") else f"pmtiles://{raw_url}"


def source_entry(tileset: dict) -> dict:
    return {
        "type": "vector",
        "url": pmtiles_url(tileset["url"]),
        "minzoom": tileset.get("minzoom", 0),
        "maxzoom": tileset.get("maxzoom", 14),
    }


def validation_path_for(tileset: dict) -> Path | None:
    key = tileset.get("key")
    if not key:
        return None
    return (OUTPUT_DIR / key).with_suffix(".validation.json")


def tileset_bounds(tileset: dict) -> list[float] | None:
    bounds = tileset.get("bounds")
    if isinstance(bounds, list) and len(bounds) == 4:
        return [float(value) for value in bounds]

    validation_path = validation_path_for(tileset)
    if validation_path and validation_path.exists():
        validation = read_json(validation_path)
        bounds = validation.get("bounds")
        if isinstance(bounds, list) and len(bounds) == 4:
            return [float(value) for value in bounds]
    return None


def style_bounds(tilesets: dict) -> list[float] | None:
    for name in ("basemap", "pois", "global"):
        if name in tilesets:
            bounds = tileset_bounds(tilesets[name])
            if bounds:
                return bounds
    return None


def center_from_bounds(bounds: list[float]) -> list[float]:
    min_lon, min_lat, max_lon, max_lat = bounds
    return [(min_lon + max_lon) / 2, (min_lat + max_lat) / 2]


def source_layers_for(tilesets: dict) -> dict[str, set[str] | None]:
    result: dict[str, set[str] | None] = {}
    for source_name, tileset in tilesets.items():
        validation_path = validation_path_for(tileset)
        if not validation_path or not validation_path.exists():
            result[source_name] = None
            continue
        validation = read_json(validation_path)
        metadata_layers = validation.get("metadata_layers")
        if isinstance(metadata_layers, list):
            result[source_name] = {str(layer) for layer in metadata_layers}
        else:
            result[source_name] = None
    return result


def filter_layers_for_sources(layers: list[dict], sources: dict, source_layers: dict[str, set[str] | None]) -> list[dict]:
    filtered = []
    for layer in layers:
        source_name = layer.get("source")
        if not source_name:
            filtered.append(layer)
            continue
        if source_name not in sources:
            continue

        allowed_layers = source_layers.get(source_name)
        source_layer = layer.get("source-layer")
        if allowed_layers is not None and source_layer and source_layer not in allowed_layers:
            continue

        filtered.append(layer)
    return filtered


def add_global_layers(layers: list[dict]) -> None:
    layers.extend(
        [
            {
                "id": "global-countries",
                "type": "fill",
                "source": "global",
                "source-layer": "countries",
                "maxzoom": 6,
                "paint": {"fill-color": "#eef0e8", "fill-opacity": 1.0},
            },
            {
                "id": "global-water",
                "type": "fill",
                "source": "global",
                "source-layer": "water",
                "maxzoom": 6,
                "paint": {"fill-color": "#a9cfe7", "fill-opacity": 0.9},
            },
            {
                "id": "global-major-roads",
                "type": "line",
                "source": "global",
                "source-layer": "major_roads",
                "maxzoom": 6,
                "paint": {
                    "line-color": "#d19947",
                    "line-width": ["interpolate", ["linear"], ["zoom"], 0, 0.2, 5, 1.2],
                },
            },
            {
                "id": "global-boundaries",
                "type": "line",
                "source": "global",
                "source-layer": "country_boundaries",
                "maxzoom": 6,
                "paint": {
                    "line-color": "#7f858a",
                    "line-width": ["interpolate", ["linear"], ["zoom"], 0, 0.2, 5, 0.8],
                    "line-dasharray": [2, 2],
                },
            },
        ]
    )


def add_basemap_layers(layers: list[dict]) -> None:
    layers.extend(
        [
            {
                "id": "landuse",
                "type": "fill",
                "source": "basemap",
                "source-layer": "landuse",
                "minzoom": 6,
                "paint": {
                    "fill-color": [
                        "match",
                        ["get", "landuse_class"],
                        "park",
                        "#b9d8a8",
                        "garden",
                        "#b9d8a8",
                        "grassland",
                        "#c9ddb1",
                        "wood",
                        "#9ec597",
                        "scrub",
                        "#b7c99b",
                        "sand",
                        "#e7d7a7",
                        "desert",
                        "#ead8ab",
                        "#d8d6c8",
                    ],
                    "fill-opacity": 0.55,
                },
            },
            {
                "id": "water",
                "type": "fill",
                "source": "basemap",
                "source-layer": "water",
                "minzoom": 6,
                "paint": {"fill-color": "#7db9d8", "fill-opacity": 0.85},
            },
            {
                "id": "boundaries",
                "type": "line",
                "source": "basemap",
                "source-layer": "boundaries",
                "minzoom": 6,
                "paint": {
                    "line-color": "#8c9196",
                    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.5, 14, 1.2],
                    "line-dasharray": [2, 2],
                },
            },
            {
                "id": "roads-casing",
                "type": "line",
                "source": "basemap",
                "source-layer": "roads",
                "minzoom": 6,
                "paint": {
                    "line-color": "#ffffff",
                    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.0, 10, 2.0, 14, 6.0],
                },
            },
            {
                "id": "roads",
                "type": "line",
                "source": "basemap",
                "source-layer": "roads",
                "minzoom": 6,
                "paint": {
                    "line-color": [
                        "match",
                        ["get", "road_class"],
                        "motorway",
                        "#cf6f3f",
                        "trunk",
                        "#d58b3f",
                        "primary",
                        "#d6a744",
                        "secondary",
                        "#c7b15a",
                        "#9da4a7",
                    ],
                    "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.55, 10, 1.25, 14, 4.0],
                },
            },
            {
                "id": "buildings",
                "type": "fill",
                "source": "basemap",
                "source-layer": "buildings",
                "minzoom": 13,
                "paint": {"fill-color": "#b8aaa1", "fill-opacity": 0.75},
            },
            {
                "id": "building-outlines",
                "type": "line",
                "source": "basemap",
                "source-layer": "buildings",
                "minzoom": 14,
                "paint": {"line-color": "#8f8178", "line-width": 0.4},
            },
        ]
    )


def add_poi_layers(layers: list[dict]) -> None:
    layers.extend(
        [
            {
                "id": "pois",
                "type": "circle",
                "source": "pois",
                "source-layer": "pois",
                "minzoom": 10,
                "paint": {
                    "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 16, 7.0],
                    "circle-color": [
                        "match",
                        ["get", "category"],
                        "restaurant",
                        "#d75d4a",
                        "cafe",
                        "#9b6b43",
                        "shop",
                        "#5d7fbf",
                        "hotel",
                        "#8062b7",
                        "fuel",
                        "#528f70",
                        "#394c59",
                    ],
                    "circle-stroke-color": "#ffffff",
                    "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 10, 0.5, 16, 1.5],
                    "circle-opacity": 0.88,
                },
            }
        ]
    )


def build_style(region: str, style_id: str = DEFAULT_STYLE) -> dict:
    manifest = load_manifest(region)
    tilesets = manifest.get("tilesets", {})
    bounds = style_bounds(tilesets)
    sources = {}

    if "global" in tilesets:
        sources["global"] = source_entry(tilesets["global"])
    if "basemap" in tilesets:
        sources["basemap"] = source_entry(tilesets["basemap"])
    if "pois" in tilesets:
        sources["pois"] = source_entry(tilesets["pois"])

    style = deepcopy(load_style_template(style_id))
    style["sources"] = sources
    style["center"] = center_from_bounds(bounds) if bounds else region_center(region)
    style["zoom"] = 10 if bounds and region != "global" else (8 if region != "global" else 2)

    metadata = style.get("metadata", {})
    metadata.update(
        {
            "schema_version": manifest.get("schema_version"),
            "region": region,
            "style_id": style_id,
            "last_updated": manifest.get("last_updated"),
            "bounds": bounds,
            "tilesets": tilesets,
        }
    )
    style["metadata"] = metadata
    style["layers"] = filter_layers_for_sources(style.get("layers", []), sources, source_layers_for(tilesets))

    return style


class Handler(BaseHTTPRequestHandler):
    server_version = "pmtiles-map-api/1.0"

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", API_CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        try:
            self.route_get()
        except FileNotFoundError as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def route_get(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path in ("/", "/demo"):
            self.write_file(Path(__file__).parent / "static" / "index.html", "text/html; charset=utf-8")
            return

        if path in ("/docs", "/api/docs"):
            self.write_text(swagger_ui_html(), "text/html; charset=utf-8")
            return

        if path in ("/openapi.json", "/api/openapi.json"):
            self.write_json(openapi_spec(), cache_seconds=60)
            return

        if path == "/api/health":
            self.write_json(
                {
                    "ok": True,
                    "default_region": DEFAULT_REGION,
                    "default_style": DEFAULT_STYLE,
                    "output_dir": str(OUTPUT_DIR),
                    "manifests_dir_exists": (OUTPUT_DIR / "manifests").exists(),
                    "styles_dir_exists": STYLES_DIR.exists(),
                    "docs_url": "/docs",
                    "openapi_url": "/api/openapi.json",
                }
            )
            return

        if path == "/api/regions":
            self.write_json({"regions": listed_regions()})
            return

        if path == "/api/styles":
            self.write_json({"styles": list_styles(), "default_style": DEFAULT_STYLE})
            return

        if path == "/api/resolve":
            region = resolve_region(query)
            self.write_json({"region": region, "manifest_url": f"/api/manifest/{region}.json"})
            return

        if path == "/api/manifest.json":
            self.write_json(load_manifest(resolve_region(query)), cache_seconds=60)
            return

        if path == "/api/style.json":
            self.write_json(build_style(resolve_region(query), resolve_style_id(query)), cache_seconds=60)
            return

        if path.startswith("/api/manifest/"):
            region = path.removeprefix("/api/manifest/").removesuffix(".json")
            self.write_json(load_manifest(region), cache_seconds=60)
            return

        if path.startswith("/api/style/"):
            region = path.removeprefix("/api/style/").removesuffix(".json")
            self.write_json(build_style(region, resolve_style_id(query)), cache_seconds=60)
            return

        self.write_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def write_json(
        self,
        payload: dict,
        status: HTTPStatus = HTTPStatus.OK,
        cache_seconds: int = 0,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cache_seconds:
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}, must-revalidate")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_text(
        self,
        payload: str,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        cache_seconds: int = 0,
    ) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_seconds:
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}, must-revalidate")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            raise FileNotFoundError(str(path))
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    httpd = ThreadingHTTPServer((API_HOST, API_PORT), Handler)
    print(f"PMTiles Map API listening on http://{API_HOST}:{API_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
