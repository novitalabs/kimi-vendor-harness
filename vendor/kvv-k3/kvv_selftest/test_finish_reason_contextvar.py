"""Tests for per-coroutine original_finish_reason storage (contextvars)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from inspect_ai.model import ChatMessageUser, GenerateConfig
from openai import BadRequestError
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kimi_model import KimiAPI


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> KimiAPI:
    monkeypatch.setenv("KIMI_API_KEY", "dummy-key")
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.example.com/v1")
    return KimiAPI("kimi/test-model")


def _completion(finish_reason: str) -> ChatCompletion:
    choice = Choice(
        index=0,
        message=ChatCompletionMessage(role="assistant", content="ok"),
        finish_reason="stop",
    )
    # openai's Choice validates finish_reason as a literal; bypass validation
    # to emulate a non-standard value from the wire (as the API can return).
    object.__setattr__(choice, "finish_reason", finish_reason)
    return ChatCompletion(
        id="chatcmpl-test",
        model="test-model",
        object="chat.completion",
        created=0,
        choices=[choice],
        usage=CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2),
    )


def _unwrap(result: object) -> object:
    # KimiAPI.generate returns (output, model_call) for the tuple case.
    if isinstance(result, tuple):
        return result[0]
    return result


def test_concurrent_generate_finish_reason_isolation(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent generate() calls must not cross-contaminate
    metadata["original_finish_reason"]: one normalizes a non-standard
    finish_reason, the other is a plain stop."""
    normalize = api._normalize_finish_reason

    async def fake_generate_completion(request, config):
        marker = str(request["messages"][-1].get("content"))
        completion = _completion(
            "unexpected_state" if "normalize-me" in marker else "stop"
        )
        normalize(completion)  # real write to the per-coroutine storage
        # Force the other task to run between the write above and the read
        # in attach_metadata. With a plain instance attribute this
        # interleaving makes the assertions below fail reliably.
        await asyncio.sleep(0.05)
        return completion

    monkeypatch.setattr(api, "_generate_completion", fake_generate_completion)

    async def run_both():
        return await asyncio.gather(
            api.generate(
                [ChatMessageUser(content="normalize-me")], [], "auto", GenerateConfig()
            ),
            api.generate(
                [ChatMessageUser(content="plain stop")], [], "auto", GenerateConfig()
            ),
        )

    res_normalized, res_stop = asyncio.run(run_both())
    out_normalized = _unwrap(res_normalized)
    out_stop = _unwrap(res_stop)

    assert (out_normalized.metadata or {}).get("original_finish_reason") == (
        "unexpected_state"
    )
    assert not (out_stop.metadata or {}).get("original_finish_reason")


def test_generate_entry_reset_prevents_stale_finish_reason_on_error_path(
    api: KimiAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A soft-error 400 (handled into a ModelOutput) never reaches
    _normalize_finish_reason, so without the generate() entry reset it would
    inherit original_finish_reason from a previous generate() in the same
    task context."""
    normalize = api._normalize_finish_reason
    state = {"fail": False}

    async def fake_generate_completion(request, config):
        if state["fail"]:
            response = httpx.Response(
                400,
                request=httpx.Request(
                    "POST", "https://api.example.com/v1/chat/completions"
                ),
            )
            raise BadRequestError(
                "input length exceeded",
                response=response,
                body={"message": "input length exceeded"},
            )
        completion = _completion("unexpected_state")
        normalize(completion)  # real write to the per-coroutine storage
        return completion

    monkeypatch.setattr(api, "_generate_completion", fake_generate_completion)

    async def run_sequential():
        first = await api.generate(
            [ChatMessageUser(content="normalize-me")], [], "auto", GenerateConfig()
        )
        state["fail"] = True
        # Soft-error 400: handle_bad_request turns it into a ModelOutput
        # without _normalize_finish_reason ever running for this call.
        second = await api.generate(
            [ChatMessageUser(content="soft 400")], [], "auto", GenerateConfig()
        )
        return first, second

    out_first, out_second = (_unwrap(r) for r in asyncio.run(run_sequential()))

    assert (out_first.metadata or {}).get("original_finish_reason") == (
        "unexpected_state"
    )
    assert not (out_second.metadata or {}).get("original_finish_reason")
