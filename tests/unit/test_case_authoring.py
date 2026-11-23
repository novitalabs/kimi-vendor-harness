"""Author errors fail closed before any client call, including frozen-plan compile."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from novita_self_test.case_catalog import lint_cases, load_cases
from novita_self_test.contracts import validate_contract
from novita_self_test.capability_runtime import CapabilityClient
from novita_self_test.config import ConfigError, ServiceAccess
from novita_self_test.execution_plan import create_plan, verify_plan

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    for name in ("src", "schemas", "capabilities", "knowledge", "profiles", "sources"):
        shutil.copytree(ROOT / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    def forbidden(*args, **kwargs):
        pytest.fail("author validation contacted an endpoint")
    monkeypatch.setattr(CapabilityClient, "call", forbidden)
    return tmp_path


@pytest.mark.parametrize("fault", ["tools", "finish", "reasoning", "implicit_effort", "observation_role", "observation_determinism"])
def test_semantic_contradictions_block_frozen_plans(checkout, fault):
    path = checkout / "capabilities/tool_call/choice/cases.jsonl"
    cases = [json.loads(line) for line in path.read_text().split("\n") if line]
    case = next(row for row in cases if row["id"] == "tool_call.choice.required")
    step = case["steps"][0]
    if fault == "tools":
        step["expect"]["tools"] = "none"
    elif fault == "finish":
        step["expect"]["finish_reason"] = "stop"
    elif fault == "reasoning":
        step["expect"]["reasoning"] = True
    elif fault == "implicit_effort":
        step["thinking"] = True
    elif fault == "observation_role":
        case["role"] = "observation"
    else:
        case["determinism"] = "observation"
    path.write_text("".join(json.dumps(row) + "\n" for row in cases))
    access = ServiceAccess.from_dict({"model_family": "kimi-k3", "base_url": "http://127.0.0.1:1/v1", "served_model": "fake",
                                      "api_key_env": "AUTHOR_NO_KEY", "headers": {}, "think_mode": "opensource"})
    with pytest.raises(ConfigError) as exc:
        create_plan(model="kimi-k3", selectors=[case["id"]], service=access, root=checkout)
    assert exc.value.code == "case_invalid"


def test_focused_lint_is_local_but_full_plan_still_fails_closed(checkout):
    bad = checkout / "capabilities/error_handling/schema/cases.jsonl"
    bad.write_text(bad.read_text() + "{broken JSON\n")
    result = lint_cases(files=["capabilities/tool_call/choice/cases.jsonl"], ids=["tool_call.choice.required"], root=checkout)
    validate_contract(result, "lint.schema.json")
    assert result["status"] == "passed" and not result["scope"]["full_catalog"]
    assert result["cases"][0]["line"] == 2
    with pytest.raises(ConfigError) as exc:
        create_plan(model="kimi-k3", selectors=["tool_call.choice.required"], root=checkout)
    assert exc.value.path == ["capabilities/error_handling/schema/cases.jsonl", 4]
    full = lint_cases(root=checkout)
    assert full["status"] == "failed" and full["scope"]["full_catalog"]


def test_lint_collects_structured_paths_and_semantic_errors(checkout):
    path = checkout / "capabilities/tool_call/choice/cases.jsonl"
    rows = [json.loads(line) for line in path.read_text().split("\n") if line]
    rows[0]["steps"][0]["expect"]["tools"] = "required"
    rows[1]["max_output_tokens"] = 0
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    report = lint_cases(files=[path.relative_to(checkout).as_posix()], root=checkout)
    assert report["status"] == "failed"
    assert any(problem["path"][-4:] == ["steps", 0, "expect", "tools"] for problem in report["problems"])
    assert any(problem["path"][-1] == "max_output_tokens" for problem in report["problems"])


def test_schema_data_ref_is_allowed_but_real_external_ref_is_rejected(checkout):
    path = checkout / "capabilities/structured_output/cases.jsonl"
    rows = [json.loads(line) for line in path.read_text().split("\n") if line]
    schema = rows[1]["steps"][0]["expect"]["content_schema"]
    schema["examples"] = [{"$ref": "literal data"}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert lint_cases(root=checkout)["status"] == "passed"
    schema["properties"]["city"] = {"$ref": "https://invalid.test/schema"}
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = lint_cases(root=checkout)
    assert result["status"] == "failed"
    assert "external $ref" in result["problems"][0]["message"]


def test_explicit_unknown_effort_blocks_verified_target(checkout):
    import yaml
    path = checkout / "profiles/kimi-k3.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["efforts"]["max"] = {"expectation": "unknown", "evidence": []}
    path.write_text(yaml.safe_dump(profile))
    access = ServiceAccess.from_dict({"model_family": "kimi-k3", "base_url": "http://127.0.0.1:1/v1", "served_model": "fake",
                                      "api_key_env": "AUTHOR_NO_KEY", "headers": {}, "think_mode": "opensource"})
    plan = create_plan(model="kimi-k3", selectors=["reasoning.effort"], service=access, root=checkout)
    assert not plan["executable"] and plan["request_budget"] == 0
    assert "effort:max:unknown" in plan["cases"][0]["missing_requirements"]
    with pytest.raises(ConfigError) as exc:
        verify_plan(plan, access, root=checkout)
    assert exc.value.code == "contract_unknown"


@pytest.mark.parametrize("raw", ['[]', '{"id":[]}', '{"id":"broken"}', '{bad JSON'])
def test_lint_malformed_records_remain_structured_diagnostics(checkout, raw):
    path = checkout / "capabilities/protocol/cases.jsonl"
    path.write_text(raw + "\n")
    report = lint_cases(files=[path.relative_to(checkout).as_posix()], root=checkout)
    validate_contract(report, "lint.schema.json")
    assert report["status"] == "failed" and report["problems"][0]["path"][:2] == [path.relative_to(checkout).as_posix(), 1]


def test_raw_alias_conflict_is_not_reinterpreted_by_semantic_lint():
    from novita_self_test.case_semantics import validate_step
    validate_step({"expect": {"status": 200, "reasoning": True}},
                  {"messages": [{"role": "user", "content": "test"}], "reasoning_effort": "none", "thinking": {"type": "enabled", "effort": "max"}})


def test_template_has_one_valid_source_case_and_no_execution_metadata():
    template = json.loads((ROOT / "examples/case-template.json").read_text())
    validate_contract(template, "capability-case.schema.json")
    assert "file" not in template and template["steps"][0]["thinking"] is False


def test_committed_cases_remain_semantically_consistent():
    assert len(load_cases()) == 59
    assert lint_cases()["status"] == "passed"
