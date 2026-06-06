from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from common import CONFIG_DIR, OUTPUT_DIR, sha256_file


COPY_SETS = (
    ("tiles", OUTPUT_DIR / "tiles", "tiles"),
    ("manifests", OUTPUT_DIR / "manifests", "manifests"),
    ("fonts", CONFIG_DIR / "glyphs", "fonts"),
    ("sprites", CONFIG_DIR / "sprites", "sprites"),
)


def should_publish(path: Path) -> bool:
    name = path.name
    if name.endswith(".tmp") or name.endswith(".journal") or name.endswith(".pmtiles-journal"):
        return False
    return path.is_file()


def copy_file(src: Path, dst: Path, *, dry_run: bool) -> dict[str, Any]:
    if dst.exists() and src.suffix == ".pmtiles" and sha256_file(src) != sha256_file(dst):
        raise RuntimeError(
            f"Refusing to overwrite immutable PMTiles with different content: {dst}. "
            "Publish a new versioned filename instead."
        )
    action = "skip"
    if not dst.exists():
        action = "copy"
    elif src.stat().st_size != dst.stat().st_size or int(src.stat().st_mtime) > int(dst.stat().st_mtime):
        action = "replace"
    if not dry_run and action != "skip":
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return {
        "source": str(src),
        "destination": str(dst),
        "bytes": src.stat().st_size,
        "action": action if not dry_run else f"dry_run_{action}",
    }


def publish_static(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    root = root.resolve()
    copied: list[dict[str, Any]] = []
    skipped_sources: list[str] = []
    for label, source_root, destination_name in COPY_SETS:
        if not source_root.exists():
            skipped_sources.append(str(source_root))
            continue
        for src in sorted(source_root.rglob("*")):
            if not should_publish(src):
                continue
            rel = src.relative_to(source_root)
            dst = root / destination_name / rel
            item = copy_file(src, dst, dry_run=dry_run)
            item["set"] = label
            copied.append(item)
    return {
        "ok": True,
        "root": str(root),
        "dry_run": dry_run,
        "files": copied,
        "skipped_sources": skipped_sources,
        "summary": {
            "files_seen": len(copied),
            "copied_or_replaced": sum(1 for item in copied if "copy" in item["action"] or "replace" in item["action"]),
            "bytes": sum(int(item["bytes"]) for item in copied),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish local TavrixMap Tiles artifacts into a static Nginx root.")
    parser.add_argument("--root", default="/var/www/tavrix-tiles", help="Static Nginx document root.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned copies without writing files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = publish_static(Path(args.root), dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
