from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def invoke(command: list[str]) -> None:
    process = subprocess.run(command, text=True, check=False)
    if process.returncode:
        raise RuntimeError(f"Code Bench command failed with exit {process.returncode}: {command[1]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="skip run/fetch and only finalize already-fetched evidence",
    )
    args = parser.parse_args(argv)
    product_root = Path(__file__).resolve().parents[3]
    runner = product_root / "suites" / "kimi-code-bench" / "runner" / "v2" / "kbench"
    if args.finalize_only:
        # Repair path for a run whose vendor traffic completed but whose local
        # finalize crashed: never send new requests, just recompute analysis.
        snapshot = args.run_dir / "config.snapshot.json"
        archive = args.run_dir / "archive"
        if not snapshot.is_file() or not archive.is_dir():
            raise RuntimeError(
                "finalize-only requires the completed fetched evidence in the run dir"
            )
    else:
        invoke([str(runner), "run", str(args.contract), "--run-dir", str(args.run_dir)])
        invoke([str(runner), "fetch", str(args.run_dir)])
    invoke([str(runner), "finalize", str(args.run_dir)])
    finalization = args.run_dir / "finalization-manifest.json"
    payload = json.loads(finalization.read_text(encoding="utf-8"))
    metrics = payload["metrics"]
    artifact_paths = [
        str(finalization.resolve()),
        str((args.run_dir / "archive" / "metrics.json").resolve()),
    ]
    for evidence in (args.contract, args.run_dir / "archive" / "runtime-versions.json"):
        if evidence.is_file():
            artifact_paths.append(str(evidence.resolve()))
    score = metrics.get("accuracy")
    score_passed = args.min_score is None or (
        isinstance(score, (int, float)) and score >= args.min_score
    )
    result = {
        "schema_version": 1,
        "suite_id": "kimi-code-bench",
        "row_ids": [],
        "status": "passed" if score_passed else "failed",
        "validity": "valid",
        "counts": {
            "tasks": metrics.get("selected_task_count"),
            "trials": metrics.get("recorded_baseline_trial_count"),
            "execution_invalid": metrics.get("execution_invalid_trials"),
        },
        "metrics": {"score": score},
        "redacted_command": [],
        "artifact_paths": artifact_paths,
        "failure_summary": None if score_passed else f"score {score} is below {args.min_score}",
        "runner_fingerprint": {"runner": "novita-kimi-code-bench", "version": "1.1.0"},
        "coverage": {
            "observed_tasks": metrics.get("selected_task_count"),
            "observed_trials": metrics.get("recorded_baseline_trial_count"),
            "finalize_only": bool(args.finalize_only),
        },
        "reason": None if score_passed else "benchmark_below_threshold",
    }
    (args.run_dir / "adapter-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
