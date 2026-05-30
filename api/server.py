from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import base64
import subprocess
import threading
import time
import uuid
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from raster_tiles import (
    MAX_RASTER_ZOOM,
    RasterTileError,
    normalize_raster_format,
    raster_content_type,
    raster_tilejson,
    render_raster_tile,
    tile_center_lonlat,
    validate_tile,
)
from tile_validation import TileCoordinateValidationError

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("pmtiles-api")

# ─── Config ───────────────────────────────────────────────────────────────────

APP_ROOT      = Path(os.getenv("APP_ROOT", "/app"))
OUTPUT_DIR    = Path(os.getenv("OUTPUT_DIR", APP_ROOT / "output"))
CONFIG_DIR    = Path(os.getenv("CONFIG_DIR", APP_ROOT / "config"))
STYLES_DIR    = Path(os.getenv("STYLES_DIR", CONFIG_DIR / "styles"))
API_HOST      = os.getenv("API_HOST", "0.0.0.0")
API_PORT      = int(os.getenv("API_PORT", "8090"))
DEFAULT_REGION     = os.getenv("REGION", "saudi")
DEFAULT_STYLE      = os.getenv("MAP_STYLE", "light")
API_CORS_ORIGIN    = os.getenv("API_CORS_ORIGIN", "*")
GLYPHS_URL         = os.getenv("GLYPHS_URL", "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf")
LOCAL_GLYPHS_URL   = os.getenv("LOCAL_GLYPHS_URL", "/api/fonts/{fontstack}/{range}.pbf")
GLYPHS_DIR         = Path(os.getenv("GLYPHS_DIR", CONFIG_DIR / "glyphs"))
SPRITES_DIR        = Path(os.getenv("SPRITES_DIR", CONFIG_DIR / "sprites"))
MAP_INTERNAL_TOKEN = os.getenv("MAP_INTERNAL_TOKEN", "")
INTERNAL_ENDPOINTS_ENABLED = os.getenv("INTERNAL_ENDPOINTS_ENABLED", "true").lower() not in {"0", "false", "no"}
MAX_AUTO_REGIONS   = int(os.getenv("MAX_AUTO_REGIONS", "8"))
AUTO_BBOX_MIN_ZOOM = float(os.getenv("AUTO_BBOX_MIN_ZOOM", "5.5"))
CACHE_TTL          = int(os.getenv("CACHE_TTL", "30"))          # seconds
STYLE_CACHE_SECONDS = int(os.getenv("STYLE_CACHE_SECONDS", "300"))
MANIFEST_CACHE_SECONDS = int(os.getenv("MANIFEST_CACHE_SECONDS", "3600"))
TILEJSON_CACHE_SECONDS = int(os.getenv("TILEJSON_CACHE_SECONDS", "3600"))
ASSET_CACHE_SECONDS = int(os.getenv("ASSET_CACHE_SECONDS", "31536000"))
TILE_CACHE_SECONDS  = int(os.getenv("TILE_CACHE_SECONDS", "31536000"))
MAX_THREADS        = int(os.getenv("MAX_THREADS", "64"))
VECTOR_TILE_CONTENT_TYPE = os.getenv("VECTOR_TILE_CONTENT_TYPE", "application/vnd.mapbox-vector-tile")
EMPTY_SPRITE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/"
    "l63dWQAAAABJRU5ErkJggg=="
)

# ─── In-memory cache ──────────────────────────────────────────────────────────

class TTLCache:
    """Thread-safe TTL cache."""

    def __init__(self, ttl: int = CACHE_TTL) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock  = threading.Lock()
        self._ttl   = ttl
        self._hits  = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry and time.monotonic() - entry[1] < self._ttl:
                self._hits += 1
                return entry[0]
            if entry:
                self._store.pop(key, None)
            self._misses += 1
            return None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (value, time.monotonic())

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def cached(self, key: str, fn):
        hit = self.get(key)
        if hit is not None:
            return hit
        value = fn()
        self.set(key, value)
        return value

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            total_size = 0
            for key, (value, _ts) in self._store.items():
                try:
                    total_size += len(key.encode("utf-8")) + len(json.dumps(value, default=str).encode("utf-8"))
                except Exception:
                    total_size += len(key.encode("utf-8")) + len(repr(value).encode("utf-8"))
            return {
                "enabled": True,
                "ttl_seconds": self._ttl,
                "items": len(self._store),
                "size_bytes": total_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(1, self._hits + self._misses), 4),
            }


_cache = TTLCache()


class APIError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

# ─── Thread-limited server ────────────────────────────────────────────────────

class BoundedThreadingMixIn(ThreadingMixIn):
    """ThreadingHTTPServer with a semaphore to cap concurrent threads."""

    daemon_threads = True
    _semaphore     = threading.Semaphore(MAX_THREADS)

    def process_request(self, request, client_address):
        if not self._semaphore.acquire(blocking=False):
            log.warning("Thread pool exhausted – dropping request from %s", client_address[0])
            try:
                request.close()
            except Exception:
                pass
            return
        t = threading.Thread(target=self._process_request_thread, args=(request, client_address))
        t.daemon = self.daemon_threads
        t.start()

    def _process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._semaphore.release()


class LimitedThreadingHTTPServer(BoundedThreadingMixIn, ThreadingHTTPServer):
    pass

# ─── Helpers ──────────────────────────────────────────────────────────────────

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-]")


def safe_id(value: str) -> str:
    """Strict allowlist: only alphanumeric, dash, underscore."""
    return _SAFE_RE.sub("", value.removesuffix(".json"))


def safe_request_id(value: str | None) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]", "", value or "")
    return cleaned[:128] or uuid.uuid4().hex


def safe_asset_segment(value: str, label: str = "path segment") -> str:
    cleaned = value.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."} or ".." in cleaned:
        raise APIError("invalid_request", f"Invalid {label}", HTTPStatus.BAD_REQUEST)
    return cleaned


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def manifest_path(region: str) -> Path:
    return OUTPUT_DIR / "manifests" / f"{safe_id(region)}.json"


def _load_manifest_raw(region: str) -> dict:
    path = manifest_path(region)
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found for region '{region}'")
    return read_json(path)


def load_manifest(region: str) -> dict:
    return _cache.cached(f"manifest:{region}", lambda: _load_manifest_raw(region))


def _load_regions_raw() -> dict:
    path = CONFIG_DIR / "regions.json"
    return read_json(path) if path.exists() else {"regions": {}}


def load_regions() -> dict:
    return _cache.cached("regions", _load_regions_raw)


def _manifest_region_ids_raw() -> set[str]:
    d = OUTPUT_DIR / "manifests"
    return {p.stem for p in d.glob("*.json")} if d.exists() else set()


def manifest_region_ids() -> set[str]:
    return _cache.cached("manifest_ids", _manifest_region_ids_raw)


def region_center(region: str) -> list[float]:
    regions = load_regions().get("regions", {})
    if region in regions and "center" in regions[region]:
        return regions[region]["center"]
    return [45.0, 24.0]


def listed_regions() -> list[dict]:
    configured   = load_regions().get("regions", {})
    manifest_ids = manifest_region_ids()
    names = sorted(set(configured) | manifest_ids)
    return [
        {
            "id": name,
            "name": configured.get(name, {}).get("name", name),
            "has_manifest": name in manifest_ids,
            "bbox": configured.get(name, {}).get("bbox"),
            "center": configured.get(name, {}).get("center"),
        }
        for name in names
    ]


def style_path(style_id: str) -> Path:
    return STYLES_DIR / f"{safe_id(style_id)}.json"


def _style_layers(style: dict) -> list[dict]:
    layers = style.get("layers", [])
    return layers if isinstance(layers, list) else []


def style_has_text(style: dict) -> bool:
    for layer in _style_layers(style):
        layout = layer.get("layout", {})
        if layer.get("type") == "symbol" and "text-field" in layout:
            return True
    return False


def style_has_icons(style: dict) -> bool:
    for layer in _style_layers(style):
        layout = layer.get("layout", {})
        if layer.get("type") == "symbol" and "icon-image" in layout:
            return True
    return False


def style_supports_3d(style: dict) -> bool:
    return bool(style.get("pitch") or style.get("bearing") or any(l.get("type") == "fill-extrusion" for l in _style_layers(style)))


def style_zoom_range(style: dict) -> tuple[float, float]:
    minzooms = [float(l["minzoom"]) for l in _style_layers(style) if isinstance(l.get("minzoom"), (int, float))]
    maxzooms = [float(l["maxzoom"]) for l in _style_layers(style) if isinstance(l.get("maxzoom"), (int, float))]
    return (min(minzooms) if minzooms else 0.0, max(maxzooms) if maxzooms else 22.0)


def style_metadata(style_id: str) -> dict:
    path = style_path(style_id)
    if not path.exists():
        raise APIError("not_found", f"Style not found: {style_id}", HTTPStatus.NOT_FOUND)
    style = read_json(path)
    minzoom, maxzoom = style_zoom_range(style)
    sources = sorted({
        str(layer.get("source"))
        for layer in _style_layers(style)
        if layer.get("source")
    })
    tilesets = sorted({
        str(layer.get("source"))
        for layer in _style_layers(style)
        if layer.get("source") in {"global", "basemap", "pois"}
    })
    return {
        "id": safe_id(style_id),
        "name": style.get("name", safe_id(style_id)),
        "layer_count": len(_style_layers(style)),
        "has_labels": style_has_text(style),
        "supports_3d": style_supports_3d(style),
        "minzoom": minzoom,
        "maxzoom": maxzoom,
        "pitch": style.get("pitch"),
        "bearing": style.get("bearing"),
        "default_pitch": style.get("pitch") or 0,
        "default_bearing": style.get("bearing") or 0,
        "sources": sources,
        "tilesets": tilesets,
        "glyphs_required": style_has_text(style),
        "sprite_required": style_has_icons(style),
        "glyphs": style.get("glyphs"),
        "sprite": style.get("sprite"),
    }


def list_styles() -> list[dict]:
    if not STYLES_DIR.exists():
        return []
    styles = []
    for path in sorted(STYLES_DIR.glob("*.json")):
        try:
            styles.append(style_metadata(path.stem))
        except (json.JSONDecodeError, APIError):
            continue
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


def _load_style_template_raw(style_id: str) -> dict:
    path = style_path(style_id)
    if not path.exists():
        raise FileNotFoundError(f"Style not found: {style_id}")
    style = read_json(path)
    configured_glyphs_url = GLYPHS_URL if GLYPHS_URL and not GLYPHS_URL.startswith("http") else LOCAL_GLYPHS_URL
    if not style.get("glyphs") or style["glyphs"] == "{GLYPHS_URL}" or str(style.get("glyphs", "")).startswith("http"):
        style["glyphs"] = configured_glyphs_url
    if style_has_icons(style) and not style.get("sprite"):
        style["sprite"] = f"/api/sprites/{safe_id(style_id)}/sprite"
    return style


def load_style_template(style_id: str) -> dict:
    return _cache.cached(f"style:{style_id}", lambda: _load_style_template_raw(style_id))


# ─── Geo helpers ──────────────────────────────────────────────────────────────

def query_float(query: dict, name: str) -> float | None:
    try:
        return float(query.get(name, [""])[0])
    except (TypeError, ValueError):
        return None


def query_bbox(query: dict) -> list[float] | None:
    raw = query.get("bbox", [""])[0].strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    if len(parts) != 4:
        return None
    try:
        w, s, e, n = [float(p) for p in parts]
    except ValueError:
        return None
    return [min(w, e), min(s, n), max(w, e), max(s, n)]


def query_center(query: dict) -> list[float] | None:
    lon = query_float(query, "lon")
    lat = query_float(query, "lat")
    if lon is None or lat is None:
        return None
    return [lon, lat]


def bbox_contains(cfg: dict, lon: float, lat: float) -> bool:
    bbox = cfg.get("bbox")
    if not bbox or len(bbox) != 4:
        return False
    mn_lon, mn_lat, mx_lon, mx_lat = [float(v) for v in bbox]
    return mn_lon <= lon <= mx_lon and mn_lat <= lat <= mx_lat


