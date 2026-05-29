from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(os.getenv("APP_ROOT", Path(__file__).resolve().parents[1])).resolve()
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", ROOT_DIR / "config")).resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", ROOT_DIR / "output")).resolve()
TMP_DIR = Path(os.getenv("TMP_DIR", ROOT_DIR / "tmp")).resolve()
LOG_DIR = Path(os.getenv("LOG_DIR", ROOT_DIR / "logs")).resolve()

PMTILES_CACHE_CONTROL = "public, max-age=31536000, immutable"
MANIFEST_CACHE_CONTROL = "public, max-age=60, must-revalidate"


def ensure_dirs() -> None:
    for path in (CONFIG_DIR, OUTPUT_DIR, TMP_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def setup_logging(name: str) -> logging.Logger:
    ensure_dirs()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)

    file_handler = logging.FileHandler(LOG_DIR / f"{name}.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
    tmp_path.replace(path)


def load_config(filename: str) -> dict[str, Any]:
    return load_json(CONFIG_DIR / filename)


def get_region(region: str) -> dict[str, Any]:
    regions = load_config("regions.json")["regions"]
    if region not in regions:
        raise KeyError(f"Unknown region '{region}'. Add it to config/regions.json.")
    return regions[region]


def get_group(target: str) -> dict[str, Any]:
    groups = load_config("layers.json")["groups"]
    if target not in groups:
        raise KeyError(f"Unknown target '{target}'. Expected one of: {', '.join(groups)}.")
    return groups[target]


def bbox_csv(region_config: dict[str, Any]) -> str:
    bbox = region_config["bbox"]
    if len(bbox) != 4:
        raise ValueError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
    return ", ".join(str(value) for value in bbox)


def render_sql(sql: str, region_name: str, region_config: dict[str, Any]) -> str:
    return sql.replace("{bbox}", bbox_csv(region_config)).replace("{region}", region_name)


def timestamp_id(dt: datetime | None = None) -> str:
    value = dt or datetime.now(timezone.utc)
    return value.strftime("%Y%m%d-%H%M")


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pg_conn_kwargs() -> dict[str, Any]:
    return {
        "host": os.getenv("POSTGIS_HOST", "localhost"),
        "port": int(os.getenv("POSTGIS_PORT", "5432")),
        "dbname": os.getenv("POSTGIS_DB", "gis"),
        "user": os.getenv("POSTGIS_USER", "pmtiles_builder"),
        "password": os.getenv("POSTGIS_PASSWORD", ""),
        "connect_timeout": 15,
    }


def ogr_pg_dsn() -> str:
    cfg = pg_conn_kwargs()
    parts = [
        f"host={cfg['host']}",
        f"port={cfg['port']}",
        f"dbname={cfg['dbname']}",
        f"user={cfg['user']}",
    ]
    if cfg.get("password"):
        parts.append(f"password={cfg['password']}")
    return "PG:" + " ".join(parts)


def redact(value: Any) -> Any:
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    password = os.getenv("POSTGIS_PASSWORD", "")
    secret = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    redacted = value
    for token in (password, secret):
        if token:
            redacted = redacted.replace(token, "***")
    return redacted


def run_command(
    cmd: list[str],
    logger: logging.Logger,
    cwd: Path | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    logger.info("Running: %s", " ".join(str(redact(part)) for part in cmd))
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        text=True,
        capture_output=capture_output,
    )
    if result.returncode != 0:
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        message = f"Command failed with exit code {result.returncode}: {' '.join(cmd)}"
        if stdout:
            message += f"\nstdout:\n{stdout[-4000:]}"
        if stderr:
            message += f"\nstderr:\n{stderr[-4000:]}"
        raise RuntimeError(redact(message))
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def output_subdir_for(region: str, target: str) -> str:
    return "global" if target == "global" else region


def storage_key_for(region: str, target: str, filename: str) -> str:
    return f"tiles/{output_subdir_for(region, target)}/{filename}"


def local_output_path_for(region: str, target: str, filename: str) -> Path:
    return OUTPUT_DIR / "tiles" / output_subdir_for(region, target) / filename


def url_for_key(key: str) -> str:
    base = os.getenv("CDN_BASE_URL") or os.getenv("STATIC_BASE_URL") or ""
    if not base:
        return key
    return base.rstrip("/") + "/" + key.lstrip("/")


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def center_lonlat(region_config: dict[str, Any]) -> tuple[float, float]:
    if "center" in region_config:
        lon, lat = region_config["center"]
        return float(lon), float(lat)
    min_lon, min_lat, max_lon, max_lat = region_config["bbox"]
    return (float(min_lon) + float(max_lon)) / 2, (float(min_lat) + float(max_lat)) / 2
