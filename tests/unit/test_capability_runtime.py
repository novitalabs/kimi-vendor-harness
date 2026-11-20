from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from novita_self_test import capability_runtime as runtime
from novita_self_test.capability_runtime import (
    CapabilityClient, Reply, assert_reply, materialize_step, parse_reply, run_plan, service_failure,
)
from novita_self_test.config import ConfigError, ServiceAccess


def completion(content="OK", *, calls=None, finish="stop", usage=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}


def tool_call(name="get_weather", arguments='{"city":"Beijing"}', id="get_weather_0"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def stream_frames():
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, **tool_call(arguments="")}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Bei'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'jing"}'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
    ]


def sse(frames, done=True):
    return "\n\n".join("data: " + json.dumps(item) for item in frames) + ("\n\ndata: [DONE]\n\n" if done else "\n\n")


def test_parse_stream_merges_tool_deltas_and_requires_done():
    reply = parse_reply(200, sse(stream_frames()), stream=True)
    assert not reply.errors
    assert reply.done
    assert reply.calls[0]["arguments"] == '{"city":"Beijing"}'
    assert reply.finish_reason == "tool_calls"
    assert reply.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}


@pytest.mark.parametrize("fault", ["missing_first_id", "missing_first_name", "missing_type", "first_arguments", "id_changed", "name_changed", "after_finish", "missing_role", "duplicate_finish", "missing_arguments"])
def test_malformed_stream_lifecycle_is_rejected(fault):
    frames = stream_frames()
    first = frames[1]["choices"][0]["delta"]["tool_calls"][0]
    second = frames[2]["choices"][0]["delta"]["tool_calls"][0]
    if fault == "missing_first_id":
        first.pop("id")
    elif fault == "missing_first_name":
        first["function"].pop("name")
    elif fault == "missing_type":
        first.pop("type")
    elif fault == "first_arguments":
        first["function"]["arguments"] = "{"
    elif fault == "id_changed":
        second["id"] = "different_9"
    elif fault == "name_changed":
        second["function"]["name"] = "different"
    elif fault == "after_finish":
        frames.insert(-1, {"choices": [{"index": 0, "delta": {"content": "late"}, "finish_reason": None}]})
    elif fault == "missing_role":
        frames[0]["choices"][0]["delta"].pop("role")
    elif fault == "duplicate_finish":
        frames.insert(-1, copy.deepcopy(frames[-2]))
    elif fault == "missing_arguments":
        second["function"].pop("arguments")
    reply = parse_reply(200, sse(frames), stream=True)
    assert reply.errors, fault


def test_stream_summary_is_allowed_but_missing_done_is_inconclusive():
    reply = parse_reply(200, sse(stream_frames(), done=False), stream=True)
    assert "missing DONE marker" in reply.errors
    assert reply.incomplete
    assert reply.calls[0]["name"] == "get_weather"
    assert not any("after finish" in error for error in reply.errors)


@pytest.mark.parametrize("body", [[], {"choices": []}, completion([]), completion(False), completion(calls={}), completion(calls=[{"function": []}]), completion(usage=[1]), completion(finish=7)])
def test_malformed_success_shape_becomes_a_case_error(body):
    reply = parse_reply(200, json.dumps(body), stream=False)
    assert reply.errors
    assert assert_reply(reply, {"status": 200, "public_turn": True}, [])


def test_assertion_distinguishes_invalid_request_from_server_failure():
    assert assert_reply(Reply(400, '{"error":{"message":"invalid schema"}}'), {"status": 400, "error_object": True}, []) == []
    assert assert_reply(Reply(500, "crashed"), {"status": 400, "error_object": True}, []) == ["status=500, expected=400"]


@pytest.mark.parametrize("status,raw,errors,expected", [
    (401, "", [], "service_invalid"), (403, "", [], "service_invalid"),
    (402, '{"error":{"code":"insufficient_quota"}}', [], "quota_exhausted"),
    (429, "rate limit", [], "rate_limited"), (503, "overloaded", [], "service_unavailable"),
    (None, "", ["transport failure: ReadTimeout"], "execution_invalid"),
    (200, "answer mentions authentication and invalid api key", [], None),
    (400, '{"error":{"message":"unsupported authentication field"}}', [], None),
])
def test_service_failure_classes_do_not_scan_model_text(status, raw, errors, expected):
    assert service_failure(status, raw, errors) == expected


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_ONLY_KEY", "synthetic-review-secret")
    monkeypatch.setenv("NVST_RESULTS_ROOT", str(tmp_path / "results"))
    return ServiceAccess.from_dict({"model_family": "kimi-k3", "base_url": "http://127.0.0.1:1/v1", "served_model": "kimi-k3", "api_key_env": "TEST_ONLY_KEY", "headers": {}, "think_mode": "opensource"})


