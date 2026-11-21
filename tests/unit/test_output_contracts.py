from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from novita_self_test.cli import main
from novita_self_test.contracts import error_result, issue, validate_contract
from novita_self_test.config import ConfigError

ROOT = Path(__file__).resolve().parents[2]


def test_all_output_schema_definitions_are_valid():
    for path in (ROOT / "schemas").glob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text()))


@pytest.mark.parametrize("args,schema,status", [
    (["plan"], "plan.schema.json", 0),
    (["plan", "--repeat", "-1"], "error.schema.json", 2),
    (["plan", "--max-requests", "-1"], "error.schema.json", 2),
    (["plan", "--timeout", "nan"], "error.schema.json", 2),
    (["doctor"], "doctor.schema.json", 0),
    (["doctor", "--max-requests", "0"], "doctor.schema.json", 2),
    (["parser", "validate"], "parser-report.schema.json", 0),
    (["renderer", "validate", "--vectors", "/missing.jsonl"], "renderer-report.schema.json", 2),
    (["compare", "--baseline", "/missing.json", "--candidate", "/missing.json"], "error.schema.json", 2),
    (["parser", "validate", "--vectors", "/missing.jsonl"], "parser-report.schema.json", 2),
    (["list"], "error.schema.json", 2),
    (["run"], "error.schema.json", 2),
    (["run", "--service-access", "/missing.json"], "error.schema.json", 2),
])
def test_cli_always_emits_one_schema_valid_json_document(args, schema, status, capsys):
    assert main(args) == status
    captured = capsys.readouterr()
    assert captured.err == ""
    value = json.loads(captured.out)
    validate_contract(value, schema)


def test_contract_validation_rejects_missing_and_unknown_fields():
    value = error_result("plan", "test_error", "a message")
    validate_contract(value, "error.schema.json")
    value["extra"] = True
    with pytest.raises(ConfigError, match="error.schema.json"):
        validate_contract(value, "error.schema.json")
    value = issue("tool_name_undeclared", "irrelevant wording", expected=["known"], actual="unknown")
    validate_contract(value, "problem.schema.json")
    del value["code"]
    with pytest.raises(ConfigError):
        validate_contract(value, "problem.schema.json")


def test_schema_oracles_cannot_fetch_external_resources(monkeypatch):
    from novita_self_test.contracts import oracle_validator
    import urllib.request
    def forbidden(*args, **kwargs):
        pytest.fail("schema validator attempted network I/O")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    with pytest.raises(Exception) as exc:
        list(oracle_validator({"$ref": "https://invalid.test/schema.json"}).iter_errors({}))
    assert "Unresolvable" in str(exc.value)


def test_broken_error_schema_still_produces_machine_error(monkeypatch, capsys):
    from novita_self_test import cli
    def broken(*args, **kwargs):
        raise OSError("missing schema")
    monkeypatch.setattr(cli, "validate_contract", broken)
    assert cli.main(["list"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["error"]["code"] == "output_contract_invalid"
    assert value["endpoint_requests"] == 0


def test_post_start_io_error_does_not_claim_zero_requests(monkeypatch, tmp_path, capsys):
    from novita_self_test import cli
    path = tmp_path / "access.json"
    path.write_text(json.dumps({"model_family": "kimi-k3", "base_url": "http://127.0.0.1:1/v1", "served_model": "fake",
        "api_key_env": "CONTRACT_TEST_KEY", "headers": {}, "think_mode": "opensource"}))
    def broken(*args, **kwargs):
        raise OSError("synthetic artifact failure")
    monkeypatch.setattr(cli, "run_plan", broken)
    assert cli.main(["run", "--service-access", str(path), "--execute"]) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["endpoint_requests"] is None
    assert value["status"] == "execution_invalid"


def test_nonfinite_response_values_cannot_break_json_evidence():
    from novita_self_test.capability_runtime import parse_reply
    text = '{"choices":[{"index":0,"message":{"role":"assistant","content":"OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1e999}}'
    assert parse_reply(200, text, stream=False).errors
