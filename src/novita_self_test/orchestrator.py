from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import AdapterContext, adapter_for
from .artifact_bundle import write_artifact_bundle
from .artifacts import SuiteResult, enforce_completeness, utc_now, write_json
from .config import (
    ConfigError,
    JudgeAccess,
    ModelProfile,
    ServiceAccess,
    SuiteDefinition,
    results_root,
    workspace_root,
)
from .diagnosis import write_diagnosis
from .events import emit_event
from .reporting import load_suite_results, write_report
from .service_probe import probe_service


def _must_halt_acceptance(profile: ModelProfile, result: SuiteResult) -> bool:
    policy = profile.profile.get("acceptance_control") or {}
    global_stop = policy.get("global_stop") or {}
    return (
        result.validity in set(global_stop.get("validities") or [])
        or result.status in set(global_stop.get("statuses") or [])
        or result.reason in set(global_stop.get("reasons") or [])
    )


def _judge_access_for_plan(
    plan: list[tuple[SuiteDefinition, str | None, list[str]]],
    path: Path | None,
) -> JudgeAccess | None:
    beam = next((suite for suite, _, _ in plan if suite.adapter == "beam"), None)
    if beam is None:
        return JudgeAccess.load(path) if path is not None else None
    if path is None:
        raise ConfigError("judge_access_required", "selected BEAM suite requires --judge-access")
    judge = JudgeAccess.load(path)
    env = dict(beam.config.get("env") or {})
    fingerprint = dict(beam.config.get("runner_fingerprint") or {})
    expected = {
        "base_url": env.get("NVST_BEAM_JUDGE_BASE_URL"),
        "model": env.get("NVST_BEAM_JUDGE_MODEL"),
        "api_key_env": env.get("NVST_BEAM_JUDGE_API_KEY_ENV"),
        "requests_per_minute": int(fingerprint.get("judge_requests_per_minute", 0)),
    }
    observed = {
        "base_url": judge.base_url,
        "model": judge.model,
        "api_key_env": judge.api_key_env,
        "requests_per_minute": judge.requests_per_minute,
    }
    if observed != expected:
        raise ConfigError(
            "judge_access_mismatch",
            f"judge access does not match profile contract: expected={expected}, observed={observed}",
        )
    if not judge.credential():
        raise ConfigError(
            "judge_credential_env_missing",
            f"judge credential environment variable is not set: {judge.api_key_env}",
        )
    return judge


class UserInterrupted(RuntimeError):
    def __init__(self, run_dir: Path) -> None:
        super().__init__(f"run interrupted: {run_dir}")
        self.run_dir = run_dir


@dataclass(frozen=True)
class RunOutcome:
    run_dir: Path
    report: dict[str, Any]
    exit_code: int


def _run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}".lower()


def _git_pin(path: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
        ).stdout.decode().strip()
        branch_process = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            capture_output=True,
            check=True,
        )
        tracked_diff = subprocess.run(
            ["git", "-C", str(path), "diff", "--binary", "HEAD", "--"],
            capture_output=True,
            check=True,
        ).stdout
        untracked_raw = subprocess.run(
            ["git", "-C", str(path), "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            check=True,
        ).stdout
        untracked = sorted(item.decode() for item in untracked_raw.split(b"\0") if item)
        untracked_digest = hashlib.sha256()
        for relative in untracked:
            candidate = path / relative
            untracked_digest.update(relative.encode("utf-8") + b"\0")
            if candidate.is_file():
                untracked_digest.update(hashlib.sha256(candidate.read_bytes()).digest())
        return {
            "path": str(path.resolve()),
            "commit": commit,
            "branch": branch_process.stdout.decode().strip() or None,
            "tracked_dirty": bool(tracked_diff),
            "tracked_diff_sha256": hashlib.sha256(tracked_diff).hexdigest()
            if tracked_diff
            else None,
            "untracked_count": len(untracked),
            "untracked_sha256": untracked_digest.hexdigest() if untracked else None,
        }
    except (FileNotFoundError, subprocess.CalledProcessError):
        return {
            "path": str(path.resolve()),
            "commit": None,
            "branch": None,
            "tracked_dirty": None,
            "tracked_diff_sha256": None,
            "untracked_count": None,
            "untracked_sha256": None,
        }


def _vendored_pin(path: Path) -> dict[str, Any]:
    """Pin a read-only snapshot vendored inside this repository.

    Its content is covered by the ``novita_self_test`` pin; the upstream commit
    comes from the snapshot's ``VENDORED_COMMIT`` file.
    """
    try:
        commit = (path / "VENDORED_COMMIT").read_text(encoding="utf-8").strip() or None
    except OSError:
        commit = None
    return {
        "path": str(path.resolve()),
        "commit": commit,
        "branch": None,
        "tracked_dirty": None,
        "tracked_diff_sha256": None,
        "untracked_count": None,
        "untracked_sha256": None,
    }


def repository_pins(root: Path) -> dict[str, dict[str, Any]]:
    return {
        "novita_self_test": _git_pin(root),
        "kvv_k3": _vendored_pin((root / "vendor" / "kvv-k3").resolve()),
    }


def _dirty_repositories(pins: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        name
        for name, pin in pins.items()
        if pin.get("commit")
        and (pin.get("tracked_dirty") or int(pin.get("untracked_count") or 0) > 0)
    )


def _verify_run_environment(
    manifest: dict[str, Any],
    profile: ModelProfile,
    root: Path,
    *,
    allow_repo_pin_mismatch: bool = False,
    allow_profile_content_drift: bool = False,
) -> None:
    expected_hashes = manifest.get("profile_hashes")
    if (
        not allow_profile_content_drift
        and expected_hashes is not None
        and expected_hashes != profile.content_hashes()
    ):
        raise ConfigError(
            "profile_content_changed",
            "profile, requirements, manifest, or pinned fixture content changed after the run started",
        )
    expected_pins = manifest.get("repo_pins")
    if allow_repo_pin_mismatch:
        return
    if manifest.get("schema_version", 1) >= 2 and isinstance(expected_pins, dict):
        current_pins = repository_pins(root)
        keys = (
            "commit",
            "tracked_dirty",
            "tracked_diff_sha256",
            "untracked_count",
            "untracked_sha256",
        )
        mismatched = [
            name
            for name, expected in expected_pins.items()
            if name not in current_pins
            or any(expected.get(key) != current_pins[name].get(key) for key in keys)
        ]
        if mismatched:
            raise ConfigError(
                "repository_pin_mismatch",
                f"run repository state no longer matches: {', '.join(sorted(mismatched))}",
            )


def _verify_execution_site(
    profile: ModelProfile, manifest: dict[str, Any] | None = None
) -> str | None:
    execution_site = profile.require_execution_site()
    if manifest is not None and manifest.get("execution_site") != execution_site:
        raise ConfigError(
            "execution_site_mismatch",
            "run execution site does not match the committed model profile",
        )
    return execution_site


def _line_count(path: Path, *, numeric_score_only: bool = False) -> int:
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        if not numeric_score_only:
            count += 1
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload.get("score"), (int, float)):
            count += 1
    return count


