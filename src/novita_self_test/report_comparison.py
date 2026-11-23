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


def unit_key(row: dict) -> tuple:
    return tuple(row.get(key, default) for key, default in (("repeat", -1), ("group", -1), ("step", -1), ("mode", "")))


def _transition(before: dict, after: dict) -> str:
    if before["status"] == "failed" and after["status"] == "passed":
        return "fixed"
    if before["status"] == "passed" and after["status"] == "failed":
        return "regressed"
    codes = lambda row: sorted(problem["code"] for problem in row.get("problems", []))
    return "unchanged" if before["status"] == after["status"] and codes(before) == codes(after) else "changed_evidence"


def compare(baseline: dict, candidate: dict, *, scope: str = "all") -> dict:
    if scope not in {"all", "candidate"}:
        raise ConfigError("comparison_scope_invalid", "comparison scope must be all or candidate")
    left, right = {}, {}
    for target, report in ((left, baseline), (right, candidate)):
        for row in report["results"]:
            target.setdefault(row["id"], []).append(row)
    changes = []
    ids = set(right) if scope == "candidate" else set(left) | set(right)
    for id in sorted(ids):
        a, b = left.get(id, []), right.get(id, [])
        indexed_a, indexed_b = {unit_key(row): row for row in a}, {unit_key(row): row for row in b}
        coordinates_valid = all(all(type(row.get(key)) is int and row[key] >= 0 for key in ("repeat", "group", "step"))
                                and row.get("mode") in {"stream", "nonstream"} for row in [*a, *b])
        comparable = (bool(a and b) and coordinates_valid and len(indexed_a) == len(a) and len(indexed_b) == len(b)
                      and indexed_a.keys() == indexed_b.keys()
                      and baseline.get("scenario_sha256", {}).get(id) is not None
                      and baseline["scenario_sha256"][id] == candidate.get("scenario_sha256", {}).get(id))
        units = []
        if comparable:
            for key in sorted(indexed_a):
                before, after = indexed_a[key], indexed_b[key]
                units.append({"repeat": key[0], "group": key[1], "step": key[2], "mode": key[3],
                              "before": before["status"], "after": after["status"], "transition": _transition(before, after),
                              "before_codes": sorted(p["code"] for p in before.get("problems", [])),
                              "after_codes": sorted(p["code"] for p in after.get("problems", []))})
        transitions = {unit["transition"] for unit in units}
        transition = ("not_comparable" if not comparable else "regressed" if "regressed" in transitions
                      else "fixed" if _case_status(a) == "failed" and _case_status(b) == "passed"
                      else "changed_evidence" if transitions - {"unchanged"} else "unchanged")
        changes.append({"id": id, "before": _case_status(a) if a else None, "after": _case_status(b) if b else None,
                        "transition": transition, "units": units,
                        "candidate_evidence": [row["evidence"] for row in b if "evidence" in row]})
    invalid_statuses = {"service_invalid", "execution_invalid", "contract_unknown", "unsupported", "not_run", "inconclusive"}
    incomplete = not changes or any(row["transition"] == "not_comparable"
                                  or any(unit["before"] in invalid_statuses or unit["after"] in invalid_statuses for unit in row["units"])
                                  for row in changes)
    comparison_status = "failed" if any(row["transition"] == "regressed" for row in changes) else "incomplete" if incomplete else "passed"
    return {"schema_version": 2, "kind": "report_comparison", "status": comparison_status, "endpoint_requests": 0,
            "baseline_run": baseline["run_dir"], "candidate_run": candidate["run_dir"], "changes": changes,
            "scope": scope, "not_retested": sorted(set(left) - set(right)),
            "retest_case_ids": failed_ids(candidate),
            "warning": "Comparison status is not candidate acceptance. Inspect unit transitions; unchanged failures remain failures. Different target revisions need a new plan. No tests were rerun."}
