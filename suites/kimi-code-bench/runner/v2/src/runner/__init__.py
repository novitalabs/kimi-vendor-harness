"""Remote runner primitives for the single official-observed schedule."""

from __future__ import annotations

import json
import hashlib
import os
import shlex
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import load_contract, task_plan
from status import initialize_status, refresh_remote_status, update_status


_REMOTE_STOP = r'''
import json
import os
import pathlib
import signal
import sys
import time

root = pathlib.Path(sys.argv[1]).resolve()
if (root / ".kbench-paused").is_file():
    print(json.dumps({"state": "paused", "already_stopped": True}))
    raise SystemExit(0)
if not (root / ".kbench-running").is_file():
    raise SystemExit("run is not marked running")
try:
    pid = int((root / ".kbench-pid").read_text().strip())
except Exception as exc:
    raise SystemExit(f"invalid runner pid: {exc}")
proc = pathlib.Path(f"/proc/{pid}")
if not proc.is_dir():
    raise SystemExit("runner pid is not active")
if (proc / "cwd").resolve() != root:
    raise SystemExit("runner pid cwd does not match the run workdir")
command = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
if b"setup_and_smoke.sh" not in command:
    raise SystemExit("runner pid command does not match setup_and_smoke.sh")
pgid = os.getpgid(pid)
if pgid <= 1:
    raise SystemExit("refusing to signal an unsafe process group")
(root / ".kbench-pause-requested").write_text("human\n")
os.killpg(pgid, signal.SIGTERM)
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    if (root / ".kbench-paused").is_file():
        print(json.dumps({"state": "paused", "pid": pid, "pgid": pgid}))
        raise SystemExit(0)
    time.sleep(0.1)
raise SystemExit("runner did not acknowledge pause")
'''


def _workdir_from_snapshot(snapshot_path: str | Path) -> str:
    """Read the immutable remote workdir from the launch snapshot."""
    return json.loads(Path(snapshot_path).read_text())["remote_workdir"]


def _bound_remote(run_dir: str | Path) -> tuple[Path, dict[str, Any], str]:
    root = Path(run_dir).resolve()
    snapshot_path = root / "config.snapshot.json"
    snapshot_data = json.loads(snapshot_path.read_text())
    contract_path = root / "run-contract.json"
    contract = load_contract(contract_path)
    contract_sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    if snapshot_data.get("runner_contract_sha256") != contract_sha:
        raise RuntimeError("snapshot contract hash does not match the self-contained run contract")
    remote_dir = _workdir_from_snapshot(snapshot_path)
    if remote_dir != contract["remote"]["workdir"]:
        raise RuntimeError("snapshot remote workdir does not match the run contract")
    return root, contract, remote_dir


def fetch_remote(run_dir: str | Path) -> Path:
    root, contract, remote_dir = _bound_remote(run_dir)
    remote = contract["remote"]
    destination = root / "remote.tar.gz"
    command = [
        "rsync", "-a", "-e", f"ssh -p {int(remote['port'])}",
        f"{remote['host']}:{remote_dir}/remote.tar.gz", str(destination),
    ]
    subprocess.run(command, check=True)
    archive_root = root / "archive"
    if archive_root.exists() and any(archive_root.iterdir()):
        raise RuntimeError(f"archive directory is not empty: {archive_root}")
    archive_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "r:gz") as archive:
        archive.extractall(archive_root, filter="data")
    update_status(root, state="completed", phase="fetch", evidence={"local_archive": str(archive_root)})
    return destination


def stop_remote(run_dir: str | Path) -> dict[str, Any]:
    root, contract, remote_dir = _bound_remote(run_dir)
    if not contract["run"].get("detached"):
        raise RuntimeError("only a detached run can be stopped")
    remote = contract["remote"]
    command = "python3 -c " + shlex.quote(_REMOTE_STOP) + " " + shlex.quote(remote_dir)
    result = subprocess.run(
        ["ssh", "-p", str(remote["port"]), remote["host"], command],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "remote stop failed")
    status = refresh_remote_status(root)
    if status["state"] != "paused":
        raise RuntimeError("remote run stopped without a paused status")
    return status


@dataclass(frozen=True)
class QueueItem:
    task: str
    round: int
    attempt: int
    phase: str = "baseline"


def build_round_major_queue(tasks: list[str], rounds: int = 3) -> list[QueueItem]:
    if rounds != 3:
        raise ValueError("official-observed runs require three rounds")
    return [QueueItem(task, round_no, round_no, "baseline") for round_no in range(1, rounds + 1) for task in tasks]


