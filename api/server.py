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
from collections import Counter, deque
from copy import deepcopy

from http import HTTPStatus

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from socketserver import ThreadingMixIn

from typing import Any

from urllib.error import HTTPError, URLError

from urllib.parse import parse_qs, quote, unquote, urlparse

from urllib.request import Request, urlopen

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


def normalize_app_environment(value: str | None) -> str:
    raw = (value or "development").strip().lower()
    aliases = {
        "dev": "development",
        "local": "development",
        "test": "development",
        "prod": "production",
    }
    normalized = aliases.get(raw, raw)
    if normalized not in {"development", "staging", "production"}:
        log.warning("Unknown APP_ENV/ENVIRONMENT %r; using development", value)
        return "development"
    return normalized


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}



# ─── Config ───────────────────────────────────────────────────────────────────



APP_ROOT      = Path(os.getenv("APP_ROOT", "/app"))

OUTPUT_DIR    = Path(os.getenv("OUTPUT_DIR", APP_ROOT / "output"))

CONFIG_DIR    = Path(os.getenv("CONFIG_DIR", APP_ROOT / "config"))

STYLES_DIR    = Path(os.getenv("STYLES_DIR", CONFIG_DIR / "styles"))

API_HOST      = os.getenv("API_HOST", "0.0.0.0")

API_PORT      = int(os.getenv("API_PORT", "8090"))

ENVIRONMENT   = normalize_app_environment(os.getenv("APP_ENV") or os.getenv("ENVIRONMENT"))
APP_ENV       = ENVIRONMENT

DEFAULT_REGION     = os.getenv("REGION", "saudi")

DEFAULT_STYLE      = os.getenv("MAP_STYLE", "light")

API_CORS_ORIGIN    = os.getenv("API_CORS_ORIGIN", "*")

DEMO_GLYPHS_URL    = "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf"
RAW_GLYPHS_URL     = os.getenv("GLYPHS_URL")
GLYPHS_URL_EXPLICIT = bool(RAW_GLYPHS_URL and RAW_GLYPHS_URL.strip())
GLYPHS_URL         = RAW_GLYPHS_URL.strip() if GLYPHS_URL_EXPLICIT else DEMO_GLYPHS_URL
LOCAL_GLYPHS_URL   = (os.getenv("LOCAL_GLYPHS_URL", "/api/fonts/{fontstack}/{range}.pbf").strip()
                      or "/api/fonts/{fontstack}/{range}.pbf")
GLYPHS_DIR         = Path(os.getenv("GLYPHS_DIR", CONFIG_DIR / "glyphs"))
GLYPH_FONTSTACKS   = tuple(
    value.strip()
    for value in os.getenv("GLYPH_FONTSTACKS", "Noto Sans Regular").split(",")
    if value.strip()
)
GLYPH_READINESS_TIMEOUT_SECONDS = float(os.getenv("GLYPH_READINESS_TIMEOUT_SECONDS", "3"))
SPRITES_DIR        = Path(os.getenv("SPRITES_DIR", CONFIG_DIR / "sprites"))
MAP_INTERNAL_TOKEN = os.getenv("MAP_INTERNAL_TOKEN", "")
TRUSTED_GATEWAY_HEADER = os.getenv("TRUSTED_GATEWAY_HEADER", "").strip()
TRUSTED_GATEWAY_HEADER_VALUE = os.getenv("TRUSTED_GATEWAY_HEADER_VALUE", "").strip()
INTERNAL_ENDPOINTS_ENABLED = os.getenv("INTERNAL_ENDPOINTS_ENABLED", "true").lower() not in {"0", "false", "no"}
PUBLIC_DIRECT_TILE_ENDPOINTS = env_bool("PUBLIC_DIRECT_TILE_ENDPOINTS", ENVIRONMENT == "development")
ALLOW_PUBLIC_DIRECT_TILES_IN_PRODUCTION = env_bool("ALLOW_PUBLIC_DIRECT_TILES_IN_PRODUCTION", False)
REQUIRE_REVERSE_PROXY = env_bool("REQUIRE_REVERSE_PROXY", False)
TRUSTED_PROXY_MODE = os.getenv("TRUSTED_PROXY_MODE", "").strip()
PUBLIC_API_BASE_URL = os.getenv("PUBLIC_API_BASE_URL", "").strip()
TILES_BEHIND_GATEWAY = env_bool("TILES_BEHIND_GATEWAY", False)
CDN_BASE_URL = os.getenv("CDN_BASE_URL", "").strip()
S3_PUBLIC_BASE_URL = os.getenv("S3_PUBLIC_BASE_URL", "").strip()
STATIC_BASE_URL = os.getenv("STATIC_BASE_URL", "").strip()
PUBLIC_STATIC_BASE_URL = S3_PUBLIC_BASE_URL or CDN_BASE_URL or STATIC_BASE_URL
SPRITES_BASE_URL = os.getenv("SPRITES_BASE_URL", "").strip()
STATIC_PUBLISH_ROOT_RAW = os.getenv("STATIC_PUBLISH_ROOT", "").strip()
STATIC_PUBLISH_ROOT = Path(STATIC_PUBLISH_ROOT_RAW) if STATIC_PUBLISH_ROOT_RAW else None
STYLE_VALIDATE_MAX_BODY_BYTES = int(os.getenv("STYLE_VALIDATE_MAX_BODY_BYTES", str(1024 * 1024)))
MAX_AUTO_REGIONS   = int(os.getenv("MAX_AUTO_REGIONS", "8"))
AUTO_BBOX_MIN_ZOOM = float(os.getenv("AUTO_BBOX_MIN_ZOOM", "5.5"))
CACHE_TTL          = int(os.getenv("CACHE_TTL", "30"))          # seconds
STYLE_CACHE_SECONDS = int(os.getenv("STYLE_CACHE_SECONDS", "300"))
MANIFEST_CACHE_SECONDS = int(os.getenv("MANIFEST_CACHE_SECONDS", "60"))
TILEJSON_CACHE_SECONDS = int(os.getenv("TILEJSON_CACHE_SECONDS", "3600"))
ASSET_CACHE_SECONDS = int(os.getenv("ASSET_CACHE_SECONDS", "31536000"))
TILE_CACHE_SECONDS  = int(os.getenv("TILE_CACHE_SECONDS", "31536000"))
MAX_THREADS        = int(os.getenv("MAX_THREADS", "64"))
VECTOR_TILE_CONTENT_TYPE = os.getenv("VECTOR_TILE_CONTENT_TYPE", "application/vnd.mapbox-vector-tile")
EMPTY_SPRITE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/"
    "l63dWQAAAABJRU5ErkJggg=="
)

DEFAULT_SUPPORTED_LANGUAGES = ["ar", "en", "ku", "fa", "tr", "fr", "de", "es", "ru", "pt", "it", "ur"]
DEFAULT_LANGUAGE_FALLBACKS = {
    "en": ["name_en", "name_int", "name_local", "name_ar", "name"],
    "ar": ["name_ar", "name_local", "name_en", "name"],
    "ku": ["name_ku", "name_ar", "name_en", "name_local", "name"],
    "fa": ["name_fa", "name_ar", "name_en", "name_local", "name"],
    "tr": ["name_tr", "name_en", "name_local", "name"],
    "fr": ["name_fr", "name_en", "name_local", "name"],
    "de": ["name_de", "name_en", "name_local", "name"],
    "es": ["name_es", "name_en", "name_local", "name"],
    "ru": ["name_ru", "name_en", "name_local", "name"],
    "pt": ["name_pt", "name_en", "name_local", "name"],
    "it": ["name_it", "name_en", "name_local", "name"],
    "ur": ["name_ur", "name_en", "name_ar", "name_local", "name"],
}


def load_language_config() -> dict[str, Any]:
    path = CONFIG_DIR / "languages.json"
    if not path.exists():
        return {"supported_languages": DEFAULT_SUPPORTED_LANGUAGES, "fallbacks": DEFAULT_LANGUAGE_FALLBACKS}
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError:
        log.warning("Invalid language config at %s; using defaults", path)
        return {"supported_languages": DEFAULT_SUPPORTED_LANGUAGES, "fallbacks": DEFAULT_LANGUAGE_FALLBACKS}
    supported = payload.get("supported_languages") or DEFAULT_SUPPORTED_LANGUAGES
    fallbacks = payload.get("fallbacks") or DEFAULT_LANGUAGE_FALLBACKS
    return {
        "supported_languages": [str(lang).strip().lower() for lang in supported if str(lang).strip()],
        "fallbacks": {
            str(lang).strip().lower(): [str(field).strip() for field in fields if str(field).strip()]
            for lang, fields in fallbacks.items()
            if str(lang).strip()
        },
    }


LANGUAGE_CONFIG = load_language_config()
SUPPORTED_LANGUAGES = tuple(LANGUAGE_CONFIG["supported_languages"])
LANGUAGE_FALLBACKS = {
    **DEFAULT_LANGUAGE_FALLBACKS,
    **LANGUAGE_CONFIG.get("fallbacks", {}),
}


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


SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "internal_token",
    "map_internal_token",
    "authorization",
    "password",
    "secret",
    "api_key",
    "key",
}


