from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import SuiteResult, write_json, write_versioned_json
from .config import ConfigError, JudgeAccess, ModelProfile, ServiceAccess, results_root, workspace_root
from .reporting import build_report, load_suite_results, render_markdown
from .schema_validation import validation_error


def _contract_hash(profile_hashes: dict[str, str]) -> str:
    payload = json.dumps(profile_hashes, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _session_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"session-{stamp}-{uuid.uuid4().hex[:8]}".lower()


def _load_run(path: Path) -> dict[str, Any]:
    try:
        return json.loads((path / "run.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigError("session_run_invalid", f"cannot load run {path}: {exc}") from exc


def _load_approval(path: Path) -> dict[str, Any]:
    try:
        approval = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigError(
            "contract_approval_invalid", f"cannot load approval {path}: {exc}"
        ) from exc
    required = {
        "from_contract_hash",
        "to_contract_hash",
        "affected_suites",
        "approved_by",
        "reason",
    }
    if not required <= set(approval):
        raise ConfigError(
            "contract_approval_invalid",
            f"approval is missing fields: {sorted(required - set(approval))}",
        )
    if not isinstance(approval["affected_suites"], list) or not approval["affected_suites"]:
        raise ConfigError("contract_approval_invalid", "affected_suites must be non-empty")
    schema_error = validation_error(approval, "contract-approval.schema.json")
    if schema_error:
        raise ConfigError("contract_approval_invalid", schema_error)
    approval["source_path"] = str(path.resolve())
    approval["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return approval


def _approval_for(
    approvals: list[dict[str, Any]],
    *,
    from_hash: str,
    to_hash: str,
    changed_suites: set[str],
) -> dict[str, Any] | None:
    for approval in approvals:
        affected = set(approval.get("affected_suites") or [])
        if (
            approval.get("from_contract_hash") == from_hash
            and approval.get("to_contract_hash") == to_hash
            and changed_suites <= affected
        ):
            return approval
    return None


def _validate_run_for_session(
    manifest: dict[str, Any],
    *,
    profile: ModelProfile,
    service_hash: str,
    judge_hash: str | None,
    approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    if manifest.get("model") != profile.model_id:
        raise ConfigError("session_run_invalid", "run model does not match session model")
    if manifest.get("service_access_hash") != service_hash:
        raise ConfigError("session_run_invalid", "run service-access hash does not match session")
    if manifest.get("requirements_snapshot_revision") != profile.snapshot_revision:
        raise ConfigError("session_run_invalid", "run requirements revision does not match session")
    beam_ids = {suite.suite_id for suite in profile.suites if suite.adapter == "beam"}
    run_suite_ids = {
        state.get("suite_id") for state in manifest.get("suite_states", [])
    }
    if beam_ids & run_suite_ids and manifest.get("judge_access_hash") != judge_hash:
        raise ConfigError("session_run_invalid", "run judge-access hash does not match session")
    observed_suite_hashes = manifest.get("suite_input_hashes")
    if not isinstance(observed_suite_hashes, dict):
        raise ConfigError(
            "session_run_missing_suite_hashes",
            "run predates per-suite input hashes and cannot be reused automatically",
        )
    current_hashes = profile.suite_input_hashes()
    changed = {
        suite_id
        for suite_id, current in current_hashes.items()
        if observed_suite_hashes.get(suite_id) != current
    }
    observed_contract = _contract_hash(dict(manifest.get("profile_hashes") or {}))
    current_contract = _contract_hash(profile.content_hashes())
    approval = None
    if observed_contract != current_contract:
        approval = _approval_for(
            approvals,
            from_hash=observed_contract,
            to_hash=current_contract,
            changed_suites=changed,
        )
        if approval is None:
            raise ConfigError(
                "contract_approval_required",
                f"run contract differs; approval must cover suites: {sorted(changed)}",
            )
    return {
        "run_contract_hash": observed_contract,
        "current_contract_hash": current_contract,
        "changed_suites": sorted(changed),
        "approval_sha256": None if approval is None else approval["sha256"],
    }


def _session_path(root: Path, model_id: str, identifier: str | None = None) -> Path:
    return results_root(root) / model_id / "sessions" / (identifier or _session_id())


def create_session(
    model_id: str,
    *,
    service_access_path: Path,
    judge_access_path: Path | None = None,
    runs: list[Path],
    approval_paths: list[Path] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or workspace_root()).resolve()
    profile = ModelProfile.load(model_id, root=root)
    service = ServiceAccess.load(service_access_path)
    beam_ids = {suite.suite_id for suite in profile.suites if suite.adapter == "beam"}
    requires_judge = any(
        state.get("selected", True) and state.get("suite_id") in beam_ids
        for run in runs
        for state in (_load_run(run.resolve()).get("suite_states") or [])
    )
    if requires_judge and judge_access_path is None:
        raise ConfigError("judge_access_required", "BEAM acceptance session requires judge access")
    judge = JudgeAccess.load(judge_access_path) if judge_access_path else None
    approvals = [_load_approval(path) for path in approval_paths or []]
    session_dir = _session_path(root, model_id)
    session = {
        "schema_version": 1,
        "session_id": session_dir.name,
        "session_dir": str(session_dir.resolve()),
        "model": model_id,
        "service_access_path": str(service.source_path),
        "service_access_hash": service.sha256,
        "judge_access_path": None if judge is None else str(judge.source_path),
        "judge_access_hash": None if judge is None else judge.sha256,
        "requirements_snapshot_revision": profile.snapshot_revision,
        "current_contract_hash": _contract_hash(profile.content_hashes()),
        "current_suite_input_hashes": profile.suite_input_hashes(),
        "runs": [],
        "contract_migrations": approvals,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    for run in runs:
        manifest = _load_run(run.resolve())
        validation = _validate_run_for_session(
            manifest,
            profile=profile,
            service_hash=service.sha256,
            judge_hash=None if judge is None else judge.sha256,
            approvals=approvals,
        )
        session["runs"].append(
            {"run_dir": str(run.resolve()), "run_id": manifest["run_id"], **validation}
        )
    write_versioned_json(
        session_dir / "session.json",
        session,
        schema_name="acceptance-session.schema.json",
    )
    return report_session(session_dir, root=root)


def continue_session(
    session_dir: Path,
    *,
    runs: list[Path],
    approval_paths: list[Path] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or workspace_root()).resolve()
    session_path = session_dir.resolve() / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    profile = ModelProfile.load(session["model"], root=root)
    approvals = list(session.get("contract_migrations") or [])
    approvals.extend(_load_approval(path) for path in approval_paths or [])
    known = {item["run_id"] for item in session.get("runs") or []}
    for run in runs:
        manifest = _load_run(run.resolve())
        if manifest["run_id"] in known:
            continue
        validation = _validate_run_for_session(
            manifest,
            profile=profile,
            service_hash=session["service_access_hash"],
            judge_hash=session.get("judge_access_hash"),
            approvals=approvals,
        )
        session["runs"].append(
            {"run_dir": str(run.resolve()), "run_id": manifest["run_id"], **validation}
        )
    session["contract_migrations"] = approvals
    session["current_contract_hash"] = _contract_hash(profile.content_hashes())
    session["current_suite_input_hashes"] = profile.suite_input_hashes()
    session["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_versioned_json(
        session_path,
        session,
        schema_name="acceptance-session.schema.json",
    )
    return report_session(session_dir, root=root)


def report_session(
    session_dir: Path,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or workspace_root()).resolve()
    session_path = session_dir.resolve() / "session.json"
    session = json.loads(session_path.read_text(encoding="utf-8"))
    profile = ModelProfile.load(session["model"], root=root)
    current_hashes = profile.suite_input_hashes()
    current_contract = _contract_hash(profile.content_hashes())
    recorded_contract = str(session.get("current_contract_hash") or "")
    if recorded_contract and recorded_contract != current_contract:
        recorded_suite_hashes = session.get("current_suite_input_hashes") or {}
        changed = {
            suite_id
            for suite_id, value in current_hashes.items()
            if recorded_suite_hashes.get(suite_id) != value
        }
        if _approval_for(
            list(session.get("contract_migrations") or []),
            from_hash=recorded_contract,
            to_hash=current_contract,
            changed_suites=changed,
        ) is None:
            raise ConfigError(
                "contract_approval_required",
                f"session contract changed; approval must cover suites: {sorted(changed)}",
            )
    attempts: dict[str, list[dict[str, Any]]] = {
        suite.suite_id: [] for suite in profile.suites
    }
    selected: dict[str, SuiteResult] = {}
    for run_ref in session.get("runs") or []:
        run_dir = Path(run_ref["run_dir"])
        manifest = _load_run(run_dir)
        observed_hashes = manifest.get("suite_input_hashes") or {}
        for result in load_suite_results(run_dir):
            expected_hash = current_hashes.get(result.suite_id)
            reusable = observed_hashes.get(result.suite_id) == expected_hash
            substantive = result.status not in {"not_run", "deferred"}
            attempt = {
                "run_id": manifest["run_id"],
                "run_dir": str(run_dir),
                "status": result.status,
                "validity": result.validity,
                "reason": result.reason,
                "suite_input_hash": observed_hashes.get(result.suite_id),
                "reusable": reusable,
                "selected": False,
            }
            attempts.setdefault(result.suite_id, []).append(attempt)
            if reusable and substantive:
                selected[result.suite_id] = result
    for suite_id, result in selected.items():
        for attempt in reversed(attempts[suite_id]):
            if attempt["reusable"] and attempt["status"] == result.status:
                attempt["selected"] = True
                break
    states = []
    missing = []
    for index, suite in enumerate(profile.suites, 1):
        result = selected.get(suite.suite_id)
        if result is None:
            missing.append(suite.suite_id)
        states.append(
            {
                "execution_id": f"{index:03d}-{suite.suite_id}",
                "suite_id": suite.suite_id,
                "mandatory": suite.mandatory,
                "long_test": suite.long_test,
                "selected": True,
                "status": "not_run" if result is None else result.status,
                "validity": "valid" if result is None else result.validity,
                "reason": "session_evidence_missing" if result is None else result.reason,
            }
        )
    synthetic_manifest = {
        "run_id": session["session_id"],
        "suite_states": states,
    }
    report = build_report(profile, list(selected.values()), synthetic_manifest)
    report.update(
        {
            "session_id": session["session_id"],
            "session_dir": str(session_dir.resolve()),
            "source_runs": [item["run_id"] for item in session.get("runs") or []],
            "missing_suites": missing,
            "suite_attempts": attempts,
            "contract_migrations": session.get("contract_migrations") or [],
        }
    )
    write_json(session_dir / "acceptance-session.json", report)
    markdown = render_markdown(report, profile)
    provenance = [
        "",
        "## Session provenance",
        "",
        f"Source runs: {', '.join(report['source_runs']) or 'none'}",
        f"Missing suites: {', '.join(missing) or 'none'}",
        "",
    ]
    (session_dir / "acceptance-session.md").write_text(
        markdown + "\n".join(provenance), encoding="utf-8"
    )
    return report
