"""Collect run artifacts along the served-model x test-time axis.

A run directory holds one execution of one suite plan against one deployment.
Consolidating several runs by hand is error-prone: the profile family alone
(``kimi-k3``) does not identify what was scored, because a single K3 catalogue
serves both ``moonshotai/kimi-k3-h`` and ``kimi-k3-flash``. This module groups
the committed runs by the observed served model, orders them by the run start
time, and emits a machine-readable index plus a readable report.

Row order comes from the run's own suite directories (``NNN-<suite_id>``),
which the orchestrator already writes in profile order, so this module never
recreates a second test matrix.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

INDEX_SCHEMA_VERSION = 1
_UNRESOLVED = "unresolved"


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _suite_dirs(run_dir: Path) -> list[Path]:
    suites = run_dir / "suites"
    if not suites.is_dir():
        return []
    return sorted(path for path in suites.iterdir() if path.is_dir())


def _execution_order(execution_id: str) -> tuple[int, str]:
    """Profile order is the orchestrator's numeric directory prefix."""
    found = re.match(r"^(\d+)", execution_id)
    return (int(found.group(1)) if found else 10**9, execution_id)


def _served_model(run_manifest: dict[str, Any], suite_dirs: list[Path]) -> str:
    """Prefer the run manifest; fall back to what the suites recorded."""
    declared = run_manifest.get("served_model")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    observed: set[str] = set()
    for suite_dir in suite_dirs:
        payload = _load_json_object(suite_dir / "suite-input.json")
        model = (payload.get("service") or {}).get("served_model")
        if isinstance(model, str) and model.strip():
            observed.add(model.strip())
    if len(observed) == 1:
        return observed.pop()
    if len(observed) > 1:
        return "mixed:" + ",".join(sorted(observed))
    return _UNRESOLVED


def _suite_row(suite_dir: Path) -> dict[str, Any]:
    result = _load_json_object(suite_dir / "suite-result.json")
    input_payload = _load_json_object(suite_dir / "suite-input.json")
    row_ids = result.get("row_ids") or input_payload.get("row_ids") or []
    metrics = dict(result.get("metrics") or {})
    return {
        "execution_id": suite_dir.name,
        "suite_id": result.get("suite_id") or suite_dir.name.split("-", 1)[-1],
        "row_ids": list(row_ids),
        "status": result.get("status"),
        "validity": result.get("validity"),
        "score": metrics.get("score"),
        "metrics": metrics,
        "reason": result.get("reason"),
        "artifact_dir": str(suite_dir.resolve()),
    }


def _run_entry(run_dir: Path) -> dict[str, Any]:
    run_manifest = _load_json_object(run_dir / "run.json")
    suite_dirs = _suite_dirs(run_dir)
    rows = [_suite_row(suite_dir) for suite_dir in suite_dirs]
    rows.sort(key=lambda item: _execution_order(str(item["execution_id"])))
    acceptance = _load_json_object(run_dir / "acceptance.json")
    bundle = run_dir / "test-artifacts" / "bundle.json"
    return {
        "run_id": run_manifest.get("run_id") or run_dir.name,
        "run_dir": str(run_dir.resolve()),
        "model_family": run_manifest.get("model"),
        "served_model": _served_model(run_manifest, suite_dirs),
        "started_at": run_manifest.get("started_at"),
        "finished_at": run_manifest.get("finished_at"),
        "acceptance_mode": run_manifest.get("acceptance_mode"),
        "selectors": list(run_manifest.get("selectors") or []),
        "verdict": acceptance.get("verdict"),
        "bundle_available": bundle.is_file(),
        "bundle_path": str(bundle.resolve()) if bundle.is_file() else None,
        "rows": rows,
    }


