"""Tool-call contract suite: engine-level checks that need nothing but a chat endpoint.

Complements the official KVV suites (which cover request validation, tool_choice, dynamic tools,
response_format validation, streaming frame shape, prompt-token oracle cases) with checks for
failure modes they do not exercise. Every test cites the contract rule it enforces (see README.md).

Classes:
  gate     deterministic or "zero violations in N samples" engine invariants; any violation fails.
  observe  behaviour that is not a hard contract (yet); never fails, writes observations.jsonl.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import tc_contract as tc

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the weather of a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}},
            "required": ["city"],
        },
    },
}
CALC = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
}
WRITE_FILE = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write text to a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}
WEATHER_PROMPT = [{"role": "user", "content": "北京今天天气怎么样？请查一下。"}]
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "temperature": {"type": "number"}, "summary": {"type": "string"}},
    "required": ["city", "temperature", "summary"],
    "additionalProperties": False,
}


def names(tools: list[dict[str, Any]]) -> set[str]:
    return {t["function"]["name"] for t in tools}


def with_strict(tool: dict[str, Any], strict: bool | None) -> dict[str, Any]:
    tool = copy.deepcopy(tool)
    if strict is not None:
        tool["function"]["strict"] = strict
    return tool


def fail_unless_clean(failures: list[str]) -> None:
    assert not failures, "\n".join(failures[:8]) + (f"\n... {len(failures)} problems in total" if len(failures) > 8 else "")


# ------------------------------------------------------------------ 1. invalid schema (gate)

BAD_SCHEMAS = {
    "unknown_type": {"type": "bogus"},
    "type_not_string": {"type": 123},
    "properties_not_object": {"type": "object", "properties": [1, 2]},
    "required_not_array": {"type": "object", "properties": {"a": {"type": "string"}}, "required": "a"},
    "nested_unknown_type": {"type": "object", "properties": {"a": {"type": "nonsense"}}},
    "enum_not_array": {"type": "object", "properties": {"a": {"enum": "x"}}},
}


@pytest.mark.parametrize("choice,strict", [("auto", True), ("required", None)], ids=["auto-strict", "required"])
@pytest.mark.parametrize("schema_id", sorted(BAD_SCHEMAS))
def test_invalid_tool_schema_is_rejected_not_crashed(ep, schema_id, choice, strict):
    """Guide 3.4/3.7: a tool schema the grammar compiler cannot handle must come back as a client
    error (4xx), never as a server failure. (Engines may also accept a lenient schema: 200 passes.)

    Messages avoid the text `HTTP 5xx` on purpose: the harness would classify that as service
    instability and rerun it, hiding exactly the defect this test exists to catch."""
    tool = with_strict(WEATHER, strict)
    tool["function"]["parameters"] = copy.deepcopy(BAD_SCHEMAS[schema_id])
    reply = ep.call(ep.request(WEATHER_PROMPT, tools=[tool], tool_choice=choice, max_tokens=16))
    assert reply.status is not None, f"no response (connection dropped): {reply.error}"
    assert reply.status < 500, (
        f"invalid tool schema {schema_id!r} crashed the server ({tc.status_text(reply)}); "
        f"expected a 4xx rejection. body: {reply.error[:200]!r}"
    )
    if reply.status >= 400:
        assert reply.error.strip(), f"{tc.status_text(reply)} with an empty error body"


# ------------------------------------------------------------------ 2. usage consistency (gate)


@pytest.mark.parametrize(
    "variant",
    [
        {"tools": [WEATHER, CALC], "tool_choice": "auto"},
        {"tools": [WEATHER, CALC], "tool_choice": "auto", "thinking": True},
        {"response_format": {"type": "json_schema", "json_schema": {"name": "s", "schema": SUMMARY_SCHEMA}}},
    ],
    ids=["tools", "tools-thinking", "response_format"],
)
def test_prompt_tokens_identical_stream_vs_nonstream(ep, effort, variant):
    """Guide 3.7: usage.prompt_tokens is a property of the rendered prompt, so it must not depend on
    streaming and must be stable across repeats (prefix-cache hits must not change it)."""
    variant = dict(variant, effort=effort if variant.get("thinking") else None)
    seen: dict[str, int | None] = {}
    for label, stream in (("nonstream-1", False), ("nonstream-2", False), ("stream", True)):
        reply = ep.call(ep.request(WEATHER_PROMPT, max_tokens=1, stream=stream, **variant))
        assert reply.ok, reply.describe()
        assert reply.prompt_tokens is not None, f"{label}: no usage.prompt_tokens in the reply"
        seen[label] = reply.prompt_tokens
    assert len(set(seen.values())) == 1, f"prompt_tokens differ across equivalent requests: {seen}"


# ------------------------------------------------------------------ 3. markers are plain text (gate)

MARKER_TEXT = "<|close|><|sep|><|end_of_msg|>"  # three K3 structural tokens spelled as text


def _marker_placements(inserted: str) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]] | None]]:
    call = {"id": "get_weather_0", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "北京"}'}}

    def tool_history(result: str):
        return [
            {"role": "user", "content": "北京天气如何？"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "get_weather_0", "content": result},
            {"role": "user", "content": "谢谢，总结一下。"},
        ]

    return {
        "user": ([{"role": "user", "content": f"前文 {inserted} 后文"}], None),
        "system": ([{"role": "system", "content": f"前文 {inserted} 后文"}, {"role": "user", "content": "你好"}], None),
        "tool_result": (tool_history(f"晴 {inserted} 26度"), [WEATHER]),
    }


@pytest.mark.parametrize("placement", ["user", "system", "tool_result"])
def test_marker_text_is_rendered_as_plain_text(ep, placement):
    """Guide 3.3: text segments are encoded as ordinary text; a literal marker typed by a user (or
    returned by a tool) must not turn into a structural token. Spelled as text, 3 markers cost many
    tokens; if the engine tokenised them as special tokens they would cost exactly 3."""
    n_special = 3

    def count(inserted: str) -> int:
        messages, tools = _marker_placements(inserted)[placement]
        reply = ep.call(ep.request(messages, tools=tools, max_tokens=1))
        assert reply.ok, f"{placement}: {reply.describe()} (literal marker text must be accepted)"
        assert reply.prompt_tokens is not None, "no usage.prompt_tokens in the reply"
        return reply.prompt_tokens

    delta = count(MARKER_TEXT) - count("")
    assert delta >= 2 * n_special, (
        f"{placement}: adding {n_special} literal markers cost only {delta} prompt tokens; "
        "they appear to be tokenised as structural tokens instead of plain text"
    )


# ------------------------------------------------------------------ 4. no empty turn, well-formed (gate)


@pytest.mark.parametrize("stream", [False, True], ids=["nostream", "stream"])
@pytest.mark.parametrize("thinking", [False, True], ids=["think-off", "think-on"])
@pytest.mark.parametrize("choice", ["auto", "required"])
def test_every_turn_has_content_or_tool_call(ep, samples, concurrency, effort, max_tokens, choice, thinking, stream):
    """Guide 3.4 (empty turn), 3.6 (no marker in content, undeclared tool dropped, arguments are JSON),
    3.7 (finish_reason). N samples, zero violations allowed; truncated samples are inconclusive."""
    tools = [WEATHER, CALC]

    def one(_: int) -> tc.Verdict:
        payload = ep.request(
            WEATHER_PROMPT, tools=tools, tool_choice=choice, thinking=thinking,
            effort=effort if thinking else None, max_tokens=max_tokens, stream=stream,
        )
        return tc.judge_reply(ep.call(payload), names(tools), require_tool_call=choice == "required")

    fail_unless_clean(tc.summarize(tc.run_samples(samples, one, concurrency)))


@pytest.mark.parametrize("stream", [False, True], ids=["nostream", "stream"])
def test_many_tools_with_default_tool_choice(ep, samples, concurrency, effort, max_tokens, stream):
    """Regression for the historical empty-turn incident: 26 tools, tool_choice omitted, thinking on."""
    filler = [
        {"type": "function", "function": {"name": f"lookup_item_{i:02d}", "description": f"Look up item {i}.",
         "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}
        for i in range(25)
    ]
    tools = filler + [WEATHER]

    def one(_: int) -> tc.Verdict:
        payload = ep.request(WEATHER_PROMPT, tools=tools, thinking=True, effort=effort, max_tokens=max_tokens, stream=stream)
        return tc.judge_reply(ep.call(payload), names(tools))

    fail_unless_clean(tc.summarize(tc.run_samples(samples, one, concurrency)))


@pytest.mark.parametrize("stream", [False, True], ids=["nostream", "stream"])
def test_non_strict_tools_return_only_declared_tools(ep, samples, concurrency, effort, max_tokens, stream):
    """Guide 3.6: with strict=false the grammar does not pin the tool name, so the parser must drop a
    call to an undeclared tool (observed once in 6 samples). required forces a call every time."""
    tools = [with_strict(t, False) for t in (WEATHER, CALC)]

    def one(_: int) -> tc.Verdict:
        payload = ep.request(WEATHER_PROMPT, tools=tools, tool_choice="required", thinking=True,
                             effort=effort, max_tokens=max_tokens, stream=stream)
        return tc.judge_reply(ep.call(payload), names(tools), require_tool_call=True)

    fail_unless_clean(tc.summarize(tc.run_samples(samples, one, concurrency)))


# ------------------------------------------------------------------ 5. response_format + tools (gate)

JSON_PROMPT = [{"role": "user", "content": "请用 JSON 格式总结：北京今天晴，26 度。包含 city、temperature、summary。不需要查询任何工具。"}]


@pytest.mark.parametrize(
    "kind,choice",
    [("json_schema", "none"), ("json_schema", "auto"), ("json_object", "none")],
    ids=["schema-none", "schema-auto", "object-none"],
)
def test_response_format_holds_when_tools_are_present(ep, samples, concurrency, effort, max_tokens, kind, choice):
    """Guide 3.4: declaring tools must not switch response_format off (observed: 2/9 conforming with tools
    vs 9/9 without), and json_object with thinking must not start with an empty key (`{"": ...}`)."""
    response_format = (
        {"type": "json_schema", "json_schema": {"name": "summary", "strict": True, "schema": SUMMARY_SCHEMA}}
        if kind == "json_schema" else {"type": "json_object"}
    )

    def one(_: int) -> tc.Verdict:
        payload = ep.request(JSON_PROMPT, tools=[WEATHER], tool_choice=choice, thinking=True, effort=effort,
                             max_tokens=max_tokens, response_format=response_format)
        reply = ep.call(payload)
        verdict = tc.judge_reply(reply, {"get_weather"})
        if verdict.problems or verdict.inconclusive:
            return verdict
        if reply.tool_calls:
            if choice == "none":
                verdict.problems.append("tool_choice=none but tool_calls were returned")
            else:
                verdict.inconclusive = True  # the model chose a tool; response_format not applicable
            return verdict
        if kind == "json_schema":
            reason = tc.json_conforms(reply.content, SUMMARY_SCHEMA)
        else:
            try:
                value = json.loads(reply.content)
                reason = None if isinstance(value, dict) and value and "" not in value else f"bad json_object: {reply.content[:120]!r}"
            except ValueError:
                reason = f"content is not JSON: {reply.content[:120]!r}"
        if reason:
            verdict.problems.append(reason)
        return verdict

    fail_unless_clean(tc.summarize(tc.run_samples(samples, one, concurrency)))


# ------------------------------------------------------------------ 6. observations (never fail)


def test_observe_parallel_tool_calls(ep, samples, concurrency, max_tokens, observe):
    """OBSERVE. Guide 3.6 says parallel_tool_calls=false keeps only the first call. That rule is vllm-int's
    own choice, not a Moonshot contract, so it is reported rather than enforced."""
    prompt = [{"role": "user", "content": "请同时查询北京和上海的天气。"}]
    result: dict[str, Any] = {"samples": samples}
    for flag in (True, False):
        replies = tc.run_samples(
            samples,
            lambda _: ep.call(ep.request(prompt, tools=[WEATHER], tool_choice="required", max_tokens=max_tokens,
                                         parallel_tool_calls=flag)),
            concurrency,
        )
        assert all(r.ok for r in replies), next(r for r in replies if not r.ok).describe()
        result[f"calls_per_response_parallel_{str(flag).lower()}"] = [len(r.tool_calls) for r in replies]
    result["parallel_false_returned_multiple_calls"] = any(n > 1 for n in result["calls_per_response_parallel_false"])
    result["parallel_true_exercised"] = any(n > 1 for n in result["calls_per_response_parallel_true"])
    observe("parallel_tool_calls", result)


def test_observe_marker_text_in_tool_arguments(ep, samples, concurrency, max_tokens, observe):
    """OBSERVE. Known-open defect (guide 3.6): a text-level parser cannot tell a real `<|close|>` token from the
    same characters typed by the model, so arguments get cut at the marker text. Reported, not enforced."""
    wanted = "<|close|>argument<|sep|>"
    prompt = [{"role": "user", "content": f"调用 write_file：path 为 a.txt，content 参数必须逐字等于下面这段文本，不要改动：\n{wanted}"}]
    tools = [with_strict(WRITE_FILE, False)]
    replies = tc.run_samples(
        samples,
        lambda _: ep.call(ep.request(prompt, tools=tools, tool_choice="required", max_tokens=max_tokens)),
        concurrency,
    )
    assert all(r.ok for r in replies), next(r for r in replies if not r.ok).describe()
    outcomes = []
    for reply in replies:
        call = reply.tool_calls[0] if reply.tool_calls else None
        try:
            args = json.loads(call["arguments"]) if call else None
        except (TypeError, ValueError):
            args = None
        outcomes.append("no_call" if call is None else "arguments_not_json" if args is None
                        else "marker_preserved" if wanted in str(args.get("content", "")) else "marker_missing")
    observe("marker_text_in_tool_arguments", {"samples": samples, "outcomes": outcomes,
                                              "truncated_or_invalid": outcomes.count("arguments_not_json")})
