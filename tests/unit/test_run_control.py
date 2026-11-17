from __future__ import annotations

import io
import json
import signal
from pathlib import Path

from scripts import nvst_run_control


AUTHORIZATION = "Authorization: " + "Bearer " + "runtime-secret"


def control_payload(tmp_path: Path, *, status: str = "paused") -> dict:
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "run_dir": str(tmp_path / "runs/kimi-k3/run-1"),
        "root": str(tmp_path),
        "endpoint": {
            "base_url": "https://vendor.example/v1",
            "served_model": "served-model",
            "model_family": "kimi-k3",
            "think_mode": "opensource",
            "headers": {},
        },
        "nvst_args": ["accept", "--model", "kimi-k3"],
        "status": status,
        "phase": status,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": None,
        "stdout_log": str(tmp_path / "stdout.log"),
        "stderr_log": str(tmp_path / "stderr.log"),
    }


def run_manifest(run_dir: Path) -> dict:
    return {
        "schema_version": 2,
        "revision": 1,
        "updated_by": "test",
        "run_id": run_dir.name,
        "mode": "accept",
        "model": "test-model",
        "profile_path": "/test/profile",
        "requirements_snapshot_revision": "test-revision",
        "execution_site": None,
        "service_access_path": "/test/service.json",
        "service_access_hash": "0" * 64,
        "judge_access_path": None,
        "judge_access_hash": None,
        "selectors": [],
        "include_long": [],
        "concurrency_override": None,
        "repo_pins": {},
        "profile_hashes": {key: "0" * 64 for key in ("a", "b", "c", "d")},
        "suite_input_hashes": {"only": "0" * 64},
        "started_at": "now",
        "finished_at": None,
        "suite_states": [
            {
                "execution_id": "001-only",
                "suite_id": "only",
                "mandatory": True,
                "long_test": None,
                "selected": True,
                "status": "running",
                "validity": "valid",
                "reason": None,
                "phase": "executing",
                "started_at": "now",
                "finished_at": None,
                "heartbeat_at": "now",
                "process_id": 1,
                "process_group_id": 1,
                "progress": {"phase": "executing", "completed": 0, "total": 1},
                "checkpoint_paths": [],
            }
        ],
    }


def test_managed_start_persists_secret_free_control(tmp_path: Path, monkeypatch) -> None:
    observed: dict[str, str] = {}

    def fake_start(control_path: Path, authorization: str) -> dict:
        observed["authorization"] = authorization
        return nvst_run_control.write_control(
            control_path,
            {
                "status": "running",
                "worker_pid": 123,
                "process_group_id": 123,
            },
        )

    monkeypatch.setattr(nvst_run_control, "start_worker", fake_start)
    monkeypatch.setattr(nvst_run_control.sys, "stdin", io.StringIO(AUTHORIZATION))

    assert nvst_run_control.main(
        [
            "start",
            "--root",
            str(tmp_path),
            "--base-url",
            "https://vendor.example/v1",
            "--served-model",
            "served-model",
            "--",
            "accept",
            "--model",
            "kimi-k3",
        ]
    ) == 0

    controls = list((tmp_path / "runs/.control").glob("*.json"))
    assert len(controls) == 1
    payload = json.loads(controls[0].read_text())
    assert payload["status"] == "running"
    assert payload["run_dir"].endswith(payload["run_id"])
    assert payload["nvst_args"][-2:] == ["--run-dir", payload["run_dir"]]
    assert "runtime-secret" not in controls[0].read_text()
    assert observed["authorization"] == AUTHORIZATION