def _suite_progress(suite: SuiteDefinition, suite_dir: Path) -> dict[str, Any]:
    if suite.adapter == "beam":
        answers = _line_count(suite_dir / "answers.jsonl")
        judge_answers = _line_count(suite_dir / "judge-answers.jsonl")
        judged = _line_count(suite_dir / "judge-scores.jsonl", numeric_score_only=True)
        return {
            "phase": "judging" if judge_answers else "generation",
            "completed": judged if judge_answers else answers,
            "total": judge_answers if judge_answers else 700,
        }
    if suite.adapter == "deepswe":
        checkpoint_path = suite_dir / "deepswe-checkpoint.json"
        if checkpoint_path.is_file():
            try:
                checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {"phase": "checkpoint_invalid", "completed": 0, "total": None}
            completed = 0
            for round_waves in checkpoint.get("waves", {}).values():
                for records in round_waves.values():
                    for record in records:
                        job_dir = Path(record.get("job_dir", ""))
                        if job_dir.is_dir():
                            completed += sum(1 for _ in job_dir.glob("*/result.json"))
            return {
                "phase": "trials",
                "completed": completed,
                "total": int(checkpoint.get("rounds", 0))
                * len(checkpoint.get("task_names") or []),
            }
        return {"phase": "preflight", "completed": 0, "total": None}
    return {"phase": "executing", "completed": 0, "total": None}


def _checkpoint_paths(suite: SuiteDefinition, suite_dir: Path) -> list[str]:
    if suite.adapter == "beam":
        return [
            str((suite_dir / "answers.jsonl").resolve()),
            str((suite_dir / "generation-result.json").resolve()),
            str((suite_dir / "beam-judge-process.json").resolve()),
            str((suite_dir / "judge-scores.jsonl").resolve()),
        ]
    if suite.adapter == "deepswe":
        return [str((suite_dir / "deepswe-checkpoint.json").resolve())]
    return []


