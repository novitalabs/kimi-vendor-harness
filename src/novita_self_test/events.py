from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import _validate_json_payload, artifact_lock, writer_identity


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _last_sequence(path: Path) -> int:
    if not path.is_file():
        return 0
    last = ""
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last = line
    if not last:
        return 0
    try:
        return int(json.loads(last).get("sequence") or 0)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def emit_event(
    run_dir: Path,
    event: str,
    *,
    suite_id: str | None = None,
    phase: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = run_dir / "events.jsonl"
    with artifact_lock(path):
        payload = {
            "schema_version": 1,
            "sequence": _last_sequence(path) + 1,
            "timestamp": utc_now(),
            "event": event,
            "run_id": run_dir.name,
            "suite_id": suite_id,
            "phase": phase,
            "data": data or {},
            "writer": writer_identity(),
        }
        _validate_json_payload(payload, "run-event.schema.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return payload
