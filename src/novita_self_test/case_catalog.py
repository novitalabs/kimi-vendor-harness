"""Discover endpoint cases once, validate their contracts, and build bounded plans."""
from __future__ import annotations

import copy
import difflib
import fnmatch
import json
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .config import ConfigError, canonical_hash, find_secret, safe_path, strict_json, workspace_root
from .target_contract import applicability
from .case_semantics import validate_role, validate_step
from .contracts import issue


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
        request = strict_json(path.read_text(encoding="utf-8"))
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


def _validate_expectation(schema: Any, path: list[str | int] | None = None) -> None:
    """Visit schema locations, not data in examples/const/enum; never fetch refs."""
    path = path or []
    if not isinstance(schema, dict):
        return
    for keyword in ("$ref", "$dynamicRef"):
        if keyword in schema and not str(schema[keyword]).startswith("#"):
            raise ConfigError("case_invalid", f"oracle schemas cannot use external {keyword}", path=[*path, keyword])
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        for name, value in schema.get(key, {}).items():
            _validate_expectation(value, [*path, key, name])
    for key in ("items", "contains", "additionalProperties", "unevaluatedProperties", "propertyNames", "not", "if", "then", "else", "unevaluatedItems", "additionalItems"):
        _validate_expectation(schema.get(key), [*path, key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for index, value in enumerate(schema.get(key, [])):
            _validate_expectation(value, [*path, key, index])


def _validate_case(case: dict, path: Path, root: Path, sources: dict) -> None:
    if case["source"]["id"] not in sources:
        raise ConfigError("case_invalid", f"unknown source for {case['id']}", path=["source", "id"])
    if case["capability"] != path.parent.relative_to(root / "capabilities").as_posix():
        raise ConfigError("case_invalid", "capability must match directory", path=["capability"])
    for index, link in enumerate(case["knowledge"]):
        if not safe_path(root, link).is_file():
            raise ConfigError("case_invalid", f"missing knowledge {link}", path=["knowledge", index])
    validate_role(case)
    for index, step in enumerate(case["steps"]):
        try:
            request = read_request(step, root)
            if not isinstance(request.get("messages"), list) or not request["messages"]:
                raise ConfigError("case_invalid", "messages must be nonempty", path=["request", "messages"])
            if any(key in request for key in ("model", "stream", "max_tokens", "max_completion_tokens")):
                raise ConfigError("case_invalid", "model/stream/token cap belong to the runner", path=["request"])
            validate_step(step, request)
            expect = step["expect"]
            if "continue_from" in step:
                binding = step["continue_from"]
                if binding["step"] >= index or case["steps"][binding["step"]]["expect"]["status"] != 200:
                    raise ConfigError("case_invalid", "continuation must reference a previous successful step", path=["continue_from", "step"])
                if len({row["name"] for row in binding["tool_results"]}) != len(binding["tool_results"]):
                    raise ConfigError("case_invalid", "canned tool result names must be unique", path=["continue_from", "tool_results"])
            if "same_prompt_as" in expect and expect["same_prompt_as"] >= index:
                raise ConfigError("case_invalid", "same_prompt_as must refer to an earlier step", path=["expect", "same_prompt_as"])
            if "minimum_prompt_delta" in expect and not index:
                raise ConfigError("case_invalid", "prompt delta needs a previous step", path=["expect", "minimum_prompt_delta"])
            for key in ("content_schema", "arguments_schema"):
                if key in expect:
                    try:
                        Draft202012Validator.check_schema(expect[key])
                        _validate_expectation(expect[key], ["expect", key])
                    except SchemaError as exc:
                        raise ConfigError("case_invalid", "invalid oracle schema", path=["expect", key, *exc.path]) from exc
        except ConfigError as exc:
            raise ConfigError(exc.code, str(exc), path=["steps", index, *exc.path]) from exc
    if secret := find_secret(case):
        raise ConfigError("credential_material_forbidden", secret)


def _scan_cases(root: Path, files: list[str] | None = None, ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    try:
        schema = strict_json((root / "schemas/capability-case.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, SchemaError) as exc:
        raise ConfigError("case_invalid", "invalid capability case schema") from exc
    validator = Draft202012Validator(schema)
    sources = read_yaml(root / "sources/provenance.yaml").get("sources", {})
    paths = sorted({safe_path(root, file) for file in files}) if files else sorted((root / "capabilities").rglob("cases.jsonl"))
    cases, diagnostics, seen, found = [], [], set(), set()
    for path in paths:
        if not path.is_relative_to(root / "capabilities") or path.name != "cases.jsonl":
            raise ConfigError("path_invalid", "lint files must be capability-owned cases.jsonl")
        relative = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeError) as exc:
            diagnostics.append(issue("case_invalid", f"cannot read {relative}: {type(exc).__name__}", path=[relative]))
            continue
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                case = strict_json(line)
                case_id = case.get("id") if isinstance(case, dict) else None
                if ids and case_id not in ids:
                    continue
                if isinstance(case_id, str):
                    found.add(case_id)
                errors = list(validator.iter_errors(case))
                if errors:
                    diagnostics.extend(issue("case_invalid", error.message, path=[relative, line_number, *error.absolute_path]) for error in errors)
                    continue
                if case_id in seen:
                    raise ConfigError("case_invalid", f"duplicate case id: {case_id}", path=["id"])
                seen.add(case_id)
                _validate_case(case, path, root, sources)
                case.update(file=relative, line=line_number)
                case.setdefault("scope", "endpoint")
                case["request_budget"] = sum(1 for _ in iter_case_steps(case, 1))
                cases.append(case)
            except ConfigError as exc:
                diagnostics.append(issue(exc.code, str(exc), path=[relative, line_number, *exc.path]))
            except ValueError:
                diagnostics.append(issue("case_invalid", "invalid JSON", path=[relative, line_number]))
    for id in sorted(set(ids or []) - found):
        diagnostics.append(issue("selector_not_found", f"case id not found in lint scope: {id}", actual=id))
    if not cases and not diagnostics:
        diagnostics.append(issue("case_invalid", "no endpoint cases found"))
    return cases, diagnostics


def load_cases(root: Path | None = None) -> list[dict[str, Any]]:
    cases, diagnostics = _scan_cases((root or workspace_root()).resolve())
    if diagnostics:
        problem = diagnostics[0]
        raise ConfigError(problem["code"], problem["message"], path=problem["path"])
    return cases


def lint_cases(*, files: list[str] | None = None, ids: list[str] | None = None, root: Path | None = None) -> dict:
    cases, diagnostics = _scan_cases((root or workspace_root()).resolve(), files, ids)
    return {"schema_version": 1, "kind": "case_lint", "status": "failed" if diagnostics else "passed",
            "endpoint_requests": 0, "scope": {"files": files or [], "ids": ids or [], "full_catalog": not files and not ids},
            "cases": [{"id": case["id"], "file": case["file"], "line": case["line"]} for case in cases],
            "problems": diagnostics, "warning": "Lint validates only its declared scope, not endpoint behavior or execution permission. Full plans still validate the catalog."}


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
    for prefix, key in (("role:", "role"), ("stage:", "stage")):
        if selector.startswith(prefix):
            return selector[len(prefix):] == case[key]
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


def selector_values(cases: list[dict]) -> list[str]:
    values = {"all", "smoke"}
    for case in cases:
        values.update((case["id"], case["capability"], "role:" + case["role"], "stage:" + case["stage"]))
        values.update("tag:" + tag for tag in case["tags"])
        values.update("component:" + component for component in case["likely_components"])
    return sorted(values)


def select_cases(cases: list[dict[str, Any]], selectors: list[str] | None, *,
                 filters: list[str] | None = None, excludes: list[str] | None = None) -> list[dict[str, Any]]:
    values = selector_values(cases)
    for selector in [*(selectors or ["smoke"]), *(filters or []), *(excludes or [])]:
        if not any(matches(case, selector) for case in cases):
            suggestions = difflib.get_close_matches(selector, values, n=5, cutoff=.5)
            raise ConfigError("selector_not_found", f"selector matched no case: {selector}",
                              actual=selector, expected={"suggestions": suggestions, "valid_values": values}, path=["select"])
    selected = [case for case in cases if any(matches(case, selector) for selector in selectors or ["smoke"])]
    # Preserve user-selected order while refinements only narrow the union.
    selected.sort(key=lambda case: next(i for i, selector in enumerate(selectors or ["smoke"]) if matches(case, selector)))
    selected = [case for case in selected if all(matches(case, selector) for selector in filters or [])
                and not any(matches(case, selector) for selector in excludes or [])]
    if not selected:
        raise ConfigError("selection_empty", "selection/refinement matched no cases; no plan generated")
    return selected


def catalog_view(plan: dict) -> dict:
    keys = ("id", "title", "capability", "file", "line", "role", "stage", "determinism", "obligation", "tags", "modes",
            "planned_requests", "max_output_tokens", "required_features", "missing_requirements", "source", "knowledge", "likely_components")
    return {"schema_version": 1, "kind": "case_catalog", "status": "ready", "endpoint_requests": 0,
            "model": plan["model"], "request_budget": plan["request_budget"], "max_output_tokens_budget": plan["max_output_tokens_budget"],
            "cases": [{key: case[key] for key in keys} for case in plan["cases"]],
            "selectors": selector_values(plan["cases"]), "warning": "Discovery view, not an executable frozen plan or authorization."}


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
    if any(isinstance(part, dict) and part.get("type") != "text"
           for message in request.get("messages", []) if isinstance(message, dict) and isinstance(message.get("content"), list)
           for part in message["content"]):
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
    supported = {name for name, rule in profile["features"].items() if rule["expectation"] in {"required", "optional"}}
    entries = []
    for original in cases:
        case = copy.deepcopy(original)
        required = set(case.get("requires", [])) | {case["capability"].split("/", 1)[0]}
        efforts: set[str] = set()
        for _, _, _, mode, step in iter_case_steps(case, 1):
            request = read_request(step, root)
            validate_step(step, request)
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
                if value and value != "none":
                    efforts.add(value)
        missing, obligation = applicability(profile, required, efforts)
        case["required_features"] = sorted(required)
        case["obligation"] = obligation
        case.update(missing_requirements=sorted(set(missing)),
                    planned_requests=0 if missing else repeat * sum(1 for _ in iter_case_steps(case, 1)))
        entries.append(case)
    plan = {"schema_version": 1, "model": model, "profile_status": profile["status"],
            "profile_notes": profile.get("notes", []), "contract_authority": profile["authority"], "repeat": repeat,
            "enabled_features": sorted(supported), "concurrency": 1, "retries": 0,
            "request_budget": sum(case["planned_requests"] for case in entries),
            "max_output_tokens_budget": sum(case["planned_requests"] * case["max_output_tokens"] for case in entries),
            "profile_sha256": canonical_hash(profile), "cases": entries}
    plan["plan_sha256"] = canonical_hash(plan)
    return plan
