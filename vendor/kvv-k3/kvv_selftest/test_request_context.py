"""Tests for request context (trace_id / request_id) capture."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import BadRequestError, RateLimitError
from inspect_ai.model import ChatMessageUser
from inspect_ai.model._model_call import ModelCall
from inspect_ai.model._model_output import ModelOutput
from inspect_ai.solver import TaskState

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kimi_model import KimiAPI, _ResponseHeaderTracker, _log_event
from utils import _copy_request_context, with_request_context


def _make_response(
    client_request_id: str,
    response_headers: dict[str, str],
    status_code: int = 200,
) -> httpx.Response:
    request = httpx.Request(
        "POST",
        "https://api.example.com/v1/chat/completions",
        headers={"x-irid": client_request_id},
    )
    return httpx.Response(
        status_code,
        headers=response_headers,
        request=request,
    )


def test_tracker_extracts_headers() -> None:
    tracker = _ResponseHeaderTracker()

    response = _make_response(
        "irid-123",
        {
            "msh-request-id": "req-msh",
            "x-request-id": "req-x",
            "x-moongate-request-id": "req-moongate",
            "x-msh-trace-id": "trace-abc",
        },
    )
    asyncio.run(tracker._on_response(response))

    context = asyncio.run(tracker.pop("irid-123"))
    assert context is not None
    assert context["client_request_id"] == "irid-123"
    assert context["request_id"] == "req-msh"
    assert context["trace_id"] == "trace-abc"


def test_tracker_falls_back_to_alternative_request_id_headers() -> None:
    tracker = _ResponseHeaderTracker()

    response = _make_response(
        "irid-456",
        {
            "x-request-id": "req-x",
            "x-msh-trace-id": "trace-def",
        },
    )
    asyncio.run(tracker._on_response(response))

    context = asyncio.run(tracker.pop("irid-456"))
    assert context is not None
    assert context["request_id"] == "req-x"
    assert context["trace_id"] == "trace-def"


def test_tracker_returns_none_for_unknown_id() -> None:
    tracker = _ResponseHeaderTracker()
    assert asyncio.run(tracker.pop("does-not-exist")) is None


def test_tracker_evicts_oldest_context_when_capacity_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_ResponseHeaderTracker, "_MAX_CONTEXTS", 2)
    tracker = _ResponseHeaderTracker()

    for i in range(3):
        response = _make_response(
            f"irid-{i}",
            {"x-request-id": f"req-{i}"},
        )
        asyncio.run(tracker._on_response(response))

    assert list(tracker._contexts) == ["irid-1", "irid-2"]


def test_initialize_installs_tracker_hook(api: KimiAPI) -> None:
    hooks = api.client._client.event_hooks.get("response", [])
    assert any(
        getattr(hook, "__self__", None) is api._response_tracker
        for hook in hooks
    )


def test_initialize_reinstalls_tracker_after_rebuild(api: KimiAPI) -> None:
    asyncio.run(api.aclose())
    api.initialize()

    hooks = api.client._client.event_hooks.get("response", [])
    assert any(
        getattr(hook, "__self__", None) is api._response_tracker
        for hook in hooks
    )


def test_capture_error_request_context_for_bad_request(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_log_event(*args: object, **kwargs: object) -> None:
        captured.append((args, kwargs))

    monkeypatch.setattr("kimi_model._log_event", fake_log_event)

    response = _make_response(
        "irid-400",
        {
            "msh-request-id": "req-400",
            "x-msh-trace-id": "trace-400",
        },
        status_code=400,
    )
    asyncio.run(api._response_tracker._on_response(response))

    ex = BadRequestError("bad request", response=response, body=None)
    request = {"extra_headers": {"x-irid": "irid-400"}}

    context = asyncio.run(api._capture_error_request_context(request, ex))

    assert context == {
        "client_request_id": "irid-400",
        "request_id": "req-400",
        "trace_id": "trace-400",
    }
    assert getattr(ex, "_kimi_request_context") == context
    assert getattr(ex, "_kimi_request_context_logged") is True
    assert captured[-1][0][0] == "error"
    assert captured[-1][1]["request_context"] == context


def test_capture_error_request_context_falls_back_to_exception_response(
    api: KimiAPI,
) -> None:
    response = _make_response(
        "irid-402",
        {
            "msh-request-id": "req-from-exception",
            "x-msh-trace-id": "trace-from-exception",
        },
        status_code=400,
    )
    ex = BadRequestError("bad request", response=response, body=None)
    request = {"extra_headers": {"x-irid": "irid-402"}}

    context = asyncio.run(api._capture_error_request_context(request, ex))

    assert context == {
        "client_request_id": "irid-402",
        "request_id": "req-from-exception",
        "trace_id": "trace-from-exception",
    }
    assert getattr(ex, "_kimi_request_context") == context


def test_capture_error_request_context_for_rate_limit_peeks_without_logging(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_log_event(*args: object, **kwargs: object) -> None:
        captured.append((args, kwargs))

    monkeypatch.setattr("kimi_model._log_event", fake_log_event)

    response = _make_response(
        "irid-429",
        {
            "msh-request-id": "req-429",
            "x-msh-trace-id": "trace-429",
        },
        status_code=429,
    )
    asyncio.run(api._response_tracker._on_response(response))

    ex = RateLimitError("rate limited", response=response, body=None)
    request = {"extra_headers": {"x-irid": "irid-429"}}

    context = asyncio.run(api._capture_error_request_context(request, ex))

    assert context == {
        "client_request_id": "irid-429",
        "request_id": "req-429",
        "trace_id": "trace-429",
    }
    assert getattr(ex, "_kimi_request_context") == context
    # Retryable errors are logged by should_retry, not here.
    assert getattr(ex, "_kimi_request_context_logged", False) is False
    assert captured == []
    # peek must not consume the tracker entry.
    assert asyncio.run(api._response_tracker.pop("irid-429")) is not None


def test_should_retry_logs_request_context_for_429(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_log_event(*args: object, **kwargs: object) -> None:
        captured.append((args, kwargs))

    monkeypatch.setattr("kimi_model._log_event", fake_log_event)

    response = _make_response(
        "irid-429b",
        {
            "msh-request-id": "req-429b",
            "x-msh-trace-id": "trace-429b",
        },
        status_code=429,
    )
    ex = RateLimitError("rate limited", response=response, body=None)
    request = {"extra_headers": {"x-irid": "irid-429b"}}

    asyncio.run(api._capture_error_request_context(request, ex))

    assert api.should_retry(ex) is True
    assert captured[-1][0][0] == "retry"
    assert captured[-1][1]["request_context"] == {
        "client_request_id": "irid-429b",
        "request_id": "req-429b",
        "trace_id": "trace-429b",
    }


def test_should_retry_logs_request_context_for_non_retryable_400(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_log_event(*args: object, **kwargs: object) -> None:
        captured.append((args, kwargs))

    monkeypatch.setattr("kimi_model._log_event", fake_log_event)

    response = _make_response("irid-401", {}, status_code=400)
    ex = BadRequestError("bad request", response=response, body=None)
    context = {
        "client_request_id": "irid-401",
        "request_id": "req-401",
        "trace_id": "trace-401",
    }
    setattr(ex, "_kimi_request_context", context)

    assert api.should_retry(ex) is False
    assert captured
    assert captured[-1][0][0] == "error"
    assert captured[-1][1]["request_context"] == context


def test_should_retry_skips_duplicate_400_logging(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_log_event(*args: object, **kwargs: object) -> None:
        captured.append((args, kwargs))

    monkeypatch.setattr("kimi_model._log_event", fake_log_event)

    response = _make_response("irid-402", {}, status_code=400)
    ex = BadRequestError("bad request", response=response, body=None)
    context = {
        "client_request_id": "irid-402",
        "request_id": "req-402",
        "trace_id": "trace-402",
    }
    setattr(ex, "_kimi_request_context", context)
    setattr(ex, "_kimi_request_context_logged", True)

    assert api.should_retry(ex) is False
    assert captured == []


def test_capture_400_does_not_consume_tracker_entry(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kimi_model._log_event", lambda *a, **k: None)

    response = _make_response(
        "irid-400b",
        {
            "msh-request-id": "req-400b",
            "x-msh-trace-id": "trace-400b",
        },
        status_code=400,
    )
    asyncio.run(api._response_tracker._on_response(response))

    ex = BadRequestError("context length exceeded", response=response, body=None)
    request = {"extra_headers": {"x-irid": "irid-400b"}}
    asyncio.run(api._capture_error_request_context(request, ex))

    # Soft-error 400 path: handle_bad_request still produces a ModelOutput,
    # so _attach_request_context must find the real context in the tracker.
    output = ModelOutput(model="test-model", choices=[])
    model_call = ModelCall.create(
        request={"extra_headers": {"x-irid": "irid-400b"}},
        response={"id": "chatcmpl-test"},
    )
    asyncio.run(api._attach_request_context(output, model_call))

    request_context = output.metadata["request_context"]
    assert request_context["client_request_id"] == "irid-400b"
    assert request_context["request_id"] == "req-400b"
    assert request_context["trace_id"] == "trace-400b"
    # _attach_request_context remains the single consumer of the entry.
    assert asyncio.run(api._response_tracker.pop("irid-400b")) is None


def test_log_event_includes_request_context_after_span(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class DummyEvent:
        span_id = "68bc6397deadbeef"

    class DummyTranscript:
        def __init__(self) -> None:
            self.events = [DummyEvent()]

        def info(self, *args: object, **kwargs: object) -> None:
            return None

    class DummyLogger:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def info(self, line: str) -> None:
            self.lines.append(line)

    dummy_logger = DummyLogger()
    monkeypatch.setattr("kimi_model._file_logger", dummy_logger)
    monkeypatch.setattr("kimi_model.transcript", lambda: DummyTranscript())

    _log_event(
        "error",
        "BadRequestError",
        'BadRequestError("Error code: 400")',
        False,
        "vendor-16-k3-test",
        request_context={
            "request_id": "req-401",
            "trace_id": "trace-401",
        },
    )

    assert dummy_logger.lines[0].startswith(
        "[ERROR] BadRequestError (model=vendor-16-k3-test, span=68bc6397, request_id=req-401, trace_id=trace-401):"
    )
    assert "span=68bc6397, request_id=req-401, trace_id=trace-401" in capsys.readouterr().out


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> KimiAPI:
    monkeypatch.setenv("KIMI_API_KEY", "dummy-key")
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.example.com/v1")
    return KimiAPI("kimi/test-model")


def test_attach_request_context_merges_metadata(api: KimiAPI) -> None:
    response = _make_response(
        "irid-789",
        {
            "msh-request-id": "req-xyz",
            "x-msh-trace-id": "trace-789",
        },
    )
    asyncio.run(api._response_tracker._on_response(response))

    output = ModelOutput(model="test-model", choices=[])
    output.metadata = {"original_finish_reason": "unexpected_state"}

    model_call = ModelCall.create(
        request={"extra_headers": {"x-irid": "irid-789"}},
        response={"id": "chatcmpl-test"},
    )

    asyncio.run(api._attach_request_context(output, model_call))

    assert output.metadata["original_finish_reason"] == "unexpected_state"
    request_context = output.metadata["request_context"]
    assert request_context["client_request_id"] == "irid-789"
    assert request_context["request_id"] == "req-xyz"
    assert request_context["trace_id"] == "trace-789"

    assert model_call.response["_request_context"] == request_context


def test_attach_request_context_without_capture(api: KimiAPI) -> None:
    output = ModelOutput(model="test-model", choices=[])
    model_call = ModelCall.create(
        request={"extra_headers": {"x-irid": "irid-no-response"}},
        response={"id": "chatcmpl-test"},
    )

    asyncio.run(api._attach_request_context(output, model_call))

    request_context = output.metadata["request_context"]
    assert request_context["client_request_id"] == "irid-no-response"
    assert request_context["request_id"] is None
    assert request_context["trace_id"] is None


def test_attach_request_context_skips_missing_header(api: KimiAPI) -> None:
    output = ModelOutput(model="test-model", choices=[])
    model_call = ModelCall.create(
        request={"extra_headers": {}},
        response={"id": "chatcmpl-test"},
    )

    asyncio.run(api._attach_request_context(output, model_call))

    assert output.metadata is None or "request_context" not in output.metadata


def test_copy_request_context_to_state_metadata() -> None:
    output = ModelOutput(model="test-model", choices=[])
    output.metadata = {
        "request_context": {
            "client_request_id": "cid-1",
            "request_id": "req-1",
            "trace_id": "trace-1",
        }
    }
    state = TaskState(
        model="kimi/test-model",
        sample_id="sample-1",
        epoch=1,
        input=[ChatMessageUser(content="hi")],
        messages=[],
        output=output,
        metadata={"foo": "bar"},
    )

    _copy_request_context(state)

    assert state.metadata["foo"] == "bar"
    assert state.metadata["request_context"]["client_request_id"] == "cid-1"
    assert state.metadata["request_context"]["request_id"] == "req-1"
    assert state.metadata["request_context"]["trace_id"] == "trace-1"


def test_with_request_context_wrapper() -> None:
    async def inner_solver(state: TaskState, generate: Any) -> TaskState:
        state.output = ModelOutput(model="test-model", choices=[])
        state.output.metadata = {
            "request_context": {
                "client_request_id": "cid-2",
                "request_id": "req-2",
                "trace_id": "trace-2",
            }
        }
        return state

    async def noop_generate(state: TaskState) -> TaskState:
        return state

    state = TaskState(
        model="kimi/test-model",
        sample_id="sample-2",
        epoch=1,
        input=[ChatMessageUser(content="hi")],
        messages=[],
        metadata={},
    )

    wrapped = with_request_context(inner_solver)
    result = asyncio.run(wrapped(state, noop_generate))

    assert result.metadata["request_context"]["client_request_id"] == "cid-2"
    assert result.metadata["request_context"]["request_id"] == "req-2"
    assert result.metadata["request_context"]["trace_id"] == "trace-2"
