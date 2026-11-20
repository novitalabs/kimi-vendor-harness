from __future__ import annotations

import json
from pathlib import Path

import pytest

from novita_self_test.case_catalog import build_plan, load_cases, load_profile, read_request, select_cases
from novita_self_test.config import ConfigError

ROOT = Path(__file__).resolve().parents[2]


def test_capability_cases_have_unique_ids_sources_and_knowledge() -> None:
    cases = load_cases(ROOT)
    ids = {case["id"] for case in cases}
    assert len(ids) == len(cases)
    assert {"tool_call.choice.none", "tool_call.choice.required"} <= ids
    assert "prompt_rendering.literal_markers" in ids
    assert all(case["scope"] == "endpoint" and case["request_budget"] > 0 for case in cases)
    assert "prompt_rendering.parser_vectors" not in ids


def test_selectors_are_capability_and_tag_oriented() -> None:
    cases = load_cases(ROOT)
    assert {case["id"] for case in select_cases(cases, ["tool_call/choice"])} >= {
        "tool_call.choice.none", "tool_call.choice.required"
    }
    assert all("smoke" in case["tags"] for case in select_cases(cases, ["smoke"]))
    with pytest.raises(ConfigError, match="matched no case"):
        select_cases(cases, ["does-not-exist"])


def test_plan_is_bounded_and_endpoint_only() -> None:
    cases = select_cases(load_cases(ROOT), ["smoke"])
    plan = build_plan(cases, model="kimi-k3", root=ROOT)
    assert plan["request_budget"] == sum(case["request_budget"] for case in cases)
    assert plan["retries"] == 0
    assert all(case["scope"] == "endpoint" and not case["missing_requirements"] for case in plan["cases"])
    assert plan["request_budget"] == 13
    assert len(plan["cases"]) == 9


def test_long_history_oracle_matches_fixture_metadata() -> None:
    cases = {case["id"]: case for case in load_cases(ROOT)}
    fixture = json.loads((ROOT / "capabilities/conversation_state/fixtures/case-a-invalid-arguments.jsonl").read_text().splitlines()[0])
    assert cases["conversation_state.arguments_recovery_a"]["steps"][0]["expect"]["forbidden_arguments"] == fixture["forbidden_arguments"][0]


def test_profile_capabilities_gate_cases() -> None:
    flash = build_plan(select_cases(load_cases(ROOT), ["reasoning.precedence"]), model="kimi-k3-flash", root=ROOT)
    assert flash["request_budget"] == 0
    assert "effort:low" in flash["cases"][0]["missing_requirements"]


def test_flash_keeps_all_top_level_capabilities_and_gates_only_low_effort() -> None:
    plan = build_plan(load_cases(ROOT), model="kimi-k3-flash", root=ROOT)
    assert not any(item.startswith("feature:") and "platform." not in item
                   for case in plan["cases"] for item in case["missing_requirements"])
    assert {case["id"] for case in plan["cases"] if "effort:low" in case["missing_requirements"]} == {
        "reasoning.precedence", "streaming.public_turn"
    }


def test_profile_has_one_authoritative_feature_and_effort_contract(tmp_path) -> None:
    import yaml
    (tmp_path / "profiles").mkdir()
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas/capability-profile.schema.json").write_text((ROOT / "schemas/capability-profile.schema.json").read_text())
    profile = load_profile("kimi-k3", ROOT)
    profile["capabilities"] = {"reasoning": {"efforts": ["medium"]}}
    (tmp_path / "profiles/kimi-k3.yaml").write_text(yaml.safe_dump(profile))
    with pytest.raises(ConfigError, match="Additional properties"):
        load_profile("kimi-k3", tmp_path)


def test_negative_probes_do_not_infer_success_requirements() -> None:
    case = next(case for case in load_cases(ROOT) if case["id"] == "structured_output.invalid_type")
    plan = build_plan([case], model="kimi-k3", root=ROOT)
    assert plan["cases"][0]["required_features"] == ["structured_output"]
    assert plan["request_budget"] == 1


def test_fixture_is_scanned_not_whitelisted_wholesale(tmp_path) -> None:
    relative = "capabilities/conversation_state/fixtures/case-a-invalid-arguments.jsonl"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    value = json.loads((ROOT / relative).read_text())
    path.write_text(json.dumps(value))
    assert read_request({"request_fixture": relative}, tmp_path)["messages"]
    value["messages"].append({"role": "user", "content": "Bearer synthetic-fixture-key-material"})
    path.write_text(json.dumps(value))
    with pytest.raises(ConfigError, match="credential material"):
        read_request({"request_fixture": relative}, tmp_path)