def bbox_intersects(cfg: dict, vp: list[float]) -> bool:
    bbox = cfg.get("bbox")
    if not bbox or len(bbox) != 4:
        return False
    mn_lon, mn_lat, mx_lon, mx_lat = [float(v) for v in bbox]
    return not (mx_lon < vp[0] or mn_lon > vp[2] or mx_lat < vp[1] or mn_lat > vp[3])


def bbox_area(cfg: dict, vp: list[float]) -> float:
    bbox = cfg.get("bbox")
    if not bbox or len(bbox) != 4:
        return 0.0
    mn_lon, mn_lat, mx_lon, mx_lat = [float(v) for v in bbox]
    w = max(0.0, min(mx_lon, vp[2]) - max(mn_lon, vp[0]))
    h = max(0.0, min(mx_lat, vp[3]) - max(mn_lat, vp[1]))
    return w * h


def fallback_region(manifests: set[str]) -> str:
    if DEFAULT_REGION in manifests and DEFAULT_REGION != "global":
        return DEFAULT_REGION
    detail = sorted(r for r in manifests if r != "global")
    if detail:
        return detail[0]
    return "global" if "global" in manifests else DEFAULT_REGION


def point_region(query: dict, configured: dict, manifests: set[str]) -> str | None:
    lon = query_float(query, "lon")
    lat = query_float(query, "lat")
    if lon is None or lat is None:
        return None
    for rid, rcfg in configured.items():
        if rid == "global" or rid not in manifests:
            continue
        if bbox_contains(rcfg, lon, lat):
            return rid
    return None


def bbox_regions(query: dict, configured: dict, manifests: set[str]) -> list[str]:
    vp   = query_bbox(query)
    zoom = query_float(query, "zoom")
    if not vp or (zoom is not None and zoom < AUTO_BBOX_MIN_ZOOM):
        return []
    matches = [
        (rid, bbox_area(rcfg, vp))
        for rid, rcfg in configured.items()
        if rid != "global" and rid in manifests and bbox_intersects(rcfg, vp)
    ]
    matches.sort(key=lambda x: (-x[1], x[0]))
    return [rid for rid, _ in matches[:MAX_AUTO_REGIONS]]


def resolve_regions(query: dict) -> list[str]:
    manifests  = manifest_region_ids()
    configured = load_regions().get("regions", {})
    regions    = bbox_regions(query, configured, manifests)
    if regions:
        return regions
    region = point_region(query, configured, manifests)
    if region:
        return [region]
    return [fallback_region(manifests)]


def resolve_region(query: dict) -> str:
    return resolve_regions(query)[0]


# ─── Tileset / source helpers ─────────────────────────────────────────────────

def pmtiles_url(raw: str) -> str:
    return raw if raw.startswith("pmtiles://") else f"pmtiles://{raw}"


def source_entry(tileset: dict) -> dict:
    return {
        "type": "vector",
        "url": pmtiles_url(tileset["url"]),
        "minzoom": tileset.get("minzoom", 0),
        "maxzoom": tileset.get("maxzoom", 14),
    }


def validation_path_for(tileset: dict) -> Path | None:
    key = tileset.get("key")
    return (OUTPUT_DIR / key).with_suffix(".validation.json") if key else None


def validation_for(tileset: dict) -> dict:
    path = validation_path_for(tileset)
    if path and path.exists():
        try:
            return read_json(path)
        except json.JSONDecodeError:
            return {}
    return {}


def tileset_bounds(tileset: dict) -> list[float] | None:
    bounds = tileset.get("bounds")
    if isinstance(bounds, list) and len(bounds) == 4:
        return [float(v) for v in bounds]
    b = validation_for(tileset).get("bounds")
    if isinstance(b, list) and len(b) == 4:
        return [float(v) for v in b]
    return None


def tileset_path(tileset: dict) -> Path:
    key = tileset.get("key")
    if key:
        return OUTPUT_DIR / key
    local_path = tileset.get("local_path") or tileset.get("path")
    if local_path:
        return Path(local_path)
    raise APIError("not_found", "Tileset has no local path", HTTPStatus.NOT_FOUND)


def tileset_center(tileset: dict) -> list[float] | None:
    validation = validation_for(tileset)
    center = validation.get("header", {}).get("center") or validation.get("center")
    if isinstance(center, list) and len(center) >= 2:
        return [float(center[0]), float(center[1])]
    bounds = tileset_bounds(tileset)
    return center_from_bounds(bounds) if bounds else None


def tileset_layers(tileset: dict) -> list[str]:
    layers = validation_for(tileset).get("metadata_layers", [])
    return [str(layer) for layer in layers] if isinstance(layers, list) else []


def tileset_metadata(region: str, tileset_id: str, tileset: dict, manifest: dict | None = None) -> dict:
    path = tileset_path(tileset)
    validation = validation_for(tileset)
    header = validation.get("header", {})
    tile_count = validation.get("tile_count") or header.get("tile_count")
    sha256 = tileset.get("sha256")
    return {
        "id": tileset_id,
        "region": region,
        "url": tileset.get("url"),
        "key": tileset.get("key"),
        "filename": tileset.get("filename") or path.name,
        "minzoom": int(tileset.get("minzoom", header.get("minzoom", 0))),
        "maxzoom": int(tileset.get("maxzoom", header.get("maxzoom", 14))),
        "bounds": tileset_bounds(tileset),
        "center": tileset_center(tileset),
        "size_bytes": int(tileset.get("size_bytes") or (path.stat().st_size if path.exists() else 0)),
        "sha256": sha256,
        "last_updated": (manifest or {}).get("last_updated"),
        "format": header.get("tile_type", "mvt"),
        "tile_count": int(tile_count) if isinstance(tile_count, (int, float, str)) and str(tile_count).isdigit() else None,
        "etag": sha256 or (hashlib.md5(str(path).encode("utf-8")).hexdigest() if path.exists() else None),
        "path": str(path),
        "exists": path.exists(),
        "vector_layers": tileset_layers(tileset),
    }


def style_bounds(tilesets: dict) -> list[float] | None:
    for name in ("basemap", "pois", "global"):
        if name in tilesets:
            b = tileset_bounds(tilesets[name])
            if b:
                return b
    return None


def source_layers_for(tilesets: dict) -> dict[str, set[str] | None]:
    result: dict[str, set[str] | None] = {}
    for src_name, tileset in tilesets.items():
        meta = validation_for(tileset).get("metadata_layers")
        result[src_name] = {str(l) for l in meta} if isinstance(meta, list) else None
    return result


def layer_allowed(layer: dict, allowed: set[str] | None) -> bool:
    src_layer = layer.get("source-layer")
    return not (allowed is not None and src_layer and src_layer not in allowed)


def filter_layers(layers: list[dict], sources: dict, src_layers: dict) -> list[dict]:
    out = []
    for layer in layers:
        src = layer.get("source")
        if not src:
            out.append(layer)
            continue
        if src not in sources:
            continue
        if not layer_allowed(layer, src_layers.get(src)):
            continue
        out.append(layer)
    return out


def center_from_bounds(bounds: list[float]) -> list[float]:
    return [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2]


def union_bounds(all_bounds: list[list[float]]) -> list[float] | None:
    valid = [b for b in all_bounds if b and len(b) == 4]
    if not valid:
        return None
    return [
        min(b[0] for b in valid), min(b[1] for b in valid),
        max(b[2] for b in valid), max(b[3] for b in valid),
    ]


# ─── Layer zoom continuity fix ────────────────────────────────────────────────
# This is the core fix for elements disappearing on zoom.
# The global layers fade OUT at zoom 6 and basemap layers fade IN at zoom 6.
# Without overlap, there is a "gap" where neither is visible.
# We create a smooth 1-zoom handoff band so coverage is always complete.

GLOBAL_MAX_ZOOM    = 8      # global layers are hidden above this zoom
BASEMAP_MIN_ZOOM   = 5      # basemap layers are visible from this zoom
TRANSITION_BAND    = 2.0    # overlap zone (zoom units) where both render

def _zoom_transition_paint(base_paint: dict, fade_in: bool, start: float, end: float) -> dict:
    """
    Inject a zoom-based opacity expression so layers fade in/out smoothly
    instead of popping on or off at a hard zoom threshold.
    """
    paint = dict(base_paint)
    opacity_key = next(
        (k for k in paint if k.endswith("-opacity") or k == "opacity"),
        None,
    )
    base_opacity = paint.get(opacity_key, 1.0) if opacity_key else 1.0
    # If base_opacity is already an expression, wrap it conservatively
    if isinstance(base_opacity, list):
        base_opacity = 1.0

    fade_expr = [
        "interpolate", ["linear"], ["zoom"],
        start, 0.0 if fade_in else float(base_opacity),
        end,   float(base_opacity) if fade_in else 0.0,
    ]
    if opacity_key:
        paint[opacity_key] = fade_expr
    else:
        paint["fill-opacity"] = fade_expr  # fallback
    return paint


def add_global_layers(layers: list[dict]) -> None:
    """
    Global base layers that are always visible at low zoom.
    They fade OUT as the user zooms in (not hard-cut).
    """
    fade_start = GLOBAL_MAX_ZOOM - TRANSITION_BAND   # e.g. 5.5
    fade_end   = GLOBAL_MAX_ZOOM                      # e.g. 7

    layers.extend([
        {
            "id": "global-countries",
            "type": "fill",
            "source": "global",
            "source-layer": "countries",
            "maxzoom": GLOBAL_MAX_ZOOM + 1,           # render slot stays open
            "paint": _zoom_transition_paint(
                {"fill-color": "#eef0e8", "fill-opacity": 1.0},
                fade_in=False, start=fade_start, end=fade_end,
            ),
        },
        {
            "id": "global-water",
            "type": "fill",
            "source": "global",
            "source-layer": "water",
            "maxzoom": GLOBAL_MAX_ZOOM + 1,
            "paint": _zoom_transition_paint(
                {"fill-color": "#a9cfe7", "fill-opacity": 0.9},
                fade_in=False, start=fade_start, end=fade_end,
            ),
        },
        {
            "id": "global-major-roads",
            "type": "line",
            "source": "global",
            "source-layer": "major_roads",
            "maxzoom": GLOBAL_MAX_ZOOM + 1,
            "paint": {
                "line-color": "#d19947",
                "line-width": ["interpolate", ["linear"], ["zoom"], 0, 0.2, 5, 1.4, 8, 2.2],
                "line-opacity": [
                    "interpolate", ["linear"], ["zoom"],
                    fade_start, 1.0,
                    fade_end, 0.0,
                ],
            },
        },
        {
            "id": "global-boundaries",
            "type": "line",
            "source": "global",
            "source-layer": "country_boundaries",
            "maxzoom": GLOBAL_MAX_ZOOM + 1,
            "paint": {
                "line-color": "#7f858a",
                "line-width": ["interpolate", ["linear"], ["zoom"], 0, 0.2, 5, 0.8, 8, 1.1],
                "line-opacity": [
                    "interpolate", ["linear"], ["zoom"],
                    fade_start, 0.9,
                    fade_end, 0.0,
                ],
                "line-dasharray": [2, 2],
            },
        },
    ])


