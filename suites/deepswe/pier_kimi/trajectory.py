"""Reconstruct an ATIF trajectory from Kimi Code CLI artifacts.

Two sources are combined:

``stream-json`` stdout (``kimi -p --output-format stream-json``)
    One JSON object per line. Shapes emitted by kimi-code 0.29.x:

        {"role": "assistant", "content": "..."}
        {"role": "assistant", "tool_calls": [
            {"type": "function", "id": "...",
             "function": {"name": "...", "arguments": "<json string>"}}]}
        {"role": "tool", "tool_call_id": "...", "content": "..."}
        {"role": "meta", "type": "turn.step.retrying", ...}
        {"role": "meta", "type": "system.version", ...}
        {"role": "meta", "type": "session.resume_hint", ...}

    Note: thinking content is NOT written to this stream (it goes to stderr),
    so reasoning_content stays absent.

``agents/*/wire.jsonl`` session logs
    Append-only op records shaped ``{"type": <op>, "time": <ms>, ...payload}``.
    ``usage.record`` carries ``{model, usage: {inputOther, output,
    inputCacheRead, inputCacheCreation}, usageScope}`` — the only place per-call
    token counts are persisted. ``full_compaction.complete`` and
    ``context.apply_compaction`` mark context-compaction boundaries.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pier.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from pier.utils.trajectory_metrics import (
    extra_with_context_metrics,
    peak_context_tokens_from_steps,
)

COMPACTION_OPS = {"full_compaction.complete", "context.apply_compaction"}
USAGE_OP = "usage.record"


def build_trajectory(
    *,
    agent_name: str,
    agent_version: str,
    model_name: str | None,
    reasoning_effort: str | None,
    stdout_path: Path,
    wire_paths: list[Path],
) -> Trajectory | None:
    """Build an ATIF trajectory, or None when nothing usable was produced."""
    events = _read_jsonl(stdout_path)
    main_records, all_records, compaction_count = _read_wire(wire_paths)

    steps = _build_steps(
        events,
        usage_records=main_records,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    if not steps:
        return None

    final_metrics = _build_final_metrics(
        steps, usage_records=all_records, compaction_count=compaction_count
    )

    return Trajectory(
        schema_version="ATIF-v1.7",
        agent=Agent(
            name=agent_name,
            version=agent_version,
            model_name=model_name,
        ),
        steps=steps,
        final_metrics=final_metrics,
    )


# --------------------------------------------------------------------- reading


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _is_main_agent_log(path: Path) -> bool:
    """True for ``agents/main/wire.jsonl``, false for ``agents/agent-N/``."""
    return path.parent.name == "main"


def _read_wire(paths: list[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Return (main-agent usage records, all usage records, compaction count).

    Per-step metrics may only use the main agent's records: subagent
    generations interleave in wall-clock time but never appear as top-level
    assistant messages, so mixing them in would shift the positional match.
    Totals, on the other hand, must include subagents so that reported output
    tokens cover all generations.
    """
    main_records: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    compaction_count = 0
    for path in paths:
        is_main = _is_main_agent_log(path)
        for record in _read_jsonl(path):
            op_type = record.get("type")
            if op_type == USAGE_OP:
                usage = record.get("usage")
                if not isinstance(usage, dict):
                    continue
                entry = {
                    "model": record.get("model"),
                    "usage": usage,
                    "scope": record.get("usageScope"),
                    "time": record.get("time"),
                }
                all_records.append(entry)
                if is_main:
                    main_records.append(entry)
            elif op_type in COMPACTION_OPS and is_main:
                compaction_count += 1
    main_records.sort(key=lambda item: item.get("time") or 0)
    all_records.sort(key=lambda item: item.get("time") or 0)
    return main_records, all_records, compaction_count


# ---------------------------------------------------------------------- steps


