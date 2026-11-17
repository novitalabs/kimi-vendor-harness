from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .artifact_bundle import write_artifact_bundle
from .artifacts import SuiteResult, utc_now, write_json
from .config import ConfigError, ModelProfile

STATUS_SEVERITY = {
    "passed": 0,
    "not_implemented": 1,
    "deferred": 2,
    "running": 2,
    "pausing": 3,
    "paused": 3,
    "interrupted": 3,
    "not_run": 3,
    "blocked": 4,
    "failed": 5,
}
VALIDITY_SEVERITY = {"valid": 0, "service_invalid": 1, "execution_invalid": 2}


def _difference(row: dict[str, Any], results: list[SuiteResult]) -> Any:
    official = row.get("official_result") or {}
    expected = official.get("value") if isinstance(official, dict) else None
    values = [item.metrics.get("score") for item in results if "score" in item.metrics]
    if isinstance(expected, (int, float)) and values and isinstance(values[0], (int, float)):
        return round(values[0] - expected, 8)
    if (
        isinstance(expected, list)
        and expected
        and all(isinstance(item, (int, float)) for item in expected)
        and values
        and isinstance(values[0], (int, float))
    ):
        nearest = min(expected, key=lambda item: abs(values[0] - item))
        return round(values[0] - nearest, 8)
    return None


def _actual(results: list[SuiteResult]) -> Any:
    if not results:
        return None
    if len(results) == 1:
        if results[0].metrics and results[0].counts:
            return {"counts": results[0].counts, "metrics": results[0].metrics}
        if results[0].metrics:
            return results[0].metrics
        if results[0].counts:
            return results[0].counts
        return None
    return [
        {"suite_id": item.suite_id, "counts": item.counts, "metrics": item.metrics}
        for item in results
    ]


def _row_result(
    section: str,
    row: dict[str, Any],
    suite_results: list[SuiteResult],
) -> dict[str, Any]:
    row_id = row["row_id"]
    matches = [item for item in suite_results if row_id in item.row_ids]
    if matches:
        status = max(matches, key=lambda item: STATUS_SEVERITY[item.status]).status
        validity = max(matches, key=lambda item: VALIDITY_SEVERITY[item.validity]).validity
        reasons = [item.reason or item.failure_summary for item in matches if item.reason or item.failure_summary]
        reason = "; ".join(reasons) if reasons else None
    else:
        status = str(row.get("always_status") or "not_run")
        validity = "valid"
        reason = row.get("reason") or "no_suite_result"
    artifacts = []
    for item in matches:
        for artifact in item.artifact_paths:
            absolute = str(Path(artifact).resolve())
            if absolute not in artifacts:
                artifacts.append(absolute)
    return {
        "row_id": row_id,
        "section": section,
        "status": status,
        "validity": validity,
        "requirement": row.get("requirement_zh") or row.get("job"),
        "test_file": row.get("test_file"),
        "parameter": row.get("parameter"),
        "actual": _actual(matches),
        "official": row.get("official_result"),
        "official_raw": row.get("official_result_raw"),
        "difference": _difference(row, matches),
        "runner_pin": [item.runner_fingerprint for item in matches],
        "artifact_paths": artifacts,
        "not_run_reason": reason if status != "passed" else None,
    }


def build_report(
    profile: ModelProfile,
    suite_results: list[SuiteResult],
    run_manifest: dict[str, Any],
) -> dict[str, Any]:
    official_rows = [
        _row_result(section, row, suite_results) for section, row in profile.official_rows()
    ]
    supplementary = [
        _row_result(section, row, suite_results) for section, row in profile.supplementary_rows()
    ]
    selected_mandatory = [
        state
        for state in run_manifest.get("suite_states", [])
        if state.get("mandatory") and state.get("selected", True)
    ]
    validities = {state.get("validity") for state in selected_mandatory}
    statuses = {state.get("status") for state in selected_mandatory}
    if "service_invalid" in validities:
        verdict = "service_invalid"
    elif any(
        state.get("status") == "failed" and state.get("validity") == "valid"
        for state in selected_mandatory
    ):
        # A valid benchmark failure is a definitive acceptance failure even
        # when later suites were materialized as blocked by fail-fast policy.
        verdict = "failed"
    elif (
        not selected_mandatory
        or "execution_invalid" in validities
        or statuses & {
            "blocked",
            "not_run",
            "not_implemented",
            "deferred",
            "running",
            "pausing",
            "paused",
            "interrupted",
        }
    ):
        verdict = "inconclusive"
    elif all(state.get("status") == "passed" for state in selected_mandatory):
        verdict = "passed"
    else:
        verdict = "failed"
    contract_invalid = verdict in {"inconclusive", "service_invalid"}
    return {
        "schema_version": 1,
        "run_id": run_manifest["run_id"],
        "model": profile.model_id,
        # The family alone does not identify the scored deployment; a submission
        # must state which served model the numbers belong to.
        "served_model": run_manifest.get("served_model"),
        "started_at": run_manifest.get("started_at"),
        "finished_at": run_manifest.get("finished_at"),
        "acceptance_mode": run_manifest.get("acceptance_mode"),
        "requirements_snapshot_revision": profile.snapshot_revision,
        "generated_at": utc_now(),
        "verdict": verdict,
        "contract_invalid": contract_invalid,
        "official_row_count": len(official_rows),
        "supplementary_row_count": len(supplementary),
        "official_rows": official_rows,
        "supplementary": supplementary,
    }


