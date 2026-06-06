from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.parse import urljoin

import requests


PMTILES_CONTENT_TYPES = {"application/vnd.pmtiles", "application/octet-stream", "binary/octet-stream"}


def _cache_header(headers: dict[str, str] | Any) -> str:
    getter = headers.get if hasattr(headers, "get") else lambda _key, _default="": ""
    return str(getter("Cache-Control", getter("cache-control", "")) or "")


def _content_type(headers: dict[str, str] | Any) -> str:
    getter = headers.get if hasattr(headers, "get") else lambda _key, _default="": ""
    return str(getter("Content-Type", getter("content-type", "")) or "").split(";")[0].strip().lower()


def _content_length(headers: dict[str, str] | Any, body: bytes) -> int:
    getter = headers.get if hasattr(headers, "get") else lambda _key, _default="": ""
    raw = getter("Content-Length", getter("content-length", ""))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return len(body)


def validate_pmtiles_url(url: str, *, session: Any = requests, timeout: float = 20.0, strict_cache: bool = False) -> dict[str, Any]:
    response = session.get(url, headers={"Range": "bytes=0-1023"}, timeout=timeout)
    body = response.content or b""
    headers = response.headers
    status = int(response.status_code)
    content_range = str(headers.get("Content-Range", headers.get("content-range", "")) or "")
    content_length = _content_length(headers, body)
    cache_control = _cache_header(headers)
    content_type = _content_type(headers)
    failures: list[str] = []
    warnings: list[str] = []

    if status not in (200, 206):
        failures.append(f"PMTiles URL returned {status}, expected 200 or 206.")
    if not body:
        failures.append("PMTiles range response body is empty.")
    if status == 206 and not content_range.lower().startswith("bytes "):
        failures.append("PMTiles 206 response is missing a sane Content-Range header.")
    if content_length <= 0:
        failures.append("PMTiles response Content-Length is missing or zero.")
    if content_type and content_type not in PMTILES_CONTENT_TYPES:
        warnings.append(f"PMTiles Content-Type is {content_type!r}; expected application/vnd.pmtiles or application/octet-stream.")
    if strict_cache and ("immutable" not in cache_control or "max-age=31536000" not in cache_control):
        failures.append("PMTiles Cache-Control must include public, max-age=31536000, immutable.")

    return {
        "ok": not failures,
        "url": url,
        "status_code": status,
        "content_length": content_length,
        "content_range": content_range or None,
        "cache_control": cache_control or None,
        "content_type": content_type or None,
        "failures": failures,
        "warnings": warnings,
    }


def validate_manifest_url(url: str, *, session: Any = requests, timeout: float = 20.0, strict_cache: bool = False) -> dict[str, Any]:
    response = session.get(url, timeout=timeout)
    headers = response.headers
    cache_control = _cache_header(headers)
    failures: list[str] = []
    warnings: list[str] = []
    payload: dict[str, Any] | None = None

    if response.status_code != 200:
        failures.append(f"Manifest URL returned {response.status_code}, expected 200.")
    try:
        payload = response.json()
    except ValueError:
        failures.append("Manifest URL did not return valid JSON.")
    if payload is not None and not isinstance(payload.get("tilesets"), dict):
        failures.append("Manifest JSON does not contain a tilesets object.")
    content_type = _content_type(headers)
    if content_type and content_type != "application/json":
        warnings.append(f"Manifest Content-Type is {content_type!r}; expected application/json.")
    if strict_cache and ("max-age=60" not in cache_control or "must-revalidate" not in cache_control):
        failures.append("Manifest Cache-Control must include public, max-age=60, must-revalidate.")

    return {
        "ok": not failures,
        "url": url,
        "status_code": int(response.status_code),
        "cache_control": cache_control or None,
        "content_type": content_type or None,
        "manifest": payload,
        "failures": failures,
        "warnings": warnings,
    }


def pmtiles_urls_from_manifest(manifest: dict[str, Any], manifest_url: str | None = None) -> list[str]:
    urls: list[str] = []
    for tileset in manifest.get("tilesets", {}).values():
        if not isinstance(tileset, dict):
            continue
        url = str(tileset.get("url") or "")
        if url.startswith(("http://", "https://")):
            urls.append(url)
            continue
        key = str(tileset.get("key") or "")
        if manifest_url and key:
            urls.append(urljoin(manifest_url, "../" + key))
    return urls


def validate_style_sources(
    style_url: str,
    *,
    public_base_url: str | None = None,
    session: Any = requests,
    timeout: float = 20.0,
) -> dict[str, Any]:
    response = session.get(style_url, timeout=timeout)
    failures: list[str] = []
    sources: dict[str, str] = {}
    if response.status_code != 200:
        failures.append(f"Style URL returned {response.status_code}, expected 200.")
        payload: dict[str, Any] = {}
    else:
        try:
            payload = response.json()
        except ValueError:
            failures.append("Style URL did not return valid JSON.")
            payload = {}

    for name, source in (payload.get("sources") or {}).items():
        if not isinstance(source, dict):
            continue
        raw_url = str(source.get("url") or "")
        if not raw_url.startswith("pmtiles://"):
            continue
        pmtiles_url = raw_url.removeprefix("pmtiles://")
        sources[str(name)] = pmtiles_url
        if not pmtiles_url.startswith(("http://", "https://")):
            failures.append(f"Style source {name} does not point to a public HTTP(S) PMTiles URL.")
        if public_base_url and not pmtiles_url.startswith(public_base_url.rstrip("/") + "/"):
            failures.append(f"Style source {name} does not point at configured public base URL {public_base_url}.")

    if not sources:
        failures.append("Style did not contain any pmtiles:// HTTP(S) sources.")
    return {
        "ok": not failures,
        "url": style_url,
        "status_code": int(response.status_code),
        "sources": sources,
        "failures": failures,
        "warnings": [],
    }


def validate_published(
    manifest_url: str,
    *,
    style_url: str | None = None,
    public_base_url: str | None = None,
    strict_cache: bool = False,
    timeout: float = 20.0,
    session: Any = requests,
) -> dict[str, Any]:
    manifest_check = validate_manifest_url(manifest_url, session=session, timeout=timeout, strict_cache=strict_cache)
    pmtiles_checks = []
    if manifest_check.get("manifest"):
        for url in pmtiles_urls_from_manifest(manifest_check["manifest"], manifest_url):
            pmtiles_checks.append(validate_pmtiles_url(url, session=session, timeout=timeout, strict_cache=strict_cache))
    style_check = None
    if style_url:
        style_check = validate_style_sources(style_url, public_base_url=public_base_url, session=session, timeout=timeout)
    checks = [manifest_check, *pmtiles_checks]
    if style_check:
        checks.append(style_check)
    return {
        "ok": bool(checks) and all(check["ok"] for check in checks),
        "manifest": manifest_check,
        "pmtiles": pmtiles_checks,
        "style": style_check,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate published PMTiles manifest and CDN/static range behavior.")
    parser.add_argument("--manifest-url")
    parser.add_argument("--static-base-url")
    parser.add_argument("--region", default="iraq")
    parser.add_argument("--style-url")
    parser.add_argument("--public-base-url")
    parser.add_argument("--strict-cache", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    manifest_url = args.manifest_url
    if not manifest_url and args.static_base_url:
        manifest_url = args.static_base_url.rstrip("/") + f"/manifests/{args.region}.json"
    if not manifest_url:
        raise SystemExit("--manifest-url or --static-base-url is required")
    result = validate_published(
        manifest_url,
        style_url=args.style_url,
        public_base_url=args.public_base_url,
        strict_cache=args.strict_cache,
        timeout=args.timeout,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
