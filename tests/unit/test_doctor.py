from __future__ import annotations

import json
from pathlib import Path

from novita_self_test.config import ModelProfile, ServiceAccess
from novita_self_test.doctor import _capacity_probe, _execution_plan, run_doctor

from .helpers import write_model, write_service


def _fake_suite() -> dict:
    return {
        "suite_id": "only",
        "adapter": "fake",
        "row_ids": ["R1"],
        "mandatory": True,
        "config": {"fake_result": {"status": "passed", "validity": "valid"}},
        "completeness": {},
    }


def _fake_beam_suite() -> dict:
    return {
        "suite_id": "beam",
        "adapter": "beam",
        "row_ids": ["R16"],
        "mandatory": True,
        "config": {
            "env": {
                "NVST_BEAM_JUDGE_BASE_URL": "https://vendor.example/v1",
                "NVST_BEAM_JUDGE_MODEL": "judge-model",
                "NVST_BEAM_JUDGE_API_KEY_ENV": "TEST_VENDOR_API_KEY",
            },
            "runner_fingerprint": {"judge_requests_per_minute": 5},
        },
        "completeness": {},
    }


def test_capacity_probe_records_concurrency_levels_without_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return b'{"choices": [{"message": {"content": "OK"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    result = _capacity_probe(ServiceAccess.load(service), levels=(1, 2), requests_per_level=2)

    assert result["status"] == "passed"
    assert [item["concurrency"] for item in result["levels"]] == [1, 2]
    assert [item["requests"] for item in result["levels"]] == [2, 4]
    assert "runtime-only-secret" not in json.dumps(result)


def test_execution_plan_derives_toolcall_trials_from_dataset_and_epochs(
    tmp_path: Path,
) -> None:
    suite = {
        "suite_id": "toolcall",
        "adapter": "inspect",
        "row_ids": ["R1"],
        "mandatory": True,
        "config": {
            "cwd": ".",
            "command": ["python", "eval.py", "--dataset", "cases.jsonl", "--epochs", "2"],
            "runner_fingerprint": {"artifact_contract": "eval"},
        },
        "completeness": {"expected_trials": 4},
    }
    write_model(tmp_path, [suite])
    (tmp_path / "cases.jsonl").write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
    profile = ModelProfile.load("test-model", root=tmp_path)
    plan = _execution_plan(profile, "full", tmp_path)
    assert plan["status"] == "passed"
    assert plan["suites"][0]["dataset_samples"] == 2
    assert plan["suites"][0]["derived_trials"] == 4


def test_execution_plan_rejects_toolcall_trial_mismatch(tmp_path: Path) -> None:
    suite = {
        "suite_id": "toolcall",
        "adapter": "inspect",
        "row_ids": ["R1"],
        "mandatory": True,
        "config": {
            "cwd": ".",
            "command": ["python", "eval.py", "--dataset", "cases.jsonl", "--epochs", "2"],
            "runner_fingerprint": {"artifact_contract": "eval"},
        },
        "completeness": {"expected_trials": 5},
    }
    write_model(tmp_path, [suite])
    (tmp_path / "cases.jsonl").write_text('{"id": 1}\n{"id": 2}\n', encoding="utf-8")
    profile = ModelProfile.load("test-model", root=tmp_path)
    plan = _execution_plan(profile, "full", tmp_path)
    assert plan["status"] == "blocked"
    assert "derived 4" in plan["errors"][0]


def test_doctor_passes_without_sending_vendor_traffic(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(tmp_path, [_fake_suite()])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")

    result = run_doctor("test-model", service_access_path=service, root=tmp_path)

    assert result["status"] == "passed"
    assert result["zero_vendor_traffic"] is True
    assert result["blocked_checks"] == []
    assert {item["name"] for item in result["checks"]} == {
        "execution_site",
        "service_access",
        "endpoint_credential_env",
        "repository_cleanliness",
        "runner_entrypoints",
    }


def test_doctor_online_canary_is_explicit_and_marks_vendor_traffic(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(tmp_path, [_fake_suite()])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    observed: dict[str, bool] = {}

    def fake_online_checks(**_kwargs):
        observed["called"] = True
        return [
            {
                "name": "online_endpoint_canary",
                "status": "passed",
                "reason": None,
                "details": {"http_status": 200},
            }
        ]

    monkeypatch.setattr("novita_self_test.doctor._online_checks", fake_online_checks)

    result = run_doctor(
        "test-model",
        service_access_path=service,
        root=tmp_path,
        online_canary=True,
    )

    assert observed["called"] is True
    assert result["zero_vendor_traffic"] is False
    assert result["status"] == "passed"


def test_doctor_reports_missing_credential_without_exposing_secret(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(tmp_path, [_fake_suite()])
    service = write_service(tmp_path)
    monkeypatch.delenv("TEST_VENDOR_API_KEY", raising=False)

    result = run_doctor("test-model", service_access_path=service, root=tmp_path)

    assert result["status"] == "blocked"
    assert result["blocked_checks"] == ["endpoint_credential_env"]
    assert "runtime-only-secret" not in str(result)


def test_doctor_allows_intentionally_shared_endpoint_and_judge_credential(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(tmp_path, [_fake_beam_suite()])
    service = write_service(tmp_path)
    judge = tmp_path / "judge-access.json"
    judge.write_text(
        json.dumps(
            {
                "base_url": "https://vendor.example/v1",
                "model": "judge-model",
                "api_key_env": "TEST_VENDOR_API_KEY",
                "requests_per_minute": 5,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "shared-runtime-secret")

    result = run_doctor(
        "test-model",
        service_access_path=service,
        judge_access_path=judge,
        root=tmp_path,
    )

    assert result["status"] == "passed"
    beam = next(item for item in result["checks"] if item["name"] == "beam_judge_contract")
    assert beam["status"] == "passed"
    assert beam["details"]["credential_shared_with_endpoint"] is True
