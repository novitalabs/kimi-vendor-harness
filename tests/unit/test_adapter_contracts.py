from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

from novita_self_test.adapters import AdapterContext, adapter_for
from novita_self_test.adapters.beam_run import main as beam_main, retry_invalid_generations
from novita_self_test.adapters.deepswe_remote import (
    _load_checkpoint,
    _merged_checkpoint_results,
    _write_task_results,
    cleanup_checkpoint_resources,
    proxy_free_docker_config,
    read_wave_results,
    run_benchmark_checkpointed,
    summarize_round,
    verify_runtime,
)
from novita_self_test.artifacts import enforce_completeness
from novita_self_test.config import ConfigError, ServiceAccess, SuiteDefinition
from novita_self_test.header_injection import merge_headers
from novita_self_test.response_artifacts import write_response_artifacts

from .helpers import write_service


def test_pytest_artifact_shortening_preserves_exception_tail() -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "suites/novita-api/pytest_artifacts.py"
    spec = importlib.util.spec_from_file_location("nvst_pytest_artifacts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = "prefix-" + ("x" * 5000) + "-Error code: 429 rate limit"
    shortened = module._shorten(value)
    assert shortened.startswith("prefix-")
    assert shortened.endswith("-Error code: 429 rate limit")
    assert "[truncated]" in shortened


def test_pytest_attempt_registers_summary_artifact_without_completeness_input(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    class ArtifactRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                '{"nodeid":"test_a","outcome":"passed","phase":"call"}\n',
                encoding="utf-8",
            )
            (artifact_dir / "summary.json").write_text("{}\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "kvv",
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "plugin"},
    )
    result = adapter_for("kvv", runner=ArtifactRunner()).run(ctx)
    assert str((ctx.suite_dir / "cases.jsonl").resolve()) in result.artifact_paths
    assert str((ctx.suite_dir / "summary.json").resolve()) in result.artifact_paths


def test_pytest_known_p0_failure_is_marked_non_blocking_only_when_exclusive(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class FailureRunner(FakeRunner):
        def __init__(self, nodeids: list[str]) -> None:
            self.nodeids = nodeids

        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                "".join(
                    json.dumps({"nodeid": nodeid, "outcome": "failed", "phase": "call"})
                    + "\n"
                    for nodeid in self.nodeids
                ),
                encoding="utf-8",
            )
            (artifact_dir / "summary.json").write_text("{}\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "novita_api",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "acceptance_non_blocking_case_patterns": ["test_stream_spec_prefill_error"],
        },
    )
    known = adapter_for(
        "novita_api", runner=FailureRunner(["test_stream_spec_prefill_error"])
    ).run(ctx)
    assert known.status == "failed"
    assert known.validity == "valid"
    assert known.coverage["acceptance_non_blocking"] is True

    unknown_ctx = replace(ctx, suite_dir=tmp_path / "run" / "unknown")
    mixed = adapter_for(
        "novita_api",
        runner=FailureRunner(["test_stream_spec_prefill_error", "test_unknown_failure"]),
    ).run(unknown_ctx)
    assert mixed.coverage.get("acceptance_non_blocking") is not True


DEEPSWE_PINS = {
    "rerun_policy": {"mode": "error_only", "max_reruns": 1, "scheduling": "batch_after_formal"},
    "transport": "local",
    "runtime_root": "/srv/kvh/deepswe",
    "pier_bin": "/srv/kvh/venvs/pier/bin/pier",
    "expected_deep_swe_commit": "435ee89ec2f2e2289f33b0da4f992f0b7b7266b9",
    "expected_pier_version": "0.3.1",
    "expected_tasks": 113,
    "rounds": 3,
    "max_reruns": 1,
    "max_concurrency": 113,
    "kimi_code_version": "0.29.0",
}


class FakeRunner:
    def run(self, command, *, cwd, env, timeout):
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")


class CapturingRunner(FakeRunner):
    def __init__(self) -> None:
        self.env = {}
        self.command = []

    def run(self, command, *, cwd, env, timeout):
        self.env = env
        self.command = command
        return super().run(command, cwd=cwd, env=env, timeout=timeout)


def artifact_dir_arg(command: list[str]) -> str:
    return next(token.split("=", 1)[1] for token in command if token.startswith("--artifact-dir="))


def context(
    tmp_path: Path,
    adapter: str,
    config: dict,
    *,
    concurrency_override: int | None = None,
) -> AdapterContext:
    service = ServiceAccess.load(write_service(tmp_path))
    suite = SuiteDefinition.from_dict(
        {
            "suite_id": f"{adapter}-suite",
            "adapter": adapter,
            "row_ids": ["R1"],
            "config": config,
        },
        index=0,
    )
    return AdapterContext(
        suite=suite,
        service=service,
        run_dir=tmp_path / "run",
        suite_dir=tmp_path / "run" / adapter,
        repository_root=tmp_path,
        concurrency_override=concurrency_override,
    )


@pytest.mark.parametrize("adapter", ["kvv", "novita_api", "walle"])
def test_pytest_adapter_builds_secret_free_command(tmp_path: Path, adapter: str) -> None:
    ctx = context(
        tmp_path,
        adapter,
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "pytest_artifacts"},
    )
    command = adapter_for(adapter, runner=FakeRunner()).build_command(ctx)
    assert command[:3] == ["python3", "-m", "pytest"]
    assert "novita_self_test.pytest_extra_headers" in command
    assert "--api-key" not in command
    assert any(token.startswith("--artifact-dir=") for token in command)
    assert "--artifact-dir" not in command  # one token; see PytestAdapter.build_command


def test_concurrency_override_rewrites_supported_runner_flags(tmp_path: Path) -> None:
    pytest_ctx = context(
        tmp_path,
        "kvv",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "pytest_args": ["-n", "50", "--dist", "load"],
        },
        concurrency_override=200,
    )
    pytest_command = adapter_for("kvv", runner=FakeRunner()).build_command(pytest_ctx)
    assert pytest_command[pytest_command.index("-n") + 1] == "200"

    inspect_ctx = context(
        tmp_path,
        "inspect",
        {"command": ["python3", "eval.py", "--max-connections", "50"]},
        concurrency_override=200,
    )
    inspect_command = adapter_for("inspect", runner=FakeRunner()).build_command(inspect_ctx)
    assert inspect_command[inspect_command.index("--max-connections") + 1] == "200"

    beam_ctx = context(
        tmp_path,
        "beam",
        {"command": ["python3", "beam.py", "--concurrency", "30"]},
        concurrency_override=200,
    )
    beam_command = adapter_for("beam", runner=FakeRunner()).build_command(beam_ctx)
    assert beam_command[beam_command.index("--concurrency") + 1] == "200"


def test_novita_api_plugin_loads_kvv_fixtures_without_option_conflict(tmp_path: Path) -> None:
    product_root = Path(__file__).resolve().parents[2]
    kvv_root = product_root / "vendor" / "kvv-k3"
    nodeid = (
        "suites/novita-api/tests/streaming/test_stream_spec.py::"
        "test_stream_spec_n_gt_1"
    )
    process = subprocess.run(
        [
            str(kvv_root / ".venv/bin/python"),
            "-m",
            "pytest",
            "-p",
            "pytest_artifacts",
            nodeid,
            "--collect-only",
            "--artifact-dir",
            str(tmp_path / "artifacts"),
        ],
        cwd=product_root,
        env={
            **os.environ,
            "PYTHONPATH": f"{product_root / 'suites/novita-api'}:{kvv_root}",
        },
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    assert "1 test collected" in process.stdout


def test_command_adapter_exports_standalone_harness_aliases(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    runner = CapturingRunner()
    ctx = context(tmp_path, "command", {"command": ["runner"]})
    adapter_for("command", runner=runner).run(ctx)
    assert runner.env["BASE_URL"] == "https://vendor.example/v1"
    assert runner.env["API_KEY"] == "runtime-only-secret"
    assert runner.env["SMODEL"] == "test-model"
    assert json.loads(runner.env["KIMI_EXTRA_HEADERS_JSON"]) == {"X-Route": "test"}
    assert runner.env["PYTHONPATH"].split(os.pathsep)[0] == str((tmp_path / "src").resolve())


def test_command_adapter_formats_literal_json_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    runner = CapturingRunner()
    ctx = context(
        tmp_path,
        "command",
        {
            "command": ["runner"],
            "env": {"NVST_KNOWN_SKIPS": '{{"nodeid":"approved"}}'},
        },
    )

    adapter_for("command", runner=runner).run(ctx)

    assert json.loads(runner.env["NVST_KNOWN_SKIPS"]) == {"nodeid": "approved"}


def test_python_script_command_uses_extra_header_wrapper(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    runner = CapturingRunner()
    ctx = context(tmp_path, "command", {"command": ["python3", "runner.py", "--flag"]})
    result = adapter_for("command", runner=runner).run(ctx)
    assert result.status == "passed"
    assert runner.command == [
        "python3",
        "-m",
        "novita_self_test.run_with_headers",
        "runner.py",
        "--flag",
    ]


def test_header_wrapper_exposes_script_sibling_imports(tmp_path: Path) -> None:
    script_dir = tmp_path / "tools"
    script_dir.mkdir()
    (script_dir / "reporting.py").write_text("VALUE = 'loaded'\n", encoding="utf-8")
    script = script_dir / "runner.py"
    script.write_text(
        "from reporting import VALUE\n"
        "assert VALUE == 'loaded'\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "novita_self_test.run_with_headers",
            str(script),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr


def test_header_merge_preserves_existing_and_applies_service_override() -> None:
    assert merge_headers(
        {"Authorization": "Bearer runtime", "X-Route": "old"},
        {"X-Route": "new", "X-Tenant": "tenant"},
    ) == {
        "Authorization": "Bearer runtime",
        "X-Route": "new",
        "X-Tenant": "tenant",
    }


def test_pytest_header_plugin_injects_httpx_headers(tmp_path: Path) -> None:
    product_root = Path(__file__).resolve().parents[2]
    kvv_python = product_root / "vendor/kvv-k3/.venv/bin/python"
    test_file = tmp_path / "test_headers.py"
    test_file.write_text(
        "import httpx\n"
        "from openai import OpenAI\n"
        "def test_headers():\n"
        "    client = httpx.Client(headers={'Existing': 'yes'})\n"
        "    assert client.headers['X-Route'] == 'route-a'\n"
        "    assert client.headers['Existing'] == 'yes'\n"
        "    client.close()\n"
        "    sdk = OpenAI(api_key='test', base_url='https://example.invalid/v1')\n"
        "    assert sdk.default_headers['X-Route'] == 'route-a'\n"
        "    sdk.close()\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [
            str(kvv_python),
            "-m",
            "pytest",
            "-p",
            "novita_self_test.pytest_extra_headers",
            str(test_file),
            "-q",
        ],
        env={
            **os.environ,
            "PYTHONPATH": str(product_root / "src"),
            "KIMI_EXTRA_HEADERS_JSON": '{"X-Route":"route-a"}',
        },
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr



def test_replay_adapter_recovers_complete_cases_after_runner_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    fixture = tmp_path / "image.jsonl"
    fixture.write_text(
        "\n".join(
            json.dumps(
                {
                    "expected_finish_reason": "stop",
                    "transformed_request": {"messages": []},
                }
            )
            for _ in range(2)
        )
        + "\n",
        encoding="utf-8",
    )

    class TimedOutReplayRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            for index in (1, 2):
                case_dir = output / f"{index:04d}-image-{index:04d}"
                case_dir.mkdir(parents=True, exist_ok=True)
                (case_dir / "response.body").write_text(
                    json.dumps(
                        {
                            "choices": [{"finish_reason": "stop"}],
                        }
                    ),
                    encoding="utf-8",
                )
            raise subprocess.TimeoutExpired(command, timeout or 900)

    ctx = context(
        tmp_path,
        "replay",
        {
            "kind": "image",
            "fixture": "image.jsonl",
            "timeout_seconds": 900,
            "failure_exit_is_valid": True,
        },
    )
    result = adapter_for("replay", runner=TimedOutReplayRunner()).run(ctx)
    assert result.status == "passed"
    assert result.validity == "valid"
    assert result.counts == {"total": 2, "passed": 2, "failed": 0}
    assert result.coverage["observed_cases"] == 2
    assert result.coverage["recovered_after_runner_timeout"] is True
    assert (ctx.suite_dir / "replay" / "verdict.json").is_file()


def test_replay_adapter_collects_image_fixture_contract(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    fixture = tmp_path / "image.jsonl"
    fixture.write_text(
        "\n".join(
            json.dumps(
                {
                    "expected_finish_reason": "stop",
                    "transformed_request": {
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "image_url", "image_url": {"url": "https://example.test/a"}}
                                    for _ in range(count)
                                ],
                            }
                        ]
                    },
                }
            )
            for count in (1, 1000)
        )
        + "\n",
        encoding="utf-8",
    )

    class ReplayRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "verdict.json").write_text(
                json.dumps({"status": "passed", "total": 2, "passed": 2, "failed": 0}),
                encoding="utf-8",
            )
            (output / "summary.jsonl").write_text(
                '{"index":1,"passed":true,"http_status":200}\n'
                '{"index":2,"passed":true,"http_status":200}\n',
                encoding="utf-8",
            )
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    ctx = context(
        tmp_path,
        "replay",
        {"kind": "image", "fixture": "image.jsonl", "failure_exit_is_valid": True},
    )
    result = adapter_for("replay", runner=ReplayRunner()).run(ctx)
    assert result.status == "passed"
    assert result.counts == {"total": 2, "passed": 2, "failed": 0}
    assert result.metrics["max_images"] == 1000
    assert result.coverage["observed_max_images"] == 1000
    assert "runtime-only-secret" not in " ".join(result.redacted_command)
    assert {Path(path).name for path in result.artifact_paths} >= {
        "verdict.json",
        "summary.jsonl",
    }


