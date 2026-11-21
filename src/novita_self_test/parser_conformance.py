"""Offline parser adapters return data, never instructions or endpoint evidence."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import shlex
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .config import redact, workspace_root
from .contracts import issue
from .parser_vectors import load_vectors

DEFAULT_VECTORS = workspace_root() / "capabilities/prompt_rendering/k3-parser-conformance.jsonl"
MAX_RESULT_BYTES = 4 * 1024 * 1024


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


def _load(vectors: Path, kind: str) -> tuple[dict, list]:
    report = {"schema_version": 1, "kind": kind, "status": "passed", "vectors": str(vectors),
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
    try:
        command = shlex.split(adapter)
        if not command or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("adapter command and finite positive timeout are required")
    except ValueError as exc:
        report.update(status="blocked", problems=[issue("parser_configuration_invalid", str(exc))])
        return report
    requests, trials = [], []
    for case in cases:
        for size in case["chunks"]:
            text = case["text"]
            chunks = [text] if size == "all" else [text[i:i + size] for i in range(0, len(text), size)]
            requests.append({"id": case["id"], "text": text, "chunks": chunks,
                             "tools": case["tools"], "finish": case.get("finish", "stop")})
            trials.append({"id": case["id"], "chunk_size": size, "status": "not_run", "problems": [],
                           "expected": norm(case["expected"]), "actual": None})
    report["results"] = trials
    payload = "".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       text=True, start_new_session=True)
        except OSError as exc:
            report.update(status="execution_invalid", problems=[issue("adapter_start_failed", str(exc))])
            return report
        try:
            process.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            report["problems"].append(issue("adapter_timeout", "parser adapter timed out", expected=timeout))
        except OSError as exc:
            report["problems"].append(issue("adapter_io_failed", str(exc)))
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
        stderr.seek(0)
        report["stderr"] = stderr.read(8192).decode("utf-8", errors="replace")
        if report["problems"]:
            report["status"] = "execution_invalid"
            return redact(report)
        if process.returncode:
            report["problems"].append(issue("adapter_exit_nonzero", "adapter exited unsuccessfully", expected=0, actual=process.returncode))
            report["status"] = "execution_invalid"
        stdout.seek(0)
        raw = stdout.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            report.update(status="execution_invalid")
            report["problems"].append(issue("adapter_output_limit", "adapter output exceeds byte ceiling", expected=MAX_RESULT_BYTES))
            return redact(report)
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            report["problems"].append(issue("adapter_encoding_invalid", "adapter result is not UTF-8"))
            report["status"] = "failed" if report["status"] == "passed" else report["status"]
            return redact(report)
        if len(lines) != len(trials):
            report["problems"].append(issue("adapter_result_count_mismatch", "adapter result count differs", expected=len(trials), actual=len(lines)))
        for index, trial in enumerate(trials):
            if index >= len(lines):
                trial["problems"].append(issue("adapter_result_missing", "missing adapter result"))
            else:
                try:
                    trial["actual"] = norm(json.loads(lines[index]))
                    if trial["actual"] != trial["expected"]:
                        trial["problems"].append(issue("parser_result_mismatch", "parser output differs from vector", expected=trial["expected"], actual=trial["actual"]))
                except (ValueError, TypeError) as exc:
                    trial["problems"].append(issue("adapter_result_invalid", str(exc)))
            trial["status"] = "failed" if trial["problems"] else "passed"
        if report["status"] == "passed" and (report["problems"] or any(trial["problems"] for trial in trials)):
            report["status"] = "failed"
    return redact(report)
