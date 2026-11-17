from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from novita_self_test.artifacts import SuiteResult, enforce_completeness
from novita_self_test.config import (
    ConfigError,
    JudgeAccess,
    ModelProfile,
    ServiceAccess,
    find_secret,
)
from novita_self_test.orchestrator import Orchestrator
from novita_self_test.cli import _parser
from novita_self_test.orchestrator import UserInterrupted
from novita_self_test.reporting import regenerate_report
from novita_self_test.requirements import (
    _python_snapshot_sha256,
    _structured_rows_sha256,
    _xml_python_snapshot_sha256,
    refresh_requirements,
)
from novita_self_test.service_probe import probe_service

from .helpers import write_model, write_service


def fake_suite(
    suite_id: str,
    row_id: str,
    *,
    status: str = "passed",
    validity: str = "valid",
    long_test: str | None = None,
    selector: str | None = None,
    completeness: dict | None = None,
    reason: str | None = None,
) -> dict:
    suite = {
        "suite_id": suite_id,
        "adapter": "fake",
        "row_ids": [row_id],
        "mandatory": True,
        "config": {
            "fake_result": {"status": status, "validity": validity, "reason": reason}
        },
        "completeness": completeness or {},
    }
    if long_test:
        suite["long_test"] = long_test
    if selector:
        suite["selectors"] = {selector: [f"case::{selector}"]}
    return suite


def test_real_profiles_run_on_any_host() -> None:
    profile = ModelProfile.load("kimi-k3")
    assert profile.require_execution_site() is None


def test_profile_execution_site_rejects_missing_marker(tmp_path: Path) -> None:
    model = write_model(tmp_path, [fake_suite("only", "R1")])
    profile_path = model / "profile.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["execution"] = {
        "site": "build-server",
        "site_marker": str(tmp_path / "missing-marker"),
    }
    profile_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    loaded = ModelProfile.load("test-model", root=tmp_path)
    with pytest.raises(ConfigError) as error:
        loaded.require_execution_site()
    assert error.value.code == "execution_site_mismatch"


def test_real_profiles_load() -> None:
    k3 = ModelProfile.load("kimi-k3")
    assert len(k3.official_rows()) == 20
    assert k3.snapshot_revision == (
        "k3-vendor-test-result-docx-444-stream-244-toolcall-docx-1200-sheet-170-kvv-6ec0eaad"
    )
    assert k3.source["novita_official_rows"] == ["18"]
    assert k3.profile["execution"] == {"row_order": "exact"}
    assert k3.profile["acceptance_control"]["global_stop"] == {
        "validities": ["service_invalid"],
        "statuses": ["blocked"],
        "reasons": [
            "runner_contract_invalid",
            "artifact_contract_incomplete",
            "runner_not_found",
            "safety_failure",
        ],
    }


