from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_TILE_PATH = "/api/vector/iraq/12/2553/1645.pbf"


@dataclass
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    error: str = ""

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def http_request(url: str, *, timeout: float = 10.0, headers: dict[str, str] | None = None) -> HttpResult:
    started = time.perf_counter()
    req = Request(url, headers=headers or {})
    try:
        with urlopen(req, timeout=timeout) as response:
            body = response.read()
            return HttpResult(response.status, dict(response.headers.items()), body, (time.perf_counter() - started) * 1000)
    except HTTPError as exc:
        return HttpResult(exc.code, dict(exc.headers.items()), exc.read(), (time.perf_counter() - started) * 1000, str(exc))
    except URLError as exc:
        return HttpResult(0, {}, b"", (time.perf_counter() - started) * 1000, str(exc))


def direct_tile_allowed_for_profile(profile: str, public_effective: bool | None) -> bool:
    if profile == "development":
        return True
    if profile == "production":
        return False
    return bool(public_effective)


def direct_tile_status_ok(status: int, profile: str, public_effective: bool | None) -> bool:
    if direct_tile_allowed_for_profile(profile, public_effective):
        return status in {200, 204, 404}
    return status in {403, 404}


def pmtiles_url_from_style(style: dict[str, Any]) -> str | None:
    sources = style.get("sources", {})
    if not isinstance(sources, dict):
        return None
    for source in sources.values():
        if not isinstance(source, dict):
            continue
        raw_url = str(source.get("url") or "")
        if raw_url.startswith("pmtiles://http://") or raw_url.startswith("pmtiles://https://"):
            return raw_url.removeprefix("pmtiles://")
    return None


def check_json_endpoint(name: str, url: str, expected_statuses: set[int], timeout: float) -> tuple[CheckResult, dict[str, Any] | None]:
    result = http_request(url, timeout=timeout)
    try:
        payload = result.json() if result.body else {}
    except json.JSONDecodeError:
        payload = None
    ok = result.status in expected_statuses and isinstance(payload, dict)
    detail = f"status={result.status} elapsed_ms={result.elapsed_ms:.1f}"
    if result.error and result.status == 0:
        detail += f" error={result.error}"
    return CheckResult(name, ok, detail), payload


def run_smoke(args: argparse.Namespace) -> list[CheckResult]:
    base_url = args.base_url.rstrip("/")
    checks: list[CheckResult] = []

    live, live_payload = check_json_endpoint("health_live", f"{base_url}/api/health/live", {200}, args.timeout)
    checks.append(live)

    ready_statuses = {200} if args.require_ready else {200, 503}
    ready, ready_payload = check_json_endpoint("health_ready", f"{base_url}/api/health/ready", ready_statuses, args.timeout)
    if args.require_ready and isinstance(ready_payload, dict):
        ready.ok = ready.ok and ready_payload.get("ok") is True
    checks.append(ready)

    health, health_payload = check_json_endpoint("health_basic", f"{base_url}/api/health", {200}, args.timeout)
    checks.append(health)

    openapi, _openapi_payload = check_json_endpoint("openapi", f"{base_url}/api/openapi.json", {200}, args.timeout)
    checks.append(openapi)

    style_path = f"/api/style/{args.region}.json?style={args.style}&lang={args.lang}"
    style_check, style_payload = check_json_endpoint("style", f"{base_url}{style_path}", {200}, args.timeout)
    checks.append(style_check)

    manifest_check, manifest_payload = check_json_endpoint("manifest", f"{base_url}/api/manifest/{args.region}.json", {200}, args.timeout)
    checks.append(manifest_check)

    public_effective = None
    for payload in (ready_payload, health_payload):
        if isinstance(payload, dict) and isinstance(payload.get("direct_tile_policy"), dict):
            public_effective = bool(payload["direct_tile_policy"].get("public_effective"))
            break

    tile_path = args.tile_path.replace("{region}", args.region)
    direct = http_request(f"{base_url}{tile_path}", timeout=args.timeout)
    direct_expected = "public" if direct_tile_allowed_for_profile(args.profile, public_effective) else "blocked"
    checks.append(CheckResult(
        "direct_tile_policy",
        direct_tile_status_ok(direct.status, args.profile, public_effective),
        f"status={direct.status} expected={direct_expected} elapsed_ms={direct.elapsed_ms:.1f}",
    ))

    if not args.skip_static:
        static_url = None
        if isinstance(style_payload, dict):
            static_url = pmtiles_url_from_style(style_payload)
        if not static_url and isinstance(manifest_payload, dict):
            tilesets = manifest_payload.get("tilesets", {})
            if isinstance(tilesets, dict):
                for tileset in tilesets.values():
                    if isinstance(tileset, dict) and str(tileset.get("url", "")).startswith(("http://", "https://")):
                        static_url = str(tileset["url"])
                        break
        if static_url:
            parsed = urlparse(static_url)
            if parsed.scheme in {"http", "https"}:
                static_result = http_request(static_url, timeout=args.timeout, headers={"Range": "bytes=0-15"})
                checks.append(CheckResult(
                    "static_pmtiles_range",
                    static_result.status in {200, 206},
                    f"status={static_result.status} url={static_url}",
                ))
            else:
                checks.append(CheckResult("static_pmtiles_range", False, f"unsupported_url={static_url}"))
        else:
            checks.append(CheckResult("static_pmtiles_range", False, "no PMTiles HTTP URL found in style or manifest"))

    return checks


def print_summary(checks: list[CheckResult]) -> None:
    passed = sum(1 for check in checks if check.ok)
    failed = len(checks) - passed
    print(f"Smoke summary: PASS={passed} FAIL={failed}")
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"{status} {check.name}: {check.detail}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safe TavrixMap Tiles API smoke checks.")
    parser.add_argument("--base-url", default="http://localhost:8090")
    parser.add_argument("--region", default="iraq")
    parser.add_argument("--style", default="light")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--profile", choices=["auto", "development", "production"], default="auto")
    parser.add_argument("--tile-path", default=DEFAULT_TILE_PATH)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--require-ready", action="store_true")
    parser.add_argument("--skip-static", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    checks = run_smoke(args)
    print_summary(checks)
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
