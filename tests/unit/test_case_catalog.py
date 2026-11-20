from __future__ import annotations

import json
from pathlib import Path

import pytest

from novita_self_test.case_catalog import build_plan, load_cases, select_cases
from novita_self_test.config import ConfigError

ROOT = Path(__file__).resolve().parents[2]


def test_capability_cases_have_unique_ids_sources_and_knowledge() -> None:
    cases = load_cases(ROOT)
    ids = {case["id"] for case in cases}
    assert len(ids) == len(cases)
    assert {"tool_call.choice.none", "tool_call.choice.required"} <= ids
    assert "prompt_rendering.literal_markers" in ids
    assert all(case["request_budget"] >= 0 for case in cases)


def test_selectors_are_capability_and_tag_oriented() -> None:
    cases = load_cases(ROOT)
    assert {case["id"] for case in select_cases(cases, ["tool_call/choice"])} >= {
        "tool_call.choice.none", "tool_call.choice.required"
    }
    assert all("smoke" in case["tags"] for case in select_cases(cases, ["smoke"]))
    with pytest.raises(ConfigError, match="matched no case"):
        select_cases(cases, ["does-not-exist"])


def test_plan_is_bounded_and_marks_non_endpoint_cases() -> None:
    cases = select_cases(load_cases(ROOT), ["smoke"])
    plan = build_plan(cases, model="kimi-k3", root=ROOT)
    assert plan["request_budget"] == sum(case["request_budget"] for case in cases)
    assert plan["retries"] == 0
    assert all(case["planned_requests"] == 0 for case in plan["cases"] if case["scope"] != "endpoint")


def test_long_history_oracle_matches_fixture_metadata() -> None:
    cases = {case["id"]: case for case in load_cases(ROOT)}
    fixture = json.loads((ROOT / "capabilities/conversation_state/fixtures/case-a-invalid-arguments.jsonl").read_text().splitlines()[0])
    assert cases["conversation_state.arguments_recovery_a"]["steps"][0]["expect"]["forbidden_arguments"] == fixture["forbidden_arguments"][0]


def test_profile_capabilities_gate_cases() -> None:
    flash = build_plan(select_cases(load_cases(ROOT), ["reasoning.precedence"]), model="kimi-k3-flash", root=ROOT)
    assert flash["request_budget"] == 0
    assert "effort:low" in flash["cases"][0]["missing_requirements"]


def test_case_files_are_jsonl_and_secret_free() -> None:
    for path in (ROOT / "capabilities").rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            if line.strip():
                case = json.loads(line)
                if case.get("scope") == "endpoint":
                    request_text = json.dumps([step.get("request", {}) for step in case["steps"]], ensure_ascii=False)
                    assert "Bearer " not in request_text
