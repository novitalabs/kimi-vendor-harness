"""Finalize one fetched run and persist every publication gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from artifact.validate import validate as validate_leaf
from config import load_contract, task_plan
from e2e import write_report as write_e2e_report
from itl import analyze_run as analyze_itl_run
from metrics import write_report as write_metrics_report
from status import finalize_status, update_status
from triage.triage_all import run as triage_all


def _receipt(path: Path, result: subprocess.CompletedProcess[str]) -> None:
    path.write_text(result.stdout + result.stderr)


def finalize(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    contract_path = root / "run-contract.json"
    contract = load_contract(contract_path)
    archive = root / "archive"
    if not archive.is_dir():
        raise RuntimeError(f"fetched archive is missing: {archive}")
    gates = root / "gates"
    gates.mkdir(exist_ok=True)
    skill_root = Path(__file__).resolve().parents[3]

    run_artifacts = subprocess.run(
        [str(skill_root / "scripts/validate-run-artifacts.sh"), str(contract_path), str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )
    _receipt(gates / "post-fetch-artifact-and-secret-gate.txt", run_artifacts)
    if run_artifacts.returncode:
        update_status(root, state="failed", phase="triage", blockers=["post-fetch-artifact-or-secret-gate-failed"])
        raise RuntimeError("post-fetch artifact or secret gate failed")

    leaves = sorted(path.parent for path in archive.glob("jobs/*/*/*/trial-record.json"))
    validation_rows = []
    invalid_leaves = []
    for leaf in leaves:
        errors = validate_leaf(leaf)
        validation_rows.append(("PASS" if not errors else "FAIL", str(leaf), ";".join(errors)))
        if errors:
            invalid_leaves.append({"trial": str(leaf), "errors": errors})
    (archive / "artifact-validation.tsv").write_text(
        "".join(f"{state}\t{trial}\t{errors}\n" for state, trial, errors in validation_rows)
    )
    if invalid_leaves:
        (archive / "artifact-validation-errors.json").write_text(json.dumps(invalid_leaves, indent=2) + "\n")
        update_status(root, state="failed", phase="triage", blockers=["leaf-artifact-validation-failed"])
        raise RuntimeError(f"artifact validation failed for {len(invalid_leaves)} leaves")

    records = triage_all(archive)
    tasks, _ = task_plan(contract)
    metrics = write_metrics_report(archive, records, tasks, baseline_trials=int(contract["run"]["rounds"]))
    if not metrics["publishable"]:
        update_status(root, state="failed", phase="metrics", blockers=["metrics-not-publishable"])
        raise RuntimeError("metrics are not publishable")
    e2e = write_e2e_report(archive)
    itl = analyze_itl_run(archive, 5000.0)
    runtime = json.loads((archive / "runtime-versions.json").read_text())
    headline = f"smoke baseline-round mean: {metrics['accuracy']:.2%}"
    evidence = {
        "archive": str(archive),
        "artifact_validated_leaves": len(leaves),
        "recorded_baseline_trials": metrics["recorded_baseline_trial_count"],
        "recorded_trials": metrics["recorded_trial_count"],
        "recovery_trials": metrics["recovery_trials"],
        "execution_invalid_trials": metrics["execution_invalid_trials"],
        "e2e_trial_count": e2e["trial_count"],
        "itl_trial_count": itl["trials"],
        "runtime_cache_root": runtime["cache_root"],
        "runtime_cache_hits": runtime["cache_hits"],
        "kimi_cli_wheel_sha256": runtime["kimi_cli_wheel_sha256"],
    }
    status = finalize_status(root, headline, evidence)
    status_gate = subprocess.run(
        [str(skill_root / "scripts/validate-run-status.sh"), str(contract_path), str(root / "status.json")],
        check=False,
        capture_output=True,
        text=True,
    )
    _receipt(gates / "final-status-validation.txt", status_gate)
    if status_gate.returncode:
        raise RuntimeError("final status validation failed")

    release_manifest = json.loads((root / "release-manifest.json").read_text())
    manifest = {
        "schema_version": 1,
        "release": release_manifest,
        "tag": contract["run"]["tag"],
        "mode": contract["task_selection"]["value"],
        "contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "status": status,
        "metrics": metrics,
        "gates": {
            "run_artifacts": "passed",
            "secret_audit": "passed",
            "leaf_artifacts": {"passed": len(leaves), "failed": 0},
            "status": "passed",
        },
        "diagnostics": {
            "e2e_report": "archive/e2e-phase-summary.json",
            "itl_report": "archive/itl-5s.json",
        },
    }
    (root / "finalization-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest
