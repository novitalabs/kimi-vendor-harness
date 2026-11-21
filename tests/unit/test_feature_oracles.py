"""Offline oracle negatives, not evidence of any engine passing these cases."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from novita_self_test.capability_runtime import assert_reply, parse_reply, run_plan, CapabilityClient
from novita_self_test.case_catalog import load_cases, build_plan, read_request
from novita_self_test.config import ServiceAccess

ROOT = Path(__file__).resolve().parents[2]


def reply(*, content="", name=None, arguments="{}"):
    message = {"role": "assistant", "content": content}
    if name is not None:
        message["tool_calls"] = [{"id": "call_0", "type": "function", "function": {"name": name, "arguments": arguments}}]
    return parse_reply(200, json.dumps({"choices": [{"index": 0, "message": message,
        "finish_reason": "tool_calls" if name else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}), stream=False)


def test_all_schema_requests_have_exact_local_oracles_and_negative_controls():
    cases = [case for case in load_cases(ROOT) if case["id"].startswith("tool_call.schema.")]
    assert len(cases) >= 4
    for case in cases:
        step = case["steps"][0]
        request = read_request(step, ROOT)
        function = request["tools"][0]["function"]
        assert function.get("strict") is True
        assert function["parameters"] == step["expect"]["arguments_schema"]
        for bad in ('{}', '[]', 'null', '{"unrelated": true}'):
            assert assert_reply(reply(name=function["name"], arguments=bad), step["expect"], []), case["id"]
        assert assert_reply(reply(name="not_declared", arguments="{}"), step["expect"], [])


@pytest.mark.parametrize("content", ["[]", "null", "{}", '{"other":"x"}', '{"city":"x","temperature":true,"summary":"x"}'])
def test_tools_plus_response_format_rejects_unrelated_or_wrong_typed_json(content):
    case = next(case for case in load_cases(ROOT) if case["id"] == "structured_output.tools_combined")
    assert assert_reply(reply(content=content), case["steps"][0]["expect"], [])


def test_first_call_only_never_exempts_later_undeclared_names():
    actual = reply(name="get_weather")
    actual.calls.append({"id": "call_1", "name": "unknown", "arguments": "{}"})
    problems = assert_reply(actual, {"status": 200, "first_call_only": True, "tool_names": ["get_weather"]}, [])
    assert any(problem["code"] == "tool_name_undeclared" for problem in problems)


def test_marker_and_conflict_probes_are_observations_not_engine_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("ORACLE_KEY", "synthetic-key")
    monkeypatch.setenv("NVST_RESULTS_ROOT", str(tmp_path))
    access = ServiceAccess.from_dict({"model_family": "kimi-k3", "base_url": "http://127.0.0.1:1/v1",
        "served_model": "fake", "api_key_env": "ORACLE_KEY", "headers": {}, "think_mode": "opensource"})
    case = copy.deepcopy(next(case for case in load_cases(ROOT) if case["id"] == "prompt_rendering.literal_markers"))
    assert case["determinism"] == "observation"
    assert "minimum_prompt_delta" not in case["steps"][1]["expect"]
    # Even a fake that tokenizes all markers as special ids cannot be a parser pass.
    monkeypatch.setattr(CapabilityClient, "call", lambda *args: reply(content="OK"))
    result = run_plan(build_plan([case], model="kimi-k3", root=ROOT), access, max_requests=2)
    assert result["status"] == "observed"
    assert {row["status"] for row in result["results"]} == {"observed"}


def test_global_and_dynamic_case_checks_both_declaration_sets():
    case = next(case for case in load_cases(ROOT) if case["id"] == "tool_call.declaration.global_and_dynamic")
    assert len(case["steps"]) == 2
    assert [step["expect"]["tool_names"] for step in case["steps"]] == [["get_weather"], ["get_stock_price"]]
    for step in case["steps"]:
        wrong = "get_stock_price" if step["expect"]["tool_names"] == ["get_weather"] else "get_weather"
        assert assert_reply(reply(name=wrong), step["expect"], [])
