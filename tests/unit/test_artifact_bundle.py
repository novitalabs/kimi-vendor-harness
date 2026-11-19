from __future__ import annotations

import json
from pathlib import Path

import pytest

from novita_self_test.artifact_bundle import write_artifact_bundle
from novita_self_test.artifacts import SuiteResult
from novita_self_test.config import ConfigError


def _write_suite(
    run_dir: Path,
    execution_id: str,
    suite_id: str,
    row_id: str,
    *,
    external: Path,
    missing: Path,
    secret_name: Path,
    secret_content: Path,
) -> SuiteResult:
    suite_dir = run_dir / "suites" / execution_id
    suite_dir.mkdir(parents=True)
    (suite_dir / "suite-input.json").write_text(
        json.dumps(
            {
                "suite_id": suite_id,
                "row_ids": [row_id],
                "acceptance_mode": "full",
                "service": {"api_key_env": "TEST_VENDOR_API_KEY"},
            }
        ),
        encoding="utf-8",
    )
    external.mkdir(parents=True)
    (external / "worker.log").write_text("task completed\n", encoding="utf-8")
    secret_name.write_text("should not be copied\n", encoding="utf-8")
    secret_content.write_text(
        "Authorization: Bearer " + "sk_" + ("a" * 26) + "\n",
        encoding="utf-8",
    )
    result = SuiteResult(
        suite_id=suite_id,
        row_ids=[row_id],
        status="passed",
        counts={"total": 1, "passed": 1},
        metrics={"score": 1.0},
        coverage={"observed_cases": ["case-1"]},
        artifact_paths=[
            str(external),
            str(missing),
            str(secret_name),
            str(secret_content),
        ],
    )
    result.write(suite_dir / "suite-result.json")
    return result


def test_bundle_partitions_suites_and_indexes_all_artifacts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "suites").mkdir(parents=True)
    (run_dir / "run.json").write_text("{\"run_id\": \"run-1\"}\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("{\"event\": \"run_completed\"}\n", encoding="utf-8")

    external = tmp_path / "external-evidence"
    missing = tmp_path / "missing-evidence.json"
    secret_name = tmp_path / "api-key.txt"
    secret_content = tmp_path / "response.body"
    result = _write_suite(
        run_dir,
        "001-first",
        "first",
        "1.0",
        external=external,
        missing=missing,
        secret_name=secret_name,
        secret_content=secret_content,
    )
    report = {
        "generated_at": "2026-09-10T00:00:00+00:00",
        "verdict": "passed",
        "official_row_count": 20,
        "supplementary_row_count": 1,
    }
    bundle = write_artifact_bundle(
        run_dir,
        suite_results=[result],
        run_manifest={
            "run_id": "run-1",
            "model": "kimi-k3",
            "acceptance_mode": "full",
            "suite_states": [
                {
                    "suite_id": "first",
                    "status": "passed",
                    "validity": "valid",
                }
            ],
        },
        report=report,
    )

    assert bundle == run_dir / "test-artifacts"
    assert (bundle / "README.md").is_file()
    assert (bundle / "run.json").is_file()
    assert (bundle / "events.jsonl").read_text().strip().endswith("run_completed\"}")
    suite_bundle = bundle / "001-first"
    assert (suite_bundle / "suite-input.json").is_file()
    assert (suite_bundle / "suite-result.json").is_file()
    assert (suite_bundle / "suite-manifest.json").is_file()
    assert (suite_bundle / "artifacts" / "external" / "0001-external-evidence" / "worker.log").is_file()

    suite_manifest = json.loads((suite_bundle / "suite-manifest.json").read_text())
    assert suite_manifest["suite_id"] == "first"
    assert suite_manifest["counts"] == {"total": 1, "passed": 1}
    assert suite_manifest["metrics"] == {"score": 1.0}
    assert suite_manifest["missing_artifact_count"] == 1
    assert suite_manifest["skipped_secret_artifact_count"] == 2

    bundle_manifest = json.loads((bundle / "bundle.json").read_text())
    assert bundle_manifest["status"] == "partial"
    assert bundle_manifest["official_row_count"] == 20
    assert bundle_manifest["supplementary_row_count"] == 1
    assert bundle_manifest["summary"]["missing_artifact_count"] == 1
    paths = {item["path"] for item in bundle_manifest["files"]}
    assert "001-first/suite-manifest.json" in paths
    assert "001-first/artifacts/external/0001-external-evidence/worker.log" in paths
    assert not (bundle / "001-first" / "artifacts" / "external" / "0003-api-key.txt").exists()
    assert not (bundle / "001-first" / "artifacts" / "external" / "0004-response.body").exists()
    skipped = {
        item["skipped"]
        for item in bundle_manifest["files"]
        if not item.get("copied") and item.get("skipped")
    }
    assert "secret_bearing_filename" in skipped
    assert "secret_bearing_content" in skipped
    generated = [item for item in bundle_manifest["files"] if item["path"] == "README.md"]
    assert generated[0]["source"] == "generated://README.md"
    assert ".test-artifacts.staging-" not in json.dumps(bundle_manifest)


def test_bundle_excludes_bulky_machine_artifacts_but_keeps_json(tmp_path: Path) -> None:
    """A DeepSWE acceptance carries ~1.6 GB of `.eval`; only JSON travels.

    The delivery bundle is the reviewer/vendor handoff, so bulk machine formats
    stay on the execution host while their paths and sizes remain recorded for
    traceability.
    """
    run_dir = tmp_path / "run"
    suite_dir = run_dir / "suites" / "001-deepswe"
    suite_dir.mkdir(parents=True)
    (suite_dir / "suite-input.json").write_text(
        json.dumps({"suite_id": "deepswe", "row_ids": ["17"]}), encoding="utf-8"
    )

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "round-summary.json").write_text('{"score": 0.61}\n', encoding="utf-8")
    (evidence / "task-results.jsonl").write_text('{"task": "a"}\n', encoding="utf-8")
    (evidence / "result.eval").write_text("x" * 4096, encoding="utf-8")
    (evidence / "raw.gz").write_text("y" * 2048, encoding="utf-8")

    result = SuiteResult(
        suite_id="deepswe",
        row_ids=["17"],
        status="failed",
        counts={"total": 113, "passed": 68},
        metrics={"score": 0.6018},
        coverage={},
        artifact_paths=[str(evidence)],
    )
    result.write(suite_dir / "suite-result.json")

    bundle = write_artifact_bundle(
        run_dir,
        suite_results=[result],
        run_manifest={
            "run_id": "run-1",
            "model": "kimi-k3",
            "served_model": "moonshotai/kimi-k3-h",
            "started_at": "2026-09-11T16:06:08+00:00",
            "acceptance_mode": "develop",
            "suite_states": [
                {"suite_id": "deepswe", "status": "failed", "validity": "valid"}
            ],
        },
        report={"generated_at": "2026-09-11T22:47:02+00:00", "verdict": "failed"},
    )

    suite_bundle = bundle / "001-deepswe"
    # Readable, structured evidence is delivered.
    assert (suite_bundle / "artifacts" / "external" / "0001-evidence" / "round-summary.json").is_file()
    assert (suite_bundle / "artifacts" / "external" / "0001-evidence" / "task-results.jsonl").is_file()
    # Bulk machine formats stay on the execution host.
    assert not list(suite_bundle.rglob("*.eval"))
    assert not list(suite_bundle.rglob("*.gz"))

    suite_manifest = json.loads((suite_bundle / "suite-manifest.json").read_text())
    assert suite_manifest["excluded_artifact_count"] == 2
    assert suite_manifest["excluded_artifact_bytes"] == 4096 + 2048
    # Exclusions are policy, not breakage: the run still reports as complete.
    assert suite_manifest["missing_artifact_count"] == 0

    bundle_manifest = json.loads((bundle / "bundle.json").read_text())
    assert bundle_manifest["status"] == "complete"
    assert bundle_manifest["summary"]["excluded_artifact_count"] == 2
    excluded = [
        item
        for item in bundle_manifest["files"]
        if item.get("skipped") == "non_deliverable_artifact"
    ]
    assert len(excluded) == 2
    # The server-side original stays traceable even though it is not shipped.
    assert all(item["source"] for item in excluded)
    assert all(item["reason"] == "kept_on_execution_host" for item in excluded)


