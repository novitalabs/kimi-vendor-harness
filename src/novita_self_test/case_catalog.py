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
from jsonschema.exceptions import SchemaError

from .config import ConfigError, canonical_hash, find_secret, safe_path, workspace_root


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("configuration_invalid", f"cannot read {path}") from exc
    if not isinstance(value, dict):
        raise ConfigError("configuration_invalid", f"expected a mapping: {path}")
    return value


# Reviewed literals inside the imported Case A history, not credential values.
FIXTURE_PUBLIC_TEXT = frozenset({
    'API_KEY = os.environ.get("MINIMAX_API_KEY")',
    'Password: https://accounts.zoho.eu/home#security/apppasswords',
    'Password: https://myaccount.google.com/apppasswords',
    'password: [REDACTED_SECRET]',
})


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
    allowed = FIXTURE_PUBLIC_TEXT if step["request_fixture"] == "capabilities/conversation_state/fixtures/case-a-invalid-arguments.jsonl" else frozenset()
    if secret := find_secret(request, allowed_text=allowed):
        raise ConfigError("credential_material_forbidden", f"{path}: {secret}")
    if "request" in request:
        if not isinstance(request["request"], dict):
            raise ConfigError("case_invalid", "fixture.request must be an object")
        return request["request"]
    if "_repeat_run" in request:
        # Imported incident records wrap an API request in source metadata.
        metadata = {"id", "created_at", "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens", "msg_count", "_repeat_run", "forbidden_arguments"}
        return {key: value for key, value in request.items() if key not in metadata}
    return request


def _validate_expectation(schema: Any) -> None:
    """Oracles are local-only; never retrieve a remote JSON Schema reference."""
    if isinstance(schema, dict):
        for keyword in ("$ref", "$dynamicRef"):
            if keyword in schema and not str(schema[keyword]).startswith("#"):
                raise ConfigError("case_invalid", f"oracle schemas cannot use external {keyword}")
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
    except (OSError, ValueError, SchemaError) as exc:
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
                if not isinstance(request.get("messages"), list) or not request["messages"]:
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
                        except SchemaError as exc:
                            raise ConfigError("case_invalid", f"{case['id']}: invalid oracle schema") from exc
            if secret := find_secret(case):
                raise ConfigError("credential_material_forbidden", f"{case['id']}: {secret}")
            case["file"] = path.relative_to(root).as_posix()
            case.setdefault("scope", "endpoint")
            case["request_budget"] = sum(1 for _ in iter_case_steps(case, 1))
            cases.append(case)
    if not cases:
        raise ConfigError("case_invalid", "no endpoint cases found")
    return cases


def load_profile(model: str, root: Path | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", model):
        raise ConfigError("profile_invalid", "invalid model profile name")
    root = root or workspace_root()
    profile = read_yaml(root / "profiles" / f"{model}.yaml")
    try:
        schema = json.loads((root / "schemas/capability-profile.schema.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError("profile_invalid", "cannot read capability profile schema") from exc
    errors = list(Draft202012Validator(schema).iter_errors(profile))
    if errors or profile.get("model_id") != model:
        detail = errors[0].message if errors else "model_id mismatch"
        raise ConfigError("profile_invalid", f"{model}: {detail}")
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


def iter_case_steps(case: dict[str, Any], repeat: int):
    """Yield exactly the request units the runner will execute."""
    if case.get("scope", "endpoint") != "endpoint":
        return
    for repeat_index in range(repeat):
        for group, mode in enumerate(case["modes"]):
            for index, step in enumerate(case["steps"]):
                yield repeat_index, group, index, step.get("mode", mode), step


def request_features(request: dict[str, Any]) -> set[str]:
    """Features needed for a successful request, not a negative rejection probe."""
    required: set[str] = set()
    choice = request.get("tool_choice")
    if choice in ("none", "auto", "required"):
        required.add(f"tool_call.choice.{choice}")
    elif isinstance(choice, dict):
        required.add("tool_call.choice.named")
    dynamic_tools = [message.get("tools") for message in request.get("messages", []) if isinstance(message, dict) and message.get("tools")]
    if dynamic_tools:
        required.add("tool_call.dynamic_tools")
        if any(tool.get("function", {}).get("parameters") for tools in dynamic_tools for tool in tools if isinstance(tool, dict)):
            required.add("tool_call.schema")
    if request.get("tools"):
        required.add("tool_call")
        if any(tool.get("function", {}).get("parameters") for tool in request["tools"] if isinstance(tool, dict)):
            required.add("tool_call.schema")
    if any(key in request for key in ("reasoning_effort", "thinking")):
        required.add("reasoning")
    if any(isinstance(message.get("content"), list) for message in request.get("messages", []) if isinstance(message, dict)):
        required.add("multimodal")
    response_format = request.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in {"text", "json_object", "json_schema"}:
        required.add(f"structured_output.{response_format['type']}")
    if request.get("reasoning_effort") == "none" or (isinstance(request.get("thinking"), dict) and request["thinking"].get("type") == "disabled"):
        required.add("reasoning.disable")
    return required


def build_plan(cases: list[dict[str, Any]], *, model: str, repeat: int = 1,
               root: Path | None = None) -> dict[str, Any]:
    if type(repeat) is not int or repeat < 1:
        raise ConfigError("plan_invalid", "repeat must be a positive integer")
    root = root or workspace_root()
    profile = load_profile(model, root)
    supported = set(profile["features"])
    entries = []
    for original in cases:
        case = copy.deepcopy(original)
        required = set(case.get("requires", [])) | {case["capability"].split("/", 1)[0]}
        missing: list[str] = []
        for _, _, _, mode, step in iter_case_steps(case, 1):
            request = read_request(step, root)
            if mode == "stream":
                required.add("streaming")
            # A rejection probe must still run when the malformed feature is not supported.
            if step["expect"]["status"] != 200:
                continue
            required.update(request_features(request))
            thinking = request.get("thinking")
            effort = thinking.get("effort") if isinstance(thinking, dict) else None
            if step.get("thinking") is False:
                required.add("reasoning.disable")
            if "thinking" in step or "reasoning_effort" in request or isinstance(thinking, dict):
                required.add("reasoning")
            candidates = [step.get("effort"), request.get("reasoning_effort"), effort]
            for value in candidates:
                if value and value != "none" and value not in profile["efforts"]:
                    missing.append(f"effort:{value}")
        missing.extend(f"feature:{value}" for value in sorted(required - supported))
        case["required_features"] = sorted(required)
        case.update(missing_requirements=sorted(set(missing)),
                    planned_requests=0 if missing else sum(1 for _ in iter_case_steps(case, repeat)))
        entries.append(case)
    plan = {"schema_version": 1, "model": model, "profile_status": profile["status"],
            "profile_notes": profile.get("notes", []), "repeat": repeat,
            "enabled_features": sorted(supported), "concurrency": 1, "retries": 0,
            "request_budget": sum(case["planned_requests"] for case in entries),
            "max_output_tokens_budget": sum(case["planned_requests"] * case["max_output_tokens"] for case in entries),
            "profile_sha256": canonical_hash(profile), "cases": entries}
    plan["plan_sha256"] = canonical_hash(plan)
    return plan
