from __future__ import annotations

import json
from pathlib import Path

try:
    from archive_trial import classify_outcome
except ModuleNotFoundError:  # direct script invocation
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from archive_trial import classify_outcome


def triage(trial_dir: str | Path) -> dict:
    root = Path(trial_dir)
    result = json.loads((root / "result.json").read_text()) if (root / "result.json").exists() else {}
    attempt = json.loads((root / "kbench-attempt.json").read_text()) if (root / "kbench-attempt.json").exists() else {}
    archived = json.loads((root / "trial-record.json").read_text()) if (root / "trial-record.json").exists() else {}
    outcome = archived.get("outcome") or classify_outcome(result, root)
    reward = outcome.get("reward")
    invalid = not outcome.get("execution_valid", False)
    return {
        "task": attempt.get("task_label") or result.get("task_name") or root.name,
        "phase": attempt.get("phase"),
        "round": attempt.get("round"),
        "attempt": attempt.get("attempt"),
        "reward": reward,
        "execution_valid": not invalid,
        "retry": invalid or reward is None,
        "termination_reason": outcome.get("termination_reason"),
        "execution_warning": outcome.get("execution_warning"),
        "trial": str(root),
    }


if __name__ == "__main__":
    import sys
    print(json.dumps(triage(sys.argv[1]), ensure_ascii=False, indent=2))
