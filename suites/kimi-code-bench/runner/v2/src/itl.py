"""Request/response timing analysis for recording-proxy artifacts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _time(value: str) -> datetime:
    # Agent telemetry may carry nanosecond fractions (RFC3339Nano); Python's
    # fromisoformat accepts at most microseconds.
    normalized = value.replace("Z", "+00:00")
    if "." in normalized:
        head, tail = normalized.split(".", 1)
        fraction = ""
        suffix = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                fraction += char
            else:
                suffix = tail[index:]
                break
        else:
            suffix = ""
        if len(fraction) > 6:
            normalized = f"{head}.{fraction[:6]}{suffix}"
    return datetime.fromisoformat(normalized)


def _arrival_index(root: Path, response_id: str) -> list[dict] | None:
    path = root / "responses" / f"{response_id}.arrival.jsonl"
    if not path.is_file():
        return None
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _events(body: bytes) -> list[tuple[int, dict]]:
    result = []
    start = 0
    marker = b"data: "
    while True:
        begin = body.find(marker, start)
        if begin < 0:
            break
        end = body.find(b"\n\n", begin)
        if end < 0:
            break
        payload = body[begin + len(marker) : end].strip()
        if payload != b"[DONE]":
            try:
                result.append((end + 2, json.loads(payload)))
            except json.JSONDecodeError:
                pass
        start = end + 2
    return result


def analyze_trial(trial_dir: str | Path, threshold_ms: float = 5000.0) -> dict:
    root = Path(trial_dir)
    if (root / "agent/llmproxy/openai/responses").is_dir():
        proxy_root = root / "agent/llmproxy/openai"
        leaf_root = root
    else:
        proxy_root = root
        leaf_root = root.parent.parent.parent if root.name == "openai" else root
    response_dir = proxy_root / "responses"
    request_times = {}
    for path in sorted((proxy_root / "requests").glob("*.json")):
        try:
            request = json.loads(path.read_text())
            request_times[path.stem] = _time(request.get("started_at") or request["timestamp"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass

    responses = []
    missing_arrival = 0
    response_ids = sorted(path.stem for path in response_dir.glob("*.json"))
    for response_position, response_id in enumerate(response_ids):
        meta_path = response_dir / f"{response_id}.json"
        try:
            meta = json.loads(meta_path.read_text())
            body = meta_path.with_suffix(".body").read_bytes()
        except (OSError, json.JSONDecodeError):
            continue
        arrival = _arrival_index(proxy_root, meta_path.stem)
        if not arrival:
            missing_arrival += 1
            continue
        events = _events(body)
        timed = []
        for end_offset, event in events:
            read = next((item for item in arrival if item["cumulative_bytes"] >= end_offset), None)
            if read:
                timed.append((end_offset, read["monotonic_ns"], event))
        gaps = [(timed[i][1] - timed[i - 1][1]) / 1_000_000 for i in range(1, len(timed))]
        long = []
        for index, gap in enumerate(gaps, start=1):
            if gap >= threshold_ms:
                long.append({
                    "gap_ms": round(gap, 3), "event_index": index,
                    "event_count": len(timed), "tail_20pct": index >= max(1, int(len(timed) * 0.8)),
                })
        service_timed = [
            item for item in timed
            if isinstance((item[2].get("sla_metrics") or {}).get("ts_us"), (int, float))
        ]
        response = {
            "id": meta_path.stem,
            "event_count": len(events),
            "arrival_timed_event_count": len(timed),
            "service_timed_event_count": len(service_timed),
            "long_gaps": long,
            "arrival_precision": "socket_read_upper_bound",
        }
        if timed:
            last_event_offset = timed[-1][0]
            last_arrival_index = next(
                (index for index, item in enumerate(arrival) if item["cumulative_bytes"] >= last_event_offset),
                len(arrival) - 1,
            )
            last_arrival = arrival[min(len(arrival) - 1, last_arrival_index)]
            response["last_event_arrival_at"] = last_arrival["timestamp"]
            first_event_arrival = arrival[next(
                (index for index, item in enumerate(arrival) if item["cumulative_bytes"] >= timed[0][0]),
                len(arrival) - 1,
            )]
            request_started = request_times.get(meta_path.stem)
            if request_started:
                response["request_started_at"] = request_started.isoformat()
                response["request_to_first_event_ms"] = round(
                    (_time(first_event_arrival["timestamp"]) - request_started).total_seconds() * 1000, 3
                )
                response["request_to_last_event_ms"] = round(
                    (_time(last_arrival["timestamp"]) - request_started).total_seconds() * 1000, 3
                )
            response["wall_clock_ms"] = round((timed[-1][1] - timed[0][1]) / 1_000_000, 3)
            if len(service_timed) >= 2:
                response["service_clock_ms"] = round((
                    service_timed[-1][2]["sla_metrics"]["ts_us"]
                    - service_timed[0][2]["sla_metrics"]["ts_us"]
                ) / 1000, 3)
                response["service_minus_proxy_ms"] = round(
                    response["service_clock_ms"] - response["wall_clock_ms"], 3
                )
            next_id = response_ids[response_position + 1] if response_position + 1 < len(response_ids) else None
            if next_id in request_times:
                response["next_request_started_at"] = request_times[next_id].isoformat()
                response["last_event_to_next_request_ms"] = round((request_times[next_id] - _time(last_arrival["timestamp"])).total_seconds() * 1000, 3)
            response["long_gap_sum_ms"] = round(sum(item["gap_ms"] for item in long), 3)
        responses.append(response)

    long_gaps = [gap for response in responses for gap in response["long_gaps"]]
    execution = {}
    try:
        execution = json.loads((leaf_root / "agent/execution-meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        pass
    agent_execution_e2e_ms = None
    if execution.get("started_at") and execution.get("finished_at"):
        agent_execution_e2e_ms = round((_time(execution["finished_at"]) - _time(execution["started_at"])).total_seconds() * 1000, 3)
    task_total_e2e_ms = None
    try:
        result = json.loads((leaf_root / "result.json").read_text())
        task_total_e2e_ms = round(
            (_time(result["finished_at"]) - _time(result["started_at"])).total_seconds() * 1000,
            3,
        )
        harbor_agent = result.get("agent_execution") or {}
        if agent_execution_e2e_ms is None and harbor_agent.get("started_at") and harbor_agent.get("finished_at"):
            agent_execution_e2e_ms = round(
                (_time(harbor_agent["finished_at"]) - _time(harbor_agent["started_at"])).total_seconds() * 1000,
                3,
            )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        pass
    long_gap_sum_ms = round(sum(item["gap_ms"] for item in long_gaps), 3)
    return {
        "trial_dir": str(leaf_root), "proxy_dir": str(proxy_root), "threshold_ms": threshold_ms,
        "responses_analyzed": len(responses), "responses_missing_arrival": missing_arrival,
        "long_gap_count": len(long_gaps),
        "long_gap_tail_20pct_count": sum(item["tail_20pct"] for item in long_gaps),
        "long_gap_tail_20pct_fraction": (sum(item["tail_20pct"] for item in long_gaps) / len(long_gaps) if long_gaps else None),
        "task_total_e2e_ms": task_total_e2e_ms,
        "agent_execution_e2e_ms": agent_execution_e2e_ms,
        "long_gap_sum_ms": long_gap_sum_ms,
        "long_gap_share_of_task_total_e2e": (round(long_gap_sum_ms / task_total_e2e_ms, 6) if task_total_e2e_ms else None),
        "long_gap_share_of_agent_execution_e2e": (round(long_gap_sum_ms / agent_execution_e2e_ms, 6) if agent_execution_e2e_ms else None),
        "responses": responses,
    }


def analyze_run(run_dir: str | Path, threshold_ms: float = 5000.0) -> dict:
    root = Path(run_dir)
    trials = sorted({proxy.parent.parent.parent for proxy in root.glob("**/agent/llmproxy/openai") if (proxy / "responses").is_dir()})
    if not trials:
        raise ValueError(f"no archived trial directories: {root}")
    results = [analyze_trial(trial, threshold_ms) for trial in trials]
    long_count = sum(item["long_gap_count"] for item in results)
    tail_count = sum(item["long_gap_tail_20pct_count"] for item in results)
    summary = {
        "run_dir": str(root), "threshold_ms": threshold_ms,
        "trials": len(results), "long_gap_count": long_count,
        "long_gap_tail_20pct_count": tail_count,
        "long_gap_tail_20pct_fraction": tail_count / long_count if long_count else None,
        "task_total_e2e_ms_total": sum(item["task_total_e2e_ms"] or 0 for item in results),
        "agent_execution_e2e_ms_total": sum(item["agent_execution_e2e_ms"] or 0 for item in results),
        "long_gap_sum_ms_total": sum(item["long_gap_sum_ms"] for item in results),
        "results": results,
    }
    threshold_label = f"{threshold_ms / 1000:g}s" if threshold_ms % 1000 == 0 else f"{threshold_ms:g}ms"
    (root / f"itl-{threshold_label}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main(path: str, threshold_ms: float = 5000.0) -> int:
    root = Path(path)
    if (root / "responses").is_dir() or (root / "agent/llmproxy/openai/responses").is_dir():
        result = analyze_trial(root, threshold_ms)
    else:
        result = analyze_run(root, threshold_ms)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