def test_replay_adapter_blocks_quota_even_after_partial_success(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class ReplayQuotaRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "verdict.json").write_text(
                json.dumps({"status": "failed", "total": 2, "passed": 1, "failed": 1}),
                encoding="utf-8",
            )
            (output / "summary.jsonl").write_text(
                '{"index":1,"passed":true,"http_status":200}\n'
                '{"index":2,"passed":false,"http_status":403,'
                '"error":{"message":"not enough balance"}}\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "replay",
        {"kind": "openclaw", "fixture": "unused.jsonl", "failure_exit_is_valid": True},
    )
    result = adapter_for("replay", runner=ReplayQuotaRunner()).run(ctx)
    assert result.status == "blocked"
    assert result.validity == "service_invalid"
    assert result.reason == "quota_exhausted"


def test_openclaw_response_artifacts_preserve_failure_body_and_redact_key(tmp_path: Path) -> None:
    response = httpx.Response(
        400,
        headers={
            "Content-Type": "application/json",
            "X-Request-Id": "request-1",
            "Set-Cookie": "must-not-persist",
        },
        content=b'{"error":"bad request","echo":"runtime-only-secret"}',
    )
    paths = write_response_artifacts(
        tmp_path,
        group="openclaw-responses",
        case_id="line-10",
        response=response,
        api_key="runtime-only-secret",
    )

    body = (tmp_path / "openclaw-responses/line-10/response.body").read_text()
    headers = json.loads(
        (tmp_path / "openclaw-responses/line-10/response.headers.json").read_text()
    )
    metadata = json.loads(
        (tmp_path / "openclaw-responses/line-10/response.meta.json").read_text()
    )
    assert "runtime-only-secret" not in body
    assert "[REDACTED]" in body
    assert headers == {
        "content-type": "application/json",
        "x-request-id": "request-1",
    }
    assert metadata["status_code"] == 400
    assert {path.name for path in paths} == {
        "response.body",
        "response.headers.json",
        "response.meta.json",
    }


def test_pytest_adapter_collects_declared_artifact_globs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class ArtifactRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            response_dir = artifact_dir / "openclaw-responses" / "line-10"
            response_dir.mkdir(parents=True)
            (response_dir / "response.body").write_text("failure body", encoding="utf-8")
            (artifact_dir / "cases.jsonl").write_text(
                '{"nodeid":"test_openclaw.py::test_case","outcome":"failed","phase":"call"}\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "novita_api",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "artifact_globs": ["openclaw-responses/*/response.body"],
        },
    )
    result = adapter_for("novita_api", runner=ArtifactRunner()).run(ctx)
    assert any(path.endswith("openclaw-responses/line-10/response.body") for path in result.artifact_paths)


def test_pytest_setup_failure_is_execution_invalid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class SetupFailureRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                '{"nodeid":"tests/example.py::test_case","outcome":"failed",'
                '"phase":"setup"}\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "kvv",
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "plugin"},
    )
    result = adapter_for("kvv", runner=SetupFailureRunner()).run(ctx)
    assert result.counts["error"] == 1
    assert result.counts["failed"] == 0
    assert result.status == "failed"
    assert result.validity == "execution_invalid"


def test_pytest_http_401_failure_is_service_invalid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class CredentialFailureRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                json.dumps(
                    {
                        "nodeid": "tests/example.py::test_case",
                        "outcome": "failed",
                        "phase": "call",
                        "longrepr": "HTTPStatusError: Client error 401 Unauthorized",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "kvv",
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "plugin"},
    )
    result = adapter_for("kvv", runner=CredentialFailureRunner()).run(ctx)
    assert result.status == "blocked"
    assert result.validity == "service_invalid"
    assert result.reason == "credential_invalid"


def test_pytest_quota_failure_is_service_invalid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class QuotaFailureRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                json.dumps(
                    {
                        "nodeid": "tests/example.py::test_passed",
                        "outcome": "passed",
                        "phase": "call",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "nodeid": "tests/example.py::test_quota",
                        "outcome": "failed",
                        "phase": "call",
                        "longrepr": "HTTP 403: not enough balance",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "kvv",
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "plugin"},
    )
    result = adapter_for("kvv", runner=QuotaFailureRunner()).run(ctx)
    assert result.status == "blocked"
    assert result.validity == "service_invalid"
    assert result.reason == "quota_exhausted"


def test_pytest_partial_timeout_requires_batch_rerun(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class PartialTimeoutRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            records = [
                {
                    "nodeid": "tests/example.py::test_passed",
                    "outcome": "passed",
                    "phase": "call",
                },
                {
                    "nodeid": "tests/example.py::test_timeout",
                    "outcome": "failed",
                    "phase": "call",
                    "longrepr": "httpx.ReadTimeout: The read operation timed out",
                },
            ]
            (artifact_dir / "cases.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "kvv",
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "plugin"},
    )
    result = adapter_for("kvv", runner=PartialTimeoutRunner()).run(ctx)
    assert result.status == "failed"
    assert result.validity == "execution_invalid"
    assert result.reason == "retryable_case_failure"


# Real pytest longrepr shape: it prints the failing function's source ("> " / indented lines),
# then the exception ("E " lines). Only the exception lines decide retryability.
_SOURCE_WITH_EXCEPT = (
    "    def test_case(client):\n"
    "        try:\n"
    "            stream = client.chat.completions.create(**kwargs)\n"
    "        except (\n"
    "            openai.APIConnectionError,\n"
    "            openai.APITimeoutError,\n"
    "        ) as exc:\n"
    "            pytest.fail(f'infrastructure/API error: {exc}')\n"
)


@pytest.mark.parametrize(
    "error_lines,expected",
    [
        (
            "E       AssertionError: prompt_tokens mismatch\n"
            "E       assert False\n"
            "E        +  where False = _prompt_tokens_are_acceptable(expected=99, actual=12)",
            ("failed", "valid", None),
        ),
        (
            "E           Failed: infrastructure/API error for case k3_vision: Connection error.",
            ("failed", "execution_invalid", "retryable_case_failure"),
        ),
    ],
    ids=["assertion-is-real-failure", "infra-error-is-retryable"],
)
def test_pytest_classifies_exception_lines_not_test_source(
    tmp_path: Path, monkeypatch, error_lines: str, expected: tuple
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class SourceInLongreprRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "nodeid": "tests/example.py::test_case",
                "outcome": "failed",
                "phase": "call",
                "longrepr": _SOURCE_WITH_EXCEPT + error_lines,
            }
            (artifact_dir / "cases.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "kvv",
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "plugin"},
    )
    result = adapter_for("kvv", runner=SourceInLongreprRunner()).run(ctx)
    assert (result.status, result.validity, result.reason) == expected


