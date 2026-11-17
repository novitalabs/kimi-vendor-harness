"""Validates the tool-call contract suite itself against a fake endpoint with injectable faults.

A healthy fake must pass every gate test; each fault reproduces one known engine defect and must be
caught by exactly the test(s) written for it. Runs the real suite with the kvv-k3 interpreter, as nvst does.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "vendor/kvv-k3/.venv/bin/python"
SUITE = "suites/novita-api/tests/toolcall_contract"
FAKE = ROOT / "tests/support/fake_k3_server.py"

pytestmark = pytest.mark.skipif(not PYTHON.exists(), reason="vendor/kvv-k3 environment not synced (uv sync)")

# fault -> test functions that must fail (and nothing else may fail)
EXPECTED = {
    "schema_500": {"test_invalid_tool_schema_is_rejected_not_crashed"},
    "usage_stream_off": {"test_prompt_tokens_identical_stream_vs_nonstream"},
    "marker_special": {"test_marker_text_is_rendered_as_plain_text"},
    "empty_turn": {
        "test_every_turn_has_content_or_tool_call",
        "test_many_tools_with_default_tool_choice",
        "test_non_strict_tools_return_only_declared_tools",
    },
    "xtml_leak": {
        "test_every_turn_has_content_or_tool_call",
        "test_many_tools_with_default_tool_choice",
        "test_non_strict_tools_return_only_declared_tools",
        "test_response_format_holds_when_tools_are_present",
    },
    "undeclared_tool": {
        "test_every_turn_has_content_or_tool_call",
        "test_many_tools_with_default_tool_choice",
        "test_non_strict_tools_return_only_declared_tools",
    },
    "bad_args": {
        "test_every_turn_has_content_or_tool_call",
        "test_many_tools_with_default_tool_choice",
        "test_non_strict_tools_return_only_declared_tools",
    },
    "tools_override_rf": {"test_response_format_holds_when_tools_are_present"},
    "json_empty_key": {"test_response_format_holds_when_tools_are_present"},
    # Observation-only behaviour: the suite must stay green.
    "ignore_parallel": set(),
}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run_suite(faults: str, tmp_path: Path) -> tuple[set[str], str, Path]:
    port = free_port()
    server = subprocess.Popen([str(PYTHON), str(FAKE), "--port", str(port), "--faults", faults],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert server.stdout.readline().strip() == "ready"
        time.sleep(0.2)
        env = {
            **os.environ,
            "PYTHONPATH": f"{ROOT}/suites/novita-api:{ROOT}/vendor/kvv-k3",
            "KVV_ROOT": str(ROOT / "vendor/kvv-k3"),
            "KIMI_API_KEY": "test",
            "TC_SAMPLES": "3",
            "TC_OBSERVATIONS_DIR": str(tmp_path),
            "NO_PROXY": "*",
        }
        for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(name, None)
        process = subprocess.run(
            [str(PYTHON), "-m", "pytest", SUITE, "--base-url", f"http://127.0.0.1:{port}/v1",
             "--smoke-model", "kimi-k3", "--think-mode", "opensource", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=600,
        )
    finally:
        server.kill()
        server.wait()
    failed = set(re.findall(r"^FAILED [^:]+::(\w+)", process.stdout, flags=re.M))
    return failed, process.stdout + process.stderr, tmp_path / "toolcall-contract-observations.jsonl"


def test_healthy_endpoint_passes_every_gate(tmp_path: Path) -> None:
    failed, output, observations = run_suite("", tmp_path)
    assert not failed, output[-3000:]
    assert observations.is_file(), "observation tests did not write their report"
    text = observations.read_text()
    assert '"observation": "parallel_tool_calls"' in text and '"observation": "marker_text_in_tool_arguments"' in text


@pytest.mark.parametrize("fault", sorted(EXPECTED))
def test_each_known_defect_is_caught_by_its_test(fault: str, tmp_path: Path) -> None:
    failed, output, _ = run_suite(fault, tmp_path)
    assert failed == EXPECTED[fault], f"fault {fault!r}: failing tests {sorted(failed)}\n{output[-2000:]}"