def test_managed_start_rejects_secret_headers_before_writing_control(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(nvst_run_control.sys, "stdin", io.StringIO(AUTHORIZATION))

    try:
        nvst_run_control.main(
            [
                "start",
                "--root",
                str(tmp_path),
                "--base-url",
                "https://vendor.example/v1",
                "--served-model",
                "served-model",
                "--headers-json",
                json.dumps({"Authorization": "Bearer " + "forbidden-secret"}),
                "--",
                "accept",
                "--model",
                "kimi-k3",
            ]
        )
    except RuntimeError as exc:
        assert "forbidden credential header" in str(exc)
    else:
        raise AssertionError("secret header was accepted")
    assert not (tmp_path / "runs/.control").exists()


def test_systemd_supervisor_uses_fifo_without_secret_in_unit_command(
    tmp_path: Path, monkeypatch
) -> None:
    control_path = tmp_path / "runs/.control/run-1.json"
    nvst_run_control.write_control(control_path, control_payload(tmp_path))
    commands: list[list[str]] = []
    observed: dict[str, str] = {}

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["systemctl", "--user", "show"]:
            return nvst_run_control.subprocess.CompletedProcess(
                command, 0, stdout="4321\n", stderr=""
            )
        return nvst_run_control.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(nvst_run_control, "systemd_available", lambda: True)
    monkeypatch.setattr(nvst_run_control.subprocess, "run", fake_run)
    monkeypatch.setattr(
        nvst_run_control.os,
        "mkfifo",
        lambda path, _mode: Path(path).touch(),
    )
    monkeypatch.setattr(
        nvst_run_control,
        "_write_fifo_secret",
        lambda _path, secret: observed.update(secret=secret),
    )

    result = nvst_run_control.start_worker(control_path, AUTHORIZATION)

    unit_command = commands[0]
    assert unit_command[:2] == ["systemd-run", "--user"]
    assert AUTHORIZATION not in " ".join(unit_command)
    assert result["supervisor"] == "systemd"
    assert result["worker_pid"] == 4321
    assert result["process_group_id"] is None
    assert observed["secret"] == AUTHORIZATION
    assert "runtime-secret" not in control_path.read_text()


def test_managed_resume_reuses_endpoint_without_persisting_credential(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "run-1"
    control_path = tmp_path / "runs/.control/run-1.json"
    nvst_run_control.write_control(
        control_path,
        {
            **control_payload(tmp_path),
            "run_id": run_id,
        },
    )
    observed: dict[str, str] = {}

    def fake_start(path: Path, authorization: str) -> dict:
        observed["authorization"] = authorization
        return nvst_run_control.write_control(path, {"status": "running"})

    monkeypatch.setattr(nvst_run_control, "process_alive", lambda _: False)
    monkeypatch.setattr(nvst_run_control, "start_worker", fake_start)
    monkeypatch.setattr(nvst_run_control.sys, "stdin", io.StringIO(AUTHORIZATION))

    assert nvst_run_control.main(
        ["resume", "--root", str(tmp_path), "--run", run_id]
    ) == 0
    payload = json.loads(control_path.read_text())
    assert payload["resuming"] is True
    assert payload["resume_options"] == {
        "continue_from": None,
        "continue_reason": None,
        "allow_repo_pin_mismatch": False,
        "only_suite": False,
        "finalize_only": False,
    }
    assert "runtime-secret" not in control_path.read_text()
    assert observed["authorization"] == AUTHORIZATION


def test_managed_resume_forwards_terminal_continuation_options(
    tmp_path: Path, monkeypatch
) -> None:
    run_id = "run-1"
    control_path = tmp_path / "runs/.control/run-1.json"
    nvst_run_control.write_control(
        control_path,
        {
            **control_payload(tmp_path),
            "run_id": run_id,
        },
    )
    observed: dict[str, str] = {}

    def fake_start(path: Path, authorization: str) -> dict:
        observed["authorization"] = authorization
        return nvst_run_control.write_control(path, {"status": "running"})

    monkeypatch.setattr(nvst_run_control, "process_alive", lambda _: False)
    monkeypatch.setattr(nvst_run_control, "start_worker", fake_start)
    monkeypatch.setattr(nvst_run_control.sys, "stdin", io.StringIO(AUTHORIZATION))

    assert nvst_run_control.main(
        [
            "resume",
            "--root",
            str(tmp_path),
            "--run",
            run_id,
            "--continue-from",
            "010-beam-1m",
            "--continue-reason",
            "cluster recovered; rerun BEAM then continue acceptance",
            "--allow-repo-pin-mismatch",
            "--only-suite",
            "--finalize-only",
        ]
    ) == 0
    payload = json.loads(control_path.read_text())
    assert payload["resume_options"] == {
        "continue_from": "010-beam-1m",
        "continue_reason": "cluster recovered; rerun BEAM then continue acceptance",
        "allow_repo_pin_mismatch": True,
        "only_suite": True,
        "finalize_only": True,
    }
    command = nvst_run_control.dynamic_command(payload, resume=True)
    assert command[-7:] == [
        "--continue-from",
        "010-beam-1m",
        "--continue-reason",
        "cluster recovered; rerun BEAM then continue acceptance",
        "--allow-repo-pin-mismatch",
        "--only-suite",
        "--finalize-only",
    ]
    assert observed["authorization"] == AUTHORIZATION


def test_status_reconciles_dead_supervisor_as_interrupted(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "runs/test-model/run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps(run_manifest(run_dir)), encoding="utf-8")
    control_path = tmp_path / "runs/.control/run-1.json"
    nvst_run_control.write_control(
        control_path,
        {
            **control_payload(tmp_path, status="running"),
            "run_dir": str(run_dir),
            "worker_pid": 999,
            "process_group_id": 999,
            "supervisor": "process_group",
            "unit_name": None,
        },
    )
    monkeypatch.setattr(nvst_run_control, "owned_worker_alive", lambda *_: False)

    result = nvst_run_control.status(tmp_path, "run-1")

    assert result["status"] == "paused"
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["suite_states"][0]["status"] == "interrupted"
    assert manifest["suite_states"][0]["reason"] == "human_interrupted"
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert events[-1]["event"] == "supervisor_reconciled"


def test_managed_pause_signals_only_owned_process_group(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "control.json"
    control = nvst_run_control.write_control(
        path,
        {
            **control_payload(tmp_path, status="running"),
            "worker_pid": 123,
            "process_group_id": 456,
        },
    )
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(nvst_run_control, "process_alive", lambda _: True)
    monkeypatch.setattr(nvst_run_control, "owned_worker_alive", lambda *_: True)
    monkeypatch.setattr(nvst_run_control, "wait_for_exit", lambda *_: True)
    monkeypatch.setattr(
        nvst_run_control,
        "cleanup_managed_resources",
        lambda _: {"checkpoint_count": 0, "stopped_containers": []},
    )
    monkeypatch.setattr(
        nvst_run_control.os,
        "killpg",
        lambda pgid, signum: signals.append((pgid, signum)),
    )

    result = nvst_run_control.signal_run(path, control, stop=False)

    assert signals == [(456, signal.SIGINT)]
    assert result["status"] == "paused"