def add_basemap_layers(layers: list[dict]) -> None:
    """
    Detailed basemap layers.  They fade IN as the user zooms in,
    starting before the global layers have fully faded so there's
    always something visible.
    """
    fade_start = BASEMAP_MIN_ZOOM
    fade_end   = BASEMAP_MIN_ZOOM + TRANSITION_BAND   # e.g. 6.5

    layers.extend([
        {
            "id": "landuse",
            "type": "fill",
            "source": "basemap",
            "source-layer": "landuse",
            "minzoom": BASEMAP_MIN_ZOOM,
            "paint": {
                "fill-color": [
                    "match", ["get", "landuse_class"],
                    "park", "#b9d8a8", "garden", "#b9d8a8",
                    "grassland", "#c9ddb1", "wood", "#9ec597",
                    "scrub", "#b7c99b", "sand", "#e7d7a7",
                    "desert", "#ead8ab", "#d8d6c8",
                ],
                "fill-opacity": [
                    "interpolate", ["linear"], ["zoom"],
                    fade_start, 0.0, fade_end, 0.55,
                ],
            },
        },
        {
            "id": "water",
            "type": "fill",
            "source": "basemap",
            "source-layer": "water",
            "minzoom": BASEMAP_MIN_ZOOM,
            "paint": {
                "fill-color": "#7db9d8",
                "fill-opacity": [
                    "interpolate", ["linear"], ["zoom"],
                    fade_start, 0.0, fade_end, 0.85,
                ],
            },
        },
        {
            "id": "boundaries",
            "type": "line",
            "source": "basemap",
            "source-layer": "boundaries",
            "minzoom": BASEMAP_MIN_ZOOM,
            "paint": {
                "line-color": "#8c9196",
                "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.5, 14, 1.2],
                "line-dasharray": [2, 2],
                "line-opacity": [
                    "interpolate", ["linear"], ["zoom"],
                    fade_start, 0.0, fade_end, 1.0,
                ],
            },
        },
        {
            "id": "roads-casing",
            "type": "line",
            "source": "basemap",
            "source-layer": "roads",
            "minzoom": BASEMAP_MIN_ZOOM,
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
            "minzoom": BASEMAP_MIN_ZOOM,
            "paint": {
                "line-color": [
                    "match", ["get", "road_class"],
                    "motorway", "#cf6f3f", "trunk", "#d58b3f",
                    "primary", "#d6a744", "secondary", "#c7b15a",
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
    ])


def add_poi_layers(layers: list[dict]) -> None:
    layers.extend([
        {
            "id": "pois",
            "type": "circle",
            "source": "pois",
            "source-layer": "pois",
            "minzoom": 10,
            "paint": {
                "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 16, 7.0],
                "circle-color": [
                    "match", ["get", "category"],
                    "restaurant", "#d75d4a", "cafe", "#9b6b43",
                    "shop", "#5d7fbf", "hotel", "#8062b7",
                    "fuel", "#528f70", "#394c59",
                ],
                "circle-stroke-color": "#ffffff",
                "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 10, 0.5, 16, 1.5],
                "circle-opacity": 0.88,
            },
        }
    ])


# ─── Style builders ───────────────────────────────────────────────────────────

def layers_with_global(template_layers: list[dict], has_global: bool) -> list[dict]:
    layers = deepcopy(template_layers)
    if not has_global or any(l.get("source") == "global" for l in layers):
        return layers
    result   = []
    inserted = False
    for layer in layers:
        result.append(layer)
        if not inserted and layer.get("type") == "background":
            add_global_layers(result)
            inserted = True
    if not inserted:
        add_global_layers(result)
    return result


def region_source_name(region: str, tileset_name: str) -> str:
    return f"{tileset_name}-{safe_id(region)}"


def expand_layers_for_regions(
    template_layers: list[dict],
    sources: dict,
    src_layers: dict,
    regions: list[str],
) -> list[dict]:
    expanded = []
    for layer in template_layers:
        src = layer.get("source")
        if src not in ("basemap", "pois"):
            if src and src not in sources:
                continue
            if src and not layer_allowed(layer, src_layers.get(src)):
                continue
            expanded.append(layer)
            continue
        for region in regions:
            rsrc = region_source_name(region, src)
            if rsrc not in sources:
                continue
            rl = deepcopy(layer)
            rl["id"]     = f"{layer['id']}-{safe_id(region)}"
            rl["source"] = rsrc
            if not layer_allowed(rl, src_layers.get(rsrc)):
                continue
            expanded.append(rl)
    return expanded


def _apply_metadata(style: dict, manifest: dict, region: str, regions: list[str],
                    style_id: str, bounds: list[float] | None, tilesets: dict) -> None:
    meta = style.get("metadata", {})
    meta.update({
        "schema_version": manifest.get("schema_version"),
        "region":         region,
        "regions":        regions,
        "style_id":       style_id,
        "style_key":      f"{style_id}:{','.join(regions)}",
        "last_updated":   manifest.get("last_updated"),
        "bounds":         bounds,
        "tilesets":       tilesets,
    })
    style["metadata"] = meta


def build_style(region: str, style_id: str = DEFAULT_STYLE) -> dict:
    manifest = load_manifest(region)
    tilesets = manifest.get("tilesets", {})
    bounds   = style_bounds(tilesets)
    sources  = {}

    for name in ("global", "basemap", "pois"):
        if name in tilesets:
            sources[name] = source_entry(tilesets[name])

    style          = deepcopy(load_style_template(style_id))
    style["sources"] = sources
    style["center"]  = center_from_bounds(bounds) if bounds else region_center(region)
    style["zoom"]    = 10 if bounds and region != "global" else (8 if region != "global" else 2)

    layers = layers_with_global(style.get("layers", []), "global" in sources)
    style["layers"] = filter_layers(layers, sources, source_layers_for(tilesets))

    _apply_metadata(style, manifest, region, [region], style_id, bounds, tilesets)
    style["metadata"]["mode"] = "single"
    return style


def global_tileset_for(manifests_list: list[dict]) -> dict | None:
    for manifest in manifests_list:
        tileset = manifest.get("tilesets", {}).get("global")
        if tileset:
            return tileset
    # separate lookup — avoids shadowing loop variable
    global_ids = manifest_region_ids()
    if "global" in global_ids:
        return load_manifest("global").get("tilesets", {}).get("global")
    return None


def build_auto_style(query: dict, style_id: str = DEFAULT_STYLE) -> dict:
    regions = resolve_regions(query)
    if len(regions) == 1:
        style = build_style(regions[0], style_id)
        style["metadata"]["mode"] = "auto"
        return style

    manifests_list = [load_manifest(r) for r in regions]
    sources: dict        = {}
    source_tilesets: dict = {}
    detail_bounds: list  = []
    last_updated: list   = []

    global_ts = global_tileset_for(manifests_list)
    if global_ts:
        sources["global"]        = source_entry(global_ts)
        source_tilesets["global"] = global_ts

    for region, manifest in zip(regions, manifests_list):
        lu = manifest.get("last_updated")
        if lu:
            last_updated.append(lu)
        for ts_name in ("basemap", "pois"):
            tileset = manifest.get("tilesets", {}).get(ts_name)
            if not tileset:
                continue
            sname = region_source_name(region, ts_name)
            sources[sname]        = source_entry(tileset)
            source_tilesets[sname] = tileset
            b = tileset_bounds(tileset)
            if b:
                detail_bounds.append(b)

    bounds = union_bounds(detail_bounds)
    style  = deepcopy(load_style_template(style_id))
    style["sources"] = sources
    style["center"] = query_center(query) or (center_from_bounds(bounds) if bounds else region_center(regions[0]))
    style["zoom"] = query_float(query, "zoom") or (10 if bounds else 8)

    layers = layers_with_global(style.get("layers", []), "global" in sources)
    style["layers"] = expand_layers_for_regions(
        layers, sources, source_layers_for(source_tilesets), regions
    )

    fake_manifest = {"schema_version": 1, "last_updated": max(last_updated) if last_updated else None}
    _apply_metadata(style, fake_manifest, regions[0], regions, style_id, bounds, source_tilesets)
    style["metadata"]["mode"] = "auto"
    return style


def tilesets_for_region(region: str) -> list[dict]:
    manifest = load_manifest(region)
    return [
        tileset_metadata(region, tileset_id, tileset, manifest)
        for tileset_id, tileset in sorted(manifest.get("tilesets", {}).items())
    ]


def all_tilesets() -> list[dict]:
    result = []
    for region in sorted(manifest_region_ids()):
        try:
            result.extend(tilesets_for_region(region))
        except FileNotFoundError:
            continue
    return result


def get_tileset(region: str, tileset_id: str) -> tuple[dict, dict]:
    manifest = load_manifest(region)
    tileset = manifest.get("tilesets", {}).get(safe_id(tileset_id))
    if not tileset:
        raise APIError("not_found", f"Tileset not found: {region}/{tileset_id}", HTTPStatus.NOT_FOUND)
    return manifest, tileset


def choose_tileset_for_zoom(manifest: dict, z: int, tileset_id: str | None = None) -> tuple[str, dict]:
    tilesets = manifest.get("tilesets", {})
    if tileset_id:
        tileset_id = safe_id(tileset_id)
        tileset = tilesets.get(tileset_id)
        if not tileset:
            raise APIError("not_found", f"Tileset not found: {tileset_id}", HTTPStatus.NOT_FOUND)
        minzoom = int(tileset.get("minzoom", 0))
        maxzoom = int(tileset.get("maxzoom", 14))
        if not (minzoom <= z <= maxzoom):
            raise APIError(
                "not_found",
                f"Tile is outside tileset zoom range {minzoom}-{maxzoom}",
                HTTPStatus.NOT_FOUND,
                {"tileset": tileset_id, "z": z},
            )
        return tileset_id, tileset

    for candidate in ("global", "basemap", "pois"):
        tileset = tilesets.get(candidate)
        if not tileset:
            continue
        if int(tileset.get("minzoom", 0)) <= z <= int(tileset.get("maxzoom", 14)):
            return candidate, tileset
    raise APIError("not_found", f"No tileset covers zoom {z}", HTTPStatus.NOT_FOUND, {"z": z})


def read_pmtiles_tile(tileset: dict, z: int, x: int, y: int) -> bytes:
    path = tileset_path(tileset)
    if not path.exists():
        raise APIError("not_found", f"PMTiles file not found: {path}", HTTPStatus.NOT_FOUND)
    try:
        result = subprocess.run(
            ["pmtiles", "tile", str(path), str(z), str(x), str(y)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
    except FileNotFoundError as exc:
        raise APIError("dependency_unavailable", "pmtiles CLI is not installed", HTTPStatus.SERVICE_UNAVAILABLE) from exc
    except subprocess.TimeoutExpired as exc:
        raise APIError("timeout", "Timed out reading PMTiles tile", HTTPStatus.GATEWAY_TIMEOUT) from exc
    if result.returncode != 0 or not result.stdout or result.stdout.startswith(b"Tile not found"):
        raise APIError("not_found", "Tile not found", HTTPStatus.NOT_FOUND, {"z": z, "x": x, "y": y})
    return result.stdout


def vector_tile_response(region: str, z: int, x: int, y: int, tileset_id: str | None = None) -> tuple[bytes, str, dict]:
    validate_tile(z, x, y)
    manifest = load_manifest(region)
    selected_id, tileset = choose_tileset_for_zoom(manifest, z, tileset_id)
    body = read_pmtiles_tile(tileset, z, x, y)
    headers = {"Content-Encoding": "gzip"} if body[:2] == b"\x1f\x8b" else {}
    headers["X-Tavrix-Region"] = region
    headers["X-Tavrix-Tileset"] = selected_id
    return body, selected_id, headers


def vector_layers_for_tilesets(tilesets: dict) -> list[dict]:
    seen: set[str] = set()
    layers = []
    for tileset in tilesets.values():
        for layer in tileset_layers(tileset):
            if layer in seen:
                continue
            seen.add(layer)
            layers.append({"id": layer, "fields": {}})
    return layers


def vector_tilejson(region: str, tile_url: str, manifest: dict, tileset_id: str | None = None) -> dict:
    tilesets = manifest.get("tilesets", {})
    if tileset_id:
        safe_tileset_id = safe_id(tileset_id)
        if safe_tileset_id not in tilesets:
            raise APIError("not_found", f"Tileset not found: {tileset_id}", HTTPStatus.NOT_FOUND)
        selected_tilesets = {safe_tileset_id: tilesets[safe_tileset_id]}
    else:
        selected_tilesets = tilesets
    bounds = union_bounds([b for b in (tileset_bounds(ts) for ts in selected_tilesets.values()) if b])
    minzooms = [int(ts.get("minzoom", 0)) for ts in selected_tilesets.values()]
    maxzooms = [int(ts.get("maxzoom", 14)) for ts in selected_tilesets.values()]
    return {
        "tilejson": "3.0.0",
        "name": f"Tavrix vector {region}" + (f" {tileset_id}" if tileset_id else ""),
        "scheme": "xyz",
        "tiles": [tile_url],
        "minzoom": min(minzooms) if minzooms else 0,
        "maxzoom": max(maxzooms) if maxzooms else 14,
        "bounds": bounds,
        "center": center_from_bounds(bounds) if bounds else manifest.get("center"),
        "attribution": "Tavrix PMTiles",
        "description": "Vector tiles rendered from Tavrix PMTiles. Region-specific URLs are recommended for production and CDN caching.",
        "vector_layers": vector_layers_for_tilesets(selected_tilesets),
        "region": region,
        "tileset": tileset_id,
    }


def coverage_for_region(region: str) -> dict:
    manifest = load_manifest(region)
    metadata = tilesets_for_region(region)
    bounds = union_bounds([item["bounds"] for item in metadata if item.get("bounds")])
    minzooms = [item["minzoom"] for item in metadata]
    maxzooms = [item["maxzoom"] for item in metadata]
    return {
        "region": region,
        "bounds": bounds,
        "center": center_from_bounds(bounds) if bounds else region_center(region),
        "minzoom": min(minzooms) if minzooms else None,
        "maxzoom": max(maxzooms) if maxzooms else None,
        "tilesets": metadata,
        "last_updated": manifest.get("last_updated"),
    }


def cache_warm(payload: dict | None = None) -> dict:
    payload = payload or {}
    regions = payload.get("regions") or sorted(manifest_region_ids())
    styles = payload.get("styles") or [item["id"] for item in list_styles()]
    warmed = {"regions": 0, "manifests": 0, "styles": 0}
    load_regions()
    warmed["regions"] = 1
    for region in regions:
        load_manifest(safe_id(str(region)))
        warmed["manifests"] += 1
    for style_id in styles:
        load_style_template(safe_id(str(style_id)))
        warmed["styles"] += 1
    return {"ok": True, "warmed": warmed, "cache": _cache.snapshot()}


def validation_issue(code: str, message: str, path: str) -> dict[str, str]:
    return {"code": code, "message": message, "path": path}


def validate_style_document(style: Any) -> dict:
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if not isinstance(style, dict):
        return {
            "valid": False,
            "errors": [validation_issue("invalid_body", "Style body must be a JSON object.", "$")],
            "warnings": [],
        }
    if style.get("version") != 8:
        errors.append(validation_issue("invalid_version", "version must be 8.", "$.version"))
    sources = style.get("sources")
    if not isinstance(sources, dict) or not sources:
        errors.append(validation_issue("missing_sources", "sources must be a non-empty object.", "$.sources"))
        sources = {}
    layers = style.get("layers")
    if not isinstance(layers, list):
        errors.append(validation_issue("invalid_layers", "layers must be an array.", "$.layers"))
        layers = []
    for index, layer in enumerate(layers):
        layer_path = f"$.layers[{index}]"
        if not isinstance(layer, dict):
            errors.append(validation_issue("invalid_layer", f"layers[{index}] must be an object.", layer_path))
            continue
        if not layer.get("id"):
            errors.append(validation_issue("missing_layer_id", f"layers[{index}] is missing id.", f"{layer_path}.id"))
        if not layer.get("type"):
            errors.append(validation_issue("missing_layer_type", f"layers[{index}] is missing type.", f"{layer_path}.type"))
        source = layer.get("source")
        if source and source not in sources:
            errors.append(
                validation_issue(
                    "missing_source_reference",
                    f"Layer '{layer.get('id', index)}' references missing source '{source}'.",
                    f"{layer_path}.source",
                )
            )
            continue
        source_def = sources.get(source) if source else None
        if (
            isinstance(source_def, dict)
            and source_def.get("type") == "vector"
            and layer.get("type") != "background"
            and not layer.get("source-layer")
        ):
            errors.append(
                validation_issue(
                    "missing_source_layer",
                    f"Layer '{layer.get('id', index)}' is missing source-layer for vector source '{source}'.",
                    f"{layer_path}.source-layer",
                )
            )
    has_text = style_has_text({"layers": layers})
    has_icons = style_has_icons({"layers": layers})
    if has_text and not style.get("glyphs"):
        errors.append(validation_issue("missing_glyphs", "Style uses text layers but glyphs URL is missing.", "$.glyphs"))
    if has_icons and not style.get("sprite"):
        errors.append(validation_issue("missing_sprite", "Style uses icon-image but sprite URL is missing.", "$.sprite"))
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def local_glyph_path(fontstack: str, glyph_range: str) -> Path:
    fontstack = safe_asset_segment(fontstack, "fontstack")
    glyph_range = safe_asset_segment(glyph_range.removesuffix(".pbf"), "glyph range")
    if not re.fullmatch(r"\d+-\d+", glyph_range):
        raise APIError("invalid_request", "Glyph range must look like 0-255", HTTPStatus.BAD_REQUEST)
    return GLYPHS_DIR / fontstack / f"{glyph_range}.pbf"


def local_sprite_path(style_id: str, filename: str) -> Path:
    style_id = safe_id(style_id)
    if not style_path(style_id).exists():
        raise APIError("not_found", f"Style not found: {style_id}", HTTPStatus.NOT_FOUND)
    filename = safe_asset_segment(filename, "sprite filename")
    return SPRITES_DIR / style_id / filename


def dependency_checks() -> list[dict]:
    checks = [
        {"name": "manifests_dir", "ok": (OUTPUT_DIR / "manifests").exists(), "path": str(OUTPUT_DIR / "manifests")},
        {"name": "styles_dir", "ok": STYLES_DIR.exists(), "path": str(STYLES_DIR)},
    ]
    pmtiles = []
    for item in all_tilesets():
        pmtiles.append({"region": item["region"], "tileset": item["id"], "path": item["path"], "ok": item["exists"]})
    checks.append({"name": "pmtiles_files", "ok": bool(pmtiles) and all(item["ok"] for item in pmtiles), "files": pmtiles})
    styles = []
    if STYLES_DIR.exists():
        for path in sorted(STYLES_DIR.glob("*.json")):
            try:
                styles.append(read_json(path))
            except json.JSONDecodeError:
                checks.append({"name": f"style:{path.stem}", "ok": False, "path": str(path), "error": "Invalid JSON"})
    glyphs_required = any(style_has_text(style) for style in styles)
    if glyphs_required:
        checks.append({"name": "glyphs_dir", "ok": GLYPHS_DIR.exists(), "path": str(GLYPHS_DIR)})
    sprites_required = any(style_has_icons(style) or style.get("sprite") for style in styles)
    if sprites_required:
        checks.append({"name": "sprites_dir", "ok": SPRITES_DIR.exists(), "path": str(SPRITES_DIR)})
    return checks


def health_details() -> dict:
    raw_checks = dependency_checks()
    checks: dict[str, bool] = {
        "manifests_dir": False,
        "styles_dir": False,
        "pmtiles_files": False,
        "glyphs_dir": True,
        "sprites_dir": True,
        "default_region": False,
        "default_style": False,
        "cache": True,
    }
    missing: list[str] = []
    warnings: list[str] = []
    for item in raw_checks:
        name = str(item.get("name"))
        ok = bool(item.get("ok"))
        checks[name] = ok
        if not ok:
            missing.append(name)
    manifests = manifest_region_ids()
    checks["default_region"] = DEFAULT_REGION in manifests
    if not checks["default_region"]:
        warnings.append(f"Default region '{DEFAULT_REGION}' has no manifest.")
    checks["default_style"] = style_path(DEFAULT_STYLE).exists()
    if not checks["default_style"]:
        warnings.append(f"Default style '{DEFAULT_STYLE}' was not found.")
    missing = sorted({name for name, value in checks.items() if not value} | set(missing))
    ok = all(checks.values())
    return {"ok": ok, "checks": checks, "missing": missing, "warnings": warnings}


# ─── OpenAPI spec ─────────────────────────────────────────────────────────────

def _json_response(schema_ref: dict, description: str = "OK") -> dict:
    return {
        "description": description,
        "content": {"application/json": {"schema": schema_ref}},
    }


def _openapi_query_params() -> dict[str, dict]:
    return {
        "style": {
            "name": "style",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Style id, for example light, dark, navigation, 3d-light, or 3d-dark.",
        },
        "lon": {
            "name": "lon",
            "in": "query",
            "required": False,
            "schema": {"type": "number", "format": "double"},
            "description": "Viewport center longitude. Used by auto region resolution.",
        },
        "lat": {
            "name": "lat",
            "in": "query",
            "required": False,
            "schema": {"type": "number", "format": "double"},
            "description": "Viewport center latitude. Used by auto region resolution.",
        },
        "zoom": {
            "name": "zoom",
            "in": "query",
            "required": False,
            "schema": {"type": "number", "format": "double"},
            "description": "Current map zoom. Returned styles use it as the initial zoom when supplied.",
        },
        "bbox": {
            "name": "bbox",
            "in": "query",
            "required": False,
            "schema": {
                "type": "string",
                "pattern": r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$",
                "example": "38.79,29.06,48.62,37.39",
            },
            "description": "Viewport bounds as west,south,east,north. Can resolve multiple regions.",
        },
        "region": {
            "name": "region",
            "in": "path",
            "required": True,
            "schema": {"type": "string", "example": "iraq"},
            "description": "Region id matching a manifest file in output/manifests.",
        },
    }


def _openapi_components() -> dict:
    return {
        "schemas": {
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
                    "cache_ttl_seconds": {"type": "integer"},
                    "max_threads": {"type": "integer"},
                },
            },
            "MessageResponse": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}, "message": {"type": "string"}},
            },
            "StyleSummary": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "layer_count": {"type": "integer"},
                    "has_labels": {"type": "boolean"},
                },
            },
            "StylesResponse": {
                "type": "object",
                "properties": {
                    "styles": {"type": "array", "items": {"$ref": "#/components/schemas/StyleSummary"}},
                    "default_style": {"type": "string"},
                },
            },
            "RegionSummary": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "has_manifest": {"type": "boolean"},
                    "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "center": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                },
                "additionalProperties": True,
            },
            "RegionsResponse": {
                "type": "object",
                "properties": {
                    "regions": {"type": "array", "items": {"$ref": "#/components/schemas/RegionSummary"}}
                },
            },
            "ResolveResponse": {
                "type": "object",
                "properties": {
                    "region": {"type": "string"},
                    "regions": {"type": "array", "items": {"type": "string"}},
                    "manifest_url": {"type": "string"},
                },
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
                "additionalProperties": True,
            },
            "Manifest": {
                "type": "object",
                "properties": {
                    "schema_version": {"type": "integer"},
                    "region": {"type": "string"},
                    "tilesets": {
                        "type": "object",
                        "additionalProperties": {"$ref": "#/components/schemas/Tileset"},
                    },
                    "environment": {"type": "string"},
                    "last_updated": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "RasterTileJson": {
                "type": "object",
                "properties": {
                    "tilejson": {"type": "string", "example": "3.0.0"},
                    "name": {"type": "string"},
                    "scheme": {"type": "string", "example": "xyz"},
                    "tiles": {"type": "array", "items": {"type": "string"}},
                    "minzoom": {"type": "integer"},
                    "maxzoom": {"type": "integer", "example": MAX_RASTER_ZOOM},
                    "bounds": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                    "format": {
                        "type": "string",
                        "enum": ["png", "webp", "jpg", "jpeg"],
                        "example": "png",
                    },
                    "region": {"type": "string"},
                    "style": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "MapLibreStyle": {
                "type": "object",
                "description": "MapLibre style specification with API-injected PMTiles sources and metadata.",
                "properties": {
                    "version": {"type": "integer", "example": 8},
                    "name": {"type": "string"},
                    "glyphs": {"type": "string"},
                    "sources": {"type": "object"},
                    "layers": {"type": "array", "items": {"type": "object"}},
                    "center": {"type": "array", "items": {"type": "number"}},
                    "zoom": {"type": "number"},
                    "pitch": {"type": "number"},
                    "bearing": {"type": "number"},
                    "metadata": {"type": "object"},
                },
                "additionalProperties": True,
            },
            "ErrorResponse": {
                "type": "object",
                "properties": {"error": {"type": "string"}},
            },
        }
    }


def openapi_spec() -> dict:
    # (kept identical to original, omitted here for brevity — paste original)
    p = _openapi_query_params()
    ok = _json_response
    z_param = {
        "name": "z",
        "in": "path",
        "required": True,
        "schema": {"type": "integer", "minimum": 0, "maximum": MAX_RASTER_ZOOM},
        "description": "XYZ zoom level.",
    }
    x_param = {
        "name": "x",
        "in": "path",
        "required": True,
        "schema": {"type": "integer", "minimum": 0},
        "description": "XYZ tile column.",
    }
    y_param = {
        "name": "y",
        "in": "path",
        "required": True,
        "schema": {"type": "integer", "minimum": 0},
        "description": "XYZ tile row.",
    }
    region_query_param = {
        "name": "region",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "example": "iraq"},
        "description": "Optional fixed region id.",
    }
    format_param = {
        "name": "format",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "enum": ["png", "webp", "jpg", "jpeg"], "default": "png"},
        "description": "Raster image output format.",
    }
    format_query_param = {
        "name": "format",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["png", "webp", "jpg", "jpeg"], "default": "png"},
        "description": "Raster image output format used in the TileJSON tile URL.",
    }
    raster_image_content = {
        "image/png": {"schema": {"type": "string", "format": "binary"}},
        "image/webp": {"schema": {"type": "string", "format": "binary"}},
        "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "PMTiles Map API",
            "version": "1.0.0",
            "description": "Local API for serving MapLibre styles, PMTiles manifests, and region resolution.",
        },
        "servers": [{"url": "/", "description": "Current host"}],
        "tags": [
            {"name": "Docs"},
            {"name": "System"},
            {"name": "Styles"},
            {"name": "Regions"},
            {"name": "Manifests"},
            {"name": "Raster Tiles"},
        ],
        "paths": {
            "/docs": {
                "get": {
                    "tags": ["Docs"],
                    "summary": "Swagger UI",
                    "operationId": "getSwaggerUi",
                    "responses": {
                        "200": {
                            "description": "Swagger UI HTML",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/api/openapi.json": {
                "get": {
                    "tags": ["Docs"],
                    "summary": "OpenAPI document",
                    "operationId": "getOpenApiSpec",
                    "responses": {"200": ok({"type": "object"})},
                }
            },
            "/api/health": {
                "get": {
                    "tags": ["System"],
                    "summary": "Health check",
                    "operationId": "getHealth",
                    "responses": {"200": ok({"$ref": "#/components/schemas/HealthResponse"})},
                }
            },
            "/api/cache/clear": {
                "get": {
                    "tags": ["System"],
                    "summary": "Clear in-memory API cache",
                    "operationId": "clearCache",
                    "responses": {"200": ok({"$ref": "#/components/schemas/MessageResponse"})},
                }
            },
            "/api/styles": {
                "get": {
                    "tags": ["Styles"],
                    "summary": "List available map styles",
                    "operationId": "listStyles",
                    "responses": {"200": ok({"$ref": "#/components/schemas/StylesResponse"})},
                }
            },
            "/api/raster/tilejson.json": {
                "get": {
                    "tags": ["Raster Tiles"],
                    "summary": "Get TileJSON for the raster tiles endpoint",
                    "operationId": "getRasterTileJson",
                    "parameters": [region_query_param, p["style"], format_query_param],
                    "responses": {"200": ok({"$ref": "#/components/schemas/RasterTileJson"})},
                }
            },
            "/api/raster/{z}/{x}/{y}.{format}": {
                "get": {
                    "tags": ["Raster Tiles"],
                    "summary": "Render a raster image tile",
                    "description": "Renders a 256x256 raster tile from local PMTiles. If region is omitted, the API resolves it from the tile center.",
                    "operationId": "getRasterTile",
                    "parameters": [
                        z_param,
                        x_param,
                        y_param,
                        format_param,
                        {
                            "name": "region",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "example": "iraq"},
                            "description": "Optional fixed region id. If omitted, region is resolved from the tile center.",
                        },
                        p["style"],
                    ],
                    "responses": {
                        "200": {
                            "description": "Raster image tile",
                            "content": raster_image_content,
                        },
                        "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid tile request"),
                        "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Tile or region not found"),
                    },
                }
            },
            "/api/raster/{region}/{z}/{x}/{y}.{format}": {
                "get": {
                    "tags": ["Raster Tiles"],
                    "summary": "Render a raster image tile for one region",
                    "operationId": "getRegionRasterTile",
                    "parameters": [p["region"], z_param, x_param, y_param, format_param, p["style"]],
                    "responses": {
                        "200": {
                            "description": "Raster image tile",
                            "content": raster_image_content,
                        },
                        "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid tile request"),
                        "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Tile or region not found"),
                    },
                }
            },
            "/api/style.json": {
                "get": {
                    "tags": ["Styles"],
                    "summary": "Build an auto-resolved MapLibre style",
                    "description": "Chooses region(s) from lon/lat or bbox and injects PMTiles sources into the selected style.",
                    "operationId": "getAutoStyle",
                    "parameters": [p["style"], p["lon"], p["lat"], p["zoom"], p["bbox"]],
                    "responses": {"200": ok({"$ref": "#/components/schemas/MapLibreStyle"})},
                }
            },
            "/api/style/{region}.json": {
                "get": {
                    "tags": ["Styles"],
                    "summary": "Build a MapLibre style for one region",
                    "operationId": "getRegionStyle",
                    "parameters": [p["region"], p["style"]],
                    "responses": {
                        "200": ok({"$ref": "#/components/schemas/MapLibreStyle"}),
                        "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Manifest or style not found"),
                    },
                }
            },
            "/api/regions": {
                "get": {
                    "tags": ["Regions"],
                    "summary": "List configured regions",
                    "operationId": "listRegions",
                    "responses": {"200": ok({"$ref": "#/components/schemas/RegionsResponse"})},
                }
            },
            "/api/resolve": {
                "get": {
                    "tags": ["Regions"],
                    "summary": "Resolve viewport to region ids",
                    "operationId": "resolveRegions",
                    "parameters": [p["lon"], p["lat"], p["bbox"]],
                    "responses": {"200": ok({"$ref": "#/components/schemas/ResolveResponse"})},
                }
            },
            "/api/manifest.json": {
                "get": {
                    "tags": ["Manifests"],
                    "summary": "Get auto-resolved PMTiles manifest",
                    "operationId": "getAutoManifest",
                    "parameters": [p["lon"], p["lat"], p["bbox"]],
                    "responses": {"200": ok({"$ref": "#/components/schemas/Manifest"})},
                }
            },
            "/api/manifest/{region}.json": {
                "get": {
                    "tags": ["Manifests"],
                    "summary": "Get PMTiles manifest for one region",
                    "operationId": "getRegionManifest",
                    "parameters": [p["region"]],
                    "responses": {
                        "200": ok({"$ref": "#/components/schemas/Manifest"}),
                        "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Manifest not found"),
                    },
                }
            },
        },
        "components": _openapi_components(),
    }


