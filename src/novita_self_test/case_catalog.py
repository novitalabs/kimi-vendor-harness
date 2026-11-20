"""Discover endpoint cases once, validate their contracts, and build bounded plans."""
from __future__ import annotations

import copy
import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .config import ConfigError, canonical_hash, find_secret, safe_path, workspace_root


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("configuration_invalid", f"cannot read {path}") from exc
    if not isinstance(value, dict):
        raise ConfigError("configuration_invalid", f"expected a mapping: {path}")
    return value


def read_request(step: dict[str, Any], root: Path) -> dict[str, Any]:
    if "request_fixture" not in step:
        return copy.deepcopy(step["request"])
    path = safe_path(root, step["request_fixture"])
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("case_invalid", f"cannot read request fixture: {path}") from exc
    if not isinstance(request, dict):
        raise ConfigError("case_invalid", f"request fixture must contain an object: {path}")
    return request


def _validate_expectation(schema: Any) -> None:
    """Oracles are local-only; never retrieve a remote JSON Schema reference."""
    if isinstance(schema, dict):
        if "$ref" in schema and not str(schema["$ref"]).startswith("#"):
            raise ConfigError("case_invalid", "oracle schemas cannot use external $ref")
        for value in schema.values():
            _validate_expectation(value)
    elif isinstance(schema, list):
        for value in schema:
            _validate_expectation(value)


def load_cases(root: Path | None = None) -> list[dict[str, Any]]:
    root = (root or workspace_root()).resolve()
    try:
        schema = json.loads((root / "schemas/capability-case.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError) as exc:
        raise ConfigError("case_invalid", "invalid capability case schema") from exc
    validator = Draft202012Validator(schema)
    sources = read_yaml(root / "sources/provenance.yaml").get("sources", {})
    cases, seen = [], set()
    for path in sorted((root / "capabilities").rglob("cases.jsonl")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except ValueError as exc:
                raise ConfigError("case_invalid", f"{path}:{line_number}: invalid JSON") from exc
            errors = list(validator.iter_errors(case))
            if errors:
                raise ConfigError("case_invalid", f"{path}:{line_number}: {errors[0].message}")
            if case["id"] in seen:
                raise ConfigError("case_invalid", f"duplicate case id: {case['id']}")
            seen.add(case["id"])
            if case["source"]["id"] not in sources:
                raise ConfigError("case_invalid", f"unknown source for {case['id']}")
            if case["capability"] != path.parent.relative_to(root / "capabilities").as_posix():
                raise ConfigError("case_invalid", f"{case['id']}: capability must match directory")
            for link in case["knowledge"]:
                if not safe_path(root, link).is_file():
                    raise ConfigError("case_invalid", f"{case['id']}: missing knowledge {link}")
            for index, step in enumerate(case["steps"]):
                request = read_request(step, root)
                if case.get("scope", "endpoint") == "endpoint" and (not isinstance(request.get("messages"), list) or not request["messages"]):
                    raise ConfigError("case_invalid", f"{case['id']}: messages must be nonempty")
                if any(key in request for key in ("model", "stream", "max_tokens", "max_completion_tokens")):
                    raise ConfigError("case_invalid", f"{case['id']}: model/stream/token cap belong to the runner")
                if "request_fixture" not in step and (secret := find_secret(request)):
                    raise ConfigError("credential_material_forbidden", f"{case['id']}: {secret}")
                expect = step["expect"]
                if "same_prompt_as" in expect and expect["same_prompt_as"] >= index:
                    raise ConfigError("case_invalid", "same_prompt_as must refer to an earlier step")
                if "minimum_prompt_delta" in expect and not index:
                    raise ConfigError("case_invalid", "prompt delta needs a previous step")
                for key in ("content_schema", "arguments_schema"):
                    if key in expect:
                        _validate_expectation(expect[key])
                        try:
                            Draft202012Validator.check_schema(expect[key])
                        except ValueError as exc:
                            raise ConfigError("case_invalid", f"{case['id']}: invalid oracle schema") from exc
            if secret := find_secret(case):
                raise ConfigError("credential_material_forbidden", f"{case['id']}: {secret}")
            case["file"] = path.relative_to(root).as_posix()
            case.setdefault("scope", "endpoint")
            case["request_budget"] = 0 if case["scope"] != "endpoint" else len(case["steps"]) * len(case["modes"])
            cases.append(case)
    if not cases:
        raise ConfigError("case_invalid", "no endpoint cases found")
    return cases


def load_profile(model: str, root: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", model):
        raise ConfigError("profile_invalid", "invalid model profile name")
    profile = read_yaml((root or workspace_root()) / "profiles" / f"{model}.yaml")
    if (profile.get("model_id") != model or profile.get("status") not in {"ready", "provisional"}
            or not isinstance(profile.get("features"), list)
            or not all(isinstance(item, str) for item in profile["features"])):
        raise ConfigError("profile_invalid", f"invalid capability profile: {model}")
    return profile


def matches(case: dict[str, Any], selector: str) -> bool:
    for prefix, key in (("tag:", "tags"), ("component:", "likely_components")):
        if selector.startswith(prefix):
            return selector[len(prefix):] in case[key]
    if selector == "smoke":
        return "smoke" in case["tags"]
    return (selector == "all" or case["id"] == selector
            or case["capability"] == selector.rstrip("/")
            or case["capability"].startswith(selector.rstrip("/") + "/")
            or fnmatch.fnmatchcase(case["id"], selector)
            or fnmatch.fnmatchcase(case["capability"], selector))


def select_cases(cases: list[dict[str, Any]], selectors: list[str] | None) -> list[dict[str, Any]]:
    selected, seen = [], set()
    for selector in selectors or ["smoke"]:
        hits = [case for case in cases if matches(case, selector)]
        if not hits:
            raise ConfigError("selector_not_found", f"selector matched no case: {selector}")
        for case in hits:
            if case["id"] not in seen:
                selected.append(case)
                seen.add(case["id"])
    return selected


def build_plan(cases: list[dict[str, Any]], *, model: str, repeat: int = 1,
               enabled_features: list[str] | None = None, root: Path | None = None) -> dict[str, Any]:
    if type(repeat) is not int or repeat < 1:
        raise ConfigError("plan_invalid", "repeat must be a positive integer")
    root = root or workspace_root()
    profile = load_profile(model, root)
    supported = set(profile["features"]) | set(enabled_features or [])
    entries = []
    for original in cases:
        case = copy.deepcopy(original)
        missing = sorted(set(case.get("requires", [])) - supported)
        efforts = profile.get("efforts", ["low", "high", "max"])
        for step in case["steps"]:
            effort = step.get("effort") or read_request(step, root).get("reasoning_effort")
            if effort and effort != "none" and effort not in efforts:
                missing.append(f"effort:{effort}")
        case.update(missing_requirements=sorted(set(missing)),
                    planned_requests=0 if missing else case["request_budget"] * repeat)
        entries.append(case)
    plan = {"schema_version": 1, "model": model, "profile_status": profile["status"],
            "profile_notes": profile.get("notes", []), "repeat": repeat,
            "enabled_features": sorted(supported), "concurrency": 1, "retries": 0,
            "request_budget": sum(case["planned_requests"] for case in entries),
            "max_output_tokens_budget": sum(case["planned_requests"] * case["max_output_tokens"] for case in entries),
            "profile_sha256": canonical_hash(profile), "cases": entries}
    plan["plan_sha256"] = canonical_hash(plan)
    return plan
