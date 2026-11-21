from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from novita_self_test.case_catalog import load_cases, load_profile, build_plan, request_features, select_cases
from novita_self_test.config import ConfigError, ServiceAccess
from novita_self_test.execution_plan import create_plan, verify_plan
from novita_self_test.target_contract import applicability, gate_summary

ROOT = Path(__file__).resolve().parents[2]


def test_k31_profile_is_explicitly_unknown_and_target_bound_execution_is_blocked():
    access = ServiceAccess.from_dict({"model_family": "kimi-k31-pro", "base_url": "http://127.0.0.1:1/v1", "served_model": "fake",
        "api_key_env": "NO_KEY", "headers": {}, "think_mode": "opensource"})
    plan = create_plan(model="kimi-k31-pro", service=access)
    assert plan["request_budget"] == 0
    assert not plan["executable"]
    assert all(any(value.endswith(":unknown") for value in case["missing_requirements"]) for case in plan["cases"])
    assert plan["contract_authority"]["checkpoint"] is None
    with pytest.raises(ConfigError) as exc:
        verify_plan(plan, access)
    assert exc.value.code == "contract_unknown"


def test_deleting_required_claim_does_not_make_requirement_optional_or_implemented():
    profile = load_profile("kimi-k3")
    del profile["features"]["tool_call"]
    missing, obligation = applicability(profile, {"tool_call"}, set())
    assert missing == ["feature:tool_call:unknown"]
    assert obligation == "unknown"


def test_gate_results_do_not_conflate_diagnostics_observations_and_required_coverage():
    case = next(case for case in load_cases() if case["id"] == "tool_call.choice.required")
    diagnostic = {**case, "id": "diagnostic", "role": "diagnostic"}
    observation = {**case, "id": "observation", "role": "observation"}
    rows = [{"id": case["id"], "status": "passed"}, {"id": case["id"], "status": "passed"},
            {"id": "diagnostic", "status": "failed"}, {"id": "observation", "status": "observed"}]
    summary = gate_summary([case, diagnostic, observation], rows, 1)
    assert summary["status"] == "passed"
    assert summary["required_cases"] == 1
    rows.pop(0)
    assert gate_summary([case], rows, 1)["status"] == "incomplete"


def test_role_and_stage_selection_are_explicit_and_text_parts_are_not_images():
    cases = load_cases()
    assert all(case["role"] == "gate" for case in select_cases(cases, ["role:gate"]))
    assert all(case["stage"] == "conversation" for case in select_cases(cases, ["stage:conversation"]))
    assert "multimodal" not in request_features({"messages": [{"content": [{"type": "text", "text": "a"}]}]})
    assert "multimodal" in request_features({"messages": [{"content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]}]})
