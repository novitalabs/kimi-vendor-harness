"""Audit an official-test intake against the model manifest.

Usage:
    uv run python scripts/audit_intake.py intake/<model>/<revision> [--manifest models/<model>/suite-manifest.yaml]

Derives coverage from two sources that the intake file cannot override:
  * the model manifest: which rows a suite declares (row_ids);
  * the filesystem: whether each item's method path exists.

Reports three kinds of problem and exits non-zero if any are found:
  contradiction  intake says 'wired' but no manifest suite declares the row
  missing_path   a method path does not exist
  gap            no method (informational; counted, does not by itself fail unless --strict)

Optional items (mandatory: false in the manifest) are reported but do not fail the audit.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def manifest_rows(manifest_path: Path) -> dict[str, dict]:
    """row id -> {'suite': suite_id, 'mandatory': bool} for every suite the manifest declares."""
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    declared: dict[str, dict] = {}
    for suite in data.get("suites", []):
        for row in suite.get("row_ids", []) or []:
            key = str(row)
            if key in declared:
                raise SystemExit(f"row {key} is declared by more than one suite")
            declared[key] = {"suite": suite["suite_id"], "mandatory": bool(suite.get("mandatory", True))}
    return declared


def method_exists(method: str) -> bool:
    return (ROOT / method.split(" ")[0]).exists()


def audit(items: list[dict], declared: dict[str, dict]) -> tuple[list[str], list[str], Counter]:
    failures: list[str] = []
    notes: list[str] = []
    derived: Counter = Counter()
    for item in items:
        item_id = str(item.get("id"))
        method = item.get("method")
        in_manifest = declared.get(item_id)
        if item.get("coverage") == "wired" and not in_manifest:
            failures.append(f"contradiction: row {item_id} is marked wired but no manifest suite declares it")
        if method and not method_exists(method):
            failures.append(f"missing_path: row {item_id} method does not exist: {method}")
        if not method:
            derived["gap"] += 1
            notes.append(f"gap: row {item_id} has no method ({item.get('requirement', '')[:60]})")
        elif in_manifest:
            derived["declared_in_manifest"] += 1
        else:
            derived["method_only"] += 1
        if in_manifest and not in_manifest["mandatory"]:
            notes.append(f"optional: row {item_id} is non-mandatory in suite {in_manifest['suite']}")
    return failures, notes, derived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", type=Path, help="intake/<model>/<revision>")
    parser.add_argument("--manifest", type=Path, help="defaults to models/<model>/suite-manifest.yaml")
    parser.add_argument("--strict", action="store_true", help="treat gap rows as failures")
    args = parser.parse_args(argv)

    directory = args.directory if args.directory.is_absolute() else ROOT / args.directory
    model = directory.parent.name
    manifest = args.manifest or ROOT / "models" / model / "suite-manifest.yaml"
    if not manifest.is_file():
        print(f"no manifest for model {model!r}: {manifest}", file=sys.stderr)
        return 2
    items_path = directory / "items.yaml"
    items = yaml.safe_load(items_path.read_text(encoding="utf-8"))["items"]

    failures, notes, derived = audit(items, manifest_rows(manifest))
    gaps = [n for n in notes if n.startswith("gap:")]
    if args.strict:
        failures.extend(gaps)
    for line in failures + [n for n in notes if not n.startswith("gap:")] + gaps:
        print(line)
    print(f"{len(items)} items: " + ", ".join(f"{k}={v}" for k, v in sorted(derived.items())))
    print(f"{len(failures)} failure(s), {len(gaps)} gap row(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
