from __future__ import annotations

import json
import sys
from pathlib import Path
from .triage_trial import triage


def run(run_dir: str | Path) -> list[dict]:
    root = Path(run_dir)
    trial_records = sorted(root.glob("jobs/*/*/*/trial-record.json"))
    if trial_records:
        trial_dirs = [path.parent for path in trial_records]
    else:
        trial_dirs = [path.parent for path in sorted(root.glob("jobs/*/*/*/result.json"))]
    records = [triage(trial_dir) for trial_dir in trial_dirs]
    (root / "triage.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return records


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1]), ensure_ascii=False, indent=2))
