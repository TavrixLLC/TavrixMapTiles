from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from common import PMTILES_CACHE_CONTROL, PMTILES_CONTENT_TYPE, storage_key_for, url_for_key, setup_logging


LOGGER = setup_logging("upload")


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return ""


def object_storage_config_status(require_bucket: bool = False) -> dict[str, Any]:
    bucket = _env("S3_BUCKET")
    region = _env("S3_REGION", "AWS_DEFAULT_REGION")
    access_key = _env("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    secret_key = _env("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    public_base_url = _env("S3_PUBLIC_BASE_URL", "CDN_BASE_URL", "STATIC_BASE_URL")
    endpoint_url = _env("S3_ENDPOINT_URL")
    force_path_style = _env("S3_FORCE_PATH_STYLE") or "false"
    publishing_enabled = bool(bucket)
    missing: list[str] = []
    warnings: list[str] = []

    if require_bucket and not bucket:
        missing.append("S3_BUCKET")
    if publishing_enabled:
        if not region:
            missing.append("S3_REGION or AWS_DEFAULT_REGION")
        if not access_key:
            missing.append("S3_ACCESS_KEY_ID or AWS_ACCESS_KEY_ID")
        if not secret_key:
            missing.append("S3_SECRET_ACCESS_KEY or AWS_SECRET_ACCESS_KEY")
        if not public_base_url:
            missing.append("S3_PUBLIC_BASE_URL or CDN_BASE_URL")
        if not endpoint_url:
            warnings.append("S3_ENDPOINT_URL is empty; boto3 will use the provider default endpoint.")
    else:
        warnings.append("S3_BUCKET is empty; uploads are disabled and artifacts remain local.")

    return {
        "ok": not missing,
        "publishing_enabled": publishing_enabled,
        "bucket": bucket or None,
        "endpoint_url": endpoint_url or None,
        "region": region or None,
        "public_base_url": public_base_url or None,
        "force_path_style": force_path_style.lower() in ("1", "true", "yes", "on"),
        "missing": missing,
        "warnings": warnings,
    }


def validate_object_storage_config(require_bucket: bool = False) -> dict[str, Any]:
    status = object_storage_config_status(require_bucket=require_bucket)
    if not status["ok"]:
        raise RuntimeError("Object storage publishing config is incomplete: " + ", ".join(status["missing"]))
    return status


def _s3_client():
    import boto3
    from botocore.config import Config

    endpoint_url = _env("S3_ENDPOINT_URL") or None
    region_name = _env("S3_REGION", "AWS_DEFAULT_REGION") or "auto"
    access_key = _env("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID") or None
    secret_key = _env("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY") or None
    force_path_style = (_env("S3_FORCE_PATH_STYLE") or "true").lower() in ("1", "true", "yes", "on")
    config = Config(s3={"addressing_style": "path" if force_path_style else "auto"})
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=config,
    )


def upload_file(
    local_path: Path,
    key: str,
    cache_control: str = PMTILES_CACHE_CONTROL,
    content_type: str = PMTILES_CONTENT_TYPE,
) -> dict[str, Any]:
    bucket = _env("S3_BUCKET")
    if bucket:
        validate_object_storage_config(require_bucket=True)
        if not local_path.exists() or local_path.stat().st_size <= 0:
            raise RuntimeError(f"Cannot upload missing or empty file: {local_path}")
        LOGGER.info("Uploading %s to s3://%s/%s", local_path, bucket, key)
        _s3_client().upload_file(
            str(local_path),
            bucket,
            key,
            ExtraArgs={
                "CacheControl": cache_control,
                "ContentType": content_type,
            },
        )
    else:
        LOGGER.info("S3_BUCKET is empty; keeping local artifact at %s", local_path)

    return {
        "bucket": bucket or None,
        "key": key,
        "url": url_for_key(key),
        "local_path": str(local_path),
        "uploaded": bool(bucket),
    }


def upload_pmtiles(artifact: dict[str, Any]) -> dict[str, Any]:
    key = storage_key_for(artifact["region"], artifact["target"], artifact["filename"])
    return upload_file(Path(artifact["path"]), key)


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload PMTiles to S3/R2-compatible storage.")
    parser.add_argument("--artifact")
    parser.add_argument("--check-config", action="store_true", help="Validate object storage env vars and exit.")
    args = parser.parse_args()
    if args.check_config:
        print(json.dumps(validate_object_storage_config(require_bucket=True), indent=2))
        return
    if not args.artifact:
        parser.error("--artifact is required unless --check-config is used")
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    result = upload_pmtiles(artifact)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