def openapi_spec() -> dict:
    ok = _json_response
    p = _openapi_query_params()
    z_param = {"name": "z", "in": "path", "required": True, "schema": {"type": "integer", "minimum": 0, "maximum": MAX_RASTER_ZOOM}}
    x_param = {"name": "x", "in": "path", "required": True, "schema": {"type": "integer", "minimum": 0}}
    y_param = {"name": "y", "in": "path", "required": True, "schema": {"type": "integer", "minimum": 0}}
    region_path = p["region"]
    tileset_path_param = {"name": "tileset", "in": "path", "required": True, "schema": {"type": "string", "example": "basemap"}}
    tileset_query = {"name": "tileset", "in": "query", "required": False, "schema": {"type": "string", "example": "basemap"}}
    style_path_param = {"name": "style_id", "in": "path", "required": True, "schema": {"type": "string", "example": "light"}}
    internal_security = [{"internalToken": []}, {"bearerInternalToken": []}]
    raster_format = {
        "name": "format",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "enum": ["png", "webp", "jpg", "jpeg"], "default": "png"},
    }
    raster_format_query = {
        "name": "format",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["png", "webp", "jpg", "jpeg"], "default": "png"},
    }
    region_query = {"name": "region", "in": "query", "required": False, "schema": {"type": "string", "example": "iraq"}}
    cache_response_headers = {
        "Cache-Control": {"schema": {"type": "string"}, "description": "Public cache policy for this response."},
        "ETag": {"schema": {"type": "string"}, "description": "Entity tag for conditional requests."},
    }
    vector_response_headers = {
        **cache_response_headers,
        "Content-Encoding": {
            "schema": {"type": "string"},
            "description": "gzip when the stored vector tile payload is gzip-compressed.",
        },
    }
    def cached_json(schema: dict, description: str = "OK") -> dict:
        response = ok(schema, description)
        response["headers"] = cache_response_headers
        return response

    vector_tile = {
        "200": {
            "description": "Vector tile. Cache-Control: public, max-age=31536000, immutable.",
            "headers": vector_response_headers,
            "content": {VECTOR_TILE_CONTENT_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        },
        "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid tile request"),
        "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Tile, region, or tileset not found"),
    }
    raster_tile = {
        "200": {
            "description": "Raster image tile. Cache-Control: public, max-age=31536000, immutable.",
            "headers": cache_response_headers,
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid tile request"),
        "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Tile or region not found"),
    }
    components = _openapi_components()
    components["schemas"].update({
        "ValidationIssue": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "example": "missing_glyphs"},
                "message": {"type": "string", "example": "Style uses text layers but glyphs URL is missing."},
                "path": {"type": "string", "example": "$.glyphs"},
            },
            "required": ["code", "message", "path"],
        },
        "ErrorResponse": {
            "type": "object",
            "properties": {
                "error": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "example": "not_found"},
                        "status": {"type": "integer", "example": 404},
                        "message": {"type": "string", "example": "Tile not found"},
                        "request_id": {"type": "string"},
                        "details": {"type": "object"},
                    },
                    "required": ["status", "code", "message", "request_id", "details"],
                }
            },
            "required": ["error"],
        },
        "CacheStatus": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "cache": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "ttl_seconds": {"type": "integer"},
                        "items": {"type": "integer"},
                        "size_bytes": {"type": "integer"},
                        "hits": {"type": "integer"},
                        "misses": {"type": "integer"},
                        "hit_rate": {"type": "number"},
                    },
                    "required": ["enabled", "ttl_seconds", "items", "size_bytes", "hits", "misses", "hit_rate"],
                },
            },
            "required": ["ok", "cache"],
        },
        "Coverage": {
            "type": "object",
            "properties": {
                "region": {"type": "string"},
                "bounds": {"type": "array", "items": {"type": "number"}},
                "center": {"type": "array", "items": {"type": "number"}},
                "minzoom": {"type": "integer", "nullable": True},
                "maxzoom": {"type": "integer", "nullable": True},
                "tilesets": {"type": "array", "items": {"$ref": "#/components/schemas/Tileset"}},
                "last_updated": {"type": "string", "nullable": True},
            },
            "required": ["region", "bounds", "center", "minzoom", "maxzoom", "tilesets", "last_updated"],
        },
        "CoverageResponse": {
            "type": "object",
            "properties": {
                "bounds": {"type": "array", "items": {"type": "number"}, "nullable": True},
                "regions": {"type": "array", "items": {"$ref": "#/components/schemas/Coverage"}},
                "minzoom": {"type": "integer", "nullable": True},
                "maxzoom": {"type": "integer", "nullable": True},
                "last_updated": {"type": "string", "nullable": True},
            },
            "required": ["bounds", "regions", "minzoom", "maxzoom", "last_updated"],
        },
        "HealthDetailed": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "checks": {
                    "type": "object",
                    "properties": {
                        "manifests_dir": {"type": "boolean"},
                        "styles_dir": {"type": "boolean"},
                        "pmtiles_files": {"type": "boolean"},
                        "glyphs_dir": {"type": "boolean"},
                        "sprites_dir": {"type": "boolean"},
                        "default_region": {"type": "boolean"},
                        "default_style": {"type": "boolean"},
                        "cache": {"type": "boolean"},
                    },
                    "required": [
                        "manifests_dir",
                        "styles_dir",
                        "pmtiles_files",
                        "glyphs_dir",
                        "sprites_dir",
                        "default_region",
                        "default_style",
                        "cache",
                    ],
                    "additionalProperties": {"type": "boolean"},
                },
                "missing": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ok", "checks", "missing", "warnings"],
        },
        "StyleMetadata": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "layer_count": {"type": "integer"},
                "has_labels": {"type": "boolean"},
                "supports_3d": {"type": "boolean"},
                "minzoom": {"type": "number"},
                "maxzoom": {"type": "number"},
                "pitch": {"type": "number", "nullable": True},
                "bearing": {"type": "number", "nullable": True},
                "default_pitch": {"type": "number"},
                "default_bearing": {"type": "number"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "tilesets": {"type": "array", "items": {"type": "string"}},
                "glyphs_required": {"type": "boolean"},
                "sprite_required": {"type": "boolean"},
                "glyphs": {"type": "string", "nullable": True},
                "sprite": {"type": "string", "nullable": True},
            },
            "required": [
                "id",
                "name",
                "layer_count",
                "has_labels",
                "supports_3d",
                "minzoom",
                "maxzoom",
                "default_pitch",
                "default_bearing",
                "sources",
                "tilesets",
                "glyphs_required",
                "sprite_required",
            ],
        },
        "StyleValidation": {
            "type": "object",
            "properties": {
                "valid": {"type": "boolean"},
                "errors": {"type": "array", "items": {"$ref": "#/components/schemas/ValidationIssue"}},
                "warnings": {"type": "array", "items": {"$ref": "#/components/schemas/ValidationIssue"}},
            },
            "required": ["valid", "errors", "warnings"],
        },
        "TileInspect": {
            "type": "object",
            "properties": {
                "exists": {"type": "boolean"},
                "region": {"type": "string"},
                "z": {"type": "integer"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "source": {"type": "string"},
                "tileset": {"type": "string"},
                "size_bytes": {"type": "integer"},
                "content_type": {"type": "string"},
                "content_encoding": {"type": "string", "nullable": True},
                "etag": {"type": "string"},
                "cache_status": {"type": "string", "enum": ["hit", "miss"]},
            },
            "required": [
                "exists",
                "region",
                "z",
                "x",
                "y",
                "source",
                "tileset",
                "size_bytes",
                "content_type",
                "content_encoding",
                "etag",
                "cache_status",
            ],
        },
        "TilesetsResponse": {
            "type": "object",
            "properties": {"tilesets": {"type": "array", "items": {"$ref": "#/components/schemas/Tileset"}}},
        },
        "Tileset": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "region": {"type": "string"},
                "url": {"type": "string", "nullable": True},
                "key": {"type": "string", "nullable": True},
                "filename": {"type": "string"},
                "minzoom": {"type": "integer"},
                "maxzoom": {"type": "integer"},
                "bounds": {"type": "array", "items": {"type": "number"}, "nullable": True},
                "center": {"type": "array", "items": {"type": "number"}, "nullable": True},
                "vector_layers": {"type": "array", "items": {"type": "string"}},
                "format": {"type": "string", "example": "mvt"},
                "tile_count": {"type": "integer", "nullable": True},
                "last_updated": {"type": "string", "nullable": True},
                "etag": {"type": "string", "nullable": True},
                "sha256": {"type": "string", "nullable": True},
                "size_bytes": {"type": "integer"},
            },
            "required": [
                "id",
                "region",
                "url",
                "key",
                "filename",
                "minzoom",
                "maxzoom",
                "bounds",
                "center",
                "vector_layers",
                "format",
                "tile_count",
                "last_updated",
                "etag",
                "sha256",
                "size_bytes",
            ],
        },
        "VectorTileJson": {
            "type": "object",
            "properties": {
                "tilejson": {"type": "string", "example": "3.0.0"},
                "name": {"type": "string"},
                "scheme": {"type": "string", "example": "xyz"},
                "tiles": {"type": "array", "items": {"type": "string"}},
                "minzoom": {"type": "integer"},
                "maxzoom": {"type": "integer"},
                "bounds": {"type": "array", "items": {"type": "number"}},
                "center": {"type": "array", "items": {"type": "number"}},
                "attribution": {"type": "string"},
                "description": {"type": "string"},
                "tileset": {"type": "string", "nullable": True},
                "vector_layers": {"type": "array", "items": {"type": "object"}},
                "region": {"type": "string"},
            },
            "required": [
                "tilejson",
                "name",
                "scheme",
                "tiles",
                "minzoom",
                "maxzoom",
                "bounds",
                "center",
                "attribution",
                "description",
                "tileset",
                "vector_layers",
                "region",
            ],
        },
    })
    components["securitySchemes"] = {
        "internalToken": {"type": "apiKey", "in": "header", "name": "x-internal-token"},
        "bearerInternalToken": {"type": "http", "scheme": "bearer"},
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Tavrix Internal Map Engine API",
            "version": "2.1.0",
            "description": "Internal PMTiles/MapLibre engine for styles, manifests, vector tiles, raster tiles, glyphs, sprites, metadata, coverage, cache, and health checks.",
        },
        "servers": [{"url": "/", "description": "Current host"}],
        "tags": [
            {"name": "Docs"}, {"name": "System"}, {"name": "Health"}, {"name": "Cache"},
            {"name": "Styles"}, {"name": "Regions"}, {"name": "Manifests"},
            {"name": "Vector Tiles"}, {"name": "Raster Tiles"}, {"name": "Assets"},
            {"name": "Tilesets"}, {"name": "Coverage"}, {"name": "Debug"},
        ],
        "paths": {
            "/docs": {"get": {"tags": ["Docs"], "summary": "Swagger UI", "responses": {"200": {"description": "HTML"}}}},
            "/api/openapi.json": {"get": {"tags": ["Docs"], "summary": "OpenAPI document", "responses": {"200": ok({"type": "object"})}}},
            "/api/health": {"get": {"tags": ["Health"], "summary": "Basic health", "responses": {"200": ok({"$ref": "#/components/schemas/HealthResponse"})}}},
            "/api/health/live": {"get": {"tags": ["Health"], "summary": "Liveness check", "responses": {"200": ok({"type": "object"}, "Live")}}},
            "/api/health/ready": {"get": {"tags": ["Health"], "summary": "Readiness check", "responses": {"200": ok({"$ref": "#/components/schemas/HealthDetailed"}, "Ready"), "503": ok({"$ref": "#/components/schemas/HealthDetailed"}, "Not ready")}}},
            "/api/health/dependencies": {"get": {"tags": ["Health"], "summary": "Dependency checks", "security": internal_security, "responses": {"200": ok({"$ref": "#/components/schemas/HealthDetailed"}), "401": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Unauthorized")}}},
            "/api/cache/status": {"get": {"tags": ["Cache"], "summary": "Cache status", "responses": {"200": ok({"$ref": "#/components/schemas/CacheStatus"})}}},
            "/api/cache/clear": {
                "get": {"tags": ["Cache"], "summary": "Clear cache (legacy GET)", "security": internal_security, "responses": {"200": ok({"$ref": "#/components/schemas/MessageResponse"}), "401": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Unauthorized")}},
                "post": {"tags": ["Cache"], "summary": "Clear cache", "security": internal_security, "responses": {"200": ok({"$ref": "#/components/schemas/MessageResponse"}), "401": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Unauthorized")}},
            },
            "/api/cache/warm": {"post": {"tags": ["Cache"], "summary": "Warm cache", "security": internal_security, "requestBody": {"required": False, "content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"200": ok({"type": "object"}), "401": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Unauthorized")}}},
            "/api/styles": {"get": {"tags": ["Styles"], "summary": "List styles", "responses": {"200": ok({"$ref": "#/components/schemas/StylesResponse"})}}},
            "/api/styles/{style_id}": {"get": {"tags": ["Styles"], "summary": "Style metadata", "parameters": [style_path_param], "responses": {"200": ok({"$ref": "#/components/schemas/StyleMetadata"}, "Style metadata. Cache-Control: public, max-age=300."), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Style not found")}}},
            "/api/styles/validate": {"post": {"tags": ["Styles"], "summary": "Validate a MapLibre style", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"200": ok({"$ref": "#/components/schemas/StyleValidation"})}}},
            "/api/style.json": {"get": {"tags": ["Styles"], "summary": "Auto-resolved MapLibre style", "description": "Returns style.json with Cache-Control: public, max-age=300 and ETag.", "parameters": [p["style"], p["lon"], p["lat"], p["zoom"], p["bbox"]], "responses": {"200": cached_json({"$ref": "#/components/schemas/MapLibreStyle"}, "MapLibre style JSON. Cache-Control: public, max-age=300.")}}},
            "/api/style/{region}.json": {"get": {"tags": ["Styles"], "summary": "Region MapLibre style", "description": "Returns style.json with Cache-Control: public, max-age=300 and ETag.", "parameters": [region_path, p["style"]], "responses": {"200": cached_json({"$ref": "#/components/schemas/MapLibreStyle"}, "MapLibre style JSON. Cache-Control: public, max-age=300."), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Manifest or style not found")}}},
            "/api/regions": {"get": {"tags": ["Regions"], "summary": "List regions", "responses": {"200": ok({"$ref": "#/components/schemas/RegionsResponse"})}}},
            "/api/resolve": {"get": {"tags": ["Regions"], "summary": "Resolve viewport to regions", "parameters": [p["lon"], p["lat"], p["bbox"]], "responses": {"200": ok({"$ref": "#/components/schemas/ResolveResponse"})}}},
            "/api/manifest.json": {"get": {"tags": ["Manifests"], "summary": "Auto-resolved manifest", "description": "Returns manifest JSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [p["lon"], p["lat"], p["bbox"]], "responses": {"200": cached_json({"$ref": "#/components/schemas/Manifest"}, "Manifest JSON. Cache-Control: public, max-age=3600.")}}},
            "/api/manifest/{region}.json": {"get": {"tags": ["Manifests"], "summary": "Region manifest", "description": "Returns manifest JSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [region_path], "responses": {"200": cached_json({"$ref": "#/components/schemas/Manifest"}, "Manifest JSON. Cache-Control: public, max-age=3600."), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Manifest not found")}}},
            "/api/vector/tilejson.json": {"get": {"tags": ["Vector Tiles"], "summary": "Vector TileJSON", "description": "Returns TileJSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [region_query, tileset_query], "responses": {"200": cached_json({"$ref": "#/components/schemas/VectorTileJson"}, "Vector TileJSON. Cache-Control: public, max-age=3600.")}}},
            "/api/vector/{region}/tilejson.json": {"get": {"tags": ["Vector Tiles"], "summary": "Region Vector TileJSON", "description": "Returns TileJSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [region_path, tileset_query], "responses": {"200": cached_json({"$ref": "#/components/schemas/VectorTileJson"}, "Vector TileJSON. Cache-Control: public, max-age=3600.")}}},
            "/api/vector/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Auto-resolved vector tile", "description": "Convenient endpoint that resolves region from the tile center. Region-specific endpoints are recommended for production and CDN caching. Cache-Control: public, max-age=31536000, immutable; ETag is returned; Content-Encoding is gzip when applicable.", "parameters": [z_param, x_param, y_param, region_query], "responses": vector_tile}},
            "/api/vector/{region}/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Region vector tile", "parameters": [region_path, z_param, x_param, y_param], "responses": vector_tile}},
            "/api/vector/{tileset}/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Tileset vector tile for default/query region", "parameters": [tileset_path_param, z_param, x_param, y_param, region_query], "responses": vector_tile}},
            "/api/vector/{region}/{tileset}/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Region tileset vector tile", "parameters": [region_path, tileset_path_param, z_param, x_param, y_param], "responses": vector_tile}},
            "/api/raster/tilejson.json": {"get": {"tags": ["Raster Tiles"], "summary": "Raster TileJSON", "description": "Returns TileJSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [region_query, p["style"], raster_format_query], "responses": {"200": cached_json({"$ref": "#/components/schemas/RasterTileJson"}, "Raster TileJSON. Cache-Control: public, max-age=3600.")}}},
            "/api/raster/{z}/{x}/{y}.{format}": {"get": {"tags": ["Raster Tiles"], "summary": "Auto-resolved raster tile", "description": "Convenient endpoint that resolves region from the tile center. Region-specific endpoints are recommended for production and CDN caching. Cache-Control: public, max-age=31536000, immutable; ETag is returned.", "parameters": [z_param, x_param, y_param, raster_format, region_query, p["style"]], "responses": raster_tile}},
            "/api/raster/{region}/{z}/{x}/{y}.{format}": {"get": {"tags": ["Raster Tiles"], "summary": "Region raster tile", "parameters": [region_path, z_param, x_param, y_param, raster_format, p["style"]], "responses": raster_tile}},
            "/api/fonts/{fontstack}/{range}.pbf": {"get": {"tags": ["Assets"], "summary": "Font glyph PBF", "description": "Returns glyph PBF with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "fontstack", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "range", "in": "path", "required": True, "schema": {"type": "string", "example": "0-255"}}], "responses": {"200": {"description": "Glyph PBF. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"application/x-protobuf": {"schema": {"type": "string", "format": "binary"}}}}, "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid glyph request"), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Glyph not found")}}},
            "/api/glyphs/{fontstack}/{range}.pbf": {"get": {"tags": ["Assets"], "summary": "Glyph PBF alias", "description": "Returns glyph PBF with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "fontstack", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "range", "in": "path", "required": True, "schema": {"type": "string", "example": "0-255"}}], "responses": {"200": {"description": "Glyph PBF. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"application/x-protobuf": {"schema": {"type": "string", "format": "binary"}}}}, "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid glyph request"), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Glyph not found")}}},
            "/api/sprites/{style}/sprite.json": {"get": {"tags": ["Assets"], "summary": "Sprite JSON", "description": "Returns sprite JSON with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": cached_json({"type": "object"}, "Sprite JSON. Cache-Control: public, max-age=31536000, immutable."), "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid sprite request"), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Sprite or style not found")}}},
            "/api/sprites/{style}/sprite.png": {"get": {"tags": ["Assets"], "summary": "Sprite PNG", "description": "Returns sprite PNG with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Sprite PNG. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}, "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid sprite request"), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Sprite or style not found")}}},
            "/api/sprites/{style}/sprite@2x.json": {"get": {"tags": ["Assets"], "summary": "Retina sprite JSON", "description": "Returns sprite JSON with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": cached_json({"type": "object"}, "Sprite JSON. Cache-Control: public, max-age=31536000, immutable."), "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid sprite request"), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Sprite or style not found")}}},
            "/api/sprites/{style}/sprite@2x.png": {"get": {"tags": ["Assets"], "summary": "Retina sprite PNG", "description": "Returns sprite PNG with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Sprite PNG. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}, "400": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Invalid sprite request"), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Sprite or style not found")}}},
            "/api/tilesets": {"get": {"tags": ["Tilesets"], "summary": "List tilesets", "responses": {"200": ok({"$ref": "#/components/schemas/TilesetsResponse"})}}},
            "/api/tilesets/{tileset_id}": {"get": {"tags": ["Tilesets"], "summary": "Tileset metadata across regions", "parameters": [{"name": "tileset_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": ok({"$ref": "#/components/schemas/TilesetsResponse"})}}},
            "/api/tilesets/{region}/{tileset_id}": {"get": {"tags": ["Tilesets"], "summary": "Region tileset metadata", "parameters": [region_path, {"name": "tileset_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": ok({"$ref": "#/components/schemas/Tileset"}), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Tileset not found")}}},
            "/api/tiles/inspect/{region}/{z}/{x}/{y}": {"get": {"tags": ["Debug"], "summary": "Inspect tile availability", "security": internal_security, "parameters": [region_path, z_param, x_param, y_param, tileset_query], "responses": {"200": ok({"$ref": "#/components/schemas/TileInspect"}), "401": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Unauthorized"), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Tile not found")}}},
            "/api/coverage": {"get": {"tags": ["Coverage"], "summary": "All coverage", "responses": {"200": ok({"$ref": "#/components/schemas/CoverageResponse"})}}},
            "/api/coverage/{region}": {"get": {"tags": ["Coverage"], "summary": "Region coverage", "parameters": [region_path], "responses": {"200": ok({"$ref": "#/components/schemas/Coverage"}), "404": ok({"$ref": "#/components/schemas/ErrorResponse"}, "Region not found")}}},
        },
        "components": components,
    }


def swagger_ui_html() -> str:
    return """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>PMTiles Map API Docs</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
<style>body{margin:0;background:#f7f8fa;}.swagger-ui .topbar{display:none;}</style>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>
window.addEventListener("load",()=>{SwaggerUIBundle({url:"/api/openapi.json",dom_id:"#swagger-ui",deepLinking:true,presets:[SwaggerUIBundle.presets.apis],layout:"BaseLayout"});});
</script>
</body></html>"""


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "pmtiles-map-api/2.0"

    def log_message(self, fmt, *args):
        log.info(json.dumps({
            "event": "access",
            "request_id": getattr(self, "request_id", None),
            "client": self.address_string(),
            "message": fmt % args,
        }, ensure_ascii=False))

    def init_request_context(self) -> float:
        self.request_id = safe_request_id(self.headers.get("X-Request-ID"))
        return time.monotonic()

    def require_internal_auth(self) -> None:
        if not INTERNAL_ENDPOINTS_ENABLED:
            raise APIError("internal_endpoint_disabled", "Internal endpoints are disabled", HTTPStatus.NOT_FOUND)
        if not MAP_INTERNAL_TOKEN:
            raise APIError(
                "internal_auth_not_configured",
                "MAP_INTERNAL_TOKEN is required for this internal endpoint",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        header_token = self.headers.get("X-Internal-Token", "")
        auth = self.headers.get("Authorization", "")
        bearer = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if header_token != MAP_INTERNAL_TOKEN and bearer != MAP_INTERNAL_TOKEN:
            raise APIError("unauthorized", "Internal token is required", HTTPStatus.UNAUTHORIZED)

    def finish_request_log(self, start: float) -> None:
        ms = (time.monotonic() - start) * 1000
        log.info(json.dumps({
            "event": "request",
            "request_id": getattr(self, "request_id", None),
            "method": self.command,
            "path": self.path,
            "duration_ms": round(ms, 1),
        }, ensure_ascii=False))

    def end_headers(self) -> None:
        if not getattr(self, "request_id", None):
            self.request_id = uuid.uuid4().hex
        self.send_header("X-Request-ID", self.request_id)
        self.send_header("Access-Control-Allow-Origin", API_CORS_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Request-ID, X-Internal-Token, Authorization")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.init_request_context()
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        start = self.init_request_context()
        try:
            self.route_get()
        except APIError as exc:
            self.write_error(exc.code, exc.message, exc.status, exc.details)
        except TileCoordinateValidationError as exc:
            self.write_error("invalid_tile_coordinate", str(exc), HTTPStatus.BAD_REQUEST)
        except FileNotFoundError as exc:
            self.write_error("not_found", str(exc), HTTPStatus.NOT_FOUND)
        except RasterTileError as exc:
            status = HTTPStatus(exc.status)
            message = str(exc)
            code = "invalid_tile_coordinate" if status == HTTPStatus.BAD_REQUEST and ("Tile x/y" in message or "zoom" in message) else "invalid_request"
            if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                code = "raster_error"
            self.write_error(code, message, status)
        except Exception as exc:
            log.exception("Unhandled error: %s", exc)
            self.write_error("internal_error", "Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self.finish_request_log(start)

    def do_POST(self) -> None:
        start = self.init_request_context()
        try:
            self.route_post()
        except APIError as exc:
            self.write_error(exc.code, exc.message, exc.status, exc.details)
        except TileCoordinateValidationError as exc:
            self.write_error("invalid_tile_coordinate", str(exc), HTTPStatus.BAD_REQUEST)
        except FileNotFoundError as exc:
            self.write_error("not_found", str(exc), HTTPStatus.NOT_FOUND)
        except RasterTileError as exc:
            status = HTTPStatus(exc.status)
            message = str(exc)
            code = "invalid_tile_coordinate" if status == HTTPStatus.BAD_REQUEST and ("Tile x/y" in message or "zoom" in message) else "invalid_request"
            if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
                code = "raster_error"
            self.write_error(code, message, status)
        except Exception as exc:
            log.exception("Unhandled error: %s", exc)
            self.write_error("internal_error", "Internal server error", HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            self.finish_request_log(start)

    def route_get(self) -> None:
        parsed = urlparse(self.path)
        path   = unquote(parsed.path)
        query  = parse_qs(parsed.query)

        if path in ("/", "/demo"):
            static = Path(__file__).parent / "static" / "index.html"
            self.write_file(static, "text/html; charset=utf-8")
            return

        if path in ("/docs", "/api/docs"):
            self.write_text(swagger_ui_html(), "text/html; charset=utf-8")
            return

        if path in ("/openapi.json", "/api/openapi.json"):
            self.write_json(openapi_spec(), cache_seconds=60)
            return

        if path == "/api/health/live":
            self.write_json({"ok": True, "status": "live"}, cache_seconds=5)
            return

        if path == "/api/health/dependencies":
            self.require_internal_auth()
            self.write_json(health_details(), cache_seconds=5)
            return

        if path == "/api/health/ready":
            details = health_details()
            ready = bool(details["ok"])
            details["status"] = "ready" if ready else "not_ready"
            self.write_json(
                details,
                status=HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                cache_seconds=5,
            )
            return

        if path == "/api/health":
            self.write_json({
                "ok": True,
                "default_region": DEFAULT_REGION,
                "default_style": DEFAULT_STYLE,
                "output_dir": str(OUTPUT_DIR),
                "manifests_dir_exists": (OUTPUT_DIR / "manifests").exists(),
                "styles_dir_exists": STYLES_DIR.exists(),
                "glyphs_dir": str(GLYPHS_DIR),
                "sprites_dir": str(SPRITES_DIR),
                "docs_url": "/docs",
                "openapi_url": "/api/openapi.json",
                "cache_ttl_seconds": CACHE_TTL,
                "max_threads": MAX_THREADS,
            })
            return

        if path == "/api/cache/status":
            self.write_json({"ok": True, "cache": _cache.snapshot()}, cache_seconds=5)
            return

        if path == "/api/cache/clear":
            self.require_internal_auth()
            _cache.clear()
            self.write_json({"ok": True, "message": "Cache cleared", "cache": _cache.snapshot()})
            return

        if path == "/api/regions":
            self.write_json({"regions": listed_regions()})
            return

        if path == "/api/styles":
            self.write_json_etag({"styles": list_styles(), "default_style": DEFAULT_STYLE}, cache_seconds=STYLE_CACHE_SECONDS)
            return

        style_meta_match = re.fullmatch(r"/api/styles/([a-zA-Z0-9_-]+)", path)
        if style_meta_match:
            self.write_json(style_metadata(safe_id(style_meta_match.group(1))), cache_seconds=STYLE_CACHE_SECONDS)
            return

        if path == "/api/tilesets":
            self.write_json_etag({"tilesets": all_tilesets()}, cache_seconds=MANIFEST_CACHE_SECONDS)
            return

        tileset_region_match = re.fullmatch(r"/api/tilesets/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)", path)
        if tileset_region_match:
            region, tileset_id = map(safe_id, tileset_region_match.groups())
            manifest, tileset = get_tileset(region, tileset_id)
            self.write_json_etag(tileset_metadata(region, tileset_id, tileset, manifest), cache_seconds=MANIFEST_CACHE_SECONDS)
            return

        tileset_match = re.fullmatch(r"/api/tilesets/([a-zA-Z0-9_-]+)", path)
        if tileset_match:
            tileset_id = safe_id(tileset_match.group(1))
            matches = [item for item in all_tilesets() if item["id"] == tileset_id]
            if not matches:
                raise APIError("not_found", f"Tileset not found: {tileset_id}", HTTPStatus.NOT_FOUND)
            self.write_json_etag({"tilesets": matches}, cache_seconds=MANIFEST_CACHE_SECONDS)
            return

        if path == "/api/coverage":
            regions = [coverage_for_region(region) for region in sorted(manifest_region_ids())]
            bounds = union_bounds([item["bounds"] for item in regions if item.get("bounds")])
            self.write_json_etag({
                "bounds": bounds,
                "regions": regions,
                "minzoom": min([r["minzoom"] for r in regions if r.get("minzoom") is not None], default=None),
                "maxzoom": max([r["maxzoom"] for r in regions if r.get("maxzoom") is not None], default=None),
                "last_updated": max([r["last_updated"] for r in regions if r.get("last_updated")], default=None),
            }, cache_seconds=MANIFEST_CACHE_SECONDS)
            return

        coverage_match = re.fullmatch(r"/api/coverage/([a-zA-Z0-9_-]+)", path)
        if coverage_match:
            self.write_json_etag(coverage_for_region(safe_id(coverage_match.group(1))), cache_seconds=MANIFEST_CACHE_SECONDS)
            return

        vector_tilejson_match = re.fullmatch(r"/api/vector(?:/([a-zA-Z0-9_-]+))?/tilejson\.json", path)
        if vector_tilejson_match:
            path_region = vector_tilejson_match.group(1)
            region = safe_id(query.get("region", [path_region or DEFAULT_REGION])[0])
            tileset_id = query.get("tileset", [None])[0]
            tileset_id = safe_id(tileset_id) if tileset_id else None
            manifest = load_manifest(region)
            host = self.headers.get("Host", f"localhost:{API_PORT}")
            if tileset_id:
                tile_url = f"http://{host}/api/vector/{region}/{tileset_id}/{{z}}/{{x}}/{{y}}.pbf"
            else:
                tile_url = f"http://{host}/api/vector/{region}/{{z}}/{{x}}/{{y}}.pbf"
            self.write_json_etag(vector_tilejson(region, tile_url, manifest, tileset_id), cache_seconds=TILEJSON_CACHE_SECONDS)
            return

        vector_region_tileset_match = re.fullmatch(
            r"/api/vector/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/(\d+)/(\d+)/(\d+)\.pbf",
            path,
        )
        if vector_region_tileset_match:
            region_raw, tileset_raw, z_raw, x_raw, y_raw = vector_region_tileset_match.groups()
            z, x, y = int(z_raw), int(x_raw), int(y_raw)
            body, _tileset_id, headers = vector_tile_response(safe_id(region_raw), z, x, y, safe_id(tileset_raw))
            self.write_binary(body, VECTOR_TILE_CONTENT_TYPE, cache_seconds=TILE_CACHE_SECONDS, extra_headers=headers)
            return

        vector_tile_match = re.fullmatch(r"/api/vector(?:/([a-zA-Z0-9_-]+))?/(\d+)/(\d+)/(\d+)\.pbf", path)
        if vector_tile_match:
            segment, z_raw, x_raw, y_raw = vector_tile_match.groups()
            z, x, y = int(z_raw), int(x_raw), int(y_raw)
            validate_tile(z, x, y)
            manifests = manifest_region_ids()
            tileset_id = None
            if not segment:
                lon, lat = tile_center_lonlat(z, x, y)
                region = resolve_region({"lon": [str(lon)], "lat": [str(lat)]})
            elif safe_id(segment) in manifests:
                region = safe_id(segment)
            else:
                region = safe_id(query.get("region", [DEFAULT_REGION])[0])
                tileset_id = safe_id(segment)
            body, _selected_id, headers = vector_tile_response(region, z, x, y, tileset_id)
            self.write_binary(body, VECTOR_TILE_CONTENT_TYPE, cache_seconds=TILE_CACHE_SECONDS, extra_headers=headers)
            return

        inspect_match = re.fullmatch(r"/api/tiles/inspect/([a-zA-Z0-9_-]+)/(\d+)/(\d+)/(\d+)", path)
        if inspect_match:
            self.require_internal_auth()
            region, z_raw, x_raw, y_raw = inspect_match.groups()
            z, x, y = int(z_raw), int(x_raw), int(y_raw)
            body, tileset_id, headers = vector_tile_response(safe_id(region), z, x, y, query.get("tileset", [None])[0])
            etag = f'"{hashlib.md5(body).hexdigest()}"'
            self.write_json({
                "exists": True,
                "region": safe_id(region),
                "z": z,
                "x": x,
                "y": y,
                "source": tileset_id,
                "tileset": tileset_id,
                "size_bytes": len(body),
                "content_type": VECTOR_TILE_CONTENT_TYPE,
                "content_encoding": headers.get("Content-Encoding"),
                "etag": etag,
                "cache_status": "hit" if self.headers.get("If-None-Match") == etag else "miss",
            }, cache_seconds=30)
            return

        glyph_match = re.fullmatch(r"/api/(?:fonts|glyphs)/([^/]+)/(\d+-\d+)\.pbf", path)
        if glyph_match:
            fontstack, glyph_range = glyph_match.groups()
            glyph_path = local_glyph_path(fontstack, glyph_range)
            if not glyph_path.exists():
                raise APIError("not_found", f"Glyph range not found: {fontstack}/{glyph_range}.pbf", HTTPStatus.NOT_FOUND)
            self.write_file(glyph_path, "application/x-protobuf", cache_seconds=ASSET_CACHE_SECONDS)
            return

        sprite_match = re.fullmatch(r"/api/sprites/([a-zA-Z0-9_-]+)/(sprite(?:@2x)?\.(?:json|png))", path)
        if sprite_match:
            style_id, filename = sprite_match.groups()
            sprite_path = local_sprite_path(style_id, filename)
            if sprite_path.exists():
                content_type = "application/json; charset=utf-8" if filename.endswith(".json") else "image/png"
                self.write_file(sprite_path, content_type, cache_seconds=ASSET_CACHE_SECONDS)
                return
            if filename.endswith(".json"):
                self.write_json_etag({}, cache_seconds=ASSET_CACHE_SECONDS)
                return
            self.write_binary(EMPTY_SPRITE_PNG, "image/png", cache_seconds=ASSET_CACHE_SECONDS)
            return

        tilejson_match = re.fullmatch(r"/api/raster(?:/([a-zA-Z0-9_-]+))?/tilejson\.json", path)
        if tilejson_match:
            path_region = tilejson_match.group(1)
            requested_region = query.get("region", [path_region or DEFAULT_REGION])[0]
            region = safe_id(requested_region)
            style_id = resolve_style_id(query)
            image_format = normalize_raster_format(query.get("format", ["png"])[0])
            manifest = load_manifest(region)
            host = self.headers.get("Host", f"localhost:{API_PORT}")
            tile_url = f"http://{host}/api/raster/{region}/{{z}}/{{x}}/{{y}}.{image_format}?style={style_id}"
            self.write_json_etag(
                raster_tilejson(region, style_id, tile_url, manifest, image_format),
                cache_seconds=TILEJSON_CACHE_SECONDS,
            )
            return

        raster_match = re.fullmatch(
            r"/api/raster(?:/([a-zA-Z0-9_-]+))?/(\d+)/(\d+)/(\d+)\.(png|webp|jpg|jpeg)",
            path,
        )
        if raster_match:
            path_region, z_raw, x_raw, y_raw, image_format_raw = raster_match.groups()
            image_format = normalize_raster_format(image_format_raw)
            z, x, y = int(z_raw), int(x_raw), int(y_raw)
            validate_tile(z, x, y)
            region = query.get("region", [path_region or ""])[0]
            if region:
                region = safe_id(region)
            else:
                lon, lat = tile_center_lonlat(z, x, y)
                region = resolve_region({"lon": [str(lon)], "lat": [str(lat)]})
            style_id = resolve_style_id(query)
            tile = render_raster_tile(load_manifest(region), OUTPUT_DIR, z, x, y, style_id, image_format)
            self.write_binary(tile, raster_content_type(image_format), cache_seconds=TILE_CACHE_SECONDS)
            return

        if path == "/api/resolve":
            regions = resolve_regions(query)
            self.write_json({
                "region": regions[0],
                "regions": regions,
                "manifest_url": f"/api/manifest/{regions[0]}.json",
            })
            return

        if path == "/api/manifest.json":
            self.write_json_etag(load_manifest(resolve_region(query)), cache_seconds=MANIFEST_CACHE_SECONDS)
            return

        if path == "/api/style.json":
            self.write_json_etag(build_auto_style(query, resolve_style_id(query)), cache_seconds=STYLE_CACHE_SECONDS)
            return

        if path.startswith("/api/manifest/"):
            region = path.removeprefix("/api/manifest/").removesuffix(".json")
            self.write_json_etag(load_manifest(safe_id(region)), cache_seconds=MANIFEST_CACHE_SECONDS)
            return

        if path.startswith("/api/style/"):
            region = path.removeprefix("/api/style/").removesuffix(".json")
            self.write_json_etag(build_style(safe_id(region), resolve_style_id(query)), cache_seconds=STYLE_CACHE_SECONDS)
            return

        self.write_error("not_found", "Not found", HTTPStatus.NOT_FOUND)

    def route_post(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/cache/clear":
            self.require_internal_auth()
            _cache.clear()
            self.write_json({"ok": True, "message": "Cache cleared", "cache": _cache.snapshot()})
            return
        if path == "/api/cache/warm":
            self.require_internal_auth()
            self.write_json(cache_warm(self.read_json_body(allow_empty=True)))
            return
        if path == "/api/styles/validate":
            self.write_json(validate_style_document(self.read_json_body()))
            return
        self.write_error("not_found", "Not found", HTTPStatus.NOT_FOUND)

    def read_json_body(self, allow_empty: bool = False) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            if allow_empty:
                return {}
            raise APIError("invalid_json", "Request body is required", HTTPStatus.BAD_REQUEST)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError("invalid_json", "Request body must be valid JSON", HTTPStatus.BAD_REQUEST) from exc
        if not isinstance(payload, dict):
            raise APIError("invalid_json", "Request body must be a JSON object", HTTPStatus.BAD_REQUEST)
        return payload

    # ── write helpers ──────────────────────────────────────────────────────────

    def _build_response(self, payload: dict) -> tuple[bytes, str]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = f'"{hashlib.md5(body).hexdigest()}"'
        return body, etag

    def write_json_etag(self, payload: dict, status=HTTPStatus.OK, cache_seconds: int = 0) -> None:
        body, etag = self._build_response(payload)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            if cache_seconds:
                immutable = ", immutable" if cache_seconds >= 31536000 else ""
                self.send_header("Cache-Control", f"public, max-age={cache_seconds}{immutable}")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        if cache_seconds:
            immutable = ", immutable" if cache_seconds >= 31536000 else ""
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}{immutable}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_json(self, payload: dict, status=HTTPStatus.OK, cache_seconds: int = 0) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cache_seconds:
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}, must-revalidate")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_error(
        self,
        code: str,
        message: str,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        details: dict | None = None,
    ) -> None:
        self.write_json(
            {
                "error": {
                    "status": int(status),
                    "code": code,
                    "message": message,
                    "request_id": getattr(self, "request_id", None) or "",
                    "details": details or {},
                }
            },
            status=status,
        )

    def write_text(self, payload: str, content_type: str, status=HTTPStatus.OK) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_binary(
        self,
        body: bytes,
        content_type: str,
        status=HTTPStatus.OK,
        cache_seconds: int = 0,
        extra_headers: dict[str, str] | None = None,
        etag: str | None = None,
    ) -> None:
        etag = etag or f'"{hashlib.md5(body).hexdigest()}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        if cache_seconds:
            immutable = ", immutable" if cache_seconds >= 31536000 else ""
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}{immutable}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def write_file(self, path: Path, content_type: str, cache_seconds: int = 0) -> None:
        if not path.exists():
            raise FileNotFoundError(str(path))
        body = path.read_bytes()
        etag = f'"{hashlib.md5(body).hexdigest()}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        if cache_seconds:
            immutable = ", immutable" if cache_seconds >= 31536000 else ""
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}{immutable}")
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Starting PMTiles Map API on http://%s:%d", API_HOST, API_PORT)
    log.info("Cache TTL=%ds  MaxThreads=%d", CACHE_TTL, MAX_THREADS)
    httpd = LimitedThreadingHTTPServer((API_HOST, API_PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")


if __name__ == "__main__":
    main()
