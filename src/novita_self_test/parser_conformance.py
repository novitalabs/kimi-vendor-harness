"""Offline parser adapters return data, never instructions or endpoint evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .config import redact, strict_json, workspace_root
from .adapter_process import exchange
from .contracts import issue
from .parser_vectors import load_vectors

DEFAULT_VECTORS = workspace_root() / "capabilities/prompt_rendering/k3-parser-conformance.jsonl"


def norm(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"reasoning", "content", "tool_calls"} <= value.keys():
        raise ValueError("result needs reasoning, content and tool_calls")
    if not isinstance(value["reasoning"], str) or not isinstance(value["content"], str) or not isinstance(value["tool_calls"], list):
        raise ValueError("result channels/calls have invalid types")
    calls = []
    for call in value["tool_calls"]:
        if (not isinstance(call, dict) or not isinstance(call.get("name"), str)
                or type(call.get("index")) is not int or call["index"] < 0
                or not isinstance(call.get("arguments"), str)):
            raise ValueError("invalid tool-call result")
        calls.append({key: call[key] for key in ("name", "index", "arguments")})
    return {"reasoning": value["reasoning"], "content": value["content"], "tool_calls": calls}


def comparison_value(value: dict, mode: str) -> dict:
    import copy
    value = copy.deepcopy(value)
    if mode == "json":
        for call in value["tool_calls"]:
            call["arguments"] = strict_json(call["arguments"])
    return value


def equivalent(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (float, int)) and isinstance(expected, (float, int)):
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(equivalent(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(equivalent(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _load(vectors: Path, kind: str) -> tuple[dict, list]:
    report = {"schema_version": 2, "kind": kind, "status": "passed", "vectors": str(vectors),
              "vectors_sha256": None, "vector_count": 0, "trial_count": 0,
              "problems": [], "results": [], "stderr": "", "endpoint_requests": 0}
    try:
        before = vectors.read_bytes()
        cases = load_vectors(vectors)
        if vectors.read_bytes() != before:
            raise ValueError("vector file changed during loading")
        report.update(vectors_sha256=hashlib.sha256(before).hexdigest(), vector_count=len(cases),
                      trial_count=sum(len(case["chunks"]) for case in cases))
        return report, cases
    except (OSError, ValueError) as exc:
        report.update(status="blocked", problems=[issue("parser_vectors_invalid", str(exc))])
        return report, []


def validate_vectors(vectors: Path) -> dict:
    return _load(vectors, "parser_validation")[0]


def run_adapter(vectors: Path, adapter: str, *, timeout: float = 30.0) -> dict:
    report, cases = _load(vectors, "parser_report")
    if report["status"] != "passed":
        return report
    requests, trials = [], []
    for case in cases:
        for size in case["chunks"]:
            text = case["text"]
            chunks = [text] if size == "all" else [text[i:i + size] for i in range(0, len(text), size)]
            requests.append({"id": case["id"], "text": text, "chunks": chunks,
                             "tools": case["tools"], "finish": case.get("finish", "stop")})
            trials.append({"id": case["id"], "chunk_size": size, "comparison": case["comparison"], "status": "not_run", "problems": [],
                           "expected": norm(case["expected"]), "actual": None})
    report["results"] = trials
    lines, report["stderr"], report["problems"], report["status"] = exchange(adapter, requests, timeout)
    if report["status"] in {"blocked", "execution_invalid"} and not lines:
        return redact(report)
    if len(lines) != len(trials):
        report["problems"].append(issue("adapter_result_count_mismatch", "adapter result count differs", expected=len(trials), actual=len(lines)))
    for index, trial in enumerate(trials):
        if index >= len(lines):
            trial["problems"].append(issue("adapter_result_missing", "missing adapter result"))
        else:
            try:
                trial["actual"] = norm(strict_json(lines[index]))
                actual, expected = comparison_value(trial["actual"], trial["comparison"]), comparison_value(trial["expected"], trial["comparison"])
                if not equivalent(actual, expected):
                    trial["problems"].append(issue("parser_result_mismatch", "parser output differs from vector", expected=trial["expected"], actual=trial["actual"]))
            except (ValueError, TypeError) as exc:
                trial["problems"].append(issue("adapter_result_invalid", str(exc)))
        trial["status"] = "failed" if trial["problems"] else "passed"
    if report["status"] == "passed" and (report["problems"] or any(trial["problems"] for trial in trials)):
        report["status"] = "failed"
    return redact(report)
