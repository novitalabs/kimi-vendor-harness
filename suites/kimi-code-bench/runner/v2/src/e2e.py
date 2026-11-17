"""Phase-level E2E analysis for archived Harbor trials."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable


PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")
TIMING_FIELDS = ("task_total_e2e_ms",) + tuple(f"{phase}_ms" for phase in PHASES)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration_ms(value: dict[str, Any] | None) -> float | None:
    if not value or not value.get("started_at") or not value.get("finished_at"):
        return None
    return round((_time(value["finished_at"]) - _time(value["started_at"])).total_seconds() * 1000, 3)


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _proxy_counts(proxy_root: Path) -> tuple[int, int, bool]:
    requests = list((proxy_root / "requests").glob("*.json"))
    responses = list((proxy_root / "responses").glob("*.json"))
    has_502 = False
    for path in responses:
        response = _load(path)
        if response.get("status_code", response.get("status")) == 502:
            has_502 = True
            break
    return len(requests), len(responses), has_502


def analyze_leaf(leaf: str | Path, high_step_threshold: int = 100) -> dict[str, Any]:
    root = Path(leaf)
    result = _load(root / "result.json")
    attempt = _load(root / "kbench-attempt.json")
    record = _load(root / "trial-record.json")
    outcome = record.get("outcome") or {}
    request_count, response_count, has_proxy_502 = _proxy_counts(root / "agent/llmproxy/openai")
    task_total = _duration_ms({"started_at": result.get("started_at"), "finished_at": result.get("finished_at")})
    timings = {f"{phase}_ms": _duration_ms(result.get(phase)) for phase in PHASES}
    return {
        "trial": str(root),
        "task": attempt.get("task_label") or result.get("task_name") or root.name,
        "phase": attempt.get("phase"),
        "round": attempt.get("round"),
        "attempt": attempt.get("attempt"),
        "reward": outcome.get("reward", (result.get("verifier_result") or {}).get("rewards", {}).get("reward")),
        "execution_valid": outcome.get("execution_valid"),
        "request_count": request_count,
        "response_count": response_count,
        "has_proxy_502": has_proxy_502,
        "step_class": "high_step" if request_count >= high_step_threshold else "normal_step",
        "task_total_e2e_ms": task_total,
        **timings,
    }


def _timing_summary(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    values = sorted(float(record[field]) for record in records if record.get(field) is not None)
    if not values:
        return {"observed": 0, "total_ms": 0.0, "mean_ms": None, "median_ms": None, "max_ms": None}
    return {
        "observed": len(values),
        "total_ms": round(sum(values), 3),
        "mean_ms": round(sum(values) / len(values), 3),
        "median_ms": round(median(values), 3),
        "max_ms": round(values[-1], 3),
    }


def _group(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "trial_count": len(records),
        "execution_invalid_count": sum(record.get("execution_valid") is False for record in records),
        "pass_count": sum(record.get("reward") == 1 for record in records),
        "request_count_total": sum(record["request_count"] for record in records),
        "timings": {field: _timing_summary(records, field) for field in TIMING_FIELDS},
    }


def analyze_run(run_dir: str | Path, high_step_threshold: int = 100) -> dict[str, Any]:
    root = Path(run_dir)
    leaf_paths = sorted(
        path.parent
        for path in root.glob("jobs/*/*/*/result.json")
        if (path.parent / "agent/llmproxy/openai").is_dir()
    )
    records = [analyze_leaf(path, high_step_threshold) for path in leaf_paths]
    selectors: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("all", lambda _record: True),
        ("baseline", lambda record: record["phase"] == "baseline"),
        ("baseline_with_proxy_502", lambda record: record["phase"] == "baseline" and record["has_proxy_502"]),
        ("baseline_without_proxy_502", lambda record: record["phase"] == "baseline" and not record["has_proxy_502"]),
        ("high_step_ge_threshold", lambda record: record["request_count"] >= high_step_threshold),
        ("normal_step_lt_threshold", lambda record: record["request_count"] < high_step_threshold),
        ("recovery", lambda record: record["phase"] == "recovery"),
    ]
    groups = {
        name: _group([record for record in records if selector(record)])
        for name, selector in selectors
    }
    return {
        "run_dir": str(root),
        "trial_count": len(records),
        "high_step_request_threshold": high_step_threshold,
        "timing_semantics": {
            "task_total_e2e_ms": "Harbor result.started_at to result.finished_at; includes setup, agent, verifier, and cleanup gaps",
            "agent_execution_ms": "Harbor result.agent_execution only",
            "groups": "Diagnostic views overlap; group totals must not be added together",
        },
        "groups": groups,
        "trials": records,
    }


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1000:.3f}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Code Bench E2E Phase Summary",
        "",
        f"Trials: {report['trial_count']}",
        f"High-step threshold: {report['high_step_request_threshold']} requests",
        "",
        "`task total` is the full Harbor trial wall time. `agent execution` is the Harbor agent phase only.",
        "Groups are overlapping diagnostic views and must not be summed.",
        "",
        "| group | trials | invalid | requests | task total sum (s) | task median (s) | agent sum (s) | agent median (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, group in report["groups"].items():
        task = group["timings"]["task_total_e2e_ms"]
        agent = group["timings"]["agent_execution_ms"]
        lines.append(
            f"| {name} | {group['trial_count']} | {group['execution_invalid_count']} | "
            f"{group['request_count_total']} | {_seconds(task['total_ms'])} | {_seconds(task['median_ms'])} | "
            f"{_seconds(agent['total_ms'])} | {_seconds(agent['median_ms'])} |"
        )
    return "\n".join(lines) + "\n"


def write_report(run_dir: str | Path, high_step_threshold: int = 100) -> dict[str, Any]:
    root = Path(run_dir)
    report = analyze_run(root, high_step_threshold)
    (root / "e2e-phase-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (root / "e2e-phase-summary.md").write_text(render_markdown(report))
    return report
