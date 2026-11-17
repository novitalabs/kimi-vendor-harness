"""Validate an official-test intake directory (intake/<model>/<revision>/items.yaml).

Exits non-zero on any violation. Nothing is repaired automatically: a broken
method path is reported, not rewritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("id", "source", "requirement", "group", "mandatory", "recipe", "coverage")
GROUPS = {"feature", "precision"}
COVERAGE = {"wired", "entry_only", "gap"}


def _method_path(method: str) -> str:
    return method.split(" ")[0]


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return [f"{path}: expected a non-empty top-level 'items' list"]
    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: not a mapping")
            continue
        item_id = item.get("id")
        label = f"{label} ({item_id!r})"
        for field in REQUIRED:
            if field not in item or item[field] in (None, ""):
                errors.append(f"{label}: missing required field {field!r}")
        if item_id in seen:
            errors.append(f"{label}: duplicate id")
        seen.add(item_id)
        if item.get("group") not in GROUPS:
            errors.append(f"{label}: group must be one of {sorted(GROUPS)}")
        coverage = item.get("coverage")
        if coverage not in COVERAGE:
            errors.append(f"{label}: coverage must be one of {sorted(COVERAGE)}")
        if coverage in {"wired", "entry_only"}:
            method = item.get("method")
            if not method:
                errors.append(f"{label}: coverage={coverage} requires a 'method' path")
            elif not (ROOT / _method_path(method)).exists():
                errors.append(f"{label}: method path does not exist: {_method_path(method)}")
        if coverage == "gap" and item.get("method"):
            errors.append(f"{label}: coverage=gap must not name a method")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="intake/<model>/<revision>")
    args = parser.parse_args(argv)
    path = (args.directory if args.directory.is_absolute() else ROOT / args.directory) / "items.yaml"
    if not path.is_file():
        print(f"missing {path}", file=sys.stderr)
        return 2
    errors = validate(path)
    for error in errors:
        print(error)
    print(f"{len(errors)} problem(s) in {path.relative_to(ROOT)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
