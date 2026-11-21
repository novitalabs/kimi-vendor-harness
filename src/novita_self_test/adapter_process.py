"""Bounded JSONL subprocess transport shared by offline component adapters."""
from __future__ import annotations

import contextlib
import json
import math
import os
import shlex
import signal
import subprocess
import tempfile

from .contracts import issue


def exchange(adapter: str, requests: list[dict], timeout: float) -> tuple[list[str], str, list[dict], str]:
    try:
        command = shlex.split(adapter)
        if not command or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("adapter command and finite positive timeout are required")
    except ValueError as exc:
        return [], "", [issue("adapter_configuration_invalid", str(exc))], "blocked"
    payload = "".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests)
    problems = []
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                       text=True, start_new_session=True)
        except OSError as exc:
            return [], "", [issue("adapter_start_failed", str(exc))], "execution_invalid"
        try:
            process.communicate(input=payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            problems.append(issue("adapter_timeout", "adapter timed out", expected=timeout))
        except OSError as exc:
            problems.append(issue("adapter_io_failed", str(exc)))
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
        stderr.seek(0)
        error_text = stderr.read(8192).decode("utf-8", errors="replace")
        if problems:
            return [], error_text, problems, "execution_invalid"
        if process.returncode:
            problems.append(issue("adapter_exit_nonzero", "adapter exited unsuccessfully", expected=0, actual=process.returncode))
        stdout.seek(0)
        raw = stdout.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return [], error_text, problems + [issue("adapter_output_limit", "adapter output exceeds 4 MiB")], "execution_invalid"
        try:
            # JSONL delimiters, not Python's broader Unicode splitlines set.
            lines = raw.decode("utf-8").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
        except UnicodeError:
            return [], error_text, problems + [issue("adapter_encoding_invalid", "adapter result is not UTF-8")], "execution_invalid" if problems else "failed"
        return lines, error_text, problems, "execution_invalid" if problems else "passed"
