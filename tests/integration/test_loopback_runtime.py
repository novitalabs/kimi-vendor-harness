"""Real HTTP negative controls, bound exclusively to 127.0.0.1 (no model).

All keys and responses here are synthetic. HTTP traffic is counted by the
server as well as the runner so records cannot be mistaken for requests.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from novita_self_test import capability_runtime as runtime
from novita_self_test.case_catalog import build_plan, load_cases, select_cases
from novita_self_test.cli import main
from novita_self_test.config import ConfigError, ServiceAccess

ROOT = Path(__file__).resolve().parents[2]


def completion(content="OK", *, calls=None, finish=None, usage=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"id": "chatcmpl-test", "model": "fake", "object": "chat.completion", "created": 0,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish or ("tool_calls" if calls else "stop")}],
            "usage": usage or {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}


def call(name="get_weather", arguments='{"city":"Beijing"}', id="call_0"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def frames():
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, **call(arguments="")}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":"Beijing"}'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}},
    ]


def sse(chunks, done=True):
    return "".join("data: " + json.dumps({"id": "chatcmpl-test", "model": "fake", "object": "chat.completion.chunk", "created": 0, **chunk}) + "\n\n" for chunk in chunks) + ("data: [DONE]\n\n" if done else "")


def send(handler, status=200, body=None, *, raw=None, stream=False):
    data = (raw if raw is not None else json.dumps(body)).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "text/event-stream" if stream else "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)
    handler.wfile.flush()


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("LOOPBACK_TEST_KEY", "synthetic-loopback-secret")
    monkeypatch.setenv("NVST_RESULTS_ROOT", str(tmp_path / "runs"))

    @contextmanager
    def serve(responder=None, base_handler=BaseHTTPRequestHandler):
        requests = []

        class Handler(base_handler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                if responder is None:
                    # Rewind-free reuse of the healthy fixture's request handler.
                    import io
                    self.rfile = io.BytesIO(json.dumps(request).encode())
                    self.headers.replace_header("Content-Length", str(len(self.rfile.getvalue())))
                    return super().do_POST()
                responder(self, request, len(requests))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        access = ServiceAccess.from_dict({"model_family": "kimi-k3", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "served_model": "fake", "api_key_env": "LOOPBACK_TEST_KEY", "headers": {}, "think_mode": "opensource"})
        try:
            yield access, requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    return serve


def catalog_plan(*selectors, repeat=1):
    return build_plan(select_cases(load_cases(ROOT), list(selectors)), model="kimi-k3", repeat=repeat, root=ROOT)


def evidence(report, n=0):
    return json.loads(Path(report["run_dir"], report["results"][n]["evidence"]).read_text())


def test_healthy_smoke_through_cli_counts_exact_http_requests(endpoint, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("loopback_fake", ROOT / "tests/support/fake_k3_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with endpoint(base_handler=module.Handler) as (access, requests):
        path = tmp_path / "access.json"
        path.write_text(json.dumps(access.public_dict()))
        for args in (["doctor", "--service-access", str(path)], ["plan", "--select", "smoke", "--format", "summary"],
                     ["plan", "--select", "smoke"], ["parser", "validate"]):
            assert main(args) == 0
            capsys.readouterr()
        assert not requests
        for old in (["list"], ["capabilities", "plan"], ["capabilities", "run", "--execute"]):
            assert main(old) == 2
            assert json.loads(capsys.readouterr().out)["error"]["code"] == "arguments_invalid"
        assert not requests
        args = ["run", "--service-access", str(path), "--select", "smoke", "--execute"]
        assert main(args + ["--max-requests", "12"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "request_budget_exceeded"
        assert not requests
        assert main(args + ["--max-requests", "13"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert len(requests) == report["actual_requests"] == report["planned_requests"] == 13
        assert len(report["results"]) == 13
        assert report["status"] == "passed"
        for row in report["results"]:
            assert Path(report["run_dir"], row["evidence"]).is_file()


@pytest.mark.parametrize("status,code,want", [
    (401, "unauthorized", "service_invalid"), (403, "forbidden", "service_invalid"),
    (402, "insufficient_quota", "service_invalid"), (429, "rate_limit", "execution_invalid"),
    (500, "server_error", "execution_invalid"), (503, "overloaded", "execution_invalid"),
])
def test_http_service_fail_fast_and_no_retry(endpoint, status, code, want):
    with endpoint(lambda h, req, n: send(h, status, {"error": {"code": code, "message": code}})) as (access, requests):
        report = runtime.run_plan(catalog_plan("smoke"), access, max_requests=13)
        assert len(requests) == report["actual_requests"] == 1
        assert report["status"] == want
        assert all(row["status"] == "not_run" for row in report["results"][1:])
        assert evidence(report)["response"]["status"] == status


def test_expected_400_but_500_is_robustness_failure_and_stops(endpoint):
    with endpoint(lambda h, req, n: send(h, 500, {"error": {"message": "grammar crashed"}})) as (access, requests):
        plan = catalog_plan("error_handling.invalid_schema", "tool_call.choice.none")
        report = runtime.run_plan(plan, access, max_requests=3)
        assert report["status"] == "failed"
        assert len(requests) == 1
        assert report["stop_reason"] == "service_unavailable"


def test_http_malformed_200_keeps_raw_evidence(endpoint):
    body = completion([])
    with endpoint(lambda h, req, n: send(h, body=body)) as (access, requests):
        report = runtime.run_plan(catalog_plan("structured_output.json_object"), access, max_requests=1)
        assert report["status"] == "failed"
        assert len(requests) == 1
        assert json.loads(evidence(report)["response"]["raw"]) == body
        assert any(item["code"] == "response_invalid" for item in report["results"][0]["problems"])


@pytest.mark.parametrize("fault", ["healthy", "missing_id", "missing_name", "id_rewrite", "after_finish", "duplicate_usage", "missing_done", "after_done"])
def test_http_stream_lifecycle(endpoint, fault):
    chunks = frames()
    if fault == "missing_id":
        del chunks[1]["choices"][0]["delta"]["tool_calls"][0]["id"]
    elif fault == "missing_name":
        del chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"]
    elif fault == "id_rewrite":
        chunks[2]["choices"][0]["delta"]["tool_calls"][0]["id"] = "changed"
    elif fault == "after_finish":
        chunks.insert(-1, {"choices": [{"index": 0, "delta": {"content": "late"}, "finish_reason": None}]})
    elif fault == "duplicate_usage":
        chunks.append(copy.deepcopy(chunks[-1]))
    raw = sse(chunks, done=fault != "missing_done")
    if fault == "after_done":
        raw += sse([chunks[-1]], done=False)
    with endpoint(lambda h, req, n: send(h, raw=raw, stream=True)) as (access, requests):
        report = runtime.run_plan(catalog_plan("streaming.tool_call_deltas"), access, max_requests=1)
        want = "passed" if fault == "healthy" else "incomplete" if fault == "missing_done" else "failed"
        assert report["status"] == want
        assert len(requests) == 1
        assert evidence(report)["response"]["raw"] == raw


@pytest.mark.parametrize("done", [False, True])
def test_http_timeout_retains_partial_or_done_bytes(endpoint, done):
    release = threading.Event()
    raw = sse(frames(), done=done)
    def stall(handler, req, n):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", str(len(raw.encode()) + 1024))
        handler.end_headers()
        handler.wfile.write(raw.encode())
        handler.wfile.flush()
        release.wait(2)
    try:
        with endpoint(stall) as (access, requests):
            report = runtime.run_plan(catalog_plan("streaming.tool_call_deltas"), access, max_requests=1, timeout=0.5)
            assert report["status"] == "execution_invalid"
            assert len(requests) == 1
            response = evidence(report)["response"]
            assert response["raw"] == raw
            assert response["done"] == done
            assert response["chunks"]
            assert response["incomplete"]
    finally:
        release.set()


@pytest.mark.parametrize("content", ["[]", "null", "{}", '{"other":1}'])
def test_http_tools_combination_rejects_wrong_json(endpoint, content):
    with endpoint(lambda h, req, n: send(h, body=completion(content))) as (access, requests):
        report = runtime.run_plan(catalog_plan("structured_output.tools_combined"), access, max_requests=2)
        assert report["status"] != "passed"
        assert report["results"][0]["status"] == "failed"
        assert any(p["code"] == "content_schema_mismatch" for p in report["results"][0]["problems"])


def test_http_secret_echo_redacts_stdout_markdown_json_and_raw(endpoint, tmp_path, capsys):
    key = "synthetic-loopback-secret"
    with endpoint(lambda h, req, n: send(h, body=completion(calls=[call(name=key)]))) as (access, requests):
        path = tmp_path / "access.json"
        path.write_text(json.dumps(access.public_dict()))
        assert main(["run", "--service-access", str(path), "--select", "tool_call.choice.required",
                     "--max-requests", "2", "--execute"]) == 1
        output = capsys.readouterr().out
        assert key not in output
        report = json.loads(output)
        for file in Path(report["run_dir"]).iterdir():
            assert key not in file.read_text(), file
        assert any(p["actual"] == "[REDACTED]" for p in report["results"][0]["problems"])
        assert "tool_name_undeclared" in Path(report["run_dir"], "report.md").read_text()


def test_http_repeat_history_and_mode_override_are_isolated(endpoint):
    def respond(handler, req, n):
        count = 100 if n <= 2 else 200
        usage = {"prompt_tokens": count, "completion_tokens": 3, "total_tokens": count + 3}
        if req["stream"]:
            send(handler, raw=sse([
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "OK"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]), stream=True)
        else:
            send(handler, body=completion(usage=usage))
    with endpoint(respond) as (access, requests):
        report = runtime.run_plan(catalog_plan("usage.stream_consistency", repeat=2), access, max_requests=4)
        assert report["status"] == "passed"
        assert len(requests) == 4
        assert [request["stream"] for request in requests] == [False, True, False, True]
        assert len({row["evidence"] for row in report["results"]}) == 4


def test_frozen_agent_flow_reports_structured_failure_and_evidence(endpoint, tmp_path, capsys):
    from novita_self_test.contracts import validate_contract
    def respond(handler, request, n):
        if request["stream"]:
            chunks = frames()
            chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] = "unexpected"
            send(handler, raw=sse(chunks), stream=True)
        else:
            send(handler, body=completion(calls=[call(name="unexpected")]))
    with endpoint(respond) as (access, requests):
        path, plan_path = tmp_path / "access.json", tmp_path / "plan.json"
        path.write_text(json.dumps(access.public_dict()))
        assert main(["plan", "--select", "tool_call.choice.required", "--service-access", str(path),
                     "--max-requests", "2", "--out", str(plan_path)]) == 0
        plan = json.loads(capsys.readouterr().out)
        validate_contract(plan, "plan.schema.json")
        assert plan["executable"] and plan["request_budget"] == 2
        assert main(["doctor", "--plan", str(plan_path), "--service-access", str(path)]) == 0
        validate_contract(json.loads(capsys.readouterr().out), "doctor.schema.json")
        assert not requests
        assert main(["run", "--plan", str(plan_path), "--service-access", str(path), "--execute"]) == 1
        report = json.loads(capsys.readouterr().out)
        validate_contract(report, "report.schema.json")
        assert len(requests) == report["actual_requests"] == 2
        assert report["plan_sha256"] == plan["plan_sha256"]
        for row in report["results"]:
            assert row["problems"][0]["code"] == "tool_name_undeclared"
            assert row["knowledge"] and row["likely_components"]
            raw = json.loads(Path(report["run_dir"], row["evidence"]).read_text())
            validate_contract(raw, "evidence.schema.json")
            assert raw["result"]["problems"][0]["actual"] == "unexpected"


@pytest.mark.parametrize("override", [
    ["--model", "kimi-k3"], ["--select", "all"], ["--repeat", "2"],
    ["--timeout", "120"], ["--max-requests", "3"],
])
def test_frozen_plan_cli_rejects_expansion_or_reselection_with_zero_requests(endpoint, tmp_path, capsys, override):
    with endpoint(lambda h, req, n: send(h, body=completion())) as (access, requests):
        access_path, plan_path = tmp_path / "access.json", tmp_path / "plan.json"
        access_path.write_text(json.dumps(access.public_dict()))
        assert main(["plan", "--select", "tool_call.choice.required", "--service-access", str(access_path),
                     "--max-requests", "2", "--out", str(plan_path)]) == 0
        capsys.readouterr()
        assert main(["run", "--plan", str(plan_path), "--service-access", str(access_path), "--execute", *override]) == 2
        result = json.loads(capsys.readouterr().out)
        assert result["error"]["code"] == "plan_override_invalid"
        assert result["endpoint_requests"] == 0
        assert not requests


def test_frozen_plan_tamper_and_target_change_have_zero_traffic(endpoint, tmp_path, capsys):
    with endpoint(lambda h, req, n: send(h, body=completion())) as (access, requests):
        access_path, plan_path = tmp_path / "access.json", tmp_path / "plan.json"
        access_path.write_text(json.dumps(access.public_dict()))
        assert main(["plan", "--service-access", str(access_path), "--out", str(plan_path)]) == 0
        plan = json.loads(capsys.readouterr().out)
        plan["request_budget"] = 0
        plan_path.write_text(json.dumps(plan))
        assert main(["run", "--plan", str(plan_path), "--service-access", str(access_path), "--execute"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "plan_integrity_invalid"
        plan["request_budget"] = 13
        plan_path.write_text(json.dumps(plan))
        changed = access.public_dict()
        changed["served_model"] = "wrong-model"
        access_path.write_text(json.dumps(changed))
        assert main(["run", "--plan", str(plan_path), "--service-access", str(access_path), "--execute"]) == 2
        assert json.loads(capsys.readouterr().out)["error"]["code"] == "plan_target_mismatch"
        assert not requests


def test_frozen_run_lowered_budget_and_missing_opt_in_ack_send_nothing(endpoint, tmp_path, capsys):
    with endpoint(lambda h, req, n: send(h, body=completion())) as (access, requests):
        access_path, plan_path = tmp_path / "access.json", tmp_path / "plan.json"
        access_path.write_text(json.dumps(access.public_dict()))
        for selector, ceiling, extra, expected in [
            ("tool_call.choice.required", 2, ["--max-requests", "1"], "request_budget_exceeded"),
            ("conversation_state.arguments_recovery_a", 1, [], "opt_in_ack_required"),
        ]:
            assert main(["plan", "--service-access", str(access_path), "--select", selector,
                         "--max-requests", str(ceiling), "--out", str(plan_path)]) == 0
            capsys.readouterr()
            assert main(["run", "--plan", str(plan_path), "--service-access", str(access_path), "--execute", *extra]) == 2
            error = json.loads(capsys.readouterr().out)
            assert error["error"]["code"] == expected
            assert error["endpoint_requests"] == 0
        assert not requests


def test_invalid_utf8_retains_offsets_hash_and_timing_and_never_passes(endpoint):
    def respond(handler, request, n):
        payload = json.dumps(completion("replacement")).encode().replace(b"replacement", b"bad\xff")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
    with endpoint(respond) as (access, requests):
        report = runtime.run_plan(catalog_plan("reasoning.disabled"), access, max_requests=1)
        assert report["status"] == "failed"
        value = evidence(report)["response"]
        assert value["invalid_utf8"][0]["bytes_hex"] == "ff"
        assert value["raw_bytes_sha256"]
        assert value["timing"]["received_bytes"] > 0
        assert value["timing"]["elapsed_seconds"] >= value["timing"]["first_byte_seconds"]


def test_actual_service_call_identity_binds_to_canned_followup_and_is_frozen(endpoint):
    from novita_self_test.execution_plan import create_plan
    def respond(handler, request, n):
        followup = request["tool_choice"] == "none"
        if followup:
            previous = request["messages"][-3]
            result = request["messages"][-2]
            assert previous["role"] == "assistant"
            assert previous["tool_calls"][0]["id"] == "actual_generated_77"
            assert result["tool_call_id"] == "actual_generated_77"
            assert "WEATHER_OK" in result["content"]
            if request["stream"]:
                send(handler, stream=True, raw=sse([
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": '{"verification_code":"WEATHER_OK"}'}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]))
            else:
                send(handler, body=completion('{"verification_code":"WEATHER_OK"}'))
        elif request["stream"]:
            chunks = frames()
            chunks[1]["choices"][0]["delta"]["tool_calls"][0]["id"] = "actual_generated_77"
            send(handler, stream=True, raw=sse(chunks))
        else:
            send(handler, body=completion(calls=[call(id="actual_generated_77")]))
    with endpoint(respond) as (access, requests):
        plan = create_plan(model="kimi-k3", selectors=["conversation_state.bound_tool_roundtrip"], service=access, max_requests=4)
        assert plan["units"][1]["binding_sha256"]
        report = runtime.run_plan(plan, access, max_requests=4)
        assert report["status"] == "passed"
        assert report["actual_requests"] == len(requests) == 4
        assert evidence(report, 1)["request"]["messages"][-2]["tool_call_id"] == "actual_generated_77"


def test_failed_parent_skips_bound_followup_without_spending_its_budget(endpoint):
    with endpoint(lambda h, req, n: send(h, body=completion(calls=[call(name="wrong")]))) as (access, requests):
        plan = catalog_plan("conversation_state.bound_tool_roundtrip")
        plan["cases"][0]["modes"] = ["nonstream"]
        plan["request_budget"] = 2
        report = runtime.run_plan(plan, access, max_requests=2)
        assert report["status"] == "failed"
        assert len(requests) == 1
        assert report["results"][1]["status"] == "not_run"


def test_failed_report_replans_whole_case_for_new_runtime_and_compares_offline(endpoint, tmp_path, capsys):
    broken = True
    def respond(handler, request, n):
        if request["stream"]:
            chunks = frames()
            if broken:
                chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] = "wrong"
            send(handler, stream=True, raw=sse(chunks))
        else:
            send(handler, body=completion(calls=[call(name="wrong" if broken else "get_weather")]))
    with endpoint(respond) as (access, requests):
        path, plan_path = tmp_path / "access.json", tmp_path / "plan.json"
        path.write_text(json.dumps(access.public_dict()))
        assert main(["run", "--select", "tool_call.choice.required", "--service-access", str(path), "--max-requests", "2", "--execute"]) == 1
        first = json.loads(capsys.readouterr().out)
        before = Path(first["run_dir"]) / "report.json"
        changed = access.public_dict()
        changed["runtime_id"] = "synthetic-fixed-runtime"
        path.write_text(json.dumps(changed))
        assert main(["plan", "--failed-from", str(before), "--service-access", str(path), "--max-requests", "2", "--out", str(plan_path)]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert [case["id"] for case in plan["cases"]] == ["tool_call.choice.required"]
        assert len(requests) == 2
        broken = False
        assert main(["run", "--plan", str(plan_path), "--service-access", str(path), "--execute"]) == 0
        second = json.loads(capsys.readouterr().out)
        after = Path(second["run_dir"]) / "report.json"
        assert len(requests) == 4
        assert main(["compare", "--baseline", str(before), "--candidate", str(after)]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["changes"][0]["transition"] == "fixed"
        assert result["endpoint_requests"] == 0
        assert len(requests) == 4


def test_opt_in_requires_ack_before_any_http(endpoint):
    with endpoint(lambda h, req, n: send(h, body=completion())) as (access, requests):
        with pytest.raises(ConfigError, match="allow-opt-in"):
            runtime.run_plan(catalog_plan("conversation_state.arguments_recovery_a"), access, max_requests=1)
        assert not requests
