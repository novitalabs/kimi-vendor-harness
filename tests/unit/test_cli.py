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
    assert "reasoning.precedence | 0 requests | unsupported: effort:low" in summary
    assert main(["plan", *options]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["cases"][0]["missing_requirements"] == ["effort:low"]
    assert plan["request_budget"] == 0


def test_plan_defaults_to_smoke_all_is_explicit_and_profile_is_validated(capsys):
    assert main(["plan"]) == 0
    smoke = json.loads(capsys.readouterr().out)
    assert len(smoke["cases"]) == 9
    assert smoke["request_budget"] == 13
    assert main(["plan", "--select", "all"]) == 0
    cases = json.loads(capsys.readouterr().out)["cases"]
    assert len(cases) == 58
    assert any("smoke" not in case["tags"] for case in cases)
    assert all(case["scope"] == "endpoint" for case in cases)
    assert main(["plan", "--model", "missing-profile"]) == 2
    assert "cannot read" in capsys.readouterr().err


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
    assert "service_access_invalid" in capsys.readouterr().err
    path = access_file(tmp_path)
    monkeypatch.delenv("CLI_TEST_KEY", raising=False)
    assert main(["doctor", "--service-access", str(path)]) == 2
    assert "credential_missing" in capsys.readouterr().err
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
    assert "model_mismatch" in capsys.readouterr().err
    assert main(["doctor", "--model", "kimi-k3-flash", "--select", "reasoning.precedence"]) == 2
    assert "selected_cases_unsupported" in json.loads(capsys.readouterr().out)["reasons"]


def test_run_requires_ack_before_access_or_traffic(capsys):
    assert main(["run", "--service-access", "/missing.json"]) == 2
    assert "execute_ack_required" in capsys.readouterr().err


def test_smoke_plan_is_endpoint_only_and_budget_is_explicit(capsys):
    assert main(["plan", "--select", "smoke"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(case["scope"] == "endpoint" for case in plan["cases"])
    assert plan["request_budget"] == sum(case["planned_requests"] for case in plan["cases"])
    assert plan["max_output_tokens_budget"] > 0
    assert plan["retries"] == 0
    assert main(["doctor", "--max-requests", "0"]) == 2
    assert "request_budget_exceeds_ceiling" in json.loads(capsys.readouterr().out)["reasons"]


@pytest.mark.parametrize("args", [
    ["list"], ["capabilities", "plan"],
    ["capabilities", "run", "--service-access", "/missing.json", "--execute"],
    ["parser", "run", "--adapter", "no-such-adapter", "--adapter-timeout", "1"],
])
def test_old_commands_and_options_are_rejected_without_traffic(args, capsys):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_single_console_entrypoint_and_no_legacy_parser_scripts():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["scripts"] == {"kimi-harness": "novita_self_test.cli:main"}
    for name in ("run-parser-conformance.py", "validate-parser-conformance.py", "parser_conformance_lib.py"):
        assert not (ROOT / "scripts" / name).exists()


def test_parser_validate_does_not_load_endpoint_catalog(monkeypatch, capsys):
    from novita_self_test import cli
    def forbidden(*args, **kwargs):
        pytest.fail("parser command loaded endpoint plan")
    monkeypatch.setattr(cli, "_plan", forbidden)
    assert main(["parser", "validate"]) == 0
    assert "7 parser conformance vectors valid" in capsys.readouterr().out
