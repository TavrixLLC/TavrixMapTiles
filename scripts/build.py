from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg2

from common import (
    TMP_DIR,
    ensure_dirs,
    iso_now,
    setup_logging,
    storage_key_for,
    timestamp_id,
    url_for_key,
    write_json_atomic,
)
from export_layers import export_layers
from generate_pmtiles import generate_pmtiles
from prune import prune
from publish_manifest import publish_manifest
from upload import upload_pmtiles
from validate_pmtiles import validate_pmtiles


LOGGER = setup_logging("build")


@contextmanager
def advisory_lock(lock_id: int) -> Iterator[None]:
    conn = psycopg2.connect(
        host=os.getenv("POSTGIS_HOST", "localhost"),
        port=int(os.getenv("POSTGIS_PORT", "5432")),
        dbname=os.getenv("POSTGIS_DB", "gis"),
        user=os.getenv("POSTGIS_USER", "pmtiles_builder"),
        password=os.getenv("POSTGIS_PASSWORD", ""),
        connect_timeout=15,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
            acquired = cur.fetchone()[0]
        if not acquired:
            raise RuntimeError(f"Build lock {lock_id} is already held; aborting overlapping build.")
        LOGGER.info("Acquired PostgreSQL advisory lock %s", lock_id)
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
            LOGGER.info("Released PostgreSQL advisory lock %s", lock_id)
        finally:
            conn.close()


def _quality_from_exports(exports_manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "build_id": exports_manifest["build_id"],
        "target": exports_manifest["target"],
        "region": exports_manifest["region"],
        "layers": {
            layer["name"]: layer.get("quality", {})
            for layer in exports_manifest.get("layers", [])
        },
    }


def _local_upload_result(artifact: dict[str, Any]) -> dict[str, Any]:
    key = storage_key_for(artifact["region"], artifact["target"], artifact["filename"])
    return {
        "bucket": None,
        "key": key,
        "url": url_for_key(key),
        "local_path": artifact["path"],
        "uploaded": False,
    }


def build_once(
    target: str,
    region: str,
    *,
    skip_upload: bool = False,
    verify_url: bool = False,
    no_prune: bool = False,
) -> dict[str, Any]:
    ensure_dirs()
    if target == "global":
        region = "global"

    stamp = timestamp_id()
    build_id = f"{target}-{region}-{stamp}"
    lock_id = int(os.getenv("BUILD_LOCK_ID", "932001"))
    started = time.monotonic()

    with advisory_lock(lock_id):
        LOGGER.info("Starting %s build for %s", target, region)
        exports_manifest = export_layers(target, region, build_id)
        artifact = generate_pmtiles(exports_manifest, stamp)
        quality = _quality_from_exports(exports_manifest)

        validation = validate_pmtiles(artifact, quality=quality)

        if skip_upload:
            upload_result = _local_upload_result(artifact)
        else:
            upload_result = upload_pmtiles(artifact)

        if verify_url:
            validation = validate_pmtiles(artifact, cdn_url=upload_result["url"], quality=quality)

        manifest = publish_manifest(artifact, upload_result)

        prune_result = None
        if not no_prune:
            try:
                prune_result = prune(int(os.getenv("RETENTION_DAYS", "30")))
            except Exception as exc:  # pruning must not break a successful release
                LOGGER.warning("Pruning failed but build succeeded: %s", exc)

    duration = time.monotonic() - started
    result = {
        "schema_version": 1,
        "build_id": build_id,
        "target": target,
        "region": region,
        "started_at": stamp,
        "finished_at": iso_now(),
        "duration_seconds": round(duration, 3),
        "artifact": artifact,
        "validation": validation,
        "upload": upload_result,
        "manifest": {
            "region": manifest["region"],
            "url": manifest.get("manifest_url"),
            "key": manifest.get("manifest_key"),
            "tilesets": manifest.get("tilesets", {}),
        },
        "prune": prune_result,
    }
    write_json_atomic(TMP_DIR / build_id / "build-result.json", result)
    LOGGER.info("Build complete: %s %s in %.1fs", target, region, duration)
    return result


def _load_schedules() -> dict[str, Any]:
    path = Path(os.getenv("CONFIG_DIR", "/app/config")) / "schedules.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _state_path() -> Path:
    return TMP_DIR / "scheduler-state.json"


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"jobs": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _job_key(job: dict[str, Any], region: str) -> str:
    return f"{job['name']}:{job['target']}:{region}"


def _is_due(job: dict[str, Any], state: dict[str, Any], region: str) -> bool:
    key = _job_key(job, region)
    last_success = _parse_time(state.get("jobs", {}).get(key, {}).get("last_success"))
    if last_success is None:
        return True
    interval = timedelta(hours=float(job["interval_hours"]))
    return datetime.now(timezone.utc) >= last_success + interval


def run_scheduler(once: bool = False) -> None:
    ensure_dirs()
    LOGGER.info("Scheduler started")
    while True:
        schedules = _load_schedules()
        state = _load_state()
        state.setdefault("jobs", {})
        for job in schedules.get("jobs", []):
            if not job.get("enabled", True):
                continue
            for region in job.get("regions", []):
                key = _job_key(job, region)
                if not _is_due(job, state, region):
                    continue
                state["jobs"].setdefault(key, {})
                state["jobs"][key]["last_attempt"] = iso_now()
                try:
                    LOGGER.info("Scheduler running %s", key)
                    build_once(
                        job["target"],
                        region,
                        verify_url=os.getenv("VERIFY_PUBLISHED_URL", "false").lower() in ("1", "true", "yes"),
                    )
                    state["jobs"][key]["last_success"] = iso_now()
                    state["jobs"][key]["failure_count"] = 0
                except Exception as exc:
                    LOGGER.exception("Scheduled build failed for %s", key)
                    state["jobs"][key]["last_failure"] = iso_now()
                    state["jobs"][key]["last_error"] = str(exc)
                    state["jobs"][key]["failure_count"] = int(state["jobs"][key].get("failure_count", 0)) + 1
                finally:
                    write_json_atomic(_state_path(), state)

        if once:
            return
        time.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Orchestrate PMTiles export, generation, validation, upload, manifest publish, and pruning.")
    parser.add_argument("--target", choices=["global", "basemap", "pois"])
    parser.add_argument("--region", default=os.getenv("REGION", "iraq"))
    parser.add_argument("--all", action="store_true", help="Run enabled jobs from schedules.json once.")
    parser.add_argument("--scheduler", action="store_true", help="Run forever using config/schedules.json.")
    parser.add_argument("--skip-upload", action="store_true", help="Do not upload even if S3_BUCKET is configured.")
    parser.add_argument("--verify-url", action="store_true", help="Require CDN/static server range validation before manifest publish.")
    parser.add_argument("--no-prune", action="store_true", help="Skip pruning after a successful build.")
    args = parser.parse_args()

    if args.scheduler:
        run_scheduler()
        return

    if args.all:
        run_scheduler(once=True)
        return

    if not args.target:
        parser.error("--target is required unless --all or --scheduler is used")

    result = build_once(
        args.target,
        args.region,
        skip_upload=args.skip_upload,
        verify_url=args.verify_url or os.getenv("VERIFY_PUBLISHED_URL", "false").lower() in ("1", "true", "yes"),
        no_prune=args.no_prune,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
