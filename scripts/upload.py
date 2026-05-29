from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from common import PMTILES_CACHE_CONTROL, storage_key_for, url_for_key, setup_logging


LOGGER = setup_logging("upload")


def _s3_client():
    endpoint_url = os.getenv("S3_ENDPOINT_URL") or None
    region_name = os.getenv("AWS_DEFAULT_REGION") or "auto"
    force_path_style = os.getenv("S3_FORCE_PATH_STYLE", "true").lower() in ("1", "true", "yes")
    config = Config(s3={"addressing_style": "path" if force_path_style else "auto"})
    return boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name, config=config)


def upload_file(
    local_path: Path,
    key: str,
    cache_control: str = PMTILES_CACHE_CONTROL,
    content_type: str = "application/vnd.pmtiles",
) -> dict[str, Any]:
    bucket = os.getenv("S3_BUCKET", "").strip()
    if bucket:
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
    parser.add_argument("--artifact", required=True)
    args = parser.parse_args()
    artifact = json.loads(Path(args.artifact).read_text(encoding="utf-8"))
    result = upload_pmtiles(artifact)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
