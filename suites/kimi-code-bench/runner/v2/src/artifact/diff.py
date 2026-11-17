"""Stable JSON/file-set diff for two individual leaves."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from .observed_contract import STABLE_FILES


def diff(left: str | Path, right: str | Path) -> dict:
    a, b = Path(left), Path(right)
    entries = []
    for rel in sorted(set(STABLE_FILES) | {"trial-record.json"}):
        pa, pb = a / rel, b / rel
        if not pa.exists() or not pb.exists():
            entries.append({"path": rel, "status": "missing-left" if not pa.exists() else "missing-right"})
            continue
        ha = hashlib.sha256(pa.read_bytes()).hexdigest()
        hb = hashlib.sha256(pb.read_bytes()).hexdigest()
        if ha != hb:
            entries.append({"path": rel, "status": "different", "left_sha256": ha, "right_sha256": hb})
    return {"left": str(a), "right": str(b), "different": len(entries), "entries": entries}


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("left"); p.add_argument("right"); p.add_argument("--json", action="store_true")
    args = p.parse_args(); out = diff(args.left, args.right); print(json.dumps(out, indent=2) if args.json else "different=" + str(out["different"]))
