"""Endpoint-only helpers for the tool-call contract suite.

Pure logic (no pytest): build requests, call a chat endpoint (non-stream or SSE),
normalise the reply, and judge it against engine-level invariants taken from the
K3 tool-call contract. Nothing here depends on which inference framework serves
the endpoint, and nothing assumes a particular model output beyond what the
contract guarantees.

Contract sources are cited next to each rule; see README.md in this directory.
"""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx

# K3 structural tokens (K3 tokenizer). K3.1 must re-verify them against its
# checkpoint; override with TC_MARKERS="<|a|>,<|b|>,..." when they differ.
DEFAULT_MARKERS = ("<|open|>", "<|close|>", "<|sep|>", "<|end_of_msg|>", "<|media_pad|>")


def markers() -> tuple[str, ...]:
    raw = os.environ.get("TC_MARKERS", "").strip()
    return tuple(m for m in (p.strip() for p in raw.split(",")) if m) if raw else DEFAULT_MARKERS


def token_param() -> str:
    """Wire name of the output-token cap (same switch as the official KVV suites)."""
    return os.environ.get("KVV_TOKEN_PARAM", "max_tokens")


def thinking_body(thinking: bool, think_mode: str, effort: str | None) -> dict[str, Any]:
    """Thinking switch in the wire style selected by --think-mode.

    Mirrors kvv-k3 tests/helper.py::get_thinking_extra_body so results are
    comparable with the official suites.
    """
    effort = effort if thinking else "none"
    if think_mode == "opensource":
        return {"reasoning_effort": effort} if effort else {}
    if not thinking:
        return {"thinking": {"type": "disabled"}}
    if effort and effort != "none":
        return {"thinking": {"type": "enabled", "keep": "all", "effort": effort}}
    return {"thinking": {"type": "enabled", "keep": "all"}}


@dataclass
class Reply:
    status: int | None  # None: transport failure (no HTTP response)
    error: str = ""  # transport error text, or raw body of a non-200 response
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {id,name,arguments,index}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    stream: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def prompt_tokens(self) -> int | None:
        value = (self.usage or {}).get("prompt_tokens")
        return value if isinstance(value, int) else None

    def describe(self) -> str:
        """Failure text. Deliberately starts with ``HTTP <status>`` for non-200 so the
        harness classifies 429/5xx/timeouts as service instability (rerun), never as a
        model failure. Tests that *expect* a 4xx/5xx use :func:`status_text` instead."""
        if self.status is None:
            return f"transport error: {self.error}"
        if self.status != 200:
            return f"HTTP {self.status}: {self.error[:300]}"
        return "HTTP 200"


def status_text(reply: Reply) -> str:
    """Status rendering that the harness stability classifier does NOT treat as a retryable
    service failure (it matches ``HTTP 5xx`` / ``Error code: 5xx``)."""
    return "no response" if reply.status is None else f"status={reply.status}"


def build_request(
    model: str,
    messages: list[dict[str, Any]],
    *,
    think_mode: str,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    thinking: bool = False,
    effort: str | None = None,
    max_tokens: int = 1024,
    stream: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": messages, token_param(): max_tokens}
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    payload.update(thinking_body(thinking, think_mode, effort))
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    payload.update(extra)
    return payload


def _usage_of(obj: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(obj.get("usage"), dict):
        return obj["usage"]
    for choice in obj.get("choices") or []:  # Kimi style: usage inside the choice
        if isinstance(choice, dict) and isinstance(choice.get("usage"), dict):
            return choice["usage"]
    return None


def _parse_message(body: dict[str, Any]) -> Reply:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls = []
    for position, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        calls.append(
            {
                "id": call.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments"),
                "index": call.get("index", position),
            }
        )
    return Reply(
        status=200,
        content=message.get("content") or "",
        reasoning=message.get("reasoning_content") or message.get("reasoning") or "",
        tool_calls=calls,
        finish_reason=choice.get("finish_reason"),
        usage=_usage_of(body),
    )


def accumulate_stream(chunks: Iterable[dict[str, Any]]) -> Reply:
    """Rebuild a non-stream-shaped Reply from SSE chunk dicts (tool calls merged by index)."""
    content: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    usage: dict[str, Any] | None = None
    for chunk in chunks:
        usage = _usage_of(chunk) or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            piece = delta.get("reasoning_content") or delta.get("reasoning")
            if piece:
                reasoning.append(piece)
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                slot = calls.setdefault(index, {"id": None, "name": None, "arguments": "", "index": index})
                slot["id"] = call.get("id") or slot["id"]
                function = call.get("function") or {}
                slot["name"] = function.get("name") or slot["name"]
                slot["arguments"] += function.get("arguments") or ""
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    return Reply(
        status=200,
        content="".join(content),
        reasoning="".join(reasoning),
        tool_calls=[calls[i] for i in sorted(calls)],
        finish_reason=finish,
        usage=usage,
        stream=True,
    )


class Endpoint:
    def __init__(self, base_url: str, api_key: str, model: str, think_mode: str, *, timeout: float = 300):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.think_mode = think_mode
        self.timeout = timeout
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def request(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        return build_request(self.model, messages, think_mode=self.think_mode, **kwargs)

    def call(self, payload: dict[str, Any]) -> Reply:
        stream = bool(payload.get("stream"))
        try:
            with httpx.Client(timeout=self.timeout) as client:
                if not stream:
                    response = client.post(self.url, json=payload, headers=self._headers)
                    if response.status_code != 200:
                        return Reply(response.status_code, error=response.text)
                    return _parse_message(response.json())
                with client.stream("POST", self.url, json=payload, headers=self._headers) as response:
                    if response.status_code != 200:
                        response.read()
                        return Reply(response.status_code, error=response.text, stream=True)
                    chunks = []
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunks.append(json.loads(data))
                    return accumulate_stream(chunks)
        except (httpx.HTTPError, ValueError) as exc:
            return Reply(None, error=f"{type(exc).__name__}: {exc}", stream=stream)


def run_samples(count: int, fn: Callable[[int], Any], concurrency: int = 4) -> list[Any]:
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, count))) as pool:
        return list(pool.map(fn, range(count)))


