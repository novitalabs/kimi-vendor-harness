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


def test_unit_regression_is_not_hidden_by_a_fix_in_another_mode():
    baseline, candidate = report("failed"), report("passed")
    baseline["results"].append({**baseline["results"][0], "group": 1, "mode": "stream", "status": "passed"})
    candidate["results"].append({**candidate["results"][0], "group": 1, "mode": "stream", "status": "failed"})
    result = compare(baseline, candidate)
    validate_contract(result, "comparison.schema.json")
    assert result["status"] == "failed"
    assert result["changes"][0]["transition"] == "regressed"
    assert [unit["transition"] for unit in result["changes"][0]["units"]] == ["fixed", "regressed"]
    candidate["results"][1]["problems"] = [{"code": "new_failure"}]
    baseline["results"][1]["status"] = "failed"
    result = compare(baseline, candidate)
    assert result["changes"][0]["units"][1]["transition"] == "changed_evidence"


def test_duplicate_unit_keys_are_not_comparable():
    baseline, candidate = report("failed"), report("passed")
    baseline["results"] *= 2
    assert compare(baseline, candidate)["status"] == "incomplete"
    baseline = report("failed")
    del baseline["results"][0]["mode"]
    del candidate["results"][0]["mode"]
    assert compare(baseline, candidate)["status"] == "incomplete"


def test_explicit_candidate_scope_does_not_claim_untested_baseline_cases():
    baseline, candidate = report("failed"), report("passed")
    baseline["results"].append({**baseline["results"][0], "id": "other", "status": "passed"})
    baseline["scenario_sha256"]["other"] = "b"*64
    assert compare(baseline, candidate)["status"] == "incomplete"
    result = compare(baseline, candidate, scope="candidate")
    validate_contract(result, "comparison.schema.json")
    assert result["status"] == "passed"
    assert result["not_retested"] == ["other"]
    assert [row["id"] for row in result["changes"]] == ["case"]


def test_partial_fix_and_incomplete_execution_do_not_claim_full_case_fix():
    baseline, candidate = report("failed"), report("passed")
    for value in (baseline, candidate):
        value["results"].append({**value["results"][0], "group": 1, "mode": "stream", "status": "failed"})
    result = compare(baseline, candidate)
    assert result["changes"][0]["transition"] == "changed_evidence"
    assert result["changes"][0]["units"][0]["transition"] == "fixed"
    candidate["results"][1]["status"] = "inconclusive"
    assert compare(baseline, candidate)["status"] == "incomplete"


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
