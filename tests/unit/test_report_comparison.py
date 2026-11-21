from __future__ import annotations

import copy

from novita_self_test.case_catalog import load_cases, build_plan
from novita_self_test.report_comparison import scenario_identity, compare, failed_ids
from novita_self_test.contracts import validate_contract


def report(status, digest="a"*64, group=0, run="synthetic"):
    return {"run_dir": run, "scenario_sha256": {"case": digest},
            "results": [{"id": "case", "status": status, "repeat": 0, "group": group, "step": 0, "mode": "nonstream", "evidence": "case.json"}]}


def test_offline_compare_fixes_regressions_and_spec_changes_are_distinct():
    result = compare(report("failed"), report("passed", run="new-runtime"))
    validate_contract(result, "comparison.schema.json")
    assert result["endpoint_requests"] == 0
    assert result["changes"][0]["transition"] == "fixed"
    assert compare(report("passed"), report("failed"))["changes"][0]["transition"] == "regressed"
    assert compare(report("failed"), report("passed", digest="b"*64))["changes"][0]["transition"] == "not_comparable"
    assert compare(report("failed"), report("passed", group=1))["changes"][0]["transition"] == "not_comparable"


def test_scenario_fingerprint_ignores_runtime_identity_not_oracle_or_dialect():
    case = load_cases()[0]
    plan = build_plan([case], model="kimi-k3")
    plan["target"] = {"runtime_id": "old", "think_mode": "opensource"}
    first = scenario_identity(case, plan)
    plan["target"]["runtime_id"] = "fixed-runtime"
    assert scenario_identity(case, plan) == first
    plan["target"]["think_mode"] = "kimi"
    assert scenario_identity(case, plan) != first
    plan["target"]["think_mode"] = "opensource"
    changed = copy.deepcopy(case)
    changed["steps"][0]["expect"]["status"] = 400
    assert scenario_identity(changed, plan) != first


def test_failed_selection_is_whole_case_and_does_not_implicitly_retry_service_errors():
    value = {"results": [{"id": "roundtrip", "status": "passed"}, {"id": "roundtrip", "status": "failed"},
                         {"id": "other", "status": "service_invalid"}, {"id": "observed", "status": "observed"}]}
    assert failed_ids(value) == ["roundtrip"]
