"""Small raw-HTTP capability runner. No benchmark runtime or hidden retries."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from .config import ConfigError, ServiceAccess, redact, results_root, workspace_root

def safe_response_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {"content-type", "date", "server", "traceparent", "x-request-id", "x-trace-id", "x-msh-providerdeployid", "x-msh-usage-prompt-tokens", "x-msh-usage-cached-tokens"}
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}

MARKERS = ("<|open|>", "<|close|>", "<|sep|>", "<|end_of_msg|>")


def token_parameter() -> str:
    value = os.environ.get("NVST_TOKEN_PARAM", os.environ.get("KVV_TOKEN_PARAM", "max_tokens"))
    if value not in {"max_tokens", "max_completion_tokens"}:
        raise ConfigError("token_parameter_invalid", "token parameter must be max_tokens or max_completion_tokens")
    return value


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
    errors: list[str] = field(default_factory=list)


def usage_of(body: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(body.get("usage"), dict):
        return body["usage"]
    for choice in body.get("choices") or []:
        if isinstance(choice.get("usage"), dict):
            return choice["usage"]
    return None


def parse_reply(status: int, text: str, *, stream: bool, headers: dict[str, str] | None = None) -> Reply:
    reply = Reply(status, text, headers=headers or {})
    if status != 200:
        return reply
    try:
        if not stream:
            body = json.loads(text)
            choice = body["choices"][0]
            message = choice["message"]
            reply.content = message.get("content") or ""
            reply.reasoning = message.get("reasoning_content") or ""
            reply.calls = [
                {"id": call.get("id"), "name": call.get("function", {}).get("name"),
                 "arguments": call.get("function", {}).get("arguments"), "index": pos}
                for pos, call in enumerate(message.get("tool_calls") or [])
            ]
            reply.finish_reason = choice.get("finish_reason")
            reply.usage = usage_of(body)
            return reply
        slots: dict[int, dict[str, Any]] = {}
        finish_count = 0
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                reply.done = True
                continue
            if reply.done:
                reply.errors.append("data after DONE")
            chunk = json.loads(data)
            reply.chunks.append(chunk)
            reply.usage = usage_of(chunk) or reply.usage
            for choice in chunk.get("choices") or []:
                if choice.get("index", 0) != 0:
                    reply.errors.append("unexpected nonzero choice index")
                    continue
                delta = choice.get("delta") or {}
                reply.content += delta.get("content") or ""
                reply.reasoning += delta.get("reasoning_content") or ""
                if choice.get("finish_reason") is not None:
                    reply.finish_reason = choice["finish_reason"]
                    finish_count += 1
                for call in delta.get("tool_calls") or []:
                    index = call.get("index")
                    if type(index) is not int or index < 0:
                        reply.errors.append("tool delta missing nonnegative integer index")
                        continue
                    slot = slots.setdefault(index, {"id": None, "name": "", "arguments": "", "index": index})
                    slot["id"] = call.get("id") or slot["id"]
                    function = call.get("function") or {}
                    if function.get("name"):
                        slot["name"] += function["name"]
                    if function.get("arguments") is not None:
                        slot["arguments"] += function["arguments"]
        reply.calls = [slots[index] for index in sorted(slots)]
        if finish_count != 1:
            reply.errors.append(f"expected one finish frame, observed {finish_count}")
        if not reply.done:
            reply.errors.append("missing DONE marker")
        if not reply.chunks:
            reply.errors.append("empty SSE stream")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        reply.errors.append(f"malformed response: {type(exc).__name__}")
    return reply


def assert_reply(reply: Reply, expect: dict[str, Any], previous: list[Reply], *, mode: str | None = None) -> list[str]:
    problems = list(reply.errors)
    if reply.status != expect["status"]:
        problems.append(f"status={reply.status}, expected={expect['status']}")
        return problems
    if expect.get("error_object"):
        try:
            if not isinstance(json.loads(reply.raw).get("error"), dict):
                problems.append("missing error object")
        except (ValueError, AttributeError):
            problems.append("error body is not a JSON object")
    if reply.status != 200:
        return problems
    if reply.finish_reason == "length":
        problems.append("output truncated: contract is inconclusive")
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
    for call in reply.calls:
        if expect.get("tool_names") and call["name"] not in expect["tool_names"]:
            problems.append(f"undeclared/unexpected tool {call['name']!r}")
        if expect.get("arguments_nonempty") and not call.get("arguments"):
            problems.append("tool-call arguments are empty")
        if expect.get("arguments_json") or expect.get("arguments_schema"):
            try:
                if not isinstance(call["arguments"], str) or not call["arguments"]:
                    raise ValueError("arguments must be a nonempty JSON string")
                parsed = json.loads(call["arguments"])
                if expect.get("arguments_schema"):
                    problems.extend("arguments schema: " + error.message for error in Draft202012Validator(expect["arguments_schema"]).iter_errors(parsed))
            except (ValueError, TypeError):
                problems.append("arguments are not a nonempty valid JSON string")
        if "forbidden_arguments" in expect and call["arguments"] == expect["forbidden_arguments"]:
            problems.append("arguments repeat forbidden history")
        if "id_min_suffix" in expect:
            match = re.fullmatch(r".+_(\d+)", call.get("id") or "")
            if not match or int(match.group(1)) < expect["id_min_suffix"]:
                problems.append("tool id suffix did not continue history")
    if expect.get("content_json") or expect.get("content_schema") or expect.get("no_empty_key"):
        try:
            value = json.loads(reply.content)
            if expect.get("content_json") == "object" and not isinstance(value, dict):
                problems.append("content is not a JSON object")
            if expect.get("no_empty_key") and isinstance(value, dict) and "" in value:
                problems.append("empty-key regression")
            if expect.get("content_schema"):
                problems.extend("content schema: " + error.message for error in Draft202012Validator(expect["content_schema"]).iter_errors(value))
        except (ValueError, TypeError):
            problems.append("content is not valid JSON")
    if expect.get("usage") or "prompt_tokens" in expect or "same_prompt_as" in expect:
        usage = reply.usage or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if type(usage.get(key)) is not int or usage[key] < 0:
                problems.append(f"usage.{key} missing or not a nonnegative integer")
        if all(type(usage.get(key)) is int for key in ("prompt_tokens", "completion_tokens", "total_tokens")) and usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            problems.append("usage total != prompt + completion")
        if "prompt_tokens" in expect and usage.get("prompt_tokens") != expect["prompt_tokens"]:
            problems.append(f"prompt_tokens={usage.get('prompt_tokens')}, expected={expect['prompt_tokens']}")
        if "same_prompt_as" in expect and (mode != "nonstream" or previous):
            index = expect["same_prompt_as"]
            if index >= len(previous) or (previous[index].usage or {}).get("prompt_tokens") != usage.get("prompt_tokens"):
                problems.append("equivalent requests have different prompt_tokens")
        if "minimum_prompt_delta" in expect:
            if not previous:
                problems.append("prompt delta needs a previous request")
            else:
                before = (previous[-1].usage or {}).get("prompt_tokens")
                after = usage.get("prompt_tokens")
                if type(before) is not int or type(after) is not int or after - before < expect["minimum_prompt_delta"]:
                    problems.append(f"prompt token delta={None if before is None or after is None else after - before}, expected at least {expect['minimum_prompt_delta']}")
    return problems


def local_case_result(case: dict[str, Any], step: dict[str, Any]) -> list[str]:
    assertion = (step.get("request") or {}).get("local_assert")
    if assertion == "token_parameter_exclusive":
        return [] if token_parameter() in {"max_tokens", "max_completion_tokens"} else ["invalid token parameter"]
    if assertion == "parser_vectors_valid":
        from pathlib import Path
        import sys
        sys.path.insert(0, str(workspace_root() / "scripts"))
        from parser_conformance_lib import load_vectors
        try:
            load_vectors(workspace_root() / "capabilities/prompt_rendering/k3-parser-conformance.jsonl")
            return []
        except (OSError, ValueError) as exc:
            return [str(exc)]
    return [f"unknown local assertion: {assertion}"]


class CapabilityClient:
    def __init__(self, service: ServiceAccess, *, timeout: float = 120):
        self.service = service
        self.timeout = timeout

    def call(self, request: dict[str, Any]) -> Reply:
        headers = {**self.service.headers, "Authorization": f"Bearer {self.service.credential()}"}
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                response = client.post(self.service.base_url + "/chat/completions", json=request, headers=headers)
                return parse_reply(response.status_code, response.text, stream=bool(request.get("stream")), headers=safe_response_headers(dict(response.headers)))
        except httpx.HTTPError as exc:
            return Reply(None, errors=[f"transport failure: {type(exc).__name__}"])


def materialize_step(step: dict[str, Any], case: dict[str, Any], service: ServiceAccess,
                     mode: str, root: Path) -> dict[str, Any]:
    if "request_fixture" in step:
        path = root / step["request_fixture"]
        sample = json.loads(path.read_text().splitlines()[0])
        request = copy.deepcopy(sample.get("request", sample))
        request = {key: request[key] for key in ("messages", "tools", "tool_choice", "response_format", "reasoning_effort", "thinking") if key in request}
    else:
        request = copy.deepcopy(step["request"])
    if "message_slice" in step:
        request["messages"] = request.get("messages", [])[:step["message_slice"]]
    request["model"] = service.served_model
    request.pop("max_tokens", None)
    request.pop("max_completion_tokens", None)
    request[token_parameter()] = case["max_output_tokens"]
    request["stream"] = step.get("mode", mode) == "stream"
    if request["stream"]:
        request["stream_options"] = {"include_usage": True}
    return request


def run_plan(plan: dict[str, Any], service: ServiceAccess, *, max_requests: int,
             root: Path | None = None, timeout: float = 120) -> dict[str, Any]:
    root = root or workspace_root()
    if plan["request_budget"] > max_requests:
        raise ConfigError("request_budget_exceeded", f"plan needs {plan['request_budget']} requests; ceiling is {max_requests}")
    if not service.credential():
        raise ConfigError("credential_missing", f"set {service.api_key_env}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = results_root(root) / plan["model"] / f"{stamp}-{os.urandom(4).hex()}"
    directory.mkdir(parents=True, exist_ok=False)
    client = CapabilityClient(service, timeout=timeout)
    results: list[dict[str, Any]] = []
    actual_requests = 0
    credential = service.credential()

    def save(path: Path, payload: Any) -> None:
        text = json.dumps(redact(payload, credential), ensure_ascii=False, indent=2, default=str)
        path.write_text(text + "\n")

    save(directory / "plan.json", plan)
    save(directory / "service-access.json", service.public_dict())
    for case in plan["cases"]:
        if case["missing_requirements"]:
            results.append({"id": case["id"], "status": "unsupported", "problems": case["missing_requirements"]})
            continue
        previous: list[Reply] = []
        for repeat in range(plan["repeat"]):
            for mode in case["modes"]:
                for index, step in enumerate(case["steps"]):
                    if case.get("scope", "endpoint") == "endpoint":
                        request = materialize_step(step, case, service, mode, root)
                        reply = client.call(request)
                        actual_requests += 1
                        problems = assert_reply(reply, step["expect"], previous, mode=mode)
                        previous.append(reply)
                        raw_response = asdict(reply)
                    else:
                        request = step.get("request") or {}
                        problems = local_case_result(case, step)
                        raw_response = {"scope": case.get("scope"), "status": 200}
                    result = {"id": case["id"], "capability": case["capability"], "repeat": repeat,
                              "mode": mode, "step": index, "status": "failed" if problems else "passed",
                              "problems": problems, "likely_components": case["likely_components"],
                              "knowledge": case["knowledge"],
                              "source": case["source"]}
                    results.append(result)
                    evidence = directory / f"{case['id']}-{repeat}-{mode}-{index}.json"
                    save(evidence, {"request": request, "response": raw_response, "result": result,
                                    "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()})
    status = "failed" if any(item["status"] == "failed" for item in results) else "incomplete" if any(item["status"] == "unsupported" for item in results) else "passed"
    report = {"schema_version": 1, "status": status, "model": plan["model"], "run_dir": str(directory),
              "planned_requests": plan["request_budget"], "actual_requests": actual_requests, "results": results,
              "disclaimer": "Capability regression evidence; not a model-quality benchmark or vendor acceptance."}
    save(directory / "report.json", report)
    lines = ["# Kimi capability regression", "", f"Status: **{status}**", f"Requests: {actual_requests}/{plan['request_budget']}", "", report["disclaimer"], ""]
    for result in results:
        lines.append(f"- {result['id']}: {result['status']}")
        lines.extend(f"  - {problem}" for problem in result["problems"])
    (directory / "report.md").write_text("\n".join(lines) + "\n")
    return report
