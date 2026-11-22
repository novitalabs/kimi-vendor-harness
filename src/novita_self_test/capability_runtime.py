"""Bounded, replayable HTTP observations; no benchmark runtime or hidden retries."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from .case_catalog import iter_case_steps, read_request
from .config import ConfigError, ServiceAccess, canonical_hash, redact, results_root, strict_json, workspace_root
from .contracts import issue, oracle_validator, validate_contract
from .target_contract import gate_summary
from .scenario_binding import bind_request
from .report_comparison import scenario_identity

MARKERS = ("<|open|>", "<|close|>", "<|sep|>", "<|end_of_msg|>")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter"}
INCOMPLETE_ERRORS = {"missing finish_reason", "missing DONE marker", "expected one finish frame, observed 0", "empty SSE stream"}
SAFE_HEADERS = {
    "content-type", "date", "server", "traceparent", "x-request-id", "x-trace-id",
    "msh-request-id", "x-msh-trace-id", "x-moongate-request-id", "x-msh-providerdeployid",
    "x-msh-usage-prompt-tokens", "x-msh-usage-cached-tokens",
}


def safe_response_headers(headers: dict[str, str]) -> dict[str, str]:
    return {key.lower(): value for key, value in headers.items() if key.lower() in SAFE_HEADERS}


def token_parameter() -> str:
    value = os.environ.get("NVST_TOKEN_PARAM", os.environ.get("KVV_TOKEN_PARAM", "max_tokens"))
    if value not in {"max_tokens", "max_completion_tokens"}:
        raise ConfigError("token_parameter_invalid", "use max_tokens or max_completion_tokens")
    return value


def service_failure(status: int | None, raw: str = "", errors: list[str] | None = None) -> str | None:
    if status in {401, 403, 407}:
        return "service_invalid"
    # Never interpret model-generated text as authentication/quota metadata.
    if status is not None and status != 200:
        try:
            error = json.loads(raw).get("error", {})
        except (ValueError, AttributeError):
            error = {}
        if isinstance(error, dict):
            text = " ".join(str(error.get(key, "")) for key in ("code", "type", "message")).lower()
            if re.search(r"insufficient[_ ](?:quota|credits|balance)|quota[_ ]exhausted|not enough (?:budget|balance)|billing quota", text):
                return "quota_exhausted"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "service_unavailable"
    if status is None or any(error.startswith("transport failure:") for error in errors or []):
        return "execution_invalid"
    return None


@dataclass
class Reply:
    status: int | None
    raw: str = ""
    content: str = ""
    reasoning: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    chunks: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    incomplete: bool = False
    errors: list[str] = field(default_factory=list)
    raw_bytes_sha256: str | None = None
    invalid_utf8: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)


def _text(value: Any, label: str, errors: list[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        errors.append(f"response shape: {label} must be a string")
        return ""
    return value


def _list(value: Any, label: str, *, optional: bool = False) -> list:
    if value is None and optional:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _object(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def usage_of(body: dict[str, Any]) -> Any:
    if body.get("usage") is not None:
        return body["usage"]
    for choice in body.get("choices", []):
        if isinstance(choice, dict) and choice.get("usage") is not None:
            return choice["usage"]
    return None


def _capture_usage(reply: Reply, body: dict) -> None:
    usage = usage_of(body)
    if usage is not None:
        reply.usage = _object(usage, "usage")


def _sse_payloads(text: str) -> list[str]:
    events, parts = [], []
    for line in re.split(r"\r\n|\r|\n", text):
        if line == "":
            if parts:
                events.append("\n".join(parts))
                parts = []
        elif line.startswith("data:"):
            parts.append(line[5:].removeprefix(" "))
        elif line == "data":
            parts.append("")
    if parts:
        events.append("\n".join(parts))
    return events


def _envelope(body: dict, *, stream: bool, errors: list[str]) -> None:
    for key in ("id", "model"):
        if not isinstance(body.get(key), str) or not body[key]:
            errors.append(f"response envelope: {key} must be a nonempty string")
    if body.get("object") != ("chat.completion.chunk" if stream else "chat.completion"):
        errors.append("response envelope: unexpected object type")
    if type(body.get("created")) is not int or body["created"] < 0:
        errors.append("response envelope: created must be a nonnegative integer")


def parse_reply(status: int, text: str, *, stream: bool, headers: dict[str, str] | None = None) -> Reply:
    reply = Reply(status, text, headers=headers or {})
    if status != 200:
        return reply
    try:
        if not stream:
            body = _object(strict_json(text), "response")
            _envelope(body, stream=False, errors=reply.errors)
            choices = _list(body.get("choices"), "choices")
            if len(choices) != 1:
                raise ValueError("expected exactly one choice")
            choice = _object(choices[0], "choice")
            if type(choice.get("index")) is not int or choice["index"] != 0:
                raise ValueError("choice index must be zero")
            message = _object(choice.get("message"), "message")
            if message.get("role") != "assistant":
                reply.errors.append("message.role must be assistant")
            reply.content = _text(message.get("content"), "content", reply.errors)
            reply.reasoning = _text(message.get("reasoning_content"), "reasoning_content", reply.errors)
            for index, call in enumerate(_list(message.get("tool_calls"), "tool_calls", optional=True)):
                call = _object(call, "tool call")
                function = _object(call.get("function"), "tool function")
                if call.get("type") != "function":
                    reply.errors.append("tool_call.type must be function")
                reply.calls.append({"id": _text(call.get("id"), "tool id", reply.errors),
                                    "name": _text(function.get("name"), "tool name", reply.errors),
                                    "arguments": _text(function.get("arguments"), "arguments", reply.errors),
                                    "index": index})
            reply.finish_reason = _text(choice.get("finish_reason"), "finish_reason", reply.errors) or None
            if reply.finish_reason is None:
                reply.errors.append("missing finish_reason")
                reply.incomplete = True
            _capture_usage(reply, body)
            return reply
        if headers is not None and "text/event-stream" not in reply.headers.get("content-type", "").lower():
            reply.errors.append("stream Content-Type must be text/event-stream")
        slots: dict[int, dict[str, Any]] = {}
        finish_count, summary_count, choice_count = 0, 0, 0
        constants: dict[str, Any] = {}
        for payload in _sse_payloads(text):
            if reply.done:
                reply.errors.append("data after DONE")
            if payload.strip() == "[DONE]":
                reply.done = True
                if not finish_count:
                    reply.errors.append("DONE before finish frame")
                continue
            chunk = _object(strict_json(payload), "chunk")
            _envelope(chunk, stream=True, errors=reply.errors)
            reply.chunks.append(chunk)
            for key in ("id", "model", "created"):
                if key in chunk:
                    if key in constants and chunk[key] != constants[key]:
                        reply.errors.append(f"stream {key} changed")
                    constants[key] = chunk[key]
            choices = _list(chunk.get("choices"), "chunk choices")
            _capture_usage(reply, chunk)
            if not choices:
                summary_count += 1
                if not finish_count or usage_of(chunk) is None:
                    reply.errors.append("usage summary must follow finish and contain usage")
                if summary_count > 1:
                    reply.errors.append("duplicate usage summary")
                continue
            if len(choices) != 1:
                reply.errors.append("expected one stream choice")
            for choice in choices:
                choice = _object(choice, "choice")
                if type(choice.get("index")) is not int or choice["index"] != 0:
                    reply.errors.append("choice index must be zero")
                    continue
                if finish_count:
                    reply.errors.append("choice data after finish frame")
                delta = _object(choice.get("delta"), "delta")
                if not choice_count and delta.get("role") != "assistant":
                    reply.errors.append("first delta.role must be assistant")
                if choice_count and "role" in delta:
                    reply.errors.append("role repeated after first delta")
                choice_count += 1
                reply.content += _text(delta.get("content"), "delta.content", reply.errors)
                reply.reasoning += _text(delta.get("reasoning_content"), "delta.reasoning_content", reply.errors)
                if sum(bool(delta.get(key)) for key in ("content", "reasoning_content", "tool_calls")) > 1:
                    reply.errors.append("stream channels overlap in one delta")
                if "finish_reason" not in choice:
                    reply.errors.append("stream choice missing finish_reason")
                if choice.get("finish_reason") is not None:
                    finish_count += 1
                    reply.finish_reason = _text(choice["finish_reason"], "finish_reason", reply.errors) or None
                    if delta:
                        reply.errors.append("finish delta must be empty")
                for call in _list(delta.get("tool_calls"), "tool_calls", optional=True):
                    call = _object(call, "tool delta")
                    index = call.get("index")
                    if type(index) is not int or index < 0:
                        reply.errors.append("tool delta missing nonnegative integer index")
                        continue
                    first = index not in slots
                    slot = slots.setdefault(index, {"id": "", "name": "", "arguments": "", "index": index})
                    function = _object(call.get("function"), "tool function")
                    name = _text(function.get("name"), "tool name", reply.errors)
                    call_id = _text(call.get("id"), "tool id", reply.errors)
                    arguments = _text(function.get("arguments"), "arguments", reply.errors)
                    if "arguments" not in function or not isinstance(function["arguments"], str):
                        reply.errors.append("tool delta arguments must be a string")
                    if first:
                        if not name or not call_id or call.get("type") != "function":
                            reply.errors.append(f"first tool delta {index} needs id, function type and name")
                        if arguments:
                            reply.errors.append("first tool delta arguments must be empty")
                        slot.update(id=call_id, name=name)
                    else:
                        if call_id and call_id != slot["id"]:
                            reply.errors.append("tool call id changed")
                        if name and name != slot["name"]:
                            reply.errors.append("tool call name changed")
                        if set(call) - {"index", "function"} or set(function) - {"arguments"}:
                            reply.errors.append("tool continuation must contain only index and arguments")
                    slot["arguments"] += arguments
        reply.calls = [slots[index] for index in sorted(slots)]
        if finish_count != 1:
            reply.errors.append(f"expected one finish frame, observed {finish_count}")
        if not reply.done:
            reply.errors.append("missing DONE marker")
        if not choice_count:
            reply.errors.append("empty SSE stream")
        reply.incomplete = finish_count == 0 or not reply.done
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        reply.errors.append(f"malformed response: {type(exc).__name__}: {exc}")
    return reply


def assert_reply(reply: Reply, expect: dict[str, Any], previous: list[Reply]) -> list[dict[str, Any]]:
    problems = [issue("response_incomplete" if error in INCOMPLETE_ERRORS else "response_invalid", error) for error in reply.errors]
    def add(code: str, message: str, expected: Any = None, actual: Any = None, path=None) -> None:
        problems.append(issue(code, message, expected=expected, actual=actual, path=path))
    if reply.status != expect["status"]:
        add("http_status_mismatch", "HTTP status differs from contract", expect["status"], reply.status)
        return problems
    if expect.get("error_object"):
        try:
            error = strict_json(reply.raw)["error"]
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                add("error_envelope_invalid", "error must contain a string message")
        except (ValueError, TypeError, KeyError):
            add("error_envelope_invalid", "missing JSON error object")
    if reply.status != 200:
        return problems
    if reply.finish_reason is not None and reply.finish_reason not in FINISH_REASONS:
        add("finish_reason_invalid", "finish reason is outside the selected protocol", sorted(FINISH_REASONS), reply.finish_reason)
    if reply.finish_reason == "length" and not expect.get("allow_truncation"):
        add("output_truncated", "output truncated: generation-dependent contract is inconclusive")
    uncertain = reply.finish_reason is None or (reply.finish_reason == "length" and not expect.get("allow_truncation")) or (reply.finish_reason == "content_filter" and expect.get("finish_reason") != "content_filter")
    if reply.finish_reason == "content_filter" and expect.get("finish_reason") != "content_filter":
        add("generation_filtered", "generation was filtered; task contract is inconclusive")
    if expect.get("no_marker_leak") and any(marker in reply.content for marker in MARKERS):
        add("marker_leaked", "structural marker leaked into content")
    if expect.get("reasoning") is False and reply.reasoning:
        add("reasoning_presence_mismatch", "disabled reasoning was emitted", False, True)
    if expect.get("tools") == "none" and reply.calls:
        add("tool_call_forbidden", "tool_choice none returned calls", 0, len(reply.calls))
    for index, call in enumerate(reply.calls):
        if "tool_names" in expect and call.get("name") not in expect["tool_names"]:
            add("tool_name_undeclared", "undeclared/unexpected tool", expect["tool_names"], call.get("name"), ["calls", index, "name"])
    # Preserve already observable wire failures; don't judge unfinished JSON/turns.
    if uncertain:
        return problems
    if expect.get("finish_reason") and reply.finish_reason != expect["finish_reason"]:
        add("finish_reason_mismatch", "finish reason differs", expect["finish_reason"], reply.finish_reason)
    if expect.get("public_turn") and not (reply.content.strip() or reply.calls):
        add("public_turn_empty", "empty public turn")
    if expect.get("tools") == "required" and not reply.calls:
        add("tool_call_missing", "required tool call missing")
    if "max_calls" in expect and len(reply.calls) > expect["max_calls"]:
        add("tool_call_count_exceeded", "too many tool calls", expect["max_calls"], len(reply.calls))
    if expect.get("reasoning") is True and not reply.reasoning:
        add("reasoning_presence_mismatch", "reasoning presence differs from contract", True, False)
    if expect.get("stream_shape") and not reply.chunks:
        add("stream_empty", "stream response has no parsed chunks")
    for value in expect.get("content_contains", []):
        if value not in reply.content:
            add("content_literal_missing", "content missing requested literal", value, reply.content)
    ids = [call.get("id") for call in reply.calls]
    if any(not call_id for call_id in ids) or len(set(ids)) != len(ids):
        add("tool_id_invalid", "tool call ids are empty or duplicated", "unique nonempty ids", ids)
    if bool(reply.calls) != (reply.finish_reason == "tool_calls"):
        add("tool_finish_mismatch", "calls and tool_calls finish reason disagree")
    for index, call in enumerate(reply.calls):
        if not call.get("name"):
            add("tool_name_empty", "empty tool name", actual=call.get("name"), path=["calls", index, "name"])
    for index, call in enumerate(reply.calls):
        selected = index == 0 or not expect.get("first_call_only")
        try:
            if not isinstance(call["arguments"], str) or not call["arguments"]:
                raise ValueError("missing JSON arguments")
            value = strict_json(call["arguments"])
            if not isinstance(value, dict):
                add("arguments_object_required", "generated tool arguments must be a JSON object", "object", value)
            if selected and "arguments_schema" in expect:
                for error in oracle_validator(expect["arguments_schema"]).iter_errors(value):
                    add("arguments_schema_mismatch", error.message, error.validator_value, error.instance,
                        ["calls", index, "arguments", *error.absolute_path])
        except (ValueError, TypeError):
            add("arguments_json_invalid", "arguments are not a nonempty valid JSON string", actual=call.get("arguments"))
        if selected and "forbidden_arguments" in expect and call["arguments"] == expect["forbidden_arguments"]:
            add("arguments_history_repeated", "arguments repeat forbidden history")
        if selected and "id_min_suffix" in expect:
            match = re.fullmatch(r".+_(\d+)", call.get("id") or "")
            if not match or int(match.group(1)) < expect["id_min_suffix"]:
                add("tool_id_history_mismatch", "tool id suffix did not continue history", expect["id_min_suffix"], call.get("id"))
    if expect.get("content_json") or "content_schema" in expect or expect.get("no_empty_key"):
        try:
            value = strict_json(reply.content)
            if expect.get("content_json") == "object" and not isinstance(value, dict):
                add("content_object_required", "content is not a JSON object", "object", value)
            if expect.get("no_empty_key") and isinstance(value, dict) and "" in value:
                add("content_empty_key", "empty-key regression")
            if "content_schema" in expect:
                for error in oracle_validator(expect["content_schema"]).iter_errors(value):
                    add("content_schema_mismatch", error.message, error.validator_value, error.instance, ["content", *error.absolute_path])
        except (ValueError, TypeError):
            add("content_json_invalid", "content is not valid JSON", actual=reply.content)
    if any(key in expect for key in ("usage", "usage_details", "max_completion_tokens", "prompt_tokens", "same_prompt_as", "minimum_prompt_delta")):
        usage = reply.usage or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if type(usage.get(key)) is not int or usage[key] < 0:
                add("usage_value_invalid", f"usage.{key} missing or not a nonnegative integer", "nonnegative integer", usage.get(key), ["usage", key])
        if all(type(usage.get(key)) is int for key in ("prompt_tokens", "completion_tokens", "total_tokens")) and usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            add("usage_total_mismatch", "usage total != prompt + completion")
        if "max_completion_tokens" in expect and type(usage.get("completion_tokens")) is int and usage["completion_tokens"] > expect["max_completion_tokens"]:
            add("completion_cap_exceeded", "completion usage exceeds selected output cap", expect["max_completion_tokens"], usage["completion_tokens"])
        if "prompt_tokens" in expect and usage.get("prompt_tokens") != expect["prompt_tokens"]:
            add("prompt_tokens_mismatch", "prompt token count differs", expect["prompt_tokens"], usage.get("prompt_tokens"))
        if expect.get("usage_details"):
            for group, key, total in (("prompt_tokens_details", "cached_tokens", "prompt_tokens"),
                                      ("completion_tokens_details", "reasoning_tokens", "completion_tokens")):
                details = usage.get(group)
                value = details.get(key) if isinstance(details, dict) else None
                if type(value) is not int or value < 0 or (type(usage.get(total)) is int and value > usage[total]):
                    add("usage_details_invalid", f"usage.{group}.{key} missing or out of bounds", actual=value, path=["usage", group, key])
        if "same_prompt_as" in expect:
            index = expect["same_prompt_as"]
            if index >= len(previous) or previous[index].status != 200 or type((previous[index].usage or {}).get("prompt_tokens")) is not int:
                add("prompt_comparison_unavailable", "previous step lacks valid usage")
            elif (previous[index].usage or {})["prompt_tokens"] != usage.get("prompt_tokens"):
                add("prompt_parity_mismatch", "equivalent requests have different prompt_tokens", previous[index].usage["prompt_tokens"], usage.get("prompt_tokens"))
        if "minimum_prompt_delta" in expect:
            before = (previous[-1].usage or {}).get("prompt_tokens") if previous else None
            after = usage.get("prompt_tokens")
            if type(before) is not int or type(after) is not int or after - before < expect["minimum_prompt_delta"]:
                add("prompt_delta_mismatch", "prompt token delta below minimum", expect["minimum_prompt_delta"], None if type(before) is not int or type(after) is not int else after - before)
    return problems


class CapabilityClient:
    def __init__(self, service: ServiceAccess, *, timeout: float = 120):
        self.service = service
        self.timeout = timeout

    def call(self, request: dict[str, Any]) -> Reply:
        return asyncio.run(self._call(request))

    async def _call(self, request: dict[str, Any]) -> Reply:
        status, headers, data = None, {}, bytearray()
        transport_error = None
        started = datetime.now(timezone.utc).isoformat()
        start_clock, first_byte = time.monotonic(), None
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                    async with client.stream("POST", self.service.base_url + "/chat/completions", json=request,
                                             headers={**self.service.headers, "Authorization": f"Bearer {self.service.credential()}"}) as response:
                        status = response.status_code
                        headers = safe_response_headers(dict(response.headers))
                        async for chunk in response.aiter_bytes():
                            if first_byte is None:
                                first_byte = time.monotonic() - start_clock
                            remaining = MAX_RESPONSE_BYTES - len(data)
                            data.extend(chunk[:remaining])
                            if len(chunk) > remaining:
                                transport_error = "transport failure: response byte limit exceeded"
                                break
        except (httpx.HTTPError, TimeoutError) as exc:
            transport_error = f"transport failure: {type(exc).__name__}"
        invalid_utf8 = []
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            invalid_utf8.append({"start": exc.start, "end": exc.end, "bytes_hex": bytes(data[exc.start:exc.end]).hex()})
            text = data.decode("utf-8", errors="replace")  # Display only, never a valid wire pass.
        reply = parse_reply(status, text, stream=bool(request.get("stream")), headers=headers) if status is not None else Reply(None)
        reply.raw_bytes_sha256 = hashlib.sha256(data).hexdigest()
        reply.invalid_utf8 = invalid_utf8
        reply.timing = {"started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(),
                        "elapsed_seconds": time.monotonic() - start_clock, "first_byte_seconds": first_byte,
                        "received_bytes": len(data)}
        if invalid_utf8:
            reply.errors.append("response encoding: invalid UTF-8 bytes")
        if transport_error:
            reply.errors.append(transport_error)
            reply.incomplete = True
        return reply


def materialize_step(step: dict[str, Any], case: dict[str, Any], service: ServiceAccess,
                     mode: str, root: Path) -> dict[str, Any]:
    request = read_request(step, root)
    if "message_slice" in step:
        request["messages"] = request.get("messages", [])[:step["message_slice"]]
    request["model"] = service.served_model
    request.pop("max_tokens", None)
    request.pop("max_completion_tokens", None)
    request[token_parameter()] = case["max_output_tokens"]
    if "thinking" in step:
        if "thinking" in request or "reasoning_effort" in request:
            raise ConfigError("case_invalid", "canonical thinking cannot be combined with raw thinking fields")
        enabled, effort = step["thinking"], step.get("effort")
        if service.think_mode == "opensource":
            request["reasoning_effort"] = (effort or "max") if enabled else "none"
        else:
            request["thinking"] = {"type": "enabled", "keep": "all", **({"effort": effort} if effort else {})} if enabled else {"type": "disabled"}
    request["stream"] = mode == "stream"
    if request["stream"]:
        request["stream_options"] = {**request.get("stream_options", {}), "include_usage": True}
    return request


def _outcome(reply: Reply, expect: dict[str, Any], problems: list[dict[str, Any]]) -> tuple[str, str | None]:
    failure = service_failure(reply.status, reply.raw, reply.errors)
    if failure in {"service_invalid", "quota_exhausted"}:
        return "service_invalid", failure
    if failure:
        # Observing a server crash instead of a contractually expected 4xx is
        # a failed robustness contract, not a model-quality failure.
        if failure == "service_unavailable" and 400 <= expect["status"] < 500:
            return "failed", failure
        return "execution_invalid", failure
    uncertain = reply.incomplete or (reply.finish_reason == "length" and not expect.get("allow_truncation") and expect.get("finish_reason") != "length") or (reply.finish_reason == "content_filter" and expect.get("finish_reason") != "content_filter")
    if uncertain and not any(problem["code"] not in {"response_incomplete", "output_truncated", "generation_filtered"} for problem in problems):
        return "inconclusive", None
    return ("failed" if problems else "passed"), None


def prepare_steps(plan: dict[str, Any], service: ServiceAccess, root: Path) -> list[tuple]:
    """Resolve requests and expectations once; execution consumes these snapshots."""
    prepared = []
    for case in plan["cases"]:
        if case["missing_requirements"]:
            continue
        for repeat, group, index, mode, step in iter_case_steps(case, plan["repeat"]):
            request = materialize_step(step, case, service, mode, root)
            expect = copy.deepcopy(step["expect"])
            if expect["status"] == 200:
                tools = list(request.get("tools") or [])
                for message in request["messages"]:
                    tools.extend(message.get("tools") or [])
                declared = [tool["function"]["name"] for tool in tools]
                if set(expect.get("tool_names", [])) - set(declared):
                    raise ConfigError("case_invalid", f"{case['id']}: oracle names an undeclared tool")
                expect.setdefault("tool_names", declared)
                if request.get("tool_choice") in ("required", "none"):
                    expect.setdefault("tools", request["tool_choice"])
            prepared.append((case, repeat, group, index, mode, step, request, expect))
    return prepared


def run_plan(plan: dict[str, Any], service: ServiceAccess, *, max_requests: int,
             root: Path | None = None, timeout: float = 120, allow_opt_in: bool = False) -> dict[str, Any]:
    root = root or workspace_root()
    if type(max_requests) is not int or max_requests < 0:
        raise ConfigError("request_budget_invalid", "max_requests must be a nonnegative integer")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError("timeout_invalid", "timeout must be finite and positive")
    if plan["model"] != service.model_family:
        raise ConfigError("model_mismatch", "service model_family does not match plan")
    opt_in = [case["id"] for case in plan["cases"] if "opt_in" in case.get("tags", []) and not case["missing_requirements"]]
    if opt_in and not allow_opt_in:
        raise ConfigError("opt_in_ack_required", "selected opt-in cases require --allow-opt-in: " + ", ".join(opt_in))
    expected_count = plan["repeat"] * sum(1 for case in plan["cases"] if not case["missing_requirements"]
                                          for _ in iter_case_steps(case, 1))
    if expected_count != plan["request_budget"]:
        raise ConfigError("plan_invalid", "request budget differs from executable plan")
    if expected_count > max_requests:
        raise ConfigError("request_budget_exceeded", f"plan needs {expected_count} requests; ceiling is {max_requests}")
    token_parameter()  # Configuration errors must fail before endpoint traffic.
    if expected_count and not service.credential():
        raise ConfigError("credential_missing", f"set {service.api_key_env}")
    if plan.get("kind") == "endpoint_plan":
        from .execution_plan import verify_plan
        prepared = verify_plan(plan, service, root=root)
        if timeout != plan["execution"]["timeout_seconds"] or max_requests > plan["execution"]["max_requests"]:
            raise ConfigError("plan_override_invalid", "execution limits cannot expand or differ from the frozen plan")
    else:
        prepared = prepare_steps(plan, service, root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = results_root(root) / plan["model"] / f"{stamp}-{os.urandom(4).hex()}"
    directory.mkdir(parents=True, exist_ok=False)
    credential = service.credential()

    def save(name: str, value: Any) -> None:
        (directory / name).write_text(json.dumps(redact(value, credential), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save("plan.json", plan)
    save("service-access.json", service.public_dict())
    results = [{"id": case["id"], "capability": case["capability"], "status": "contract_unknown" if any(value.endswith(":unknown") for value in case["missing_requirements"]) else "unsupported",
                "role": case.get("role", "gate"), "stage": case.get("stage", "protocol"), "obligation": case.get("obligation", "required"),
                "problems": [issue("requirement_missing", "case not applicable", actual=value) for value in case["missing_requirements"]], "knowledge": case["knowledge"],
                "likely_components": case["likely_components"], "source": case["source"]}
               for case in plan["cases"] if case["missing_requirements"]]
    client, actual_requests, stop_reason = CapabilityClient(service, timeout=timeout), 0, None
    histories: dict[tuple[str, int, int], list[Reply]] = {}
    sent_requests: dict[tuple[str, int, int], list[dict]] = {}
    outcomes: dict[tuple[str, int, int], list[str]] = {}
    for case, repeat, group, index, mode, step, request, expect in prepared:
        result = {"id": case["id"], "capability": case["capability"], "repeat": repeat,
                  "group": group, "mode": mode, "step": index, "problems": [], "likely_components": case["likely_components"],
                  "role": case.get("role", "gate"), "stage": case.get("stage", "protocol"), "obligation": case.get("obligation", "required"),
                  "knowledge": case["knowledge"], "source": case["source"]}
        evidence_name = f"{case['id']}-{repeat}-{group}-{mode}-{index}.json"
        if stop_reason or actual_requests >= max_requests:
            stop_reason = stop_reason or "request_budget_exceeded"
            result.update(status="not_run", problems=[issue("execution_stopped", "not run after a previous failure", actual=stop_reason)])
            results.append(result)
            continue
        reply, request_sent = None, False
        template_hash = canonical_hash(request)
        key = (case["id"], repeat, group)
        history = histories.setdefault(key, [])
        requests = sent_requests.setdefault(key, [])
        statuses_before = outcomes.setdefault(key, [])
        try:
            if "continue_from" in step:
                previous_index = step["continue_from"]["step"]
                if previous_index >= len(statuses_before) or statuses_before[previous_index] not in {"passed", "observed"}:
                    raise ConfigError("scenario_dependency_invalid", "previous scenario step did not pass")
                request = bind_request(request, step["continue_from"], history, requests)
            actual_requests += 1
            request_sent = True
            reply = client.call(request)
            problems = assert_reply(reply, expect, history)
            status, failure = _outcome(reply, expect, problems)
            if status == "passed" and case.get("determinism") == "observation":
                status = "observed"
            history.append(reply)
            requests.append(request)
            statuses_before.append(status)
            result.update(status=status, problems=problems, failure_class=failure)
            if failure:
                stop_reason = failure
        except ConfigError as exc:
            # A failed dependency only blocks that turn, not unrelated scenarios.
            if request_sent:
                stop_reason = "runner_error"
            history.append(reply if reply is not None else Reply(None))
            requests.append(request)
            statuses_before.append("execution_invalid" if request_sent else "not_run")
            result.update(status="execution_invalid" if request_sent else "not_run", failure_class=exc.code, problems=[issue(exc.code, str(exc))])
        except Exception as exc:
            stop_reason = "runner_error"
            result.update(status="execution_invalid", failure_class="runner_error",
                          problems=[issue("runner_error", f"runner error: {type(exc).__name__}: {exc}")])
        result["evidence"] = evidence_name
        evidence = redact({"schema_version": 2, "kind": "endpoint_evidence", "request": request,
                           "response": asdict(reply) if reply is not None else None, "expect": expect,
                           "result": result, "request_sha256": canonical_hash(request), "request_sent": request_sent,
                           "template_request_sha256": template_hash, "binding_sha256": canonical_hash(step.get("continue_from"))}, credential)
        validate_contract(evidence, "evidence.schema.json")
        save(evidence_name, evidence)
        results.append(result)
    statuses = {result["status"] for result in results}
    status = next((value for value in ("service_invalid", "execution_invalid", "failed") if value in statuses), None)
    status = status or ("incomplete" if statuses & {"inconclusive", "unsupported", "contract_unknown", "not_run"} else "observed" if "observed" in statuses else "passed")
    report = redact({"schema_version": 2, "kind": "endpoint_report", "status": status, "model": plan["model"], "run_dir": str(directory),
                     "plan_sha256": plan.get("plan_sha256"), "target": service.public_dict(),
                     "gate_summary": gate_summary(plan["cases"], results, plan["repeat"]),
                     "scenario_sha256": {case["id"]: scenario_identity(case, plan) for case in plan["cases"]},
                     "stop_reason": stop_reason, "planned_requests": expected_count, "actual_requests": actual_requests,
                     "results": results, "disclaimer": "Capability regression evidence; not a model-quality benchmark or vendor acceptance."}, credential)
    validate_contract(report, "report.schema.json")
    save("report.json", report)
    lines = ["# Kimi capability regression", "", f"Status: **{status}**", f"Requests: {actual_requests}/{expected_count}", "", report["disclaimer"], ""]
    for result in report["results"]:
        lines.append(f"- {result['id']} ({result.get('mode', 'not executed')}): **{result['status']}**")
        lines.extend(f"  - {problem['code']}: {problem['message']}" for problem in result["problems"])
        if result.get("evidence"):
            lines.append(f"  - [evidence]({result['evidence']})")
        lines.append("  - Components: " + ", ".join(result["likely_components"]))
        lines.append("  - Knowledge: " + ", ".join(result["knowledge"]))
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