def _build_steps(
    events: list[dict[str, Any]],
    *,
    usage_records: list[dict[str, Any]],
    model_name: str | None,
    reasoning_effort: str | None,
) -> list[Step]:
    """Turn stream-json events into ATIF steps.

    ATIF v1.7 wants strictly one LLM turn per agent step, so each assistant
    message opens a new step and the following ``role: tool`` messages attach
    to it as observations. Usage records are consumed in order and matched to
    agent steps positionally — the CLI emits one ``usage.record`` per LLM
    generation, in the same order the assistant messages appear.
    """
    steps: list[Step] = []
    usage_index = 0
    pending_results: dict[str, list[ObservationResult]] = {}

    for event in events:
        role = event.get("role")

        if role == "meta":
            continue

        if role == "assistant":
            content = event.get("content") or ""
            raw_tool_calls = event.get("tool_calls") or []
            if not content and not raw_tool_calls:
                continue

            metrics = None
            if usage_index < len(usage_records):
                metrics = _metrics_from_usage(usage_records[usage_index]["usage"])
                usage_index += 1

            step = Step(
                step_id=len(steps) + 1,
                source="agent",
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                message=content,
                tool_calls=_parse_tool_calls(raw_tool_calls) or None,
                metrics=metrics,
                llm_call_count=1,
            )
            steps.append(step)
            continue

        if role == "tool":
            call_id = event.get("tool_call_id")
            result = ObservationResult(
                source_call_id=call_id if isinstance(call_id, str) else None,
                content=_stringify(event.get("content")),
            )
            if steps:
                _attach_observation(steps[-1], result)
            else:
                pending_results.setdefault("orphan", []).append(result)

    return steps


def _attach_observation(step: Step, result: ObservationResult) -> None:
    if step.observation is None:
        step.observation = Observation(results=[result])
    else:
        step.observation.results.append(result)


def _parse_tool_calls(raw_tool_calls: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if not isinstance(raw_tool_calls, list):
        return calls
    for index, raw in enumerate(raw_tool_calls):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else None
        raw_arguments = function.get("arguments") if isinstance(function, dict) else None
        calls.append(
            ToolCall(
                tool_call_id=str(raw.get("id") or f"call_{index}"),
                function_name=str(name or "unknown"),
                arguments=_parse_arguments(raw_arguments),
            )
        )
    return calls


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string; keep non-object payloads intact."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"_value": parsed}
    return {}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# --------------------------------------------------------------------- metrics


def _metrics_from_usage(usage: dict[str, Any]) -> Metrics | None:
    """Map Kimi's TokenUsage breakdown onto ATIF Metrics."""
    input_other = _as_int(usage.get("inputOther"))
    cache_read = _as_int(usage.get("inputCacheRead"))
    cache_creation = _as_int(usage.get("inputCacheCreation"))
    output = _as_int(usage.get("output"))
    if input_other is None and cache_read is None and output is None:
        return None

    prompt_tokens = sum(
        value for value in (input_other, cache_read, cache_creation) if value is not None
    )
    return Metrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=output,
        cached_tokens=cache_read,
    )


def _build_final_metrics(
    steps: list[Step],
    *,
    usage_records: list[dict[str, Any]],
    compaction_count: int,
) -> FinalMetrics:
    """Aggregate over ALL usage records, not just the ones matched to steps.

    The doc reports ``avg output tokens``, so completion totals must include
    subagent generations that never surface as top-level assistant messages.
    """
    total_prompt = 0
    total_completion = 0
    total_cached = 0
    for record in usage_records:
        usage = record["usage"]
        total_prompt += (
            (_as_int(usage.get("inputOther")) or 0)
            + (_as_int(usage.get("inputCacheRead")) or 0)
            + (_as_int(usage.get("inputCacheCreation")) or 0)
        )
        total_completion += _as_int(usage.get("output")) or 0
        total_cached += _as_int(usage.get("inputCacheRead")) or 0

    return FinalMetrics(
        total_prompt_tokens=total_prompt or None,
        total_completion_tokens=total_completion or None,
        total_cached_tokens=total_cached or None,
        total_steps=len(steps),
        extra=extra_with_context_metrics(
            {"llm_call_count": len(usage_records)} if usage_records else None,
            peak_context_tokens=peak_context_tokens_from_steps(steps),
            summarization_count=compaction_count or None,
        ),
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _iso(timestamp_ms: Any) -> str | None:
    if not isinstance(timestamp_ms, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


__all__ = ["build_trajectory"]
