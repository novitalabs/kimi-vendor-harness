from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import openai

from novita_self_test.stability import (
    failure_class_counts,
    is_retryable_failure,
    service_invalid_reason,
)

CASE_FILES = {
    "a": "toolcall_thinking_case_a_invalid_arguments_repeat.jsonl",
    "b": "toolcall_thinking_case_b_truncated_arguments.jsonl",
}
CASE_B_EXPECTED_NAMES = {"WriteFile", "SetTodoList"}


def load_sample(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        sample = json.loads(handle.readline())
    messages = sample.get("messages")
    tools = sample.get("tools")
    if not isinstance(messages, list) or not isinstance(tools, list):
        raise ValueError(f"{path.name} requires messages and tools lists")
    return sample


def prepare_request(sample: dict[str, Any], case: str) -> dict[str, Any]:
    messages = sample["messages"]
    if case == "a":
        messages = messages[:143]
    else:
        messages = messages[:-2]
    return {
        "messages": messages,
        "tools": sample["tools"],
        "thinking": sample.get("thinking", {"type": "enabled"}),
        "forbidden": (sample.get("forbidden_arguments") or [None])[0],
    }


def call_model(
    client: openai.Client, model: str, request: dict[str, Any]
) -> tuple[str | None, str | None, str | None]:
    response = client.chat.completions.create(
        model=model,
        messages=request["messages"],
        tools=request["tools"],
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=32000,
        temperature=1.0,
        extra_body={"thinking": request["thinking"]},
    )
    finish_reason = None
    tool_names: dict[int, str] = {}
    argument_parts: dict[int, list[str]] = {}
    for chunk in response:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        for tool_call in choice.delta.tool_calls or []:
            if tool_call.index is None:
                raise ValueError("streamed tool_call is missing index")
            index = tool_call.index
            argument_parts.setdefault(index, [])
            if tool_call.function and tool_call.function.name:
                tool_names[index] = tool_call.function.name
            if tool_call.function and tool_call.function.arguments:
                argument_parts[index].append(tool_call.function.arguments)
    if 0 not in tool_names and 0 not in argument_parts:
        return finish_reason, None, None
    return finish_reason, tool_names.get(0), "".join(argument_parts.get(0, []))


def judge(
    case: str,
    request: dict[str, Any],
    finish_reason: str | None,
    tool_name: str | None,
    arguments: str | None,
) -> list[str]:
    failures: list[str] = []
    if finish_reason != "tool_calls":
        failures.append(f"finish_reason={finish_reason!r}, expected tool_calls")
    if case == "b" and tool_name not in CASE_B_EXPECTED_NAMES:
        failures.append(
            f"tool_name={tool_name!r}, expected one of {sorted(CASE_B_EXPECTED_NAMES)}"
        )
    if arguments is None:
        failures.append("response has no tool_call")
        return failures
    if arguments == "":
        failures.append("tool_call arguments are empty")
        return failures
    if case == "a" and request["forbidden"] and arguments == request["forbidden"]:
        failures.append("arguments repeat the sample forbidden invalid pattern")
    try:
        json.loads(arguments)
    except json.JSONDecodeError as exc:
        failures.append(f"arguments are not valid JSON: {exc}")
    return failures


def run_case(
    client: openai.Client,
    model: str,
    case: str,
    fixture_dir: Path,
    output_dir: Path,
    runs: int,
) -> tuple[dict[str, Any], bool]:
    sample = load_sample(fixture_dir / CASE_FILES[case])
    request = prepare_request(sample, case)
    attempts: list[dict[str, Any]] = []
    stability_errors: list[str] = []
    for index in range(runs):
        try:
            finish_reason, tool_name, arguments = call_model(client, model, request)
            failures = judge(case, request, finish_reason, tool_name, arguments)
            attempts.append(
                {
                    "run": index,
                    "finish_reason": finish_reason,
                    "tool_name": tool_name,
                    "arguments_bytes": len(arguments.encode("utf-8")) if arguments is not None else 0,
                    "arguments_empty": arguments == "",
                    "passed": not failures,
                    "failures": failures,
                }
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            stability_errors.append(message)
            attempts.append(
                {
                    "run": index,
                    "passed": False,
                    "error": message,
                    "failure_class": "stability" if is_retryable_failure(message) else "service_or_runner",
                }
            )
            if service_invalid_reason([message]) is not None:
                break

    result = {
        "case": case,
        "fixture": CASE_FILES[case],
        "source_id": sample.get("id"),
        "source_messages": len(sample["messages"]),
        "source_tools": len(sample["tools"]),
        "replayed_messages": len(request["messages"]),
        "runs_requested": runs,
        "attempts": attempts,
        "passed": sum(bool(item.get("passed")) for item in attempts),
        "model_failures": sum(bool(item.get("failures")) for item in attempts),
        "stability_errors": stability_errors,
        "failure_class_counts": failure_class_counts(stability_errors),
    }
    path = output_dir / f"case-{case}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    service_reason = service_invalid_reason(stability_errors)
    if service_reason is not None:
        return result, False
    return result, len(attempts) == runs and result["passed"] == runs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    api_key = os.environ.get("API_KEY")
    if not api_key:
        parser.error("API_KEY environment variable is required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    client = openai.Client(base_url=args.base_url, api_key=api_key, timeout=args.timeout)
    results = []
    passed = True
    for case in ("a", "b"):
        result, case_passed = run_case(
            client, args.model, case, args.fixture_dir, args.output_dir, args.runs
        )
        results.append(result)
        passed = passed and case_passed
        if service_invalid_reason(result["stability_errors"]) is not None:
            break
    all_stability_errors = [error for result in results for error in result["stability_errors"]]
    model_failures = sum(result["model_failures"] for result in results)
    adapter_result = {
        "schema_version": 1,
        "suite_id": "toolcall-arguments-recovery",
        "row_ids": ["18"],
        "status": "passed" if passed else ("blocked" if service_invalid_reason(all_stability_errors) else "failed"),
        "validity": (
            "valid"
            if passed or not all_stability_errors
            else (
                "service_invalid"
                if service_invalid_reason(all_stability_errors)
                else "execution_invalid"
            )
        ),
        "counts": {
            "cases": len(results),
            "runs_requested": args.runs * 2,
            "runs_observed": sum(len(result["attempts"]) for result in results),
            "passed": sum(result["passed"] for result in results),
            "model_failures": model_failures,
            "stability_errors": len(all_stability_errors),
        },
        "metrics": {},
        "redacted_command": [],
        "artifact_paths": [str((args.output_dir / f"case-{case}.json").resolve()) for case in ("a", "b") if (args.output_dir / f"case-{case}.json").is_file()],
        "failure_summary": None if passed else "; ".join(
            error
            for result in results
            for attempt in result["attempts"]
            for error in attempt.get("failures", [])
        ) or None,
        "runner_fingerprint": {"runner": "novita/toolcall-arguments-recovery", "runs_per_case": str(args.runs)},
        "coverage": {
            "cases": [result["case"] for result in results],
            "failure_class_counts": failure_class_counts(all_stability_errors),
        },
        "reason": None if passed else (
            service_invalid_reason(all_stability_errors) or "toolcall_arguments_recovery_failed"
        ),
    }
    (args.output_dir / "adapter-result.json").write_text(
        json.dumps(adapter_result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
