"""Offline token-id parity against developer-supplied, provenance-pinned goldens."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .adapter_process import exchange
from .config import ConfigError, find_secret, redact, strict_json
from .contracts import issue, validate_contract


def load_vectors(path: Path) -> list[dict]:
    rows, ids = [], set()
    try:
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            value = strict_json(line)
            validate_contract(value, "renderer-vector.schema.json")
            if value["id"] in ids or find_secret(value):
                raise ValueError("duplicate vector id or forbidden credential-like material")
            ids.add(value["id"])
            rows.append(value)
    except (OSError, ValueError, ConfigError) as exc:
        raise ConfigError("renderer_vectors_invalid", "cannot load renderer vectors: " + str(exc)) from exc
    if not rows:
        raise ConfigError("renderer_vectors_invalid", "renderer vector file is empty")
    return rows


def run(vectors: Path, adapter: str | None = None, timeout: float = 30) -> dict:
    report = {"schema_version": 1, "kind": "renderer_validation" if adapter is None else "renderer_report", "status": "passed", "vectors": str(vectors),
              "vectors_sha256": None, "results": [], "problems": [], "stderr": "", "endpoint_requests": 0}
    try:
        before = vectors.read_bytes()
        cases = load_vectors(vectors)
        if vectors.read_bytes() != before:
            raise ConfigError("renderer_vectors_invalid", "vector file changed while loading")
        report["vectors_sha256"] = hashlib.sha256(before).hexdigest()
    except (OSError, ConfigError) as exc:
        report.update(status="blocked", problems=[issue("renderer_vectors_invalid", str(exc))])
        return report
    if adapter is None:
        report["results"] = [{"id": c["id"], "status": "validated", "problems": [], "first_difference": None} for c in cases]
        return report
    requests = [{"id": c["id"], "request": c["request"], "authority": c["authority"]} for c in cases]
    lines, report["stderr"], report["problems"], report["status"] = exchange(adapter, requests, timeout)
    if report["status"] != "passed" and not lines:
        return redact(report)
    if len(lines) != len(cases):
        report["problems"].append(issue("adapter_result_count_mismatch", "renderer result count differs", expected=len(cases), actual=len(lines)))
    for index, case in enumerate(cases):
        row = {"id": case["id"], "status": "passed", "problems": [], "first_difference": None}
        try:
            value = strict_json(lines[index])
            if not isinstance(value, dict) or value.get("id") != case["id"]:
                raise ValueError("result id does not match the input vector")
            actual, expected = value.get("token_ids"), case["expected_token_ids"]
            if not isinstance(actual, list) or any(type(token) is not int or token < 0 for token in actual):
                raise ValueError("token_ids must be nonnegative integers")
            offset = next((i for i in range(max(len(actual), len(expected))) if i >= len(actual) or i >= len(expected) or actual[i] != expected[i]), None)
            if offset is not None:
                row["first_difference"] = {"index": offset, "expected_length": len(expected), "actual_length": len(actual),
                                           "expected_context": expected[max(0, offset-4):offset+5], "actual_context": actual[max(0, offset-4):offset+5]}
                row["problems"].append(issue("renderer_tokens_mismatch", "token ids differ from the pinned golden", path=["token_ids", offset]))
        except (ValueError, IndexError, TypeError) as exc:
            row["problems"].append(issue("adapter_result_invalid", str(exc)))
        row["status"] = "failed" if row["problems"] else "passed"
        report["results"].append(row)
    if report["status"] == "passed" and (report["problems"] or any(row["status"] == "failed" for row in report["results"])):
        report["status"] = "failed"
    return redact(report)
