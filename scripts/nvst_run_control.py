from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .nvst_dynamic_endpoint import parse_secret_input
except ImportError:
    from nvst_dynamic_endpoint import parse_secret_input

from novita_self_test.artifacts import update_versioned_json, write_json
from novita_self_test.config import find_secret
from novita_self_test.events import emit_event


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}".lower()


def write_control(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    return update_versioned_json(
        path,
        updates,
        schema_name="managed-control.schema.json",
    )


def read_control(root: Path, run: str) -> tuple[Path, dict[str, Any]]:
    run_id_value = Path(run).name
    path = root / "runs" / ".control" / f"{run_id_value}.json"
    if not path.is_file():
        raise RuntimeError(f"managed run control not found: {run_id_value}")
    return path, json.loads(path.read_text(encoding="utf-8"))


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    if not state or state.startswith("Z"):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owned_worker_alive(control_path: Path, control: dict[str, Any]) -> bool:
    if control.get("supervisor") == "systemd":
        unit_name = control.get("unit_name")
        return (
            isinstance(unit_name, str)
            and unit_name.startswith(f"nvst-{control.get('run_id')}-")
            and systemd_unit_active(unit_name)
        )
    pid = int(control.get("worker_pid") or 0)
    if not process_alive(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        rendered = cmdline.read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        rendered = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
    return "nvst_run_control.py" in rendered and str(control_path) in rendered


def systemd_available() -> bool:
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    process = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        text=True,
        capture_output=True,
        check=False,
    )
    return process.stdout.strip() in {"running", "degraded"}


def systemd_unit_active(unit_name: str | None) -> bool:
    if not unit_name:
        return False
    process = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit_name],
        text=True,
        capture_output=True,
        check=False,
    )
    return process.returncode == 0


def _write_fifo_secret(path: Path, secret_input: str, *, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None and time.monotonic() < deadline:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:
                raise
            time.sleep(0.05)
    if descriptor is None:
        raise RuntimeError("managed worker did not open the credential FIFO")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secret_input)
        handle.flush()


