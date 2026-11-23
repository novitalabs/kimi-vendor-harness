"""Cross-field checks for local test specifications, not model behavior guesses."""
from __future__ import annotations

from .config import ConfigError


def validate_step(step: dict, request: dict) -> None:
    if "thinking" in step and any(key in request for key in ("thinking", "reasoning_effort")):
        raise ConfigError("case_invalid", "canonical thinking cannot be combined with raw fields", path=["thinking"])
    if step.get("thinking") is True and not step.get("effort"):
        raise ConfigError("case_invalid", "canonical thinking=true requires an explicit verified effort", path=["effort"])
    if "effort" in step and (not step.get("thinking") or step["effort"] == "none"):
        raise ConfigError("case_invalid", "effort requires thinking=true and a non-none effort", path=["effort"])
    expect = step.get("expect", {"status": 200})
    if expect["status"] != 200:
        return  # Preserve raw malformed/alias rejection probes.
    choice = request.get("tool_choice")
    if choice in ("required", "none") and expect.get("tools", choice) != choice:
        raise ConfigError("case_invalid", "expect.tools contradicts request.tool_choice", path=["expect", "tools"])
    finish = expect.get("finish_reason")
    tools = expect.get("tools", choice)
    if (tools == "required" and finish not in (None, "tool_calls", "length", "content_filter")) or (tools == "none" and finish == "tool_calls"):
        raise ConfigError("case_invalid", "finish_reason contradicts tool-choice expectation", path=["expect", "finish_reason"])
    if finish is not None and finish not in {"stop", "length", "tool_calls", "content_filter"}:
        raise ConfigError("case_invalid", "unknown expected finish_reason", path=["expect", "finish_reason"])
    raw_thinking = request.get("thinking")
    # Conflicting raw aliases are deliberate precedence probes; don't guess which wins.
    disabled = (step.get("thinking") is False
                or "thinking" not in request and request.get("reasoning_effort") == "none"
                or "reasoning_effort" not in request and isinstance(raw_thinking, dict) and raw_thinking.get("type") == "disabled")
    if disabled and expect.get("reasoning") is True:
        raise ConfigError("case_invalid", "reasoning expectation contradicts disabled thinking", path=["expect", "reasoning"])
    global_tools = request.get("tools", [])
    if not isinstance(global_tools, list):
        raise ConfigError("case_invalid", "successful request tools must be an array", path=["request", "tools"])
    declarations = list(global_tools)
    for message in request.get("messages", []):
        if not isinstance(message, dict):
            raise ConfigError("case_invalid", "successful request messages must be objects", path=["request", "messages"])
        tools = message.get("tools", [])
        if not isinstance(tools, list):
            raise ConfigError("case_invalid", "dynamic tools must be an array", path=["request", "messages"])
        declarations.extend(tools)
    try:
        functions = {tool["function"]["name"]: tool["function"] for tool in declarations}
    except (KeyError, TypeError) as exc:
        raise ConfigError("case_invalid", "successful request tool declarations need function/name", path=["request", "tools"]) from exc
    if any(not isinstance(name, str) or not name for name in functions):
        raise ConfigError("case_invalid", "declared names must be nonempty strings", path=["request", "tools"])
    names = expect.get("tool_names", list(functions))
    if set(names) - functions.keys():
        raise ConfigError("case_invalid", "oracle names an undeclared tool", path=["expect", "tool_names"])
    if choice == "required" and not functions:
        raise ConfigError("case_invalid", "successful required-tool request needs declared tools", path=["request", "tools"])
    # Schemas may intentionally express stronger or narrower diagnostics. Do not
    # confuse different bytes/annotations with a proof of semantic contradiction.
    root_type = expect.get("arguments_schema", {}).get("type")
    if root_type and "object" not in (root_type if isinstance(root_type, list) else [root_type]):
        raise ConfigError("case_invalid", "arguments oracle excludes the required object root", path=["expect", "arguments_schema", "type"])


def validate_role(case: dict) -> None:
    if (case["role"] == "observation") != (case["determinism"] == "observation"):
        raise ConfigError("case_invalid", "observation role and determinism must agree", path=["role"])