def test_bundle_records_served_model_and_start_time(tmp_path: Path) -> None:
    """One K3 catalogue serves both `kimi-k3-h` and `kimi-k3-flash`.

    Recording only the profile family let a `kimi-k3-flash` result be counted as
    a K3 result, so the bundle states the observed deployment and the run start
    time that orders results along the model x time axis.
    """
    run_dir = tmp_path / "run"
    suite_dir = run_dir / "suites" / "001-toolcall-arguments-recovery"
    suite_dir.mkdir(parents=True)
    (suite_dir / "suite-input.json").write_text(
        json.dumps(
            {
                "suite_id": "toolcall-arguments-recovery",
                "row_ids": ["18"],
                "service": {"served_model": "kimi-k3-flash"},
            }
        ),
        encoding="utf-8",
    )
    result = SuiteResult(
        suite_id="toolcall-arguments-recovery",
        row_ids=["18"],
        status="failed",
        counts={"total": 1, "passed": 0},
        metrics={},
        coverage={},
        artifact_paths=[],
    )
    result.write(suite_dir / "suite-result.json")

    bundle = write_artifact_bundle(
        run_dir,
        suite_results=[result],
        run_manifest={
            "run_id": "run-1",
            "model": "kimi-k3",
            "served_model": "kimi-k3-flash",
            "started_at": "2026-09-12T12:54:26+00:00",
            "acceptance_mode": "develop",
            "suite_states": [
                {
                    "suite_id": "toolcall-arguments-recovery",
                    "status": "failed",
                    "validity": "execution_invalid",
                }
            ],
        },
        report={"generated_at": "2026-09-12T12:56:00+00:00", "verdict": "failed"},
    )

    bundle_manifest = json.loads((bundle / "bundle.json").read_text())
    assert bundle_manifest["model"] == "kimi-k3"
    assert bundle_manifest["served_model"]["served"] == "kimi-k3-flash"
    assert bundle_manifest["served_model"]["family"] == "kimi-k3"
    assert bundle_manifest["started_at"] == "2026-09-12T12:54:26+00:00"


def test_bundle_flags_mixed_served_models(tmp_path: Path) -> None:
    """A run that served more than one deployment is not a single comparison."""
    run_dir = tmp_path / "run"
    results = []
    for index, model in enumerate(("moonshotai/kimi-k3-h", "kimi-k3-flash"), start=1):
        execution_id = f"{index:03d}-suite{index}"
        suite_dir = run_dir / "suites" / execution_id
        suite_dir.mkdir(parents=True)
        (suite_dir / "suite-input.json").write_text(
            json.dumps({"suite_id": f"suite{index}", "service": {"served_model": model}}),
            encoding="utf-8",
        )
        result = SuiteResult(
            suite_id=f"suite{index}",
            row_ids=[str(index)],
            status="passed",
            counts={"total": 1, "passed": 1},
            metrics={"score": 1.0},
            coverage={},
            artifact_paths=[],
        )
        result.write(suite_dir / "suite-result.json")
        results.append(result)

    bundle = write_artifact_bundle(
        run_dir,
        suite_results=results,
        run_manifest={"run_id": "run-1", "model": "kimi-k3", "suite_states": []},
        report={},
    )
    served = json.loads((bundle / "bundle.json").read_text())["served_model"]
    assert served["mixed"] is True
    assert served["distinct_observed"] == ["kimi-k3-flash", "moonshotai/kimi-k3-h"]
    assert served["served"] is None