def render_markdown(report: dict[str, Any], profile: ModelProfile) -> str:
    columns = profile.profile.get("reporting", {}).get("columns") or [
        "row_id",
        "status",
        "actual",
        "official",
        "difference",
        "runner_pin",
        "artifact_paths",
        "not_run_reason",
    ]
    labels = {column: column for column in columns}
    lines = [
        f"# {profile.profile.get('display_name', profile.model_id)} Acceptance",
        "",
        f"Acceptance mode: **{report.get('acceptance_mode') or 'unknown'}**",
        "",
        f"Served model: **{report.get('served_model') or 'unrecorded'}**"
        f" (profile family `{report.get('model')}`)",
        "",
        f"Run window: {report.get('started_at') or 'unknown'} → "
        f"{report.get('finished_at') or 'in progress'}",
        "",
        f"Final verdict: **{report['verdict']}**",
        "",
    ]
    sections = profile.profile["reporting"]["sections"]
    for section in sections:
        rows = [row for row in report["official_rows"] if row["section"] == section]
        lines.extend([f"## {section}", ""])
        lines.append("| " + " | ".join(labels[column] for column in columns) + " |")
        lines.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                text = "" if value is None else str(value)
                values.append(text.replace("|", "\\|").replace("\n", " "))
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")
    if report["supplementary"]:
        lines.extend(["## supplementary", "", "| row_id | status | detail |", "| --- | --- | --- |"])
        for row in report["supplementary"]:
            lines.append(
                f"| {row['row_id']} | {row['status']} | "
                f"{str(row.get('not_run_reason') or row.get('actual') or '').replace('|', '\\|')} |"
            )
        lines.append("")
    return "\n".join(lines)


def _delivery_suite_by_row(profile: ModelProfile) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for suite in profile.suites:
        for row_id in suite.row_ids:
            result[str(row_id)] = suite
    return result


def _delivery_actual(row: dict[str, Any]) -> str:
    actual = row.get("actual")
    if actual is None:
        return "—"
    if isinstance(actual, dict):
        counts = actual.get("counts") or actual
        metrics = actual.get("metrics") or {}
        if isinstance(metrics.get("score"), (int, float)):
            return f"score={float(metrics['score']):.4f}"
        if counts:
            passed = counts.get("passed")
            total = counts.get("total")
            if passed is not None and total is not None:
                return f"{passed}/{total} passed"
            if counts.get("cases") is not None and counts.get("runs_observed") is not None:
                return f"{counts['runs_observed']}/{counts['runs_requested']} runs, {counts['cases']} cases"
            return json.dumps(counts, ensure_ascii=False, sort_keys=True)
        return json.dumps(metrics, ensure_ascii=False, sort_keys=True) if metrics else "—"
    return json.dumps(actual, ensure_ascii=False, sort_keys=True) if isinstance(actual, (dict, list)) else str(actual)


