from __future__ import annotations

import concurrent.futures
import json
import os
import resource
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .adapters.deepswe_remote import verify_runtime
from .config import ConfigError, JudgeAccess, ModelProfile, ServiceAccess, workspace_root
from .orchestrator import repository_pins
from .schema_validation import validation_error
from .service_probe import probe_service
from .stability import classify_failure


def _verifier_source_preflight(profile: ModelProfile, root: Path) -> dict[str, Any] | None:
    contract = profile.source.get("verifier_repository") or {}
    if not isinstance(contract, dict) or not contract.get("checkout"):
        return None
    checkout = (root / str(contract["checkout"])).resolve()
    expected_commit = str(contract.get("commit") or "")
    observed_commit = ""
    vendored = checkout / "VENDORED_COMMIT"
    try:
        if vendored.is_file():
            observed_commit = vendored.read_text(encoding="utf-8").strip()
        else:
            observed_commit = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return {
            "status": "blocked",
            "reason": "verifier_checkout_missing",
            "checkout": str(checkout),
            "expected_commit": expected_commit,
            "observed_commit": None,
        }
    lfs_paths = list(contract.get("lfs_paths") or [])
    pointer_paths: list[str] = []
    missing_paths: list[str] = []
    for pattern in lfs_paths:
        matches = sorted(checkout.glob(str(pattern)))
        if not matches:
            missing_paths.append(str(pattern))
            continue
        for path in matches:
            try:
                first_line = path.open("rb").readline()
            except OSError:
                first_line = b""
            if first_line.startswith(b"version https://git-lfs.github.com/spec/v1"):
                pointer_paths.append(str(path.relative_to(checkout)))
    return {
        "status": "passed"
        if observed_commit == expected_commit and not missing_paths and not pointer_paths
        else "blocked",
        "reason": None
        if observed_commit == expected_commit and not missing_paths and not pointer_paths
        else "verifier_source_incomplete",
        "checkout": str(checkout),
        "expected_commit": expected_commit,
        "observed_commit": observed_commit,
        "missing_lfs_paths": missing_paths,
        "pointer_paths": pointer_paths,
    }

def _execution_plan(profile: ModelProfile, acceptance_mode: str, root: Path) -> dict[str, Any]:
    selectors = list(profile.acceptance_mode_config(acceptance_mode).get("selectors") or [])
    suites: list[dict[str, Any]] = []
    errors: list[str] = []
    for selector in selectors:
        for suite, selector_args in profile.select([selector]):
            completeness = suite.completeness
            fingerprint = dict(suite.config.get("runner_fingerprint") or {})
            expected_trials = completeness.get("expected_trials")
            expected_counts = completeness.get("expected_counts") or {}
            per_repeat = completeness.get("per_repeat") or {}
            if expected_trials is None and isinstance(expected_counts, dict):
                expected_trials = expected_counts.get("total")
            if isinstance(per_repeat, dict) and per_repeat.get("expected_counts"):
                expected_trials = per_repeat["expected_counts"].get("total", expected_trials)
            required_artifacts = list(completeness.get("required_artifacts") or [])
            required_artifacts.extend(completeness.get("required_artifact_patterns") or [])
            if isinstance(per_repeat, dict):
                required_artifacts.extend(per_repeat.get("required_artifacts") or [])
                required_artifacts.extend(per_repeat.get("required_artifact_patterns") or [])
            if suite.adapter == "deepswe" and expected_trials is None:
                expected_trials = int(completeness.get("expected_tasks", 0)) * int(completeness.get("expected_rounds", 0))
            entry = {
                "selector": selector,
                "suite_id": suite.suite_id,
                "adapter": suite.adapter,
                "row_ids": list(suite.row_ids),
                "selector_args": list(selector_args),
                "expected_trials_per_repeat": expected_trials,
                "expected_repeats": completeness.get("expected_repeats", 1),
                "retry_policy": suite.config.get("rerun_policy") or {},
                "artifact_contract": fingerprint.get("artifact_contract"),
                "required_artifacts": list(dict.fromkeys(str(item) for item in required_artifacts)),
            }
            if suite.adapter in {"kvv", "walle", "inspect", "replay", "novita_api", "deepswe", "beam", "code_bench"} and expected_trials is None:
                errors.append(f"{suite.suite_id}: missing expected trial/count contract")
            if suite.adapter in {"kvv", "walle", "inspect", "replay", "novita_api", "deepswe", "beam", "code_bench"} and not entry["artifact_contract"]:
                errors.append(f"{suite.suite_id}: missing runner_fingerprint.artifact_contract")
            if suite.adapter == "inspect":
                command = list(suite.config.get("command") or [])
                dataset = None
                epochs = 1
                for index, value in enumerate(command):
                    if value == "--dataset" and index + 1 < len(command):
                        dataset = str(command[index + 1])
                    if value == "--epochs" and index + 1 < len(command):
                        epochs = int(command[index + 1])
                if dataset:
                    dataset_path = suite.config.get("cwd", ".")
                    path = (root / str(dataset_path) / dataset).resolve()
                    if not path.is_file():
                        errors.append(f"{suite.suite_id}: dataset missing: {path}")
                    else:
                        sample_count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
                        derived_trials = sample_count * epochs
                        entry["dataset"] = str(path)
                        entry["dataset_samples"] = sample_count
                        entry["epochs"] = epochs
                        entry["derived_trials"] = derived_trials
                        if expected_trials is not None and suite.suite_id.startswith("toolcall") and int(expected_trials) != derived_trials:
                            errors.append(f"{suite.suite_id}: expected {expected_trials} trials but derived {derived_trials}")
            suites.append(entry)
    return {
        "schema_version": 1,
        "acceptance_mode": acceptance_mode,
        "selectors": selectors,
        "suites": suites,
        "status": "passed" if not errors else "blocked",
        "errors": errors,
    }