def route_group_for_path(path: str) -> str:
    clean_path = unquote(urlparse(path).path)
    if clean_path in ("/", "/demo", "/docs", "/api/docs"):
        return "docs"
    if clean_path in ("/openapi.json", "/api/openapi.json"):
        return "openapi"
    if clean_path in ("/api/metrics", "/api/health/metrics"):
        return "metrics"
    if clean_path.startswith("/api/health"):
        return "health"
    if clean_path.startswith("/api/cache"):
        return "cache"
    if clean_path.startswith("/api/style"):
        return "style"
    if clean_path.startswith("/api/styles"):
        return "styles"
    if clean_path.startswith("/api/manifest"):
        return "manifest"
    if re.fullmatch(r"/api/vector(?:/[a-zA-Z0-9_-]+)?/tilejson\.json", clean_path):
        return "vector_tilejson"
    if re.fullmatch(r"/api/vector(?:/[a-zA-Z0-9_-]+)?(?:/[a-zA-Z0-9_-]+)?/-?\d+/-?\d+/-?\d+\.pbf", clean_path):
        return "vector_tile"
    if re.fullmatch(r"/api/raster(?:/[a-zA-Z0-9_-]+)?/tilejson\.json", clean_path):
        return "raster_tilejson"
    if re.fullmatch(r"/api/raster(?:/[a-zA-Z0-9_-]+)?/-?\d+/-?\d+/-?\d+\.(?:png|webp|jpg|jpeg)", clean_path):
        return "raster_tile"
    if clean_path.startswith("/api/fonts") or clean_path.startswith("/api/glyphs"):
        return "glyphs"
    if clean_path.startswith("/api/sprites"):
        return "sprites"
    if clean_path.startswith("/api/tiles/inspect"):
        return "tile_inspect"
    if clean_path.startswith("/api/tilesets"):
        return "tilesets"
    if clean_path.startswith("/api/coverage"):
        return "coverage"
    if clean_path.startswith("/api/regions") or clean_path.startswith("/api/resolve"):
        return "regions"
    return "other"


def is_direct_tile_route_group(route_group: str) -> bool:
    return route_group in {"vector_tile", "raster_tile"}


def redact_path(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.query:
        return value
    redacted_parts: list[str] = []
    for part in parsed.query.split("&"):
        if not part:
            redacted_parts.append(part)
            continue
        key, sep, raw_value = part.partition("=")
        if key.strip().lower() in SENSITIVE_QUERY_KEYS:
            redacted_parts.append(f"{key}{sep}[REDACTED]" if sep else key)
        else:
            redacted_parts.append(part)
    query = "&".join(redacted_parts)
    return parsed._replace(query=query).geturl()


class MetricsCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._total_requests = 0
        self._requests_by_status: Counter[str] = Counter()
        self._requests_by_route_group: Counter[str] = Counter()
        self._error_count = 0
        self._direct_tile_requests_total = 0
        self._direct_tile_blocked_total = 0
        self._style_requests_total = 0
        self._manifest_requests_total = 0
        self._raster_requests_total = 0
        self._vector_requests_total = 0
        self._latencies_ms: deque[float] = deque(maxlen=4096)

    def record_request(self, route_group: str, status: int, duration_ms: float, direct_tile_blocked: bool = False) -> None:
        status_key = str(int(status or 0))
        with self._lock:
            self._total_requests += 1
            self._requests_by_status[status_key] += 1
            self._requests_by_route_group[route_group] += 1
            if int(status or 0) >= 400:
                self._error_count += 1
            if is_direct_tile_route_group(route_group):
                self._direct_tile_requests_total += 1
            if direct_tile_blocked:
                self._direct_tile_blocked_total += 1
            if route_group == "style":
                self._style_requests_total += 1
            if route_group == "manifest":
                self._manifest_requests_total += 1
            if route_group.startswith("raster"):
                self._raster_requests_total += 1
            if route_group.startswith("vector"):
                self._vector_requests_total += 1
            self._latencies_ms.append(float(duration_ms))

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return round(values[0], 3)
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((percentile / 100) * (len(ordered) - 1)))))
        return round(ordered[index], 3)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            latencies = list(self._latencies_ms)
            return {
                "ok": True,
                "environment": ENVIRONMENT,
                "uptime_seconds": round(time.monotonic() - self._started, 3),
                "total_requests": self._total_requests,
                "requests_by_status": dict(sorted(self._requests_by_status.items())),
                "requests_by_route_group": dict(sorted(self._requests_by_route_group.items())),
                "error_count": self._error_count,
                "latency_ms": {
                    "p50": self._percentile(latencies, 50),
                    "p95": self._percentile(latencies, 95),
                    "p99": self._percentile(latencies, 99),
                    "samples": len(latencies),
                },
                "cache": _cache.snapshot(),
                "direct_tile_requests_total": self._direct_tile_requests_total,
                "direct_tile_blocked_total": self._direct_tile_blocked_total,
                "style_requests_total": self._style_requests_total,
                "manifest_requests_total": self._manifest_requests_total,
                "raster_requests_total": self._raster_requests_total,
                "vector_requests_total": self._vector_requests_total,
            }

    def reset(self) -> None:
        with self._lock:
            self._started = time.monotonic()
            self._total_requests = 0
            self._requests_by_status.clear()
            self._requests_by_route_group.clear()
            self._error_count = 0
            self._direct_tile_requests_total = 0
            self._direct_tile_blocked_total = 0
            self._style_requests_total = 0
            self._manifest_requests_total = 0
            self._raster_requests_total = 0
            self._vector_requests_total = 0
            self._latencies_ms.clear()


_metrics = MetricsCollector()


def prometheus_metrics(payload: dict[str, Any]) -> str:
    lines = [
        "# HELP tavrix_tiles_total_requests Total HTTP requests handled by the tile API.",
        "# TYPE tavrix_tiles_total_requests counter",
        f"tavrix_tiles_total_requests {payload['total_requests']}",
        "# HELP tavrix_tiles_error_count HTTP requests with status >= 400.",
        "# TYPE tavrix_tiles_error_count counter",
        f"tavrix_tiles_error_count {payload['error_count']}",
        "# HELP tavrix_tiles_uptime_seconds Tile API process uptime.",
        "# TYPE tavrix_tiles_uptime_seconds gauge",
        f"tavrix_tiles_uptime_seconds {payload['uptime_seconds']}",
    ]
    for status, count in payload["requests_by_status"].items():
        lines.append(f'tavrix_tiles_requests_by_status{{status="{status}"}} {count}')
    for route_group, count in payload["requests_by_route_group"].items():
        lines.append(f'tavrix_tiles_requests_by_route_group{{route_group="{route_group}"}} {count}')
    for name in ("p50", "p95", "p99"):
        lines.append(f'tavrix_tiles_latency_ms{{quantile="{name.removeprefix("p")}"}} {payload["latency_ms"][name]}')
    for key in (
        "direct_tile_requests_total",
        "direct_tile_blocked_total",
        "style_requests_total",
        "manifest_requests_total",
        "raster_requests_total",
        "vector_requests_total",
    ):
        lines.append(f"tavrix_tiles_{key} {payload[key]}")
    cache = payload["cache"]
    lines.extend([
        f"tavrix_tiles_cache_hits {cache.get('hits', 0)}",
        f"tavrix_tiles_cache_misses {cache.get('misses', 0)}",
        f"tavrix_tiles_cache_items {cache.get('items', 0)}",
    ])
    return "\n".join(lines) + "\n"


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


def resolve_language(query: dict[str, list[str]]) -> str | None:
    raw = query.get("lang", [None])[0]
    if raw is None or str(raw).strip() == "":
        return None
    lang = str(raw).strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise APIError(
            "unsupported_language",
            f"Unsupported language '{raw}'. Supported languages: {', '.join(SUPPORTED_LANGUAGES)}",
            HTTPStatus.BAD_REQUEST,
            {"supported_languages": list(SUPPORTED_LANGUAGES)},
        )
    return lang


def language_fallback_fields(lang: str | None) -> list[str]:
    if not lang:
        return ["name"]
    return LANGUAGE_FALLBACKS.get(lang) or DEFAULT_LANGUAGE_FALLBACKS.get(lang, ["name"])


def _get_expr(field: str) -> list[str]:
    return ["get", field]


def _expr_contains_get(expr: Any, field: str) -> bool:
    if isinstance(expr, list):
        if len(expr) == 2 and expr[0] == "get" and expr[1] == field:
            return True
        return any(_expr_contains_get(item, field) for item in expr)
    return False


def language_text_expression(lang: str, original: Any | None = None) -> list[Any]:
    fallback = [_get_expr(field) for field in language_fallback_fields(lang)]
    if original is not None and _expr_contains_get(original, "ref"):
        return ["coalesce", _get_expr("ref"), *fallback]
    return ["coalesce", *fallback]


def apply_language_to_style(style: dict, lang: str | None) -> dict:
    if not lang:
        return style
    for layer in style.get("layers", []):
        if layer.get("type") != "symbol":
            continue
        layout = layer.get("layout")
        if not isinstance(layout, dict) or "text-field" not in layout:
            continue
        layout["text-field"] = language_text_expression(lang, layout.get("text-field"))
    metadata = style.get("metadata", {})
    metadata["language"] = lang
    style["metadata"] = metadata
    return style


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
    glyphs = style.get("glyphs")
    if not glyphs or glyphs == "{GLYPHS_URL}" or str(glyphs).startswith("http"):
        glyphs = style_glyphs_url()
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
        "glyphs": glyphs,
        "sprite": style.get("sprite") or (style_sprite_url(style_id) if style_has_icons(style) else None),
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
    if not style.get("glyphs") or style["glyphs"] == "{GLYPHS_URL}" or str(style.get("glyphs", "")).startswith("http"):
        style["glyphs"] = style_glyphs_url()
    if style_has_icons(style) and not style.get("sprite"):
        style["sprite"] = style_sprite_url(style_id)
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





def build_style(region: str, style_id: str = DEFAULT_STYLE, lang: str | None = None) -> dict:

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
    apply_language_to_style(style, lang)

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





def build_auto_style(query: dict, style_id: str = DEFAULT_STYLE, lang: str | None = None) -> dict:

    regions = resolve_regions(query)

    if len(regions) == 1:

        style = build_style(regions[0], style_id, lang)

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
    apply_language_to_style(style, lang)
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


