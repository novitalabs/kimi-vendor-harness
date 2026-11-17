from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from novita_self_test.artifacts import SuiteResult, enforce_completeness
from novita_self_test.config import ModelProfile, SuiteDefinition
from novita_self_test.orchestrator import _effective_completeness


@pytest.fixture
def suite() -> SuiteDefinition:
    profile = ModelProfile.load("kimi-k3-flash")
    return next(item for item in profile.suites if item.suite_id == "deepswe")


def complete_result(rounds: int) -> SuiteResult:
    return SuiteResult(
        suite_id="deepswe",
        row_ids=["10"],
        status="passed",
        validity="valid",
        coverage={
            "observed_tasks": 113,
            "observed_rounds": rounds,
            "observed_max_reruns": 1,
        },
        artifact_paths=[
            "local-preflight.json",
            "deepswe-checkpoint.json",
            *(f"round-{index:02d}-summary.json" for index in range(1, rounds + 1)),
            "round-summary.json",
            "task-results.jsonl",
            "adapter-result.json",
        ],
    )


def test_single_round_complete_artifacts_are_valid(suite: SuiteDefinition) -> None:
    contract = _effective_completeness(suite, "10", 1, "develop")
    result = enforce_completeness(complete_result(1), contract)

    assert contract["expected_rounds"] == 1
    assert result.status == "passed"
    assert result.validity == "valid"


def test_single_round_still_requires_its_summary(suite: SuiteDefinition) -> None:
    contract = _effective_completeness(suite, "10", 1, "develop")
    result = complete_result(1)
    result.artifact_paths.remove("round-01-summary.json")

    enforce_completeness(result, contract)

    assert result.validity == "execution_invalid"
    assert result.reason == "artifact_contract_incomplete"
    assert "round-01-summary.json" in result.failure_summary


@pytest.mark.parametrize("missing_round", [1, 2, 3])
def test_three_rounds_still_require_every_summary(
    suite: SuiteDefinition, missing_round: int
) -> None:
    contract = _effective_completeness(suite, "10", 3, "full")
    result = complete_result(3)
    missing = f"round-{missing_round:02d}-summary.json"
    result.artifact_paths.remove(missing)

    enforce_completeness(result, contract)

    assert result.validity == "execution_invalid"
    assert result.reason == "artifact_contract_incomplete"
    assert missing in result.failure_summary


@pytest.mark.parametrize("rounds", [None, 3])
def test_default_three_round_contract_is_unchanged(
    suite: SuiteDefinition, rounds: int | None
) -> None:
    contract = _effective_completeness(suite, "10", rounds, "full")

    assert contract == suite.completeness
    result = enforce_completeness(complete_result(3), contract)
    assert result.status == "passed"
    assert result.validity == "valid"


def test_more_rounds_require_all_requested_summaries(suite: SuiteDefinition) -> None:
    contract = _effective_completeness(suite, "10", 4, "develop")
    result = enforce_completeness(complete_result(4), contract)

    assert "round-04-summary.json" in contract["required_artifacts"]
    assert result.status == "passed"
    result.artifact_paths.remove("round-04-summary.json")
    enforce_completeness(result, contract)
    assert result.validity == "execution_invalid"


def test_non_round_requirements_and_profile_are_preserved(suite: SuiteDefinition) -> None:
    extra = ["round-summary.json.sha256", "round-extra-summary.json", "prefix-round-02-summary.json"]
    suite = replace(suite, completeness={
        **suite.completeness,
        "required_artifacts": [*suite.completeness["required_artifacts"], *extra],
    })
    original = deepcopy(suite.completeness)
    contract = _effective_completeness(suite, "10", 1, "develop")

    assert suite.completeness == original
    assert contract["required_artifacts"] is not suite.completeness["required_artifacts"]
    assert contract["required_artifacts"] == [
        name for name in original["required_artifacts"]
        if name not in {"round-02-summary.json", "round-03-summary.json"}
    ]
    result = complete_result(1)
    result.artifact_paths.extend(extra)
    result.artifact_paths.remove("local-preflight.json")
    enforce_completeness(result, contract)
    assert result.validity == "execution_invalid"
    assert "local-preflight.json" in result.failure_summary


def test_other_suite_contract_is_unchanged(suite: SuiteDefinition) -> None:
    other = replace(suite, adapter="fake")
    original = deepcopy(other.completeness)

    contract = _effective_completeness(other, "10", 1, "develop")

    assert contract == original
    assert other.completeness == original
