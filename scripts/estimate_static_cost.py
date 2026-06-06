from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROFILE_PRICE_PER_GB = {
    "vps": 0.0,
    "r2": 0.0,
    "bunny_mea": 0.06,
    "bunny_asia": 0.03,
    "bunny_eu_us": 0.01,
}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[index]


def parse_log_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def estimate_static_cost(
    log_path: Path,
    *,
    provider_profile: str,
    days_sampled: float,
    price_per_gb: float | None = None,
) -> dict[str, Any]:
    if days_sampled <= 0:
        raise ValueError("--days-sampled must be greater than zero")
    price = PROFILE_PRICE_PER_GB[provider_profile] if price_per_gb is None else price_per_gb
    total_requests = 0
    total_bytes = 0
    status_counts: Counter[str] = Counter()
    range_requests = 0
    pmtiles_requests = 0
    request_times: list[float] = []
    malformed_lines = 0

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            row = parse_log_line(line)
            if row is None:
                malformed_lines += 1
                continue
            total_requests += 1
            uri = str(row.get("uri") or "")
            status_counts[str(row.get("status") or "")] += 1
            sent = int(numeric(row.get("bytes_sent") or row.get("body_bytes_sent")))
            total_bytes += max(sent, 0)
            if str(row.get("http_range") or ""):
                range_requests += 1
            if uri.endswith(".pmtiles") or "/tiles/" in uri:
                pmtiles_requests += 1
            elapsed = numeric(row.get("request_time"))
            if elapsed >= 0:
                request_times.append(elapsed)

    bytes_per_day = total_bytes / days_sampled
    monthly_bytes = bytes_per_day * 30.0
    monthly_gb = monthly_bytes / (1024 ** 3)
    requests_per_day = total_requests / days_sampled
    monthly_requests = requests_per_day * 30.0
    avg_bytes = total_bytes / total_requests if total_requests else 0
    return {
        "ok": True,
        "log_path": str(log_path),
        "provider_profile": provider_profile,
        "price_per_gb_usd": price,
        "cost_note": "Infrastructure estimate only; override --price-per-gb with the current provider rate.",
        "days_sampled": days_sampled,
        "total_requests": total_requests,
        "pmtiles_requests": pmtiles_requests,
        "range_requests": range_requests,
        "total_bytes": total_bytes,
        "avg_bytes_per_request": avg_bytes,
        "status_counts": dict(status_counts),
        "static_response_time_seconds": {
            "avg": statistics.fmean(request_times) if request_times else 0.0,
            "p95": percentile(request_times, 95),
            "p99": percentile(request_times, 99),
        },
        "projected_monthly_requests": monthly_requests,
        "projected_monthly_bandwidth_gb": monthly_gb,
        "projected_monthly_provider_cost_usd": monthly_gb * price,
        "malformed_lines": malformed_lines,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate static PMTiles bandwidth and provider cost from Nginx JSON logs.")
    parser.add_argument("--log", required=True, help="Path to tavrix-tiles.access.log.")
    parser.add_argument("--provider-profile", choices=sorted(PROFILE_PRICE_PER_GB), default="vps")
    parser.add_argument("--days-sampled", type=float, default=1.0)
    parser.add_argument("--price-per-gb", type=float, help="Override the profile price per GB.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = estimate_static_cost(
        Path(args.log),
        provider_profile=args.provider_profile,
        days_sampled=args.days_sampled,
        price_per_gb=args.price_per_gb,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
