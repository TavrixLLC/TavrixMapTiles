from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests


INTERNAL_URL_MARKERS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "static:80",
    "host.docker.internal",
    "tavrixmaptiles-map-api",
    "map-api:8090",
    "pmtiles-builder",
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    skipped: bool = False


def header_value(headers: Any, name: str) -> str:
    getter = headers.get if hasattr(headers, "get") else lambda _name, _default="": ""
    return str(getter(name, getter(name.lower(), "")) or "")


def cache_contains(cache_control: str, *parts: str) -> bool:
    return all(part in cache_control for part in parts)


def request_json(
    session: requests.Session,
    url: str,
    *,
    token: str | None = None,
    request_id: str | None = None,
    timeout: float = 20.0,
) -> tuple[int, dict[str, str], Any, str]:
    headers = {}
    if token:
        headers["X-Tavrix-Token"] = token
    if request_id:
        headers["X-Request-ID"] = request_id
    response = session.get(url, headers=headers, timeout=timeout)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, dict(response.headers), payload, response.text[:500]


def collect_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(collect_strings(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(collect_strings(item))
        return result
    return []


def style_pmtiles_sources(style: dict[str, Any]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for name, source in (style.get("sources") or {}).items():
        if not isinstance(source, dict):
            continue
        raw = str(source.get("url") or "")
        if raw.startswith("pmtiles://"):
            sources[str(name)] = raw.removeprefix("pmtiles://")
    return sources


def style_url(gateway_base_url: str, region: str, style: str, lang: str) -> str:
    query = urlencode({"style": style, "lang": lang})
    return gateway_base_url.rstrip("/") + f"/style/{region}.json?{query}"


def manifest_url(tiles_base_url: str, region: str) -> str:
    return tiles_base_url.rstrip("/") + f"/manifests/{region}.json"


def extract_gateway_map_load_total(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    candidates = [
        payload.get("map_loads"),
        (payload.get("usage") or {}).get("map_loads") if isinstance(payload.get("usage"), dict) else None,
        (payload.get("totals") or {}).get("map_loads") if isinstance(payload.get("totals"), dict) else None,
    ]
    for candidate in candidates:
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def fetch_gateway_usage_count(session: requests.Session, url: str, token: str, timeout: float) -> tuple[int | None, str]:
    status, _headers, payload, text = request_json(session, url, token=token, timeout=timeout)
    if status != 200:
        return None, f"usage endpoint status={status} body={text}"
    count = extract_gateway_map_load_total(payload)
    if count is None:
        return None, f"usage endpoint did not expose map_loads-compatible JSON: {payload}"
    return count, f"map_loads={count}"


def validate_style_payload(style_payload: Any, tiles_base_url: str) -> tuple[bool, str]:
    if not isinstance(style_payload, dict):
        return False, "style payload is not JSON object"
    sources = style_pmtiles_sources(style_payload)
    failures = []
    if not sources:
        failures.append("no pmtiles:// sources found")
    for source_name, url in sources.items():
        if not url.startswith(tiles_base_url.rstrip("/") + "/"):
            failures.append(f"source {source_name} points to {url}, expected {tiles_base_url}")
        if url.startswith("http://"):
            failures.append(f"source {source_name} uses http: {url}")
    for value in collect_strings(style_payload):
        lowered = value.lower()
        if any(marker in lowered for marker in INTERNAL_URL_MARKERS):
            failures.append(f"style contains internal/local URL value: {value}")
    return not failures, f"sources={sources} failures={failures}"


def validate_static(session: requests.Session, tiles_base_url: str, region: str, timeout: float) -> list[CheckResult]:
    checks: list[CheckResult] = []
    url = manifest_url(tiles_base_url, region)
    response = session.get(url, timeout=timeout)
    manifest_cache = header_value(response.headers, "Cache-Control")
    manifest_ok = response.status_code == 200 and cache_contains(manifest_cache, "max-age=60", "must-revalidate")
    checks.append(CheckResult("static_manifest_short_cache", manifest_ok, f"status={response.status_code} cache={manifest_cache or '-'} url={url}"))
    try:
        manifest = response.json()
    except ValueError:
        checks.append(CheckResult("static_manifest_json", False, "manifest is not JSON"))
        return checks
    tilesets = manifest.get("tilesets") if isinstance(manifest, dict) else None
    if not isinstance(tilesets, dict) or not tilesets:
        checks.append(CheckResult("static_manifest_tilesets", False, "manifest has no tilesets"))
        return checks
    for tileset_id, tileset in sorted(tilesets.items()):
        if not isinstance(tileset, dict) or not tileset.get("key"):
            checks.append(CheckResult(f"static_pmtiles_{tileset_id}", False, "tileset missing key"))
            continue
        pmtiles_url = tiles_base_url.rstrip("/") + "/" + str(tileset["key"]).lstrip("/")
        tile_response = session.get(pmtiles_url, headers={"Range": "bytes=0-1023"}, timeout=timeout)
        cache = header_value(tile_response.headers, "Cache-Control")
        content_range = header_value(tile_response.headers, "Content-Range")
        immutable_ok = cache_contains(cache, "max-age=31536000", "immutable")
        range_ok = tile_response.status_code in {200, 206} and bool(tile_response.content)
        if tile_response.status_code == 206:
            range_ok = range_ok and content_range.lower().startswith("bytes ")
        checks.append(CheckResult(
            f"static_pmtiles_{tileset_id}",
            range_ok and immutable_ok,
            f"status={tile_response.status_code} bytes={len(tile_response.content)} cache={cache or '-'} content_range={content_range or '-'} url={pmtiles_url}",
        ))
    return checks


def validate_direct_blocked(session: requests.Session, internal_map_api: str, region: str, style: str, timeout: float) -> list[CheckResult]:
    base = internal_map_api.rstrip("/")
    paths = (
        f"/api/vector/{region}/12/2553/1645.pbf",
        f"/api/raster/{region}/12/2553/1645.png?style={style}",
    )
    checks: list[CheckResult] = []
    for path in paths:
        url = base + path
        response = session.get(url, timeout=timeout)
        ok = response.status_code in {403, 404}
        checks.append(CheckResult(f"direct_blocked_{'vector' if 'vector' in path else 'raster'}", ok, f"status={response.status_code} url={url}"))
    return checks


def run_smoke(args: argparse.Namespace) -> list[CheckResult]:
    session = requests.Session()
    checks: list[CheckResult] = []
    gateway_base = args.gateway_base_url.rstrip("/")
    tiles_base = args.tiles_base_url.rstrip("/")

    before_usage: int | None = None
    if args.usage_url:
        before_usage, detail = fetch_gateway_usage_count(session, args.usage_url, args.valid_token, args.timeout)
        checks.append(CheckResult("gateway_usage_before", before_usage is not None, detail))
    else:
        checks.append(CheckResult(
            "gateway_usage_counter_hook",
            True,
            "skipped: provide --usage-url to verify optional Gateway-owned map_load counter behavior",
            skipped=True,
        ))

    request_id = "tavrix-smoke-" + uuid.uuid4().hex
    status, headers, payload, text = request_json(
        session,
        style_url(gateway_base, args.region, args.style, "en"),
        token=args.valid_token,
        request_id=request_id,
        timeout=args.timeout,
    )
    checks.append(CheckResult("gateway_valid_style_en", status == 200 and isinstance(payload, dict), f"status={status} body={text[:120]}"))
    returned_request_id = header_value(headers, "X-Request-ID")
    checks.append(CheckResult("request_id_preserved", returned_request_id == request_id, f"sent={request_id} returned={returned_request_id or '-'}"))
    style_ok, style_detail = validate_style_payload(payload, tiles_base)
    checks.append(CheckResult("style_sources_public_static", style_ok, style_detail))

    if args.usage_url and before_usage is not None:
        after_valid, detail = fetch_gateway_usage_count(session, args.usage_url, args.valid_token, args.timeout)
        checks.append(CheckResult("gateway_usage_valid_style_incremented_once", after_valid == before_usage + 1, f"before={before_usage} after={after_valid} {detail}"))
    else:
        after_valid = before_usage

    invalid_status, _headers, _payload, invalid_text = request_json(
        session,
        style_url(gateway_base, args.region, args.style, "en"),
        token=args.invalid_token,
        request_id="tavrix-smoke-invalid-" + uuid.uuid4().hex,
        timeout=args.timeout,
    )
    checks.append(CheckResult("gateway_invalid_token_blocked", invalid_status in {401, 403}, f"status={invalid_status} body={invalid_text[:120]}"))

    unsupported_status, _headers, _payload, unsupported_text = request_json(
        session,
        style_url(gateway_base, args.region, args.style, "zz"),
        token=args.valid_token,
        request_id="tavrix-smoke-unsupported-lang-" + uuid.uuid4().hex,
        timeout=args.timeout,
    )
    checks.append(CheckResult("unsupported_lang_rejected", unsupported_status == 400, f"status={unsupported_status} body={unsupported_text[:120]}"))

    if args.usage_url and after_valid is not None:
        after_invalid, detail = fetch_gateway_usage_count(session, args.usage_url, args.valid_token, args.timeout)
        checks.append(CheckResult("gateway_usage_invalid_and_400_not_counted", after_invalid == after_valid, f"after_valid={after_valid} after_invalid={after_invalid} {detail}"))

    for lang in ("ar", "ku"):
        lang_status, _headers, lang_payload, lang_text = request_json(
            session,
            style_url(gateway_base, args.region, args.style, lang),
            token=args.valid_token,
            request_id=f"tavrix-smoke-{lang}-" + uuid.uuid4().hex,
            timeout=args.timeout,
        )
        lang_style_ok, lang_style_detail = validate_style_payload(lang_payload, tiles_base)
        checks.append(CheckResult(f"gateway_valid_style_{lang}", lang_status == 200 and lang_style_ok, f"status={lang_status} {lang_style_detail} body={lang_text[:120]}"))

    checks.extend(validate_direct_blocked(session, args.internal_map_api, args.region, args.style, args.timeout))
    checks.extend(validate_static(session, tiles_base, args.region, args.timeout))
    return checks


def print_summary(checks: list[CheckResult]) -> None:
    passed = sum(1 for check in checks if check.ok and not check.skipped)
    skipped = sum(1 for check in checks if check.skipped)
    failed = sum(1 for check in checks if not check.ok)
    print(f"Single-server production smoke summary: PASS={passed} FAIL={failed} SKIP={skipped}")
    for check in checks:
        status = "SKIP" if check.skipped else ("PASS" if check.ok else "FAIL")
        print(f"{status} {check.name}: {check.detail}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke TavrixMap Tiles Phase 1 single-server production behind Gateway.")
    parser.add_argument("--gateway-base-url", default="https://api.tavrix.com/maps")
    parser.add_argument("--tiles-base-url", default="https://tiles.tavrix.com")
    parser.add_argument("--internal-map-api", default="http://localhost:8090")
    parser.add_argument("--valid-token", required=True)
    parser.add_argument("--invalid-token", default="invalid-token")
    parser.add_argument("--region", default="iraq")
    parser.add_argument("--style", default="light")
    parser.add_argument("--usage-url", help="Optional Gateway-owned usage-counter test hook returning map_loads-compatible JSON. Tiles does not own this counter.")
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    started = time.perf_counter()
    args = parse_args(argv or sys.argv[1:])
    checks = run_smoke(args)
    print_summary(checks)
    print(f"elapsed_seconds={(time.perf_counter() - started):.2f}")
    return 0 if checks and all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