def test_pytest_batch_rerun_recovers_only_retryable_case(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class BatchRunner(FakeRunner):
        def __init__(self) -> None:
            self.commands = []

        def run(self, command, *, cwd, env, timeout):
            self.commands.append(command)
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            if len(self.commands) == 1:
                records = [
                    {"nodeid": "tests/example.py::test_ok", "outcome": "passed", "phase": "call"},
                    {
                        "nodeid": "tests/example.py::test_timeout",
                        "outcome": "failed",
                        "phase": "call",
                        "longrepr": "OpenAI Error code: 429 - rate limit exceeded",
                    },
                ]
                returncode = 1
            else:
                records = [
                    {
                        "nodeid": "tests/example.py::test_timeout",
                        "outcome": "passed",
                        "phase": "call",
                    }
                ]
                returncode = 0
            (artifact_dir / "cases.jsonl").write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, returncode, stdout="", stderr="")

    runner = BatchRunner()
    ctx = context(
        tmp_path,
        "kvv",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "rerun_policy": {
                "mode": "invalid_only",
                "max_reruns": 3,
                "scheduling": "batch_after_formal",
            },
        },
    )
    result = adapter_for("kvv", runner=runner).run(ctx)
    assert result.status == "passed"
    assert result.counts == {"total": 2, "passed": 2, "failed": 0, "skipped": 0, "error": 0}
    assert result.coverage["rerun_recovered_cases"] == 1
    assert len(runner.commands) == 2
    assert "tests/example.py::test_timeout" in runner.commands[1]
    assert "tests/example.py::test_ok" not in runner.commands[1]


def test_pytest_any_pass_reruns_explicit_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class AnyPassRunner(FakeRunner):
        def __init__(self) -> None:
            self.calls = 0

        def run(self, command, *, cwd, env, timeout):
            self.calls += 1
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            outcome = "failed" if self.calls == 1 else "passed"
            (artifact_dir / "cases.jsonl").write_text(
                json.dumps(
                    {
                        "nodeid": "tests/example.py::test_behavior",
                        "outcome": outcome,
                        "phase": "call",
                        "longrepr": "behavior mismatch" if outcome == "failed" else "",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command, 1 if outcome == "failed" else 0, stdout="", stderr=""
            )

    runner = AnyPassRunner()
    ctx = context(
        tmp_path,
        "kvv",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "rerun_policy": {
                "mode": "any_pass",
                "max_reruns": 3,
                "scheduling": "batch_after_formal",
            },
        },
    )
    result = adapter_for("kvv", runner=runner).run(ctx)
    assert result.status == "passed"
    assert result.validity == "valid"
    assert result.counts["passed"] == 1
    assert result.coverage["rerun_recovered_cases"] == 1
    assert runner.calls == 2


@pytest.mark.parametrize(
    "longrepr,expected",
    [
        ("E       AssertionError: behavior mismatch", ("failed", "valid")),
        ("E       httpx.ReadTimeout: The read operation timed out", ("failed", "execution_invalid")),
    ],
    ids=["persistent-model-failure-is-a-valid-fail", "persistent-timeout-is-invalid"],
)
def test_pytest_any_pass_exhausted_reruns_keep_failure_class(
    tmp_path: Path, monkeypatch, longrepr: str, expected: tuple
) -> None:
    """any_pass reruns every failure, but exhausting the reruns must not turn a deterministic model
    failure into execution_invalid; only stability failures stay invalid."""
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class AlwaysFailRunner(FakeRunner):
        def __init__(self) -> None:
            self.calls = 0

        def run(self, command, *, cwd, env, timeout):
            self.calls += 1
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            record = {"nodeid": "tests/example.py::test_behavior", "outcome": "failed", "phase": "call",
                      "longrepr": longrepr}
            (artifact_dir / "cases.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    runner = AlwaysFailRunner()
    ctx = context(
        tmp_path,
        "kvv",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "rerun_policy": {"mode": "any_pass", "max_reruns": 3, "scheduling": "batch_after_formal"},
        },
    )
    result = adapter_for("kvv", runner=runner).run(ctx)
    assert runner.calls == 4  # formal + 3 reruns, for both failure classes
    assert (result.status, result.validity) == expected


def test_pytest_records_nodeids_selectable_from_suite_cwd(tmp_path: Path) -> None:
    """Rerun waves pass recorded node ids back to pytest as selectors from the suite's cwd. If the
    adapter's command lets pytest pick this repository as rootdir (instead of vendor/kvv-k3), the
    ids gain a ``vendor/kvv-k3/`` prefix and every rerun silently collects zero tests."""
    root = Path(__file__).resolve().parents[2]
    python = root / "vendor/kvv-k3/.venv/bin/python"
    if not python.exists():
        pytest.skip("vendor/kvv-k3 environment not synced (uv sync)")
    from novita_self_test.config import ModelProfile

    profile = ModelProfile.load("kimi-k3", allow_blocked=True)
    suite = next(s for s in profile.suites if s.suite_id == "kvv-prompt-tokens")
    service = ServiceAccess.load(write_service(tmp_path))
    # Like a real run: the artifact directory exists and lives under this repository's runs/.
    suite_dir = root / "runs" / f"_rootdir_probe_{os.getpid()}" / "suite"
    suite_dir.mkdir(parents=True, exist_ok=True)
    ctx = AdapterContext(suite=suite, service=service, run_dir=suite_dir.parent,
                         suite_dir=suite_dir, repository_root=root)
    command = adapter_for("kvv", runner=FakeRunner()).build_command(ctx)
    flags_with_value = {"-n", "--dist"}  # xdist options from pytest_args; not needed to collect
    command = [t for i, t in enumerate(command)
               if t not in flags_with_value and (i == 0 or command[i - 1] not in flags_with_value)]
    try:
        probe = subprocess.run(
            command + ["--collect-only", "-q", "-o", "addopts="],
            cwd=root / str(suite.config["cwd"]),
            env={**os.environ, "PYTHONPATH": f"{root}/src:{root}/suites/novita-api:{root}/vendor/kvv-k3"},
            capture_output=True, text=True, timeout=300,
        )
    finally:
        shutil.rmtree(suite_dir.parent, ignore_errors=True)
    collected = [line for line in probe.stdout.splitlines() if "::" in line]
    assert collected, probe.stdout[-1500:] + probe.stderr[-1500:]
    assert all(line.startswith("tests/prompt_tokens/") for line in collected), collected[:3]

def test_pytest_adapter_repeats_and_aggregates_independent_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class RepeatRunner(FakeRunner):
        def __init__(self) -> None:
            self.artifact_dirs = []

        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            self.artifact_dirs.append(artifact_dir)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                '{"nodeid":"tests/example.py::test_case","outcome":"passed",'
                '"phase":"call"}\n',
                encoding="utf-8",
            )
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    runner = RepeatRunner()
    ctx = context(
        tmp_path,
        "kvv",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "repeat": 3,
        },
    )
    ctx = replace(
        ctx,
        suite=replace(
            ctx.suite,
            completeness={
                "expected_repeats": 3,
                "required_artifacts": ["repeat-summary.json"],
                "per_repeat": {
                    "expected_counts": {
                        "total": 1,
                        "passed": 1,
                        "failed": 0,
                        "skipped": 0,
                        "error": 0,
                    },
                    "required_artifacts": ["cases.jsonl"],
                },
            },
        ),
    )

    result = adapter_for("kvv", runner=runner).run(ctx)
    result = enforce_completeness(result, ctx.suite.completeness)

    assert result.status == "passed"
    assert result.validity == "valid"
    assert result.counts == {
        "total": 3,
        "passed": 3,
        "failed": 0,
        "skipped": 0,
        "error": 0,
    }
    assert result.metrics == {"repeat_passed": 3, "repeat_failed": 0}
    assert result.coverage["observed_repeats"] == 3
    assert sorted(path.parent.name for path in runner.artifact_dirs) == [
        "repeat-01",
        "repeat-02",
        "repeat-03",
    ]
    assert {path.name for path in runner.artifact_dirs} == {"formal"}
    summary = json.loads((ctx.suite_dir / "repeat-summary.json").read_text())
    assert summary["requested_repeats"] == 3
    assert summary["observed_repeats"] == 3
    assert len(summary["iterations"]) == 3


