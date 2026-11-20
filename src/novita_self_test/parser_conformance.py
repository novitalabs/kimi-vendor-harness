"""Run offline K3 XTML vectors with a framework-supplied JSONL parser adapter.

The adapter reads requests from stdin and emits exactly one result per line:
request: {id, text, chunks: [str], tools: [str], finish: stop|length}
result: {reasoning: str, content: str, tool_calls: [{name: str, index: int, arguments: str}]}

No endpoint/model is involved. The process is bounded by the CLI --timeout
(total wall-clock seconds) and must exit successfully after stdin EOF.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import workspace_root
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


def validate_vectors(vectors: Path) -> int:
    try:
        cases = load_vectors(vectors)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{len(cases)} parser conformance vectors valid: {vectors}")
    return 0


def run_adapter(vectors: Path, adapter: str, *, timeout: float = 30.0) -> int:
    try:
        cases = load_vectors(vectors)
        command = shlex.split(adapter)
        if not command or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("adapter command and finite positive timeout are required")
    except (OSError, ValueError) as exc:
        print(f"parser conformance configuration: {exc}", file=sys.stderr)
        return 2
    requests, expected, labels = [], [], []
    for case in cases:
        for size in case["chunks"]:
            text = case["text"]
            chunks = [text] if size == "all" else [text[i:i + size] for i in range(0, len(text), size)]
            requests.append({"id": case["id"], "text": text, "chunks": chunks,
                             "tools": case["tools"], "finish": case.get("finish", "stop")})
            expected.append(norm(case["expected"]))
            labels.append(f"{case['id']} chunk={size}")
    payload = "".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests)
    failures = []
    # File-backed output prevents a noisy stderr pipe from deadlocking the adapter.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       text=True, start_new_session=True)
        except OSError as exc:
            print(f"cannot start parser adapter: {exc}", file=sys.stderr)
            return 2
        communicate_error = None
        try:
            process.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"parser adapter timed out after {timeout}s", file=sys.stderr)
            return 2
        except (BrokenPipeError, OSError) as exc:
            communicate_error = f"adapter I/O failed: {type(exc).__name__}: {exc}"
        finally:
            # Kill the isolated process group, including any surviving children.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
        if communicate_error:
            failures.append(communicate_error)
        if process.returncode:
            failures.append(f"adapter exited with status {process.returncode}")
        stdout.seek(0)
        raw = stdout.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            print("parser adapter result exceeds 4 MiB", file=sys.stderr)
            return 2
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeError:
            print("parser adapter result is not UTF-8", file=sys.stderr)
            return 1
        if len(lines) != len(expected):
            failures.append(f"adapter result count={len(lines)}, expected={len(expected)}")
        for label, line, wanted in zip(labels, lines, expected):
            try:
                actual = norm(json.loads(line))
            except (ValueError, TypeError) as exc:
                failures.append(f"{label}: invalid adapter JSON/result: {exc}")
                continue
            if actual != wanted:
                failures.append(f"{label}:\n  expected={json.dumps(wanted, ensure_ascii=False)}\n  actual={json.dumps(actual, ensure_ascii=False)}")
        stderr.seek(0)
        error_text = stderr.read(8192).decode("utf-8", errors="replace")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        if error_text:
            print(f"adapter stderr (bounded):\n{error_text}", file=sys.stderr)
        return 1
    print(f"{len(cases)} vectors passed across {len(requests)} chunk-size trials")
    return 0
