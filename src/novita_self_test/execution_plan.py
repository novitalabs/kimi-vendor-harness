"""Frozen plans detect local drift; hashes are integrity checks, not authorization."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

from . import __version__
from .case_catalog import build_plan, load_cases, load_profile, select_cases
from .capability_runtime import MAX_RESPONSE_BYTES, prepare_steps, token_parameter
from .config import ConfigError, ServiceAccess, canonical_hash, redact, safe_path, strict_json, workspace_root
from .contracts import validate_contract

NETWORK_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
               "SSL_CERT_FILE", "SSL_CERT_DIR")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def producer_identity(root: Path) -> dict[str, str]:
    files = [*sorted((root / "src/novita_self_test").glob("*.py")),
             *sorted((root / "schemas").glob("*.schema.json")), root / "pyproject.toml", root / "uv.lock"]
    return {"version": __version__, "source_sha256": canonical_hash({path.relative_to(root).as_posix(): file_hash(path) for path in files})}


def _inputs(cases: list[dict], model: str, root: Path) -> dict[str, str]:
    paths = {f"profiles/{model}.yaml", "sources/provenance.yaml"}
    for case in cases:
        paths.add(case["file"])
        for step in case["steps"]:
            if "request_fixture" in step:
                paths.add(step["request_fixture"])
    return {relative: file_hash(safe_path(root, relative)) for relative in sorted(paths)}


def execution_config(*, max_requests: int, timeout: float) -> dict:
    if type(max_requests) is not int or max_requests < 0:
        raise ConfigError("request_budget_invalid", "max_requests must be a nonnegative integer")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("timeout_invalid", "timeout must be finite and positive")
    return {"token_parameter": token_parameter(), "timeout_seconds": timeout,
            "max_response_bytes": MAX_RESPONSE_BYTES, "max_requests": max_requests,
            "network_env_sha256": canonical_hash({name: os.environ.get(name) for name in NETWORK_ENV})}


def _unit_hashes(prepared: list[tuple]) -> list[dict]:
    return [{"id": case["id"], "repeat": repeat, "group": group, "step": index, "mode": mode,
             "request_sha256": canonical_hash(request), "expect_sha256": canonical_hash(expect),
             "binding_sha256": canonical_hash(step.get("continue_from"))}
            for case, repeat, group, index, mode, step, request, expect in prepared]


def _seal(plan: dict) -> dict:
    plan.pop("plan_sha256", None)
    plan["plan_sha256"] = canonical_hash(plan)
    validate_contract(plan, "plan.schema.json")
    return plan


def create_plan(*, model: str, selectors: list[str] | None = None, repeat: int = 1,
                service: ServiceAccess | None = None, max_requests: int = 32, timeout: float = 120,
                root: Path | None = None) -> dict:
    root = (root or workspace_root()).resolve()
    load_profile(model, root)
    selected = select_cases(load_cases(root), selectors)
    return _freeze(selected, model=model, repeat=repeat, service=service,
                   max_requests=max_requests, timeout=timeout, root=root)[0]


def _freeze(cases: list[dict], *, model: str, repeat: int, service: ServiceAccess | None,
            max_requests: int, timeout: float, root: Path) -> tuple[dict, list]:
    config = execution_config(max_requests=max_requests, timeout=timeout)
    # Hash before and after resolving fixtures so the plan never approves mixed bytes.
    before = _inputs(cases, model, root)
    producer = producer_identity(root)
    current_cases = {case["id"]: case for case in load_cases(root)}
    if any(case["id"] not in current_cases or current_cases[case["id"]] != case for case in cases):
        raise ConfigError("plan_drift", "selected case snapshots changed before freezing; create a new plan")
    plan = build_plan(cases, model=model, repeat=repeat, root=root)
    if service is not None and service.model_family != model:
        raise ConfigError("model_mismatch", "service model_family does not match plan")
    if service is not None and plan["request_budget"] > max_requests:
        raise ConfigError("request_budget_exceeded", f"plan needs {plan['request_budget']} requests; ceiling is {max_requests}")
    prepared = prepare_steps(plan, service, root) if service is not None else []
    if before != _inputs(cases, model, root) or producer != producer_identity(root):
        raise ConfigError("plan_drift", "plan inputs changed during preparation; create a new plan")
    plan.update(schema_version=3, kind="endpoint_plan", status="ready", producer=producer, inputs=before,
                target=service.public_dict() if service else None,
                executable=service is not None and not any(value.endswith(":unknown") for case in plan["cases"] for value in case["missing_requirements"]),
                execution=config, units=_unit_hashes(prepared),
                opt_in_cases=[case["id"] for case in plan["cases"] if "opt_in" in case["tags"] and not case["missing_requirements"]])
    if redact(plan) != plan or (service and service.credential() and redact(plan, service.credential()) != plan):
        raise ConfigError("plan_material_forbidden", "plan contains credential-like text; sanitize case or access metadata")
    return _seal(plan), prepared


def load_plan(path: Path) -> dict:
    try:
        plan = strict_json(path.read_text(encoding="utf-8"))
        validate_contract(plan, "plan.schema.json")
    except (OSError, ValueError, ConfigError) as exc:
        raise ConfigError("plan_invalid", "cannot read a valid frozen plan") from exc
    return plan


def verify_plan(plan: dict, service: ServiceAccess, *, root: Path | None = None) -> list:
    root = (root or workspace_root()).resolve()
    try:
        validate_contract(plan, "plan.schema.json")
        unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if canonical_hash(unsigned) != plan["plan_sha256"]:
            raise ConfigError("plan_integrity_invalid", "plan hash differs from its content")
        if plan["target"] is None:
            raise ConfigError("plan_target_required", "discovery-only plan cannot execute; plan again with --service-access")
        if not plan["executable"]:
            raise ConfigError("contract_unknown", "selected target requirements are unverified; resolve authoritative evidence before execution")
        if plan["target"] != service.public_dict():
            raise ConfigError("plan_target_mismatch", "public target identity changed; create a new plan")
        if plan["producer"] != producer_identity(root):
            raise ConfigError("plan_drift", "harness implementation or schemas changed; create a new plan")
        for path, digest in plan["inputs"].items():
            if file_hash(safe_path(root, path)) != digest:
                raise ConfigError("plan_drift", "case/profile/fixture inputs changed; create a new plan")
        available = {case["id"]: case for case in load_cases(root)}
        ids = [case["id"] for case in plan["cases"]]
        if len(set(ids)) != len(ids) or any(id not in available for id in ids):
            raise ConfigError("plan_drift", "selected case ids changed")
        current, prepared = _freeze([available[id] for id in ids], model=plan["model"], repeat=plan["repeat"],
                                    service=service, root=root, max_requests=plan["execution"]["max_requests"],
                                    timeout=plan["execution"]["timeout_seconds"])
        if current != plan:
            raise ConfigError("plan_drift", "frozen requests, assertions, applicability or execution configuration changed; create a new plan")
        return prepared
    except ConfigError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError("plan_drift", "cannot verify frozen plan inputs; create a new plan") from exc