def test_pytest_adapter_repeat_continues_after_valid_failure(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class RepeatRunner(FakeRunner):
        def __init__(self) -> None:
            self.calls = 0

        def run(self, command, *, cwd, env, timeout):
            self.calls += 1
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            outcome = "failed" if artifact_dir.parent.name == "repeat-02" else "passed"
            (artifact_dir / "cases.jsonl").write_text(
                json.dumps(
                    {
                        "nodeid": "tests/example.py::test_case",
                        "outcome": outcome,
                        "phase": "call",
                        "longrepr": "behavior mismatch" if outcome == "failed" else "",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command, 1 if outcome == "failed" else 0, stdout="", stderr=""
            )

    runner = RepeatRunner()
    ctx = context(
        tmp_path,
        "kvv",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "repeat": 3,
        },
    )
    ctx = replace(
        ctx,
        suite=replace(
            ctx.suite,
            completeness={
                "expected_repeats": 3,
                "per_repeat": {"required_artifacts": ["cases.jsonl"]},
            },
        ),
    )

    result = adapter_for("kvv", runner=runner).run(ctx)

    assert runner.calls == 3
    assert result.status == "failed"
    assert result.validity == "valid"
    assert result.reason == "repeat_failed"
    assert result.counts["passed"] == 2
    assert result.counts["failed"] == 1
    assert result.coverage["observed_repeats"] == 3


def test_pytest_valid_case_failure_is_not_runner_contract_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class FailedCaseRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                json.dumps(
                    {
                        "nodeid": "tests/example.py::test_behavior",
                        "outcome": "failed",
                        "phase": "call",
                        "longrepr": "known model behavior mismatch",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    ctx = context(
        tmp_path,
        "kvv",
        {"python": "python3", "paths": ["tests/example"], "artifact_plugin": "plugin"},
    )
    result = adapter_for("kvv", runner=FailedCaseRunner()).run(ctx)

    assert result.status == "failed"
    assert result.validity == "valid"
    assert result.reason is None


def test_pytest_adapter_runs_all_formal_rounds_after_execution_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class SetupFailureRunner(FakeRunner):
        def __init__(self) -> None:
            self.calls = 0

        def run(self, command, *, cwd, env, timeout):
            self.calls += 1
            artifact_dir = Path(artifact_dir_arg(command))
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "cases.jsonl").write_text(
                '{"nodeid":"tests/example.py::test_case","outcome":"failed",'
                '"phase":"setup"}\n',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")

    runner = SetupFailureRunner()
    ctx = context(
        tmp_path,
        "kvv",
        {
            "python": "python3",
            "paths": ["tests/example"],
            "artifact_plugin": "plugin",
            "repeat": 3,
        },
    )
    ctx = replace(
        ctx,
        suite=replace(
            ctx.suite,
            completeness={
                "expected_repeats": 3,
                "per_repeat": {"required_artifacts": ["cases.jsonl"]},
            },
        ),
    )

    result = adapter_for("kvv", runner=runner).run(ctx)

    assert runner.calls == 3
    assert result.status == "failed"
    assert result.validity == "execution_invalid"
    assert result.coverage["observed_repeats"] == 3


def test_command_adapter_treats_missing_structured_result_as_contract_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class FailedRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="failed")

    ctx = context(tmp_path, "beam", {"command": ["runner"]})
    result = adapter_for("beam", runner=FailedRunner()).run(ctx)

    assert result.status == "failed"
    assert result.validity == "execution_invalid"
    assert result.reason == "runner_contract_invalid"


@pytest.mark.parametrize("adapter", ["beam", "stress"])
def test_command_adapters_accept_fake_runner(tmp_path: Path, monkeypatch, adapter: str) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    ctx = context(tmp_path, adapter, {"command": ["runner", "--out", "{suite_dir}"]})
    result = adapter_for(adapter, runner=FakeRunner()).run(ctx)
    assert result.status == "passed"
    assert result.validity == "valid"
    assert result.redacted_command[0] == "runner"
    assert "runtime-only-secret" not in " ".join(result.redacted_command)


def test_inspect_adapter_parses_complete_fake_eval(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            Path(command[-1]).touch()
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    score = SimpleNamespace(
        metrics={"accuracy": SimpleNamespace(value=0.887)},
        scored_samples=1000,
        unscored_samples=0,
    )
    log = SimpleNamespace(
        status="success",
        results=SimpleNamespace(
            scores=[score], total_samples=1000, completed_samples=1000
        ),
    )
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda _: log)
    eval_path = tmp_path / "run" / "inspect" / "fake.eval"
    ctx = context(
        tmp_path,
        "inspect",
        {
            "command": ["runner", str(eval_path)],
            "official_score": 0.90,
            "tolerance_pp": 2.0,
        },
    )
    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)
    assert result.status == "passed"
    assert result.metrics["score"] == 0.887
    assert result.coverage["observed_trials"] == 1000


def test_inspect_adapter_scores_each_epoch_not_last_sample_only(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            Path(command[-1]).touch()
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    samples = [
        SimpleNamespace(
            id=sample_id,
            epoch=epoch,
            error=None,
            scores={"score": SimpleNamespace(value=value)},
            output=SimpleNamespace(metadata={}),
        )
        for sample_id, epoch, value in (
            ("a", 1, "I"), ("a", 2, "C"),
            ("b", 1, "C"), ("b", 2, "C"),
        )
    ]
    log = SimpleNamespace(
        status="success",
        results=SimpleNamespace(
            scores=[SimpleNamespace(metrics={"accuracy": SimpleNamespace(value=0.75)})],
            total_samples=4,
        ),
        samples=samples,
    )
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda _: log)
    eval_path = tmp_path / "run" / "inspect" / "fake.eval"
    ctx = context(
        tmp_path,
        "inspect",
        {"command": ["runner", str(eval_path)], "official_score": 0.75},
    )
    ctx = replace(
        ctx,
        suite=replace(
            ctx.suite,
            completeness={"expected_counts": {"total": 4, "unscored": 0}},
        ),
    )
    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)
    result = enforce_completeness(result, ctx.suite.completeness)
    assert result.status == "passed"
    assert result.validity == "valid"
    assert result.counts == {"total": 4, "completed": 4, "scored": 4, "unscored": 0}
    assert result.coverage["observed_trials"] == 4
    assert result.metrics["score"] == pytest.approx(0.75)


def test_inspect_batch_rerun_replaces_only_error_sample(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def __init__(self) -> None:
            self.commands = []

        def run(self, command, *, cwd, env, timeout):
            self.commands.append(command)
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    def sample(sample_id: str, value: str | None):
        return SimpleNamespace(
            id=sample_id,
            error=None if value is not None else SimpleNamespace(message="timeout"),
            scores={} if value is None else {"score": SimpleNamespace(value=value)},
            output=SimpleNamespace(metadata={}),
        )

    def log(samples, accuracy):
        score = SimpleNamespace(
            metrics={"accuracy": SimpleNamespace(value=accuracy)},
            scored_samples=sum(bool(item.scores) for item in samples),
            unscored_samples=0,
        )
        return SimpleNamespace(
            status="success",
            results=SimpleNamespace(
                scores=[score],
                total_samples=len(samples),
                completed_samples=sum(bool(item.scores) for item in samples),
            ),
            samples=samples,
        )

    formal = log([sample("retry-me", None), sample("explicit-wrong", "I")], 0.0)
    retry = log([sample("retry-me", "C")], 1.0)
    monkeypatch.setattr(
        "novita_self_test.adapters.builtins.InspectAdapter._load_eval_log",
        staticmethod(
            lambda directory: (
                (retry, directory / "retry.eval")
                if directory.name.startswith("rerun-")
                else (formal, directory / "formal.eval")
            )
        ),
    )
    runner = InspectRunner()
    ctx = context(
        tmp_path,
        "inspect",
        {
            "command": ["runner"],
            "official_score": 0.40,
            "tolerance_pp": 2.0,
            "tolerance_mode": "lower_bound",
            "rerun_policy": {
                "mode": "invalid_only",
                "max_reruns": 3,
                "scheduling": "batch_after_formal",
            },
        },
    )
    result = adapter_for("inspect", runner=runner).run(ctx)
    assert result.status == "passed"
    assert result.validity == "valid"
    assert result.metrics["score"] == pytest.approx(0.5)
    assert result.coverage["rerun_recovered_samples"] == 1
    assert len(runner.commands) == 2
    assert runner.commands[1][-2:] == ["--sample-id", "retry-me"]
    assert "explicit-wrong" not in runner.commands[1]


def test_inspect_adapter_blocks_quota_without_scoring(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            Path(command[-1]).touch()
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    sample = SimpleNamespace(
        id="quota",
        error=SimpleNamespace(message="HTTP 403 not enough balance"),
        scores={},
        output=None,
    )
    score = SimpleNamespace(
        metrics={"accuracy": SimpleNamespace(value=0.0)},
        scored_samples=0,
        unscored_samples=1,
    )
    log = SimpleNamespace(
        status="success",
        results=SimpleNamespace(scores=[score], total_samples=1, completed_samples=0),
        samples=[sample],
    )
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda _: log)
    eval_path = tmp_path / "run" / "inspect" / "fake.eval"
    ctx = context(
        tmp_path,
        "inspect",
        {
            "command": ["runner", str(eval_path)],
            "official_score": 0.8,
            "rerun_policy": {
                "mode": "runner_owned",
                "max_reruns": 3,
                "scheduling": "immediate",
            },
        },
    )
    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)
    assert result.status == "blocked"
    assert result.validity == "service_invalid"
    assert result.reason == "quota_exhausted"
    assert result.metrics == {}


@pytest.mark.parametrize(
    ("accuracy", "official_scores", "expected_status"),
    [
        (0.815606936416185, [0.79], "passed"),
        (0.90, [0.87, 0.83], "passed"),
        (0.81, [0.87, 0.83], "passed"),
        (0.809, [0.87, 0.83], "failed"),
    ],
)
def test_inspect_lower_bound_tolerance_is_one_sided(
    tmp_path: Path,
    monkeypatch,
    accuracy: float,
    official_scores: list[float],
    expected_status: str,
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            Path(command[-1]).touch()
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    score = SimpleNamespace(
        metrics={"accuracy": SimpleNamespace(value=accuracy)},
        scored_samples=10,
        unscored_samples=0,
    )
    log = SimpleNamespace(
        status="success",
        results=SimpleNamespace(scores=[score], total_samples=10, completed_samples=10),
    )
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda _: log)
    eval_path = tmp_path / "run" / "inspect" / "fake.eval"
    ctx = context(
        tmp_path,
        "inspect",
        {
            "command": ["runner", str(eval_path)],
            "official_scores": official_scores,
            "tolerance_pp": 2.0,
            "tolerance_mode": "lower_bound",
        },
    )

    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)

    assert result.status == expected_status
    assert result.validity == "valid"
    if expected_status == "failed":
        assert result.reason == "benchmark_below_tolerance"
        assert "below official lower bound" in str(result.failure_summary)


def test_inspect_adapter_accepts_only_pinned_sample_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            Path(command[-1]).touch()
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    known_errors = {"bad-a", "bad-b", "bad-c"}
    score = SimpleNamespace(
        metrics={"accuracy": SimpleNamespace(value=0.8363636363636363)},
        scored_samples=110,
        unscored_samples=0,
    )
    samples = [
        SimpleNamespace(id=f"ok-{index}", error=None, scores={"score": object()})
        for index in range(110)
    ] + [
        SimpleNamespace(id=sample_id, error=SimpleNamespace(message="known"), scores={})
        for sample_id in sorted(known_errors)
    ]
    log = SimpleNamespace(
        status="success",
        results=SimpleNamespace(scores=[score], total_samples=113, completed_samples=110),
        samples=samples,
    )
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda _: log)
    eval_path = tmp_path / "run" / "inspect" / "fake.eval"
    ctx = context(
        tmp_path,
        "inspect",
        {
            "command": ["runner", str(eval_path)],
            "official_scores": [0.87, 0.83],
            "tolerance_pp": 2.0,
        },
    )
    ctx = replace(
        ctx,
        suite=replace(
            ctx.suite,
            completeness={"allowed_error_sample_ids": sorted(known_errors)},
        ),
    )

    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)

    assert result.status == "passed"
    assert result.failure_summary is None
    assert result.coverage["observed_trials"] == 110
    assert set(result.coverage["observed_error_sample_ids"]) == known_errors

    score.scored_samples = 113
    log.results.completed_samples = 113
    log.samples = [
        SimpleNamespace(id=f"ok-{index}", error=None, scores={"score": object()})
        for index in range(113)
    ]
    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)
    assert result.status == "passed"
    assert result.coverage["observed_trials"] == 113
    assert result.coverage["observed_error_sample_ids"] == []

    score.scored_samples = 110
    log.results.completed_samples = 110
    log.samples = samples
    samples[-1].id = "unexpected"
    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)
    assert result.status == "failed"
    assert result.validity == "execution_invalid"
    assert result.reason == "inspect_samples_incomplete"


