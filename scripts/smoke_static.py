from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from validate_published import validate_style_sources


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def http_request(url: str, *, timeout: float = 10.0, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes, float, str]:
    started = time.perf_counter()
    req = Request(url, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read(), (time.perf_counter() - started) * 1000, ""
    except HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read(), (time.perf_counter() - started) * 1000, str(exc)
    except URLError as exc:
        return 0, {}, b"", (time.perf_counter() - started) * 1000, str(exc)


def header_value(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def cache_ok(cache_control: str, expected: str) -> bool:
    expected_parts = [part.strip() for part in expected.split(",")]
    return all(part in cache_control for part in expected_parts)


def run_smoke(
    base_url: str,
    region: str,
    timeout: float,
    *,
    strict_cache: bool = False,
    style_api_base_url: str | None = None,
    style: str = "light",
    lang: str = "en",
) -> list[CheckResult]:
    base_url = base_url.rstrip("/")
    checks: list[CheckResult] = []
    manifest_url = f"{base_url}/manifests/{region}.json"
    status, headers, body, elapsed_ms, error = http_request(manifest_url, timeout=timeout)
    if status != 200:
        checks.append(CheckResult("manifest", False, f"status={status} elapsed_ms={elapsed_ms:.1f} error={error}"))
        return checks
    try:
        manifest = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        checks.append(CheckResult("manifest", False, f"invalid_json={exc}"))
        return checks
    manifest_cache = header_value(headers, "Cache-Control")
    manifest_cache_valid = cache_ok(manifest_cache, "public, max-age=60, must-revalidate")
    checks.append(CheckResult(
        "manifest",
        (not strict_cache or manifest_cache_valid),
        f"status=200 cache_control={manifest_cache or '-'} url={manifest_url}",
    ))

    tilesets = manifest.get("tilesets", {})
    if not isinstance(tilesets, dict) or not tilesets:
        checks.append(CheckResult("tilesets", False, "manifest has no tilesets"))
        return checks
    for tileset_id, tileset in sorted(tilesets.items()):
        key = tileset.get("key") if isinstance(tileset, dict) else None
        if not key:
            checks.append(CheckResult(f"pmtiles_{tileset_id}", False, "missing key"))
            continue
        tile_url = f"{base_url}/{key}"
        tile_status, tile_headers, tile_body, tile_elapsed, tile_error = http_request(tile_url, timeout=timeout, headers={"Range": "bytes=0-1023"})
        content_range = header_value(tile_headers, "Content-Range")
        content_length = header_value(tile_headers, "Content-Length")
        tile_cache = header_value(tile_headers, "Cache-Control")
        tile_cache_valid = cache_ok(tile_cache, "public, max-age=31536000, immutable")
        range_ok = tile_status in {200, 206} and bool(tile_body) and (tile_status == 200 or content_range.lower().startswith("bytes "))
        checks.append(CheckResult(
            f"pmtiles_{tileset_id}",
            range_ok and (not strict_cache or tile_cache_valid),
            f"status={tile_status} bytes={len(tile_body)} content_length={content_length or '-'} content_range={content_range or '-'} cache_control={tile_cache or '-'} elapsed_ms={tile_elapsed:.1f} url={tile_url} error={tile_error}",
        ))
    if style_api_base_url:
        style_url = style_api_base_url.rstrip("/") + f"/api/style/{region}.json?style={style}&lang={lang}"
        style_check = validate_style_sources(style_url, public_base_url=base_url, timeout=timeout)
        checks.append(CheckResult(
            "style_pmtiles_sources",
            style_check["ok"],
            f"status={style_check['status_code']} sources={style_check['sources']} failures={style_check['failures']}",
        ))
    return checks


def print_summary(checks: list[CheckResult]) -> None:
    passed = sum(1 for check in checks if check.ok)
    failed = len(checks) - passed
    print(f"Static smoke summary: PASS={passed} FAIL={failed}")
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.name}: {check.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe TavrixMap static PMTiles smoke checks.")
    parser.add_argument("--base-url", default="http://localhost:8088")
    parser.add_argument("--region", default="iraq")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--strict-cache", action="store_true")
    parser.add_argument("--style-api-base-url")
    parser.add_argument("--style", default="light")
    parser.add_argument("--lang", default="en")
    args = parser.parse_args(argv or sys.argv[1:])
    checks = run_smoke(
        args.base_url,
        args.region,
        args.timeout,
        strict_cache=args.strict_cache,
        style_api_base_url=args.style_api_base_url,
        style=args.style,
        lang=args.lang,
    )
    print_summary(checks)
    return 0 if checks and all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
