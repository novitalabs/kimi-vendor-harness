"""Offline comparisons/retest selection: no network, retries or stale-plan reuse."""
from __future__ import annotations

from pathlib import Path

from .config import ConfigError, canonical_hash, strict_json
from .contracts import validate_contract


def scenario_identity(case: dict, plan: dict) -> str:
    # Source files/paths/target/runtime are not logical test identity. Fixture bytes are.
    steps = []
    for step in case["steps"]:
        value = dict(step)
        if "request_fixture" in value:
            value["fixture_sha256"] = plan.get("inputs", {}).get(value.pop("request_fixture"))
        steps.append(value)
    contract = {key: case.get(key) for key in ("id", "modes", "max_output_tokens", "role", "stage", "obligation", "required_features", "missing_requirements")}
    contract.update(steps=steps, model=plan["model"], profile_sha256=plan.get("profile_sha256"),
                    token_parameter=plan.get("execution", {}).get("token_parameter"),
                    think_mode=(plan.get("target") or {}).get("think_mode"), producer=plan.get("producer"))
    return canonical_hash(contract)


def load_report(path: Path) -> dict:
    try:
        value = strict_json(path.read_text(encoding="utf-8"))
        validate_contract(value, "report.schema.json")
        return value
    except (OSError, ValueError, ConfigError) as exc:
        raise ConfigError("report_invalid", "cannot load endpoint report") from exc


def failed_ids(report: dict) -> list[str]:
    if report.get("status") in {"service_invalid", "execution_invalid"}:
        return []  # Resolve invalid execution first; don't turn its tail into retries.
    return sorted({row["id"] for row in report["results"] if row["status"] in {"failed", "inconclusive", "not_run"}})


def _case_status(rows: list[dict]) -> str:
    statuses = {row["status"] for row in rows}
    return next((status for status in ("service_invalid", "execution_invalid", "failed", "contract_unknown", "unsupported", "not_run", "inconclusive", "observed") if status in statuses), "passed")


def compare(baseline: dict, candidate: dict) -> dict:
    left, right = {}, {}
    for target, report in ((left, baseline), (right, candidate)):
        for row in report["results"]:
            target.setdefault(row["id"], []).append(row)
    changes = []
    for id in sorted(set(left) | set(right)):
        a, b = left.get(id, []), right.get(id, [])
        before, after = _case_status(a) if a else None, _case_status(b) if b else None
        units_a = sorted((row.get("repeat", -1), row.get("group", -1), row.get("step", -1), row.get("mode", "")) for row in a)
        units_b = sorted((row.get("repeat", -1), row.get("group", -1), row.get("step", -1), row.get("mode", "")) for row in b)
        comparable = bool(a and b) and units_a == units_b and baseline.get("scenario_sha256", {}).get(id) == candidate.get("scenario_sha256", {}).get(id) and baseline.get("scenario_sha256", {}).get(id) is not None
        if not comparable:
            transition = "not_comparable"
        elif before == "failed" and after == "passed":
            transition = "fixed"
        elif before == "passed" and after == "failed":
            transition = "regressed"
        elif before == after:
            transition = "unchanged"
        else:
            transition = "changed_evidence"
        changes.append({"id": id, "before": before, "after": after, "transition": transition,
                        "candidate_evidence": [row["evidence"] for row in b if "evidence" in row]})
    comparison_status = "failed" if any(row["transition"] == "regressed" for row in changes) else "incomplete" if any(row["transition"] == "not_comparable" for row in changes) else "passed"
    return {"schema_version": 1, "kind": "report_comparison", "status": comparison_status, "endpoint_requests": 0,
            "baseline_run": baseline["run_dir"], "candidate_run": candidate["run_dir"], "changes": changes,
            "retest_case_ids": failed_ids(candidate),
            "warning": "Status describes comparison, not candidate acceptance: failed=regression, incomplete=spec/scope mismatch. Unchanged failures remain failures. Different target revisions are expected; check runtime provenance. No tests were rerun."}