def test_inspect_adapter_rejects_engine_overloaded_as_unscored(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            Path(command[-1]).touch()
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    score = SimpleNamespace(
        metrics={"accuracy": SimpleNamespace(value=0.99)},
        scored_samples=3,
        unscored_samples=0,
    )
    samples = [
        SimpleNamespace(
            id="overloaded",
            error=None,
            scores={"score": object()},
            output=SimpleNamespace(
                metadata={"original_finish_reason": "engine_overloaded"}
            ),
        ),
        SimpleNamespace(
            id="normal-a",
            error=None,
            scores={"score": object()},
            output=SimpleNamespace(metadata={"original_finish_reason": "stop"}),
        ),
        SimpleNamespace(
            id="normal-b",
            error=None,
            scores={"score": object()},
            output=SimpleNamespace(metadata={}),
        ),
    ]
    log = SimpleNamespace(
        status="success",
        results=SimpleNamespace(scores=[score], total_samples=3, completed_samples=3),
        samples=samples,
    )
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda _: log)
    eval_path = tmp_path / "run" / "inspect" / "fake.eval"
    ctx = context(
        tmp_path,
        "inspect",
        {
            "command": ["runner", str(eval_path)],
            "official_score": 0.80,
            "tolerance_pp": 2.0,
        },
    )

    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)

    assert result.status == "failed"
    assert result.validity == "execution_invalid"
    assert result.reason == "inspect_invalid_finish_reason"
    assert result.counts == {
        "total": 3,
        "completed": 2,
        "scored": 2,
        "unscored": 1,
    }
    assert result.coverage["observed_invalid_finish_reason_sample_ids"] == [
        "overloaded"
    ]
    assert result.coverage["invalid_finish_reason_counts"] == {
        "engine_overloaded": 1
    }


def test_inspect_adapter_rejects_exhausted_engine_overload_error(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InspectRunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            Path(command[-1]).touch()
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    known_error = "known-error"
    score = SimpleNamespace(
        metrics={"accuracy": SimpleNamespace(value=0.99)},
        scored_samples=2,
        unscored_samples=0,
    )
    samples = [
        SimpleNamespace(
            id="overloaded",
            error=SimpleNamespace(message="service returned engine_overloaded"),
            scores={},
            output=None,
        ),
        SimpleNamespace(
            id=known_error,
            error=SimpleNamespace(message="allowed request error"),
            scores={},
            output=None,
        ),
        SimpleNamespace(
            id="normal-a",
            error=None,
            scores={"score": object()},
            output=SimpleNamespace(metadata={}),
        ),
        SimpleNamespace(
            id="normal-b",
            error=None,
            scores={"score": object()},
            output=SimpleNamespace(metadata={}),
        ),
    ]
    log = SimpleNamespace(
        status="success",
        results=SimpleNamespace(scores=[score], total_samples=4, completed_samples=2),
        samples=samples,
    )
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda _: log)
    eval_path = tmp_path / "run" / "inspect" / "fake.eval"
    ctx = context(
        tmp_path,
        "inspect",
        {
            "command": ["runner", str(eval_path)],
            "official_score": 0.80,
            "tolerance_pp": 2.0,
        },
    )
    ctx = replace(
        ctx,
        suite=replace(
            ctx.suite,
            completeness={"allowed_error_sample_ids": [known_error]},
        ),
    )

    result = adapter_for("inspect", runner=InspectRunner()).run(ctx)

    assert result.status == "failed"
    assert result.validity == "execution_invalid"
    assert result.reason == "inspect_invalid_finish_reason"
    assert result.counts == {
        "total": 4,
        "completed": 2,
        "scored": 2,
        "unscored": 1,
    }
    assert result.coverage["observed_error_sample_ids"] == [known_error, "overloaded"]
    assert result.coverage["observed_invalid_finish_reason_sample_ids"] == [
        "overloaded"
    ]


def test_code_bench_finalize_only_repairs_crashed_finalize(tmp_path: Path) -> None:
    product_root = Path(__file__).resolve().parents[2]
    ctx = context(tmp_path, "code_bench", {"run_contract": "contract.json"})
    ctx = replace(ctx, repository_root=product_root)
    adapter = adapter_for("code_bench", runner=FakeRunner())

    command = adapter.build_command(ctx)
    assert "--finalize-only" not in command

    ctx.suite_dir.mkdir(parents=True, exist_ok=True)
    (ctx.suite_dir / "config.snapshot.json").write_text("{}", encoding="utf-8")
    (ctx.suite_dir / "archive").mkdir(exist_ok=True)
    repair = adapter.build_command(ctx)
    assert "--finalize-only" in repair
    contract = repair[repair.index("--contract") + 1]
    assert contract == str(ctx.suite_dir / "code-bench-run-contract.json")


