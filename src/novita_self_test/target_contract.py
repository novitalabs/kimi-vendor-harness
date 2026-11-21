"""Target obligations describe expected behavior, never the engine's progress."""
from __future__ import annotations

from collections import Counter
from typing import Any


def claim(profile: dict, group: str, name: str) -> str:
    return profile[group].get(name, {"expectation": "unknown"})["expectation"]


def applicability(profile: dict, required: set[str], efforts: set[str]) -> tuple[list[str], str]:
    missing, optional = [], False
    for prefix, group, names in (("feature", "features", required), ("effort", "efforts", efforts)):
        for name in sorted(names):
            state = claim(profile, group, name)
            if state in {"unknown", "unsupported"}:
                missing.append(f"{prefix}:{name}:{state}")
            optional |= state == "optional"
    # Stream parser behavior is a selected protocol, not a guessed generic dialect.
    if profile["authority"]["stream_protocol"] is None:
        missing.append("contract:stream_protocol:unknown")
    if profile["authority"]["api_contract"] is None or profile["status"] == "provisional":
        missing.append("contract:target_contract:unknown")
    obligation = "unknown" if any(value.endswith(":unknown") for value in missing) else "not_applicable" if missing else "optional" if optional else "required"
    return missing, obligation


def gate_summary(cases: list[dict], results: list[dict], repeat: int) -> dict[str, Any]:
    mandatory = [case for case in cases if case.get("role", "gate") == "gate" and case.get("obligation", "required") in {"required", "unknown"}]
    by_id = {}
    for row in results:
        by_id.setdefault(row["id"], []).append(row)
    failures, gaps = [], []
    for case in mandatory:
        rows = by_id.get(case["id"], [])
        expected = case.get("request_budget", len(case["steps"]) * len(case["modes"])) * repeat
        if any(row["status"] == "failed" for row in rows):
            failures.append(case["id"])
        elif len(rows) != expected or any(row["status"] != "passed" for row in rows):
            gaps.append(case["id"])
    return {"status": "failed" if failures else "incomplete" if gaps else "passed" if mandatory else "not_selected",
            "required_cases": len(mandatory), "failures": failures, "gaps": gaps,
            "result_counts": dict(sorted(Counter(row["status"] for row in results).items())),
            "role_counts": dict(sorted(Counter(case.get("role", "gate") for case in cases).items())),
            "scope": "Selected mandatory cases only; not complete model support or acceptance."}