def render_delivery_markdown(
    report: dict[str, Any],
    profile: ModelProfile,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Render a profile-driven cross-run delivery report.

    ``build_report`` owns result aggregation and verdict semantics. This layer
    only adds delivery scope, optional-suite disposition, operator exceptions,
    supplemental notes, and evidence paths. No model id is interpreted here.
    """
    metadata = metadata or {}
    suite_by_row = _delivery_suite_by_row(profile)
    excluded = {str(item) for item in metadata.get("excluded_row_ids") or []}
    evidence_paths = {str(key): str(value) for key, value in (metadata.get("evidence_paths") or {}).items()}
    rows = list(report.get("official_rows") or []) + list(report.get("supplementary") or [])
    title = metadata.get("title") or f"{profile.profile.get('display_name', profile.model_id)} Acceptance Evidence Report"
    verdict = metadata.get("verdict") or report.get("verdict") or "unknown"
    lines = [f"# {title}", "", "## Overall Disposition", ""]
    lines.append(f"- **Verdict**: `{verdict}`")
    lines.append(f"- **Profile**: `{profile.model_id}`")
    if report.get("served_model"):
        lines.append(f"- **Served model**: `{report['served_model']}`")
    if report.get("requirements_snapshot_revision"):
        lines.append(f"- **Requirements snapshot**: `{report['requirements_snapshot_revision']}`")
    for item in metadata.get("scope_notes") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Acceptance Matrix", "", "| Row | Requirement | Status | Result | Disposition | Evidence |", "|---|---|---|---|---|---|"])
    for row in rows:
        row_id = str(row["row_id"])
        suite = suite_by_row.get(row_id)
        optional = suite is not None and not suite.mandatory
        status = str(row.get("status") or "not_run")
        if row_id in excluded:
            status = "optional / not run in scope"
            disposition = "optional; does not gate this acceptance"
        elif optional and status in {"not_run", "blocked"}:
            status = "optional / not run"
            disposition = "optional; does not gate this acceptance"
        elif row_id in {str(item) for item in metadata.get("non_blocking_row_ids") or []}:
            disposition = "non-blocking operator exception"
        elif status == "passed":
            disposition = "required item passed"
        elif row_id in {str(item) for item in metadata.get("supplemental_row_ids") or []}:
            disposition = "required item; supplemental evidence does not rewrite formal verdict"
        else:
            disposition = "required item incomplete or failed"
        evidence = evidence_paths.get(row_id)
        if evidence:
            evidence_label = "suite result"
            evidence_target = evidence
        else:
            evidence_label = "remote evidence"
            evidence_target = "remote-evidence-index.json"
        lines.append(
            f"| {row_id} | {str(row.get('requirement') or '—').replace('|', '\\|')} | "
            f"{status} | {_delivery_actual(row)} | {disposition} | "
            f"[{evidence_label}]({evidence_target}) |"
        )
    exceptions = metadata.get("known_non_blocking") or []
    if exceptions:
        lines.extend(["", "## Known Non-Blocking Exceptions", "", "| Row | Case | Problem | Handling |", "|---|---|---|---|"])
        for item in exceptions:
            lines.append(
                f"| {item.get('row_id', '—')} | `{item.get('case', '—')}` | "
                f"{item.get('problem', '—')} | {item.get('handling', 'non-blocking operator exception')} |"
            )
    supplemental = metadata.get("supplemental_sections") or []
    for section in supplemental:
        lines.extend(["", f"## {section.get('title', 'Supplemental Evidence')}", ""])
        for paragraph in section.get("paragraphs") or []:
            lines.extend([str(paragraph), ""])
        for item in section.get("bullets") or []:
            lines.append(f"- {item}")
        lines.append("")
    pending = metadata.get("pending") or []
    if pending:
        lines.extend(["## Optional / Pending Items", "", "| Item | Status | Note |", "|---|---|---|"])
        for item in pending:
            lines.append(f"| {item.get('item', '—')} | {item.get('status', '—')} | {item.get('note', '—')} |")
    lines.extend(["", "## Evidence Policy", "", "Raw request/response evidence, logs, checkpoints, and large intermediate artifacts remain on the canonical build-server. The Mac package retains normalized suite summaries and a remote evidence index.", ""])
    return "\n".join(lines)


def write_delivery_report(
    path: Path,
    report: dict[str, Any],
    profile: ModelProfile,
    *,
    metadata: dict[str, Any] | None = None,
) -> str:
    markdown = render_delivery_markdown(report, profile, metadata=metadata)
    path.write_text(markdown, encoding="utf-8")
    return markdown


def write_report(
    run_dir: Path,
    profile: ModelProfile,
    suite_results: list[SuiteResult],
    run_manifest: dict[str, Any],
) -> dict[str, Any]:
    report = build_report(profile, suite_results, run_manifest)
    write_json(run_dir / "acceptance.json", report)
    (run_dir / "acceptance.md").write_text(render_markdown(report, profile), encoding="utf-8")
    return report


def load_suite_results(run_dir: Path) -> list[SuiteResult]:
    results = []
    for path in sorted((run_dir / "suites").glob("*/suite-result.json")):
        results.append(SuiteResult.load(path))
    return results


def regenerate_report(run_dir: Path, *, root: Path | None = None) -> dict[str, Any]:
    try:
        manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ConfigError("run_manifest_invalid", f"cannot load run.json: {exc}") from exc
    profile = ModelProfile.load(manifest.get("model", ""), root=root)
    if manifest.get("requirements_snapshot_revision") != profile.snapshot_revision:
        raise ConfigError(
            "requirements_revision_mismatch",
            "run is pinned to a different requirements revision than the checkout",
        )
    if manifest.get("profile_hashes") is not None:
        from .orchestrator import _verify_run_environment

        _verify_run_environment(manifest, profile, (root or profile.directory.parents[1]).resolve())
    results = load_suite_results(run_dir)
    report = write_report(run_dir, profile, results, manifest)
    write_artifact_bundle(
        run_dir,
        suite_results=results,
        run_manifest=manifest,
        report=report,
    )
    return report
