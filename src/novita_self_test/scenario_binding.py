"""Declarative synthetic tool-result binding, never dispatching generated calls."""
from __future__ import annotations

import copy
import json

from .config import ConfigError


def bind_request(request: dict, binding: dict, history: list, requests: list[dict]) -> dict:
    index = binding["step"]
    if index >= len(history) or history[index].status != 200 or history[index].incomplete or history[index].errors:
        raise ConfigError("scenario_dependency_invalid", "previous response is missing, incomplete or malformed")
    reply = history[index]
    if len(reply.calls) != binding["call_count"]:
        raise ConfigError("scenario_call_count_mismatch", "previous tool-call count differs from frozen binding")
    canned = {row["name"]: row["content"] for row in binding["tool_results"]}
    if any(call["name"] not in canned or not call["id"] for call in reply.calls) or len({call["id"] for call in reply.calls}) != len(reply.calls):
        raise ConfigError("scenario_tool_unknown", "previous response cannot map to approved canned tools")
    result = copy.deepcopy(request)
    assistant = {"role": "assistant", "content": reply.content,
                 "tool_calls": [{"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}} for call in reply.calls]}
    if reply.reasoning:
        assistant["reasoning_content"] = reply.reasoning
    # The frozen tool_results order is intentional (e.g. out-of-order completion).
    results = [{"role": "tool", "tool_call_id": call["id"], "content": row["content"]}
               for row in binding["tool_results"] for call in reply.calls if call["name"] == row["name"]]
    result["messages"] = copy.deepcopy(requests[index]["messages"]) + [assistant, *results] + result["messages"]
    # Fixed bounded call count + normal response byte cap bounds each new dynamic turn.
    if len(result["messages"]) > 512:
        raise ConfigError("scenario_message_limit", "bound scenario exceeds 512-message ceiling")
    if len(json.dumps(result, ensure_ascii=True).encode()) > 32 * 1024 * 1024:
        raise ConfigError("scenario_byte_limit", "bound scenario exceeds 32 MiB request ceiling")
    return result