def start_worker(control_path: Path, secret_input: str) -> dict[str, Any]:
    parse_secret_input(secret_input)
    if systemd_available():
        control = json.loads(control_path.read_text(encoding="utf-8"))
        unit_name = f"nvst-{control['run_id']}-r{int(control.get('revision') or 0) + 1}.service"
        fifo = control_path.with_suffix(".secret.fifo")
        try:
            fifo.unlink()
        except FileNotFoundError:
            pass
        os.mkfifo(fifo, 0o600)
        command = [
            "systemd-run",
            "--user",
            "--unit",
            unit_name,
            "--collect",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=45",
            # Walle-style suites spawn hundreds of xdist workers; the default
            # user-service soft nofile (1024) makes pytest die with EMFILE.
            "--property=LimitNOFILE=1048576",
            sys.executable,
            str(Path(__file__).resolve()),
            "_worker",
            "--control",
            str(control_path),
            "--secret-fifo",
            str(fifo),
        ]
        process = subprocess.run(command, text=True, capture_output=True, check=False)
        if process.returncode:
            fifo.unlink(missing_ok=True)
            raise RuntimeError(process.stderr.strip() or "systemd-run failed")
        try:
            _write_fifo_secret(fifo, secret_input)
        except Exception:
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name],
                text=True,
                capture_output=True,
                check=False,
            )
            raise
        finally:
            fifo.unlink(missing_ok=True)
        main_pid = subprocess.run(
            ["systemctl", "--user", "show", "--property=MainPID", "--value", unit_name],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        return write_control(
            control_path,
            {
                "status": "running",
                "supervisor": "systemd",
                "unit_name": unit_name,
                "worker_pid": int(main_pid) if main_pid.isdigit() and int(main_pid) else None,
                "process_group_id": None,
                "heartbeat_at": utc_now(),
                "updated_at": utc_now(),
            },
        )
    if os.environ.get("NVST_ALLOW_PROCESS_FALLBACK") != "1":
        raise RuntimeError(
            "user systemd is unavailable; set NVST_ALLOW_PROCESS_FALLBACK=1 for explicit fallback"
        )
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "_worker",
            "--control",
            str(control_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    assert process.stdin is not None
    process.stdin.write(secret_input)
    process.stdin.close()
    return write_control(
        control_path,
        {
            "status": "running",
            "supervisor": "process_group",
            "unit_name": None,
            "worker_pid": process.pid,
            "process_group_id": process.pid,
            "heartbeat_at": utc_now(),
            "updated_at": utc_now(),
        },
    )


def dynamic_command(control: dict[str, Any], *, resume: bool = False) -> list[str]:
    root = Path(control["root"])
    endpoint = control["endpoint"]
    command = [
        sys.executable,
        str(root / "scripts" / "nvst_dynamic_endpoint.py"),
        "--base-url",
        endpoint["base_url"],
        "--served-model",
        endpoint["served_model"],
        "--model-family",
        endpoint["model_family"],
        "--think-mode",
        endpoint["think_mode"],
        "--headers-json",
        json.dumps(endpoint["headers"], ensure_ascii=True, sort_keys=True),
        "--",
    ]
    if resume:
        command.extend(["resume", "--run", control["run_dir"]])
        resume_options = control.get("resume_options") or {}
        continue_from = resume_options.get("continue_from")
        continue_reason = resume_options.get("continue_reason")
        allow_repo_pin_mismatch = bool(resume_options.get("allow_repo_pin_mismatch"))
        if continue_from:
            command.extend(["--continue-from", str(continue_from)])
        if continue_reason:
            command.extend(["--continue-reason", str(continue_reason)])
        if allow_repo_pin_mismatch:
            command.append("--allow-repo-pin-mismatch")
        if resume_options.get("only_suite"):
            command.append("--only-suite")
        if resume_options.get("finalize_only"):
            command.append("--finalize-only")
    else:
        command.extend(control["nvst_args"])
    return command


def worker(control_path: Path, secret_fifo: Path | None = None) -> int:
    if secret_fifo is None:
        secret_input = sys.stdin.read()
    else:
        with secret_fifo.open("r", encoding="utf-8") as handle:
            secret_input = handle.read()
    parse_secret_input(secret_input)
    control = json.loads(control_path.read_text(encoding="utf-8"))
    log_path = Path(control["stdout_log"])
    error_path = Path(control["stderr_log"])
    command = dynamic_command(control, resume=bool(control.get("resuming")))
    with log_path.open("a", encoding="utf-8") as stdout, error_path.open(
        "a", encoding="utf-8"
    ) as stderr:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
        assert child.stdin is not None
        child.stdin.write(secret_input)
        child.stdin.close()
        write_control(
            control_path,
            {
                "child_pid": child.pid,
                "phase": "resuming" if control.get("resuming") else "executing",
                "heartbeat_at": utc_now(),
                "updated_at": utc_now(),
            },
        )
        try:
            while child.poll() is None:
                write_control(
                    control_path,
                    {"heartbeat_at": utc_now(), "updated_at": utc_now()},
                )
                time.sleep(5)
        except KeyboardInterrupt:
            write_control(
                control_path,
                {
                    "status": "pausing",
                    "phase": "interrupting",
                    "heartbeat_at": utc_now(),
                    "updated_at": utc_now(),
                },
            )
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=30)
        code = int(child.returncode or 0)
    paused = code in {3, -signal.SIGINT, -signal.SIGTERM}
    write_control(
        control_path,
        {
            "status": "paused" if paused else "completed" if code == 0 else "failed",
            "phase": "paused" if paused else "finished",
            "exit_code": code,
            "finished_at": None if paused else utc_now(),
            "heartbeat_at": utc_now(),
            "updated_at": utc_now(),
            "resuming": False,
        },
    )
    return code


def wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.25)
    return not process_alive(pid)


def cleanup_managed_resources(control: dict[str, Any]) -> dict[str, Any]:
    from novita_self_test.adapters.deepswe_remote import cleanup_checkpoint_resources

    run_dir = Path(control["run_dir"])
    checkpoints = sorted(run_dir.glob("suites/*/deepswe-checkpoint.json"))
    results = [cleanup_checkpoint_resources(path) for path in checkpoints]
    return {
        "checkpoint_count": len(checkpoints),
        "stopped_containers": [
            container
            for result in results
            for container in result.get("stopped_containers", [])
        ],
    }


