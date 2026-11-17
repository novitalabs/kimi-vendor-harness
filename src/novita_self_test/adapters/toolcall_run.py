from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from typing import Any

from inspect_ai import Task, eval, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import get_model
from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    scorer,
    stderr,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import ToolInfo
from inspect_ai.tool._tool_params import ToolParams
from jsonschema import Draft7Validator


FIXTURE_SCHEMA_VERSION = 1
MEANINGLESS_VALUES = ("", None, " ", ": ", ":", ":158")


class EngineOverloadedError(RuntimeError):
    """A service overload response that should be retried, not scored."""


def _original_finish_reason(output: Any) -> str | None:
    metadata = getattr(output, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("original_finish_reason")
    else:
        value = getattr(metadata, "original_finish_reason", None)
    return str(value) if value is not None else None


def load_fixture(path: Path) -> tuple[MemoryDataset, dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported toolcall fixture schema: {payload.get('schema_version')!r}"
        )
    catalog = payload.get("tool_catalog")
    rows = payload.get("samples")
    if not isinstance(catalog, dict) or not isinstance(rows, list):
        raise ValueError("toolcall fixture requires tool_catalog and samples")

    samples = []
    for row in rows:
        refs = row.get("tool_refs") or []
        try:
            tools = [catalog[ref] for ref in refs]
        except KeyError as exc:
            raise ValueError(f"toolcall fixture references unknown tool {exc}") from exc
        samples.append(
            Sample(
                id=row["id"],
                input=row["input"],
                target=row.get("target", ""),
                metadata={
                    "tools": tools,
                    "repeat_run_target_arguments_valid_non_empty": bool(
                        row.get("target_arguments_valid_non_empty", False)
                    ),
                },
            )
        )
    return MemoryDataset(samples=samples, name="kimi_toolcall"), dict(
        payload.get("source") or {}
    )


def _tool_info(raw: dict[str, Any]) -> ToolInfo:
    function = raw.get("function") or {}
    return ToolInfo(
        name=str(function.get("name") or ""),
        description=str(function.get("description") or ""),
        parameters=ToolParams.model_validate(function.get("parameters") or {}),
    )


@solver
def kimi_toolcall_solver() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        del generate
        tools = [_tool_info(item) for item in state.metadata.get("tools", [])]
        output = await get_model().generate(
            input=state.messages,
            tools=tools,
            tool_choice="auto",
        )
        if _original_finish_reason(output) == "engine_overloaded":
            raise EngineOverloadedError(
                "service returned finish_reason=engine_overloaded"
            )
        state.output = output
        state.messages.append(output.message)
        return state

    return solve


def _semantically_empty(value: Any, path: str = "") -> tuple[str, Any] | None:
    if isinstance(value, str) and value in MEANINGLESS_VALUES:
        return path, value
    if isinstance(value, dict):
        for key, item in value.items():
            found = _semantically_empty(item, f"{path}.{key}" if path else str(key))
            if found:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _semantically_empty(item, f"{path}[{index}]")
            if found:
                return found
    return None


def _check_tool_call(
    tool_call: Any, tool_schemas: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    arguments = getattr(tool_call, "arguments", None)
    function = str(getattr(tool_call, "function", "") or "")
    checks: dict[str, bool] = {}
    details: dict[str, str] = {}

    if not isinstance(arguments, dict) or not arguments or not any(
        value not in MEANINGLESS_VALUES for value in arguments.values()
    ):
        checks["empty_or_meaningless"] = True

    parse_error = getattr(tool_call, "parse_error", None)
    if parse_error:
        checks["json_parse"] = True
        details["json_parse"] = str(parse_error)

    schema = tool_schemas.get(function)
    if schema is None:
        checks["unknown_tool"] = True
        details["unknown_tool"] = f"tool {function!r} is not declared for this sample"
    elif isinstance(arguments, dict):
        error = next(Draft7Validator(schema).iter_errors(arguments), None)
        if error is not None:
            checks["schema_validation"] = True
            details["schema_validation"] = error.message

    empty = _semantically_empty(arguments)
    if empty is not None:
        field, value = empty
        checks["semantically_empty"] = True
        details["semantically_empty"] = (
            f"{field}={value!r} is semantically empty "
            "(schema allows any string, but value is a placeholder)"
        )

    if not checks:
        return None
    result: dict[str, Any] = {
        "tool_call_id": str(getattr(tool_call, "id", "") or ""),
        "tool_name": function,
        "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        "checks": checks,
    }
    if details:
        result["details"] = details
    return result


def score_tool_calls(
    tool_calls: list[Any],
    tools: list[dict[str, Any]],
    *,
    target_arguments_valid_non_empty: bool,
) -> Score:
    if not tool_calls:
        if not target_arguments_valid_non_empty:
            return Score(
                value=CORRECT,
                explanation=(
                    "No tool_calls in output; repeat-run target is a known bad "
                    "tool-call example, so reproducing it is not required"
                ),
            )
        return Score(
            value=INCORRECT,
            explanation="No tool_calls in output for a valid tool-call target",
        )

    tool_schemas = {
        str(item.get("function", {}).get("name") or ""): dict(
            item.get("function", {}).get("parameters") or {}
        )
        for item in tools
    }
    problems = [
        problem
        for tool_call in tool_calls
        if (problem := _check_tool_call(tool_call, tool_schemas)) is not None
    ]
    if not problems:
        return Score(
            value=CORRECT,
            explanation=f"All {len(tool_calls)} tool_call(s) passed format checks",
        )

    check_order = (
        "empty_or_meaningless",
        "json_parse",
        "unknown_tool",
        "schema_validation",
        "semantically_empty",
    )
    failed_checks = [
        check
        for check in check_order
        if any(problem["checks"].get(check) for problem in problems)
    ]
    return Score(
        value=INCORRECT,
        answer=json.dumps(problems, ensure_ascii=False),
        explanation=(
            f"Found {len(problems)} problematic tool_call(s): "
            + ", ".join(failed_checks)
        ),
    )


@scorer(metrics=[accuracy(), stderr()])
def kimi_toolcall_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        del target
        metadata = state.metadata or {}
        tool_calls = state.output.message.tool_calls if state.output.choices else []
        return score_tool_calls(
            tool_calls or [],
            list(metadata.get("tools") or []),
            target_arguments_valid_non_empty=bool(
                metadata.get("repeat_run_target_arguments_valid_non_empty", False)
            ),
        )

    return score


@task
def kimi_toolcall(dataset_path: str) -> Task:
    dataset, _ = load_fixture(Path(dataset_path))
    return Task(
        dataset=dataset,
        solver=kimi_toolcall_solver(),
        scorer=kimi_toolcall_scorer(),
    )


def _thinking_extra_body(think_mode: str, effort: str) -> dict[str, Any]:
    if think_mode == "opensource":
        return {
            "chat_template_kwargs": {
                "thinking": True,
                "preserve_thinking": True,
                "thinking_effort": effort,
            }
        }
    if think_mode == "kimi":
        return {"thinking": {"type": "enabled", "keep": "all", "effort": effort}}
    raise ValueError(f"unsupported think mode for toolcall benchmark: {think_mode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="K3 toolcall thinking-effort benchmark")
    parser.add_argument("--dataset-path", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--think-mode", choices=("kimi", "opensource"), required=True)
    parser.add_argument("--thinking-effort", choices=("low", "high", "max"), required=True)
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--max-connections", type=int, default=50)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--client-timeout", type=int, default=86400)
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--retry-on-error", type=int, default=3)
    parser.add_argument("--stream", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import kimi_model  # noqa: F401 - registers the Kimi/opensource Inspect providers

    eval(
        [kimi_toolcall(str(args.dataset_path.resolve()))],
        [args.model],
        max_tokens=args.max_tokens,
        max_connections=args.max_connections,
        epochs=1,
        extra_body=_thinking_extra_body(args.think_mode, args.thinking_effort),
        retry_on_error=args.retry_on_error,
        continue_on_fail=True,
        fail_on_error=False,
        sample_id=args.sample_id or None,
        temperature=args.temperature,
        top_p=args.top_p,
        model_args={
            "stream": args.stream,
            "max_retries": 0,
            "timeout": args.client_timeout,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