def test_code_bench_and_deepswe_commands_use_owned_wrappers(tmp_path: Path) -> None:
    code = context(
        tmp_path,
        "code_bench",
        {"run_contract": "contract.json"},
    )
    product_root = Path(__file__).resolve().parents[2]
    code = replace(code, repository_root=product_root)
    code_command = adapter_for("code_bench", runner=FakeRunner()).build_command(code)
    assert code_command[1:3] == ["-m", "novita_self_test.adapters.codebench_run"]
    contract = code.suite_dir / "code-bench-run-contract.json"
    assert contract.is_file()
    validation = subprocess.run(
        [str(product_root / "suites/kimi-code-bench/scripts/validate-run-contract.sh"), str(contract)],
        cwd=product_root / "suites/kimi-code-bench",
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stderr

    deep = context(tmp_path, "deepswe", dict(DEEPSWE_PINS))
    deep_command = adapter_for("deepswe", runner=FakeRunner()).build_command(deep)
    assert deep_command[1:3] == ["-m", "novita_self_test.adapters.deepswe_remote"]
    assert "--transport" not in deep_command
    assert deep_command[deep_command.index("--runtime-root") + 1] == "/srv/kvh/deepswe"
    assert deep_command[deep_command.index("--expected-pier-version") + 1] == "0.3.1"
    assert deep_command[deep_command.index("--expected-tasks") + 1] == "113"


def test_deepswe_pier_uses_a_proxy_free_docker_client_config(tmp_path: Path) -> None:
    """The verifier must see no proxy variables, not blank ones.

    Python's ``getproxies_environment`` pops a proxy when the matching
    ``*_proxy`` variable is set to an empty string, so a blank override breaks
    verifiers that set their own proxy; only true absence works. A Docker client
    config without a ``proxies`` block leaves the variables unset.
    """
    directory = tmp_path / "docker-client-config"
    path = proxy_free_docker_config(directory)

    assert path == directory / "config.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {}
    # No credential material is copied into the generated client config.
    assert "auth" not in path.read_text(encoding="utf-8")


def test_deepswe_command_ignores_explicit_concurrency_override(tmp_path: Path) -> None:
    deep = context(
        tmp_path,
        "deepswe",
        {**DEEPSWE_PINS, "concurrency": 113},
        concurrency_override=10,
    )
    command = adapter_for("deepswe", runner=FakeRunner()).build_command(deep)
    assert command[command.index("--concurrency") + 1] == "113"


def test_deepswe_command_writes_supplemental_plan(tmp_path: Path) -> None:
    plan = {
        "source_run_id": "source-run",
        "round_tasks": [{"round": 1, "task": "task-a"}],
    }
    deep = context(
        tmp_path,
        "deepswe",
        {**DEEPSWE_PINS, "concurrency": 113},
        concurrency_override=5,
    )
    deep = replace(deep, deepswe_supplemental_plan=plan)
    command = adapter_for("deepswe", runner=FakeRunner()).build_command(deep)
    plan_path = Path(command[command.index("--supplemental-plan") + 1])
    assert json.loads(plan_path.read_text(encoding="utf-8")) == plan


def test_deepswe_supplemental_command_uses_override_concurrency(tmp_path: Path) -> None:
    # Supplemental recovery is gated on an explicit low --concurrency; the
    # adapter must pass it through instead of the manifest's formal value.
    plan = {
        "source_run_id": "source-run",
        "round_tasks": [{"round": 1, "task": "task-a"}],
    }
    deep = context(
        tmp_path,
        "deepswe",
        {**DEEPSWE_PINS, "concurrency": 50},
        concurrency_override=4,
    )
    deep = replace(deep, deepswe_supplemental_plan=plan)
    command = adapter_for("deepswe", runner=FakeRunner()).build_command(deep)
    assert command[command.index("--concurrency") + 1] == "4"
    assert command[command.index("--max-concurrency") + 1] == "113"


def test_deepswe_command_uses_manifest_concurrency_without_override(tmp_path: Path) -> None:
    deep = context(tmp_path, "deepswe", {**DEEPSWE_PINS, "concurrency": 113})
    command = adapter_for("deepswe", runner=FakeRunner()).build_command(deep)
    assert command[command.index("--concurrency") + 1] == "113"
    assert command[command.index("--max-concurrency") + 1] == "113"


def test_deepswe_command_uses_acceptance_round_override(tmp_path: Path) -> None:
    deep = context(tmp_path, "deepswe", {**DEEPSWE_PINS, "concurrency": 113})
    deep = replace(deep, acceptance_mode="simple", deepswe_rounds=1)
    command = adapter_for("deepswe", runner=FakeRunner()).build_command(deep)
    assert command[command.index("--rounds") + 1] == "1"


def test_deepswe_command_allows_override_above_global_acceptance_budget(tmp_path: Path) -> None:
    deep = context(tmp_path, "deepswe", {**DEEPSWE_PINS, "concurrency": 113})
    deep = replace(deep, concurrency_override=114)
    command = adapter_for("deepswe", runner=FakeRunner()).build_command(deep)
    assert command[command.index("--concurrency") + 1] == "113"


def test_deepswe_command_requires_one_retry(tmp_path: Path) -> None:
    deep = context(
        tmp_path,
        "deepswe",
        {**DEEPSWE_PINS, "concurrency": 113, "max_reruns": 2},
    )
    with pytest.raises(ConfigError, match="max_reruns is fixed at 1"):
        adapter_for("deepswe", runner=FakeRunner()).build_command(deep)


def test_deepswe_command_rejects_missing_runtime_pins(tmp_path: Path) -> None:
    deep = context(tmp_path, "deepswe", {})
    with pytest.raises(ConfigError, match="runtime_root"):
        adapter_for("deepswe", runner=FakeRunner()).build_command(deep)


def test_deepswe_command_rejects_nonlocal_transport(tmp_path: Path) -> None:
    deep = context(tmp_path, "deepswe", {**DEEPSWE_PINS, "transport": "ssh"})
    with pytest.raises(ConfigError, match="requires local transport"):
        adapter_for("deepswe", runner=FakeRunner()).build_command(deep)


def test_deepswe_adapter_enforces_profile_minimum_score(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class DeepSWERunner(FakeRunner):
        def run(self, command, *, cwd, env, timeout):
            output = Path(command[command.index("--output-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "adapter-result.json").write_text(
                json.dumps(
                    {
                        "suite_id": "deepswe",
                        "row_ids": ["17"],
                        "status": "passed",
                        "validity": "valid",
                        "metrics": {"score": 0.60},
                    }
                ),
                encoding="utf-8",
            )
            return super().run(command, cwd=cwd, env=env, timeout=timeout)

    ctx = context(tmp_path, "deepswe", {**DEEPSWE_PINS, "min_score": 0.647})
    result = adapter_for("deepswe", runner=DeepSWERunner()).run(ctx)
    assert result.status == "failed"
    assert result.validity == "valid"
    assert result.reason == "benchmark_below_tolerance"


def test_deepswe_blocks_nonempty_headers_until_remote_runtime_supports_them(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    ctx = context(
        tmp_path,
        "deepswe",
        {**DEEPSWE_PINS, "extra_headers": "unsupported"},
    )
    result = adapter_for("deepswe", runner=FakeRunner()).run(ctx)
    assert result.status == "blocked"
    assert result.validity == "execution_invalid"
    assert result.reason == "extra_headers_unsupported"


def test_deepswe_checkpoint_merges_completed_shards(tmp_path: Path) -> None:
    first = tmp_path / "job-1"
    second = tmp_path / "job-2"
    for job, task, reward in ((first, "task-a", 0), (second, "task-b", 1)):
        trial = job / task
        trial.mkdir(parents=True)
        (trial / "result.json").write_text(
            json.dumps(
                {
                    "trial_name": task,
                    "config": {"task": {"path": f"/tasks/{task}"}},
                    "verifier_result": {"rewards": {"reward": reward}},
                    "exception_info": None,
                    "agent_result": {"n_output_tokens": 10},
                }
            ),
            encoding="utf-8",
        )
    records = [
        {"job_dir": str(first), "selected_tasks": ["task-a", "task-b"]},
        {"job_dir": str(second), "selected_tasks": ["task-b"]},
    ]

    merged = _merged_checkpoint_results(records, ["task-a", "task-b"])

    assert merged["task-a"]["reward"] == 0
    assert merged["task-b"]["reward"] == 1


def test_deepswe_checkpoint_rejects_contract_drift(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    _, checkpoint = _load_checkpoint(
        output,
        mode="full",
        task_names=["task-a"],
        rounds=3,
        max_reruns=1,
        concurrency=113,
    )
    assert checkpoint["task_names"] == ["task-a"]
    assert checkpoint["agent_timeout_multiplier"] == 1.0

    with pytest.raises(RuntimeError, match="checkpoint contract changed"):
        _load_checkpoint(
            output,
            mode="full",
            task_names=["task-a", "task-b"],
            rounds=3,
            max_reruns=1,
            concurrency=113,
        )


def test_deepswe_mixes_formal_rounds_and_serializes_each_task(
    tmp_path: Path, monkeypatch
) -> None:
    import threading
    import time

    runtime = tmp_path / "runtime"
    tasks = runtime / "deep-swe/tasks"
    jobs = runtime / "jobs"
    for task in ("task-a", "task-b", "task-c"):
        (tasks / task).mkdir(parents=True)
    commands: list[list[str]] = []
    events: list[tuple[str, int, str]] = []
    active_tasks: set[str] = set()
    active_lock = threading.Lock()

    def fake_run(command, **kwargs):
        if command[0] == "date":
            return subprocess.CompletedProcess(
                command, 0, stdout="20260908T100000Z\n", stderr=""
            )
        assert command[0] == "pier"
        commands.append(command)
        job_dir = jobs / command[command.index("--job-name") + 1]
        selected = [
            command[index + 1]
            for index, token in enumerate(command[:-1])
            if token == "--include-task-name"
        ]
        assert len(selected) == 1
        task = selected[0]
        job_name = command[command.index("--job-name") + 1]
        round_no = int(job_name.split("-round-")[1].split("-")[0])
        with active_lock:
            assert task not in active_tasks
            active_tasks.add(task)
            events.append(("start", round_no, task))
        time.sleep(0.05 if task == "task-a" else 0.005)
        for task in selected:
            trial = job_dir / task
            trial.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "trial_name": task,
                        "config": {"task": {"path": f"/tasks/{task}"}},
                        "verifier_result": {"rewards": {"reward": 1}},
                        "exception_info": None,
                        "agent_result": {"n_output_tokens": 10},
                    }
                ),
                encoding="utf-8",
            )
        with active_lock:
            active_tasks.remove(task)
            events.append(("finish", round_no, task))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run
    )
    result = run_benchmark_checkpointed(
        output_dir=tmp_path / "run/suites/001-deepswe",
        runtime_root=str(runtime),
        pier_bin="pier",
        mode="full",
        concurrency=3,
        expected_tasks=3,
        rounds=3,
        max_reruns=1,
        kimi_code_version="0.29.0",
        model="vendor-model",
        execution_env={"KIMI_API_KEY": "runtime-only-secret"},
        preflight={
            "observed": {
                "pier_version": "0.3.1",
                "deep_swe_commit": "test-commit",
            }
        },
    )

    assert all(command[command.index("--n-concurrent") + 1] == "1" for command in commands)
    assert all(
        command[command.index("--agent-timeout-multiplier") + 1] == "1.0"
        for command in commands
    )
    assert all("-task-" in command[command.index("--job-name") + 1] for command in commands)
    assert result["counts"] == {
        "tasks": 3,
        "corpus_tasks": 3,
        "selected_tasks": 3,
        "is_subset": 0,
        "rounds": 3,
        "total": 9,
        "completed": 9,
        "errored": 0,
        "round_task_passes": 9,
        "round_task_not_passes": 0,
        "round_task_exec_unknown": 0,
    }
    assert result["runner_fingerprint"]["formal_rounds_mixed"] == "true"
    assert result["runner_fingerprint"]["round_scheduling"] == "mixed_round_task_locked"
    assert result["runner_fingerprint"]["task_pass_policy"] == "error_only"
    assert result["runner_fingerprint"]["concurrency_scope"] == "global_task_jobs"
    assert result["runner_fingerprint"]["same_task_overlap"] == "forbidden"
    # Slow task-a is still in round 1 while fast tasks already started round 2.
    last_round_one_finish = max(
        i for i, event in enumerate(events) if event[0] == "finish" and event[1] == 1
    )
    first_round_two_start = min(
        i for i, event in enumerate(events) if event[0] == "start" and event[1] == 2
    )
    assert first_round_two_start < last_round_one_finish
    # Each task's own rounds remain strictly serialized.
    for name in ("task-a", "task-b", "task-c"):
        own = [event for event in events if event[2] == name]
        for round_no in (1, 2):
            finish = own.index(("finish", round_no, name))
            start_next = own.index(("start", round_no + 1, name))
            assert finish < start_next


def test_deepswe_retries_only_incomplete_pairs_after_formal(
    tmp_path: Path, monkeypatch
) -> None:
    import threading

    runtime = tmp_path / "runtime"
    tasks = runtime / "deep-swe/tasks"
    jobs = runtime / "jobs"
    for task in ("task-a", "task-b"):
        (tasks / task).mkdir(parents=True)
    commands: list[list[str]] = []
    events: list[tuple[str, int, int, str]] = []
    events_lock = threading.Lock()

    def fake_run(command, **kwargs):
        if command[0] == "date":
            return subprocess.CompletedProcess(
                command, 0, stdout="20260908T100000Z\n", stderr=""
            )
        assert command[0] == "pier"
        commands.append(command)
        job_name = command[command.index("--job-name") + 1]
        job_dir = jobs / job_name
        task = command[command.index("--include-task-name") + 1]
        round_no = int(job_name.split("-round-")[1].split("-")[0])
        attempt_no = int(job_name.split("-attempt-")[1].split("-")[0])
        with events_lock:
            events.append(("start", round_no, attempt_no, task))
        trial = job_dir / task
        trial.mkdir(parents=True)
        # task-a finishes round 1 cleanly with reward 0: a scored model failure
        # that must never be rerun. task-b's round-1 attempt is interrupted and
        # produces no result, so only that pair is retried and then passes.
        interrupted = task == "task-b" and round_no == 1 and attempt_no == 1
        if not interrupted:
            reward = 0 if (task == "task-a" and round_no == 1) else 1
            (trial / "result.json").write_text(
                json.dumps(
                    {
                        "trial_name": task,
                        "config": {"task": {"path": f"/tasks/{task}"}},
                        "verifier_result": {"rewards": {"reward": reward}},
                        "exception_info": None,
                        "agent_result": {"n_output_tokens": 10},
                    }
                ),
                encoding="utf-8",
            )
        with events_lock:
            events.append(("finish", round_no, attempt_no, task))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run
    )
    result = run_benchmark_checkpointed(
        output_dir=tmp_path / "run/suites/001-deepswe",
        runtime_root=str(runtime),
        pier_bin="pier",
        mode="full",
        concurrency=2,
        expected_tasks=2,
        rounds=2,
        max_reruns=1,
        kimi_code_version="0.29.0",
        model="vendor-model",
        execution_env={"KIMI_API_KEY": "runtime-only-secret"},
        preflight={
            "observed": {
                "pier_version": "0.3.1",
                "deep_swe_commit": "test-commit",
            }
        },
    )

    # 4 formal attempts plus exactly one retry, for the interrupted pair only.
    assert len(commands) == 5
    retry_commands = [
        command
        for command in commands
        if "-attempt-02-" in command[command.index("--job-name") + 1]
    ]
    assert len(retry_commands) == 1
    retry_job = retry_commands[0][retry_commands[0].index("--job-name") + 1]
    assert "task-b" in retry_job
    assert "round-01" in retry_job
    # The clean reward-0 pair for task-a round 1 is never retried.
    assert not any(
        "task-a" in command[command.index("--job-name") + 1]
        and "-attempt-02-" in command[command.index("--job-name") + 1]
        for command in commands
    )
    assert result["validity"] == "valid"
    assert result["status"] == "passed"
    assert result["metrics"]["score"] == 0.75
    assert result["counts"]["round_task_passes"] == 3
    assert result["counts"]["round_task_not_passes"] == 1
    # Batch retries start only after every formal attempt has finished.
    last_formal_finish = max(
        i for i, event in enumerate(events) if event[0] == "finish" and event[2] == 1
    )
    first_retry_start = min(
        i for i, event in enumerate(events) if event[0] == "start" and event[2] == 2
    )
    assert last_formal_finish < first_retry_start


def test_deepswe_persistent_errors_exhaust_one_rerun_and_stay_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    tasks = runtime / "deep-swe/tasks"
    (tasks / "task-a").mkdir(parents=True)
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "date":
            return subprocess.CompletedProcess(
                command, 0, stdout="20260908T100000Z\n", stderr=""
            )
        assert command[0] == "pier"
        commands.append(command)
        # No result.json is written: the trial outcome stays unclear.
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="boom")

    monkeypatch.setattr(
        "novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run
    )
    result = run_benchmark_checkpointed(
        output_dir=tmp_path / "run/suites/001-deepswe",
        runtime_root=str(runtime),
        pier_bin="pier",
        mode="full",
        concurrency=1,
        expected_tasks=1,
        rounds=1,
        max_reruns=1,
        kimi_code_version="0.29.0",
        model="vendor-model",
        execution_env={"KIMI_API_KEY": "runtime-only-secret"},
        preflight={
            "observed": {
                "pier_version": "0.3.1",
                "deep_swe_commit": "test-commit",
            }
        },
    )

    # One formal attempt plus exactly one rerun wave, then still unclear.
    assert len(commands) == 2
    assert result["status"] == "failed"
    assert result["validity"] == "execution_invalid"
    assert result["reason"] == "deepswe_rounds_incomplete"
    assert result["counts"]["errored"] == 2


