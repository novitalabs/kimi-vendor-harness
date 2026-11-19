#!/usr/bin/env python3
"""Run framework-neutral K3 XTML output-parser vectors through an adapter.

The adapter is an executable supplied by the framework under test. It receives one
JSON request per line on stdin and must return one JSON result per line on stdout.
No model or endpoint is involved: these vectors constrain the parser after token
generation, including chunk-boundary behaviour.

Adapter request:
  {"id": str, "text": str, "chunks": [str|"all"], "tools": [str], "finish": "stop"|"length"}

Adapter result:
  {"reasoning": str, "content": str, "tool_calls":
   [{"name": str, "index": int, "arguments": str}]}

Usage:
  "$NVST_HARNESS_PYTHON" scripts/run-parser-conformance.py --adapter 'python adapter.py' \
      --vectors suites/novita-api/tests/k3-parser-conformance.jsonl
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VECTORS = ROOT / "suites/novita-api/tests/k3-parser-conformance.jsonl"


def norm(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "reasoning": value.get("reasoning", ""),
        "content": value.get("content", ""),
        "tool_calls": [
            {
                "name": call.get("name"),
                "index": call.get("index"),
                "arguments": call.get("arguments", ""),
            }
            for call in value.get("tool_calls", [])
        ],
    }


def run(vectors: Path, adapter: str) -> int:
    try:
        from parser_conformance_lib import load_vectors
        cases = load_vectors(vectors)
    except (ImportError, OSError, ValueError) as exc:
        print(f"cannot load parser conformance vectors: {exc}", file=sys.stderr)
        return 2
    try:
        process = subprocess.Popen(
            shlex.split(adapter), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except OSError as exc:
        print(f"cannot start parser adapter: {exc}", file=sys.stderr)
        return 2
    assert process.stdin is not None and process.stdout is not None
    failures: list[str] = []
    try:
        for case in cases:
            for chunk_size in case["chunks"]:
                text = case["text"]
                if chunk_size == "all":
                    chunks = [text]
                elif chunk_size == 1:
                    chunks = list(text)
                else:
                    chars = list(text)
                    chunks = ["".join(chars[i:i + chunk_size]) for i in range(0, len(chars), chunk_size)]
                request = {
                    "id": case["id"],
                    "text": text,
                    "chunks": chunks,
                    "tools": case.get("tools", []),
                    "finish": case.get("finish", "stop"),
                }
                process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                process.stdin.flush()
                line = process.stdout.readline()
                if not line:
                    failures.append(f"{case['id']} chunk={chunk_size}: adapter exited without a result")
                    break
                try:
                    actual = norm(json.loads(line))
                except (json.JSONDecodeError, TypeError) as exc:
                    failures.append(f"{case['id']} chunk={chunk_size}: invalid adapter JSON: {exc}")
                    continue
                expected = norm(case["expected"])
                if actual != expected:
                    failures.append(
                        f"{case['id']} chunk={chunk_size}:\n"
                        f"  expected={json.dumps(expected, ensure_ascii=False, sort_keys=True)}\n"
                        f"  actual={json.dumps(actual, ensure_ascii=False, sort_keys=True)}"
                    )
    finally:
        if process.stdin:
            process.stdin.close()
        process.terminate()
        stderr = process.stderr.read() if process.stderr else ""
        process.wait(timeout=5)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        if stderr:
            print(f"adapter stderr:\n{stderr}", file=sys.stderr)
        return 1
    print(f"{len(cases)} vectors passed across requested chunk sizes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    args = parser.parse_args()
    try:
        return run(args.vectors, args.adapter)
    except OSError as exc:
        print(f"parser conformance vector I/O failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