def test_bundle_replaces_previous_delivery_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "suites").mkdir(parents=True)
    (run_dir / "test-artifacts").mkdir()
    (run_dir / "test-artifacts" / "stale.txt").write_text("stale", encoding="utf-8")
    result = SuiteResult(suite_id="empty", row_ids=["R1"], status="passed")
    suite_dir = run_dir / "suites" / "001-empty"
    suite_dir.mkdir()
    result.write(suite_dir / "suite-result.json")

    write_artifact_bundle(
        run_dir,
        suite_results=[result],
        run_manifest={"run_id": "run-2", "model": "test-model", "suite_states": []},
        report={"verdict": "passed", "official_row_count": 1},
    )

    assert not (run_dir / "test-artifacts" / "stale.txt").exists()
    assert (run_dir / "test-artifacts" / "bundle.json").is_file()


REPO_PINS = {
    "novita_self_test": {
        "path": "/opt/workspace/novita-self-test",
        "commit": "abc123" + "0" * 34,
        "branch": "main",
        "tracked_dirty": False,
        "untracked_count": 0,
    },
    "kvv_k3": {
        "path": "/opt/workspace/kvv-k3",
        "commit": "3dad65a" + "1" * 33,
        "branch": "main",
        "tracked_dirty": False,
        "untracked_count": 0,
    },
}


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "suites").mkdir(parents=True)
    (run_dir / "run.json").write_text("{\"run_id\": \"run-deps\"}\n", encoding="utf-8")
    return run_dir


def _write_suite_input(suite_dir: Path, payload: dict) -> None:
    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / "suite-input.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _bundle_kwargs(result: SuiteResult) -> dict:
    return {
        "suite_results": [result],
        "run_manifest": {
            "run_id": "run-deps",
            "model": "kimi-k3",
            "acceptance_mode": "full",
            "concurrency_override": None,
            "repo_pins": REPO_PINS,
            "suite_states": [
                {"suite_id": result.suite_id, "status": result.status, "validity": result.validity}
            ],
        },
        "report": {
            "generated_at": "2026-09-10T00:00:00+00:00",
            "verdict": "passed",
            "official_row_count": 20,
            "supplementary_row_count": 1,
        },
    }


def _write_deepswe_suite(run_dir: Path) -> SuiteResult:
    execution_id = "001-deepswe"
    suite_dir = run_dir / "suites" / execution_id
    suite_dir.mkdir(parents=True)
    commit = "435ee89ec2f2e2289f33b0da4f992f0b7b7266b9"
    _write_suite_input(
        suite_dir,
        {
            "schema_version": 1,
            "suite_id": "deepswe",
            "row_ids": ["17"],
            "adapter": "deepswe",
            "selector": None,
            "selector_args": [],
            "acceptance_mode": "full",
            "concurrency_override": None,
            "deepswe_rounds": 3,
            "service": {"served_model": "moonshotai/Kimi-K3", "think_mode": "thinking"},
            "config": {
                "concurrency": 113,
                "rounds": 3,
                "expected_pier_version": "0.3.1",
                "expected_deep_swe_commit": commit,
                "kimi_code_version": "0.29.0",
                "rerun_policy": {"mode": "any_pass", "max_reruns": 3, "scheduling": "batch_after_formal"},
                "runner_fingerprint": {"corpus": "deep-swe-113"},
            },
        },
    )
    preflight = suite_dir / "local-preflight.json"
    preflight.write_text(
        json.dumps(
            {
                "status": "passed",
                "expected": {
                    "deep_swe_commit": commit,
                    "pier_version": "0.3.1",
                    "tasks": 113,
                    "tracked_worktree_clean": True,
                },
                "observed": {
                    "deep_swe_commit": commit,
                    "pier_version": "0.3.1",
                    "tasks": 113,
                    "tracked_worktree_clean": True,
                },
            }
        )
        + "\n"
    )
    result = SuiteResult(
        suite_id="deepswe",
        row_ids=["17"],
        status="passed",
        counts={"tasks": 113, "rounds": 3},
        metrics={"score": 0.7},
        artifact_paths=[str(preflight.resolve())],
        runner_fingerprint={
            "pier": "0.3.1",
            "deep_swe_commit": commit,
            "kimi_code": "0.29.0",
            "corpus": "deep-swe-113",
        },
    )
    result.write(suite_dir / "suite-result.json")
    return result


