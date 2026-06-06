from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass
class Sample:
    path: str
    status: int
    elapsed_ms: float
    error: str = ""


def http_get(base_url: str, path: str, timeout: float) -> Sample:
    started = time.perf_counter()
    req = Request(f"{base_url.rstrip('/')}{path}")
    try:
        with urlopen(req, timeout=timeout) as response:
            response.read()
            return Sample(path, response.status, (time.perf_counter() - started) * 1000)
    except HTTPError as exc:
        exc.read()
        return Sample(path, exc.code, (time.perf_counter() - started) * 1000, str(exc))
    except URLError as exc:
        return Sample(path, 0, (time.perf_counter() - started) * 1000, str(exc))


def get_json(base_url: str, path: str, timeout: float) -> dict[str, Any]:
    sample = http_get(base_url, path, timeout)
    if sample.status not in {200, 503}:
        return {}
    req = Request(f"{base_url.rstrip('/')}{path}")
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((percentile_value / 100) * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def discover_direct_policy(base_url: str, timeout: float) -> bool:
    for path in ("/api/health/ready", "/api/health"):
        payload = get_json(base_url, path, timeout)
        policy = payload.get("direct_tile_policy") if isinstance(payload, dict) else None
        if isinstance(policy, dict):
            return bool(policy.get("public_effective"))
    return False


def run_load(args: argparse.Namespace) -> list[Sample]:
    paths = [
        f"/api/style/{args.region}.json?style={args.style}&lang={args.lang}",
        f"/api/manifest/{args.region}.json",
    ]
    if args.include_direct or (not args.no_direct and discover_direct_policy(args.base_url, args.timeout)):
        paths.append(args.tile_path.replace("{region}", args.region))

    scheduled = [paths[index % len(paths)] for index in range(args.requests)]
    samples: list[Sample] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(http_get, args.base_url, path, args.timeout) for path in scheduled]
        for future in as_completed(futures):
            samples.append(future.result())
    return samples


def print_summary(samples: list[Sample]) -> None:
    latencies = [sample.elapsed_ms for sample in samples]
    status_counts = Counter(str(sample.status) for sample in samples)
    path_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for sample in samples:
        path_counts[sample.path][str(sample.status)] += 1
    error_count = sum(1 for sample in samples if sample.status == 0 or sample.status >= 400)

    print("Load smoke summary")
    print(f"requests={len(samples)} errors={error_count} p50_ms={percentile(latencies, 50)} p95_ms={percentile(latencies, 95)} p99_ms={percentile(latencies, 99)}")
    print(f"status_counts={dict(sorted(status_counts.items()))}")
    for path, counts in sorted(path_counts.items()):
        print(f"path={path} status_counts={dict(sorted(counts.items()))}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Small safe TavrixMap Tiles load smoke.")
    parser.add_argument("--base-url", default="http://localhost:8090")
    parser.add_argument("--region", default="iraq")
    parser.add_argument("--style", default="light")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--tile-path", default="/api/vector/{region}/12/2553/1645.pbf")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--include-direct", action="store_true")
    parser.add_argument("--no-direct", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.requests <= 0 or args.concurrency <= 0:
        print("--requests and --concurrency must be positive", file=sys.stderr)
        return 2
    samples = run_load(args)
    print_summary(samples)
    return 0 if samples and all(sample.status and sample.status < 500 for sample in samples) else 1


if __name__ == "__main__":
    raise SystemExit(main())
