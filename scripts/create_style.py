from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from common import CONFIG_DIR


def safe_style_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError("Style id cannot be empty")
    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new MapLibre style file from an existing style.")
    parser.add_argument("--id", required=True, help="New style id, for example taxi-night")
    parser.add_argument("--name", help="Display name. Defaults to the id.")
    parser.add_argument("--from-style", default="light", help="Existing style id to copy. Defaults to light.")
    parser.add_argument("--force", action="store_true", help="Overwrite the target style if it already exists.")
    args = parser.parse_args()

    styles_dir = CONFIG_DIR / "styles"
    source_path = styles_dir / f"{safe_style_id(args.from_style)}.json"
    target_id = safe_style_id(args.id)
    target_path = styles_dir / f"{target_id}.json"

    if not source_path.exists():
        raise FileNotFoundError(f"Source style not found: {source_path}")
    if target_path.exists() and not args.force:
        raise FileExistsError(f"Target style already exists: {target_path}. Use --force to overwrite.")

    style = json.loads(source_path.read_text(encoding="utf-8"))
    style["name"] = args.name or target_id.replace("-", " ").title()
    metadata = style.get("metadata", {})
    metadata["derived_from"] = source_path.stem
    style["metadata"] = metadata

    target_path.write_text(json.dumps(style, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(target_path)


if __name__ == "__main__":
    main()