def test_deepswe_manifest_exposes_preflight_observations(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    result = _write_deepswe_suite(run_dir)

    bundle = write_artifact_bundle(run_dir, **_bundle_kwargs(result))

    manifest = json.loads(
        (bundle / "001-deepswe" / "suite-manifest.json").read_text()
    )
    dependencies = manifest["dependencies"]

    pier = dependencies["pier"]
    assert pier["status"] == "observed"
    assert pier["expected"] == {"version": "0.3.1"}
    assert pier["observed"] == {"version": "0.3.1"}
    assert pier["match"] is True
    assert pier["evidence"] == "local-preflight.json"

    deep_swe = dependencies["deep_swe"]
    assert deep_swe["observed"]["commit"] == "435ee89ec2f2e2289f33b0da4f992f0b7b7266b9"
    assert deep_swe["observed"]["tracked_worktree_clean"] is True
    assert deep_swe["observed"]["task_count"] == 113
    assert deep_swe["match"] is True

    kimi_code = dependencies["kimi_code"]
    assert kimi_code["expected"] == {"version": "0.29.0"}
    assert kimi_code["observed"] is None
    assert kimi_code["status"] == "expected_only"

    parameters = manifest["parameters"]
    assert parameters["rounds"] == 3
    assert parameters["concurrency"]["effective"] == 113
    assert parameters["dataset"] == {"name": "deep-swe-113"}
    assert parameters["retry_policy"] == {
        "mode": "any_pass",
        "max_reruns": 3,
        "scheduling": "batch_after_formal",
    }
    assert (bundle / "001-deepswe" / "artifacts" / "local-preflight.json").is_file()


def test_generic_suite_reports_repository_commits_and_runner(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    execution_id = "001-kvv-params"
    suite_dir = run_dir / "suites" / execution_id
    suite_dir.mkdir(parents=True)
    _write_suite_input(
        suite_dir,
        {
            "schema_version": 1,
            "suite_id": "kvv-params",
            "row_ids": ["3"],
            "adapter": "kvv",
            "selector": "3",
            "selector_args": ["tests/params"],
            "acceptance_mode": "full",
            "config": {
                "repeat": 10,
                "rerun_policy": {"mode": "any_pass", "max_reruns": 3, "scheduling": "batch_after_formal"},
                "runner_fingerprint": {
                    "repository": "MoonshotAI/kvv-k3",
                    "upstream": "3dad65a",
                    "concurrency": "18",
                },
            },
        },
    )
    result = SuiteResult(
        suite_id="kvv-params",
        row_ids=["3"],
        status="passed",
        counts={"total": 18, "passed": 18},
        redacted_command=["pytest", "tests/params", "-n", "18"],
    )
    result.write(suite_dir / "suite-result.json")

    bundle = write_artifact_bundle(run_dir, **_bundle_kwargs(result))

    manifest = json.loads(
        (bundle / "001-kvv-params" / "suite-manifest.json").read_text()
    )
    dependencies = manifest["dependencies"]
    kvv = dependencies["kvv_k3"]
    assert kvv["expected"] == {"commit": "3dad65a"}
    assert kvv["observed"]["commit"] == REPO_PINS["kvv_k3"]["commit"]
    # The short upstream pin matches the observed full commit by prefix.
    assert kvv["match"] is True
    assert dependencies["novita_self_test"]["status"] == "observed"

    parameters = manifest["parameters"]
    assert parameters["rounds"] == 10
    assert parameters["concurrency"]["effective"] == "18"
    assert parameters["redacted_command"] == ["pytest", "tests/params", "-n", "18"]
