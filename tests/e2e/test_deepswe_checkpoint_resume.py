from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from novita_self_test.adapters.deepswe_remote import (
    run_benchmark_checkpointed,
    run_supplemental_recovery,
)


def result_payload(task: str) -> dict:
    return {
        "trial_name": task,
        "config": {"task": {"path": f"/tasks/{task}"}},
        "verifier_result": {"rewards": {"reward": 1}},
        "exception_info": None,
        "agent_result": {"n_output_tokens": 10},
    }


def test_deepswe_resume_runs_only_missing_task_after_interruption(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    tasks = runtime / "deep-swe/tasks"
    for task in ("task-a", "task-b"):
        (tasks / task).mkdir(parents=True)
    output = tmp_path / "run/suites/001-deepswe"
    output.mkdir(parents=True)
    calls: list[tuple[str, list[str]]] = []
    interrupted = {"done": False}

    def fake_run(command, **_kwargs):
        if command[:3] == ["date", "-u", "+%Y%m%dT%H%M%SZ"]:
            return subprocess.CompletedProcess(command, 0, stdout="20260101T000000Z\n", stderr="")
        if command[:3] == ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"]:
            return subprocess.CompletedProcess(command, 0, stdout="2026-01-01T00:00:00Z\n", stderr="")
        job_name = command[command.index("--job-name") + 1]
        selected = [
            command[index + 1]
            for index, token in enumerate(command[:-1])
            if token == "--include-task-name"
        ]
        calls.append((job_name, selected))
        if selected == ["task-b"] and not interrupted["done"]:
            interrupted["done"] = True
            raise KeyboardInterrupt
        for task in selected:
            trial = runtime / "jobs" / job_name / task
            trial.mkdir(parents=True, exist_ok=True)
            (trial / "result.json").write_text(
                json.dumps(result_payload(task)), encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(
        "novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run
    )
    kwargs = {
        "output_dir": output,
        "runtime_root": str(runtime),
        "pier_bin": "/fake/pier",
        "mode": "full",
        "concurrency": 2,
        "expected_tasks": 2,
        "rounds": 1,
        "max_reruns": 1,
        "kimi_code_version": "0.29.0",
        "model": "fake-model",
        "execution_env": {"KIMI_API_KEY": "runtime-secret"},
        "preflight": {
            "observed": {"pier_version": "0.3.1", "deep_swe_commit": "abc"}
        },
    }

    with pytest.raises(KeyboardInterrupt):
        run_benchmark_checkpointed(**kwargs)
    checkpoint = json.loads((output / "deepswe-checkpoint.json").read_text())
    assert {
        tuple(record["selected_tasks"])
        for record in checkpoint["waves"]["1"]["1"]
    } == {("task-a",), ("task-b",)}

    result = run_benchmark_checkpointed(**kwargs)

    assert result["validity"] == "valid"
    assert result["metrics"]["score"] == 1.0
    resumed_calls = [selected for name, selected in calls if "task-task-b-part-02" in name]
    assert resumed_calls == [["task-b"]]
    assert all("task-a" not in selected for selected in resumed_calls)


def test_deepswe_resume_skips_pairs_holding_retry_wave_rewards(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    tasks = runtime / "deep-swe/tasks"
    for task in ("task-a", "task-b"):
        (tasks / task).mkdir(parents=True)
    output = tmp_path / "run/suites/001-deepswe"
    output.mkdir(parents=True)
    jobs: list[str] = []
    resumed = {"enabled": False}

    def payload(task: str, reward: int | None) -> dict:
        if reward is None:
            return {
                "trial_name": task,
                "config": {"task": {"path": f"/tasks/{task}"}},
                "verifier_result": {"rewards": {}},
                "exception_info": {
                    "exception_type": "AgentTimeoutError",
                    "exception_message": "timeout",
                },
                "agent_result": {"n_output_tokens": 10},
            }
        return {
            "trial_name": task,
            "config": {"task": {"path": f"/tasks/{task}"}},
            "verifier_result": {"rewards": {"reward": reward}},
            "exception_info": None,
            "agent_result": {"n_output_tokens": 10},
        }

    def fake_run(command, **_kwargs):
        if command[:2] == ["date", "-u"]:
            return subprocess.CompletedProcess(command, 0, stdout="20260101T000000Z\n", stderr="")
        job_name = command[command.index("--job-name") + 1]
        selected = [
            command[index + 1]
            for index, token in enumerate(command[:-1])
            if token == "--include-task-name"
        ]
        jobs.append(job_name)
        attempt = int(job_name.split("attempt-")[1][:2])
        for task in selected:
            if resumed["enabled"]:
                reward = 1
            elif attempt == 1:
                reward = None
            elif attempt == 2 and task == "task-a":
                reward = 0
            else:
                reward = None
            trial = runtime / "jobs" / job_name / task
            trial.mkdir(parents=True, exist_ok=True)
            (trial / "result.json").write_text(
                json.dumps(payload(task, reward)), encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(
        "novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run
    )
    kwargs = {
        "output_dir": output,
        "runtime_root": str(runtime),
        "pier_bin": "/fake/pier",
        "mode": "full",
        "concurrency": 2,
        "expected_tasks": 2,
        "rounds": 1,
        "max_reruns": 1,
        "kimi_code_version": "0.29.0",
        "model": "fake-model",
        "execution_env": {"KIMI_API_KEY": "runtime-secret"},
        "preflight": {"observed": {"pier_version": "0.3.1", "deep_swe_commit": "abc"}},
    }

    first = run_benchmark_checkpointed(**kwargs)
    assert first["status"] == "failed"
    assert first["validity"] == "execution_invalid"
    assert first["counts"]["round_task_passes"] == 0
    assert first["counts"]["round_task_not_passes"] == 1

    resumed["enabled"] = True
    jobs.clear()
    second = run_benchmark_checkpointed(**kwargs)

    assert second["status"] == "passed"
    assert second["metrics"]["score"] == 0.5
    assert jobs
    assert all("task-b" in job for job in jobs)
    assert any("attempt-01" in job and "part-02" in job for job in jobs)


@pytest.mark.parametrize("source_suite_dir", ["001-deepswe", "011-deepswe"])
def test_deepswe_supplemental_recovery_runs_only_selected_unresolved_pairs(
    tmp_path: Path, monkeypatch, source_suite_dir: str
) -> None:
    runtime = tmp_path / "runtime"
    tasks = runtime / "deep-swe/tasks"
    for task in ("task-a", "task-b"):
        (tasks / task).mkdir(parents=True)
    model_root = tmp_path / "runs/test-model"
    # 001-deepswe is the single-suite develop layout; 011-deepswe is the
    # accept-run layout (DeepSWE as the 11th suite in the flash manifest).
    source_output = model_root / f"source-run/suites/{source_suite_dir}"
    output = model_root / "supplemental-run/suites/001-deepswe"
    source_output.mkdir(parents=True)
    output.mkdir(parents=True)

    def write_source(task: str, reward: int, exception: dict | None) -> dict:
        job_dir = runtime / "jobs" / f"source-{task}"
        trial = job_dir / task
        trial.mkdir(parents=True)
        payload = result_payload(task)
        payload["verifier_result"] = {"rewards": {"reward": reward}}
        payload["exception_info"] = exception
        (trial / "result.json").write_text(json.dumps(payload), encoding="utf-8")
        return {
            "round": 1,
            "attempt": 1,
            "task": task,
            "selected_tasks": [task],
            "job_dir": str(job_dir),
            "status": "finished",
        }

    source_checkpoint = {
        "schema_version": 1,
        "task_names": ["task-a", "task-b"],
        "rounds": 1,
        "waves": {
            "1": {
                "1": [
                    write_source(
                        "task-a",
                        0,
                        {"exception_type": "AgentTimeoutError", "exception_message": "timeout"},
                    ),
                    write_source("task-b", 1, None),
                ]
            }
        },
    }
    (source_output / "deepswe-checkpoint.json").write_text(
        json.dumps(source_checkpoint), encoding="utf-8"
    )
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        if command[:2] == ["date", "-u"]:
            return subprocess.CompletedProcess(command, 0, stdout="20260101T000000Z\n", stderr="")
        selected = [command[command.index("--include-task-name") + 1]]
        calls.append(selected)
        job_name = command[command.index("--job-name") + 1]
        trial = runtime / "jobs" / job_name / selected[0]
        trial.mkdir(parents=True, exist_ok=True)
        (trial / "result.json").write_text(
            json.dumps(result_payload(selected[0])), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(
        "novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run
    )
    result = run_supplemental_recovery(
        output_dir=output,
        runtime_root=str(runtime),
        pier_bin="/fake/pier",
        mode="full",
        concurrency=1,
        expected_tasks=2,
        rounds=1,
        kimi_code_version="0.29.0",
        model="fake-model",
        execution_env={"KIMI_API_KEY": "runtime-secret"},
        preflight={"observed": {"pier_version": "0.3.1", "deep_swe_commit": "abc"}},
        plan={
            "source_run_id": "source-run",
            "round_tasks": [{"round": 1, "task": "task-a"}],
        },
    )

    assert calls == [["task-a"]]
    assert result["status"] == "passed"
    assert result["metrics"]["score"] == 1.0
    assert result["coverage"]["supplemental_round_tasks"] == [
        {"round": 1, "task": "task-a"}
    ]
    assert result["counts"]["round_task_passes"] == 2


def test_deepswe_finalize_only_summarizes_without_new_work(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    tasks = runtime / "deep-swe/tasks"
    for task in ("task-a", "task-b"):
        (tasks / task).mkdir(parents=True)
    output = tmp_path / "run/suites/001-deepswe"
    output.mkdir(parents=True)
    calls: list[str] = []

    def fake_run(command, **_kwargs):
        if command[:2] == ["date", "-u"]:
            return subprocess.CompletedProcess(command, 0, stdout="20260101T000000Z\n", stderr="")
        job_name = command[command.index("--job-name") + 1]
        calls.append(job_name)
        selected = [
            command[index + 1]
            for index, token in enumerate(command[:-1])
            if token == "--include-task-name"
        ]
        for task in selected:
            trial = runtime / "jobs" / job_name / task
            trial.mkdir(parents=True, exist_ok=True)
            reward = 1 if task == "task-a" else 0
            (trial / "result.json").write_text(
                json.dumps(result_payload(task) | {"verifier_result": {"rewards": {"reward": reward}}}),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(
        "novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run
    )
    kwargs = {
        "output_dir": output,
        "runtime_root": str(runtime),
        "pier_bin": "/fake/pier",
        "mode": "full",
        "concurrency": 2,
        "expected_tasks": 2,
        "rounds": 1,
        "max_reruns": 1,
        "kimi_code_version": "0.29.0",
        "model": "fake-model",
        "execution_env": {"KIMI_API_KEY": "runtime-secret"},
        "preflight": {"observed": {"pier_version": "0.3.1", "deep_swe_commit": "abc"}},
    }

    first = run_benchmark_checkpointed(**kwargs)
    assert first["status"] == "passed"
    assert calls, "formal run should execute jobs"
    calls.clear()

    repaired = run_benchmark_checkpointed(**kwargs, finalize_only=True)
    assert calls == [], "finalize-only must not schedule new jobs"
    assert repaired["status"] == "passed"
    assert repaired["metrics"]["score"] == 0.5
    assert repaired["coverage"]["finalize_only"] is True
