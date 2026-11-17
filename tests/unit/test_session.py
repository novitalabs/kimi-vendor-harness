from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from novita_self_test.config import ConfigError, ModelProfile
from novita_self_test.orchestrator import Orchestrator
from novita_self_test.session import continue_session, create_session

from .helpers import write_model, write_service


def suite(suite_id: str, row_id: str) -> dict:
    return {
        "suite_id": suite_id,
        "adapter": "fake",
        "row_ids": [row_id],
        "mandatory": True,
        "selectors": {row_id: [f"case::{row_id}"]},
        "config": {"fake_result": {"status": "passed", "validity": "valid"}},
        "completeness": {},
    }


def contract_hash(profile_hashes: dict[str, str]) -> str:
    payload = json.dumps(profile_hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def run_row(root: Path, service: Path, row_id: str) -> Path:
    return Orchestrator(root=root).execute(
        mode="develop",
        model_id="test-model",
        service_access_path=service,
        selectors=[row_id],
    ).run_dir


def test_session_merges_hash_compatible_suite_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    write_model(tmp_path, [suite("first", "R1"), suite("second", "R2")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    first = run_row(tmp_path, service, "R1")
    second = run_row(tmp_path, service, "R2")

    report = create_session(
        "test-model",
        service_access_path=service,
        runs=[first, second],
        root=tmp_path,
    )

    assert report["verdict"] == "passed"
    assert report["missing_suites"] == []
    assert report["source_runs"] == [first.name, second.name]
    selected = {
        suite_id: [attempt["run_id"] for attempt in attempts if attempt["selected"]]
        for suite_id, attempts in report["suite_attempts"].items()
    }
    assert selected == {"first": [first.name], "second": [second.name]}
    session_dir = Path(report["session_dir"])
    assert (session_dir / "acceptance-session.json").is_file()
    assert (session_dir / "acceptance-session.md").is_file()


def test_session_does_not_require_optional_beam_judge_when_beam_unselected(
    tmp_path: Path, monkeypatch
) -> None:
    selected = suite("required", "R1")
    optional_beam = suite("beam-1m", "16")
    optional_beam["adapter"] = "beam"
    optional_beam["mandatory"] = False
    write_model(tmp_path, [selected, optional_beam])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    run = run_row(tmp_path, service, "R1")

    report = create_session(
        "test-model",
        service_access_path=service,
        runs=[run],
        root=tmp_path,
    )

    assert report["verdict"] == "passed"
    assert report["missing_suites"] == ["beam-1m"]


def test_session_requires_approval_and_new_attempt_for_changed_suite(
    tmp_path: Path, monkeypatch
) -> None:
    model_dir = write_model(tmp_path, [suite("first", "R1"), suite("second", "R2")])
    service = write_service(tmp_path)
    monkeypatch.setenv("TEST_VENDOR_API_KEY", "runtime-only-secret")
    first = run_row(tmp_path, service, "R1")
    second = run_row(tmp_path, service, "R2")
    old_manifest = json.loads((first / "run.json").read_text())
    old_contract = contract_hash(old_manifest["profile_hashes"])

    manifest_path = model_dir / "suite-manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["suites"][1]["config"]["contract_revision"] = 2
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    profile = ModelProfile.load("test-model", root=tmp_path)
    new_contract = contract_hash(profile.content_hashes())

    with pytest.raises(ConfigError, match="approval must cover suites"):
        create_session(
            "test-model",
            service_access_path=service,
            runs=[first, second],
            root=tmp_path,
        )

    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "from_contract_hash": old_contract,
                "to_contract_hash": new_contract,
                "affected_suites": ["second"],
                "approved_by": "test-operator",
                "reason": "approved test contract migration",
            }
        ),
        encoding="utf-8",
    )
    report = create_session(
        "test-model",
        service_access_path=service,
        runs=[first, second],
        approval_paths=[approval],
        root=tmp_path,
    )
    assert report["missing_suites"] == ["second"]
    assert report["verdict"] == "inconclusive"

    replacement = run_row(tmp_path, service, "R2")
    continued = continue_session(
        Path(report["session_dir"]),
        runs=[replacement],
        root=tmp_path,
    )
    assert continued["missing_suites"] == []
    assert continued["verdict"] == "passed"
    assert any(
        attempt["run_id"] == replacement.name and attempt["selected"]
        for attempt in continued["suite_attempts"]["second"]
    )