# --------------------------------------------------------------------------- judging


@dataclass
class Verdict:
    problems: list[str] = field(default_factory=list)
    inconclusive: bool = False  # e.g. truncated by max_tokens: engine behaviour not observable


def leaks(text: str) -> list[str]:
    return [m for m in markers() if m in text]


def judge_reply(
    reply: Reply,
    declared: set[str],
    *,
    require_tool_call: bool = False,
    allow_reasoning_only: bool = False,
) -> Verdict:
    """Engine-level invariants of one tool-calling response.

    Source rules: guide 3.4 (empty turn), 3.6 (no XTML in content, undeclared tool dropped,
    arguments must be JSON), 3.7 (finish_reason is tool_calls iff there are tool calls).
    """
    verdict = Verdict()
    if not reply.ok:
        verdict.problems.append(reply.describe())
        return verdict
    for label, text in (("content", reply.content), ("reasoning", reply.reasoning)):
        found = leaks(text)
        if found:
            verdict.problems.append(f"structural marker leaked into {label}: {found}")
    for call in reply.tool_calls:
        name = call.get("name")
        arguments = call.get("arguments") or ""
        for found in leaks(arguments):
            verdict.problems.append(f"structural marker leaked into arguments of {name!r}: {found}")
    if reply.finish_reason == "length":
        verdict.inconclusive = True
        return verdict
    if not reply.tool_calls and not reply.content.strip() and not (allow_reasoning_only and reply.reasoning.strip()):
        verdict.problems.append(
            f"empty turn: no content and no tool_calls (finish_reason={reply.finish_reason!r})"
        )
    if require_tool_call and not reply.tool_calls:
        verdict.problems.append("tool_choice=required but no tool_calls returned")
    ids = [call.get("id") for call in reply.tool_calls]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        verdict.problems.append(f"tool_call ids missing or duplicated: {ids}")
    for call in reply.tool_calls:
        name = call.get("name")
        if name not in declared:
            verdict.problems.append(f"undeclared tool returned: {name!r} (declared: {sorted(declared)})")
        try:
            parsed = json.loads(call.get("arguments") or "")
        except (TypeError, ValueError):
            verdict.problems.append(f"arguments of {name!r} are not valid JSON: {(call.get('arguments') or '')[:120]!r}")
            continue
        if not isinstance(parsed, dict):
            verdict.problems.append(f"arguments of {name!r} are not a JSON object: {parsed!r}")
    if reply.tool_calls and reply.finish_reason != "tool_calls":
        verdict.problems.append(f"finish_reason={reply.finish_reason!r} with tool_calls present")
    if not reply.tool_calls and reply.finish_reason == "tool_calls":
        verdict.problems.append("finish_reason='tool_calls' without any tool_calls")
    return verdict


def summarize(verdicts: list[Verdict], *, max_inconclusive: float = 0.5) -> list[str]:
    """Collapse per-sample verdicts into failure lines (empty list = pass)."""
    failures: list[str] = []
    for position, verdict in enumerate(verdicts):
        for problem in verdict.problems:
            failures.append(f"sample {position}: {problem}")
    inconclusive = sum(v.inconclusive for v in verdicts)
    if verdicts and inconclusive / len(verdicts) > max_inconclusive:
        failures.append(
            f"{inconclusive}/{len(verdicts)} samples were cut off by max_tokens; "
            "raise TC_MAX_TOKENS so the engine behaviour can be observed"
        )
    return failures


def json_conforms(text: str, schema: dict[str, Any]) -> str | None:
    """None if ``text`` is JSON satisfying ``schema``; otherwise the reason."""
    import jsonschema

    try:
        value = json.loads(text)
    except ValueError as exc:
        return f"content is not JSON ({exc}): {text[:120]!r}"
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=str)
    return f"schema violation: {errors[0].message}" if errors else None