@contextmanager
def _running_state(
    manifest: dict[str, Any],
    manifest_path: Path,
    state: dict[str, Any],
    suite: SuiteDefinition,
    suite_dir: Path,
):
    stopped = threading.Event()
    state.update(
        {
            "status": "running",
            "phase": "starting",
            "started_at": utc_now(),
            "finished_at": None,
            "heartbeat_at": utc_now(),
            "process_id": os.getpid(),
            "process_group_id": os.getpgrp(),
            "progress": _suite_progress(suite, suite_dir),
            "checkpoint_paths": _checkpoint_paths(suite, suite_dir),
        }
    )
    write_json(manifest_path, manifest)
    emit_event(
        manifest_path.parent,
        "suite_started",
        suite_id=suite.suite_id,
        phase="starting",
        data={"checkpoint_paths": state["checkpoint_paths"]},
    )

    def heartbeat() -> None:
        while not stopped.wait(5):
            state["heartbeat_at"] = utc_now()
            state["phase"] = _suite_progress(suite, suite_dir)["phase"]
            state["progress"] = _suite_progress(suite, suite_dir)
            write_json(manifest_path, manifest)
            emit_event(
                manifest_path.parent,
                "progress_updated",
                suite_id=suite.suite_id,
                phase=str(state["phase"]),
                data=dict(state["progress"]),
            )

    thread = threading.Thread(target=heartbeat, name=f"heartbeat-{suite.suite_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=10)
        state["heartbeat_at"] = utc_now()
        state["phase"] = "finishing"
        state["progress"] = _suite_progress(suite, suite_dir)
        write_json(manifest_path, manifest)
        emit_event(
            manifest_path.parent,
            "progress_updated",
            suite_id=suite.suite_id,
            phase="finishing",
            data=dict(state["progress"]),
        )


def _finish_state(state: dict[str, Any], result: SuiteResult) -> None:
    counts = result.counts
    completed = counts.get("completed")
    if not isinstance(completed, int):
        completed = sum(
            int(counts.get(key) or 0)
            for key in ("passed", "failed", "skipped", "error")
        )
    total = counts.get("total")
    if result.reason == "beam_judge_pending":
        state.update(
            {
                "status": "running",
                "validity": result.validity,
                "reason": result.reason,
                "phase": "judging",
                "finished_at": None,
                "heartbeat_at": utc_now(),
                "progress": {
                    "phase": "judging",
                    "completed": completed,
                    "total": total if isinstance(total, int) else None,
                },
                "checkpoint_paths": list(
                    dict.fromkeys(
                        list(state.get("checkpoint_paths") or [])
                        + [
                            path
                            for path in result.artifact_paths
                            if "checkpoint" in Path(path).name
                            or "scores" in Path(path).name
                            or "process" in Path(path).name
                        ]
                    )
                ),
            }
        )
        return
    state.update(
        {
            "status": "paused" if result.reason == "human_interrupted" else result.status,
            "validity": result.validity,
            "reason": result.reason,
            "phase": "paused" if result.reason == "human_interrupted" else "finished",
            "finished_at": None if result.reason == "human_interrupted" else utc_now(),
            "heartbeat_at": utc_now(),
            "progress": {
                "phase": "paused" if result.reason == "human_interrupted" else "finished",
                "completed": completed,
                "total": total if isinstance(total, int) else None,
            },
            "checkpoint_paths": list(
                dict.fromkeys(
                    list(state.get("checkpoint_paths") or [])
                    + [
                        path
                        for path in result.artifact_paths
                        if "checkpoint" in Path(path).name or "scores" in Path(path).name
                    ]
                )
            ),
        }
    )


def _state(
    execution_id: str,
    suite: SuiteDefinition,
    *,
    selected: bool,
    status: str = "not_run",
    validity: str = "valid",
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "execution_id": execution_id,
        "suite_id": suite.suite_id,
        "mandatory": suite.mandatory,
        "long_test": suite.long_test,
        "selected": selected,
        "status": status,
        "validity": validity,
        "reason": reason,
        "phase": "pending",
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": None,
        "process_id": None,
        "process_group_id": None,
        "progress": {"phase": "pending", "completed": 0, "total": None},
        "checkpoint_paths": [],
    }


def _write_suite_input(
    suite_dir: Path,
    *,
    suite: SuiteDefinition,
    service: ServiceAccess,
    selector: str | None,
    selector_args: list[str] | tuple[str, ...],
    acceptance_mode: str,
    concurrency_override: int | None,
    deepswe_rounds: int | None,
    result: SuiteResult | None = None,
) -> Path:
    """Persist the non-secret inputs and final result for one suite."""
    suite_dir.mkdir(parents=True, exist_ok=True)
    input_path = suite_dir / "suite-input.json"
    safe_headers = {
        key: value
        for key, value in service.headers.items()
        if key.lower()
        not in {"authorization", "proxy-authorization", "cookie", "set-cookie"}
    }
    payload: dict[str, Any] = {
        "schema_version": 1,
        "suite_id": suite.suite_id,
        "row_ids": list(suite.row_ids),
        "adapter": suite.adapter,
        "selector": selector,
        "selector_args": list(selector_args),
        "acceptance_mode": acceptance_mode,
        "concurrency_override": concurrency_override,
        "deepswe_rounds": deepswe_rounds,
        "service": {
            "model_family": service.model_family,
            "base_url": service.base_url,
            "served_model": service.served_model,
            "api_key_env": service.api_key_env,
            "headers": safe_headers,
            "think_mode": service.think_mode,
            "deployment_id": service.deployment_id,
            "runtime_id": service.runtime_id,
        },
        "config": suite.config,
        "result": None if result is None else result.to_dict(),
    }
    write_json(input_path, payload)
    if result is not None:
        result.artifact_paths = list(dict.fromkeys(result.artifact_paths + [str(input_path.resolve())]))
    return input_path


def _effective_completeness(
    suite: SuiteDefinition,
    selector: str | None,
    deepswe_rounds: int | None,
    acceptance_mode: str | None = None,
) -> dict[str, Any]:
    completeness = suite.completeness
    if acceptance_mode == "develop" and suite.config.get("develop_repeat") == 1:
        per_repeat = completeness.get("per_repeat")
        if isinstance(per_repeat, dict):
            return dict(per_repeat)
    selector_contracts = completeness.get("selector_completeness") or {}
    if selector and selector in selector_contracts:
        completeness = selector_contracts[selector]
    if suite.adapter == "deepswe" and deepswe_rounds is not None:
        completeness = dict(completeness)
        completeness["expected_rounds"] = int(deepswe_rounds)
        if "required_artifacts" in completeness:
            required_artifacts = []
            round_artifacts_added = False
            for artifact in completeness["required_artifacts"]:
                if re.fullmatch(r"round-[0-9]+-summary\.json", artifact):
                    if not round_artifacts_added:
                        required_artifacts.extend(
                            f"round-{index:02d}-summary.json"
                            for index in range(1, int(deepswe_rounds) + 1)
                        )
                        round_artifacts_added = True
                else:
                    required_artifacts.append(artifact)
            completeness["required_artifacts"] = required_artifacts
    return completeness


def _restore_checkpoint_evidence(
    suite: SuiteDefinition, archived_dir: Path, live_dir: Path
) -> list[str]:
    """Copy resumable checkpoint evidence back into the fresh suite directory.

    Continuation archives suite evidence before a rerun, but checkpointed
    adapters (DeepSWE, BEAM) are documented to resume from their checkpoints.
    The checkpoint file and the per-attempt logs it references are copied back
    so the rerun only schedules work that never produced a result.
    """
    restored: list[str] = []
    for raw in _checkpoint_paths(suite, archived_dir):
        archived_checkpoint = Path(raw)
        if not archived_checkpoint.is_file():
            continue
        live_dir.mkdir(parents=True, exist_ok=True)
        target = live_dir / archived_checkpoint.name
        shutil.copy2(archived_checkpoint, target)
        restored.append(str(target.resolve()))
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for round_waves in (payload.get("waves") or {}).values():
            if not isinstance(round_waves, dict):
                continue
            for records in round_waves.values():
                if not isinstance(records, list):
                    continue
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    for key in ("stdout_path", "stderr_path"):
                        name = Path(str(record.get(key) or "")).name
                        if not name:
                            continue
                        source = archived_dir / name
                        if source.is_file():
                            shutil.copy2(source, live_dir / name)
                            restored.append(str((live_dir / name).resolve()))
    return restored


class Orchestrator:
    def __init__(self, *, root: Path | None = None, service_probe=probe_service) -> None:
        self.root = (root or workspace_root()).resolve()
        self.service_probe = service_probe

    def _apply_failed_preflight(
        self,
        *,
        run_dir: Path,
        manifest: dict[str, Any],
        profile: ModelProfile,
        plan: list[tuple[SuiteDefinition, str | None, list[str]]],
        service: ServiceAccess,
        preflight: dict[str, Any],
    ) -> RunOutcome:
        preflight_path = run_dir / "service-preflight.json"
        write_json(preflight_path, preflight)
        manifest["service_preflight"] = {
            key: preflight.get(key)
            for key in ("status", "validity", "reason", "http_status")
        }
        results = []
        for index, (suite, selector, _) in enumerate(plan, start=1):
            result = SuiteResult(
                suite_id=suite.suite_id,
                row_ids=list(suite.row_ids),
                status="blocked",
                validity="service_invalid",
                reason=str(preflight.get("reason") or "service_preflight_failed"),
                failure_summary=preflight.get("failure_summary"),
                artifact_paths=[str(preflight_path.resolve())],
                started_at=utc_now(),
                finished_at=utc_now(),
            )
            suite_dir = run_dir / "suites" / f"{index:03d}-{suite.suite_id}"
            _write_suite_input(
                suite_dir,
                suite=suite,
                service=service,
                selector=selector,
                selector_args=[],
                acceptance_mode=str(manifest.get("acceptance_mode") or manifest.get("mode")),
                concurrency_override=manifest.get("concurrency_override"),
                deepswe_rounds=manifest.get("deepswe_rounds"),
                result=result,
            )
            result.write(suite_dir / "suite-result.json")
            results.append(result)
            manifest["suite_states"][index - 1].update(
                {"status": result.status, "validity": result.validity, "reason": result.reason}
            )
            if index == 1 and manifest["mode"] == "develop":
                write_diagnosis(
                    run_dir,
                    model_id=profile.model_id,
                    selector=selector or suite.suite_id,
                    service=service,
                    result=result,
                )
        manifest["finished_at"] = utc_now()
        write_json(run_dir / "run.json", manifest)
        report = write_report(run_dir, profile, results, manifest)
        write_artifact_bundle(
            run_dir,
            suite_results=results,
            run_manifest=manifest,
            report=report,
        )
        return RunOutcome(run_dir, report, 2)

    def execute(
        self,
        *,
        mode: str,
        model_id: str,
        service_access_path: Path,
        judge_access_path: Path | None = None,
        selectors: list[str] | None = None,
        include_long: set[str] | None = None,
        concurrency_override: int | None = None,
        deepswe_rounds_override: int | None = None,
        deepswe_supplemental_plan: dict[str, object] | None = None,
        deepswe_tasks: list[str] | None = None,
        continue_on_failure: bool = False,
        acceptance_mode: str | None = None,
        run_dir: Path | None = None,
    ) -> RunOutcome:
        if mode not in {"develop", "accept"}:
            raise ConfigError("mode_invalid", f"unsupported mode: {mode}")
        selectors = selectors or []
        include_long = include_long or set()
        acceptance_mode = acceptance_mode or ("develop" if mode == "develop" else "full")
        if mode == "develop" and not selectors:
            raise ConfigError("selector_required", "develop mode requires at least one --select")
        if mode == "accept" and selectors:
            raise ConfigError("selector_forbidden", "accept mode always loads the complete manifest")
        if mode == "develop" and acceptance_mode != "develop":
            raise ConfigError("acceptance_mode_invalid", "develop command only supports develop mode")
        if mode == "accept" and acceptance_mode not in {"simple", "full", "precision-core", "k3-sop"}:
            raise ConfigError(
                "acceptance_mode_invalid",
                "accept command requires simple, full, precision-core, or k3-sop mode",
            )
        if concurrency_override is not None and (
            isinstance(concurrency_override, bool)
            or not isinstance(concurrency_override, int)
            or concurrency_override < 1
        ):
            raise ConfigError("concurrency_invalid", "concurrency must be a positive integer")
        if mode == "accept" and concurrency_override is not None and (
            isinstance(concurrency_override, bool)
            or not isinstance(concurrency_override, int)
            or concurrency_override < 1
        ):
            raise ConfigError("concurrency_invalid", "acceptance concurrency must be a positive integer")
        if mode == "accept" and concurrency_override is not None and not continue_on_failure:
            raise ConfigError(
                "concurrency_override_forbidden",
                "acceptance concurrency override requires --continue-on-failure",
            )
        if deepswe_rounds_override is not None and (
            isinstance(deepswe_rounds_override, bool)
            or not isinstance(deepswe_rounds_override, int)
            or deepswe_rounds_override < 1
        ):
            raise ConfigError("deepswe_rounds_invalid", "deepswe rounds must be a positive integer")
        if mode == "accept" and deepswe_rounds_override is not None:
            raise ConfigError(
                "deepswe_rounds_override_forbidden",
                "acceptance runs use the profile DeepSWE round count; --deepswe-rounds is develop-only",
            )
        profile = ModelProfile.load(model_id, root=self.root)
        mode_config = profile.acceptance_mode_config(acceptance_mode)
        include_long |= set(mode_config.get("include_long") or [])
        deepswe_rounds = (
            deepswe_rounds_override
            if deepswe_rounds_override is not None
            else mode_config.get("deepswe_rounds")
        )
        if deepswe_tasks is not None:
            if mode != "develop" or selectors != ["10"]:
                raise ConfigError(
                    "deepswe_tasks_invalid",
                    "DeepSWE task subset requires develop mode with exactly --select 10",
                )
            if not deepswe_tasks or len(set(deepswe_tasks)) != len(deepswe_tasks):
                raise ConfigError("deepswe_tasks_invalid", "DeepSWE task subset must be non-empty and unique")
            if any(not isinstance(task, str) or not task or "/" in task or "\\\\" in task for task in deepswe_tasks):
                raise ConfigError("deepswe_tasks_invalid", "DeepSWE task names must be safe directory names")
        if deepswe_supplemental_plan is not None:
            deepswe_rows = sorted(
                {
                    row
                    for suite in profile.suites
                    if suite.suite_id == "deepswe"
                    for row in (suite.row_ids or [])
                }
            )
            if mode != "develop" or len(deepswe_rows) != 1 or selectors != deepswe_rows:
                raise ConfigError(
                    "deepswe_supplemental_invalid",
                    "supplemental recovery requires develop mode with exactly "
                    "the profile's DeepSWE row selected",
                )
            source_run_id = deepswe_supplemental_plan.get("source_run_id")
            round_tasks = deepswe_supplemental_plan.get("round_tasks")
            if (
                not isinstance(source_run_id, str)
                or not source_run_id
                or Path(source_run_id).name != source_run_id
                or not isinstance(round_tasks, list)
                or not round_tasks
            ):
                raise ConfigError(
                    "deepswe_supplemental_invalid",
                    "supplemental recovery requires a safe source run id and non-empty round/task list",
                )
            if concurrency_override is None:
                raise ConfigError(
                    "deepswe_supplemental_invalid",
                    "supplemental recovery requires an explicit low --concurrency",
                )
            for pair in round_tasks:
                if (
                    not isinstance(pair, dict)
                    or not isinstance(pair.get("round"), int)
                    or not 1 <= int(pair["round"]) <= int(deepswe_rounds or 0)
                    or not isinstance(pair.get("task"), str)
                    or not pair["task"]
                ):
                    raise ConfigError(
                        "deepswe_supplemental_invalid",
                        f"invalid supplemental round/task pair: {pair!r}",
                    )
        execution_site = _verify_execution_site(profile)
        service = ServiceAccess.load(service_access_path)
        accepted_families = set(profile.profile.get("model_families") or [model_id])
        if service.model_family not in accepted_families:
            raise ConfigError(
                "service_model_family_mismatch",
                f"service model_family {service.model_family!r} is not valid for {model_id}",
            )
        if not service.credential():
            raise ConfigError(
                "credential_env_missing", f"credential environment variable is not set: {service.api_key_env}"
            )
        unknown_long = include_long - {
            suite.long_test for suite in profile.suites if suite.long_test is not None
        }
        if unknown_long:
            raise ConfigError("long_test_unknown", f"unknown long tests: {sorted(unknown_long)}")

        pins = repository_pins(self.root)
        if mode == "accept":
            dirty = _dirty_repositories(pins)
            if dirty:
                raise ConfigError(
                    "repository_worktree_dirty",
                    "accept requires clean child repositories: " + ", ".join(dirty),
                )

        if mode == "develop":
            plan = [
                (suite, selector, args)
                for selector in selectors
                for suite, args in profile.select([selector])
            ]
        else:
            plan = [
                (suite, selector, args)
                for selector in list(mode_config.get("selectors") or [])
                for suite, args in profile.select([selector])
            ]
        judge = _judge_access_for_plan(plan, judge_access_path)

        run_dir = (run_dir or results_root(self.root) / model_id / _run_id()).resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
        states = [
            _state(f"{index:03d}-{suite.suite_id}", suite, selected=True)
            for index, (suite, _, _) in enumerate(plan, start=1)
        ]
        manifest: dict[str, Any] = {
            "schema_version": 2,
            "run_id": run_dir.name,
            "mode": mode,
            "acceptance_mode": acceptance_mode,
            "model": model_id,
            # The profile family (``model``) is not the deployed system: one K3
            # catalogue serves both ``moonshotai/kimi-k3-h`` and
            # ``kimi-k3-flash``. Freeze the observed deployment so results can be
            # grouped by served model without mixing them.
            "served_model": service.served_model,
            "execution_site": execution_site,
            "profile_path": str(profile.directory.resolve()),
            "requirements_snapshot_revision": profile.snapshot_revision,
            "service_access_path": str(service.source_path),
            "service_access_hash": service.sha256,
            "judge_access_path": None if judge is None else str(judge.source_path),
            "judge_access_hash": None if judge is None else judge.sha256,
            "selectors": selectors,
            "include_long": sorted(include_long),
            "concurrency_override": concurrency_override,
            "continue_on_failure": continue_on_failure,
            "deepswe_rounds": deepswe_rounds,
            "deepswe_supplemental_plan": deepswe_supplemental_plan,
            "repo_pins": pins,
            "profile_hashes": profile.content_hashes(),
            "suite_input_hashes": profile.suite_input_hashes(),
            "started_at": utc_now(),
            "finished_at": None,
            "suite_states": states,
        }
        write_json(run_dir / "run.json", manifest)
        emit_event(
            run_dir,
            "run_started",
            phase="preflight",
            data={"mode": mode, "selectors": selectors},
        )

        preflight = self.service_probe(service)
        preflight_path = run_dir / "service-preflight.json"
        write_json(preflight_path, preflight)
        manifest["service_preflight"] = {
            key: preflight.get(key)
            for key in ("status", "validity", "reason", "http_status")
        }
        write_json(run_dir / "run.json", manifest)
        emit_event(
            run_dir,
            "preflight_completed",
            phase="preflight",
            data={
                "status": preflight.get("status"),
                "validity": preflight.get("validity"),
                "reason": preflight.get("reason"),
            },
        )
        if preflight.get("status") != "passed":
            return self._apply_failed_preflight(
                run_dir=run_dir,
                manifest=manifest,
                profile=profile,
                plan=plan,
                service=service,
                preflight=preflight,
            )

        results: list[SuiteResult] = []
        pending_background: list[tuple[int, AdapterContext, Any, SuiteDefinition, str | None, list[str], Path]] = []
        halted_by: SuiteResult | None = None
        interrupted = False
        for index, (suite, selector, selector_args) in enumerate(plan, start=1):
            execution_id = f"{index:03d}-{suite.suite_id}"
            suite_dir = run_dir / "suites" / execution_id
            adapter_instance = None
            if suite.long_test and suite.long_test not in include_long:
                result = SuiteResult(
                    suite_id=suite.suite_id,
                    row_ids=list(suite.row_ids),
                    status="not_run",
                    reason="long_test_not_selected",
                    failure_summary=(
                        f"enable with: nvst accept --model {model_id} --service-access "
                        f"{service.source_path} --include-long {suite.long_test}"
                    ),
                    runner_fingerprint={
                        str(key): str(value)
                        for key, value in (suite.config.get("runner_fingerprint") or {}).items()
                    },
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
                states[index - 1]["selected"] = False
            elif halted_by is not None:
                result = SuiteResult(
                    suite_id=suite.suite_id,
                    row_ids=list(suite.row_ids),
                    status="blocked",
                    validity=halted_by.validity,
                    reason=f"blocked_by:{halted_by.suite_id}",
                    failure_summary="requests stopped after a service or runner contract failure",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
            else:
                context = AdapterContext(
                    suite=suite,
                    service=service,
                    run_dir=run_dir,
                    suite_dir=suite_dir,
                    repository_root=self.root,
                    selector=selector,
                    selector_args=tuple(selector_args),
                    concurrency_override=concurrency_override,
                    acceptance_mode=acceptance_mode,
                    deepswe_rounds=deepswe_rounds,
                    deepswe_supplemental_plan=deepswe_supplemental_plan,
                    deepswe_tasks=tuple(deepswe_tasks) if deepswe_tasks else None,
                )
                with _running_state(
                    manifest,
                    run_dir / "run.json",
                    states[index - 1],
                    suite,
                    suite_dir,
                ):
                    try:
                        adapter_instance = adapter_for(suite.adapter)
                        result = adapter_instance.run(context)
                    except KeyboardInterrupt:
                        result = SuiteResult(
                            suite_id=suite.suite_id,
                            row_ids=list(suite.row_ids),
                            status="deferred",
                            reason="human_interrupted",
                            started_at=utc_now(),
                            finished_at=utc_now(),
                        )
                        interrupted = True
                if adapter_instance is not None and getattr(adapter_instance, "has_pending_work", lambda _result: False)(result):
                    pending_background.append(
                        (
                            index - 1,
                            context,
                            adapter_instance,
                            suite,
                            selector,
                            list(selector_args),
                            suite_dir,
                        )
                    )
                else:
                    if deepswe_supplemental_plan is None:
                        result = enforce_completeness(
                            result,
                            _effective_completeness(
                                suite, selector, deepswe_rounds, acceptance_mode
                            ),
                        )

            suite_dir.mkdir(parents=True, exist_ok=True)
            _write_suite_input(
                suite_dir,
                suite=suite,
                service=service,
                selector=selector,
                selector_args=selector_args,
                acceptance_mode=acceptance_mode,
                concurrency_override=concurrency_override,
                deepswe_rounds=deepswe_rounds,
                result=result,
            )
            result.write(suite_dir / "suite-result.json")
            results.append(result)
            _finish_state(states[index - 1], result)
            emit_event(
                run_dir,
                "suite_completed",
                suite_id=suite.suite_id,
                phase=str(states[index - 1]["phase"]),
                data={
                    "status": result.status,
                    "validity": result.validity,
                    "reason": result.reason,
                    "counts": result.counts,
                    "metrics": result.metrics,
                },
            )
            failure_classes = dict(result.coverage.get("failure_class_counts") or {})
            degraded = {
                key: value
                for key, value in failure_classes.items()
                if key
                in {
                    "rate_limited",
                    "engine_overloaded",
                    "service_unavailable",
                    "timeout",
                    "connection_error",
                }
            }
            if degraded:
                emit_event(
                    run_dir,
                    "service_degraded",
                    suite_id=suite.suite_id,
                    phase="finished",
                    data={"failure_class_counts": degraded},
                )
            write_json(run_dir / "run.json", manifest)

            if mode == "develop" and (
                (result.status == "failed" and result.validity == "valid")
                or result.status == "blocked"
                or result.validity != "valid"
            ):
                write_diagnosis(
                    run_dir,
                    model_id=model_id,
                    selector=selector or suite.suite_id,
                    service=service,
                    result=result,
                )
                break
            if mode == "accept" and (
                (
                    not continue_on_failure
                    and result.status == "failed"
                    and not result.coverage.get("acceptance_non_blocking")
                )
                or _must_halt_acceptance(profile, result)
            ):
                halted_by = result
            if interrupted:
                halted_by = result

        if len(results) < len(plan):
            for index, (suite, _, _) in enumerate(plan[len(results) :], start=len(results) + 1):
                execution_id = f"{index:03d}-{suite.suite_id}"
                result = SuiteResult(
                    suite_id=suite.suite_id,
                    row_ids=list(suite.row_ids),
                    status="blocked" if interrupted else "not_run",
                    reason="human_interrupted" if interrupted else "develop_fail_fast",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
                suite_dir = run_dir / "suites" / execution_id
                _write_suite_input(
                    suite_dir,
                    suite=suite,
                    service=service,
                    selector=None,
                    selector_args=[],
                    acceptance_mode=acceptance_mode,
                    concurrency_override=concurrency_override,
                    deepswe_rounds=deepswe_rounds,
                    result=result,
                )
                result.write(suite_dir / "suite-result.json")
                results.append(result)
                states[index - 1].update(
                    {
                        "status": result.status,
                        "validity": result.validity,
                        "reason": result.reason,
                        "selected": False,
                    }
                )

        for result_index, context, adapter_instance, suite, selector, selector_args, suite_dir in pending_background:
            final_result = adapter_instance.finalize(context, results[result_index])
            final_result = enforce_completeness(
                final_result,
                _effective_completeness(
                    suite, selector, deepswe_rounds, acceptance_mode
                ),
            )
            results[result_index] = final_result
            _write_suite_input(
                suite_dir,
                suite=suite,
                service=service,
                selector=selector,
                selector_args=selector_args,
                acceptance_mode=acceptance_mode,
                concurrency_override=concurrency_override,
                deepswe_rounds=deepswe_rounds,
                result=final_result,
            )
            final_result.write(suite_dir / "suite-result.json")
            _finish_state(states[result_index], final_result)
            emit_event(
                run_dir,
                "suite_completed",
                suite_id=suite.suite_id,
                phase="judge",
                data={
                    "status": final_result.status,
                    "validity": final_result.validity,
                    "reason": final_result.reason,
                    "counts": final_result.counts,
                    "metrics": final_result.metrics,
                },
            )

        manifest["finished_at"] = None if interrupted else utc_now()
        write_json(run_dir / "run.json", manifest)
        report = write_report(run_dir, profile, results, manifest)
        emit_event(
            run_dir,
            "run_interrupted" if interrupted else "run_completed",
            phase="paused" if interrupted else "finished",
            data={"verdict": report["verdict"]},
        )
        write_artifact_bundle(
            run_dir,
            suite_results=results,
            run_manifest=manifest,
            report=report,
        )
        if interrupted:
            raise UserInterrupted(run_dir)
        if report["contract_invalid"]:
            exit_code = 2
        else:
            exit_code = 0 if report["verdict"] == "passed" else 1
        return RunOutcome(run_dir, report, exit_code)

    def resume(
        self,
        run_dir: Path,
        *,
        continue_from: str | None = None,
        continue_reason: str | None = None,
        allow_repo_pin_mismatch: bool = False,
        only_suite: bool = False,
        finalize_only: bool = False,
    ) -> RunOutcome:
        run_dir = run_dir.resolve()
        try:
            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ConfigError("run_manifest_invalid", f"cannot load run.json: {exc}") from exc
        if manifest.get("finished_at") and not continue_from:
            profile = ModelProfile.load(manifest.get("model", ""), root=self.root)
            _verify_execution_site(profile, manifest)
            if manifest.get("requirements_snapshot_revision") != profile.snapshot_revision:
                raise ConfigError(
                    "requirements_revision_mismatch",
                    "cannot re-report against a different requirements snapshot",
                )
            _verify_run_environment(manifest, profile, self.root)
            results = load_suite_results(run_dir)
            report = write_report(run_dir, profile, results, manifest)
            write_artifact_bundle(
                run_dir,
                suite_results=results,
                run_manifest=manifest,
                report=report,
            )
            exit_code = 2 if report["contract_invalid"] else (0 if report["verdict"] == "passed" else 1)
            return RunOutcome(run_dir, report, exit_code)

        model_id = manifest.get("model", "")
        profile = ModelProfile.load(model_id, root=self.root)
        _verify_execution_site(profile, manifest)
        if manifest.get("requirements_snapshot_revision") != profile.snapshot_revision:
            raise ConfigError(
                "requirements_revision_mismatch",
                "cannot resume against a different requirements snapshot",
            )
        _verify_run_environment(
            manifest,
            profile,
            self.root,
            allow_repo_pin_mismatch=allow_repo_pin_mismatch,
            allow_profile_content_drift=only_suite and allow_repo_pin_mismatch,
        )
        service = ServiceAccess.load(Path(manifest.get("service_access_path", "")))
        if service.sha256 != manifest.get("service_access_hash"):
            raise ConfigError(
                "service_access_changed", "service-access content changed after the run started"
            )
        if not service.credential():
            raise ConfigError(
                "credential_env_missing", f"credential environment variable is not set: {service.api_key_env}"
            )
        mode = manifest.get("mode")
        acceptance_mode = str(manifest.get("acceptance_mode") or ("develop" if mode == "develop" else "full"))
        mode_config = profile.acceptance_mode_config(acceptance_mode)
        selectors = list(manifest.get("selectors") or [])
        include_long = set(manifest.get("include_long") or [])
        include_long |= set(mode_config.get("include_long") or [])
        deepswe_rounds = manifest.get("deepswe_rounds", mode_config.get("deepswe_rounds"))
        deepswe_supplemental_plan = manifest.get("deepswe_supplemental_plan")
        deepswe_tasks = manifest.get("deepswe_tasks")
        continue_on_failure = bool(manifest.get("continue_on_failure", False))
        if deepswe_supplemental_plan is not None and not isinstance(
            deepswe_supplemental_plan, dict
        ):
            raise ConfigError(
                "run_manifest_invalid", "deepswe_supplemental_plan must be an object"
            )
        concurrency_override = manifest.get("concurrency_override")
        if concurrency_override is not None and (
            isinstance(concurrency_override, bool)
            or not isinstance(concurrency_override, int)
            or concurrency_override < 1
        ):
            raise ConfigError("run_manifest_invalid", "concurrency_override must be a positive integer")
        if mode == "accept" and concurrency_override is not None and not continue_on_failure:
            raise ConfigError(
                "concurrency_override_forbidden",
                "acceptance concurrency override requires --continue-on-failure",
            )
        if mode == "develop":
            plan = [
                (suite, selector, args)
                for selector in selectors
                for suite, args in profile.select([selector])
            ]
        elif mode == "accept":
            plan = [
                (suite, selector, args)
                for selector in list(mode_config.get("selectors") or [])
                for suite, args in profile.select([selector])
            ]
        else:
            raise ConfigError("run_manifest_invalid", f"invalid run mode: {mode}")
        judge_path_value = manifest.get("judge_access_path")
        judge = _judge_access_for_plan(
            plan,
            Path(judge_path_value) if isinstance(judge_path_value, str) else None,
        )
        if judge is not None and judge.sha256 != manifest.get("judge_access_hash"):
            raise ConfigError(
                "judge_access_changed", "judge-access content changed after the run started"
            )
        states = manifest.get("suite_states") or []
        if len(states) != len(plan):
            raise ConfigError("run_manifest_invalid", "suite plan no longer matches run manifest")
        if continue_from is not None:
            if mode != "accept" and not only_suite:
                raise ConfigError(
                    "continuation_mode_invalid",
                    "from_here continuation is only supported for managed acceptance runs",
                )
            if not continue_reason or not continue_reason.strip():
                raise ConfigError(
                    "continuation_reason_required",
                    "continue_from requires a human-supplied continuation reason",
                )
            continue_ids = [
                index
                for index, (suite, _, _) in enumerate(plan)
                if suite.suite_id == continue_from or f"{index + 1:03d}-{suite.suite_id}" == continue_from
            ]
            if not continue_ids:
                raise ConfigError(
                    "continuation_suite_not_found",
                    f"continue_from does not match this run's plan: {continue_from}",
                )
            start_index = continue_ids[0]
            suite, _, _ = plan[start_index]
            suite_contract_drift: dict[str, Any] | None = None
            if allow_repo_pin_mismatch:
                expected_suite_hashes = manifest.get("suite_input_hashes") or {}
                current_suite_hashes = profile.suite_input_hashes()
                if (
                    only_suite
                    and not finalize_only
                    and expected_suite_hashes.get(suite.suite_id) != current_suite_hashes.get(suite.suite_id)
                ):
                    raise ConfigError(
                        "suite_input_hash_mismatch",
                        "single-suite continuation requires the target suite input hash to match",
                    )
                if not only_suite and expected_suite_hashes != current_suite_hashes:
                    raise ConfigError(
                        "suite_input_hash_mismatch",
                        "repo pin mismatch cannot be approved because suite inputs changed",
                    )
                if finalize_only:
                    continuation_drift = {
                        "observed": expected_suite_hashes.get(suite.suite_id),
                        "current": current_suite_hashes.get(suite.suite_id),
                    }
                    if continuation_drift["observed"] != continuation_drift["current"]:
                        # finalize-only never sends traffic under a drifted
                        # contract; it only summarizes records already written.
                        suite_contract_drift = continuation_drift
            expected_failed = [index for index, state in enumerate(states) if state.get("status") == "failed"]
            if not only_suite and expected_failed and start_index != min(expected_failed):
                raise ConfigError(
                    "continuation_start_invalid",
                    "continue_from must name the first failed suite in the terminal run",
                )
            continuation_entry = {
                "from_suite": suite.suite_id,
                "from_execution_id": f"{start_index + 1:03d}-{suite.suite_id}",
                "reason": continue_reason.strip(),
                "requested_at": utc_now(),
                "allow_repo_pin_mismatch": allow_repo_pin_mismatch,
                "scope": "single_suite" if only_suite else "from_here",
                "finalize_only": finalize_only,
            }
            if suite_contract_drift:
                continuation_entry["suite_contract_drift"] = suite_contract_drift
            if only_suite:
                states[start_index].update(
                    _state(
                        f"{start_index + 1:03d}-{suite.suite_id}",
                        suite,
                        selected=True,
                    )
                )
            else:
                archive_id = f"{utc_now().replace(':', '').replace('+', 'Z')}-continued-from-{suite.suite_id}"
                archive_root = run_dir / "continuations" / archive_id
                archive_root.mkdir(parents=True, exist_ok=False)
                for filename in ("run.json", "acceptance.json", "acceptance.md", "events.jsonl", "test-artifacts"):
                    source = run_dir / filename
                    if source.exists():
                        shutil.move(str(source), str(archive_root / filename))
                restored_checkpoints: list[str] = []
                for offset in range(start_index, len(plan)):
                    suite_dir = run_dir / "suites" / f"{offset + 1:03d}-{plan[offset][0].suite_id}"
                    if suite_dir.exists():
                        destination = archive_root / "suites" / suite_dir.name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(suite_dir), str(destination))
                        restored_checkpoints.extend(
                            _restore_checkpoint_evidence(plan[offset][0], destination, suite_dir)
                        )
                    states[offset].update(
                        _state(
                            f"{offset + 1:03d}-{plan[offset][0].suite_id}",
                            plan[offset][0],
                            selected=True,
                        )
                    )
                continuation_entry.update(
                    {
                        "archived_at": archive_id,
                        "archive_path": str(archive_root.resolve()),
                        "restored_checkpoints": restored_checkpoints,
                    }
                )
            manifest["finished_at"] = None
            manifest.setdefault("continuations", []).append(continuation_entry)
            write_json(run_dir / "run.json", manifest)
            emit_event(
                run_dir,
                "run_continuation_started",
                phase="resuming",
                data={
                    "from_suite": suite.suite_id,
                    "from_execution_id": f"{start_index + 1:03d}-{suite.suite_id}",
                    "scope": "single_suite" if only_suite else "from_here",
                    "archive_path": continuation_entry.get("archive_path"),
                    "reason": continue_reason.strip(),
                },
            )
        else:
            resumable = [
                index
                for index, state in enumerate(states)
                if state.get("reason") == "human_interrupted"
                or state.get("reason") == "beam_judge_pending"
                or state.get("status") in {"paused", "interrupted"}
            ]
            if not resumable:
                raise ConfigError(
                    "resume_requires_interrupted_run", "unfinished run has no human_interrupted suite"
                )
            start_index = min(resumable)
        preflight = self.service_probe(service)
        preflight_path = run_dir / "service-preflight.json"
        write_json(preflight_path, preflight)
        manifest["service_preflight"] = {
            key: preflight.get(key)
            for key in ("status", "validity", "reason", "http_status")
        }
        stop_index = start_index + 1 if only_suite else len(plan)
        if preflight.get("status") != "passed":
            for offset in range(start_index, stop_index):
                suite, selector, _ = plan[offset]
                result = SuiteResult(
                    suite_id=suite.suite_id,
                    row_ids=list(suite.row_ids),
                    status="blocked",
                    validity="service_invalid",
                    reason=str(preflight.get("reason") or "service_preflight_failed"),
                    failure_summary=preflight.get("failure_summary"),
                    artifact_paths=[str(preflight_path.resolve())],
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
                suite_dir = run_dir / "suites" / f"{offset + 1:03d}-{suite.suite_id}"
                _write_suite_input(
                    suite_dir,
                    suite=suite,
                    service=service,
                    selector=selector,
                    selector_args=[],
                    acceptance_mode=acceptance_mode,
                    concurrency_override=concurrency_override,
                    deepswe_rounds=deepswe_rounds,
                    result=result,
                )
                result.write(suite_dir / "suite-result.json")
                states[offset].update(
                    {"status": result.status, "validity": result.validity, "reason": result.reason}
                )
                if offset == start_index and mode == "develop":
                    write_diagnosis(
                        run_dir,
                        model_id=model_id,
                        selector=selector or suite.suite_id,
                        service=service,
                        result=result,
                    )
            manifest["finished_at"] = utc_now()
            write_json(run_dir / "run.json", manifest)
            report = write_report(run_dir, profile, load_suite_results(run_dir), manifest)
            write_artifact_bundle(
                run_dir,
                suite_results=load_suite_results(run_dir),
                run_manifest=manifest,
                report=report,
            )
            return RunOutcome(run_dir, report, 2)
        halted_by: SuiteResult | None = None
        interrupted = False
        results = load_suite_results(run_dir)
        if continue_from is not None and len(results) < len(plan):
            results.extend(
                SuiteResult(
                    suite_id=suite.suite_id,
                    row_ids=list(suite.row_ids),
                    status="deferred",
                    validity="valid",
                    reason="continuation_pending",
                )
                for suite, _, _ in plan[len(results) :]
            )
        pending_background: list[tuple[int, AdapterContext, Any, SuiteDefinition, str | None, list[str], Path]] = []
        for offset in range(start_index, stop_index):
            suite, selector, selector_args = plan[offset]
            execution_id = f"{offset + 1:03d}-{suite.suite_id}"
            suite_dir = run_dir / "suites" / execution_id
            adapter_instance = None
            if suite.long_test and suite.long_test not in include_long:
                result = SuiteResult(
                    suite_id=suite.suite_id,
                    row_ids=list(suite.row_ids),
                    status="not_run",
                    reason="long_test_not_selected",
                    failure_summary=(
                        f"enable with: nvst accept --model {model_id} --service-access "
                        f"{service.source_path} --include-long {suite.long_test}"
                    ),
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
                states[offset]["selected"] = False
            elif halted_by is not None:
                result = SuiteResult(
                    suite_id=suite.suite_id,
                    row_ids=list(suite.row_ids),
                    status="blocked",
                    validity=halted_by.validity,
                    reason=f"blocked_by:{halted_by.suite_id}",
                    failure_summary="requests stopped after a service or runner contract failure",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                )
            else:
                context = AdapterContext(
                    suite=suite,
                    service=service,
                    run_dir=run_dir,
                    suite_dir=suite_dir,
                    repository_root=self.root,
                    selector=selector,
                    selector_args=tuple(selector_args),
                    concurrency_override=concurrency_override,
                    acceptance_mode=acceptance_mode,
                    deepswe_rounds=deepswe_rounds,
                    deepswe_supplemental_plan=deepswe_supplemental_plan,
                    deepswe_tasks=deepswe_tasks,
                    finalize_only=finalize_only,
                )
                _write_suite_input(
                    suite_dir,
                    suite=suite,
                    service=service,
                    selector=selector,
                    selector_args=selector_args,
                    acceptance_mode=acceptance_mode,
                    concurrency_override=concurrency_override,
                    deepswe_rounds=deepswe_rounds,
                )
                with _running_state(
                    manifest,
                    run_dir / "run.json",
                    states[offset],
                    suite,
                    suite_dir,
                ):
                    try:
                        adapter_instance = adapter_for(suite.adapter)
                        result = adapter_instance.run(context)
                    except KeyboardInterrupt:
                        result = SuiteResult(
                            suite_id=suite.suite_id,
                            row_ids=list(suite.row_ids),
                            status="deferred",
                            reason="human_interrupted",
                            started_at=utc_now(),
                            finished_at=utc_now(),
                        )
                        interrupted = True
                if adapter_instance is not None and getattr(adapter_instance, "has_pending_work", lambda _result: False)(result):
                    pending_background.append(
                        (
                            offset,
                            context,
                            adapter_instance,
                            suite,
                            selector,
                            list(selector_args),
                            suite_dir,
                        )
                    )
                else:
                    if deepswe_supplemental_plan is None:
                        result = enforce_completeness(
                            result,
                            _effective_completeness(
                                suite, selector, deepswe_rounds, acceptance_mode
                            ),
                        )
            _write_suite_input(
                suite_dir,
                suite=suite,
                service=service,
                selector=selector,
                selector_args=selector_args,
                acceptance_mode=acceptance_mode,
                concurrency_override=concurrency_override,
                deepswe_rounds=deepswe_rounds,
                result=result,
            )
            result.write(suite_dir / "suite-result.json")
            if offset < len(results):
                results[offset] = result
            else:
                results.append(result)
            _finish_state(states[offset], result)
            write_json(run_dir / "run.json", manifest)
            if mode == "develop" and (
                (result.status == "failed" and result.validity == "valid")
                or result.status == "blocked"
                or result.validity != "valid"
            ):
                write_diagnosis(
                    run_dir,
                    model_id=model_id,
                    selector=selector or suite.suite_id,
                    service=service,
                    result=result,
                )
                for remaining in range(offset + 1, len(plan)):
                    states[remaining].update(
                        {
                            "status": "not_run",
                            "validity": "valid",
                            "reason": "develop_fail_fast",
                            "selected": False,
                        }
                    )
                break
            if mode == "accept" and (
                (
                    not continue_on_failure
                    and result.status == "failed"
                    and not result.coverage.get("acceptance_non_blocking")
                )
                or _must_halt_acceptance(profile, result)
            ):
                halted_by = result
            if interrupted:
                for remaining in range(offset + 1, len(plan)):
                    states[remaining].update(
                        {"status": "not_run", "validity": "valid", "reason": "human_interrupted"}
                    )
                break

        for result_index, context, adapter_instance, suite, selector, selector_args, suite_dir in pending_background:
            final_result = adapter_instance.finalize(context, results[result_index])
            final_result = enforce_completeness(
                final_result,
                _effective_completeness(
                    suite, selector, deepswe_rounds, acceptance_mode
                ),
            )
            results[result_index] = final_result
            _write_suite_input(
                suite_dir,
                suite=suite,
                service=service,
                selector=selector,
                selector_args=selector_args,
                acceptance_mode=acceptance_mode,
                concurrency_override=concurrency_override,
                deepswe_rounds=deepswe_rounds,
                result=final_result,
            )
            final_result.write(suite_dir / "suite-result.json")
            _finish_state(states[result_index], final_result)
            emit_event(
                run_dir,
                "suite_completed",
                suite_id=suite.suite_id,
                phase="judge",
                data={
                    "status": final_result.status,
                    "validity": final_result.validity,
                    "reason": final_result.reason,
                    "counts": final_result.counts,
                    "metrics": final_result.metrics,
                },
            )

        manifest["finished_at"] = None if interrupted else utc_now()
        write_json(run_dir / "run.json", manifest)
        results = load_suite_results(run_dir)
        report = write_report(run_dir, profile, results, manifest)
        emit_event(
            run_dir,
            "run_interrupted" if interrupted else "run_completed",
            phase="paused" if interrupted else "finished",
            data={"verdict": report["verdict"]},
        )
        write_artifact_bundle(
            run_dir,
            suite_results=results,
            run_manifest=manifest,
            report=report,
        )
        if interrupted:
            raise UserInterrupted(run_dir)
        exit_code = 2 if report["contract_invalid"] else (0 if report["verdict"] == "passed" else 1)
        return RunOutcome(run_dir, report, exit_code)
