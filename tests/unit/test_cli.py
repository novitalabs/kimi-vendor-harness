from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest

from novita_self_test.cli import main
from novita_self_test.capability_runtime import CapabilityClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_endpoint_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("zero-traffic command accessed endpoint")
    monkeypatch.setattr(CapabilityClient, "call", forbidden)


def test_summary_and_json_share_model_applicability(capsys):
    options = ["--model", "kimi-k3-flash", "--select", "reasoning.precedence"]
    assert main(["plan", *options, "--format", "summary"]) == 0
    summary = capsys.readouterr().out
    assert "reasoning.precedence | 0 requests | blocked: effort:low" in summary
    assert main(["plan", *options]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["cases"][0]["missing_requirements"] == ["effort:low:unsupported"]
    assert plan["request_budget"] == 0


def test_plan_defaults_to_smoke_all_is_explicit_and_profile_is_validated(capsys):
    assert main(["plan"]) == 0
    smoke = json.loads(capsys.readouterr().out)
    assert len(smoke["cases"]) == 9
    assert smoke["request_budget"] == 13
    assert main(["plan", "--select", "all"]) == 0
    cases = json.loads(capsys.readouterr().out)["cases"]
    assert len(cases) == 59
    assert any("smoke" not in case["tags"] for case in cases)
    assert all(case["scope"] == "endpoint" for case in cases)
    assert main(["plan", "--model", "missing-profile"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "configuration_invalid"


@pytest.mark.parametrize("format", ["summary", "json"])
def test_plan_out_matches_stdout_and_repeat_budget(tmp_path, capsys, format):
    out = tmp_path / "plan.txt"
    assert main(["plan", "--repeat", "2", "--format", format, "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert text == out.read_text()
    if format == "json":
        assert json.loads(text)["request_budget"] == 26
    else:
        assert "requests: 26" in text
        assert "No endpoint traffic generated" in text


def test_catalog_refinement_suggestions_and_lint(capsys):
    assert main(["plan", "--select", "role:gate", "--filter", "component:tool_parser", "--format", "catalog"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "case_catalog"
    assert len(result["cases"]) == 7 and result["request_budget"] == 14
    assert all(case["file"] and case["line"] > 0 and "steps" not in case for case in result["cases"])
    assert main(["plan", "--select", "tool_call/choice", "--exclude", "tag:negative", "--format", "catalog"]) == 0
    assert not any("negative" in row["tags"] for row in json.loads(capsys.readouterr().out)["cases"])
    assert main(["plan", "--select", "component:tool_parsr"]) == 2
    problem = json.loads(capsys.readouterr().out)["error"]
    assert "component:tool_parser" in problem["expected"]["suggestions"]
    assert main(["lint", "--file", "capabilities/tool_call/choice/cases.jsonl", "--id", "tool_call.choice.required"]) == 0
    lint = json.loads(capsys.readouterr().out)
    assert lint["kind"] == "case_lint" and lint["cases"][0]["line"] == 2
    assert lint["endpoint_requests"] == 0


def access_file(tmp_path, **overrides):
    data = {"model_family": "kimi-k3", "base_url": "http://127.0.0.1:1/v1",
            "served_model": "some-custom-deployment", "api_key_env": "CLI_TEST_KEY",
            "headers": {}, "think_mode": "opensource"}
    data.update(overrides)
    path = tmp_path / "access.json"
    path.write_text(json.dumps(data))
    return path


def test_doctor_validates_access_file_and_key_without_probe(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing.json"
    assert main(["doctor", "--service-access", str(missing)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "service_access_invalid"
    path = access_file(tmp_path)
    monkeypatch.delenv("CLI_TEST_KEY", raising=False)
    assert main(["doctor", "--service-access", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "credential_missing"
    monkeypatch.setenv("CLI_TEST_KEY", "synthetic-cli-key")
    assert main(["doctor", "--service-access", str(path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["access_checked"]
    assert not output["endpoint_probe"]
    assert "synthetic-cli-key" not in json.dumps(output)


def test_doctor_blocks_access_model_mismatch_and_unsupported_selection(tmp_path, monkeypatch, capsys):
    path = access_file(tmp_path, model_family="kimi-k3-flash")
    monkeypatch.setenv("CLI_TEST_KEY", "test")
    assert main(["doctor", "--service-access", str(path)]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "model_mismatch"
    assert main(["doctor", "--model", "kimi-k3-flash", "--select", "reasoning.precedence"]) == 2
    assert "selected_cases_unsupported" in {p["code"] for p in json.loads(capsys.readouterr().out)["problems"]}


@pytest.mark.parametrize("field,value", [("think_mode", "unsupported"), ("api_key_env", None), ("headers", None)])
def test_access_schema_rejects_invalid_or_missing_required_fields(tmp_path, capsys, field, value):
    path = access_file(tmp_path)
    data = json.loads(path.read_text())
    if value is None:
        data.pop(field)
    else:
        data[field] = value
    path.write_text(json.dumps(data))
    assert main(["doctor", "--service-access", str(path)]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error"]["code"] == "service_access_invalid"
    assert output["endpoint_requests"] == 0


def test_run_requires_ack_before_access_or_traffic(capsys):
    assert main(["run", "--service-access", "/missing.json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "execute_ack_required"


def test_smoke_plan_is_endpoint_only_and_budget_is_explicit(capsys):
    assert main(["plan", "--select", "smoke"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(case["scope"] == "endpoint" for case in plan["cases"])
    assert plan["request_budget"] == sum(case["planned_requests"] for case in plan["cases"])
    assert plan["max_output_tokens_budget"] > 0
    assert plan["retries"] == 0
    assert main(["doctor", "--max-requests", "0"]) == 2
    assert "request_budget_exceeds_ceiling" in {p["code"] for p in json.loads(capsys.readouterr().out)["problems"]}


@pytest.mark.parametrize("args", [
    ["list"], ["capabilities", "plan"],
    ["capabilities", "run", "--service-access", "/missing.json", "--execute"],
    ["parser", "run", "--adapter", "no-such-adapter", "--adapter-timeout", "1"],
])
def test_old_commands_and_options_are_rejected_without_traffic(args, capsys):
    assert main(args) == 2
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["error"]["code"] == "arguments_invalid"


def test_single_console_entrypoint_and_no_legacy_parser_scripts():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["scripts"] == {"kimi-harness": "novita_self_test.cli:main"}
    for name in ("run-parser-conformance.py", "validate-parser-conformance.py", "parser_conformance_lib.py"):
        assert not (ROOT / "scripts" / name).exists()


def test_parser_validate_does_not_load_endpoint_catalog(monkeypatch, capsys):
    from novita_self_test import cli
    def forbidden(*args, **kwargs):
        pytest.fail("parser command loaded endpoint plan")
    monkeypatch.setattr(cli, "create_plan", forbidden)
    assert main(["parser", "validate"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["vector_count"] == 7
    assert output["trial_count"] == 21
    assert output["kind"] == "parser_validation"