def make_case(id="case.one", expect=None):
    return {"id": id, "capability": "protocol", "scope": "endpoint", "modes": ["nonstream"],
            "steps": [{"request": {"messages": [{"role": "user", "content": "Hi"}]}, "expect": expect or {"status": 200, "public_turn": True}}],
            "max_output_tokens": 16, "missing_requirements": [], "likely_components": ["api"], "knowledge": [], "source": {}}


def plan(*cases, repeat=1):
    count = sum(len(case["steps"]) * len(case["modes"]) for case in cases) * repeat
    return {"model": "kimi-k3", "repeat": repeat, "request_budget": count, "cases": [{**case, "missing_requirements": []} for case in cases]}


@pytest.mark.parametrize("reply,status", [(Reply(401, '{"error":{"message":"invalid api key"}}'), "service_invalid"), (Reply(429), "execution_invalid"), (Reply(503), "execution_invalid"), (Reply(None, errors=["transport failure: ConnectError"]), "execution_invalid")])
def test_service_failure_stops_before_later_cases(service, monkeypatch, tmp_path, reply, status):
    calls = []
    def fake_call(self, request):
        calls.append(request)
        return reply
    monkeypatch.setattr(CapabilityClient, "call", fake_call)
    report = run_plan(plan(make_case(), make_case("case.two")), service, max_requests=2, root=tmp_path)
    assert len(calls) == report["actual_requests"] == 1
    assert report["status"] == status
    assert [item["status"] for item in report["results"]] == [status, "not_run"]
    assert Path(report["run_dir"], report["results"][0]["evidence"]).is_file()


def test_invalid_schema_server_crash_is_contract_failure_and_stops(service, monkeypatch, tmp_path):
    monkeypatch.setattr(CapabilityClient, "call", lambda self, request: Reply(500, "crashed"))
    report = run_plan(plan(make_case(expect={"status": 400, "error_object": True}), make_case("later")), service, max_requests=2, root=tmp_path)
    assert report["status"] == "failed"
    assert report["actual_requests"] == 1
    assert report["stop_reason"] == "service_unavailable"


def test_malformed_response_and_oracle_exception_keep_evidence(service, monkeypatch, tmp_path):
    reply = parse_reply(200, json.dumps(completion([])), stream=False)
    monkeypatch.setattr(CapabilityClient, "call", lambda self, request: reply)
    report = run_plan(plan(make_case()), service, max_requests=1, root=tmp_path)
    evidence = json.loads(Path(report["run_dir"], report["results"][0]["evidence"]).read_text())
    assert evidence["response"]["raw"] == reply.raw
    assert report["status"] == "failed"
    def broken_oracle(*args, **kwargs):
        raise RuntimeError("oracle defect")
    monkeypatch.setattr(runtime, "assert_reply", broken_oracle)
    report = run_plan(plan(make_case()), service, max_requests=1, root=tmp_path)
    evidence = json.loads(Path(report["run_dir"], report["results"][0]["evidence"]).read_text())
    assert evidence["response"]["raw"] == reply.raw
    assert report["status"] == "execution_invalid"
    assert report["stop_reason"] == "runner_error"


def test_all_report_representations_redact_echoed_secret(service, monkeypatch, tmp_path):
    key = service.credential()
    reply = parse_reply(200, json.dumps(completion("", calls=[tool_call(name=key)], finish="tool_calls")), stream=False)
    monkeypatch.setattr(CapabilityClient, "call", lambda self, request: reply)
    case = make_case(expect={"status": 200})
    report = run_plan(plan(case), service, max_requests=1, root=tmp_path)
    assert key not in json.dumps(report)
    for file in Path(report["run_dir"]).iterdir():
        assert key not in file.read_text(), file
    assert "[REDACTED]" in Path(report["run_dir"], "report.md").read_text()