def collect_runs(
    runs_root: Path,
    *,
    model_family: str | None = None,
    served_model: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Index every committed run under ``runs_root`` by served model and time."""
    runs_root = runs_root.resolve()
    entries: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return {
            "schema_version": INDEX_SCHEMA_VERSION,
            "model_family": model_family,
            "served_model_filter": served_model,
            "since": since,
            "deployment_count": 0,
            "run_count": 0,
            "deployments": [],
        }
    for model_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        if model_family and model_dir.name != model_family:
            continue
        for run_dir in sorted(path for path in model_dir.iterdir() if path.is_dir()):
            if not (run_dir / "run.json").is_file():
                continue
            entry = _run_entry(run_dir)
            if served_model and entry["served_model"] != served_model:
                continue
            if since and str(entry.get("started_at") or "") < since:
                continue
            entries.append(entry)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        grouped.setdefault(str(entry["served_model"]), []).append(entry)

    deployments: list[dict[str, Any]] = []
    for name in sorted(grouped):
        runs = sorted(grouped[name], key=lambda item: str(item.get("started_at") or ""))
        statuses = [row["status"] for run in runs for row in run["rows"]]
        deployments.append(
            {
                "served_model": name,
                "run_count": len(runs),
                "row_count": len(statuses),
                "passed_rows": statuses.count("passed"),
                "failed_rows": statuses.count("failed"),
                "first_started_at": runs[0].get("started_at") if runs else None,
                "last_started_at": runs[-1].get("started_at") if runs else None,
                "runs": runs,
            }
        )
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "model_family": model_family,
        "served_model_filter": served_model,
        "since": since,
        "deployment_count": len(deployments),
        "run_count": len(entries),
        "deployments": deployments,
    }


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return "%.4f" % value
    if value is None or value == "":
        return "—"
    return str(value)


def render_index_markdown(index: dict[str, Any]) -> str:
    lines: list[str] = []
    family = index.get("model_family") or "(all)"
    lines.append("# Test result index — model × time")
    lines.append("")
    lines.append(
        "Grouped by the **served model** observed at run time, then ordered by the "
        "run start time. The profile family (`%s`) is not the scored system: one K3 "
        "catalogue serves both `moonshotai/kimi-k3-h` and `kimi-k3-flash`." % family
    )
    lines.append("")
    lines.append(
        "Runs: **%d** across **%d** served model(s)."
        % (index.get("run_count", 0), index.get("deployment_count", 0))
    )
    lines.append("")
    for deployment in index.get("deployments", []):
        lines.append("## %s" % deployment["served_model"])
        lines.append("")
        lines.append(
            "- runs: %s · rows: %s (passed %s / failed %s)"
            % (
                deployment["run_count"],
                deployment["row_count"],
                deployment["passed_rows"],
                deployment["failed_rows"],
            )
        )
        lines.append(
            "- window: %s → %s"
            % (_fmt(deployment.get("first_started_at")), _fmt(deployment.get("last_started_at")))
        )
        lines.append("")
        for run in deployment["runs"]:
            lines.append(
                "### %s — %s (%s)"
                % (run.get("started_at") or run["run_id"], run["run_id"], run.get("acceptance_mode"))
            )
            lines.append("")
            lines.append("| order | suite | row | status | score | validity |")
            lines.append("|---|---|---|---|---|---|")
            for order, row in enumerate(run["rows"], start=1):
                lines.append(
                    "| %d | %s | %s | %s | %s | %s |"
                    % (
                        order,
                        row["suite_id"],
                        ",".join(str(item) for item in row["row_ids"]) or "—",
                        _fmt(row["status"]),
                        _fmt(row["score"]),
                        _fmt(row["validity"]),
                    )
                )
            lines.append("")
    if not index.get("deployments"):
        lines.append("_No runs matched the filters._")
        lines.append("")
    return "\n".join(lines) + "\n"


def _deployment_dirname(served_model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", served_model).strip("_") or "unresolved"


def _time_prefix(started_at: Any, run_id: str) -> str:
    compact = re.sub(r"[^0-9]", "", str(started_at or ""))[:14]
    return f"{compact}-{run_id}" if compact else run_id


def write_collection(
    runs_root: Path,
    out_dir: Path,
    *,
    model_family: str | None = None,
    served_model: str | None = None,
    since: str | None = None,
    copy_bundles: bool = False,
) -> dict[str, Any]:
    """Write ``index.json``/``index.md`` and optionally stage each run bundle.

    Copies are laid out as ``<served-model>/<started-at>-<run-id>/`` so the
    package handed to a reviewer is ordered along the same axis as the index.
    """
    index = collect_runs(
        runs_root,
        model_family=model_family,
        served_model=served_model,
        since=since,
    )
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    staged: list[dict[str, Any]] = []
    if copy_bundles:
        for deployment in index["deployments"]:
            target_root = out_dir / _deployment_dirname(deployment["served_model"])
            for run in deployment["runs"]:
                source = Path(run["bundle_path"]) if run.get("bundle_path") else None
                if not source or not source.is_file():
                    continue
                destination = target_root / _time_prefix(run.get("started_at"), run["run_id"])
                if destination.exists():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source.parent, destination)
                staged.append(
                    {
                        "run_id": run["run_id"],
                        "served_model": deployment["served_model"],
                        "started_at": run.get("started_at"),
                        "path": str(destination.relative_to(out_dir).as_posix()),
                    }
                )
    index["staged_bundles"] = staged
    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "index.md").write_text(render_index_markdown(index), encoding="utf-8")
    return index