def test_deepswe_cleanup_stops_only_checkpoint_owned_containers(
    tmp_path: Path, monkeypatch
) -> None:
    checkpoint = tmp_path / "deepswe-checkpoint.json"
    owned_job = tmp_path / "jobs/owned"
    checkpoint.write_text(
        json.dumps(
            {
                "waves": {
                    "1": {
                        "1": [
                            {
                                "job_dir": str(owned_job),
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["docker", "ps"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"owned-id\t{owned_job}/trial/docker-compose.json\n"
                    f"other-id\t{tmp_path}/jobs/other/docker-compose.json\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("novita_self_test.adapters.deepswe_remote.subprocess.run", fake_run)

    result = cleanup_checkpoint_resources(checkpoint)

    assert result["stopped_containers"] == ["owned-id"]
    assert commands[-1] == ["docker", "stop", "owned-id"]


def test_deepswe_wave_results_preserve_stability_message(tmp_path: Path) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "trial",
                "config": {"task": {"path": "/tasks/task-a"}},
                "verifier_result": None,
                "exception_info": {
                    "exception_type": "RuntimeError",
                    "exception_message": "HTTP 403 not enough balance",
                },
                "agent_result": None,
            }
        ),
        encoding="utf-8",
    )
    result = read_wave_results(tmp_path, ["task-a"])["task-a"]
    assert result["reward"] is None
    assert result["exception_type"] == "RuntimeError"
    assert result["exception_message"] == "HTTP 403 not enough balance"
    assert result["failure_class"] == "quota_exhausted"


def test_deepswe_zero_with_agent_exception_is_retryable_incomplete(tmp_path: Path) -> None:
    trial = tmp_path / "trial"
    trial.mkdir()
    (trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "trial",
                "config": {"task": {"path": "/tasks/task-a"}},
                "verifier_result": {"rewards": {"reward": 0}},
                "exception_info": {
                    "exception_type": "NonZeroAgentExitCodeError",
                    "exception_message": "provider returned engine_overloaded",
                },
                "agent_result": None,
            }
        ),
        encoding="utf-8",
    )
    result = read_wave_results(tmp_path, ["task-a"])["task-a"]
    assert result["verifier_reward"] == 0
    assert result["reward"] is None
    assert result["retryable_incomplete"] is True
    assert result["failure_class"] == "engine_overloaded"


def test_deepswe_checkpoint_pins_concurrency_and_retry_contract(tmp_path: Path) -> None:
    _, checkpoint = _load_checkpoint(
        tmp_path / "suite",
        mode="full",
        task_names=["task-a"],
        rounds=1,
        max_reruns=1,
        concurrency=113,
        max_concurrency=113,
    )
    assert checkpoint["max_concurrency"] == 113
    assert checkpoint["max_reruns"] == 1
    assert checkpoint["max_parts_per_wave"] == 2


def test_deepswe_results_include_verifier_and_request_evidence(tmp_path: Path) -> None:
    trial = tmp_path / "trial"
    evidence = trial / "logs" / "agent" / "request-evidence"
    evidence.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "trial",
                "config": {"task": {"path": "/tasks/task-a"}},
                "verifier_result": {
                    "rewards": {
                        "reward": 1,
                        "partial": 1,
                        "f2p_total": 20,
                        "f2p_passed": 20,
                        "p2p_total": 3,
                        "p2p_passed": 3,
                    }
                },
                "exception_info": None,
                "agent_result": {"n_output_tokens": 42, "n_steps": 7},
            }
        ),
        encoding="utf-8",
    )
    result = read_wave_results(tmp_path, ["task-a"])["task-a"]
    assert result["reward"] == 1
    assert result["partial"] == 1
    assert result["f2p_passed"] == 20
    assert result["p2p_passed"] == 3
    assert result["output_tokens"] == 42
    assert result["agent_steps"] == 7
    assert result["request_evidence_dir"] == str(evidence.resolve())

    summary = summarize_round(["task-a"], [{"task-a": result}])
    summary["round"] = 1
    output = tmp_path / "task-results.jsonl"
    _write_task_results(output, [summary])
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["task"] == "task-a"
    assert record["attempts"][0]["request_evidence_dir"] == str(evidence.resolve())


def test_deepswe_round_summary_uses_any_pass_and_explicit_not_pass() -> None:
    def attempt(reward):
        return {
            "trial_name": "trial",
            "reward": reward,
            "exception_type": None if reward is not None else "RuntimeError",
            "output_tokens": 1,
            "result_path": "/result.json",
        }

    summary = summarize_round(
        ["task-a", "task-b", "task-c"],
        [
            {"task-a": attempt(0), "task-b": attempt(None), "task-c": attempt(1)},
            {"task-a": attempt(1), "task-b": attempt(0)},
        ],
    )
    assert summary["score"] == pytest.approx(2 / 3)
    assert summary["passed_tasks"] == 2
    assert summary["not_pass_tasks"] == 1
    assert summary["unclear_tasks"] == []
    assert summary["task_results"]["task-a"]["status"] == "passed"
    assert len(summary["task_results"]["task-a"]["attempts"]) == 2


class LocalPreflightRunner:
    def __init__(self, *, tasks: int = 113) -> None:
        self.tasks = tasks
        self.commands: list[list[str]] = []

    def __call__(self, command):
        self.commands.append(command)
        rendered = " ".join(command)
        if "rev-parse" in rendered:
            output = DEEPSWE_PINS["expected_deep_swe_commit"] + "\n"
        elif "status --porcelain" in rendered:
            output = ""
        elif "--version" in rendered:
            output = "0.3.1\n"
        elif command[0] == "find":
            output = "".join(f"/tasks/{index}\n" for index in range(self.tasks))
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")


def test_deepswe_local_preflight_accepts_exact_runtime() -> None:
    runner = LocalPreflightRunner()
    result = verify_runtime(
        runtime_root="/srv/kvh/deepswe",
        expected_commit=DEEPSWE_PINS["expected_deep_swe_commit"],
        expected_pier_version=DEEPSWE_PINS["expected_pier_version"],
        expected_tasks=DEEPSWE_PINS["expected_tasks"],
        pier_bin=DEEPSWE_PINS["pier_bin"],
        runner=runner,
    )

    assert result["status"] == "passed"
    assert result["transport"] == "local"
    assert result["mismatches"] == {}
    assert all(command[0] != "ssh" for command in runner.commands)