def test_profile_loader_rejects_missing_official_row_coverage(tmp_path: Path) -> None:
    model = write_model(tmp_path, [fake_suite("first", "R1"), fake_suite("second", "R2")])
    manifest = yaml.safe_load((model / "suite-manifest.yaml").read_text())
    manifest["suites"].pop()
    (model / "suite-manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        ModelProfile.load("test-model", root=tmp_path)
    assert error.value.code == "suite_manifest_invalid"
    assert "missing=['R2']" in str(error.value)


def test_profile_loader_enforces_declared_exact_row_order(tmp_path: Path) -> None:
    model = write_model(tmp_path, [fake_suite("first", "R1"), fake_suite("second", "R2")])
    profile = yaml.safe_load((model / "profile.yaml").read_text())
    profile["execution"] = {"row_order": "exact"}
    (model / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    manifest = yaml.safe_load((model / "suite-manifest.yaml").read_text())
    manifest["suites"].reverse()
    (model / "suite-manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        ModelProfile.load("test-model", root=tmp_path)
    assert error.value.code == "suite_manifest_invalid"
    assert "row order" in str(error.value)


def test_profile_loader_rejects_pinned_fixture_sha_drift(tmp_path: Path) -> None:
    model = write_model(tmp_path, [fake_suite("only", "R1")])
    fixture = tmp_path / "fixture.jsonl"
    fixture.write_text("{}\n", encoding="utf-8")
    source_path = model / "requirements/source.yaml"
    source = yaml.safe_load(source_path.read_text())
    source["fixture_lock"] = [
        {
            "name": "fixture.jsonl",
            "path": "fixture.jsonl",
            "sha256": "0" * 64,
        }
    ]
    source_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        ModelProfile.load("test-model", root=tmp_path)
    assert error.value.code == "requirements_snapshot_invalid"
    assert "SHA mismatch" in str(error.value)


def test_k3_stream_suites_follow_the_official_file_policy() -> None:
    profile = ModelProfile.load("kimi-k3")
    p0 = next(item for item in profile.suites if item.suite_id == "stream-spec-p0")
    p1 = next(item for item in profile.suites if item.suite_id == "stream-spec-p1")
    p2 = next(item for item in profile.suites if item.suite_id == "stream-spec-p2")
    assert p0.completeness["expected_counts"] == {
        "total": 25,
        "passed": 9,
        "skipped": 16,
        "failed": 0,
        "error": 0,
    }
    assert p1.completeness["expected_counts"] == {
        "total": 25,
        "passed": 12,
        "skipped": 13,
        "failed": 0,
        "error": 0,
    }
    assert p2.completeness["expected_counts"] == {
        "total": 25,
        "passed": 3,
        "skipped": 22,
        "failed": 0,
        "error": 0,
    }
    assert p0.selectors["1.0"] == [
        "suites/novita-api/tests/streaming/test_stream_spec.py"
    ]
    assert p1.selectors["1.1"] == [
        "suites/novita-api/tests/streaming/test_stream_spec.py"
    ]
    assert p2.selectors["1.2"] == [
        "suites/novita-api/tests/streaming/test_stream_spec.py"
    ]
    assert p0.config["env"]["STREAM_SPEC_PRIORITY"] == "P0"
    assert p1.config["env"]["STREAM_SPEC_PRIORITY"] == "P1"
    assert p2.config["env"]["STREAM_SPEC_PRIORITY"] == "P2"


def test_k3_walle_preserves_official_runner_reruns() -> None:
    profile = ModelProfile.load("kimi-k3")
    walle = next(item for item in profile.suites if item.suite_id == "walle")
    assert walle.config["pytest_args"] == [
        "-n",
        "428",
        "--dist",
        "load",
        "--thinking",
        "--reruns",
        "3",
        "--reruns-delay",
        "2",
        "--tool-json-report",
        "{suite_dir}/tool-call-schema-report.json",
    ]
    assert walle.completeness["per_repeat"]["required_artifacts"] == [
        "cases.jsonl",
        "summary.json",
    ]


def test_k3_kvv_rows_use_case_count_xdist_workers_and_round_barrier() -> None:
    profile = ModelProfile.load("kimi-k3")
    expected = {
        "kvv-params": "19",
        "walle": "428",
        "kvv-k3-features": "118",
        "kvv-prompt-tokens": "74",
    }
    for suite_id, workers in expected.items():
        suite = next(item for item in profile.suites if item.suite_id == suite_id)
        args = suite.config["pytest_args"]
        assert args[:4] == ["-n", workers, "--dist", "load"]
        assert suite.config["repeat_schedule"] == "round_barrier"


def test_json_schemas_and_service_example_validate() -> None:
    root = Path(__file__).resolve().parents[2]
    for path in sorted((root / "schemas").glob("*.json")):
        Draft202012Validator.check_schema(json.loads(path.read_text()))
    schema = json.loads((root / "schemas/service-access.schema.json").read_text())
    example = json.loads((root / "cases/service-access.example.json").read_text())
    Draft202012Validator(schema).validate(example)


def test_judge_access_is_secret_free_and_requires_separate_environment(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "judge-access.json"
    path.write_text(
        json.dumps(
            {
                "base_url": "https://judge.test/v1",
                "model": "judge-model",
                "api_key_env": "TEST_JUDGE_KEY",
                "requests_per_minute": 5,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_JUDGE_KEY", "judge-runtime-secret")

    access = JudgeAccess.load(path)

    assert access.credential() == "judge-runtime-secret"
    assert "judge-runtime-secret" not in path.read_text()


def test_service_access_rejects_literal_credentials(tmp_path: Path) -> None:
    path = write_service(
        tmp_path,
        extra={"headers": {"Authorization": "Bearer do-not-store-this-token"}},
    )
    with pytest.raises(ConfigError) as error:
        ServiceAccess.load(path)
    assert error.value.code == "credential_material_forbidden"


def test_secret_scanner_allows_public_task_name_with_embedded_sk_prefix() -> None:
    assert find_secret({"task_names": ["aiomonitor-task-snapshots-diff"]}) is None


@pytest.mark.parametrize("prefix", ["sk-", "sk_"])
def test_secret_scanner_rejects_standalone_api_key_prefixes(prefix: str) -> None:
    assert find_secret(f"credential={prefix}runtimeonlysecret") is not None


def test_suite_result_runtime_schema_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "suite-result.json"
    payload = SuiteResult(suite_id="suite", row_ids=["R1"], status="passed").to_dict()
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        SuiteResult.load(path)
    assert error.value.code == "suite_result_invalid"
    assert "unexpected" in str(error.value)


def test_suite_result_write_registers_its_result_artifact(tmp_path: Path) -> None:
    path = tmp_path / "suite-result.json"
    result = SuiteResult(suite_id="suite", row_ids=["R1"], status="passed")
    result.write(path)
    assert str(path.resolve()) in result.artifact_paths


def test_completeness_rejects_partial_pass() -> None:
    result = SuiteResult(
        suite_id="fixture",
        row_ids=["R1"],
        status="passed",
        counts={"total": 1, "passed": 1},
        coverage={"observed_trials": 2},
    )
    enforce_completeness(
        result,
        {"expected_counts": {"total": 2, "passed": 2}, "expected_trials": 3},
    )
    assert result.status == "failed"
    assert result.validity == "execution_invalid"
    assert result.reason == "artifact_contract_incomplete"


def test_completeness_accepts_required_artifact_pattern() -> None:
    result = SuiteResult(
        suite_id="inspect",
        row_ids=["R1"],
        status="passed",
        artifact_paths=["/tmp/suite/inspect-2026.eval"],
    )
    enforce_completeness(result, {"required_artifact_patterns": ["**/*.eval"]})
    assert result.status == "passed"


def test_develop_runs_only_selected_case_and_stops(tmp_path: Path, monkeypatch) -> None:
    write_model(
        tmp_path,
        [
            fake_suite("first", "R1", status="failed", selector="R1/case"),
            fake_suite("second", "R2", selector="R2/case"),
        ],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    outcome = Orchestrator(root=tmp_path).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=["R1/case", "R2/case"],
    )
    assert outcome.exit_code == 1
    states = json.loads((outcome.run_dir / "run.json").read_text())["suite_states"]
    assert states[0]["status"] == "failed"
    assert states[1]["status"] == "not_run"
    assert states[1]["reason"] == "develop_fail_fast"
    diagnosis = json.loads((outcome.run_dir / "diagnosis.json").read_text())
    assert diagnosis["minimal_rerun"][-1] == "R1/case"
    assert "runtime-only-secret" not in (outcome.run_dir / "reproduce.sh").read_text()
    suite_input = outcome.run_dir / "suites" / "001-first" / "suite-input.json"
    assert suite_input.is_file()
    suite_payload = json.loads(suite_input.read_text())
    assert suite_payload["selector"] == "R1/case"
    assert suite_payload["service"]["api_key_env"] == "TEST_VENDOR_API_KEY"
    assert "runtime-only-secret" not in suite_input.read_text()
    suite_result = json.loads(
        (outcome.run_dir / "suites" / "001-first" / "suite-result.json").read_text()
    )
    assert str(suite_input.resolve()) in suite_result["artifact_paths"]


def test_accept_stops_after_valid_failure_and_records_blocked_tail(tmp_path: Path, monkeypatch) -> None:
    write_model(
        tmp_path,
        [
            fake_suite("failed-short", "R1", status="failed"),
            fake_suite("passed-short", "R2"),
            fake_suite("failed-long", "R3", status="failed", long_test="benchmark"),
        ],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    outcome = Orchestrator(root=tmp_path).execute(
        mode="accept", model_id="test-model", service_access_path=service
    )
    states = json.loads((outcome.run_dir / "run.json").read_text())["suite_states"]
    assert [state["status"] for state in states] == ["failed", "blocked", "not_run"]
    assert states[2]["reason"] == "long_test_not_selected"
    assert states[2]["selected"] is False
    assert outcome.exit_code == 1
    assert outcome.report["official_row_count"] == 3

    included = Orchestrator(root=tmp_path).execute(
        mode="accept",
        model_id="test-model",
        service_access_path=service,
        include_long={"benchmark"},
    )
    included_states = json.loads((included.run_dir / "run.json").read_text())["suite_states"]
    assert [state["status"] for state in included_states] == ["failed", "blocked", "blocked"]


def test_accept_blocks_after_runner_contract_failure(tmp_path: Path, monkeypatch) -> None:
    write_model(
        tmp_path,
        [
            fake_suite(
                "broken",
                "R1",
                status="failed",
                validity="execution_invalid",
                reason="runner_contract_invalid",
            ),
            fake_suite("would-pass", "R2"),
        ],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    outcome = Orchestrator(root=tmp_path).execute(
        mode="accept", model_id="test-model", service_access_path=service
    )
    states = json.loads((outcome.run_dir / "run.json").read_text())["suite_states"]
    assert states[0]["validity"] == "execution_invalid"
    assert states[1]["status"] == "blocked"
    assert states[1]["reason"] == "blocked_by:broken"
    assert outcome.exit_code == 2
    assert (outcome.run_dir / "acceptance.json").is_file()
    assert (outcome.run_dir / "acceptance.md").is_file()


def test_accept_stops_after_suite_local_execution_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(
        tmp_path,
        [
            fake_suite(
                "overloaded",
                "R1",
                status="failed",
                validity="execution_invalid",
                reason="inspect_invalid_finish_reason",
            ),
            fake_suite("still-runs", "R2"),
        ],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    outcome = Orchestrator(root=tmp_path).execute(
        mode="accept", model_id="test-model", service_access_path=service
    )

    states = json.loads((outcome.run_dir / "run.json").read_text())["suite_states"]
    assert [state["status"] for state in states] == ["failed", "blocked"]
    assert states[0]["validity"] == "execution_invalid"
    assert outcome.exit_code == 2


def test_accept_blocks_every_suite_after_service_preflight_failure(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(
        tmp_path,
        [fake_suite("first", "R1"), fake_suite("second", "R2")],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    preflight = {
        "schema_version": 1,
        "status": "blocked",
        "validity": "service_invalid",
        "reason": "credential_invalid",
        "http_status": 401,
        "response_headers": {"x-request-id": "request-1"},
        "failure_summary": "service preflight returned HTTP 401",
        "started_at": "now",
        "finished_at": "now",
    }

    outcome = Orchestrator(root=tmp_path, service_probe=lambda _: preflight).execute(
        mode="accept", model_id="test-model", service_access_path=service
    )
    states = json.loads((outcome.run_dir / "run.json").read_text())["suite_states"]
    assert outcome.exit_code == 2
    assert outcome.report["contract_invalid"] is True
    assert [state["status"] for state in states] == ["blocked", "blocked"]
    assert {state["validity"] for state in states} == {"service_invalid"}
    assert (outcome.run_dir / "service-preflight.json").is_file()


def test_service_probe_classifies_models_quota_body(tmp_path: Path, monkeypatch) -> None:
    service = ServiceAccess.load(
        write_service(
            tmp_path,
            extra={"base_url": "https://api.test.local/v1", "served_model": "model"},
        )
    )
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class QuotaResponse:
        status = 403
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"error":{"message":"not enough balance"}}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: QuotaResponse())
    result = probe_service(service)
    assert result["status"] == "blocked"
    assert result["validity"] == "service_invalid"
    assert result["reason"] == "quota_exhausted"


def test_service_probe_allows_model_alias_missing_from_models_list(
    tmp_path: Path, monkeypatch
) -> None:
    service_path = write_service(
        tmp_path,
        extra={"base_url": "https://api.test.local/v1", "served_model": "missing-model"},
    )
    service = ServiceAccess.load(service_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return b'{"data":[{"id":"available-model"}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    result = probe_service(service)
    assert result["status"] == "passed"
    assert result["validity"] == "valid"
    assert result["reason"] == "served_model_catalog_alias"
    assert result["served_model_listed"] is False
    assert result["model_catalog_alias_mismatch"] is True


def test_service_probe_allows_reachable_endpoint_with_unparseable_models_body(
    tmp_path: Path, monkeypatch
) -> None:
    service_path = write_service(
        tmp_path,
        extra={"base_url": "https://api.test.local/v1", "served_model": "alias-model"},
    )
    service = ServiceAccess.load(service_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class Response:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self) -> bytes:
            return b"gateway reachable"

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    result = probe_service(service)
    assert result["status"] == "passed"
    assert result["validity"] == "valid"
    assert result["reason"] is None
    assert result["served_model_listed"] is None
    assert result["model_catalog_alias_mismatch"] is False


def test_service_probe_blocks_quota_from_real_chat_request(
    tmp_path: Path, monkeypatch
) -> None:
    service_path = write_service(
        tmp_path,
        extra={"base_url": "https://api.test.local/v1", "served_model": "model"},
    )
    service = ServiceAccess.load(service_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class ModelsResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"data":[{"id":"model"}]}'

    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelsResponse()
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "Forbidden",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":{"message":"not enough balance"}}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = probe_service(service)
    assert result["status"] == "blocked"
    assert result["validity"] == "service_invalid"
    assert result["reason"] == "quota_exhausted"
    assert result["chat_http_status"] == 403


def _dirty_pins() -> dict:
    return {
        "novita_self_test": {
            "path": "/tmp/self-test",
            "commit": "a" * 40,
            "branch": "main",
            "tracked_dirty": True,
            "tracked_diff_sha256": "b" * 64,
            "untracked_count": 1,
            "untracked_sha256": "c" * 64,
        },
        "kvv_k3": {
            "path": "/tmp/kvv",
            "commit": "d" * 40,
            "branch": "main",
            "tracked_dirty": False,
            "tracked_diff_sha256": None,
            "untracked_count": 0,
            "untracked_sha256": None,
        },
    }


def test_accept_rejects_dirty_child_repository(tmp_path: Path, monkeypatch) -> None:
    write_model(tmp_path, [fake_suite("only", "R1")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    monkeypatch.setattr("novita_self_test.orchestrator.repository_pins", lambda _: _dirty_pins())

    with pytest.raises(ConfigError) as error:
        Orchestrator(root=tmp_path).execute(
            mode="accept", model_id="test-model", service_access_path=service
        )
    assert error.value.code == "repository_worktree_dirty"
    assert not (tmp_path / "runs").exists()


def test_develop_records_deepswe_rounds_override(tmp_path: Path, monkeypatch) -> None:
    write_model(tmp_path, [fake_suite("only", "R1", selector="R1/case")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    outcome = Orchestrator(root=tmp_path).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=["R1/case"],
        deepswe_rounds_override=3,
    )
    manifest = json.loads((outcome.run_dir / "run.json").read_text())
    assert manifest["deepswe_rounds"] == 3

    with pytest.raises(ConfigError) as error:
        Orchestrator(root=tmp_path).execute(
            mode="accept",
            model_id="test-model",
            service_access_path=service,
            deepswe_rounds_override=3,
        )
    assert error.value.code == "deepswe_rounds_override_forbidden"


def test_develop_records_and_forwards_deepswe_supplemental_plan(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(tmp_path, [fake_suite("deepswe", "17", selector="17")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    observed: dict = {}

    class ObservingAdapter:
        def run(self, context):
            observed.update(context.deepswe_supplemental_plan or {})
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="passed",
            )

    monkeypatch.setattr(
        "novita_self_test.orchestrator.adapter_for", lambda _: ObservingAdapter()
    )
    plan = {
        "source_run_id": "source-run",
        "round_tasks": [{"round": 1, "task": "task-a"}],
    }
    outcome = Orchestrator(root=tmp_path).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=["17"],
        concurrency_override=5,
        deepswe_rounds_override=3,
        deepswe_supplemental_plan=plan,
    )

    manifest = json.loads((outcome.run_dir / "run.json").read_text())
    assert manifest["deepswe_supplemental_plan"] == plan
    assert observed == plan


def test_develop_supplemental_plan_uses_profile_deepswe_row(
    tmp_path: Path, monkeypatch
) -> None:
    # kimi-k3-flash numbers DeepSWE as row 10 (not 17); the supplemental gate
    # must follow the profile's actual row id instead of a hardcoded value.
    write_model(tmp_path, [fake_suite("deepswe", "10", selector="10")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class ObservingAdapter:
        def run(self, context):
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="passed",
            )

    monkeypatch.setattr(
        "novita_self_test.orchestrator.adapter_for", lambda _: ObservingAdapter()
    )
    plan = {
        "source_run_id": "source-run",
        "round_tasks": [{"round": 1, "task": "task-a"}],
    }
    outcome = Orchestrator(root=tmp_path).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=["10"],
        concurrency_override=5,
        deepswe_rounds_override=3,
        deepswe_supplemental_plan=plan,
    )
    manifest = json.loads((outcome.run_dir / "run.json").read_text())
    assert manifest["deepswe_supplemental_plan"] == plan

    with pytest.raises(ConfigError) as error:
        Orchestrator(root=tmp_path).execute(
            mode="develop",
            model_id="test-model",
            service_access_path=service,
            selectors=["9"],
            concurrency_override=5,
            deepswe_rounds_override=3,
            deepswe_supplemental_plan=plan,
        )
    assert error.value.code == "deepswe_supplemental_invalid"


def test_concurrency_override_must_be_positive(tmp_path: Path, monkeypatch) -> None:
    write_model(tmp_path, [fake_suite("only", "R1")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    with pytest.raises(ConfigError) as error:
        Orchestrator(root=tmp_path).execute(
            mode="accept",
            model_id="test-model",
            service_access_path=service,
            concurrency_override=0,
        )
    assert error.value.code == "concurrency_invalid"


def test_accept_forbids_concurrency_override(tmp_path: Path, monkeypatch) -> None:
    write_model(tmp_path, [fake_suite("only", "R1")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    with pytest.raises(ConfigError) as error:
        Orchestrator(root=tmp_path).execute(
            mode="accept",
            model_id="test-model",
            service_access_path=service,
            concurrency_override=2,
        )
    assert error.value.code == "concurrency_override_forbidden"


def test_accept_cli_exposes_explicit_nonblocking_concurrency_override() -> None:
    args = _parser().parse_args(
        [
            "accept",
            "--model",
            "kimi-k3",
            "--service-access",
            "service.json",
            "--acceptance",
            "full",
            "--concurrency",
            "100",
            "--continue-on-failure",
        ]
    )
    assert args.concurrency == 100
    assert args.continue_on_failure is True


def test_develop_records_dirty_fingerprint_and_profile_hashes(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(tmp_path, [fake_suite("only", "R1", selector="R1/case")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    monkeypatch.setattr("novita_self_test.orchestrator.repository_pins", lambda _: _dirty_pins())

    outcome = Orchestrator(root=tmp_path).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=["R1/case"],
        concurrency_override=200,
    )
    manifest = json.loads((outcome.run_dir / "run.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["concurrency_override"] == 200
    assert manifest["repo_pins"] == _dirty_pins()
    assert set(manifest["profile_hashes"]) == {
        "models/test-model/profile.yaml",
        "models/test-model/requirements/rows.yaml",
        "models/test-model/requirements/source.yaml",
        "models/test-model/suite-manifest.yaml",
    }


def test_report_rejects_profile_content_drift(tmp_path: Path, monkeypatch) -> None:
    model = write_model(tmp_path, [fake_suite("only", "R1", selector="R1/case")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    outcome = Orchestrator(root=tmp_path).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=["R1/case"],
    )
    profile_path = model / "profile.yaml"
    profile_path.write_text(profile_path.read_text() + "description: changed\n")

    with pytest.raises(ConfigError) as error:
        regenerate_report(outcome.run_dir, root=tmp_path)
    assert error.value.code == "profile_content_changed"


def test_requirements_refresh_is_read_only_until_apply(tmp_path: Path) -> None:
    write_model(tmp_path, [fake_suite("suite", "R1")])
    source = tmp_path / "models/test-model/requirements/source.yaml"
    before = source.read_text()

    def fetcher(_: str) -> dict:
        return {
            "doc_token": "new-token",
            "doc_revision": 2,
            "content_sha256": "a" * 64,
            "attachment_lock": [{"name": "fixture.jsonl", "token": "file-token", "size": 12}],
        }

    diff = refresh_requirements(
        "test-model", doc="new-token", root=tmp_path, fetcher=fetcher
    )
    assert diff["changes"]["doc_revision"] == {"before": 1, "after": 2}
    assert source.read_text() == before
    applied = refresh_requirements(
        "test-model", doc="new-token", root=tmp_path, fetcher=fetcher, apply=True
    )
    assert applied["applied"] is True
    assert "new-token" in source.read_text()


def test_structured_table_digest_tracks_acceptance_row_changes() -> None:
    header = "## K3-vendor-test-result\n\n| index | job |\n| --- | --- |\n"
    content = header + "".join(f"| {index} | job-{index} |\n" for index in range(15))
    changed = content.replace("job-14", "DeepSWE changed")

    assert _structured_rows_sha256(content)
    assert _structured_rows_sha256(content) != _structured_rows_sha256(changed)


def test_structured_table_digest_parses_feishu_html_rowspans() -> None:
    rows = "".join(
        f"<tr><td>{index}</td><td>job-{index}</td><td>{index} pass</td></tr>"
        for index in range(2, 15)
    )
    content = (
        "# K3-vendor-test-result\n"
        "<table><tbody>"
        "<tr><td>index</td><td>job</td><td>result</td></tr>"
        "<tr><td>0</td><td rowspan=\"2\">Stream spec</td><td>p0</td></tr>"
        "<tr><td>1</td><td>p1</td></tr>"
        f"{rows}</tbody></table>"
    )
    changed = content.replace("job-14", "DeepSWE changed")

    assert _structured_rows_sha256(content)
    assert _structured_rows_sha256(content) != _structured_rows_sha256(changed)


def test_stream_snapshot_digest_joins_consecutive_official_python_blocks() -> None:
    first = "import pytest\n\ndef test_stream_spec_first():\n    pass\n"
    second = "@pytest.mark.priority('P0')\ndef test_stream_spec_second():\n    pass\n"
    content = (
        "```Python\n"
        f"{first}```\n"
        "```python\n"
        f"{second}```\n"
        "```python\nprint('unrelated')\n```\n"
    )

    assert _python_snapshot_sha256(content) == hashlib.sha256(
        f"{first}{second}".encode("utf-8")
    ).hexdigest()


def test_stream_snapshot_digest_reads_split_feishu_xml_code_blocks() -> None:
    first = "import pytest\n\ndef test_stream_spec_first():\n    assert 1 &lt; 2\n"
    second = "@pytest.mark.priority(&quot;P0&quot;)\ndef test_stream_spec_second():\n    pass\n"
    content = (
        '<pre lang="Bash"><code>pytest test_stream_spec.py</code></pre>'
        f'<pre lang="Python"><code>{first.replace(chr(10), "<br/>")}</code></pre>'
        f'<pre lang="python"><code>{second.replace(chr(10), "<br/>")}</code></pre>'
    )
    expected = f"{first}{second}".replace("&lt;", "<").replace("&quot;", '"')

    assert _xml_python_snapshot_sha256(content) == hashlib.sha256(
        expected.encode("utf-8")
    ).hexdigest()


def test_k3_style_refresh_fetches_both_docs_and_requires_reviewed_table_change(
    tmp_path: Path,
) -> None:
    model = write_model(tmp_path, [fake_suite("suite", "R1")])
    profile_path = model / "profile.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["requirements"] = {"stream_spec_doc_token": "stream-doc"}
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

    stream_snapshot = tmp_path / "stream.py"
    stream_snapshot.write_text("def test_stream_spec_case():\n    pass\n", encoding="utf-8")
    stream_sha = hashlib.sha256(stream_snapshot.read_bytes()).hexdigest()
    rows_path = model / "requirements/rows.yaml"
    initial_rows_sha = hashlib.sha256(rows_path.read_bytes()).hexdigest()
    source_path = model / "requirements/source.yaml"
    source = yaml.safe_load(source_path.read_text())
    source.update(
        {
            "content_sha256": "1" * 64,
            "snapshot_revision_format": "test-docx-{doc_revision}-stream-{stream_spec_revision}",
            "stream_spec_revision": 1,
            "table_contract": {
                "title": "K3-vendor-test-result",
                "structured_rows_sha256": "2" * 64,
                "rows_file_sha256": initial_rows_sha,
            },
            "stream_spec_contract": {
                "doc_token": "stream-doc",
                "doc_revision": 1,
                "snapshot_path": "stream.py",
                "sha256": stream_sha,
            },
        }
    )
    source_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")

    candidate_table_sha = "3" * 64
    fetched = []

    def fetcher(token: str) -> dict:
        fetched.append(token)
        if token == "main-doc":
            return {
                "doc_token": "main-doc",
                "doc_revision": 2,
                "content_sha256": "4" * 64,
                "structured_rows_sha256": candidate_table_sha,
                "attachment_lock": [],
            }
        assert token == "stream-doc"
        return {
            "doc_token": "stream-doc",
            "doc_revision": 2,
            "content_sha256": "5" * 64,
            "snapshot_sha256": stream_sha,
            "attachment_lock": [],
        }

    diff = refresh_requirements(
        "test-model", doc="main-doc", root=tmp_path, fetcher=fetcher
    )
    assert fetched == ["main-doc", "stream-doc"]
    assert diff["rows"]["changed"] is True
    assert diff["stream_spec"]["executable_changed"] is False
    with pytest.raises(ConfigError) as unreviewed:
        refresh_requirements(
            "test-model", doc="main-doc", root=tmp_path, fetcher=fetcher, apply=True
        )
    assert unreviewed.value.code == "requirements_rows_changed"

    rows = yaml.safe_load(rows_path.read_text())
    rows["official"][0]["reviewed_table_change"] = True
    rows_path.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")
    applied = refresh_requirements(
        "test-model",
        doc="main-doc",
        root=tmp_path,
        fetcher=fetcher,
        apply=True,
        accept_table_sha=candidate_table_sha,
    )
    updated = yaml.safe_load(source_path.read_text())
    assert applied["applied"] is True
    assert updated["doc_revision"] == 2
    assert updated["stream_spec_revision"] == 2
    assert updated["table_contract"]["structured_rows_sha256"] == candidate_table_sha


def test_run_manifest_records_running_lifecycle(tmp_path: Path, monkeypatch) -> None:
    write_model(tmp_path, [fake_suite("only", "R1", selector="R1/case")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    observed: dict = {}

    class ObservingAdapter:
        def run(self, context):
            manifest = json.loads((context.run_dir / "run.json").read_text())
            observed.update(manifest["suite_states"][0])
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="passed",
                counts={"total": 1, "passed": 1},
            )

    monkeypatch.setattr(
        "novita_self_test.orchestrator.adapter_for", lambda _: ObservingAdapter()
    )
    outcome = Orchestrator(root=tmp_path).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=["R1/case"],
    )

    assert observed["status"] == "running"
    assert observed["heartbeat_at"]
    assert observed["process_id"] > 0
    assert observed["process_group_id"] > 0
    final = json.loads((outcome.run_dir / "run.json").read_text())["suite_states"][0]
    assert final["status"] == "passed"
    assert final["phase"] == "finished"
    assert final["progress"] == {"phase": "finished", "completed": 1, "total": 1}
    events = [
        json.loads(line)
        for line in (outcome.run_dir / "events.jsonl").read_text().splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert {event["event"] for event in events} >= {
        "run_started",
        "preflight_completed",
        "suite_started",
        "progress_updated",
        "suite_completed",
        "run_completed",
    }


def test_beam_generation_releases_acceptance_queue_before_background_judge(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(
        tmp_path,
        [fake_suite("beam", "R1"), fake_suite("next", "R2")],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    order: list[str] = []

    class BeamGenerationAdapter:
        def run(self, context):
            order.append("beam-generation")
            return SuiteResult(
                suite_id="beam",
                row_ids=["R1"],
                status="deferred",
                reason="beam_judge_pending",
                counts={"total": 1, "completed": 1},
                coverage={"generation_complete": True},
            )

        def has_pending_work(self, _result):
            return True

        def finalize(self, _context, result):
            order.append("beam-judge")
            result.status = "passed"
            result.reason = None
            result.coverage["background_judge_pending"] = False
            return result

    class NextAdapter:
        def run(self, _context):
            order.append("next-suite")
            return SuiteResult(suite_id="next", row_ids=["R2"], status="passed")

    adapters = iter([BeamGenerationAdapter(), NextAdapter()])
    monkeypatch.setattr("novita_self_test.orchestrator.adapter_for", lambda _: next(adapters))
    outcome = Orchestrator(root=tmp_path).execute(
        mode="accept",
        model_id="test-model",
        service_access_path=service,
    )

    assert order == ["beam-generation", "next-suite", "beam-judge"]
    assert outcome.report["verdict"] == "passed"
    states = json.loads((outcome.run_dir / "run.json").read_text())["suite_states"]
    assert [state["status"] for state in states] == ["passed", "passed"]


def test_resume_retries_human_interrupted_suite_in_place(tmp_path: Path, monkeypatch) -> None:
    write_model(tmp_path, [fake_suite("only", "R1", selector="R1/case")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class InterruptOnce:
        calls = 0
        overrides = []

        def run(self, context):
            self.calls += 1
            self.overrides.append(context.concurrency_override)
            if self.calls == 1:
                raise KeyboardInterrupt
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="passed",
            )

    adapter = InterruptOnce()
    monkeypatch.setattr("novita_self_test.orchestrator.adapter_for", lambda _: adapter)
    with pytest.raises(UserInterrupted) as interrupted:
        Orchestrator(root=tmp_path).execute(
            mode="develop",
            model_id="test-model",
            service_access_path=service,
            selectors=["R1/case"],
            concurrency_override=200,
        )
    run_dir = interrupted.value.run_dir
    interrupted_manifest = json.loads((run_dir / "run.json").read_text())
    assert interrupted_manifest["finished_at"] is None
    assert interrupted_manifest["suite_states"][0]["status"] == "paused"
    assert interrupted_manifest["suite_states"][0]["phase"] == "paused"
    resumed = Orchestrator(root=tmp_path).resume(run_dir)
    assert resumed.run_dir == run_dir
    assert resumed.exit_code == 0
    assert adapter.overrides == [200, 200]
    assert json.loads((run_dir / "run.json").read_text())["finished_at"] is not None


def test_resume_continue_from_terminal_run_archives_and_reruns_affected_suites(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(
        tmp_path,
        [
            fake_suite("passed-suite", "R1"),
            fake_suite("failed-suite", "R2", status="failed", reason="benchmark_below_threshold"),
            fake_suite("blocked-suite", "R3"),
        ],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    monkeypatch.setattr(
        "novita_self_test.orchestrator.probe_service",
        lambda _service: {"status": "passed", "validity": "valid", "reason": None, "http_status": 200},
    )

    adapters = {}

    class FakeAdapter:
        def __init__(self, suite_id: str, results: list[tuple[str, str, str | None]]) -> None:
            self.suite_id = suite_id
            self.calls = 0
            self._results = list(results)

        def run(self, context):
            self.calls += 1
            status, validity, reason = self._results.pop(0)
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status=status,
                validity=validity,
                reason=reason,
                artifact_paths=[str((context.suite_dir / "original.txt").resolve())],
            )

    adapters["passed-suite"] = FakeAdapter("passed-suite", [("passed", "valid", None)])
    adapters["failed-suite"] = FakeAdapter(
        "failed-suite",
        [("failed", "valid", "benchmark_below_threshold"), ("passed", "valid", None)],
    )
    adapters["blocked-suite"] = FakeAdapter(
        "blocked-suite",
        [("passed", "valid", None)],
    )
    # execute() requests adapters for passed/failed only (fail-fast blocks the
    # third suite without an adapter call); the continuation requests
    # failed/blocked in order.
    selected = iter(
        (
            adapters["passed-suite"],
            adapters["failed-suite"],
            adapters["failed-suite"],
            adapters["blocked-suite"],
        )
    )
    monkeypatch.setattr(
        "novita_self_test.orchestrator.adapter_for",
        lambda _adapter: next(selected),
    )

    outcome = Orchestrator(root=tmp_path).execute(
        mode="accept",
        model_id="test-model",
        service_access_path=service,
    )
    run_dir = outcome.run_dir
    manifest = json.loads((run_dir / "run.json").read_text())
    assert [state["status"] for state in manifest["suite_states"]] == [
        "passed",
        "failed",
        "blocked",
    ]
    for execution_id in ("002-failed-suite", "003-blocked-suite"):
        suite_dir = run_dir / "suites" / execution_id
        (suite_dir / "original.txt").write_text(f"original {execution_id}", encoding="utf-8")
    checkpoint = {
        "waves": {
            "1": {
                "1": [
                    {
                        "task": "task-a",
                        "stdout_path": str(
                            (run_dir / "suites" / "002-failed-suite" / "round-01.stdout.log").resolve()
                        ),
                        "stderr_path": str(
                            (run_dir / "suites" / "002-failed-suite" / "round-01.stderr.log").resolve()
                        ),
                    }
                ]
            }
        }
    }
    failed_dir = run_dir / "suites" / "002-failed-suite"
    (failed_dir / "deepswe-checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
    (failed_dir / "round-01.stdout.log").write_text("checkpointed log", encoding="utf-8")
    (failed_dir / "round-01.stderr.log").write_text("checkpointed err", encoding="utf-8")
    monkeypatch.setattr(
        "novita_self_test.orchestrator._checkpoint_paths",
        lambda _suite, suite_dir: [str(Path(suite_dir) / "deepswe-checkpoint.json")],
    )
    result_path = run_dir / "suites" / "002-failed-suite" / "suite-result.json"
    result = json.loads(result_path.read_text())
    result["artifact_paths"] = [str((run_dir / "suites" / "002-failed-suite" / "original.txt").resolve())]
    result_path.write_text(json.dumps(result), encoding="utf-8")

    resumed = Orchestrator(root=tmp_path).resume(
        run_dir,
        continue_from="failed-suite",
        continue_reason="cluster recovered; rerun BEAM then continue acceptance",
    )
    assert resumed.run_dir == run_dir
    manifest = json.loads((run_dir / "run.json").read_text())
    assert manifest["continuations"][-1]["from_suite"] == "failed-suite"
    assert manifest["continuations"][-1]["reason"] == "cluster recovered; rerun BEAM then continue acceptance"
    archives = sorted(path.name for path in (run_dir / "continuations").iterdir())
    assert len(archives) == 1
    archive = run_dir / "continuations" / archives[0]
    assert (archive / "suites" / "002-failed-suite" / "original.txt").read_text() == "original 002-failed-suite"
    assert (archive / "suites" / "003-blocked-suite" / "original.txt").read_text() == "original 003-blocked-suite"
    assert (archive / "run.json").is_file()
    assert (archive / "acceptance.json").is_file()
    restored = manifest["continuations"][-1]["restored_checkpoints"]
    assert str((failed_dir / "deepswe-checkpoint.json").resolve()) in restored
    assert (failed_dir / "deepswe-checkpoint.json").is_file()
    assert (failed_dir / "round-01.stdout.log").read_text() == "checkpointed log"
    assert (failed_dir / "round-01.stderr.log").read_text() == "checkpointed err"
    assert adapters["passed-suite"].calls == 1
    assert adapters["failed-suite"].calls == 2
    assert adapters["blocked-suite"].calls == 1
    assert [state["suite_id"] for state in manifest["suite_states"]] == [
        "passed-suite",
        "failed-suite",
        "blocked-suite",
    ]
    assert manifest["suite_states"][0]["status"] == "passed"
    assert manifest["suite_states"][1]["status"] == "passed"
    assert manifest["suite_states"][2]["status"] == "passed"


def test_resume_only_suite_reruns_single_terminal_suite_in_place(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(
        tmp_path,
        [
            fake_suite("passed-suite", "R1"),
            fake_suite("failed-suite", "R2", status="failed", reason="benchmark_below_threshold"),
            fake_suite("blocked-suite", "R3"),
        ],
    )
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    monkeypatch.setattr(
        "novita_self_test.orchestrator.probe_service",
        lambda _service: {"status": "passed", "validity": "valid", "reason": None, "http_status": 200},
    )

    class FakeAdapter:
        def __init__(self, results):
            self.calls = 0
            self._results = list(results)

        def run(self, context):
            self.calls += 1
            status, validity, reason = self._results.pop(0)
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status=status,
                validity=validity,
                reason=reason,
            )

    first = FakeAdapter([("passed", "valid", None)])
    second = FakeAdapter(
        [("failed", "valid", "benchmark_below_threshold"), ("passed", "valid", None)]
    )
    third = FakeAdapter([("blocked", "execution_invalid", "blocked_by:failed-suite")])
    # execute() requests adapters for the first two suites; fail-fast blocks
    # the third without an adapter call; the single-suite resume requests only
    # the failed suite again.
    selected = iter((first, second, second))
    monkeypatch.setattr(
        "novita_self_test.orchestrator.adapter_for",
        lambda _adapter: next(selected),
    )

    outcome = Orchestrator(root=tmp_path).execute(
        mode="accept",
        model_id="test-model",
        service_access_path=service,
    )
    run_dir = outcome.run_dir
    assert not (run_dir / "continuations").exists()

    resumed = Orchestrator(root=tmp_path).resume(
        run_dir,
        continue_from="failed-suite",
        continue_reason="rerun only this suite after cluster recovery",
        only_suite=True,
    )
    assert resumed.run_dir == run_dir
    manifest = json.loads((run_dir / "run.json").read_text())
    entry = manifest["continuations"][-1]
    assert entry["scope"] == "single_suite"
    assert "archive_path" not in entry
    assert not (run_dir / "continuations").exists()
    assert first.calls == 1
    assert second.calls == 2
    assert third.calls == 0
    assert [state["status"] for state in manifest["suite_states"]] == [
        "passed",
        "passed",
        "blocked",
    ]
