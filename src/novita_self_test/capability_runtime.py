"""Bounded, replayable HTTP observations; no benchmark runtime or hidden retries."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from .case_catalog import iter_case_steps, read_request
from .config import ConfigError, ServiceAccess, canonical_hash, redact, results_root, workspace_root

MARKERS = ("<|open|>", "<|close|>", "<|sep|>", "<|end_of_msg|>")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
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


def strict_json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON constant: {value}")
    return json.loads(text, parse_constant=reject_constant)


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
    for line in text.splitlines():
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


def parse_reply(status: int, text: str, *, stream: bool, headers: dict[str, str] | None = None) -> Reply:
    reply = Reply(status, text, headers=headers or {})
    if status != 200:
        return reply
    try:
        if not stream:
            body = _object(strict_json(text), "response")
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


def assert_reply(reply: Reply, expect: dict[str, Any], previous: list[Reply], *, mode: str | None = None) -> list[str]:
    problems = list(reply.errors)
    if reply.status != expect["status"]:
        return problems + [f"status={reply.status}, expected={expect['status']}"]
    if expect.get("error_object"):
        try:
            error = strict_json(reply.raw)["error"]
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                problems.append("error must contain a string message")
        except (ValueError, TypeError, KeyError):
            problems.append("missing JSON error object")
    if reply.status != 200:
        return problems
    if reply.finish_reason == "length" and not expect.get("allow_truncation"):
        return problems + ["output truncated: contract is inconclusive"]
    if expect.get("finish_reason") and reply.finish_reason != expect["finish_reason"]:
        problems.append(f"finish_reason={reply.finish_reason!r}, expected={expect['finish_reason']!r}")
    if expect.get("public_turn") and not (reply.content.strip() or reply.calls):
        problems.append("empty public turn")
    if expect.get("tools") == "none" and reply.calls:
        problems.append("tool_choice none returned calls")
    if expect.get("tools") == "required" and not reply.calls:
        problems.append("required tool call missing")
    if "max_calls" in expect and len(reply.calls) > expect["max_calls"]:
        problems.append("too many tool calls")
    if "reasoning" in expect and bool(reply.reasoning) != expect["reasoning"]:
        problems.append("reasoning presence differs from contract")
    if expect.get("stream_shape") and not reply.chunks:
        problems.append("stream response has no parsed chunks")
    if expect.get("no_marker_leak") and any(marker in reply.content for marker in MARKERS):
        problems.append("structural marker leaked into content")
    for value in expect.get("content_contains", []):
        if value not in reply.content:
            problems.append(f"content missing {value!r}")
    ids = [call.get("id") for call in reply.calls]
    if any(not call_id for call_id in ids) or len(set(ids)) != len(ids):
        problems.append("tool call ids are empty or duplicated")
    if reply.calls and reply.finish_reason != "tool_calls":
        problems.append("calls require finish_reason=tool_calls")
    if not reply.calls and reply.finish_reason == "tool_calls":
        problems.append("finish_reason=tool_calls without calls")
    for call in reply.calls:
        if not call.get("name"):
            problems.append("empty tool name")
        if "tool_names" in expect and call.get("name") not in expect["tool_names"]:
            problems.append(f"undeclared/unexpected tool {call.get('name')!r}")
    calls = reply.calls[:1] if expect.get("first_call_only") else reply.calls
    for call in calls:
        if expect.get("arguments_nonempty") and not call.get("arguments"):
            problems.append("tool-call arguments are empty")
        if expect.get("arguments_json") or "arguments_schema" in expect:
            try:
                if not isinstance(call["arguments"], str) or not call["arguments"]:
                    raise ValueError("missing JSON arguments")
                value = strict_json(call["arguments"])
                if "arguments_schema" in expect:
                    problems.extend("arguments schema: " + error.message for error in Draft202012Validator(expect["arguments_schema"]).iter_errors(value))
            except (ValueError, TypeError):
                problems.append("arguments are not a nonempty valid JSON string")
        if "forbidden_arguments" in expect and call["arguments"] == expect["forbidden_arguments"]:
            problems.append("arguments repeat forbidden history")
        if "id_min_suffix" in expect:
            match = re.fullmatch(r".+_(\d+)", call.get("id") or "")
            if not match or int(match.group(1)) < expect["id_min_suffix"]:
                problems.append("tool id suffix did not continue history")
    if expect.get("content_json") or "content_schema" in expect or expect.get("no_empty_key"):
        try:
            value = strict_json(reply.content)
            if expect.get("content_json") == "object" and not isinstance(value, dict):
                problems.append("content is not a JSON object")
            if expect.get("no_empty_key") and isinstance(value, dict) and "" in value:
                problems.append("empty-key regression")
            if "content_schema" in expect:
                problems.extend("content schema: " + error.message for error in Draft202012Validator(expect["content_schema"]).iter_errors(value))
        except (ValueError, TypeError):
            problems.append("content is not valid JSON")
    if any(key in expect for key in ("usage", "usage_details", "max_completion_tokens", "prompt_tokens", "same_prompt_as", "minimum_prompt_delta")):
        usage = reply.usage or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if type(usage.get(key)) is not int or usage[key] < 0:
                problems.append(f"usage.{key} missing or not a nonnegative integer")
        if all(type(usage.get(key)) is int for key in ("prompt_tokens", "completion_tokens", "total_tokens")) and usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            problems.append("usage total != prompt + completion")
        if "max_completion_tokens" in expect and type(usage.get("completion_tokens")) is int and usage["completion_tokens"] > expect["max_completion_tokens"]:
            problems.append("completion usage exceeds selected output cap")
        if "prompt_tokens" in expect and usage.get("prompt_tokens") != expect["prompt_tokens"]:
            problems.append(f"prompt_tokens={usage.get('prompt_tokens')}, expected={expect['prompt_tokens']}")
        if expect.get("usage_details"):
            for group, key, total in (("prompt_tokens_details", "cached_tokens", "prompt_tokens"),
                                      ("completion_tokens_details", "reasoning_tokens", "completion_tokens")):
                details = usage.get(group)
                value = details.get(key) if isinstance(details, dict) else None
                if type(value) is not int or value < 0 or (type(usage.get(total)) is int and value > usage[total]):
                    problems.append(f"usage.{group}.{key} missing or out of bounds")
        if "same_prompt_as" in expect:
            index = expect["same_prompt_as"]
            if index >= len(previous) or previous[index].status != 200 or type((previous[index].usage or {}).get("prompt_tokens")) is not int:
                problems.append("prompt comparison unavailable: previous step lacks valid usage")
            elif (previous[index].usage or {})["prompt_tokens"] != usage.get("prompt_tokens"):
                problems.append("equivalent requests have different prompt_tokens")
        if "minimum_prompt_delta" in expect:
            before = (previous[-1].usage or {}).get("prompt_tokens") if previous else None
            after = usage.get("prompt_tokens")
            if type(before) is not int or type(after) is not int or after - before < expect["minimum_prompt_delta"]:
                problems.append(f"prompt token delta below {expect['minimum_prompt_delta']}")
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
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                    async with client.stream("POST", self.service.base_url + "/chat/completions", json=request,
                                             headers={**self.service.headers, "Authorization": f"Bearer {self.service.credential()}"}) as response:
                        status = response.status_code
                        headers = safe_response_headers(dict(response.headers))
                        async for chunk in response.aiter_bytes():
                            remaining = MAX_RESPONSE_BYTES - len(data)
                            data.extend(chunk[:remaining])
                            if len(chunk) > remaining:
                                transport_error = "transport failure: response byte limit exceeded"
                                break
        except (httpx.HTTPError, TimeoutError) as exc:
            transport_error = f"transport failure: {type(exc).__name__}"
        reply = parse_reply(status, data.decode("utf-8", errors="replace"), stream=bool(request.get("stream")), headers=headers) if status is not None else Reply(None)
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
        enabled, effort = step["thinking"], step.get("effort")
        if service.think_mode == "opensource":
            request["reasoning_effort"] = (effort or "max") if enabled else "none"
        else:
            request["thinking"] = {"type": "enabled", "keep": "all", **({"effort": effort} if effort else {})} if enabled else {"type": "disabled"}
    request["stream"] = mode == "stream"
    if request["stream"]:
        request["stream_options"] = {**request.get("stream_options", {}), "include_usage": True}
    return request


def _outcome(reply: Reply, expect: dict[str, Any], problems: list[str]) -> tuple[str, str | None]:
    failure = service_failure(reply.status, reply.raw, reply.errors)
    if failure in {"service_invalid", "quota_exhausted"}:
        return "service_invalid", failure
    if failure:
        # Observing a server crash instead of a contractually expected 4xx is
        # a failed robustness contract, not a model-quality failure.
        if failure == "service_unavailable" and 400 <= expect["status"] < 500:
            return "failed", failure
        return "execution_invalid", failure
    if reply.incomplete or (reply.finish_reason == "length" and not expect.get("allow_truncation") and expect.get("finish_reason") != "length"):
        return "inconclusive", None
    return ("failed" if problems else "passed"), None


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
    units = [(case, *unit) for case in plan["cases"] if not case["missing_requirements"]
             for unit in iter_case_steps(case, plan["repeat"])]
    expected_count = len(units)
    if expected_count != plan["request_budget"]:
        raise ConfigError("plan_invalid", "request budget differs from executable plan")
    if expected_count > max_requests:
        raise ConfigError("request_budget_exceeded", f"plan needs {expected_count} requests; ceiling is {max_requests}")
    token_parameter()  # Configuration errors must fail before endpoint traffic.
    if expected_count and not service.credential():
        raise ConfigError("credential_missing", f"set {service.api_key_env}")
    # Resolve all fixtures before sending the first request, freezing the exact
    # request bytes represented by this run (not a later-mutated fixture).
    prepared = []
    for case, repeat, group, index, mode, step in units:
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
        prepared.append((case, repeat, group, index, mode, step, request, expect))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = results_root(root) / plan["model"] / f"{stamp}-{os.urandom(4).hex()}"
    directory.mkdir(parents=True, exist_ok=False)
    credential = service.credential()

    def save(name: str, value: Any) -> None:
        (directory / name).write_text(json.dumps(redact(value, credential), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save("plan.json", plan)
    save("service-access.json", service.public_dict())
    results = [{"id": case["id"], "capability": case["capability"], "status": "unsupported",
                "problems": case["missing_requirements"], "knowledge": case["knowledge"],
                "likely_components": case["likely_components"], "source": case["source"]}
               for case in plan["cases"] if case["missing_requirements"]]
    client, actual_requests, stop_reason = CapabilityClient(service, timeout=timeout), 0, None
    histories: dict[tuple[str, int, int], list[Reply]] = {}
    for case, repeat, group, index, mode, step, request, expect in prepared:
        result = {"id": case["id"], "capability": case["capability"], "repeat": repeat,
                  "mode": mode, "step": index, "problems": [], "likely_components": case["likely_components"],
                  "knowledge": case["knowledge"], "source": case["source"]}
        evidence_name = f"{case['id']}-{repeat}-{group}-{mode}-{index}.json"
        if stop_reason or actual_requests >= max_requests:
            stop_reason = stop_reason or "request_budget_exceeded"
            result.update(status="not_run", problems=[f"stopped after {stop_reason}"])
            results.append(result)
            continue
        reply = None
        history = histories.setdefault((case["id"], repeat, group), [])
        try:
            actual_requests += 1
            reply = client.call(request)
            problems = assert_reply(reply, expect, history, mode=mode)
            status, failure = _outcome(reply, expect, problems)
            if status == "passed" and case.get("determinism") == "observation":
                status = "observed"
            history.append(reply)
            result.update(status=status, problems=problems, failure_class=failure)
            if failure:
                stop_reason = failure
        except Exception as exc:
            stop_reason = "runner_error"
            result.update(status="execution_invalid", failure_class="runner_error",
                          problems=[f"runner error: {type(exc).__name__}: {exc}"])
        result["evidence"] = evidence_name
        save(evidence_name, {"request": request, "response": asdict(reply) if reply is not None else None,
                             "expect": expect, "result": result, "request_sha256": canonical_hash(request)})
        results.append(result)
    statuses = {result["status"] for result in results}
    status = next((value for value in ("service_invalid", "execution_invalid", "failed") if value in statuses), None)
    status = status or ("incomplete" if statuses & {"inconclusive", "unsupported", "not_run"} else "observed" if "observed" in statuses else "passed")
    report = redact({"schema_version": 1, "status": status, "model": plan["model"], "run_dir": str(directory),
                     "stop_reason": stop_reason, "planned_requests": expected_count, "actual_requests": actual_requests,
                     "results": results, "disclaimer": "Capability regression evidence; not a model-quality benchmark or vendor acceptance."}, credential)
    save("report.json", report)
    lines = ["# Kimi capability regression", "", f"Status: **{status}**", f"Requests: {actual_requests}/{expected_count}", "", report["disclaimer"], ""]
    for result in report["results"]:
        lines.append(f"- {result['id']} ({result.get('mode', 'not executed')}): **{result['status']}**")
        lines.extend(f"  - {problem}" for problem in result["problems"])
        if result.get("evidence"):
            lines.append(f"  - [evidence]({result['evidence']})")
        lines.append("  - Components: " + ", ".join(result["likely_components"]))
        lines.append("  - Knowledge: " + ", ".join(result["knowledge"]))
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