def test_deepswe_local_preflight_blocks_task_drift() -> None:
    result = verify_runtime(
        runtime_root="/srv/kvh/deepswe",
        expected_commit=DEEPSWE_PINS["expected_deep_swe_commit"],
        expected_pier_version=DEEPSWE_PINS["expected_pier_version"],
        expected_tasks=DEEPSWE_PINS["expected_tasks"],
        pier_bin=DEEPSWE_PINS["pier_bin"],
        runner=LocalPreflightRunner(tasks=112),
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "local_pin_mismatch"
    assert result["mismatches"]["tasks"] == {"expected": 113, "observed": 112}


def test_beam_runner_writes_structured_blocker_without_judge_contract(
    tmp_path: Path, monkeypatch
) -> None:
    for name in (
        "NVST_BEAM_JUDGE_BASE_URL",
        "NVST_BEAM_JUDGE_MODEL",
        "NVST_BEAM_JUDGE_API_KEY_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    output_dir = tmp_path / "beam"
    assert beam_main(
        [
            "--output-dir",
            str(output_dir),
            "--python",
            "python3",
            "--kvv-root",
            str(tmp_path / "kvv"),
            "--expected-trials",
            "700",
            "--max-context-tokens",
            "1048064",
        ]
    ) == 2
    result = json.loads((output_dir / "adapter-result.json").read_text())
    assert result["status"] == "blocked"
    assert result["validity"] == "execution_invalid"
    assert result["reason"] == "beam_judge_contract_missing"


def test_beam_runner_forwards_pinned_generation_context(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []

    def fake_invoke(command: list[str], *, env: dict[str, str]) -> None:
        commands.append(command)
        output = Path(command[command.index("--output") + 1])
        if "novita_self_test.adapters.beam_judge_run" in command:
            output.write_text(
                '{"chat_id":"1","question_type":"abstention",'
                '"question_index":0,"score":1.0}\n',
                encoding="utf-8",
            )
        else:
            output.write_text(
                '{"chat_id":"1","question_type":"abstention",'
                '"question_index":0,"response":"answer","error":null}\n',
                encoding="utf-8",
            )

    monkeypatch.setattr("novita_self_test.adapters.beam_run.invoke", fake_invoke)
    monkeypatch.setenv("NVST_BEAM_JUDGE_BASE_URL", "https://judge.test/v1")
    monkeypatch.setenv("NVST_BEAM_JUDGE_MODEL", "judge-model")
    monkeypatch.setenv("NVST_BEAM_JUDGE_API_KEY_ENV", "TEST_JUDGE_KEY")
    monkeypatch.setenv("TEST_JUDGE_KEY", "judge-secret")
    monkeypatch.setenv("KIMI_API_KEY", "vendor-secret")
    monkeypatch.setenv("KIMI_MODEL", "vendor-model")
    monkeypatch.setenv("KIMI_BASE_URL", "https://vendor.test/v1")

    assert beam_main(
        [
            "--output-dir",
            str(tmp_path / "beam"),
            "--python",
            "python3",
            "--kvv-root",
            str(tmp_path / "kvv"),
            "--expected-trials",
            "1",
            "--concurrency",
            "30",
            "--max-context-tokens",
            "1048064",
        ]
    ) == 0
    generate = commands[0]
    assert generate[generate.index("--concurrency") + 1] == "30"
    assert generate[generate.index("--max-context-tokens") + 1] == "1048064"
    assert generate[generate.index("--tokenizer") + 1] == str(
        tmp_path / "kvv" / "beam" / "tokenizer"
    )
    assert generate[generate.index("--max-retries") + 1] == "8"
    assert generate[generate.index("--timeout") + 1] == "3600"
    judge = commands[1]
    assert judge[judge.index("--concurrency") + 1] == "64"
    assert judge[judge.index("--max-retries") + 1] == "8"
    assert judge[judge.index("--timeout") + 1] == "600"


def test_beam_generation_only_writes_pending_checkpoint_without_judging(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []

    def fake_invoke(command: list[str], *, env: dict[str, str]) -> None:
        commands.append(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            '{"chat_id":"1","question_type":"abstention",'
            '"question_index":0,"response":"answer","error":null}\n',
            encoding="utf-8",
        )

    monkeypatch.setattr("novita_self_test.adapters.beam_run.invoke", fake_invoke)
    monkeypatch.setenv("NVST_BEAM_JUDGE_BASE_URL", "https://judge.test/v1")
    monkeypatch.setenv("NVST_BEAM_JUDGE_MODEL", "judge-model")
    monkeypatch.setenv("NVST_BEAM_JUDGE_API_KEY_ENV", "TEST_JUDGE_KEY")
    monkeypatch.setenv("TEST_JUDGE_KEY", "judge-secret")
    monkeypatch.setenv("KIMI_API_KEY", "vendor-secret")
    monkeypatch.setenv("KIMI_MODEL", "vendor-model")
    monkeypatch.setenv("KIMI_BASE_URL", "https://vendor.test/v1")

    output = tmp_path / "beam"
    assert beam_main(
        [
            "--output-dir", str(output),
            "--python", "python3",
            "--kvv-root", str(tmp_path / "kvv"),
            "--expected-trials", "1",
            "--max-context-tokens", "1048064",
            "--generation-only",
        ]
    ) == 0

    assert len(commands) == 1
    result = json.loads((output / "adapter-result.json").read_text())
    assert result["status"] == "deferred"
    assert result["reason"] == "beam_judge_pending"
    assert result["coverage"]["generation_complete"] is True
    assert result["coverage"]["background_judge_pending"] is True


def test_beam_retries_invalid_repetition_task_up_to_three_times(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "beam"
    output.mkdir()
    answers = output / "answers.jsonl"
    answers.write_text(
        json.dumps(
            {
                "chat_id": "1",
                "question_type": "event_ordering",
                "question_index": 0,
                "response": "",
                "finish_reason": "repetition",
                "error": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    chats = tmp_path / "chats.jsonl.gz"
    with gzip.open(chats, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"chat_id": "1", "messages": []}) + "\n")
    questions = tmp_path / "questions.jsonl"
    questions.write_text(
        json.dumps(
            {
                "chat_id": "1",
                "question_type": "event_ordering",
                "question_index": 0,
                "question": "q",
                "gold_answer": "a",
                "rubric": ["r"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_invoke(command: list[str], *, env: dict[str, str]) -> None:
        calls.append(command)
        retry_output = Path(command[command.index("--output") + 1])
        retry_output.write_text(
            json.dumps(
                {
                    "chat_id": "1",
                    "question_type": "event_ordering",
                    "question_index": 0,
                    "response": "recovered",
                    "finish_reason": "stop",
                    "error": None,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    monkeypatch.setattr("novita_self_test.adapters.beam_run.invoke", fake_invoke)
    rows, artifacts, attempts = retry_invalid_generations(
        output_dir=output,
        answers_path=answers,
        source_chats=chats,
        source_questions=questions,
        python="python3",
        kvv_root=tmp_path / "kvv",
        model="model",
        base_url="https://vendor.test/v1",
        concurrency=256,
        task_retries=3,
        request_max_retries=8,
        generation_timeout=3600,
        max_context_tokens=1048064,
        env={},
    )

    assert len(calls) == 1
    assert rows[0]["response"] == "recovered"
    assert attempts["1|event_ordering|0"] == 1
    assert any(path.endswith("generation-retry-summary.json") for path in artifacts)
    summary = json.loads((output / "generation-retry-summary.json").read_text())
    assert summary["task_retry_max"] == 3
    assert summary["remaining_invalid_keys"] == []


def test_beam_runner_scores_terminal_empty_zero_except_abstention(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []
    output = tmp_path / "beam"
    output.mkdir()
    (output / "answers.jsonl").write_text(
        '{"chat_id":"1","question_type":"knowledge_update",'
        '"question_index":0,"response":"answer","finish_reason":"stop",'
        '"error":null}\n'
        '{"chat_id":"2","question_type":"summarization",'
        '"question_index":1,"response":"","finish_reason":"length",'
        '"error":null}\n'
        '{"chat_id":"3","question_type":"abstention",'
        '"question_index":0,"response":"","finish_reason":"stop",'
        '"error":null}\n',
        encoding="utf-8",
    )

    def fake_invoke(command: list[str], *, env: dict[str, str]) -> None:
        commands.append(command)
        assert "novita_self_test.adapters.beam_judge_run" in command
        judge_input = Path(command[command.index("--answers") + 1])
        rows = [json.loads(line) for line in judge_input.read_text().splitlines()]
        # The empty abstention response must reach the judge; the empty
        # non-abstention response must not.
        assert [(row["chat_id"], row["response"]) for row in rows] == [
            ("1", "answer"),
            ("3", ""),
        ]
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(
            '{"chat_id":"1","question_type":"knowledge_update",'
            '"question_index":0,"score":1.0}\n'
            '{"chat_id":"3","question_type":"abstention",'
            '"question_index":0,"score":1.0}\n',
            encoding="utf-8",
        )

    monkeypatch.setattr("novita_self_test.adapters.beam_run.invoke", fake_invoke)
    monkeypatch.setenv("NVST_BEAM_JUDGE_BASE_URL", "https://judge.test/v1")
    monkeypatch.setenv("NVST_BEAM_JUDGE_MODEL", "judge-model")
    monkeypatch.setenv("NVST_BEAM_JUDGE_API_KEY_ENV", "TEST_JUDGE_KEY")
    monkeypatch.setenv("TEST_JUDGE_KEY", "judge-secret")
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    assert beam_main(
        [
            "--output-dir", str(output),
            "--python", "python3",
            "--kvv-root", str(tmp_path / "kvv"),
            "--expected-trials", "3",
            "--max-context-tokens", "1048064",
            "--reuse-answers",
        ]
    ) == 0
    assert len(commands) == 1
    result = json.loads((output / "adapter-result.json").read_text())
    assert result["counts"] == {
        "total": 3,
        "scored": 3,
        "judged": 2,
        "zero_scored": 1,
        "judge_errors": 0,
        "remaining": 0,
    }
    assert result["metrics"]["score"] == pytest.approx(2 / 3)
    assert result["validity"] == "valid"
    assert result["coverage"]["zero_scored_generation_keys"] == [
        "2:summarization:1"
    ]
    scores = [
        json.loads(line) for line in (output / "scores.jsonl").read_text().splitlines()
    ]
    assert [row["score"] for row in scores] == [1.0, 0.0, 1.0]


def test_beam_runner_blocks_quota_without_judging(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_invoke(command: list[str], *, env: dict[str, str]) -> None:
        commands.append(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            '{"chat_id":"1","question_type":"abstention",'
            '"question_index":0,"response":"",'
            '"error":"HTTP 403 not enough balance"}\n',
            encoding="utf-8",
        )

    monkeypatch.setattr("novita_self_test.adapters.beam_run.invoke", fake_invoke)
    monkeypatch.setenv("NVST_BEAM_JUDGE_BASE_URL", "https://judge.test/v1")
    monkeypatch.setenv("NVST_BEAM_JUDGE_MODEL", "judge-model")
    monkeypatch.setenv("NVST_BEAM_JUDGE_API_KEY_ENV", "TEST_JUDGE_KEY")
    monkeypatch.setenv("TEST_JUDGE_KEY", "judge-secret")
    monkeypatch.setenv("KIMI_API_KEY", "vendor-secret")
    monkeypatch.setenv("KIMI_MODEL", "vendor-model")
    monkeypatch.setenv("KIMI_BASE_URL", "https://vendor.test/v1")
    output = tmp_path / "beam"
    assert beam_main(
        [
            "--output-dir", str(output),
            "--python", "python3",
            "--kvv-root", str(tmp_path / "kvv"),
            "--expected-trials", "1",
            "--max-context-tokens", "1048064",
        ]
    ) == 2
    assert len(commands) == 1
    result = json.loads((output / "adapter-result.json").read_text())
    assert result["status"] == "blocked"
    assert result["validity"] == "service_invalid"
    assert result["reason"] == "quota_exhausted"
    assert result["metrics"] == {}


def test_beam_runner_rejects_invalid_generations_before_judging(
    tmp_path: Path, monkeypatch
) -> None:
    commands: list[list[str]] = []

    def fake_invoke(command: list[str], *, env: dict[str, str]) -> None:
        commands.append(command)
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            '{"chat_id":"1","question_type":"abstention",'
            '"question_index":0,"response":"","error":"server overload"}\n',
            encoding="utf-8",
        )

    monkeypatch.setattr("novita_self_test.adapters.beam_run.invoke", fake_invoke)
    monkeypatch.setenv("NVST_BEAM_JUDGE_BASE_URL", "https://judge.test/v1")
    monkeypatch.setenv("NVST_BEAM_JUDGE_MODEL", "judge-model")
    monkeypatch.setenv("NVST_BEAM_JUDGE_API_KEY_ENV", "TEST_JUDGE_KEY")
    monkeypatch.setenv("TEST_JUDGE_KEY", "judge-secret")
    monkeypatch.setenv("KIMI_API_KEY", "vendor-secret")
    monkeypatch.setenv("KIMI_MODEL", "vendor-model")
    monkeypatch.setenv("KIMI_BASE_URL", "https://vendor.test/v1")
    output = tmp_path / "beam"
    assert beam_main(
        [
            "--output-dir", str(output),
            "--python", "python3",
            "--kvv-root", str(tmp_path / "kvv"),
            "--expected-trials", "1",
            "--max-context-tokens", "1048064",
        ]
    ) == 2
    assert len(commands) == 4
    result = json.loads((output / "adapter-result.json").read_text())
    assert result["status"] == "failed"
    assert result["validity"] == "execution_invalid"
    assert result["reason"] == "beam_generation_invalid"
    assert result["counts"] == {"total": 1, "completed": 0, "errored": 1}
    assert result["coverage"]["task_retry_max"] == 3
    assert result["coverage"]["task_retry_attempts"]["1|abstention|0"] == 3
    assert result["coverage"]["invalid_generation_keys"] == ["1:abstention:0"]
    assert not any("beam_judge_run" in command for command in commands)
