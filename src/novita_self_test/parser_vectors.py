"""Load and validate framework-neutral K3 XTML output-parser vectors."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import strict_json

REQUIRED_EXPECTED = {"reasoning", "content", "tool_calls"}


def validate_case(case: Any, line: int) -> list[str]:
    errors: list[str] = []
    prefix = f"line {line}"
    if not isinstance(case, dict):
        return [f"{prefix}: expected an object"]
    for key in ("id", "source_test", "source_revision", "text", "chunks", "tools", "expected"):
        if key not in case:
            errors.append(f"{prefix}: missing {key!r}")
    if not isinstance(case.get("id"), str) or not case.get("id"):
        errors.append(f"{prefix}: id must be a non-empty string")
    if not isinstance(case.get("source_test"), str) or not case.get("source_test"):
        errors.append(f"{prefix}: source_test must be a non-empty string")
    if not isinstance(case.get("source_revision"), str) or not case.get("source_revision"):
        errors.append(f"{prefix}: source_revision must be a non-empty string")
    if not isinstance(case.get("text"), str) or "<|open|>" not in case.get("text", ""):
        errors.append(f"{prefix}: text must be XTML containing <|open|>")
    if "finish" in case and case["finish"] not in ("stop", "length"):
        errors.append(f"{prefix}: finish must be stop or length")
    if case.get("comparison") not in {"json", "bytes"}:
        errors.append(f"{prefix}: comparison must explicitly be json or bytes")
    chunks = case.get("chunks")
    if not isinstance(chunks, list) or not chunks or not all(
        (type(v) is int and v > 0) or v == "all" for v in chunks
    ):
        errors.append(f"{prefix}: chunks must be positive integers or 'all'")
    tools = case.get("tools")
    if not isinstance(tools, list) or not all(isinstance(v, str) and v for v in tools):
        errors.append(f"{prefix}: tools must be a list of non-empty strings")
    expected = case.get("expected")
    if not isinstance(expected, dict) or set(expected) != REQUIRED_EXPECTED:
        errors.append(f"{prefix}: expected must contain exactly {sorted(REQUIRED_EXPECTED)}")
    elif not isinstance(expected["reasoning"], str) or not isinstance(expected["content"], str):
        errors.append(f"{prefix}: expected reasoning/content must be strings")
    elif not isinstance(expected["tool_calls"], list):
        errors.append(f"{prefix}: expected tool_calls must be a list")
    else:
        for index, call in enumerate(expected["tool_calls"]):
            if not isinstance(call, dict):
                errors.append(f"{prefix}: tool_calls[{index}] must be an object")
                continue
            if (not isinstance(call.get("name"), str) or not call["name"]
                    or type(call.get("index")) is not int or call["index"] < 0):
                errors.append(f"{prefix}: tool_calls[{index}] needs nonempty name and nonnegative integer index")
            if not isinstance(call.get("arguments"), str):
                errors.append(f"{prefix}: tool_calls[{index}].arguments must be a string")
    return errors


def load_vectors(path: Path) -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for line, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
        if not raw.strip():
            continue
        try:
            case = strict_json(raw)
        except ValueError as exc:
            errors.append(f"line {line}: invalid JSON: {exc}")
            continue
        errors.extend(validate_case(case, line))
        if isinstance(case, dict):
            case_id = case.get("id")
            if isinstance(case_id, str) and case_id in seen:
                errors.append(f"line {line}: duplicate id {case_id!r}")
            if isinstance(case_id, str):
                seen.add(case_id)
            vectors.append(case)
    if errors:
        raise ValueError("\n".join(errors))
    if not vectors:
        raise ValueError(f"{path}: no vectors")
    return vectors