def _capacity_probe(
    service: ServiceAccess,
    *,
    levels: tuple[int, ...] = (1, 10, 20),
    requests_per_level: int = 3,
    timeout: float = 90,
) -> dict[str, Any]:
    from collections import Counter

    def request_once(level: int, index: int) -> dict[str, Any]:
        started = time.monotonic()
        payload = json.dumps({
            "model": service.served_model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "stream": False,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{service.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {service.credential()}",
                "Content-Type": "application/json",
                **service.headers,
            },
            method="POST",
        )
        status = None
        body = ""
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                body = response.read(4096).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            body = exc.read(4096).decode("utf-8", errors="replace")
        except Exception as exc:
            body = f"{type(exc).__name__}: {exc}"
        return {
            "level": level,
            "index": index,
            "http_status": status,
            "latency_seconds": round(time.monotonic() - started, 3),
            "failure_class": None
            if status is not None and 200 <= status < 300
            else classify_failure(f"HTTP {status} {body}"),
        }

    levels_result = []
    for level in levels:
        with concurrent.futures.ThreadPoolExecutor(max_workers=level) as executor:
            observations = list(executor.map(lambda index: request_once(level, index), range(requests_per_level * level)))
        classes = dict(Counter(
            item["failure_class"] for item in observations if item["failure_class"]
        ))
        successful = sum(item["http_status"] is not None and 200 <= item["http_status"] < 300 for item in observations)
        levels_result.append({
            "concurrency": level,
            "requests": len(observations),
            "successful": successful,
            "success_rate": successful / len(observations) if observations else 0.0,
            "failure_class_counts": classes,
            "observations": observations,
        })
    blocked = any(
        item["success_rate"] < 0.98
        or any(key in item["failure_class_counts"] for key in ("engine_overloaded", "rate_limited", "service_unavailable"))
        for item in levels_result
    )
    return {
        "schema_version": 1,
        "status": "blocked" if blocked else "passed",
        "reason": "capacity_probe_failure" if blocked else None,
        "levels": levels_result,
    }

def _check(
    name: str,
    passed: bool,
    *,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "passed" if passed else "blocked",
        "reason": None if passed else reason,
        "details": details or {},
    }


def _entrypoint_exists(value: str, root: Path) -> bool:
    rendered = value.replace("{repository_root}", str(root))
    if "/" not in rendered:
        return shutil.which(rendered) is not None
    return Path(rendered).resolve().is_file()


def _judge_canary(judge: JudgeAccess, *, timeout: float = 30) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": judge.model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{judge.base_url}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {judge.credential()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            response.read(4096)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except Exception as exc:
        return {
            "status": "blocked",
            "reason": f"{type(exc).__name__}: judge canary failed",
            "details": {"http_status": None},
        }
    return {
        "status": "passed" if 200 <= status <= 299 else "blocked",
        "reason": None if 200 <= status <= 299 else "judge canary returned non-2xx",
        "details": {"http_status": status},
    }


def _host_capacity_canary(root: Path, runtime_root: Path | None) -> dict[str, Any]:
    target = runtime_root if runtime_root and runtime_root.exists() else root
    disk = shutil.disk_usage(target)
    stat = os.statvfs(target)
    soft_fd, hard_fd = resource.getrlimit(resource.RLIMIT_NOFILE)
    docker = subprocess.run(
        ["docker", "info", "--format", "{{json .ServerVersion}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    images = subprocess.run(
        ["docker", "images", "--quiet"],
        text=True,
        capture_output=True,
        check=False,
    )
    passed = docker.returncode == 0 and disk.free > 0 and stat.f_favail > 0
    return {
        "status": "passed" if passed else "blocked",
        "reason": None if passed else "host capacity canary failed",
        "details": {
            "path": str(target),
            "disk_free_bytes": disk.free,
            "inode_free": stat.f_favail,
            "fd_soft_limit": soft_fd,
            "fd_hard_limit": hard_fd,
            "docker_ready": docker.returncode == 0,
            "cached_image_ids": len(set(images.stdout.splitlines())),
        },
    }


def _docker_network_canary() -> dict[str, Any]:
    name = f"nvst-doctor-{uuid.uuid4().hex[:8]}"
    create = subprocess.run(
        [
            "docker",
            "network",
            "create",
            "--label",
            "novita-self-test.doctor=true",
            "--subnet",
            "192.168.255.240/28",
            name,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        passed = create.returncode == 0
        return {
            "status": "passed" if passed else "blocked",
            "reason": None if passed else "Docker /28 network canary failed",
            "details": {"subnet": "192.168.255.240/28"},
        }
    finally:
        if create.returncode == 0:
            subprocess.run(
                ["docker", "network", "rm", name],
                text=True,
                capture_output=True,
                check=False,
            )


def _online_checks(
    *,
    service: ServiceAccess,
    judge: JudgeAccess | None,
    root: Path,
    runtime_root: Path | None,
    capacity_probe: bool = False,
) -> list[dict[str, Any]]:
    endpoint = probe_service(service)
    endpoint_passed = endpoint.get("status") == "passed"
    checks = [
        _check(
            "online_endpoint_canary",
            endpoint_passed,
            reason=str(endpoint.get("reason") or "endpoint canary failed"),
            details={
                key: endpoint.get(key)
                for key in ("http_status", "chat_http_status", "reason")
            },
        )
    ]
    if judge is not None:
        judge_result = _judge_canary(judge)
        checks.append(
            _check(
                "online_judge_canary",
                judge_result["status"] == "passed",
                reason=judge_result.get("reason"),
                details=judge_result["details"],
            )
        )
    capacity = _host_capacity_canary(root, runtime_root)
    checks.append(
        _check(
            "online_host_capacity",
            capacity["status"] == "passed",
            reason=capacity.get("reason"),
            details=capacity["details"],
        )
    )
    network = _docker_network_canary()
    checks.append(
        _check(
            "online_docker_network",
            network["status"] == "passed",
            reason=network.get("reason"),
            details=network["details"],
        )
    )
    if capacity_probe:
        capacity = _capacity_probe(service)
        checks.append(
            _check(
                "online_endpoint_capacity",
                capacity["status"] == "passed",
                reason=capacity.get("reason"),
                details=capacity,
            )
        )
    return checks


def run_doctor(
    model_id: str,
    *,
    service_access_path: Path,
    judge_access_path: Path | None = None,
    online_canary: bool = False,
    acceptance_mode: str = "full",
    capacity_probe: bool = False,
    root: Path | None = None,
) -> dict[str, Any]:
    root = (root or workspace_root()).resolve()
    if capacity_probe and not online_canary:
        raise ConfigError("capacity_probe_requires_online_canary", "capacity_probe requires online_canary")
    profile = ModelProfile.load(model_id, root=root)
    plan = _execution_plan(profile, acceptance_mode, root)
    service = ServiceAccess.load(service_access_path)
    judge = JudgeAccess.load(judge_access_path) if judge_access_path else None
    checks: list[dict[str, Any]] = []
    if profile.source.get("verifier_repository"):
        checks.append(
            _check(
                "execution_plan",
                plan["status"] == "passed",
                reason="static execution plan contract failed",
                details=plan,
            )
        )

    try:
        execution_site = profile.require_execution_site()
    except Exception as exc:  # ConfigError is rendered as a structured blocker.
        checks.append(_check("execution_site", False, reason=str(exc)))
    else:
        checks.append(_check("execution_site", True, details={"site": execution_site}))
        if execution_site == "build-server":
            systemd = subprocess.run(
                ["systemctl", "--user", "is-system-running"],
                text=True,
                capture_output=True,
                check=False,
            )
            linger = subprocess.run(
                ["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                text=True,
                capture_output=True,
                check=False,
            )
            supervisor_ok = (
                systemd.stdout.strip() in {"running", "degraded"}
                and linger.stdout.strip() == "yes"
            )
            checks.append(
                _check(
                    "managed_systemd_supervisor",
                    supervisor_ok,
                    reason="user systemd must be running with linger enabled",
                    details={
                        "systemd_state": systemd.stdout.strip(),
                        "linger": linger.stdout.strip(),
                    },
                )
            )

    accepted_families = set(profile.profile.get("model_families") or [model_id])
    checks.append(
        _check(
            "service_access",
            service.model_family in accepted_families,
            reason="service_model_family_mismatch",
            details={
                "model_family": service.model_family,
                "served_model": service.served_model,
                "credential_env": service.api_key_env,
            },
        )
    )
    checks.append(
        _check(
            "endpoint_credential_env",
            bool(service.credential()),
            reason=f"credential environment variable is not set: {service.api_key_env}",
            details={"credential_env": service.api_key_env},
        )
    )

    pins = repository_pins(root)
    dirty = sorted(
        name
        for name, pin in pins.items()
        if pin.get("commit")
        and (pin.get("tracked_dirty") or int(pin.get("untracked_count") or 0) > 0)
    )
    checks.append(
        _check(
            "repository_cleanliness",
            not dirty,
            reason="formal acceptance requires clean child repositories",
            details={"dirty_repositories": dirty, "pins": pins},
        )
    )

    verifier_preflight = _verifier_source_preflight(profile, root)
    if verifier_preflight is not None:
        checks.append(
            _check(
                "verifier_source",
                verifier_preflight.get("status") == "passed",
                reason=str(
                    verifier_preflight.get("reason") or "verifier source preflight failed"
                ),
                details=verifier_preflight,
            )
        )

    missing_runners: list[str] = []
    for suite in profile.suites:
        configured_python = suite.config.get("python")
        if isinstance(configured_python, str):
            if not _entrypoint_exists(configured_python, root):
                missing_runners.append(f"{suite.suite_id}:{configured_python}")
        command = suite.config.get("command")
        if isinstance(command, list) and command and isinstance(command[0], str):
            if not _entrypoint_exists(command[0], root):
                missing_runners.append(f"{suite.suite_id}:{command[0]}")
    checks.append(
        _check(
            "runner_entrypoints",
            not missing_runners,
            reason="runner entrypoint is missing",
            details={"missing": missing_runners},
        )
    )

    beam = next((suite for suite in profile.suites if suite.adapter == "beam"), None)
    if beam is not None:
        env = dict(beam.config.get("env") or {})
        judge_env = env.get("NVST_BEAM_JUDGE_API_KEY_ENV")
        fingerprint = dict(beam.config.get("runner_fingerprint") or {})
        judge_rpm = fingerprint.get("judge_requests_per_minute")
        judge_ok = (
            judge is not None
            and judge.api_key_env == judge_env
            and judge.base_url == env.get("NVST_BEAM_JUDGE_BASE_URL")
            and judge.model == env.get("NVST_BEAM_JUDGE_MODEL")
            and str(judge.requests_per_minute) == str(judge_rpm)
            and bool(judge.credential())
        )
        checks.append(
            _check(
                "beam_judge_contract",
                judge_ok,
                reason="BEAM judge credential, endpoint, model, or declared RPM is missing",
                details={
                    "credential_env": judge_env,
                    "credential_set": bool(judge and judge.credential()),
                    "credential_shared_with_endpoint": bool(
                        judge and judge.credential() == service.credential()
                    ),
                    "access_path": None if judge is None else str(judge.source_path),
                    "base_url": env.get("NVST_BEAM_JUDGE_BASE_URL"),
                    "model": env.get("NVST_BEAM_JUDGE_MODEL"),
                    "requests_per_minute": judge_rpm,
                },
            )
        )

    deepswe = next((suite for suite in profile.suites if suite.adapter == "deepswe"), None)
    runtime_root: Path | None = None
    if deepswe is not None:
        config = deepswe.config
        runtime_root = Path(str(config.get("runtime_root", "")))
        preflight = verify_runtime(
            runtime_root=str(config.get("runtime_root", "")),
            expected_commit=str(config.get("expected_deep_swe_commit", "")),
            expected_pier_version=str(config.get("expected_pier_version", "")),
            expected_tasks=int(config.get("expected_tasks", 0)),
            pier_bin=str(config.get("pier_bin", "pier")),
        )
        checks.append(
            _check(
                "deepswe_runtime",
                preflight.get("status") == "passed",
                reason=str(preflight.get("reason") or "deepswe preflight failed"),
                details={
                    "expected": preflight.get("expected") or {},
                    "observed": preflight.get("observed") or {},
                    "mismatches": preflight.get("mismatches") or {},
                },
            )
        )

    if online_canary:
        checks.extend(
            _online_checks(
                service=service,
                judge=judge,
                root=root,
                runtime_root=runtime_root,
                capacity_probe=capacity_probe,
            )
        )
    blocked = [item for item in checks if item["status"] == "blocked"]
    result = {
        "schema_version": 1,
        "model": model_id,
        "status": "passed" if not blocked else "blocked",
        "zero_vendor_traffic": not online_canary,
        "profile_hashes": profile.content_hashes(),
        "checks": checks,
        "blocked_checks": [item["name"] for item in blocked],
    }
    schema_error = validation_error(result, "doctor-result.schema.json")
    if schema_error:
        raise RuntimeError(f"invalid doctor result: {schema_error}")
    return result
