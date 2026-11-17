"""Official-observed Code Bench score aggregation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def aggregate(records: Iterable[dict[str, Any]], selected_tasks: Iterable[str], *, baseline_trials: int) -> dict[str, Any]:
    """Average the independent baseline-round scores.

    Recovery is evidence for abnormal executions. It must never turn a failed
    baseline round into a pass, otherwise a single recovery pass changes the
    baseline-round statistics into an any-pass metric.
    """
    selected = list(selected_tasks)
    selected_set = set(selected)
    baseline_slots: dict[tuple[int, str], dict[str, Any]] = {}
    duplicate_slots: list[dict[str, Any]] = []
    recovery_records: list[dict[str, Any]] = []
    all_records = list(records)

    for record in all_records:
        task = record.get("identity", {}).get("task") or record.get("task")
        phase = record.get("phase") or record.get("execution", {}).get("phase")
        round_number = record.get("round") or record.get("execution", {}).get("round")
        if task not in selected_set:
            continue
        if phase == "recovery":
            recovery_records.append(record)
            continue
        if phase not in (None, "baseline"):
            continue
        # A single-round legacy record is unambiguous. Multi-round records must
        # carry their observed round rather than being assigned by ordering.
        if round_number is None and baseline_trials == 1:
            round_number = 1
        try:
            round_number = int(round_number)
        except (TypeError, ValueError):
            round_number = 0
        if not 1 <= round_number <= baseline_trials:
            continue
        key = (round_number, str(task))
        if key in baseline_slots:
            duplicate_slots.append({"round": round_number, "task": str(task)})
            continue
        baseline_slots[key] = record

    missing_slots = [
        {"round": round_number, "task": task}
        for round_number in range(1, baseline_trials + 1)
        for task in selected
        if (round_number, task) not in baseline_slots
    ]
    missing_tasks = sorted({entry["task"] for entry in missing_slots})
    round_scores = []
    for round_number in range(1, baseline_trials + 1):
        records_for_round = [baseline_slots[(round_number, task)] for task in selected if (round_number, task) in baseline_slots]
        passed = sum(_reward_is_pass(record) for record in records_for_round)
        complete = len(records_for_round) == len(selected)
        round_scores.append({
            "round": round_number,
            "recorded_tasks": len(records_for_round),
            "passed_tasks": passed,
            "failed_tasks": len(records_for_round) - passed,
            "accuracy": (passed / len(selected)) if complete and selected else None,
            "complete": complete,
        })

    publishable = not missing_slots and not duplicate_slots
    accuracy = (
        sum(float(round_score["accuracy"]) for round_score in round_scores) / baseline_trials
        if publishable and baseline_trials
        else None
    )
    any_pass_diagnostic = {
        task: any(_reward_is_pass(baseline_slots[(round_number, task)]) for round_number in range(1, baseline_trials + 1) if (round_number, task) in baseline_slots)
        for task in selected
    }
    return {
        "schema_version": 2,
        "aggregation": "baseline_round_mean",
        "selected_task_count": len(selected),
        "baseline_trial_count": baseline_trials,
        "recorded_baseline_trial_count": len(baseline_slots),
        "recorded_trial_count": len(all_records),
        "missing_baseline_tasks": missing_tasks,
        "missing_baseline_slots": missing_slots,
        "duplicate_baseline_slots": duplicate_slots,
        "publishable": publishable,
        "accuracy": accuracy,
        "round_scores": round_scores,
        "recovery_trials": len(recovery_records),
        "recovery_passes": sum(_reward_is_pass(record) for record in recovery_records),
        "execution_invalid_trials": sum(1 for record in all_records if not _execution_valid(record)),
        "diagnostic_any_baseline_passed_tasks": sum(any_pass_diagnostic.values()),
        "diagnostic_any_baseline_task_results": any_pass_diagnostic,
    }


def write_report(run_dir: str | Path, records: Iterable[dict[str, Any]], selected_tasks: Iterable[str], *, baseline_trials: int) -> dict[str, Any]:
    root = Path(run_dir)
    result = aggregate(records, selected_tasks, baseline_trials=baseline_trials)
    if not result["publishable"]:
        return result
    (root / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines = [
        "# Code Bench Metrics",
        "",
        "aggregation: baseline_round_mean",
        f"accuracy: {result['accuracy']:.6%}",
        "",
        "| round | passed | total | accuracy |",
        "|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {entry['round']} | {entry['passed_tasks']} | {entry['recorded_tasks']} | {entry['accuracy']:.6%} |"
        for entry in result["round_scores"]
    )
    (root / "metrics.md").write_text("\n".join(lines) + "\n")
    return result


def _reward_is_pass(record: dict[str, Any]) -> bool:
    reward = record.get("outcome", {}).get("reward", record.get("reward", 0))
    try:
        return float(reward or 0) == 1.0
    except (TypeError, ValueError):
        return False


def _execution_valid(record: dict[str, Any]) -> bool:
    return bool(record.get("outcome", {}).get("execution_valid", record.get("execution_valid", True)))