def test_repeat_usage_comparison_is_local_to_each_repeat(service, monkeypatch, tmp_path):
    case = make_case()
    case["steps"] = [
        {"request": case["steps"][0]["request"], "expect": {"status": 200, "usage": True}},
        {"request": case["steps"][0]["request"], "expect": {"status": 200, "usage": True, "same_prompt_as": 0}},
    ]
    counts = iter([100, 100, 200, 200])
    def fake_call(self, request):
        n = next(counts)
        return parse_reply(200, json.dumps(completion(usage={"prompt_tokens": n, "completion_tokens": 3, "total_tokens": n+3})), stream=False)
    monkeypatch.setattr(CapabilityClient, "call", fake_call)
    report = run_plan(plan(case, repeat=2), service, max_requests=4, root=tmp_path)
    assert report["status"] == "passed"
    assert report["actual_requests"] == 4


def test_truncated_output_is_inconclusive_not_model_failure(service, monkeypatch, tmp_path):
    monkeypatch.setattr(CapabilityClient, "call", lambda self, request: parse_reply(200, json.dumps(completion("partial", finish="length")), stream=False))
    report = run_plan(plan(make_case()), service, max_requests=1, root=tmp_path)
    assert report["status"] == "incomplete"
    assert report["results"][0]["status"] == "inconclusive"


def test_output_cap_allows_intentional_truncation_but_checks_usage(service, monkeypatch, tmp_path):
    from novita_self_test.case_catalog import load_cases
    case = next(item for item in load_cases() if item["id"] == "protocol.token_limits")
    replies = iter([
        parse_reply(200, json.dumps(completion("", finish="length", usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})), stream=False),
        parse_reply(200, json.dumps(completion("", finish="length", usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4})), stream=False),
    ])
    monkeypatch.setattr(CapabilityClient, "call", lambda self, request: next(replies))
    first = run_plan(plan(case), service, max_requests=1, root=tmp_path)
    second = run_plan(plan(case), service, max_requests=1, root=tmp_path)
    assert first["status"] == "passed"
    assert second["status"] == "failed"
    assert "completion usage exceeds selected output cap" in second["results"][0]["problems"]


def test_token_wire_parameter_is_exclusive_and_invalid_value_is_local(tmp_path, service, monkeypatch):
    step = {"request": {"messages": [{"role": "user", "content": "Hi"}]}}
    monkeypatch.setenv("NVST_TOKEN_PARAM", "max_completion_tokens")
    wire = materialize_step(step, {"max_output_tokens": 1}, service, "nonstream", tmp_path)
    assert wire["max_completion_tokens"] == 1
    assert "max_tokens" not in wire
    monkeypatch.setenv("NVST_TOKEN_PARAM", "invalid")
    with pytest.raises(ConfigError, match="max_tokens or max_completion_tokens"):
        run_plan(plan(make_case()), service, max_requests=1, root=tmp_path)


def test_budget_mismatch_and_invalid_timeout_send_nothing(service, monkeypatch, tmp_path):
    def must_not_call(*args):
        pytest.fail("traffic before preflight")
    monkeypatch.setattr(CapabilityClient, "call", must_not_call)
    bad_plan = plan(make_case(), repeat=2)
    bad_plan["request_budget"] = 1
    with pytest.raises(ConfigError, match="differs from executable"):
        run_plan(bad_plan, service, max_requests=1, root=tmp_path)
    with pytest.raises(ConfigError, match="ceiling"):
        run_plan(plan(make_case(), repeat=2), service, max_requests=1, root=tmp_path)
    with pytest.raises(ConfigError, match="finite and positive"):
        run_plan(plan(make_case()), service, max_requests=1, timeout=float("nan"), root=tmp_path)
    assert not (tmp_path / "results").exists()


def test_fixture_preserves_non_runner_fields_and_stream_options(tmp_path, service):
    request = {"messages": [{"role": "user", "content": "Hi"}], "temperature": 0.6, "top_p": 0.95,
               "parallel_tool_calls": False, "stream_options": {"include_internal_content": True}}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request, indent=2))
    wire = materialize_step({"request_fixture": path.name}, {"max_output_tokens": 16}, service, "stream", tmp_path)
    assert {key: wire[key] for key in request if key != "stream_options"} == {key: request[key] for key in request if key != "stream_options"}
    assert wire["stream_options"] == {"include_internal_content": True, "include_usage": True}
    assert set(wire) - set(request) == {"model", "stream", "max_tokens"}
