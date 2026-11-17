"""Persistent local status for one detached Code Bench run."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import load_contract


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _contract_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_status(contract: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    required = int(contract["run"]["required_trial_count"])
    return {
        "schema_version": 2,
        "observed_at": _now(),
        "tag": contract["run"]["tag"],
        "runner_contract_sha256": _contract_sha(run_dir / "run-contract.json"),
        "state": "planned",
        "phase": "preflight",
        "remote": {
            "workdir": contract["remote"]["workdir"],
            "marker": "none",
            "pid": None,
            "exit_code": None,
        },
        "local_run_dir": contract["run"]["evidence_dir"],
        "progress": {
            "selected_tasks": int(contract["run"]["selected_task_count"]),
            "required_trials": required,
            "started_trials": 0,
            "finished_trials": 0,
            "valid_pass": 0,
            "valid_zero": 0,
            "error_zero": 0,
            "unclassified": 0,
            "not_started_trials": required,
        },
        "activity": {"setup_log_bytes": 0, "last_progress_at": None},
        "evidence": {},
        "blockers": [],
        "headline_result": None,
    }


def write_status(run_dir: str | Path, status: dict[str, Any]) -> Path:
    root = Path(run_dir).resolve()
    status["observed_at"] = _now()
    path = root / "status.json"
    temporary = root / "status.json.tmp"
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    return path


def initialize_status(run_dir: str | Path, *, state: str = "preparing", phase: str = "preflight") -> dict[str, Any]:
    root = Path(run_dir).resolve()
    contract = load_contract(root / "run-contract.json")
    status = _base_status(contract, root)
    status["state"] = state
    status["phase"] = phase
    write_status(root, status)
    return status


def update_status(run_dir: str | Path, **changes: Any) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    path = root / "status.json"
    status = json.loads(path.read_text()) if path.is_file() else initialize_status(root)
    for key, value in changes.items():
        if key in {"remote", "progress", "activity", "evidence"}:
            status.setdefault(key, {}).update(value)
        else:
            status[key] = value
    write_status(root, status)
    return status


_REMOTE_INSPECT = r'''
import datetime
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
phase = (root / ".kbench-phase").read_text().strip() if (root / ".kbench-phase").is_file() else "remote_setup"
if (root / ".kbench-done").is_file():
    marker, state = "done", "completed"
elif (root / ".kbench-paused").is_file():
    marker, state = "paused", "paused"
elif (root / ".kbench-failed").is_file():
    marker, state = "failed", "failed"
elif (root / ".kbench-running").is_file():
    marker, state = "running", "running"
else:
    marker, state = "unknown", "unknown"

def integer(path):
    try:
        return int(path.read_text().strip())
    except Exception:
        return None

records = []
for path in sorted((root / "tsv").glob("baseline-*.tsv")):
    try:
        fields = path.read_text().strip().split("\t")
        records.append((int(fields[5]), int(fields[6])))
    except Exception:
        records.append((-1, -1))
finished = len(records)
error_zero = sum(abnormal == 1 for _, abnormal in records)
valid_pass = sum(success > 0 and abnormal == 0 for success, abnormal in records)
valid_zero = sum(success == 0 and abnormal == 0 for success, abnormal in records)
unclassified = finished - error_zero - valid_pass - valid_zero
started = max(finished, len(list((root / "state/started").glob("baseline-*"))))
last_paths = list((root / "tsv").glob("*.tsv"))
last_progress = None
if last_paths:
    timestamp = max(path.stat().st_mtime for path in last_paths)
    last_progress = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
setup_log = root / "setup.log"
print(json.dumps({
    "state": state,
    "phase": phase,
    "marker": marker,
    "pid": integer(root / ".kbench-pid"),
    "exit_code": integer(root / ".kbench-exit-code"),
    "started": started,
    "finished": finished,
    "valid_pass": valid_pass,
    "valid_zero": valid_zero,
    "error_zero": error_zero,
    "unclassified": unclassified,
    "setup_log_bytes": setup_log.stat().st_size if setup_log.is_file() else 0,
    "last_progress_at": last_progress,
    "recovery_trials": len(list((root / "tsv").glob("recovery-*.tsv"))),
    "remote_archive_present": (root / "remote.tar.gz").is_file(),
}))
'''


def refresh_remote_status(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    contract = load_contract(root / "run-contract.json")
    remote = contract["remote"]
    command = "python3 -c " + shlex.quote(_REMOTE_INSPECT) + " " + shlex.quote(remote["workdir"])
    result = subprocess.run(
        ["ssh", "-p", str(remote["port"]), remote["host"], command],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return update_status(root, state="unknown", blockers=[f"remote-status-exit-{result.returncode}"])
    observed = json.loads(result.stdout)
    required = int(contract["run"]["required_trial_count"])
    started = min(required, int(observed["started"]))
    finished = min(required, int(observed["finished"]))
    phase_map = {
        "remote_setup": "remote_setup",
        "preflight": "preflight",
        "baseline": "pull_and_test",
        "recovery": "pull_and_test",
        "archive": "fetch",
        "done": "fetch",
    }
    return update_status(
        root,
        state=observed["state"],
        phase=phase_map.get(observed["phase"], "remote_setup"),
        remote={
            "marker": observed["marker"],
            "pid": observed["pid"],
            "exit_code": observed["exit_code"],
        },
        progress={
            "started_trials": started,
            "finished_trials": finished,
            "valid_pass": int(observed["valid_pass"]),
            "valid_zero": int(observed["valid_zero"]),
            "error_zero": int(observed["error_zero"]),
            "unclassified": int(observed["unclassified"]),
            "not_started_trials": required - started,
        },
        activity={
            "setup_log_bytes": int(observed["setup_log_bytes"]),
            "last_progress_at": observed["last_progress_at"],
        },
        evidence={
            "recovery_trials": int(observed["recovery_trials"]),
            "remote_archive_present": bool(observed["remote_archive_present"]),
        },
        blockers=(
            ["remote-run-failed"] if observed["state"] == "failed"
            else ["human-paused"] if observed["state"] == "paused"
            else []
        ),
    )


def finalize_status(run_dir: str | Path, headline: str, evidence: dict[str, Any]) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    status = refresh_remote_status(root)
    required = status["progress"]["required_trials"]
    if status["state"] != "completed" or status["progress"]["finished_trials"] != required:
        raise RuntimeError("remote run is not complete")
    return update_status(
        root,
        state="finalized",
        phase="done",
        headline_result=headline,
        evidence=evidence,
        blockers=[],
    )
