from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


_PATHS: dict[str, Path] | None = None
_RECORDS: list[dict] = []
_STARTED_AT: float | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--artifact-dir", default=os.environ.get("K3_ARTIFACT_DIR", ""))


def pytest_configure(config: pytest.Config) -> None:
    global _PATHS, _STARTED_AT
    raw = str(config.getoption("artifact_dir") or "").strip()
    if not raw:
        return
    root = Path(raw).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _PATHS = {"cases": root / "cases.jsonl", "summary": root / "summary.json"}
    _STARTED_AT = time.time()


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if _PATHS is None:
        return
    if report.when == "setup" and not (report.skipped or report.failed):
        return
    if report.when == "teardown" and not report.failed:
        return
    if report.when not in {"setup", "call", "teardown"}:
        return
    _RECORDS.append(
        {
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "phase": report.when,
            "duration_s": round(report.duration, 6),
            "longrepr": "" if report.longrepr is None else str(report.longrepr)[:4096],
        }
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int):
    yield
    if _PATHS is None:
        return
    counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "other": 0}
    for record in _RECORDS:
        outcome = record["outcome"]
        counts[outcome] = counts.get(outcome, 0) + 1
    _PATHS["cases"].write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in _RECORDS),
        encoding="utf-8",
    )
    _PATHS["summary"].write_text(
        json.dumps(
            {
                "artifact_dir": str(_PATHS["cases"].parent),
                "exitstatus": exitstatus,
                "started_at": _STARTED_AT,
                "finished_at": time.time(),
                "counts": counts,
                "total_cases": len(_RECORDS),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
