from __future__ import annotations

import json
from pathlib import Path

from novita_self_test.collection import collect_runs, write_collection


def _write_run(
    runs_root: Path,
    family: str,
    run_id: str,
    *,
    served_model: str | None,
    started_at: str,
    suites: list[tuple[str, str, str, dict]] | None = None,
) -> Path:
    run_dir = runs_root / family / run_id
    (run_dir / "suites").mkdir(parents=True)
    manifest = {
        "schema_version": 2,
        "run_id": run_id,
        "model": family,
        "started_at": started_at,
        "finished_at": started_at,
        "acceptance_mode": "develop",
        "selectors": [],
    }
    if served_model is not None:
        manifest["served_model"] = served_model
    (run_dir / "run.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "acceptance.json").write_text(json.dumps({"verdict": "passed"}), encoding="utf-8")
    for execution_id, suite_id, row_id, result in suites or []:
        suite_dir = run_dir / "suites" / execution_id
        suite_dir.mkdir(parents=True)
        # Observed service identity lives in suite-input.json, which is how the
        # older runs recorded it before run.json carried served_model.
        (suite_dir / "suite-input.json").write_text(
            json.dumps(
                {
                    "suite_id": suite_id,
                    "row_ids": [row_id],
                    "service": {"served_model": served_model} if served_model else {},
                }
            ),
            encoding="utf-8",
        )
        (suite_dir / "suite-result.json").write_text(
            json.dumps({"suite_id": suite_id, "row_ids": [row_id], **result}),
            encoding="utf-8",
        )
    return run_dir


def test_collect_groups_by_served_model_and_orders_by_time(tmp_path: Path) -> None:
    """Two deployments of one catalogue must never be merged into one score."""
    runs_root = tmp_path / "runs"
    _write_run(
        runs_root,
        "kimi-k3",
        "run-later",
        served_model="kimi-k3-flash",
        started_at="2026-09-12T12:54:26+00:00",
        suites=[("001-toolcall-arguments-recovery", "toolcall-arguments-recovery", "18",
                 {"status": "failed", "validity": "execution_invalid", "metrics": {}})],
    )
    _write_run(
        runs_root,
        "kimi-k3",
        "run-earlier",
        served_model="moonshotai/kimi-k3-h",
        started_at="2026-09-11T16:06:08+00:00",
        suites=[("001-deepswe", "deepswe", "17",
                 {"status": "failed", "validity": "valid", "metrics": {"score": 0.5841}})],
    )

    index = collect_runs(runs_root, model_family="kimi-k3")

    assert index["run_count"] == 2
    by_model = {item["served_model"]: item for item in index["deployments"]}
    assert set(by_model) == {"kimi-k3-flash", "moonshotai/kimi-k3-h"}
    assert by_model["moonshotai/kimi-k3-h"]["runs"][0]["run_id"] == "run-earlier"
    assert by_model["kimi-k3-flash"]["runs"][0]["rows"][0]["row_ids"] == ["18"]


def test_collect_falls_back_to_suite_input_when_manifest_lacks_served_model(
    tmp_path: Path,
) -> None:
    """Runs written before served_model existed still resolve correctly."""
    runs_root = tmp_path / "runs"
    _write_run(
        runs_root,
        "kimi-k3",
        "legacy",
        served_model="moonshotai/kimi-k3-h",
        started_at="2026-09-10T07:41:12+00:00",
        suites=[("004-inspect-ocr-low", "inspect-ocr-low", "10",
                 {"status": "passed", "validity": "valid", "metrics": {"score": 0.897}})],
    )
    manifest_path = runs_root / "kimi-k3" / "legacy" / "run.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.pop("served_model")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    index = collect_runs(runs_root)

    assert index["deployments"][0]["served_model"] == "moonshotai/kimi-k3-h"
    assert index["deployments"][0]["runs"][0]["rows"][0]["score"] == 0.897


def test_collect_reports_unresolved_when_nothing_records_the_served_model(
    tmp_path: Path,
) -> None:
    """An unattributable run is isolated, never folded into a real deployment."""
    runs_root = tmp_path / "runs"
    _write_run(
        runs_root,
        "kimi-k3",
        "unknown",
        served_model=None,
        started_at="2026-09-01T00:00:00+00:00",
        suites=[("001-walle", "walle", "4", {"status": "passed", "validity": "valid", "metrics": {}})],
    )

    index = collect_runs(runs_root)

    assert index["deployments"][0]["served_model"] == "unresolved"


def test_collect_filters_by_since_and_served_model(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    _write_run(
        runs_root, "kimi-k3", "old", served_model="moonshotai/kimi-k3-h",
        started_at="2026-09-01T00:00:00+00:00",
        suites=[("001-walle", "walle", "4", {"status": "passed", "validity": "valid", "metrics": {}})],
    )
    _write_run(
        runs_root, "kimi-k3", "new", served_model="moonshotai/kimi-k3-h",
        started_at="2026-09-11T16:06:08+00:00",
        suites=[("001-walle", "walle", "4", {"status": "passed", "validity": "valid", "metrics": {}})],
    )

    index = collect_runs(runs_root, since="2026-09-11T00:00:00+00:00")

    assert index["run_count"] == 1
    assert index["deployments"][0]["runs"][0]["run_id"] == "new"


def test_write_collection_stages_bundles_under_model_and_time(tmp_path: Path) -> None:
    """The delivered package is laid out on the same axis as the index."""
    runs_root = tmp_path / "runs"
    run_dir = _write_run(
        runs_root, "kimi-k3", "run-1", served_model="moonshotai/kimi-k3-h",
        started_at="2026-09-11T16:06:08+00:00",
        suites=[("001-deepswe", "deepswe", "17",
                 {"status": "failed", "validity": "valid", "metrics": {"score": 0.5841}})],
    )
    bundle = run_dir / "test-artifacts"
    bundle.mkdir()
    (bundle / "bundle.json").write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")

    out_dir = tmp_path / "package"
    index = write_collection(
        runs_root, out_dir, model_family="kimi-k3", copy_bundles=True
    )

    assert (out_dir / "index.json").is_file()
    assert (out_dir / "index.md").is_file()
    assert index["staged_bundles"][0]["served_model"] == "moonshotai/kimi-k3-h"
    staged = out_dir / "moonshotai_kimi-k3-h" / "20260911160608-run-1"
    assert (staged / "bundle.json").is_file()
    markdown = (out_dir / "index.md").read_text()
    assert "moonshotai/kimi-k3-h" in markdown
    assert "deepswe" in markdown