def build_mixed_queue(tasks: list[str], rounds: int = 3) -> list[QueueItem]:
    """Backward-compatible name for the baseline queue builder."""
    return build_round_major_queue(tasks, rounds)


def recovery_queue(records: list[dict[str, Any]]) -> list[QueueItem]:
    # Reward-zero is a valid observation; only execution-invalid records retry.
    tasks = dict.fromkeys(
        str(record["task"])
        for record in records
        if record.get("execution_valid") is False
    )
    return [QueueItem(task, 0, 1, "recovery") for task in tasks]


def snapshot(contract_path: str | Path, run_dir: str | Path) -> Path:
    contract = load_contract(contract_path)
    tasks, count = task_plan(contract)
    root = Path(run_dir); root.mkdir(parents=True, exist_ok=True)
    contract_sha = hashlib.sha256(Path(contract_path).read_bytes()).hexdigest()
    data = {
        "tag": contract["run"]["tag"],
        "base_url": contract["service"]["base_url"],
        "model": contract["service"]["model"],
        "headers": contract["service"].get("headers", {}),
        "preserve_thinking": contract["service"]["preserve_thinking"],
        "remote_workdir": contract["remote"]["workdir"],
        "runtime_cache": {
            "root": contract["remote"]["runtime_cache_root"],
            "uv_version": contract["pins"]["uv_version"],
            "harbor_python": contract["pins"]["harbor_python"],
        },
        "runner_contract_sha256": contract_sha,
        "authentication": contract["authentication"],
        "task_selection": {"kind": "builtin", "value": contract["task_selection"]["value"], "count": count},
        "attempts_per_task": contract["run"]["attempts_per_task"],
        "rounds": contract["run"]["rounds"],
        "expected_trial_count": contract["run"]["required_trial_count"],
        "concurrency": contract["run"]["concurrency"],
        "tasks": tasks,
        "schedule": contract["execution_profile"]["schedule"],
        "concurrency_scope": contract["execution_profile"]["concurrency_scope"],
        "per_task_concurrency": contract["execution_profile"]["per_task_concurrency"],
        "recovery": contract["execution_profile"]["recovery"],
        "harbor_n_attempts": contract["execution_profile"]["harbor_n_attempts"],
        "harbor_timeout_multipliers": contract["execution_profile"]["harbor_timeout_multipliers"],
        "kimi_loop_control": contract["execution_profile"]["kimi_loop_control"],
    }
    path = root / "config.snapshot.json"
    _write_immutable(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
    return path


def _write_immutable(path: Path, content: bytes) -> None:
    if path.is_file():
        if path.read_bytes() != content:
            raise RuntimeError(f"immutable run file differs: {path}")
        return
    path.write_bytes(content)


def prepare_run(contract_path: str | Path, run_dir: str | Path) -> tuple[dict[str, Any], Path]:
    source = Path(contract_path).resolve()
    contract = load_contract(source)
    root = Path(run_dir).resolve()
    expected_root = Path(contract["run"]["evidence_dir"]).resolve()
    if root != expected_root:
        raise RuntimeError("run directory does not match contract evidence_dir")
    root.mkdir(parents=True, exist_ok=True)
    local_contract = root / "run-contract.json"
    _write_immutable(local_contract, source.read_bytes())
    snapshot_path = snapshot(local_contract, root)

    template_dir = Path(__file__).resolve().parent / "templates"
    setup_template = (template_dir / "setup_and_smoke.sh.tmpl").read_text()
    agent_template = (template_dir / "local_kimi_cli_vendor_agent.py.tmpl").read_text()
    substitutions = {
        "@@CONTRACT_JSON@@": local_contract.read_text(),
        "@@SNAPSHOT_JSON@@": snapshot_path.read_text(),
    }
    rendered_setup = setup_template
    for marker, value in substitutions.items():
        rendered_setup = rendered_setup.replace(marker, value)
    rendered_setup_path = root / "setup_and_smoke.sh.rendered"
    rendered_agent_path = root / "local_kimi_cli_vendor_agent.py.rendered"
    rendered_setup_path.write_text(rendered_setup)
    rendered_agent_path.write_text(agent_template)
    for path in (rendered_setup_path, rendered_agent_path):
        path.chmod(0o700)

    runner_root = Path(__file__).resolve().parents[2]
    for source_path, name in (
        (runner_root / "src/recording_proxy.py", "recording_proxy.py"),
        (runner_root / "src/remote_preflight.py", "remote_preflight.py"),
        (runner_root / "src/runtime_cache.py", "runtime_cache.py"),
        (runner_root / "src/archive_trial.py", "archive_trial.py"),
    ):
        (root / name).write_text(source_path.read_text())
    _write_immutable(root / "release-manifest.json", (runner_root / "release-manifest.json").read_bytes())
    initialize_status(root, state="preparing", phase="preflight")
    return contract, root


def run_remote(contract_path: str | Path, run_dir: str | Path) -> int:
    source_contract = Path(contract_path).resolve()
    skill_root = Path(__file__).resolve().parents[4]
    validator = skill_root / "scripts/validate-run-contract.sh"
    validation = subprocess.run([str(validator), str(source_contract)], check=False, capture_output=True, text=True)
    if validation.returncode:
        raise RuntimeError(validation.stderr.strip() or "run contract validation failed")
    contract, root = prepare_run(contract_path, run_dir)
    gates = root / "gates"
    gates.mkdir(exist_ok=True)
    (gates / "contract-validation.txt").write_text(validation.stdout)
    audit = subprocess.run(
        [str(skill_root / "scripts/audit-run-secrets.sh"), str(root / "run-contract.json"), str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    (gates / "pre-push-secret-audit.txt").write_text(audit.stdout + audit.stderr)
    if audit.returncode:
        update_status(root, state="failed", phase="preflight", blockers=["pre-push-secret-audit-failed"])
        return audit.returncode
    remote = contract["remote"]
    host = remote["host"]
    port = str(remote["port"])
    remote_dir = remote["workdir"]
    ssh = ["ssh", "-p", port, host]
    mkdir = subprocess.run(
        ssh + [f"test ! -e {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_dir)}"],
        check=False,
    )
    if mkdir.returncode:
        update_status(root, state="failed", phase="remote_setup", blockers=["remote-workdir-exists-or-create-failed"])
        return mkdir.returncode
    auth_env = contract.get("authentication", {}).get("env_name")
    auth_value = os.environ.get(auth_env, "") if auth_env else ""
    if contract.get("authentication", {}).get("kind") == "env" and not auth_value:
        update_status(root, state="failed", phase="remote_setup", blockers=["authentication-environment-unset"])
        raise RuntimeError(f"authentication environment variable is unset: {auth_env}")
    if auth_value:
        secret_path = f"{remote_dir}/.kbench-api-key"
        result = subprocess.run(
            ssh + [f"umask 077; cat > {shlex.quote(secret_path)}; chmod 600 {shlex.quote(secret_path)}"],
            input=(auth_value + "\n").encode(),
            check=False,
        )
        if result.returncode:
            return result.returncode
    local_files = (
        root / "setup_and_smoke.sh.rendered",
        root / "local_kimi_cli_vendor_agent.py.rendered",
        root / "recording_proxy.py",
        root / "remote_preflight.py",
        root / "runtime_cache.py",
        root / "archive_trial.py",
    )
    for local_path in local_files:
        remote_path = f"{remote_dir}/{local_path.name}"
        result = subprocess.run(
            ssh + [f"cat > {shlex.quote(remote_path)}"],
            input=local_path.read_bytes(),
            check=False,
        )
        if result.returncode:
            subprocess.run(ssh + [f"rm -f {shlex.quote(remote_dir + '/.kbench-api-key')}"], check=False)
            update_status(root, state="failed", phase="remote_setup", blockers=[f"remote-upload-failed:{local_path.name}"])
            return result.returncode
    remote_command = (
        f"cd {shlex.quote(remote_dir)} && "
        "chmod 700 setup_and_smoke.sh.rendered local_kimi_cli_vendor_agent.py.rendered archive_trial.py && "
        "mv setup_and_smoke.sh.rendered setup_and_smoke.sh && "
        "mv local_kimi_cli_vendor_agent.py.rendered local_kimi_cli_vendor_agent.py && "
        "bash setup_and_smoke.sh"
    )
    if contract["run"].get("detached"):
        remote_command = (
            "nohup bash -lc " + shlex.quote(remote_command) +
            " > " + shlex.quote(f"{remote_dir}/setup.log") +
            " 2>&1 < /dev/null & echo $!"
        )
    result = subprocess.run(ssh + [remote_command], check=False, capture_output=True, text=True)
    if result.returncode:
        update_status(root, state="failed", phase="remote_setup", blockers=["remote-launch-failed"])
        return result.returncode
    pid = None
    if contract["run"].get("detached"):
        try:
            pid = int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
    update_status(
        root,
        state="running",
        phase="remote_setup",
        remote={"marker": "running", "pid": pid, "exit_code": None},
        blockers=[],
    )
    return 0
