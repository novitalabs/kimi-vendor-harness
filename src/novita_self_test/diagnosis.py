from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from .artifacts import SuiteResult, write_json
from .config import ServiceAccess, find_secret


def _boundary(result: SuiteResult) -> str:
    if result.validity == "service_invalid":
        return "service"
    if result.validity == "execution_invalid":
        return "runner"
    return "source_or_model_behavior"


def write_diagnosis(
    run_dir: Path,
    *,
    model_id: str,
    selector: str,
    service: ServiceAccess,
    result: SuiteResult,
) -> None:
    rerun = [
        "nvst",
        "develop",
        "--model",
        model_id,
        "--service-access",
        str(service.source_path),
        "--select",
        selector,
    ]
    source_targets = [target.to_dict() for target in service.source_targets]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "selector": selector,
        "suite_id": result.suite_id,
        "first_divergence": result.failure_summary or result.reason or result.status,
        "boundary": _boundary(result),
        "status": result.status,
        "validity": result.validity,
        "artifacts": result.artifact_paths,
        "suggested_source_targets": source_targets,
        "minimal_rerun": rerun,
    }
    write_json(run_dir / "diagnosis.json", payload)

    target_lines = []
    for target in source_targets:
        logs = ", ".join(target["log_entrypoints"]) or "none declared"
        target_lines.append(
            f"- `{target['repo']}`: `{target['worktree']}` at "
            f"`{target.get('commit') or target.get('branch') or 'unresolved'}`; logs: {logs}"
        )
    artifacts = "\n".join(f"- `{path}`" for path in result.artifact_paths) or "- none"
    markdown = "\n".join(
        [
            "# Development Diagnosis",
            "",
            f"- Selector: `{selector}`",
            f"- Suite: `{result.suite_id}`",
            f"- Status: `{result.status}`",
            f"- Validity: `{result.validity}`",
            f"- Boundary: `{payload['boundary']}`",
            f"- First divergence: {payload['first_divergence']}",
            "",
            "## Artifacts",
            "",
            artifacts,
            "",
            "## Suggested source checks",
            "",
            "\n".join(target_lines) or "- no source targets declared",
            "",
            "## Minimal rerun",
            "",
            f"`{shlex.join(rerun)}`",
            "",
        ]
    )
    if find_secret(markdown):
        raise ValueError("diagnosis rendering unexpectedly contained credential material")
    (run_dir / "diagnosis.md").write_text(markdown, encoding="utf-8")
    reproduce = run_dir / "reproduce.sh"
    reproduce.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + shlex.join(rerun) + "\n", encoding="utf-8")
    os.chmod(reproduce, 0o755)

