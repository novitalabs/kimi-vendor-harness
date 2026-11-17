from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from novita_self_test.artifacts import write_versioned_json
from novita_self_test.events import emit_event
from novita_self_test.stability import (
    CREDENTIAL_INVALID,
    classify_failure,
    failure_class_counts,
    service_invalid_reason,
)

DEFAULT_PIER_AGENT_TIMEOUT_SECONDS = 5400


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(command, text=True, capture_output=True, check=False, **kwargs)
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or f"command failed: {command[0]}")
    return process


def proxy_free_docker_config(directory: Path) -> Path:
    """Write a minimal Docker client config with no proxy block.

    A Docker client config ``proxies`` block makes the client inject
    ``HTTP_PROXY``/``HTTPS_PROXY``/``http_proxy``/``https_proxy`` into every
    container it creates. Task verifiers legitimately exercise environment
    proxy handling, and Python's ``getproxies_environment`` lets an empty
    ``*_proxy`` value pop a proxy a test just set, so a blank override is not
    equivalent to absence. Pointing the client at a config without the proxy
    block is the only form that leaves those variables unset, which is what the
    task verifier needs; the registry ``auths`` block is intentionally not
    copied and the pinned task images and build inputs are public.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    path.write_text("{}\n", encoding="utf-8")
    return path


def verify_runtime(
    *,
    runtime_root: str,
    expected_commit: str,
    expected_pier_version: str,
    expected_tasks: int,
    pier_bin: str,
    runner=run,
) -> dict[str, Any]:
    repository = f"{runtime_root}/deep-swe"
    tasks = f"{repository}/tasks"

    try:
        commit = runner(["git", "-C", repository, "rev-parse", "HEAD"]).stdout.strip()
        dirty = runner(
            ["git", "-C", repository, "status", "--porcelain", "--untracked-files=no"]
        ).stdout.strip()
        version_output = runner([pier_bin, "--version"]).stdout.strip()
        task_paths = runner(
            ["find", tasks, "-mindepth", "1", "-maxdepth", "1", "-type", "d"]
        ).stdout.strip()
    except (OSError, RuntimeError) as exc:
        return {
            "schema_version": 1,
            "status": "blocked",
            "reason": "local_preflight_failed",
            "transport": "local",
            "runtime_root": runtime_root,
            "expected": {
                "deep_swe_commit": expected_commit,
                "pier_version": expected_pier_version,
                "tasks": expected_tasks,
                "tracked_worktree_clean": True,
            },
            "observed": {},
            "failure_summary": str(exc),
        }

    match = re.search(r"\d+\.\d+\.\d+", version_output)
    pier_version = match.group(0) if match else version_output
    task_count = len([line for line in task_paths.splitlines() if line.strip()])
    observed = {
        "deep_swe_commit": commit,
        "pier_version": pier_version,
        "tasks": task_count,
        "tracked_worktree_clean": not bool(dirty),
    }
    expected = {
        "deep_swe_commit": expected_commit,
        "pier_version": expected_pier_version,
        "tasks": expected_tasks,
        "tracked_worktree_clean": True,
    }
    mismatches = {
        key: {"expected": value, "observed": observed.get(key)}
        for key, value in expected.items()
        if observed.get(key) != value
    }
    return {
        "schema_version": 1,
        "status": "passed" if not mismatches else "blocked",
        "reason": None if not mismatches else "local_pin_mismatch",
        "transport": "local",
        "runtime_root": runtime_root,
        "expected": expected,
        "observed": observed,
        "mismatches": mismatches,
        "failure_summary": None if not mismatches else f"runtime pin mismatch: {mismatches}",
    }


def blocked_result(preflight_path: Path, preflight: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "suite_id": "deepswe",
        "row_ids": ["17"],
        "status": "blocked",
        "validity": "execution_invalid",
        "counts": {},
        "metrics": {},
        "redacted_command": [],
        "artifact_paths": [str(preflight_path.resolve())],
        "failure_summary": preflight.get("failure_summary"),
        "runner_fingerprint": {"transport": "local"},
        "coverage": {},
        "reason": preflight.get("reason") or "local_preflight_failed",
    }


def _request_evidence_dir(result_path: Path) -> str | None:
    """Locate the host-mounted /logs/agent evidence for a Pier trial."""
    trial_dir = result_path.parent
    candidates = (
        trial_dir / "logs" / "agent" / "request-evidence",
        trial_dir / "agent" / "request-evidence",
        trial_dir / "request-evidence",
    )
    observed = next((path for path in candidates if path.exists()), candidates[0])
    return str(observed.resolve())


def read_wave_results(
    job_dir: Path, selected_tasks: list[str], *, secret: str = ""
) -> dict[str, dict[str, Any]]:
    selected = set(selected_tasks)
    outcomes: dict[str, dict[str, Any]] = {}
    for path in sorted(job_dir.glob("*/result.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            task = Path(payload["config"]["task"]["path"]).name
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
            continue
        if task not in selected or task in outcomes:
            continue
        verifier_result = payload.get("verifier_result") or {}
        rewards = verifier_result.get("rewards") or {}
        raw_reward = rewards.get("reward")
        verifier_reward = int(raw_reward) if raw_reward in (0, 1, 0.0, 1.0) else None
        exception = payload.get("exception_info") or {}
        exception_type = exception.get("exception_type")
        exception_message = str(exception.get("exception_message") or "").replace(
            secret, "[REDACTED]"
        ) if secret else str(exception.get("exception_message") or "")
        failure_class = classify_failure(exception_message) if exception_message else "execution_unknown"
        retryable_incomplete = verifier_reward is None or (
            verifier_reward == 0 and bool(exception)
        )
        reward = None if retryable_incomplete else verifier_reward
        agent_result = payload.get("agent_result") or {}
        outcomes[task] = {
            "trial_name": payload.get("trial_name"),
            "reward": reward,
            "verifier_reward": verifier_reward,
            "retryable_incomplete": retryable_incomplete,
            "exception_type": exception_type,
            "failure_class": failure_class,
            "exception_message": exception_message or None,
            "output_tokens": agent_result.get("n_output_tokens"),
            "agent_steps": agent_result.get("n_steps"),
            "partial": rewards.get("partial"),
            "f2p_total": rewards.get("f2p_total"),
            "f2p_passed": rewards.get("f2p_passed"),
            "p2p_total": rewards.get("p2p_total"),
            "p2p_passed": rewards.get("p2p_passed"),
            "result_path": str(path.resolve()),
            "request_evidence_dir": _request_evidence_dir(path),
        }
    for task in selected_tasks:
        outcomes.setdefault(
            task,
            {
                "trial_name": None,
                "reward": None,
                "verifier_reward": None,
                "retryable_incomplete": True,
                "exception_type": "TrialResultMissing",
                "failure_class": "execution_unknown",
                "exception_message": "trial result is missing",
                "output_tokens": None,
                "agent_steps": None,
                "partial": None,
                "f2p_total": None,
                "f2p_passed": None,
                "p2p_total": None,
                "p2p_passed": None,
                "result_path": None,
                "request_evidence_dir": None,
            },
        )
    return outcomes


def summarize_round(
    task_names: list[str], wave_results: list[dict[str, dict[str, Any]]]
) -> dict[str, Any]:
    task_results: dict[str, dict[str, Any]] = {}
    for task in task_names:
        attempts = [wave[task] for wave in wave_results if task in wave]
        rewards = [attempt["reward"] for attempt in attempts]
        passed = 1 in rewards
        explicit_not_pass = not passed and 0 in rewards
        task_results[task] = {
            "status": "passed" if passed else "not_pass" if explicit_not_pass else "unclear",
            "attempts": attempts,
        }
    passed_tasks = sum(item["status"] == "passed" for item in task_results.values())
    not_pass_tasks = sum(item["status"] == "not_pass" for item in task_results.values())
    exec_unknown_tasks = sum(item["status"] == "unclear" for item in task_results.values())
    unclear_tasks = sorted(
        task for task, item in task_results.items() if item["status"] == "unclear"
    )
    return {
        "score": passed_tasks / len(task_names) if task_names else 0.0,
        "passed_tasks": passed_tasks,
        "not_pass_tasks": not_pass_tasks,
        "exec_unknown_tasks": exec_unknown_tasks,
        "unclear_tasks": unclear_tasks,
        "task_results": task_results,
    }


def _write_task_results(path: Path, round_summaries: list[dict[str, Any]]) -> None:
    """Persist one inspectable JSONL record for every round/task result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for summary in round_summaries:
        round_index = int(summary["round"])
        for task, result in sorted((summary.get("task_results") or {}).items()):
            lines.append(
                json.dumps(
                    {
                        "schema_version": 1,
                        "round": round_index,
                        "task": task,
                        "status": result.get("status"),
                        "attempts": result.get("attempts") or [],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_benchmark_mixed(
    *,
    output_dir: Path,
    runtime_root: str,
    pier_bin: str,
    mode: str,
    concurrency: int,
    expected_tasks: int,
    rounds: int,
    max_reruns: int,
    kimi_code_version: str,
    model: str,
    execution_env: dict[str, str],
    preflight: dict[str, Any],
) -> dict[str, Any]:
    # Legacy alias: the checkpointed scheduler now implements the official
    # mixed round/task scheduling with a task-local lock forbidding same-task
    # overlap.
    return run_benchmark_checkpointed(
        output_dir=output_dir,
        runtime_root=runtime_root,
        pier_bin=pier_bin,
        mode=mode,
        concurrency=concurrency,
        expected_tasks=expected_tasks,
        rounds=rounds,
        max_reruns=max_reruns,
        kimi_code_version=kimi_code_version,
        model=model,
        execution_env=execution_env,
        preflight=preflight,
    )


def _load_checkpoint(
    output_dir: Path,
    *,
    mode: str,
    task_names: list[str],
    rounds: int,
    max_reruns: int,
    concurrency: int,
    max_concurrency: int = 113,
    agent_timeout_multiplier: float = 1.0,
    tolerate_drift: bool = False,
) -> tuple[Path, dict[str, Any]]:
    path = output_dir / "deepswe-checkpoint.json"
    if path.is_file():
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "mode": mode,
            "task_names": task_names,
            "rounds": rounds,
            "max_reruns": max_reruns,
            "concurrency": concurrency,
            "max_concurrency": max_concurrency,
            "agent_timeout_multiplier": agent_timeout_multiplier,
            "round_scheduling": "mixed_round_task_locked",
            "max_parts_per_wave": max_reruns + 1,
        }
        mismatches = {
            key: {"expected": value, "observed": checkpoint.get(key)}
            for key, value in expected.items()
            if checkpoint.get(key) != value
        }
        if tolerate_drift and set(mismatches) <= {"max_reruns", "max_parts_per_wave"}:
            # Finalize-only repair: the execution happened under an older retry
            # budget. Summarization is wave-content based, so the budget fields
            # are the only harmless drift; anything else stays fatal.
            checkpoint["finalize_drift"] = mismatches
            mismatches = {}
        if mismatches:
            raise RuntimeError(f"DeepSWE checkpoint contract changed: {mismatches}")
        return path, checkpoint
    stamp = subprocess.run(
        ["date", "-u", "+%Y%m%dT%H%M%SZ"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    checkpoint = {
        "schema_version": 1,
        "mode": mode,
        "stamp": stamp,
        "task_names": task_names,
        "rounds": rounds,
        "max_reruns": max_reruns,
        "concurrency": concurrency,
        "max_concurrency": max_concurrency,
        "agent_timeout_multiplier": agent_timeout_multiplier,
        "round_scheduling": "mixed_round_task_locked",
        "max_parts_per_wave": max_reruns + 1,
        "waves": {str(index): {} for index in range(1, rounds + 1)},
        "created_at": stamp,
        "updated_at": stamp,
    }
    write_versioned_json(
        path,
        checkpoint,
        schema_name="deepswe-checkpoint.schema.json",
    )
    run_dir = output_dir.parents[1]
    if (run_dir / "run.json").is_file():
        emit_event(
            run_dir,
            "checkpoint_written",
            suite_id="deepswe",
            phase="checkpoint_created",
            data={"path": str(path.resolve()), "revision": checkpoint["revision"]},
        )
    return path, checkpoint


def _checkpoint_records(
    checkpoint: dict[str, Any], round_index: int, wave_index: int
) -> list[dict[str, Any]]:
    waves = checkpoint.setdefault("waves", {}).setdefault(str(round_index), {})
    return waves.setdefault(str(wave_index), [])


def _merged_checkpoint_results(
    records: list[dict[str, Any]], selected_tasks: list[str], *, secret: str = ""
) -> dict[str, dict[str, Any]]:
    merged = {
        task: {
            "trial_name": None,
            "reward": None,
            "verifier_reward": None,
            "retryable_incomplete": True,
            "exception_type": "TrialResultMissing",
            "exception_message": "trial result is missing",
            "output_tokens": None,
            "agent_steps": None,
            "partial": None,
            "f2p_total": None,
            "f2p_passed": None,
            "p2p_total": None,
            "p2p_passed": None,
            "result_path": None,
            "request_evidence_dir": None,
        }
        for task in selected_tasks
    }
    for record in records:
        job_dir = Path(record["job_dir"])
        observed = read_wave_results(
            job_dir, list(record.get("selected_tasks") or []), secret=secret
        )
        for task, outcome in observed.items():
            if task in merged and outcome.get("reward") is not None:
                merged[task] = outcome
    return merged


def cleanup_checkpoint_resources(checkpoint_path: Path) -> dict[str, Any]:
    if not checkpoint_path.is_file():
        return {"status": "not_found", "stopped_containers": []}
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    job_dirs = {
        str(Path(record["job_dir"]).resolve())
        for round_waves in checkpoint.get("waves", {}).values()
        for records in round_waves.values()
        for record in records
        if record.get("job_dir")
    }
    process = subprocess.run(
        [
            "docker",
            "ps",
            "--format",
            '{{.ID}}\t{{.Label "com.docker.compose.project.config_files"}}',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    owned = []
    for line in process.stdout.splitlines():
        container_id, _, config_files = line.partition("\t")
        if container_id and any(job_dir in config_files for job_dir in job_dirs):
            owned.append(container_id)
    if owned:
        subprocess.run(
            ["docker", "stop", *owned],
            text=True,
            capture_output=True,
            check=False,
        )
    return {"status": "stopped", "stopped_containers": owned}


def run_benchmark_checkpointed(
    *,
    output_dir: Path,
    runtime_root: str,
    pier_bin: str,
    mode: str,
    concurrency: int,
    expected_tasks: int,
    rounds: int,
    max_reruns: int,
    kimi_code_version: str,
    model: str,
    execution_env: dict[str, str],
    preflight: dict[str, Any],
    max_concurrency: int = 113,
    finalize_only: bool = False,
    task_subset: list[str] | None = None,
    agent_timeout_multiplier: float = 1.0,
) -> dict[str, Any]:
    if max_reruns != 1:
        raise ValueError("DeepSWE max_reruns is fixed at 1")
    if agent_timeout_multiplier <= 0:
        raise ValueError("DeepSWE agent_timeout_multiplier must be positive")
    if not 1 <= concurrency <= max_concurrency:
        raise ValueError(
            f"DeepSWE concurrency must be between 1 and max_concurrency={max_concurrency}"
        )
    tasks_root = Path(runtime_root) / "deep-swe" / "tasks"
    all_task_names = sorted(path.name for path in tasks_root.iterdir() if path.is_dir())
    if len(all_task_names) != expected_tasks:
        raise RuntimeError(f"expected {expected_tasks} task directories, found {len(all_task_names)}")
    task_names = list(task_subset) if task_subset else all_task_names
    unknown_tasks = sorted(set(task_names) - set(all_task_names))
    if unknown_tasks:
        raise RuntimeError(f"unknown DeepSWE task names: {unknown_tasks}")
    jobs_root = Path(runtime_root) / "jobs"
    checkpoint_task_names = list(task_subset) if task_subset else task_names
    checkpoint_path, checkpoint = _load_checkpoint(
        output_dir,
        mode=mode,
        task_names=checkpoint_task_names,
        rounds=rounds,
        max_reruns=max_reruns,
        concurrency=concurrency,
        max_concurrency=max_concurrency,
        agent_timeout_multiplier=agent_timeout_multiplier,
        tolerate_drift=finalize_only,
    )
    secret = execution_env.get("KIMI_API_KEY", "")
    stability_texts: list[str] = []
    blocking_service_reason: str | None = None
    checkpoint_lock = threading.Lock()
    task_locks = {task: threading.Lock() for task in task_names}

    def save_checkpoint() -> None:
        with checkpoint_lock:
            checkpoint["updated_at"] = subprocess.run(
                ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            write_versioned_json(
                checkpoint_path,
                checkpoint,
                schema_name="deepswe-checkpoint.schema.json",
            )
            run_dir = output_dir.parents[1]
            if (run_dir / "run.json").is_file():
                emit_event(
                    run_dir,
                    "checkpoint_written",
                    suite_id="deepswe",
                    phase="checkpoint_updated",
                    data={
                        "path": str(checkpoint_path.resolve()),
                        "revision": checkpoint["revision"],
                    },
                )

    def execute_record(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        task = str(record["task"])
        # A task-local lock is held for the complete Pier/Harbor lifecycle, so
        # the mixed round/task scheduler can never overlap the same task's
        # Harbor workspace.
        with task_locks[task]:
            command = [
                pier_bin,
                "run",
                "-p",
                str(tasks_root),
                "--agent-import-path",
                "pier_kimi.kimi_code:KimiCode",
                "--model",
                f"kimi/{model}",
                "--ak",
                f"version={kimi_code_version}",
                "--env",
                "docker",
                "--n-concurrent",
                "1",
                "--n-attempts",
                "1",
                "--max-retries",
                "0",
                "--agent-timeout-multiplier",
                str(agent_timeout_multiplier),
                "--jobs-dir",
                str(jobs_root),
                "--job-name",
                record["job_name"],
                "--include-task-name",
                task,
            ]
            process = subprocess.run(
                command,
                env=execution_env,
                text=True,
                capture_output=True,
                check=False,
            )
            stdout_path = Path(record["stdout_path"])
            stderr_path = Path(record["stderr_path"])
            stdout_path.write_text(
                process.stdout.replace(secret, "[REDACTED]"), encoding="utf-8"
            )
            stderr_text = process.stderr.replace(secret, "[REDACTED]")
            stderr_path.write_text(stderr_text, encoding="utf-8")
            finished = {
                **record,
                "status": "finished",
                "process_returncode": process.returncode,
                "runner_concurrency": 1,
            }
            with checkpoint_lock:
                record.update(finished)
                checkpoint["updated_at"] = subprocess.run(
                    ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                write_versioned_json(
                    checkpoint_path,
                    checkpoint,
                    schema_name="deepswe-checkpoint.schema.json",
                )
            return finished, stderr_text[-4000:]

    def create_pending_records(
        wave_index: int,
        round_index: int,
        selected_tasks: list[str],
    ) -> list[dict[str, Any]]:
        pending_records: list[dict[str, Any]] = []
        records = _checkpoint_records(checkpoint, round_index, wave_index)
        for task in selected_tasks:
            observed = _merged_checkpoint_results(records, [task], secret=secret)[task]
            if observed.get("reward") is not None:
                continue
            task_parts = [record for record in records if record.get("task") == task]
            # A fresh retry wave has one part per task. If a run is resumed
            # while a task's previous part was interrupted, add a replacement
            # part in the same logical wave without overlapping the task lock.
            if len(task_parts) >= int(checkpoint["max_parts_per_wave"]):
                continue
            part = len(task_parts) + 1
            task_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", task)[:80]
            job_suffix = f"task-{task_label}-part-{part:02d}"
            record = {
                    "round": round_index,
                    "attempt": wave_index,
                    "part": part,
                    "task": task,
                    "selected_tasks": [task],
                    "requested_concurrency": concurrency,
                    "runner_concurrency": 1,
                    "job_name": (
                        f"nvst-deepswe-{mode}-{checkpoint['stamp']}-round-{round_index:02d}-"
                        f"attempt-{wave_index:02d}-{job_suffix}"
                    ),
                    "job_dir": str(
                        (
                            jobs_root
                            / (
                                f"nvst-deepswe-{mode}-{checkpoint['stamp']}-round-"
                                f"{round_index:02d}-attempt-{wave_index:02d}-{job_suffix}"
                            )
                        ).resolve()
                    ),
                    "stdout_path": str(
                        (
                            output_dir
                            / (
                                f"round-{round_index:02d}-attempt-{wave_index:02d}-"
                                f"{job_suffix}.stdout.log"
                            )
                        ).resolve()
                    ),
                    "stderr_path": str(
                        (
                            output_dir
                            / (
                                f"round-{round_index:02d}-attempt-{wave_index:02d}-"
                                f"{job_suffix}.stderr.log"
                            )
                        ).resolve()
                    ),
                    "status": "running",
                }
            records.append(record)
            pending_records.append(record)
        if pending_records:
            save_checkpoint()
        return pending_records

    stop_event = threading.Event()

    def note_completion(stderr_text: str) -> None:
        nonlocal blocking_service_reason
        reason = service_invalid_reason([stderr_text])
        with checkpoint_lock:
            stability_texts.append(stderr_text)
            if reason is not None and blocking_service_reason is None:
                blocking_service_reason = reason
        if reason is not None:
            stop_event.set()

    def pair_needs_retry(round_index: int, task: str) -> bool:
        """Only an unfinished (execution-invalid) attempt is retried.

        A complete attempt carries an explicit reward 0 or 1 and is scored as a
        model result; rerunning it would measure the retry policy instead of
        the model. Only a pair whose attempts never produced a verifier reward
        — an agent exception, timeout, or interrupted run — is retried.
        """
        for wave_index in range(1, max_reruns + 2):
            records = [
                record
                for record in _checkpoint_records(checkpoint, round_index, wave_index)
                if record.get("task") == task
            ]
            if not records:
                continue
            observed = _merged_checkpoint_results(records, [task], secret=secret)
            if observed[task].get("reward") is not None:
                return False
        return True

    def run_formal_pipeline(task: str) -> None:
        # Official mixed round/task scheduling: each task advances through its
        # formal rounds as soon as its previous attempt completes, without
        # waiting for other tasks. The task-local lock inside execute_record
        # structurally forbids same-task overlap.
        for round_index in range(1, rounds + 1):
            if stop_event.is_set():
                return
            if not pair_needs_retry(round_index, task):
                # A pair already holding a reward in any wave is complete. On
                # resume it must not receive another formal replacement part;
                # a completed reward=0 stays a scored model failure.
                continue
            for record in create_pending_records(1, round_index, [task]):
                _, stderr_text = execute_record(record)
                note_completion(stderr_text)

    formal_workers = min(max(1, concurrency), len(task_names))
    if not finalize_only:
        with ThreadPoolExecutor(max_workers=formal_workers) as executor:
            futures = [executor.submit(run_formal_pipeline, task) for task in task_names]
            for future in as_completed(futures):
                future.result()

    # Error-only retry phase: only after all formal round/task attempts
    # complete may a round/task whose execution never produced a reward
    # receive up to max_reruns batch waves.
    if not finalize_only:
        for wave_index in range(2, max_reruns + 2):
            if blocking_service_reason is not None:
                break
            pending_records = []
            any_retryable = False
            for round_index in range(1, rounds + 1):
                retryable = [
                    task for task in task_names if pair_needs_retry(round_index, task)
                ]
                any_retryable = any_retryable or bool(retryable)
                pending_records.extend(
                    create_pending_records(wave_index, round_index, retryable)
                )
            if not any_retryable:
                break
            if not pending_records:
                continue
            worker_count = min(max(1, concurrency), len(pending_records))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(execute_record, record) for record in pending_records
                ]
                for future in as_completed(futures):
                    _, stderr_text = future.result()
                    note_completion(stderr_text)

    def wave_indexes(round_index: int) -> list[int]:
        if not finalize_only:
            return list(range(1, max_reruns + 2))
        # A finalize-only repair must summarize every wave the checkpoint
        # actually holds, including waves from an older retry budget.
        round_waves = (checkpoint.get("waves") or {}).get(str(round_index)) or {}
        return sorted(int(key) for key in round_waves)

    wave_results: dict[int, list[dict[str, dict[str, Any]]]] = {}
    for round_index in range(1, rounds + 1):
        round_results: list[dict[str, dict[str, Any]]] = []
        for wave_index in wave_indexes(round_index):
            records = _checkpoint_records(checkpoint, round_index, wave_index)
            if not records:
                continue
            selected = sorted({str(record.get("task")) for record in records})
            round_results.append(
                _merged_checkpoint_results(records, selected, secret=secret)
            )
        wave_results[round_index] = round_results

    round_summaries = []
    artifact_paths = [str(checkpoint_path.resolve())]
    total_trials = completed_trials = errored_trials = output_tokens = 0
    for round_index in range(1, rounds + 1):
        summary = summarize_round(task_names, wave_results[round_index])
        records = [
            record
            for wave_index in wave_indexes(round_index)
            for record in _checkpoint_records(checkpoint, round_index, wave_index)
        ]
        for record in records:
            selected = list(record.get("selected_tasks") or [])
            outcomes = read_wave_results(Path(record["job_dir"]), selected, secret=secret)
            total_trials += len(selected)
            completed_trials += sum(item.get("reward") is not None for item in outcomes.values())
            errored_trials += sum(item.get("reward") is None for item in outcomes.values())
            output_tokens += sum(int(item.get("output_tokens") or 0) for item in outcomes.values())
            artifact_paths.extend([record["stdout_path"], record["stderr_path"]])
            for outcome in outcomes.values():
                if outcome.get("exception_message"):
                    stability_texts.append(str(outcome["exception_message"]))
                if outcome.get("result_path"):
                    artifact_paths.append(str(outcome["result_path"]))
                if outcome.get("request_evidence_dir"):
                    artifact_paths.append(str(outcome["request_evidence_dir"]))
        summary.update(
            {
                "round": round_index,
                "waves": records,
                "requested_concurrency": concurrency,
                "formal_rounds_mixed": True,
            }
        )
        round_summaries.append(summary)
        path = output_dir / f"round-{round_index:02d}-summary.json"
        _write_json(path, summary)
        artifact_paths.append(str(path.resolve()))

    task_results_path = output_dir / "task-results.jsonl"
    _write_task_results(task_results_path, round_summaries)
    artifact_paths.append(str(task_results_path.resolve()))

    round_scores = [float(item["score"]) for item in round_summaries]
    unclear = {
        str(item["round"]): item["unclear_tasks"]
        for item in round_summaries
        if item["unclear_tasks"]
    }
    # Keep model failures and execution-unknown work separate all the way to the
    # report: a task whose execution never produced a reward is not a model
    # failure and must not be read as one.
    outcome_split = {
        "passed_round_tasks": sum(int(item["passed_tasks"]) for item in round_summaries),
        "model_not_pass_round_tasks": sum(
            int(item["not_pass_tasks"]) for item in round_summaries
        ),
        "exec_unknown_round_tasks": sum(
            int(item["exec_unknown_tasks"]) for item in round_summaries
        ),
    }
    score = sum(round_scores) / rounds
    aggregate_path = output_dir / "round-summary.json"
    _write_json(
        aggregate_path,
        {
            "schema_version": 1,
            "rounds": rounds,
            "max_reruns_per_task_per_round": max_reruns,
            "max_concurrency": max_concurrency,
            "requested_concurrency": concurrency,
            "agent_timeout_multiplier": agent_timeout_multiplier,
            "agent_timeout_seconds": int(DEFAULT_PIER_AGENT_TIMEOUT_SECONDS * agent_timeout_multiplier),
            "task_subset": task_names,
            "formal_rounds_mixed": True,
            "round_scores": round_scores,
            "score": score,
            "outcome_split": outcome_split,
            "total_trials": total_trials,
            "completed_trials": completed_trials,
            "errored_trials": errored_trials,
            "round_summaries": round_summaries,
        },
    )
    artifact_paths.append(str(aggregate_path.resolve()))
    complete = blocking_service_reason is None and not unclear
    if blocking_service_reason is not None:
        status = "blocked"
        validity = "service_invalid"
        reason = blocking_service_reason
        failure_summary = "endpoint credential or quota is invalid"
    else:
        status = "passed" if complete else "failed"
        validity = "valid" if complete else "execution_invalid"
        reason = None if complete else "deepswe_rounds_incomplete"
        failure_summary = None if complete else f"rounds lack clear pass/not-pass: {unclear}"
    return {
        "schema_version": 1,
        "suite_id": "deepswe",
        "row_ids": ["17"],
        "status": status,
        "validity": validity,
        "counts": {
            "tasks": len(task_names),
            "corpus_tasks": expected_tasks,
            "selected_tasks": len(task_names),
            "is_subset": int(len(task_names) != expected_tasks),
            "rounds": rounds,
            "total": total_trials,
            "completed": completed_trials,
            "errored": errored_trials,
            "round_task_passes": outcome_split["passed_round_tasks"],
            "round_task_not_passes": outcome_split["model_not_pass_round_tasks"],
            "round_task_exec_unknown": outcome_split["exec_unknown_round_tasks"],
        },
        "metrics": {
            "score": score,
            **{f"round_{index}_score": value for index, value in enumerate(round_scores, 1)},
            "output_tokens": output_tokens,
        },
        "redacted_command": [],
        "artifact_paths": list(dict.fromkeys(artifact_paths)),
        "failure_summary": failure_summary,
        "runner_fingerprint": {
            "pier": preflight["observed"]["pier_version"],
            "deep_swe_commit": preflight["observed"]["deep_swe_commit"],
            "kimi_code": kimi_code_version,
            "transport": "local",
            "rounds": str(rounds),
            "max_reruns": str(max_reruns),
            "max_concurrency": str(max_concurrency),
            "task_pass_policy": "error_only",
            "formal_rounds_mixed": "true",
            "round_scheduling": "mixed_round_task_locked",
            "concurrency_scope": "global_task_jobs",
            "same_task_overlap": "forbidden",
            "checkpoint_resume": "round-task-attempt",
            "agent_timeout_multiplier": str(agent_timeout_multiplier),
            "agent_timeout_seconds": str(int(DEFAULT_PIER_AGENT_TIMEOUT_SECONDS * agent_timeout_multiplier)),
            "task_subset": ",".join(task_names),
        },
        "coverage": {
            "corpus_tasks": expected_tasks,
            "selected_tasks": len(task_names),
            "is_subset": len(task_names) != expected_tasks,
            "observed_rounds": rounds,
            "observed_max_reruns": max_reruns,
            "max_concurrency": max_concurrency,
            "observed_trials": total_trials,
            "round_scores": round_scores,
            "round_passed_tasks": [item["passed_tasks"] for item in round_summaries],
            "round_not_pass_tasks": [item["not_pass_tasks"] for item in round_summaries],
            "round_exec_unknown_tasks": [
                item["exec_unknown_tasks"] for item in round_summaries
            ],
            "outcome_split": outcome_split,
            "exec_unknown_round_tasks": unclear,
            "failure_class_counts": failure_class_counts(stability_texts),
            "finalize_only": finalize_only,
            "checkpoint_contract_drift": checkpoint.get("finalize_drift"),
        },
        "reason": reason,
    }


def run_supplemental_recovery(
    *,
    output_dir: Path,
    runtime_root: str,
    pier_bin: str,
    mode: str,
    concurrency: int,
    expected_tasks: int,
    rounds: int,
    kimi_code_version: str,
    model: str,
    execution_env: dict[str, str],
    preflight: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Run one operator-authorized attempt for selected unresolved round/task pairs."""
    source_run_id = str(plan.get("source_run_id") or "")
    raw_pairs = list(plan.get("round_tasks") or [])
    pairs = [(int(item["round"]), str(item["task"])) for item in raw_pairs]
    if not source_run_id or not pairs or len(set(pairs)) != len(pairs):
        raise ValueError("supplemental plan requires a source run and unique round/task pairs")
    if not 1 <= concurrency <= 10:
        raise ValueError("supplemental recovery concurrency must be between 1 and 10")

    tasks_root = Path(runtime_root) / "deep-swe" / "tasks"
    task_names = sorted(path.name for path in tasks_root.iterdir() if path.is_dir())
    if len(task_names) != expected_tasks:
        raise RuntimeError(f"expected {expected_tasks} task directories, found {len(task_names)}")
    for round_index, task in pairs:
        if not 1 <= round_index <= rounds or task not in task_names:
            raise RuntimeError(f"invalid supplemental pair: round={round_index}, task={task}")

    model_root = output_dir.parents[2]
    source_suites_dir = model_root / source_run_id / "suites"
    source_candidates = sorted(
        path
        for path in source_suites_dir.glob("*-deepswe/deepswe-checkpoint.json")
        if path.is_file()
    )
    if len(source_candidates) != 1:
        raise RuntimeError(
            "source DeepSWE checkpoint not found or ambiguous: "
            f"{source_suites_dir}/*-deepswe/deepswe-checkpoint.json"
        )
    source_checkpoint_path = source_candidates[0]
    source_bytes = source_checkpoint_path.read_bytes()
    source_checkpoint = json.loads(source_bytes)
    if source_checkpoint.get("task_names") != task_names or source_checkpoint.get("rounds") != rounds:
        raise RuntimeError("source DeepSWE checkpoint does not match task inventory or rounds")

    secret = execution_env.get("KIMI_API_KEY", "")
    for round_index, task in pairs:
        records = [
            record
            for wave in (source_checkpoint.get("waves") or {}).get(str(round_index), {}).values()
            for record in wave
            if record.get("task") == task
        ]
        if not records or any(record.get("status") != "finished" for record in records):
            raise RuntimeError(f"source pair is not terminal: round={round_index}, task={task}")
        for record in records:
            outcome = read_wave_results(
                Path(record["job_dir"]), [task], secret=secret
            )[task]
            if outcome.get("reward") is not None:
                raise RuntimeError(
                    f"source pair already has a binary reward: round={round_index}, task={task}"
                )

    checkpoint_path = output_dir / "deepswe-supplemental-checkpoint.json"
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    stamp = subprocess.run(
        ["date", "-u", "+%Y%m%dT%H%M%SZ"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if (
            checkpoint.get("source_checkpoint_sha256") != source_sha256
            or checkpoint.get("round_tasks") != raw_pairs
            or checkpoint.get("concurrency") != concurrency
        ):
            raise RuntimeError("supplemental checkpoint contract changed")
    else:
        checkpoint = {
            "schema_version": 1,
            "mode": "operator_authorized_supplemental",
            "source_run_id": source_run_id,
            "source_checkpoint": str(source_checkpoint_path.resolve()),
            "source_checkpoint_sha256": source_sha256,
            "round_tasks": raw_pairs,
            "concurrency": concurrency,
            "created_at": stamp,
            "updated_at": stamp,
            "records": [],
        }
        _write_json(checkpoint_path, checkpoint)

    records = list(checkpoint.get("records") or [])
    if records and any(record.get("status") != "finished" for record in records):
        raise RuntimeError("supplemental checkpoint contains interrupted work; manual cleanup required")
    existing_pairs = {(int(record["round"]), str(record["task"])) for record in records}
    jobs_root = Path(runtime_root) / "jobs"
    for round_index, task in pairs:
        if (round_index, task) in existing_pairs:
            continue
        task_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", task)[:80]
        job_name = (
            f"nvst-deepswe-supplemental-{stamp}-round-{round_index:02d}-"
            f"task-{task_label}"
        )
        records.append(
            {
                "round": round_index,
                "attempt": "supplemental",
                "task": task,
                "selected_tasks": [task],
                "requested_concurrency": concurrency,
                "runner_concurrency": 1,
                "job_name": job_name,
                "job_dir": str((jobs_root / job_name).resolve()),
                "stdout_path": str(
                    (output_dir / f"round-{round_index:02d}-task-{task_label}.stdout.log").resolve()
                ),
                "stderr_path": str(
                    (output_dir / f"round-{round_index:02d}-task-{task_label}.stderr.log").resolve()
                ),
                "status": "pending",
            }
        )
    checkpoint["records"] = records
    _write_json(checkpoint_path, checkpoint)

    checkpoint_lock = threading.Lock()
    task_locks = {task: threading.Lock() for _, task in pairs}
    stability_texts: list[str] = []

    def execute_record(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        task = str(record["task"])
        with task_locks[task]:
            with checkpoint_lock:
                record["status"] = "running"
                checkpoint["updated_at"] = stamp
                _write_json(checkpoint_path, checkpoint)
            command = [
                pier_bin,
                "run",
                "-p",
                str(tasks_root),
                "--agent-import-path",
                "pier_kimi.kimi_code:KimiCode",
                "--model",
                f"kimi/{model}",
                "--ak",
                f"version={kimi_code_version}",
                "--env",
                "docker",
                "--n-concurrent",
                "1",
                "--n-attempts",
                "1",
                "--max-retries",
                "0",
                "--jobs-dir",
                str(jobs_root),
                "--job-name",
                str(record["job_name"]),
                "--include-task-name",
                task,
            ]
            process = subprocess.run(
                command,
                env=execution_env,
                text=True,
                capture_output=True,
                check=False,
            )
            stdout_path = Path(record["stdout_path"])
            stderr_path = Path(record["stderr_path"])
            stdout_path.write_text(process.stdout.replace(secret, "[REDACTED]"), encoding="utf-8")
            stderr_text = process.stderr.replace(secret, "[REDACTED]")
            stderr_path.write_text(stderr_text, encoding="utf-8")
            with checkpoint_lock:
                record.update(
                    {
                        "status": "finished",
                        "process_returncode": process.returncode,
                    }
                )
                checkpoint["updated_at"] = subprocess.run(
                    ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                _write_json(checkpoint_path, checkpoint)
            return record, stderr_text[-4000:]

    pending = [record for record in records if record.get("status") == "pending"]
    if pending:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(pending))) as executor:
            futures = [executor.submit(execute_record, record) for record in pending]
            for future in as_completed(futures):
                _, stderr_text = future.result()
                stability_texts.append(stderr_text)

    supplemental_by_round: dict[int, dict[str, dict[str, Any]]] = {
        index: {} for index in range(1, rounds + 1)
    }
    artifact_paths = [
        str(checkpoint_path.resolve()),
        str(source_checkpoint_path.resolve()),
    ]
    for record in records:
        task = str(record["task"])
        outcome = read_wave_results(Path(record["job_dir"]), [task], secret=secret)[task]
        supplemental_by_round[int(record["round"])][task] = outcome
        artifact_paths.extend([record["stdout_path"], record["stderr_path"]])
        if outcome.get("result_path"):
            artifact_paths.append(str(outcome["result_path"]))
        if outcome.get("request_evidence_dir"):
            artifact_paths.append(str(outcome["request_evidence_dir"]))

    round_summaries: list[dict[str, Any]] = []
    for round_index in range(1, rounds + 1):
        wave_results: list[dict[str, dict[str, Any]]] = []
        for wave_index in sorted(
            (source_checkpoint.get("waves") or {}).get(str(round_index), {}), key=int
        ):
            source_records = _checkpoint_records(source_checkpoint, round_index, int(wave_index))
            selected = sorted({str(record.get("task")) for record in source_records})
            if source_records:
                wave_results.append(
                    _merged_checkpoint_results(source_records, selected, secret=secret)
                )
        if supplemental_by_round[round_index]:
            wave_results.append(supplemental_by_round[round_index])
        summary = summarize_round(task_names, wave_results)
        summary["round"] = round_index
        round_summaries.append(summary)
        summary_path = output_dir / f"round-{round_index:02d}-summary.json"
        _write_json(summary_path, summary)
        artifact_paths.append(str(summary_path.resolve()))

    task_results_path = output_dir / "task-results.jsonl"
    _write_task_results(task_results_path, round_summaries)
    artifact_paths.append(str(task_results_path.resolve()))
    round_scores = [float(summary["score"]) for summary in round_summaries]
    outcome_split = {
        "passed_round_tasks": sum(int(item["passed_tasks"]) for item in round_summaries),
        "model_not_pass_round_tasks": sum(int(item["not_pass_tasks"]) for item in round_summaries),
        "exec_unknown_round_tasks": sum(int(item["exec_unknown_tasks"]) for item in round_summaries),
    }
    unclear = {
        str(item["round"]): item["unclear_tasks"]
        for item in round_summaries
        if item["unclear_tasks"]
    }
    score = sum(round_scores) / rounds
    aggregate_path = output_dir / "round-summary.json"
    _write_json(
        aggregate_path,
        {
            "schema_version": 1,
            "source_run_id": source_run_id,
            "supplemental_policy": "operator_authorized_after_pinned_retry_budget",
            "supplemental_round_tasks": raw_pairs,
            "round_scores": round_scores,
            "score": score,
            "outcome_split": outcome_split,
            "round_summaries": round_summaries,
        },
    )
    artifact_paths.append(str(aggregate_path.resolve()))
    complete = not unclear
    return {
        "schema_version": 1,
        "suite_id": "deepswe",
        "row_ids": ["17"],
        "status": "passed" if complete else "failed",
        "validity": "valid" if complete else "execution_invalid",
        "counts": {
            "tasks": expected_tasks,
            "rounds": rounds,
            "total": expected_tasks * rounds,
            "completed": outcome_split["passed_round_tasks"]
            + outcome_split["model_not_pass_round_tasks"],
            "errored": outcome_split["exec_unknown_round_tasks"],
            "round_task_passes": outcome_split["passed_round_tasks"],
            "round_task_not_passes": outcome_split["model_not_pass_round_tasks"],
            "round_task_exec_unknown": outcome_split["exec_unknown_round_tasks"],
        },
        "metrics": {
            "score": score,
            **{f"round_{index}_score": value for index, value in enumerate(round_scores, 1)},
        },
        "redacted_command": [],
        "artifact_paths": list(dict.fromkeys(artifact_paths)),
        "failure_summary": None if complete else f"rounds lack clear pass/not-pass: {unclear}",
        "runner_fingerprint": {
            "pier": preflight["observed"]["pier_version"],
            "deep_swe_commit": preflight["observed"]["deep_swe_commit"],
            "kimi_code": kimi_code_version,
            "transport": "local",
            "rounds": str(rounds),
            "max_reruns": "1",
            "supplemental_attempts": str(len(pairs)),
            "supplemental_policy": "operator_authorized_after_pinned_retry_budget",
        },
        "coverage": {
            "observed_tasks": len(task_names),
            "observed_rounds": rounds,
            "observed_max_reruns": 1,
            "outcome_split": outcome_split,
            "round_scores": round_scores,
            "round_passed_tasks": [item["passed_tasks"] for item in round_summaries],
            "round_not_pass_tasks": [item["not_pass_tasks"] for item in round_summaries],
            "round_exec_unknown_tasks": [item["exec_unknown_tasks"] for item in round_summaries],
            "exec_unknown_round_tasks": unclear,
            "source_run_id": source_run_id,
            "source_checkpoint_sha256": source_sha256,
            "supplemental_round_tasks": raw_pairs,
            "supplemental_policy": "operator_authorized_after_pinned_retry_budget",
            "failure_class_counts": failure_class_counts(stability_texts),
        },
        "reason": None if complete else "deepswe_supplemental_incomplete",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--pier-bin", required=True)
    parser.add_argument("--mode", choices=("smoke", "subset", "full"), default="full")
    parser.add_argument("--concurrency", type=int, default=113)
    parser.add_argument("--expected-deep-swe-commit", required=True)
    parser.add_argument("--expected-pier-version", required=True)
    parser.add_argument("--expected-tasks", required=True, type=int)
    parser.add_argument("--rounds", required=True, type=int)
    parser.add_argument("--max-reruns", required=True, type=int)
    parser.add_argument("--max-concurrency", type=int, default=113)
    parser.add_argument("--agent-timeout-multiplier", type=float, default=1.0)
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--kimi-code-version", required=True)
    parser.add_argument(
        "--supplemental-plan",
        type=Path,
        help="operator-authorized plan for selected unresolved round/task pairs",
    )
    parser.add_argument(
        "--finalize-only",
        action="store_true",
        help="summarize the existing checkpoint without scheduling new work",
    )
    args = parser.parse_args(argv)
    if (
        args.concurrency < 1
        or args.rounds < 1
        or args.max_reruns < 0
        or args.max_concurrency < 1
    ):
        parser.error("concurrency/rounds must be positive and retry counts non-negative")
    if args.supplemental_plan is not None and args.finalize_only:
        parser.error("--supplemental-plan and --finalize-only are mutually exclusive")
    base_url = os.environ.get("KIMI_BASE_URL")
    api_key = os.environ.get("KIMI_API_KEY")
    model = os.environ.get("KIMI_MODEL")
    if not base_url or not api_key or not model:
        parser.error("KIMI_BASE_URL, KIMI_API_KEY, and KIMI_MODEL are required")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    preflight_path = args.output_dir / "local-preflight.json"
    preflight = verify_runtime(
        runtime_root=args.runtime_root,
        expected_commit=args.expected_deep_swe_commit,
        expected_pier_version=args.expected_pier_version,
        expected_tasks=args.expected_tasks,
        pier_bin=args.pier_bin,
    )
    preflight_path.write_text(
        json.dumps(preflight, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if preflight["status"] != "passed":
        (args.output_dir / "adapter-result.json").write_text(
            json.dumps(blocked_result(preflight_path, preflight), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(preflight["failure_summary"], file=sys.stderr)
        return 2

    package_root = Path(__file__).resolve().parents[3]
    adapter_dir = package_root / "suites" / "deepswe" / "pier_kimi"
    adapter_target = Path(args.runtime_root) / "pier-kimi-agent" / "pier_kimi"
    if not args.finalize_only:
        adapter_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(adapter_dir, adapter_target, dirs_exist_ok=True)

    # Task verifiers exercise environment proxy handling, and a blank override
    # is not equivalent to absence in Python's getproxies_environment, so the
    # Pier subprocess creates its containers through a Docker client config
    # without the injected proxy block.
    docker_client_dir = args.output_dir / "docker-client-config"
    proxy_free_docker_config(docker_client_dir)
    execution_env = {
        **os.environ,
        "DOCKER_CONFIG": str(docker_client_dir),
        "KIMI_API_KEY": api_key,
        "KIMI_BASE_URL": base_url,
        "PYTHONPATH": os.pathsep.join(
            item
            for item in (
                f"{args.runtime_root}/pier-kimi-agent",
                os.environ.get("PYTHONPATH", ""),
            )
            if item
        ),
        "KIMI_DISABLE_TELEMETRY": "1",
        "KIMI_CODE_NO_AUTO_UPDATE": "1",
    }
    if args.supplemental_plan is not None:
        try:
            supplemental_plan = json.loads(args.supplemental_plan.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read supplemental plan: {exc}")
        result = run_supplemental_recovery(
            output_dir=args.output_dir,
            runtime_root=args.runtime_root,
            pier_bin=args.pier_bin,
            mode=args.mode,
            concurrency=args.concurrency,
            expected_tasks=args.expected_tasks,
            rounds=args.rounds,
            kimi_code_version=args.kimi_code_version,
            model=model,
            execution_env=execution_env,
            preflight=preflight,
            plan=supplemental_plan,
        )
        result["artifact_paths"].append(str(args.supplemental_plan.resolve()))
    else:
        result = run_benchmark_checkpointed(
            output_dir=args.output_dir,
            runtime_root=args.runtime_root,
            pier_bin=args.pier_bin,
            mode=args.mode,
            concurrency=args.concurrency,
            expected_tasks=args.expected_tasks,
            rounds=args.rounds,
            max_reruns=args.max_reruns,
            kimi_code_version=args.kimi_code_version,
            model=model,
            execution_env=execution_env,
            preflight=preflight,
            max_concurrency=args.max_concurrency,
            finalize_only=args.finalize_only,
            task_subset=args.task or None,
            agent_timeout_multiplier=args.agent_timeout_multiplier,
        )
    result["artifact_paths"].append(str(preflight_path.resolve()))
    (args.output_dir / "adapter-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if result["validity"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
