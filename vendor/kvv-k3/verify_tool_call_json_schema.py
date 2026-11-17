"""Verify tool-call arguments against walle MFJS schema cases.

Every walle-valid schema is an MFJS schema that can define tool-call
`parameters`. This script sends selected schemas as `tools[].function.parameters`,
forces a tool call, and validates the returned `function.arguments` JSON with
jsonschema.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI


DEFAULT_CASE_DIR = (
    Path(__file__).parent
    / "testdata"
    / "walle_validator_cases"
    / "validator_cases"
)
DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
TOOL_NAME = "kvv_walle_case"
TOOL_KEYWORDS = (
    "tool",
    "function",
    "tool_call",
    "function_call",
    "arguments",
    "parameters",
)


@dataclass(frozen=True)
class ValidatorCase:
    suite: str
    kind: str
    line: int
    schema_text: str


REQUEST_MODES = ("non-stream", "stream")


@dataclass
class ToolCaseResult:
    suite: str
    line: int
    selection_reason: str
    status: str
    message: str
    mode: str = "non-stream"
    http_status: int | None = None
    error_type: str | None = None
    arguments: str | None = None


@dataclass
class ToolResponse:
    accepted: bool
    message: str
    arguments: str | None = None
    http_status: int | None = None
    error_type: str | None = None


def truncate(value: str, limit: int = 500) -> str:
    value = value.replace("\n", "\\n")
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def suite_dirs(case_dir: Path) -> list[Path]:
    return sorted(path for path in case_dir.iterdir() if path.is_dir())


def load_cases(case_dir: Path) -> list[ValidatorCase]:
    if not case_dir.is_dir():
        raise FileNotFoundError(f"case directory not found: {case_dir}")

    cases: list[ValidatorCase] = []
    for suite_path in suite_dirs(case_dir):
        valid_path = suite_path / "valid.jsonl"
        with valid_path.open(encoding="utf-8") as file:
            for line_number, raw_line in enumerate(file, start=1):
                schema_text = raw_line.strip()
                if not schema_text:
                    continue
                cases.append(
                    ValidatorCase(
                        suite=suite_path.name,
                        kind="valid",
                        line=line_number,
                        schema_text=schema_text,
                    )
                )
    return cases


def has_non_finite_number(value: Any) -> bool:
    if isinstance(value, float):
        return value != value or value in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return any(has_non_finite_number(item) for item in value)
    if isinstance(value, dict):
        return any(has_non_finite_number(item) for item in value.values())
    return False


def parse_for_transport(case: ValidatorCase) -> tuple[bool, Any | None, str]:
    try:
        schema = json.loads(case.schema_text)
    except json.JSONDecodeError as exc:
        return False, None, f"schema text is not valid JSON: {exc.msg}"

    if has_non_finite_number(schema):
        return False, None, "schema contains non-finite numeric value"

    return True, schema, ""


def make_client(base_url: str, api_key: str, timeout: float) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)


def error_message(exc: APIStatusError) -> str:
    parts = [str(exc)]
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parts.append(response.text)
        except Exception:
            pass
    return "\n".join(part for part in parts if part)


def case_id(case: ValidatorCase) -> str:
    return f"{case.suite}:{case.line}"


def schema_text_has_tool_keyword(schema: Any) -> bool:
    text = json.dumps(schema, ensure_ascii=False).lower()
    return any(keyword in text for keyword in TOOL_KEYWORDS)


def is_object_parameter_schema(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    return (
        schema_type == "object"
        or (isinstance(schema_type, list) and "object" in schema_type)
    ) and isinstance(schema.get("properties"), dict)


def rewrite_root_refs(obj: Any, target_ref: str) -> Any:
    """Rewrite JSON Schema root self-references to *target_ref*."""
    if isinstance(obj, dict):
        rewritten: dict[str, Any] = {}
        for key, value in obj.items():
            if key == "$ref" and value == "#":
                rewritten[key] = target_ref
            else:
                rewritten[key] = rewrite_root_refs(value, target_ref)
        return rewritten
    if isinstance(obj, list):
        return [rewrite_root_refs(item, target_ref) for item in obj]
    return obj


def wrap_schema_as_parameter_property(schema: Any) -> Any:
    """Use the case schema as one tool argument property schema."""
    if not isinstance(schema, dict):
        return schema

    wrapped: dict[str, Any] = {
        "type": "object",
        "required": ["value"],
        "additionalProperties": False,
    }

    if "#" in _collect_refs(schema):
        defs = dict(schema.get("$defs", {}))
        def_name = "__case_schema"
        while def_name in defs:
            def_name = f"_{def_name}"
        target_ref = f"#/$defs/{def_name}"
        rewritten = rewrite_root_refs(schema, target_ref)
        defs.update(rewritten.get("$defs", {}))
        defs[def_name] = {
            k: v for k, v in rewritten.items() if k not in ("$defs", "$id")
        }
        wrapped["properties"] = {"value": {"$ref": target_ref}}
        wrapped["$defs"] = defs
    else:
        wrapped["properties"] = {
            "value": {k: v for k, v in schema.items() if k not in ("$defs", "$id")}
        }
        if "$defs" in schema:
            wrapped["$defs"] = schema["$defs"]

    if "$id" in schema:
        wrapped["$id"] = schema["$id"]
    return wrapped


def _collect_refs(obj: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(obj, dict):
        if "$ref" in obj:
            refs.append(obj["$ref"])
        for v in obj.values():
            refs.extend(_collect_refs(v))
    elif isinstance(obj, list):
        for item in obj:
            refs.extend(_collect_refs(item))
    return refs


def _is_primitive_type(t: Any) -> bool:
    if isinstance(t, str):
        return t in ("string", "number", "integer", "boolean", "null")
    if isinstance(t, list):
        return any(_is_primitive_type(x) for x in t)
    return False


def _has_non_recursive_option(obj: Any) -> bool:
    """Return True if *obj* allows a value that does not recurse."""
    if isinstance(obj, dict):
        if _is_primitive_type(obj.get("type")):
            return True
        if "anyOf" in obj:
            return any(_has_non_recursive_option(s) for s in obj["anyOf"])
        if "oneOf" in obj:
            return any(_has_non_recursive_option(s) for s in obj["oneOf"])
        if "enum" in obj:
            return True
        for v in obj.values():
            if _has_non_recursive_option(v):
                return True
    elif isinstance(obj, list):
        return any(_has_non_recursive_option(item) for item in obj)
    return False


def has_recursive_ref_without_termination(schema: Any) -> bool:
    """Detect recursive $ref that cannot terminate (no escape + required)."""
    if not isinstance(schema, dict):
        return False
    defs = schema.get("$defs", {})
    for ref in _collect_refs(schema):
        if not ref.startswith("#/$defs/"):
            continue
        def_name = ref.split("/")[-1]
        if def_name not in defs:
            continue
        def_schema = defs[def_name]
        for inner_ref in _collect_refs(def_schema):
            if inner_ref != ref:
                continue
            # Self-reference found – check if there is a non-recursive way out.
            if _has_non_recursive_option(def_schema):
                continue
            # No escape route – check whether the recursive prop is required.
            required = set(def_schema.get("required", []))
            props = def_schema.get("properties", {})
            for prop_name, prop_schema in props.items():
                if ref in _collect_refs(prop_schema) and prop_name in required:
                    return True
    return False


def has_exotic_property_keys(schema: Any) -> bool:
    """Detect property keys known to confuse the decoder.

    Leading/trailing whitespace and empty-string keys are stripped by the
    decoder.  Literal escape sequences (e.g. ``\\n``, ``\\t``) in keys are
    also mishandled – walle test data double-escapes them while the decoder
    emits actual control chars, so the keys never match.
    """
    if not isinstance(schema, dict):
        return False
    for key in schema.get("properties", {}).keys():
        if key != key.strip():
            return True
        if key == "":
            return True
        if any(seq in key for seq in (r"\n", r"\t", r"\r", r"\b", r"\f")):
            return True
    return False


def strip_keyword_recursive(obj: Any, keyword: str) -> Any:
    """Recursively remove *keyword* from every dict in *obj*."""
    if isinstance(obj, dict):
        return {
            k: strip_keyword_recursive(v, keyword)
            for k, v in obj.items()
            if k != keyword
        }
    if isinstance(obj, list):
        return [strip_keyword_recursive(item, keyword) for item in obj]
    return obj


def schema_shape(schema: Any) -> str:
    if not isinstance(schema, dict):
        return type(schema).__name__
    if not schema:
        return "empty_parameter_schema"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return "union_parameter_schema"
    if isinstance(schema_type, str):
        return f"{schema_type}_parameter_schema"
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            return f"{keyword}_parameter_schema"
    if "$ref" in schema:
        return "ref_parameter_schema"
    return "schema_parameter_schema"


def classify_case(case: ValidatorCase) -> tuple[Any | None, str | None]:
    transportable, schema, reason = parse_for_transport(case)
    if not transportable:
        return None, f"unsupported_by_transport: {reason}"

    # 1. Check skip conditions on the *original* schema before wrapping
    #    changes its shape.
    if isinstance(schema, dict):
        if schema.get("type") == "string":
            min_len = schema.get("minLength")
            if min_len is not None and min_len > 1000:
                return None, "skipped_extreme_minlength_not_supported"
        if has_exotic_property_keys(schema):
            return None, "skipped_exotic_property_keys"
        if has_recursive_ref_without_termination(schema):
            return None, "skipped_recursive_ref"

    # 2. Detect tool-keyword schemas first (before wrapping changes text).
    has_tool_kw = schema_text_has_tool_keyword(schema)

    # 3. Keep the selection reason based on the original case schema.
    if has_tool_kw:
        selection_reason = "explicit_tool_keyword"
    elif is_object_parameter_schema(schema):
        selection_reason = "object_parameter_schema"
    else:
        selection_reason = schema_shape(schema)

    # 4. Use the case schema as a tool argument property schema where safe.
    schema = wrap_schema_as_parameter_property(schema)

    # 5. Strip ``default`` – it triggers decoder instability (observed on
    #    kimi-k2.5 where required properties are occasionally omitted).
    schema = strip_keyword_recursive(schema, "default")

    return schema, selection_reason


def parse_case_values(raw_cases: list[str] | None) -> set[str]:
    selected: set[str] = set()
    for raw in raw_cases or []:
        selected.update(part.strip() for part in raw.split(",") if part.strip())
    return selected


def select_cases(
    cases: list[ValidatorCase],
    *,
    selection: str,
    requested_cases: set[str],
    max_cases: int | None,
) -> list[tuple[ValidatorCase, Any, str]]:
    selected: list[tuple[ValidatorCase, Any, str]] = []
    case_map = {case_id(case): case for case in cases}

    if requested_cases:
        unknown = sorted(requested_cases - set(case_map))
        if unknown:
            raise ValueError(f"unknown case id(s): {', '.join(unknown)}")
        for selected_id in sorted(requested_cases):
            case = case_map[selected_id]
            schema, reason = classify_case(case)
            if schema is None:
                raise ValueError(f"case {selected_id} is not runnable: {reason}")
            selected.append((case, schema, "requested"))
        return selected

    for case in cases:
        schema, reason = classify_case(case)
        if schema is None:
            continue
        if selection == "explicit" and reason != "explicit_tool_keyword":
            continue
        if selection == "object" and reason != "object_parameter_schema":
            continue
        selected.append((case, schema, reason))
        if max_cases is not None and len(selected) >= max_cases:
            break

    return selected


def get_field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def extract_tool_arguments(completion: Any) -> tuple[str | None, str]:
    choices = get_field(completion, "choices") or []
    if not choices:
        return None, "response has no choices"

    message = get_field(choices[0], "message")
    if message is None:
        return None, "response first choice has no message"

    tool_calls = get_field(message, "tool_calls") or []
    if not tool_calls:
        content = get_field(message, "content")
        return (
            None,
            f"response did not include tool_calls; content={truncate(str(content or ''))}",
        )

    tool_call_names: list[str] = []
    for tool_call in tool_calls:
        function = get_field(tool_call, "function")
        name = get_field(function, "name")
        tool_call_names.append(str(name))
        if name != TOOL_NAME:
            continue

        arguments = get_field(function, "arguments")
        if arguments is None:
            return None, f"tool call {TOOL_NAME} did not include function.arguments"
        if isinstance(arguments, str):
            return arguments, f"tool call {TOOL_NAME} returned arguments"
        return json.dumps(arguments, ensure_ascii=False), (
            f"tool call {TOOL_NAME} returned non-string arguments"
        )

    return (
        None,
        f"response did not include {TOOL_NAME} tool call; "
        f"tool_calls={', '.join(tool_call_names)}",
    )


def reassemble_stream(stream: Any) -> tuple[str | None, str]:
    """Iterate streaming chunks and rebuild the target tool call.

    OpenAI-style streaming delivers ``tool_calls`` as deltas keyed by ``index``;
    ``function.name`` typically arrives first and ``function.arguments`` is
    concatenated across chunks. We reassemble per index, then look for the
    entry whose accumulated name equals ``TOOL_NAME``.
    """
    by_index: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    last_content = ""
    chunk_count = 0

    for chunk in stream:
        chunk_count += 1
        choices = get_field(chunk, "choices") or []
        if not choices:
            continue
        choice = choices[0]
        fr = get_field(choice, "finish_reason")
        if fr:
            finish_reason = fr
        delta = get_field(choice, "delta")
        if delta is None:
            continue
        content_delta = get_field(delta, "content")
        if isinstance(content_delta, str):
            last_content += content_delta
        for tc_delta in get_field(delta, "tool_calls") or []:
            idx = get_field(tc_delta, "index")
            if idx is None:
                idx = 0
            entry = by_index.setdefault(int(idx), {"name": "", "arguments": ""})
            function = get_field(tc_delta, "function")
            if function is None:
                continue
            name_delta = get_field(function, "name")
            if isinstance(name_delta, str):
                entry["name"] += name_delta
            args_delta = get_field(function, "arguments")
            if isinstance(args_delta, str):
                entry["arguments"] += args_delta

    if not by_index:
        return (
            None,
            f"streamed response had no tool_calls "
            f"(chunks={chunk_count}, finish_reason={finish_reason}, "
            f"content={truncate(last_content)})",
        )

    seen_names: list[str] = []
    for idx in sorted(by_index):
        entry = by_index[idx]
        seen_names.append(entry["name"])
        if entry["name"] == TOOL_NAME:
            return entry["arguments"], (
                f"streamed tool call {TOOL_NAME} reassembled from "
                f"{chunk_count} chunk(s)"
            )

    return (
        None,
        f"streamed response did not include {TOOL_NAME} tool call; "
        f"tool_calls={', '.join(seen_names)}",
    )


def send_tool_schema(
    client: OpenAI,
    model: str,
    schema: Any,
    max_tokens: int,
    thinking: bool,
    think_mode: str,
    *,
    stream: bool = False,
) -> ToolResponse:
    extra_body = get_thinking_extra_body(thinking, think_mode)
    tool_choice = "auto"
    if os.getenv("ENABLE_THINKING_EFFORT", "false") == "true":
        tool_choice = "required"
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Call the {TOOL_NAME} tool exactly once with minimum"
                    "runtime arguments that satisfy its parameter schema, "
                    "try your best to create the arguments. "
                    "Do not copy or describe the JSON Schema itself. Do not "
                    "include schema keywords like type, properties, required, "
                    "or additionalProperties unless the schema explicitly "
                    "requires them as argument property names. If the schema "
                    "defines a top-level value argument, provide the minimal "
                    "valid value for it. Always include every required "
                    "property. Respect minItems, minProperties, enum, const, minimum, and "
                    "minLength constraints. Prefer empty arrays and empty "
                    "objects only when those constraints allow them. Do not "
                    "answer with plain text."
                ),
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": TOOL_NAME,
                    "description": (
                        "Submit minimal JSON arguments that validate against "
                        "this JSON Schema."
                    ),
                    "parameters": schema,
                    "strict": True,
                },
            }
        ],
        # ENABLE_THINKING_EFFORT是给K3准备的，所以前有这个env就表明支持tool_choice required
        "tool_choice": tool_choice,
        "max_tokens": max_tokens,
    }
    if stream:
        request["stream"] = True
    if extra_body:
        request["extra_body"] = extra_body

    try:
        response = client.chat.completions.create(**request)
        if stream:
            arguments, message = reassemble_stream(response)
        else:
            arguments, message = extract_tool_arguments(response)
        return ToolResponse(accepted=True, message=message, arguments=arguments)
    except APIStatusError as exc:
        return ToolResponse(
            accepted=False,
            message=error_message(exc),
            http_status=exc.status_code,
            error_type=type(exc).__name__,
        )
    except (APIConnectionError, APITimeoutError, TimeoutError) as exc:
        return ToolResponse(
            accepted=False,
            message=str(exc),
            error_type=type(exc).__name__,
        )
    except Exception as exc:
        return ToolResponse(
            accepted=False,
            message=str(exc),
            error_type=type(exc).__name__,
        )


def validate_arguments(schema: Any, arguments: str | None) -> tuple[bool, str]:
    if arguments is None:
        return False, "tool call arguments are missing"

    try:
        instance = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return (
            False,
            "tool call arguments are not valid JSON: "
            f"{exc.msg}; arguments={truncate(arguments)}",
        )

    try:
        Draft202012Validator(schema).validate(instance)
    except SchemaError as exc:
        return False, f"local jsonschema rejected schema: {truncate(str(exc))}"
    except ValidationError as exc:
        return (
            False,
            "tool call arguments do not match schema: "
            f"{truncate(exc.message)}; arguments={truncate(arguments)}",
        )

    return True, f"tool call arguments matched schema; arguments={truncate(arguments)}"


def get_thinking_extra_body(thinking: bool, think_mode: str) -> dict[str, Any]:
    if think_mode == "none":
        return {}
    if think_mode == "opensource":
        return {"chat_template_kwargs": {"thinking": thinking}}
    return {"thinking": {"type": "enabled" if thinking else "disabled"}}


def evaluate_case(
    client: OpenAI,
    model: str,
    case: ValidatorCase,
    schema: Any,
    selection_reason: str,
    max_tokens: int,
    thinking: bool,
    think_mode: str,
    *,
    mode: str = "non-stream",
) -> ToolCaseResult:
    response = send_tool_schema(
        client,
        model,
        schema,
        max_tokens,
        thinking,
        think_mode,
        stream=mode == "stream",
    )
    if response.error_type and (
        response.http_status is None
        or response.http_status == 429
        or response.http_status >= 500
    ):
        return ToolCaseResult(
            suite=case.suite,
            line=case.line,
            selection_reason=selection_reason,
            status="infra_error",
            message=truncate(response.message),
            mode=mode,
            http_status=response.http_status,
            error_type=response.error_type,
        )

    if not response.accepted:
        return ToolCaseResult(
            suite=case.suite,
            line=case.line,
            selection_reason=selection_reason,
            status="failed",
            message=truncate(f"tool schema rejected: {response.message}"),
            mode=mode,
            http_status=response.http_status,
            error_type=response.error_type,
        )

    valid, message = validate_arguments(schema, response.arguments)
    return ToolCaseResult(
        suite=case.suite,
        line=case.line,
        selection_reason=selection_reason,
        status="passed" if valid else "failed",
        message=message,
        mode=mode,
        arguments=truncate(response.arguments or "", 1000),
    )


def result_sort_key(result: ToolCaseResult) -> tuple[str, int, str]:
    return (result.suite, result.line, result.mode)


def print_result(result: ToolCaseResult) -> None:
    label = result.status.upper()
    location = f"{result.suite}/valid.jsonl:{result.line}"
    mode_tag = f"[{result.mode}]"
    if result.status == "passed":
        print(f"[{label}] {mode_tag} {location} ({result.selection_reason})")
    else:
        print(
            f"[{label}] {mode_tag} {location} "
            f"({result.selection_reason}) - {result.message}"
        )


def summarize(results: list[ToolCaseResult]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_mode: dict[str, dict[str, int]] = {}
    for result in results:
        statuses[result.status] = statuses.get(result.status, 0) + 1
        by_reason[result.selection_reason] = (
            by_reason.get(result.selection_reason, 0) + 1
        )
        mode_stats = by_mode.setdefault(result.mode, {})
        mode_stats[result.status] = mode_stats.get(result.status, 0) + 1
    return {
        "total": len(results),
        "by_status": dict(sorted(statuses.items())),
        "by_selection_reason": dict(sorted(by_reason.items())),
        "by_mode": {
            mode: dict(sorted(stats.items()))
            for mode, stats in sorted(by_mode.items())
        },
    }


def write_report(
    path: Path,
    *,
    selected: list[tuple[ValidatorCase, Any, str]],
    results: list[ToolCaseResult],
    model: str,
    base_url: str,
    dry_run: bool,
    thinking: bool,
    think_mode: str,
    modes: tuple[str, ...],
) -> None:
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": model,
        "base_url": base_url,
        "tool_name": TOOL_NAME,
        "dry_run": dry_run,
        "thinking": thinking,
        "think_mode": think_mode,
        "modes": list(modes),
        "selected_cases": [
            {
                "suite": case.suite,
                "line": case.line,
                "selection_reason": reason,
                "schema": schema,
            }
            for case, schema, reason in selected
        ],
        "summary": summarize(results),
        "results": [asdict(result) for result in sorted(results, key=result_sort_key)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def run_cases(
    *,
    selected: list[tuple[ValidatorCase, Any, str]],
    client: OpenAI,
    model: str,
    max_tokens: int,
    thinking: bool,
    think_mode: str,
    max_connections: int,
    fail_fast: bool,
    modes: tuple[str, ...],
) -> list[ToolCaseResult]:
    work: list[tuple[ValidatorCase, Any, str, str]] = [
        (case, schema, reason, mode)
        for case, schema, reason in selected
        for mode in modes
    ]

    if fail_fast or max_connections <= 1:
        results = []
        for case, schema, reason, mode in work:
            result = evaluate_case(
                client,
                model,
                case,
                schema,
                reason,
                max_tokens,
                thinking,
                think_mode,
                mode=mode,
            )
            print_result(result)
            results.append(result)
            if fail_fast and result.status in {"failed", "infra_error"}:
                break
        return results

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_connections) as executor:
        futures = {
            executor.submit(
                evaluate_case,
                client,
                model,
                case,
                schema,
                reason,
                max_tokens,
                thinking,
                think_mode,
                mode=mode,
            ): (case, mode)
            for case, schema, reason, mode in work
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            print_result(result)
            results.append(result)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify tool-call arguments against walle MFJS JSON schemas."
        )
    )
    parser.add_argument("--model", required=True, help="Vendor model name")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KIMI_BASE_URL", DEFAULT_BASE_URL),
        help="API base URL (default: $KIMI_BASE_URL or Moonshot API)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KIMI_API_KEY"),
        help="API key (default: $KIMI_API_KEY)",
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=DEFAULT_CASE_DIR,
        help=f"walle validator_cases directory (default: {DEFAULT_CASE_DIR})",
    )
    parser.add_argument(
        "--selection",
        choices=("explicit", "object", "all"),
        default="all",
        help=(
            "Case selection mode: all MFJS schemas, explicit keyword matches, "
            "or object parameter schemas (default: all)"
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        help="Run a case id like TestRequired:5; may be repeated or comma-separated",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        help="Maximum number of selected cases to run",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="List selected cases without calling the vendor API",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=4,
        help="Maximum concurrent requests (default: 4)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2048,
        help="Maximum output tokens for each tool-call response (default: 2048)",
    )
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="Enable thinking mode for the tool-call request",
    )
    parser.add_argument(
        "--think-mode",
        choices=("kimi", "opensource", "none"),
        default=os.environ.get("THINK_MODE", "kimi"),
        help="Thinking param format (default: $THINK_MODE or kimi)",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop at the first failed or infra-error case",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=REQUEST_MODES,
        default=list(REQUEST_MODES),
        help=(
            "Request modes to run for each case; each enabled mode produces "
            "an independent result (default: both non-stream and stream)"
        ),
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        help="Write full JSON report to this path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load and select cases without calling the vendor API",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    modes: tuple[str, ...] = tuple(dict.fromkeys(args.modes))
    if not modes:
        print("Error: --modes must enable at least one mode", file=sys.stderr)
        return 2

    try:
        cases = load_cases(args.case_dir)
        selected = select_cases(
            cases,
            selection=args.selection,
            requested_cases=parse_case_values(args.case),
            max_cases=args.max_cases,
        )
    except Exception as exc:
        print(f"Error selecting cases: {exc}", file=sys.stderr)
        return 2

    print(f"Loaded {len(cases)} walle valid schema cases from {args.case_dir}")
    print(f"Selected {len(selected)} tool-call schema case(s)")
    print(f"Selection mode: {args.selection}")
    print(f"Modes: {', '.join(modes)}")
    print(f"Model: {args.model}")
    print(f"Base URL: {args.base_url}")
    print(f"Dry run: {args.dry_run or args.list_cases}")
    print()

    if not selected:
        print("No tool-call schema cases selected.")
        return 1

    if args.list_cases or args.dry_run:
        results = []
        for case, _schema, reason in selected:
            for mode in modes:
                result = ToolCaseResult(
                    suite=case.suite,
                    line=case.line,
                    selection_reason=reason,
                    status="dry_run",
                    message="selected",
                    mode=mode,
                )
                print_result(result)
                results.append(result)
        if args.json_report:
            write_report(
                args.json_report,
                selected=selected,
                results=results,
                model=args.model,
                base_url=args.base_url,
                dry_run=True,
                thinking=args.thinking,
                think_mode=args.think_mode,
                modes=modes,
            )
        return 0

    if not args.api_key:
        print("Error: Set KIMI_API_KEY env var or use --api-key", file=sys.stderr)
        return 2

    client = make_client(args.base_url, args.api_key, args.timeout)
    results = run_cases(
        selected=selected,
        client=client,
        model=args.model,
        max_tokens=max(1, args.max_tokens),
        thinking=args.thinking,
        think_mode=args.think_mode,
        max_connections=max(1, args.max_connections),
        fail_fast=args.fail_fast,
        modes=modes,
    )

    if args.json_report:
        write_report(
            args.json_report,
            selected=selected,
            results=results,
            model=args.model,
            base_url=args.base_url,
            dry_run=False,
            thinking=args.thinking,
            think_mode=args.think_mode,
            modes=modes,
        )
        print(f"\nWrote JSON report: {args.json_report}")

    summary = summarize(results)
    print("\nSummary:")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    bad_results = [result for result in results if result.status != "passed"]
    if bad_results:
        print(
            f"\nVerification FAILED: {len(bad_results)} of {len(results)} "
            "tool-call case results were not successful."
        )
        return 1

    print(
        f"\nVerification PASSED: all {len(results)} tool-call case results "
        "were successful."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
