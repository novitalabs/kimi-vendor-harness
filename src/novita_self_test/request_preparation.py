"""Local request compilation shared by planning and execution; never sends traffic."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import NamedTuple

from .case_catalog import iter_case_steps, read_request
from .case_semantics import validate_step
from .config import ConfigError, ServiceAccess

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class PreparedStep(NamedTuple):
    case: dict
    repeat: int
    group: int
    index: int
    mode: str
    step: dict
    request: dict
    expect: dict


def token_parameter() -> str:
    value = os.environ.get("NVST_TOKEN_PARAM", os.environ.get("KVV_TOKEN_PARAM", "max_tokens"))
    if value not in {"max_tokens", "max_completion_tokens"}:
        raise ConfigError("token_parameter_invalid", "use max_tokens or max_completion_tokens")
    return value


def materialize_step(step: dict, case: dict, service: ServiceAccess, mode: str, root: Path) -> dict:
    request = read_request(step, root)
    validate_step(step, request)
    if "message_slice" in step:
        request["messages"] = request.get("messages", [])[:step["message_slice"]]
    request["model"] = service.served_model
    request.pop("max_tokens", None)
    request.pop("max_completion_tokens", None)
    request[token_parameter()] = case["max_output_tokens"]
    if "thinking" in step:
        enabled, effort = step["thinking"], step.get("effort")
        if service.think_mode == "opensource":
            request["reasoning_effort"] = effort if enabled else "none"
        else:
            request["thinking"] = {"type": "enabled", "keep": "all", "effort": effort} if enabled else {"type": "disabled"}
    request["stream"] = mode == "stream"
    if request["stream"]:
        request["stream_options"] = {**request.get("stream_options", {}), "include_usage": True}
    return request


def prepare_steps(plan: dict, service: ServiceAccess, root: Path) -> list[PreparedStep]:
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
                    expect["tools"] = request["tool_choice"]
            prepared.append(PreparedStep(case, repeat, group, index, mode, step, request, expect))
    return prepared
