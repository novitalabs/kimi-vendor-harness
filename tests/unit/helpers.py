from __future__ import annotations

import json
from pathlib import Path

import yaml


def write_model(root: Path, suites: list[dict], *, model_id: str = "test-model") -> Path:
    model = root / "models" / model_id
    requirements = model / "requirements"
    requirements.mkdir(parents=True)
    rows = []
    seen = set()
    for suite in suites:
        for row_id in suite.get("row_ids", []):
            if row_id not in seen:
                rows.append(
                    {
                        "row_id": row_id,
                        "requirement_zh": f"Requirement {row_id}",
                        "official_result_raw": None,
                        "official_result": {"kind": None, "value": None},
                    }
                )
                seen.add(row_id)
    (model / "profile.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "model_id": model_id,
                "display_name": model_id,
                "model_families": [model_id],
                "acceptance_control": {
                    "global_stop": {
                        "validities": ["service_invalid"],
                        "statuses": ["blocked"],
                        "reasons": [
                            "runner_contract_invalid",
                            "artifact_contract_incomplete",
                            "runner_not_found",
                            "safety_failure",
                        ],
                    }
                },
                "reporting": {
                    "sections": ["official"],
                    "columns": [
                        "row_id",
                        "status",
                        "validity",
                        "actual",
                        "official_raw",
                        "artifact_paths",
                        "not_run_reason",
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (requirements / "source.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "status": "ready",
                "snapshot_revision": "test-revision-1",
                "doc_token": "test-doc",
                "doc_revision": 1,
                "attachment_lock": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (requirements / "rows.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "official": rows, "supplementary": []}),
        encoding="utf-8",
    )
    (model / "suite-manifest.yaml").write_text(
        yaml.safe_dump({"schema_version": 1, "suites": suites}, sort_keys=False),
        encoding="utf-8",
    )
    return model


def write_service(root: Path, *, model_id: str = "test-model", extra: dict | None = None) -> Path:
    payload = {
        "model_family": model_id,
        "base_url": "https://vendor.example/v1",
        "served_model": model_id,
        "api_key_env": "TEST_VENDOR_API_KEY",
        "headers": {"X-Route": "test"},
        "think_mode": "opensource",
        "deployment_id": "deployment-test",
        "runtime_id": "runtime-test",
        "source_targets": [
            {
                "repo": "source",
                "worktree": "/tmp/source",
                "branch": "main",
                "commit": "abc123",
                "log_entrypoints": ["logs/service.log"],
            }
        ],
    }
    payload.update(extra or {})
    path = root / "service-access.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path