def wait_for_systemd_exit(unit_name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not systemd_unit_active(unit_name):
            return True
        time.sleep(0.25)
    return not systemd_unit_active(unit_name)


def emit_control_event(
    control: dict[str, Any], event: str, *, data: dict[str, Any] | None = None
) -> None:
    run_dir = Path(control["run_dir"])
    if (run_dir / "run.json").is_file():
        emit_event(run_dir, event, phase=str(control.get("phase") or "control"), data=data)


def signal_run(path: Path, control: dict[str, Any], *, stop: bool) -> dict[str, Any]:
    emit_control_event(
        control,
        "stop_requested" if stop else "pause_requested",
        data={"supervisor": control.get("supervisor")},
    )
    if control.get("supervisor") == "systemd":
        unit_name = str(control.get("unit_name") or "")
        if not owned_worker_alive(path, control):
            cleanup = cleanup_managed_resources(control)
            return write_control(
                path,
                {
                    "status": "stopped" if stop else "paused",
                    "phase": "not_running",
                    "updated_at": utc_now(),
                    "resource_cleanup": cleanup,
                },
            )
        write_control(
            path,
            {
                "status": "stopping" if stop else "pausing",
                "phase": "signalling",
                "updated_at": utc_now(),
            },
        )
        command = ["systemctl", "--user"]
        if stop:
            command.extend(["stop", unit_name])
        else:
            command.extend(["kill", "--kill-whom=all", "--signal=SIGINT", unit_name])
        subprocess.run(command, text=True, capture_output=True, check=False)
        if not wait_for_systemd_exit(unit_name, 45):
            subprocess.run(
                ["systemctl", "--user", "stop", unit_name],
                text=True,
                capture_output=True,
                check=False,
            )
            wait_for_systemd_exit(unit_name, 45)
        cleanup = cleanup_managed_resources(control)
        return write_control(
            path,
            {
                "status": "stopped" if stop else "paused",
                "phase": "stopped" if stop else "paused",
                "finished_at": utc_now() if stop else None,
                "heartbeat_at": utc_now(),
                "updated_at": utc_now(),
                "resource_cleanup": cleanup,
            },
        )
    pid = int(control.get("worker_pid") or 0)
    pgid = int(control.get("process_group_id") or pid)
    if not process_alive(pid):
        cleanup = cleanup_managed_resources(control)
        return write_control(
            path,
            {
                "status": "stopped" if stop else "paused",
                "phase": "not_running",
                "updated_at": utc_now(),
                "resource_cleanup": cleanup,
            },
        )
    if not owned_worker_alive(path, control):
        raise RuntimeError("managed worker PID no longer belongs to this run")
    write_control(
        path,
        {
            "status": "stopping" if stop else "pausing",
            "phase": "signalling",
            "updated_at": utc_now(),
        },
    )
    os.killpg(pgid, signal.SIGTERM if stop else signal.SIGINT)
    if not wait_for_exit(pid, 30):
        os.killpg(pgid, signal.SIGTERM)
    if not wait_for_exit(pid, 30):
        os.killpg(pgid, signal.SIGKILL)
        wait_for_exit(pid, 10)
    cleanup = cleanup_managed_resources(control)
    return write_control(
        path,
        {
            "status": "stopped" if stop else "paused",
            "phase": "stopped" if stop else "paused",
            "finished_at": utc_now() if stop else None,
            "heartbeat_at": utc_now(),
            "updated_at": utc_now(),
            "resource_cleanup": cleanup,
        },
    )


def reconcile_interrupted_manifest(control: dict[str, Any]) -> dict[str, Any] | None:
    manifest_path = Path(control["run_dir"]) / "run.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("finished_at"):
        return manifest
    changed = False
    for state in manifest.get("suite_states", []):
        if state.get("status") in {"running", "pausing"}:
            state.update(
                {
                    "status": "interrupted",
                    "reason": "human_interrupted",
                    "phase": "supervisor_interrupted",
                    "finished_at": None,
                    "heartbeat_at": utc_now(),
                }
            )
            changed = True
    if changed:
        write_json(manifest_path, manifest)
        emit_event(
            manifest_path.parent,
            "supervisor_reconciled",
            phase="supervisor_interrupted",
            data={"reason": "worker_not_active"},
        )
    return manifest


def status(root: Path, run: str) -> dict[str, Any]:
    path, control = read_control(root, run)
    alive = owned_worker_alive(path, control)
    run_manifest: dict[str, Any] | None = None
    manifest_path = Path(control["run_dir"]) / "run.json"
    if manifest_path.is_file():
        run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if control.get("status") in {"running", "pausing", "stopping"} and not alive:
        run_manifest = reconcile_interrupted_manifest(control) or run_manifest
        deferred = bool(
            run_manifest
            and any(
                item.get("status") in {"deferred", "paused", "interrupted"}
                for item in run_manifest.get("suite_states", [])
            )
        )
        control = write_control(
            path,
            {
                "status": "paused" if deferred else "failed",
                "phase": "paused" if deferred else "worker_exited",
                "updated_at": utc_now(),
            },
        )
    return {
        **control,
        "worker_alive": alive,
        "run_manifest": None
        if run_manifest is None
        else {
            "started_at": run_manifest.get("started_at"),
            "finished_at": run_manifest.get("finished_at"),
            "suite_states": run_manifest.get("suite_states") or [],
        },
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Manage durable nvst runs")
    commands = result.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--root", required=True, type=Path)
    start.add_argument("--base-url", required=True)
    start.add_argument("--served-model", required=True)
    start.add_argument("--model-family", default="kimi-k3")
    start.add_argument("--think-mode", default="opensource")
    start.add_argument("--headers-json", default="{}")
    start.add_argument("nvst_args", nargs=argparse.REMAINDER)
    for name in ("status", "pause", "stop", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--run", required=True)
        if name == "resume":
            command.add_argument("--continue-from")
            command.add_argument("--continue-reason")
            command.add_argument("--allow-repo-pin-mismatch", action="store_true")
            command.add_argument("--only-suite", action="store_true")
            command.add_argument("--finalize-only", action="store_true")
    internal = commands.add_parser("_worker")
    internal.add_argument("--control", required=True, type=Path)
    internal.add_argument("--secret-fifo", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "_worker":
        return worker(args.control, args.secret_fifo)
    root = args.root.resolve()
    if args.command == "start":
        nvst_args = list(args.nvst_args)
        if nvst_args[:1] == ["--"]:
            nvst_args.pop(0)
        if not nvst_args or nvst_args[0] not in {"accept", "develop"}:
            raise RuntimeError("managed start requires accept or develop")
        if "--run-dir" in nvst_args:
            raise RuntimeError("managed start owns --run-dir")
        try:
            headers = json.loads(args.headers_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid headers JSON: {exc}") from exc
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise RuntimeError("headers JSON must be a string mapping")
        model = nvst_args[nvst_args.index("--model") + 1]
        identifier = run_id()
        managed_run_dir = root / "runs" / model / identifier
        nvst_args.extend(["--run-dir", str(managed_run_dir)])
        control_path = root / "runs" / ".control" / f"{identifier}.json"
        endpoint = {
            "base_url": args.base_url.rstrip("/"),
            "served_model": args.served_model,
            "model_family": args.model_family,
            "think_mode": args.think_mode,
            "headers": headers,
        }
        secret = find_secret({"endpoint": endpoint, "nvst_args": nvst_args})
        if secret:
            raise RuntimeError(secret)
        control = write_control(
            control_path,
            {
                "schema_version": 1,
                "run_id": identifier,
                "run_dir": str(managed_run_dir),
                "root": str(root),
                "endpoint": endpoint,
                "nvst_args": nvst_args,
                "status": "starting",
                "phase": "starting",
                "started_at": utc_now(),
                "finished_at": None,
                "stdout_log": str(control_path.with_suffix(".stdout.log")),
                "stderr_log": str(control_path.with_suffix(".stderr.log")),
            },
        )
        control = start_worker(control_path, sys.stdin.read())
    else:
        control_path, control = read_control(root, args.run)
        if args.command == "status":
            control = status(root, args.run)
        elif args.command == "pause":
            control = signal_run(control_path, control, stop=False)
        elif args.command == "stop":
            control = signal_run(control_path, control, stop=True)
        elif args.command == "resume":
            if owned_worker_alive(control_path, control):
                raise RuntimeError("managed run is already active")
            if bool(args.continue_from) != bool(args.continue_reason):
                raise RuntimeError("--continue-from and --continue-reason must be provided together")
            if args.allow_repo_pin_mismatch and not args.continue_from:
                raise RuntimeError("--allow-repo-pin-mismatch requires --continue-from")
            if args.only_suite and not args.continue_from:
                raise RuntimeError("--only-suite requires --continue-from")
            if args.finalize_only and not args.only_suite:
                raise RuntimeError("--finalize-only requires --only-suite")
            control = write_control(
                control_path,
                {
                    "resuming": True,
                    "resume_options": {
                        "continue_from": args.continue_from,
                        "continue_reason": args.continue_reason,
                        "allow_repo_pin_mismatch": args.allow_repo_pin_mismatch,
                        "only_suite": args.only_suite,
                        "finalize_only": args.finalize_only,
                    },
                    "status": "starting",
                    "phase": "resuming",
                    "finished_at": None,
                    "updated_at": utc_now(),
                },
            )
            emit_control_event(control, "resume_requested")
            control = start_worker(control_path, sys.stdin.read())
    print(json.dumps(control, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc
