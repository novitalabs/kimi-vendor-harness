from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from scripts import nvst_run_control


AUTHORIZATION = "Authorization: " + "Bearer " + "runtime-secret"


FAKE_DYNAMIC = r'''#!/usr/bin/env python3
import json
import signal
import sys
import time
from pathlib import Path

args = sys.argv[1:]
sys.stdin.read()
if "resume" in args:
    run_dir = Path(args[args.index("--run") + 1])
    manifest = json.loads((run_dir / "run.json").read_text())
    state = manifest["suite_states"][0]
    state.update({"status": "passed", "validity": "valid", "reason": None, "phase": "finished", "finished_at": "done"})
    manifest["finished_at"] = "done"
    (run_dir / "run.json").write_text(json.dumps(manifest))
    raise SystemExit(0)
run_dir = Path(args[args.index("--run-dir") + 1])
run_dir.mkdir(parents=True)
state = {
    "execution_id": "001-fake", "suite_id": "fake", "mandatory": True,
    "long_test": None, "selected": True, "status": "running", "validity": "valid",
    "reason": None, "phase": "executing", "started_at": "now", "finished_at": None,
    "heartbeat_at": "now", "process_id": 1, "process_group_id": 1,
    "progress": {"phase": "executing", "completed": 0, "total": 1},
    "checkpoint_paths": []
}
manifest = {
    "schema_version": 2, "revision": 1, "updated_by": "fake", "run_id": run_dir.name,
    "mode": "accept", "model": "fake-model", "profile_path": "/fake/profile",
    "requirements_snapshot_revision": "fake-revision", "execution_site": None,
    "service_access_path": "/fake/service.json", "service_access_hash": "0" * 64,
    "judge_access_path": None, "judge_access_hash": None, "selectors": [],
    "include_long": [], "concurrency_override": None, "repo_pins": {},
    "profile_hashes": {"a": "0"*64, "b": "0"*64, "c": "0"*64, "d": "0"*64},
    "suite_input_hashes": {"fake": "0"*64}, "started_at": "now", "finished_at": None,
    "suite_states": [state]
}
(run_dir / "run.json").write_text(json.dumps(manifest))

def pause(_signum, _frame):
    manifest["suite_states"][0].update({"status": "paused", "reason": "human_interrupted", "phase": "paused"})
    (run_dir / "run.json").write_text(json.dumps(manifest))
    raise SystemExit(3)

signal.signal(signal.SIGINT, pause)
while True:
    time.sleep(0.1)
'''


def wait_until(predicate, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def test_managed_lifecycle_pause_resume_complete_without_vendor_traffic(
    tmp_path: Path, monkeypatch
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    dynamic = scripts / "nvst_dynamic_endpoint.py"
    dynamic.write_text(FAKE_DYNAMIC, encoding="utf-8")
    dynamic.chmod(0o755)
    monkeypatch.setenv("NVST_ALLOW_PROCESS_FALLBACK", "1")
    monkeypatch.setattr(nvst_run_control, "systemd_available", lambda: False)
    monkeypatch.setattr(nvst_run_control.sys, "stdin", io.StringIO(AUTHORIZATION))

    assert nvst_run_control.main(
        [
            "start", "--root", str(tmp_path),
            "--base-url", "https://vendor.invalid/v1",
            "--served-model", "fake-model", "--",
            "accept", "--model", "fake-model",
        ]
    ) == 0
    control_path = next((tmp_path / "runs/.control").glob("*.json"))
    run_id = json.loads(control_path.read_text())["run_id"]
    run_dir = tmp_path / "runs/fake-model" / run_id
    wait_until(lambda: (run_dir / "run.json").is_file())

    path, control = nvst_run_control.read_control(tmp_path, run_id)
    paused = nvst_run_control.signal_run(path, control, stop=False)
    assert paused["status"] == "paused"
    events = [
        json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    assert [event["sequence"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert events[-1]["event"] == "pause_requested"
    assert json.loads((run_dir / "run.json").read_text())["suite_states"][0]["status"] == "paused"

    monkeypatch.setattr(nvst_run_control.sys, "stdin", io.StringIO(AUTHORIZATION))
    assert nvst_run_control.main(
        ["resume", "--root", str(tmp_path), "--run", run_id]
    ) == 0
    wait_until(
        lambda: json.loads(control_path.read_text()).get("status") == "completed"
    )
    final = json.loads((run_dir / "run.json").read_text())
    assert final["finished_at"] == "done"
    assert final["suite_states"][0]["status"] == "passed"
    assert "runtime-secret" not in control_path.read_text()
    assert not list(tmp_path.rglob("*.secret.fifo"))