def vector_tilejson(region: str, tile_url: str | None, manifest: dict, tileset_id: str | None = None) -> dict:
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
        "tiles": [tile_url] if tile_url else [],
        "minzoom": min(minzooms) if minzooms else 0,
        "maxzoom": max(maxzooms) if maxzooms else 14,
        "bounds": bounds,
        "center": center_from_bounds(bounds) if bounds else manifest.get("center"),
        "attribution": "Tavrix PMTiles",
        "description": "Vector tiles rendered from Tavrix PMTiles. Region-specific URLs are recommended for production and CDN caching.",
        "vector_layers": vector_layers_for_tilesets(selected_tilesets),
        "region": region,
        "tileset": tileset_id,
        "direct_tiles_enabled": bool(tile_url),
        "pmtiles": {
            name: tileset.get("url")
            for name, tileset in selected_tilesets.items()
            if tileset.get("url")
        },
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


PRODUCTION_ENVIRONMENTS = {"prod", "production"}
REQUIRED_GLYPH_RANGES = (
    {"id": "latin_basic", "range": "0-255", "script": "Latin basic", "languages": ["en", "tr", "fr", "de", "es", "pt", "it"]},
    {"id": "cyrillic", "range": "1024-1279", "script": "Cyrillic", "languages": ["ru"]},
    {"id": "arabic", "range": "1536-1791", "script": "Arabic", "languages": ["ar", "ku", "fa", "ur"]},
)


def is_production_environment() -> bool:
    return ENVIRONMENT in PRODUCTION_ENVIRONMENTS


def is_demo_glyph_url(value: str | None = None) -> bool:
    url = (value if value is not None else GLYPHS_URL).strip().lower()
    return "demotiles.maplibre.org" in url


def style_glyphs_url() -> str:
    if is_production_environment() and (not GLYPHS_URL_EXPLICIT or is_demo_glyph_url(GLYPHS_URL)):
        return ""
    return GLYPHS_URL if GLYPHS_URL else DEMO_GLYPHS_URL


def style_sprite_url(style_id: str) -> str:
    style_id = safe_id(style_id)
    if SPRITES_BASE_URL:
        return SPRITES_BASE_URL.rstrip("/") + f"/{style_id}/sprite"
    return f"/api/sprites/{style_id}/sprite"


def _is_local_glyph_url(value: str) -> bool:
    parsed = urlparse(value)
    if not parsed.scheme and value.startswith("/"):
        return True
    return parsed.path.startswith(("/api/fonts/", "/api/glyphs/")) and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _glyph_request_url(template: str, fontstack: str, glyph_range: str) -> str:
    return (
        template
        .replace("{fontstack}", quote(fontstack, safe=""))
        .replace("{range}", glyph_range)
    )


