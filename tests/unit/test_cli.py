from __future__ import annotations

import json
from pathlib import Path

import pytest

from novita_self_test.cli import main
from novita_self_test.capability_runtime import CapabilityClient


@pytest.fixture(autouse=True)
def no_endpoint_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("zero-traffic command accessed endpoint")
    monkeypatch.setattr(CapabilityClient, "call", forbidden)


def test_list_and_plan_share_model_applicability(capsys):
    assert main(["list", "--model", "kimi-k3-flash", "--select", "reasoning.precedence"]) == 0
    listing = json.loads(capsys.readouterr().out)
    case = listing["cases"][0]
    assert case["missing_requirements"] == ["effort:low"]
    assert case["planned_requests"] == 0
    assert main(["capabilities", "plan", "--model", "kimi-k3-flash", "--select", "reasoning.precedence"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["cases"][0]["missing_requirements"] == case["missing_requirements"]
    assert plan["request_budget"] == 0


def test_list_defaults_to_all_and_validates_profile(capsys):
    assert main(["list"]) == 0
    cases = json.loads(capsys.readouterr().out)["cases"]
    assert any("smoke" not in case["tags"] for case in cases)
    assert all(case["scope"] == "endpoint" for case in cases)
    assert main(["list", "--model", "missing-profile"]) == 2
    assert "cannot read" in capsys.readouterr().err


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
    assert main(["capabilities", "run", "--service-access", "/missing.json"]) == 2
    assert "execute_ack_required" in capsys.readouterr().err


def test_smoke_plan_is_endpoint_only_and_budget_is_explicit(capsys):
    assert main(["capabilities", "plan", "--select", "smoke"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(case["scope"] == "endpoint" for case in plan["cases"])
    assert plan["request_budget"] == sum(case["planned_requests"] for case in plan["cases"])
    assert plan["max_output_tokens_budget"] > 0
    assert plan["retries"] == 0
    assert main(["doctor", "--max-requests", "0"]) == 2
    assert "request_budget_exceeds_ceiling" in json.loads(capsys.readouterr().out)["reasons"]