def _fontstacks_from_text_font(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if all(isinstance(item, str) for item in value):
            return [str(item) for item in value]
        if len(value) >= 2 and value[0] == "literal" and isinstance(value[1], list):
            return [str(item) for item in value[1] if isinstance(item, str)]
    return []


def style_fontstacks(styles: list[dict]) -> list[str]:
    fontstacks: set[str] = set()
    for style in styles:
        for layer in _style_layers(style):
            if layer.get("type") != "symbol":
                continue
            fontstacks.update(_fontstacks_from_text_font(layer.get("layout", {}).get("text-font")))
    if not fontstacks:
        fontstacks.update(GLYPH_FONTSTACKS or ("Noto Sans Regular",))
    return sorted(fontstacks)


def _remote_glyph_probe(url: str) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/x-protobuf,application/octet-stream,*/*"})
    with urlopen(request, timeout=GLYPH_READINESS_TIMEOUT_SECONDS) as response:
        body = response.read(1)
        content_length = int(response.headers.get("Content-Length") or len(body))
        return {
            "status": int(response.status),
            "content_type": response.headers.get("Content-Type"),
            "content_length": content_length,
            "ok": response.status in (200, 206) and bool(body or content_length > 0),
        }


def glyph_readiness(styles: list[dict] | None = None) -> dict[str, Any]:
    styles = styles if styles is not None else []
    glyphs_required = any(style_has_text(style) for style in styles) if styles else True
    url = GLYPHS_URL if GLYPHS_URL else DEMO_GLYPHS_URL
    production = is_production_environment()
    demo = is_demo_glyph_url(url)
    local = _is_local_glyph_url(url)
    fontstacks = style_fontstacks(styles)
    result: dict[str, Any] = {
        "ok": True,
        "required": glyphs_required,
        "environment": ENVIRONMENT,
        "url": url,
        "explicit": GLYPHS_URL_EXPLICIT,
        "demo": demo,
        "mode": "local" if local else ("demo" if demo else "external"),
        "fontstacks": fontstacks,
        "required_ranges": list(REQUIRED_GLYPH_RANGES),
        "checks": [],
        "failures": [],
        "warnings": [],
    }

    if not glyphs_required:
        return result

    if production and not GLYPHS_URL_EXPLICIT:
        result["failures"].append("GLYPHS_URL must be explicitly configured in production.")
    if production and demo:
        result["failures"].append("Demo MapLibre glyphs are not allowed in production.")

    if demo:
        result["warnings"].append("Demo MapLibre glyphs are allowed only for development.")
        result["ok"] = not result["failures"]
        return result

    if local:
        for fontstack in fontstacks:
            for spec in REQUIRED_GLYPH_RANGES:
                glyph_range = spec["range"]
                path = local_glyph_path(fontstack, glyph_range)
                ok = path.exists() and path.stat().st_size > 0
                check = {
                    "fontstack": fontstack,
                    "range": glyph_range,
                    "script": spec["script"],
                    "path": str(path),
                    "ok": ok,
                }
                result["checks"].append(check)
                if not ok:
                    result["failures"].append(f"Missing local glyph PBF: {fontstack}/{glyph_range}.pbf")
    else:
        for fontstack in fontstacks:
            for spec in REQUIRED_GLYPH_RANGES:
                glyph_range = spec["range"]
                request_url = _glyph_request_url(url, fontstack, glyph_range)
                check = {
                    "fontstack": fontstack,
                    "range": glyph_range,
                    "script": spec["script"],
                    "url": request_url,
                    "ok": True,
                }
                result["checks"].append(check)

    result["ok"] = not result["failures"]
    return result


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


DEV_INTERNAL_TOKEN = "dev-internal-token"
VERSIONED_PMTILES_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:-[a-z0-9][a-z0-9_-]*)?-z\d+-z\d+-\d{8}-\d{4}\.pmtiles$")


def _status(ok: bool, *, code: str | None = None, message: str | None = None, warnings: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    failures = []
    if not ok and code:
        failures.append({"code": code, "message": message or code})
    result = {
        "ok": ok,
        "code": code,
        "warnings": warnings or [],
        "failures": failures,
    }
    result.update(extra)
    return result


def cors_origins() -> list[str]:
    raw = (API_CORS_ORIGIN or "").strip()
    if not raw:
        return []
    if raw == "*":
        return ["*"]
    return [part.strip() for part in raw.split(",") if part.strip()]


def cors_response_origin(request_origin: str | None) -> str | None:
    origins = cors_origins()
    if not origins:
        return None
    if "*" in origins:
        return "*"
    if request_origin and request_origin in origins:
        return request_origin
    return origins[0]


def cors_status() -> dict[str, Any]:
    origins = cors_origins()
    if is_production_environment():
        if not origins or "*" in origins:
            return _status(
                False,
                code="unsafe_cors_origin",
                message="API_CORS_ORIGIN must be an explicit origin allowlist in production.",
                origins=[] if "*" in origins else origins,
            )
        return _status(True, origins=origins)
    warnings = []
    if "*" in origins:
        warnings.append("Wildcard API_CORS_ORIGIN is allowed only outside production.")
    return _status(True, warnings=warnings, origins=origins)


def internal_token_status() -> dict[str, Any]:
    token = MAP_INTERNAL_TOKEN or ""
    gateway_header_configured = bool(TRUSTED_GATEWAY_HEADER and TRUSTED_GATEWAY_HEADER_VALUE)
    if is_production_environment():
        if not token and not gateway_header_configured:
            return _status(False, code="unsafe_internal_token", message="MAP_INTERNAL_TOKEN or TRUSTED_GATEWAY_HEADER_VALUE must be set in production.")
        if token and token == DEV_INTERNAL_TOKEN:
            return _status(False, code="unsafe_internal_token", message="MAP_INTERNAL_TOKEN must not use the development default in production.")
        if token and len(token) < 32:
            return _status(False, code="unsafe_internal_token", message="MAP_INTERNAL_TOKEN must be at least 32 characters in production.")
        if gateway_header_configured and len(TRUSTED_GATEWAY_HEADER_VALUE) < 32:
            return _status(False, code="unsafe_internal_token", message="TRUSTED_GATEWAY_HEADER_VALUE must be at least 32 characters in production.")
        return _status(True, configured=bool(token), trusted_gateway_header_configured=gateway_header_configured, min_length=32)
    warnings = []
    if not token and not gateway_header_configured:
        warnings.append("MAP_INTERNAL_TOKEN/TRUSTED_GATEWAY_HEADER_VALUE are not configured; internal endpoints will be unavailable.")
    elif token == DEV_INTERNAL_TOKEN:
        warnings.append("Development internal token is allowed only outside production.")
    elif token and len(token) < 32:
        warnings.append("Short MAP_INTERNAL_TOKEN is allowed only outside production.")
    return _status(True, warnings=warnings, configured=bool(token), trusted_gateway_header_configured=gateway_header_configured, min_length=32)


def internal_token_matches(headers: Any) -> bool:
    if not MAP_INTERNAL_TOKEN:
        return False
    getter = headers.get if hasattr(headers, "get") else lambda _name, default="": default
    header_token = getter("X-Internal-Token", "")
    auth = getter("Authorization", "")
    bearer = auth.removeprefix("Bearer ").strip() if isinstance(auth, str) and auth.startswith("Bearer ") else ""
    return header_token == MAP_INTERNAL_TOKEN or bearer == MAP_INTERNAL_TOKEN


def trusted_gateway_header_matches(headers: Any) -> bool:
    if not TRUSTED_GATEWAY_HEADER or not TRUSTED_GATEWAY_HEADER_VALUE:
        return False
    getter = headers.get if hasattr(headers, "get") else lambda _name, default="": default
    return getter(TRUSTED_GATEWAY_HEADER, "") == TRUSTED_GATEWAY_HEADER_VALUE


def internal_auth_allowed(headers: Any) -> bool:
    return internal_token_status()["ok"] and (internal_token_matches(headers) or trusted_gateway_header_matches(headers))


def direct_tile_policy_status() -> dict[str, Any]:
    public_configured = bool(PUBLIC_DIRECT_TILE_ENDPOINTS)
    production = is_production_environment()
    public_effective = public_configured and (not production or ALLOW_PUBLIC_DIRECT_TILES_IN_PRODUCTION)
    warnings: list[str] = []
    failures: list[dict[str, str]] = []

    if production and public_configured and not ALLOW_PUBLIC_DIRECT_TILES_IN_PRODUCTION:
        failures.append({
            "code": "unsafe_direct_tile_policy",
            "message": "PUBLIC_DIRECT_TILE_ENDPOINTS=true in production requires ALLOW_PUBLIC_DIRECT_TILES_IN_PRODUCTION=true.",
        })
    if production and not REQUIRE_REVERSE_PROXY:
        failures.append({
            "code": "unsafe_reverse_proxy_config",
            "message": "REQUIRE_REVERSE_PROXY=true is required in production after configuring a reverse proxy/CDN.",
        })
    if production and public_configured and not (TRUSTED_PROXY_MODE or PUBLIC_API_BASE_URL):
        failures.append({
            "code": "unsafe_reverse_proxy_config",
            "message": "Set TRUSTED_PROXY_MODE or PUBLIC_API_BASE_URL when exposing direct tile endpoints through a proxy/CDN.",
        })
    if production and public_effective:
        warnings.append("Public direct vector/raster tile API endpoints are enabled in production; prefer CDN/static PMTiles and enforce reverse proxy/CDN rate limiting.")
    if not production and public_configured:
        warnings.append("Public direct tile endpoints are enabled for development.")

    return {
        "ok": not failures,
        "public_configured": public_configured,
        "public_effective": public_effective,
        "internal_token_bypass": True,
        "require_reverse_proxy": REQUIRE_REVERSE_PROXY,
        "trusted_proxy_mode": bool(TRUSTED_PROXY_MODE),
        "public_api_base_url": bool(PUBLIC_API_BASE_URL),
        "warnings": warnings,
        "failures": failures,
    }


def direct_tiles_publicly_enabled() -> bool:
    return bool(direct_tile_policy_status()["public_effective"])


def direct_tile_access_allowed(headers: Any) -> bool:
    return direct_tiles_publicly_enabled() or internal_auth_allowed(headers)


def direct_tile_access_details() -> dict[str, Any]:
    policy = direct_tile_policy_status()
    return {
        "public_direct_tile_endpoints": policy["public_effective"],
        "internal_token_bypass": policy["internal_token_bypass"],
        "recommended_public_serving": "static PMTiles via CDN/object storage plus map-api style/manifest endpoints",
    }


FORBIDDEN_PUBLIC_URL_MARKERS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "static:80",
    "host.docker.internal",
    "tavrixmaptiles-map-api",
    "map-api:8090",
    "pmtiles-builder",
)


def _url_failure(value: str, field: str) -> dict[str, str] | None:
    lowered = value.lower()
    if not value:
        return {"code": "missing_public_url", "message": f"{field} must be configured."}
    if not value.startswith("https://"):
        return {"code": "unsafe_public_url", "message": f"{field} must use https in production: {value}"}
    for marker in FORBIDDEN_PUBLIC_URL_MARKERS:
        if marker in lowered:
            return {"code": "unsafe_public_url", "message": f"{field} contains an internal or local hostname: {value}"}
    return None


def _collect_string_values(value: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        result: list[tuple[str, str]] = []
        for key, item in value.items():
            result.extend(_collect_string_values(item, f"{path}.{key}"))
        return result
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            result.extend(_collect_string_values(item, f"{path}[{index}]"))
        return result
    return []


def _public_url_hygiene_failures(payload: Any, *, label: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for path, value in _collect_string_values(payload):
        lowered = value.lower()
        if any(marker in lowered for marker in FORBIDDEN_PUBLIC_URL_MARKERS):
            failures.append({
                "code": "unsafe_public_url",
                "message": f"{label} contains internal/local URL value at {path}: {value}",
            })
        if "pmtiles://" in lowered and "http://" in lowered:
            failures.append({
                "code": "unsafe_public_url",
                "message": f"{label} PMTiles source must use https in production at {path}: {value}",
            })
    return failures


def static_publication_status(manifest_status: dict[str, Any]) -> dict[str, Any]:
    if not STATIC_PUBLISH_ROOT:
        return _status(
            True,
            configured=False,
            path=None,
            warnings=["STATIC_PUBLISH_ROOT is not configured; readiness verifies local output only."],
        )
    failures: list[dict[str, str]] = []
    root = STATIC_PUBLISH_ROOT
    if not root.exists():
        failures.append({"code": "missing_static_publish_root", "message": f"STATIC_PUBLISH_ROOT does not exist: {root}"})
    for subdir in ("tiles", "manifests", "fonts", "sprites"):
        path = root / subdir
        if not path.exists():
            failures.append({"code": "missing_static_publish_root", "message": f"Static publication path is missing: {path}"})
    for reference in manifest_status.get("references", []):
        key = str(reference.get("key") or "")
        if key and not (root / key).exists():
            failures.append({"code": "missing_static_artifact", "message": f"Static PMTiles artifact is missing: {root / key}"})
    return {
        "ok": not failures,
        "configured": True,
        "path": str(root),
        "warnings": [],
        "failures": failures,
    }


def gateway_deployment_status(manifest_status: dict[str, Any] | None = None) -> dict[str, Any]:
    production = is_production_environment()
    enabled = bool(TILES_BEHIND_GATEWAY)
    manifest_status = manifest_status or manifest_reference_status(DEFAULT_REGION)
    static_status = static_publication_status(manifest_status)
    failures: list[dict[str, str]] = []
    warnings: list[str] = []

    if production and not enabled:
        failures.append({
            "code": "gateway_mode_disabled",
            "message": "TILES_BEHIND_GATEWAY=true is required for the Phase 1 production deployment.",
        })

    if not enabled:
        return {
            "ok": not failures,
            "enabled": enabled,
            "public_api_base_url": PUBLIC_API_BASE_URL,
            "public_static_base_url": PUBLIC_STATIC_BASE_URL,
            "sprites_base_url": SPRITES_BASE_URL,
            "static_publication": static_status,
            "gateway_contract_documented": True,
            "gateway_contract": {
                "path": "docs/gateway-integration-contract.md",
                "tiles_role": "internal_map_engine",
                "gateway_role": "public_entry_point",
            },
            "metrics_internal_url": "/api/metrics",
            "warnings": warnings,
            "failures": failures,
        }

    for field, value in (
        ("PUBLIC_API_BASE_URL", PUBLIC_API_BASE_URL),
        ("CDN_BASE_URL/S3_PUBLIC_BASE_URL", PUBLIC_STATIC_BASE_URL),
        ("GLYPHS_URL", GLYPHS_URL),
    ):
        failure = _url_failure(value, field)
        if failure:
            failures.append(failure)

    if SPRITES_BASE_URL:
        failure = _url_failure(SPRITES_BASE_URL, "SPRITES_BASE_URL")
        if failure:
            failures.append(failure)
    else:
        warnings.append("SPRITES_BASE_URL is not configured; styles with sprites will use the internal API sprite route.")

    if PUBLIC_STATIC_BASE_URL and GLYPHS_URL and "{fontstack}" in GLYPHS_URL and not GLYPHS_URL.startswith(PUBLIC_STATIC_BASE_URL.rstrip("/") + "/"):
        warnings.append("GLYPHS_URL does not use the configured static base URL.")
    if SPRITES_BASE_URL and PUBLIC_STATIC_BASE_URL and not SPRITES_BASE_URL.startswith(PUBLIC_STATIC_BASE_URL.rstrip("/") + "/"):
        warnings.append("SPRITES_BASE_URL does not use the configured static base URL.")

    direct = direct_tile_policy_status()
    if direct.get("public_effective"):
        failures.append({
            "code": "unsafe_direct_tile_policy",
            "message": "PUBLIC_DIRECT_TILE_ENDPOINTS must be false/effectively disabled behind the Gateway.",
        })

    failures.extend(static_status.get("failures", []))
    warnings.extend(static_status.get("warnings", []))

    try:
        sample_style = build_style(DEFAULT_REGION, DEFAULT_STYLE, "en")
        failures.extend(_public_url_hygiene_failures(sample_style, label="Sample production style"))
    except Exception as exc:
        if production:
            failures.append({"code": "style_generation_failed", "message": f"Could not build sample style for readiness: {exc}"})
        else:
            warnings.append(f"Could not build sample style for public URL hygiene check: {exc}")

    return {
        "ok": not failures,
        "enabled": enabled,
        "public_api_base_url": PUBLIC_API_BASE_URL,
        "public_static_base_url": PUBLIC_STATIC_BASE_URL,
        "sprites_base_url": SPRITES_BASE_URL,
        "static_publication": static_status,
        "gateway_contract_documented": True,
        "gateway_contract": {
            "path": "docs/gateway-integration-contract.md",
            "tiles_role": "internal_map_engine",
            "gateway_role": "public_entry_point",
        },
        "metrics_internal_url": "/api/metrics",
        "warnings": warnings,
        "failures": failures,
    }


def manifest_reference_status(region: str) -> dict[str, Any]:
    configured_regions = load_regions().get("regions", {})
    if region not in configured_regions and region != "global":
        return _status(
            False,
            code="missing_default_region_manifest",
            message=f"REGION '{region}' is not configured in config/regions.json.",
            region=region,
            references=[],
        )

    path = manifest_path(region)
    if not path.exists():
        return _status(
            False,
            code="missing_default_region_manifest",
            message=f"Default region manifest does not exist: {path}",
            region=region,
            manifest_path=str(path),
            references=[],
        )

    try:
        manifest = read_json(path)
    except json.JSONDecodeError:
        return _status(
            False,
            code="missing_default_region_manifest",
            message=f"Default region manifest is not valid JSON: {path}",
            region=region,
            manifest_path=str(path),
            references=[],
        )

    references = []
    failures: list[dict[str, str]] = []
    tilesets = manifest.get("tilesets")
    if not isinstance(tilesets, dict) or not tilesets:
        failures.append({"code": "invalid_manifest_reference", "message": "Default region manifest has no tilesets."})
        tilesets = {}

    for tileset_id, tileset in tilesets.items():
        if not isinstance(tileset, dict):
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' is not an object."})
            continue
        key = str(tileset.get("key") or "")
        filename = str(tileset.get("filename") or (Path(key).name if key else ""))
        local_path = OUTPUT_DIR / key if key else Path("")
        validation_path = local_path.with_suffix(".validation.json") if key else Path("")
        item = {
            "tileset": str(tileset_id),
            "filename": filename,
            "key": key,
            "path": str(local_path) if key else "",
            "validation_path": str(validation_path) if key else "",
            "file_exists": bool(key and local_path.exists()),
            "validation_exists": bool(key and validation_path.exists()),
            "versioned": bool(VERSIONED_PMTILES_RE.fullmatch(filename)),
            "validation_ok": False,
        }
        references.append(item)
        if not key:
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' is missing key."})
            continue
        if filename != Path(key).name:
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' filename does not match key."})
        if not item["versioned"]:
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' does not reference a versioned immutable PMTiles filename."})
        if not local_path.exists():
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' PMTiles file is missing."})
        if not validation_path.exists():
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' validation JSON is missing."})
            continue
        try:
            validation = read_json(validation_path)
        except json.JSONDecodeError:
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' validation JSON is invalid."})
            continue
        item["validation_ok"] = validation.get("ok") is True
        if not item["validation_ok"]:
            failures.append({"code": "invalid_manifest_reference", "message": f"Tileset '{tileset_id}' validation JSON is not ok=true."})

    if failures:
        return {
            "ok": False,
            "code": failures[0]["code"],
            "region": region,
            "manifest_path": str(path),
            "references": references,
            "warnings": [],
            "failures": failures,
        }
    return _status(True, region=region, manifest_path=str(path), references=references)


def production_hardening_status() -> dict[str, Any]:
    internal = internal_token_status()
    cors = cors_status()
    manifest = manifest_reference_status(DEFAULT_REGION)
    direct = direct_tile_policy_status()
    gateway = gateway_deployment_status(manifest)
    statuses = {
        "internal_token": internal,
        "cors": cors,
        "default_region": manifest,
        "direct_tile_policy": direct,
        "gateway_deployment": gateway,
    }
    warnings: list[str] = []
    failures: list[dict[str, str]] = []
    for status in statuses.values():
        warnings.extend(str(item) for item in status.get("warnings", []))
        if is_production_environment():
            failures.extend(status.get("failures", []))
        else:
            warnings.extend(f"{failure.get('code')}: {failure.get('message')}" for failure in status.get("failures", []))
    production_ready = True if not is_production_environment() else not failures
    default_region_ready = manifest["ok"] or not is_production_environment()
    return {
        "environment": ENVIRONMENT,
        "ok": production_ready,
        "production": is_production_environment(),
        "production_config_ready": production_ready,
        "internal_token_safe": internal["ok"],
        "cors_safe": cors["ok"],
        "default_region_ready": default_region_ready,
        "manifest_ready": default_region_ready,
        "direct_tile_policy": direct,
        "gateway_deployment": gateway,
        "warnings": warnings,
        "failures": failures,
        "statuses": statuses,
    }


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
        glyphs = glyph_readiness(styles)
        checks.append({"name": "glyphs_dir", "ok": glyphs["mode"] != "local" or GLYPHS_DIR.exists(), "path": str(GLYPHS_DIR), "required": glyphs["mode"] == "local"})
        checks.append({"name": "glyphs_ready", "ok": glyphs["ok"], "glyphs": glyphs})
    sprites_required = any(style_has_icons(style) or style.get("sprite") for style in styles)
    if sprites_required:
        checks.append({"name": "sprites_dir", "ok": SPRITES_DIR.exists(), "path": str(SPRITES_DIR)})
    return checks


def health_details() -> dict:
    raw_checks = dependency_checks()
    hardening = production_hardening_status()
    checks: dict[str, bool] = {
        "manifests_dir": False,
        "styles_dir": False,
        "pmtiles_files": False,
        "glyphs_dir": True,
        "glyphs_ready": True,
        "sprites_dir": True,
        "default_region": False,
        "default_style": False,
        "cache": True,
        "production_config_ready": hardening["production_config_ready"],
        "cors_safe": hardening["cors_safe"],
        "internal_token_safe": hardening["internal_token_safe"],
        "default_region_ready": hardening["default_region_ready"],
        "direct_tile_policy": hardening["direct_tile_policy"]["ok"],
        "gateway_deployment": hardening["gateway_deployment"]["ok"],
        "manifest_ready": hardening["manifest_ready"],
    }
    missing: list[str] = []
    warnings: list[str] = list(hardening["warnings"])
    failures: list[dict[str, str]] = list(hardening["failures"])
    glyphs: dict[str, Any] = glyph_readiness([])
    for item in raw_checks:
        name = str(item.get("name"))
        ok = bool(item.get("ok"))
        checks[name] = ok
        if name == "glyphs_ready" and isinstance(item.get("glyphs"), dict):
            glyphs = item["glyphs"]
            warnings.extend(str(value) for value in glyphs.get("warnings", []))
            if is_production_environment() and not glyphs.get("ok", True):
                failures.append({
                    "code": "unsafe_glyphs_url",
                    "message": "; ".join(str(value) for value in glyphs.get("failures", [])) or "Glyph readiness failed in production.",
                })
        if not ok:
            missing.append(name)
    manifests = manifest_region_ids()
    default_region_has_manifest = DEFAULT_REGION in manifests
    checks["default_region"] = default_region_has_manifest or not is_production_environment()
    if not default_region_has_manifest:
        warnings.append(f"Default region '{DEFAULT_REGION}' has no manifest.")
    checks["default_style"] = style_path(DEFAULT_STYLE).exists()
    if not checks["default_style"]:
        warnings.append(f"Default style '{DEFAULT_STYLE}' was not found.")
    missing = sorted({name for name, value in checks.items() if not value} | set(missing))
    ok = all(checks.values())
    return {
        "ok": ok,
        "environment": ENVIRONMENT,
        "production_config_ready": hardening["production_config_ready"],
        "cors_safe": hardening["cors_safe"],
        "internal_token_safe": hardening["internal_token_safe"],
        "default_region_ready": hardening["default_region_ready"],
        "direct_tile_policy": hardening["direct_tile_policy"],
        "gateway_deployment": hardening["gateway_deployment"],
        "glyphs_ready": checks["glyphs_ready"],
        "manifest_ready": hardening["manifest_ready"],
        "checks": checks,
        "missing": missing,
        "warnings": sorted(set(warnings)),
        "failures": failures,
        "glyphs": glyphs,
        "metrics_url": "/api/metrics",
        "gateway_contract_documented": bool(hardening["gateway_deployment"].get("gateway_contract_documented")),
        "hardening": hardening["statuses"],
    }




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
        "lang": {
            "name": "lang",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "enum": list(SUPPORTED_LANGUAGES)},
            "description": "Optional label language. Omit to preserve the default style text fields.",
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
                    "metrics_url": {"type": "string"},
                    "cache_ttl_seconds": {"type": "integer"},
                    "max_threads": {"type": "integer"},
                    "environment": {"type": "string"},
                    "direct_tile_policy": {"type": "object"},
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
                    "language": {"type": "string", "enum": list(SUPPORTED_LANGUAGES)},
                },
                "required": [
                    "tilejson",
                    "name",
                    "scheme",
                    "tiles",
                    "minzoom",
                    "maxzoom",
                    "bounds",
                    "format",
                    "region",
                    "style",
                ],
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
                "required": ["version", "sources", "layers"],
                "additionalProperties": True,
            },
            "ErrorResponse": {
                "type": "object",
                "properties": {"error": {"type": "string"}},
            },
        }
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
        "X-Request-ID": {"schema": {"type": "string"}, "description": "Global unique request identifier, preserved from request or generated on server."},
    }
    vector_response_headers = {
        **cache_response_headers,
        "Content-Encoding": {
            "schema": {"type": "string"},
            "description": "gzip when the stored vector tile payload is gzip-compressed.",
        },
    }
    request_id_header = {
        "X-Request-ID": {
            "schema": {"type": "string"},
            "description": "Global unique request identifier, preserved from request or generated on server.",
        }
    }

    def ok_with_request_id(schema: dict, description: str = "OK", extra_headers: dict | None = None) -> dict:
        headers = {**request_id_header, **(extra_headers or {})}
        res = ok(schema, description)
        res["headers"] = headers
        return res

    def error_response(description: str) -> dict:
        return {
            "description": description,
            "headers": request_id_header,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
        }

    def cached_json(schema: dict, description: str = "OK") -> dict:
        response = ok(schema, description)
        response["headers"] = cache_response_headers
        return response

    direct_tile_disabled_response = error_response(
        "Public direct tile endpoints are disabled by default in production when PUBLIC_DIRECT_TILE_ENDPOINTS=false. "
        "Use PMTiles CDN URLs from style/manifest. Error code: direct_tile_endpoints_disabled."
    )
    direct_tile_disabled_response["content"]["application/json"]["examples"] = {
        "disabled": {
            "value": {
                "error": {
                    "status": 403,
                    "code": "direct_tile_endpoints_disabled",
                    "message": "Direct vector/raster tile API endpoints are disabled by policy. Use static PMTiles/CDN serving or provide internal token for diagnostics.",
                    "request_id": "request-id",
                    "details": {
                        "recommended_public_serving": "static PMTiles via CDN/object storage plus map-api style/manifest endpoints"
                    },
                }
            }
        }
    }
    direct_tile_description = (
        "These endpoints are diagnostic/development or explicitly-enabled production endpoints. "
        "Recommended public production serving is PMTiles via CDN/object storage using URLs from the style or manifest. "
        "When PUBLIC_DIRECT_TILE_ENDPOINTS=false, public requests return 403 direct_tile_endpoints_disabled."
    )
    vector_tile = {
        "200": {
            "description": "Vector tile. Cache-Control: public, max-age=31536000, immutable.",
            "headers": vector_response_headers,
            "content": {VECTOR_TILE_CONTENT_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        },
        "400": error_response("Invalid tile request or coordinate (invalid_tile_coordinate)."),
        "403": direct_tile_disabled_response,
        "404": error_response("Tile, region, or tileset not found"),
    }
    raster_response_headers = {
        **cache_response_headers,
        "Content-Encoding": {
            "schema": {"type": "string"},
            "description": "Omitted for raster tiles since PNG/WebP/JPEG images are already compressed format-level.",
        },
    }
    raster_tile = {
        "200": {
            "description": "Raster image tile. Cache-Control: public, max-age=31536000, immutable.",
            "headers": raster_response_headers,
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        "400": error_response("Invalid tile request or coordinate (invalid_tile_coordinate)."),
        "403": direct_tile_disabled_response,
        "404": error_response("Tile or region not found"),
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
        "MetricsResponse": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "environment": {"type": "string"},
                "uptime_seconds": {"type": "number"},
                "total_requests": {"type": "integer"},
                "requests_by_status": {"type": "object", "additionalProperties": {"type": "integer"}},
                "requests_by_route_group": {"type": "object", "additionalProperties": {"type": "integer"}},
                "error_count": {"type": "integer"},
                "latency_ms": {
                    "type": "object",
                    "properties": {
                        "p50": {"type": "number"},
                        "p95": {"type": "number"},
                        "p99": {"type": "number"},
                        "samples": {"type": "integer"},
                    },
                    "required": ["p50", "p95", "p99", "samples"],
                },
                "cache": {"$ref": "#/components/schemas/CacheStatus/properties/cache"},
                "direct_tile_requests_total": {"type": "integer"},
                "direct_tile_blocked_total": {"type": "integer"},
                "style_requests_total": {"type": "integer"},
                "manifest_requests_total": {"type": "integer"},
                "raster_requests_total": {"type": "integer"},
                "vector_requests_total": {"type": "integer"},
            },
            "required": [
                "ok",
                "environment",
                "uptime_seconds",
                "total_requests",
                "requests_by_status",
                "requests_by_route_group",
                "error_count",
                "latency_ms",
                "cache",
                "direct_tile_requests_total",
                "direct_tile_blocked_total",
                "style_requests_total",
                "manifest_requests_total",
                "raster_requests_total",
                "vector_requests_total",
            ],
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
                        "glyphs_ready": {"type": "boolean"},
                        "sprites_dir": {"type": "boolean"},
                        "default_region": {"type": "boolean"},
                        "default_style": {"type": "boolean"},
                        "cache": {"type": "boolean"},
                        "production_config_ready": {"type": "boolean"},
                        "cors_safe": {"type": "boolean"},
                        "internal_token_safe": {"type": "boolean"},
                        "default_region_ready": {"type": "boolean"},
                        "direct_tile_policy": {"type": "boolean"},
                        "gateway_deployment": {"type": "boolean"},
                        "manifest_ready": {"type": "boolean"},
                    },
                    "required": [
                        "manifests_dir",
                        "styles_dir",
                        "pmtiles_files",
                        "glyphs_dir",
                        "glyphs_ready",
                        "sprites_dir",
                        "default_region",
                        "default_style",
                        "cache",
                        "production_config_ready",
                        "cors_safe",
                        "internal_token_safe",
                        "default_region_ready",
                        "direct_tile_policy",
                        "gateway_deployment",
                        "manifest_ready",
                    ],
                    "additionalProperties": {"type": "boolean"},
                },
                "environment": {"type": "string", "enum": ["development", "staging", "production"]},
                "production_config_ready": {"type": "boolean"},
                "cors_safe": {"type": "boolean"},
                "internal_token_safe": {"type": "boolean"},
                "default_region_ready": {"type": "boolean"},
                "direct_tile_policy": {"type": "object"},
                "gateway_deployment": {"type": "object"},
                "glyphs_ready": {"type": "boolean"},
                "manifest_ready": {"type": "boolean"},
                "missing": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "failures": {"type": "array", "items": {"type": "object"}},
                "metrics_url": {"type": "string"},
                "gateway_contract_documented": {"type": "boolean"},
                "glyphs": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "required": {"type": "boolean"},
                        "environment": {"type": "string"},
                        "url": {"type": "string"},
                        "explicit": {"type": "boolean"},
                        "demo": {"type": "boolean"},
                        "mode": {"type": "string", "enum": ["local", "external", "demo"]},
                        "fontstacks": {"type": "array", "items": {"type": "string"}},
                        "required_ranges": {"type": "array", "items": {"type": "object"}},
                        "checks": {"type": "array", "items": {"type": "object"}},
                        "failures": {"type": "array", "items": {"type": "string"}},
                        "warnings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["ok", "required", "environment", "url", "explicit", "demo", "mode", "fontstacks", "required_ranges", "checks", "failures", "warnings"],
                },
            },
            "required": ["ok", "environment", "production_config_ready", "cors_safe", "internal_token_safe", "default_region_ready", "direct_tile_policy", "gateway_deployment", "glyphs_ready", "manifest_ready", "checks", "missing", "warnings", "failures", "glyphs", "metrics_url", "gateway_contract_documented"],
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
            "required": ["tilesets"],
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
            "description": "Internal PMTiles/MapLibre engine for styles, manifests, vector tiles, raster tiles, glyphs, sprites, metadata, coverage, cache, and health checks. All responses include the X-Request-ID header.",
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
            "/api/openapi.json": {"get": {"tags": ["Docs"], "summary": "OpenAPI document", "responses": {"200": ok_with_request_id({"type": "object"})}}},
            "/api/health": {"get": {"tags": ["Health"], "summary": "Basic health", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/HealthResponse"})}}},
            "/api/health/live": {"get": {"tags": ["Health"], "summary": "Liveness check", "responses": {"200": ok_with_request_id({"type": "object"}, "Live")}}},
            "/api/health/ready": {"get": {"tags": ["Health"], "summary": "Readiness check", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/HealthDetailed"}, "Ready"), "503": ok_with_request_id({"$ref": "#/components/schemas/HealthDetailed"}, "Not ready")}}},
            "/api/health/dependencies": {"get": {"tags": ["Health"], "summary": "Dependency checks", "security": internal_security, "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/HealthDetailed"}), "401": error_response("Unauthorized")}}},
            "/api/metrics": {"get": {"tags": ["Health"], "summary": "Runtime request metrics", "description": "Returns low-cardinality runtime counters and latency percentiles. Use ?format=prometheus for Prometheus text format. Metrics are secret-free, but production deployments should protect this endpoint with a reverse proxy, IP allowlist, or equivalent trusted access control.", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/MetricsResponse"})}}},
            "/api/health/metrics": {"get": {"tags": ["Health"], "summary": "Runtime request metrics alias", "description": "Alias for /api/metrics. Metrics are secret-free, but production deployments should protect this endpoint with a reverse proxy, IP allowlist, or equivalent trusted access control.", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/MetricsResponse"})}}},
            "/api/cache/status": {"get": {"tags": ["Cache"], "summary": "Cache status", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/CacheStatus"})}}},
            "/api/cache/clear": {
                "get": {"tags": ["Cache"], "summary": "Clear cache (legacy GET)", "security": internal_security, "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/MessageResponse"}), "401": error_response("Unauthorized")}},
                "post": {"tags": ["Cache"], "summary": "Clear cache", "security": internal_security, "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/MessageResponse"}), "401": error_response("Unauthorized")}},
            },
            "/api/cache/warm": {"post": {"tags": ["Cache"], "summary": "Warm cache", "security": internal_security, "requestBody": {"required": False, "content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"200": ok_with_request_id({"type": "object"}), "401": error_response("Unauthorized")}}},
            "/api/styles": {"get": {"tags": ["Styles"], "summary": "List styles", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/StylesResponse"})}}},
            "/api/styles/{style_id}": {"get": {"tags": ["Styles"], "summary": "Style metadata", "parameters": [style_path_param], "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/StyleMetadata"}, "Style metadata. Cache-Control: public, max-age=300."), "404": error_response("Style not found")}}},
            "/api/styles/validate": {"post": {"tags": ["Styles"], "summary": "Validate a MapLibre style", "description": f"Validates a MapLibre style document. Request body limit: {STYLE_VALIDATE_MAX_BODY_BYTES} bytes.", "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}, "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/StyleValidation"}), "413": error_response("Request body too large")}}},
            "/api/style.json": {"get": {"tags": ["Styles"], "summary": "Auto-resolved MapLibre style", "description": "Returns style.json with Cache-Control: public, max-age=300 and ETag.", "parameters": [p["style"], p["lang"], p["lon"], p["lat"], p["zoom"], p["bbox"], region_query], "responses": {"200": cached_json({"$ref": "#/components/schemas/MapLibreStyle"}, "MapLibre style JSON. Cache-Control: public, max-age=300."), "400": error_response("Unsupported language")}}},
            "/api/style/{region}.json": {"get": {"tags": ["Styles"], "summary": "Region MapLibre style", "description": "Returns style.json with Cache-Control: public, max-age=300 and ETag.", "parameters": [region_path, p["style"], p["lang"]], "responses": {"200": cached_json({"$ref": "#/components/schemas/MapLibreStyle"}, "MapLibre style JSON. Cache-Control: public, max-age=300."), "400": error_response("Unsupported language"), "404": error_response("Manifest or style not found")}}},
            "/api/regions": {"get": {"tags": ["Regions"], "summary": "List regions", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/RegionsResponse"})}}},
            "/api/resolve": {"get": {"tags": ["Regions"], "summary": "Resolve viewport to regions", "parameters": [p["lon"], p["lat"], p["bbox"]], "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/ResolveResponse"})}}},
            "/api/manifest.json": {"get": {"tags": ["Manifests"], "summary": "Auto-resolved manifest", "description": f"Returns active manifest JSON with Cache-Control: public, max-age={MANIFEST_CACHE_SECONDS}, must-revalidate and ETag. Static CDN manifests use Cache-Control: public, max-age=60, must-revalidate; API manifests intentionally use the same short active-manifest cache policy by default.", "parameters": [p["lon"], p["lat"], p["bbox"]], "responses": {"200": cached_json({"$ref": "#/components/schemas/Manifest"}, f"Manifest JSON. Cache-Control: public, max-age={MANIFEST_CACHE_SECONDS}, must-revalidate.")}}},
            "/api/manifest/{region}.json": {"get": {"tags": ["Manifests"], "summary": "Region manifest", "description": f"Returns active manifest JSON with Cache-Control: public, max-age={MANIFEST_CACHE_SECONDS}, must-revalidate and ETag. Static CDN manifests use Cache-Control: public, max-age=60, must-revalidate; API manifests intentionally use the same short active-manifest cache policy by default.", "parameters": [region_path], "responses": {"200": cached_json({"$ref": "#/components/schemas/Manifest"}, f"Manifest JSON. Cache-Control: public, max-age={MANIFEST_CACHE_SECONDS}, must-revalidate."), "404": error_response("Manifest not found")}}},
            "/api/vector/tilejson.json": {"get": {"tags": ["Vector Tiles"], "summary": "Vector TileJSON", "description": "Returns TileJSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [region_query, tileset_query], "responses": {"200": cached_json({"$ref": "#/components/schemas/VectorTileJson"}, "Vector TileJSON. Cache-Control: public, max-age=3600.")}}},
            "/api/vector/{region}/tilejson.json": {"get": {"tags": ["Vector Tiles"], "summary": "Region Vector TileJSON", "description": "Returns TileJSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [region_path, tileset_query], "responses": {"200": cached_json({"$ref": "#/components/schemas/VectorTileJson"}, "Vector TileJSON. Cache-Control: public, max-age=3600.")}}},
            "/api/vector/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Auto-resolved vector tile", "description": direct_tile_description + " This convenience endpoint resolves region from the tile center.", "parameters": [z_param, x_param, y_param, region_query], "responses": vector_tile}},
            "/api/vector/{region}/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Region vector tile", "description": direct_tile_description, "parameters": [region_path, z_param, x_param, y_param], "responses": vector_tile}},
            "/api/vector/{tileset}/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Tileset vector tile for default/query region", "description": direct_tile_description, "parameters": [tileset_path_param, z_param, x_param, y_param, region_query], "responses": vector_tile}},
            "/api/vector/{region}/{tileset}/{z}/{x}/{y}.pbf": {"get": {"tags": ["Vector Tiles"], "summary": "Region tileset vector tile", "description": direct_tile_description, "parameters": [region_path, tileset_path_param, z_param, x_param, y_param], "responses": vector_tile}},
            "/api/raster/tilejson.json": {"get": {"tags": ["Raster Tiles"], "summary": "Raster TileJSON", "description": "Returns TileJSON with Cache-Control: public, max-age=3600 and ETag.", "parameters": [region_query, p["style"], p["lang"], raster_format_query], "responses": {"200": cached_json({"$ref": "#/components/schemas/RasterTileJson"}, "Raster TileJSON. Cache-Control: public, max-age=3600."), "400": error_response("Unsupported language")}}},
            "/api/raster/{z}/{x}/{y}.{format}": {"get": {"tags": ["Raster Tiles"], "summary": "Auto-resolved raster tile", "description": direct_tile_description + " This convenience endpoint resolves region from the tile center.", "parameters": [z_param, x_param, y_param, raster_format, region_query, p["style"], p["lang"]], "responses": raster_tile}},
            "/api/raster/{region}/{z}/{x}/{y}.{format}": {"get": {"tags": ["Raster Tiles"], "summary": "Region raster tile", "description": direct_tile_description, "parameters": [region_path, z_param, x_param, y_param, raster_format, p["style"], p["lang"]], "responses": raster_tile}},
            "/api/fonts/{fontstack}/{range}.pbf": {"get": {"tags": ["Assets"], "summary": "Font glyph PBF", "description": "Returns glyph PBF with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "fontstack", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "range", "in": "path", "required": True, "schema": {"type": "string", "example": "0-255"}}], "responses": {"200": {"description": "Glyph PBF. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"application/x-protobuf": {"schema": {"type": "string", "format": "binary"}}}}, "400": error_response("Invalid glyph request"), "404": error_response("Glyph not found")}}},
            "/api/glyphs/{fontstack}/{range}.pbf": {"get": {"tags": ["Assets"], "summary": "Glyph PBF alias", "description": "Returns glyph PBF with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "fontstack", "in": "path", "required": True, "schema": {"type": "string"}}, {"name": "range", "in": "path", "required": True, "schema": {"type": "string", "example": "0-255"}}], "responses": {"200": {"description": "Glyph PBF. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"application/x-protobuf": {"schema": {"type": "string", "format": "binary"}}}}, "400": error_response("Invalid glyph request"), "404": error_response("Glyph not found")}}},
            "/api/sprites/{style}/sprite.json": {"get": {"tags": ["Assets"], "summary": "Sprite JSON", "description": "Returns sprite JSON with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": cached_json({"type": "object"}, "Sprite JSON. Cache-Control: public, max-age=31536000, immutable."), "400": error_response("Invalid sprite request"), "404": error_response("Sprite or style not found")}}},
            "/api/sprites/{style}/sprite.png": {"get": {"tags": ["Assets"], "summary": "Sprite PNG", "description": "Returns sprite PNG with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Sprite PNG. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}, "400": error_response("Invalid sprite request"), "404": error_response("Sprite or style not found")}}},
            "/api/sprites/{style}/sprite@2x.json": {"get": {"tags": ["Assets"], "summary": "Retina sprite JSON", "description": "Returns sprite JSON with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": cached_json({"type": "object"}, "Sprite JSON. Cache-Control: public, max-age=31536000, immutable."), "400": error_response("Invalid sprite request"), "404": error_response("Sprite or style not found")}}},
            "/api/sprites/{style}/sprite@2x.png": {"get": {"tags": ["Assets"], "summary": "Retina sprite PNG", "description": "Returns sprite PNG with Cache-Control: public, max-age=31536000, immutable and ETag.", "parameters": [{"name": "style", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": {"description": "Sprite PNG. Cache-Control: public, max-age=31536000, immutable.", "headers": cache_response_headers, "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}, "400": error_response("Invalid sprite request"), "404": error_response("Sprite or style not found")}}},
            "/api/tilesets": {"get": {"tags": ["Tilesets"], "summary": "List tilesets", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/TilesetsResponse"})}}},
            "/api/tilesets/{tileset_id}": {"get": {"tags": ["Tilesets"], "summary": "Tileset metadata across regions", "parameters": [{"name": "tileset_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/TilesetsResponse"})}}},
            "/api/tilesets/{region}/{tileset_id}": {"get": {"tags": ["Tilesets"], "summary": "Region tileset metadata", "parameters": [region_path, {"name": "tileset_id", "in": "path", "required": True, "schema": {"type": "string"}}], "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/Tileset"}), "404": error_response("Tileset not found")}}},
            "/api/tiles/inspect/{region}/{z}/{x}/{y}": {
                "get": {
                    "tags": ["Debug"],
                    "summary": "Inspect tile availability",
                    "security": internal_security,
                    "parameters": [region_path, z_param, x_param, y_param, tileset_query],
                    "responses": {
                        "200": ok_with_request_id({"$ref": "#/components/schemas/TileInspect"}),
                        "400": error_response("Invalid tile coordinate (invalid_tile_coordinate)."),
                        "401": error_response("Unauthorized"),
                        "404": error_response("Tile not found"),
                    }
                }
            },
            "/api/coverage": {"get": {"tags": ["Coverage"], "summary": "All coverage", "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/CoverageResponse"})}}},
            "/api/coverage/{region}": {"get": {"tags": ["Coverage"], "summary": "Region coverage", "parameters": [region_path], "responses": {"200": ok_with_request_id({"$ref": "#/components/schemas/Coverage"}), "404": error_response("Region not found")}}},
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

    def send_response(self, code, message=None):
        self.response_status = int(code)
        super().send_response(code, message)

    def log_message(self, fmt, *args):
        raw_message = fmt % args
        message = re.sub(r"(GET|POST|OPTIONS) ([^ ]+)", lambda match: f"{match.group(1)} {redact_path(match.group(2))}", raw_message)
        log.info(json.dumps({
            "event": "access",
            "request_id": getattr(self, "request_id", None),
            "client": self.address_string(),
            "method": getattr(self, "command", None),
            "path": redact_path(getattr(self, "path", "")),
            "route_group": route_group_for_path(getattr(self, "path", "")),
            "status": getattr(self, "response_status", None),
            "environment": ENVIRONMENT,
            "message": message,
        }, ensure_ascii=False))

    def init_request_context(self) -> float:
        self.request_id = safe_request_id(self.headers.get("X-Request-ID"))
        self.response_status = 0
        self.direct_tile_blocked = False
        return time.monotonic()

    def require_internal_auth(self) -> None:
        if not INTERNAL_ENDPOINTS_ENABLED:
            raise APIError("internal_endpoint_disabled", "Internal endpoints are disabled", HTTPStatus.NOT_FOUND)
        if not MAP_INTERNAL_TOKEN and not (TRUSTED_GATEWAY_HEADER and TRUSTED_GATEWAY_HEADER_VALUE):
            raise APIError(
                "internal_auth_not_configured",
                "MAP_INTERNAL_TOKEN or trusted Gateway header is required for this internal endpoint",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        if not internal_auth_allowed(self.headers):
            raise APIError("unauthorized", "Internal token or trusted Gateway header is required", HTTPStatus.UNAUTHORIZED)

    def require_direct_tile_access(self) -> None:
        if direct_tile_access_allowed(self.headers):
            return
        self.direct_tile_blocked = True
        log.warning(json.dumps({
            "event": "direct_tile_blocked",
            "request_id": getattr(self, "request_id", None),
            "method": getattr(self, "command", None),
            "path": redact_path(getattr(self, "path", "")),
            "route_group": route_group_for_path(getattr(self, "path", "")),
            "environment": ENVIRONMENT,
            "user_agent": self.headers.get("User-Agent", ""),
        }, ensure_ascii=False))
        raise APIError(
            "direct_tile_endpoints_disabled",
            "Direct vector/raster tile API endpoints are disabled by policy. Use static PMTiles/CDN serving or provide internal token for diagnostics.",
            HTTPStatus.FORBIDDEN,
            direct_tile_access_details(),
        )

    def finish_request_log(self, start: float) -> None:
        ms = (time.monotonic() - start) * 1000
        path = getattr(self, "path", "")
        route_group = route_group_for_path(path)
        status = int(getattr(self, "response_status", 0) or 500)
        direct_tile_blocked = bool(getattr(self, "direct_tile_blocked", False))
        _metrics.record_request(route_group, status, ms, direct_tile_blocked)
        log.info(json.dumps({
            "event": "request",
            "request_id": getattr(self, "request_id", None),
            "method": self.command,
            "path": redact_path(path),
            "route_group": route_group,
            "status": status,
            "duration_ms": round(ms, 1),
            "environment": ENVIRONMENT,
            "direct_tile_blocked": direct_tile_blocked,
            "user_agent": self.headers.get("User-Agent", ""),
        }, ensure_ascii=False))

    def end_headers(self) -> None:
        if not getattr(self, "request_id", None):
            self.request_id = uuid.uuid4().hex
        self.send_header("X-Request-ID", self.request_id)
        response_origin = cors_response_origin(self.headers.get("Origin"))
        if response_origin:
            self.send_header("Access-Control-Allow-Origin", response_origin)
            if response_origin != "*":
                self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Request-ID, X-Internal-Token, X-Tavrix-Gateway-Token, Authorization")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        start = self.init_request_context()
        try:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        finally:
            self.finish_request_log(start)

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

        if path in ("/api/metrics", "/api/health/metrics"):
            payload = _metrics.snapshot()
            requested_format = (query.get("format", [""])[0] or "").lower()
            accept = (self.headers.get("Accept") or "").lower()
            if requested_format in {"prometheus", "prom"} or "text/plain" in accept:
                self.write_text(prometheus_metrics(payload), "text/plain; version=0.0.4; charset=utf-8")
            else:
                self.write_json(payload)
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
                "metrics_url": "/api/metrics",
                "cache_ttl_seconds": CACHE_TTL,
                "max_threads": MAX_THREADS,
                "environment": ENVIRONMENT,
                "direct_tile_policy": direct_tile_policy_status(),
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
            tile_url = None
            if direct_tile_access_allowed(self.headers):
                host = self.headers.get("Host", f"localhost:{API_PORT}")
                base_url = PUBLIC_API_BASE_URL.rstrip("/") if PUBLIC_API_BASE_URL else f"http://{host}"
                if tileset_id:
                    tile_url = f"{base_url}/api/vector/{region}/{tileset_id}/{{z}}/{{x}}/{{y}}.pbf"
                else:
                    tile_url = f"{base_url}/api/vector/{region}/{{z}}/{{x}}/{{y}}.pbf"
            self.write_json_etag(vector_tilejson(region, tile_url, manifest, tileset_id), cache_seconds=TILEJSON_CACHE_SECONDS)
            return

        vector_region_tileset_match = re.fullmatch(
            r"/api/vector/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_-]+)/(-?\d+)/(-?\d+)/(-?\d+)\.pbf",
            path,
        )
        if vector_region_tileset_match:
            self.require_direct_tile_access()
            region_raw, tileset_raw, z_raw, x_raw, y_raw = vector_region_tileset_match.groups()
            z, x, y = int(z_raw), int(x_raw), int(y_raw)
            body, _tileset_id, headers = vector_tile_response(safe_id(region_raw), z, x, y, safe_id(tileset_raw))
            self.write_binary(body, VECTOR_TILE_CONTENT_TYPE, cache_seconds=TILE_CACHE_SECONDS, extra_headers=headers)
            return

        vector_tile_match = re.fullmatch(r"/api/vector(?:/([a-zA-Z0-9_-]+))?/(-?\d+)/(-?\d+)/(-?\d+)\.pbf", path)
        if vector_tile_match:
            self.require_direct_tile_access()
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

        inspect_match = re.fullmatch(r"/api/tiles/inspect/([a-zA-Z0-9_-]+)/(-?\d+)/(-?\d+)/(-?\d+)", path)
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
                raise APIError(
                    "not_found",
                    f"Glyph range not found: {fontstack}/{glyph_range}.pbf",
                    HTTPStatus.NOT_FOUND,
                    {"fontstack": fontstack, "range": glyph_range, "glyphs_dir": str(GLYPHS_DIR)},
                )
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
            lang = resolve_language(query)
            image_format = normalize_raster_format(query.get("format", ["png"])[0])
            manifest = load_manifest(region)
            tile_url = None
            if direct_tile_access_allowed(self.headers):
                host = self.headers.get("Host", f"localhost:{API_PORT}")
                base_url = PUBLIC_API_BASE_URL.rstrip("/") if PUBLIC_API_BASE_URL else f"http://{host}"
                tile_url = f"{base_url}/api/raster/{region}/{{z}}/{{x}}/{{y}}.{image_format}?style={style_id}"
                if lang:
                    tile_url += f"&lang={lang}"
            self.write_json_etag(
                raster_tilejson(region, style_id, tile_url, manifest, image_format, lang),
                cache_seconds=TILEJSON_CACHE_SECONDS,
            )
            return

        raster_match = re.fullmatch(
            r"/api/raster(?:/([a-zA-Z0-9_-]+))?/(-?\d+)/(-?\d+)/(-?\d+)\.(png|webp|jpg|jpeg)",
            path,
        )
        if raster_match:
            self.require_direct_tile_access()
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
            lang = resolve_language(query)
            tile = render_raster_tile(load_manifest(region), OUTPUT_DIR, z, x, y, style_id, image_format, lang)
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

            style_id = resolve_style_id(query)
            lang = resolve_language(query)
            requested_region = query.get("region", [None])[0]
            if requested_region:
                style = build_style(safe_id(requested_region), style_id, lang)
            else:
                style = build_auto_style(query, style_id, lang)
            self.write_json_etag(style, cache_seconds=STYLE_CACHE_SECONDS)
            return



        if path.startswith("/api/manifest/"):

            region = path.removeprefix("/api/manifest/").removesuffix(".json")

            self.write_json_etag(load_manifest(safe_id(region)), cache_seconds=MANIFEST_CACHE_SECONDS)
            return



        if path.startswith("/api/style/"):

            region = path.removeprefix("/api/style/").removesuffix(".json")

            self.write_json_etag(
                build_style(safe_id(region), resolve_style_id(query), resolve_language(query)),
                cache_seconds=STYLE_CACHE_SECONDS,
            )
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
            self.write_json(validate_style_document(self.read_json_body(max_bytes=STYLE_VALIDATE_MAX_BODY_BYTES)))
            return
        self.write_error("not_found", "Not found", HTTPStatus.NOT_FOUND)

    def read_json_body(self, allow_empty: bool = False, max_bytes: int | None = None) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if max_bytes is not None and length > max_bytes:
            raise APIError(
                "payload_too_large",
                f"JSON request body exceeds {max_bytes} bytes.",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"max_bytes": max_bytes},
            )
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
                immutable = ", immutable" if cache_seconds >= 31536000 else ", must-revalidate"
                self.send_header("Cache-Control", f"public, max-age={cache_seconds}{immutable}")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        if cache_seconds:
            immutable = ", immutable" if cache_seconds >= 31536000 else ", must-revalidate"
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
