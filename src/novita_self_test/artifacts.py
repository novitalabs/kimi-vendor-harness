from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import STATUSES, VALIDITIES, ConfigError, find_secret
from .schema_validation import validation_error


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def artifact_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(lock_path.resolve())
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def writer_identity() -> str:
    return f"pid:{os.getpid()}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_json_payload(payload: dict[str, Any], schema_name: str | None) -> None:
    secret = find_secret(payload)
    if secret:
        raise ConfigError("credential_material_forbidden", secret)
    if schema_name:
        schema_error = validation_error(payload, schema_name)
        if schema_error:
            raise ConfigError("artifact_schema_invalid", schema_error)


def _write_json_unlocked(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(
    path: Path,
    payload: dict[str, Any],
    *,
    schema_name: str | None = None,
) -> None:
    with artifact_lock(path):
        _validate_json_payload(payload, schema_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def write_json(
    path: Path, payload: dict[str, Any], *, schema_name: str | None = None
) -> None:
    if schema_name is None and path.name == "run.json" and payload.get("schema_version") == 2:
        schema_name = "run.schema.json"
    with artifact_lock(path):
        if schema_name == "run.schema.json":
            current_revision = 0
            if path.is_file():
                try:
                    current_revision = int(
                        json.loads(path.read_text(encoding="utf-8")).get("revision") or 0
                    )
                except (json.JSONDecodeError, ValueError, TypeError):
                    current_revision = 0
            payload["revision"] = max(current_revision, int(payload.get("revision") or 0)) + 1
            payload["updated_by"] = writer_identity()
        _validate_json_payload(payload, schema_name)
        _write_json_unlocked(path, payload)


def write_versioned_json(
    path: Path,
    payload: dict[str, Any],
    *,
    schema_name: str,
) -> dict[str, Any]:
    with artifact_lock(path):
        current: dict[str, Any] = {}
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError("artifact_json_invalid", f"invalid JSON in {path}: {exc}") from exc
        rendered = dict(payload)
        rendered["revision"] = int(current.get("revision") or 0) + 1
        rendered["updated_by"] = writer_identity()
        _validate_json_payload(rendered, schema_name)
        _write_json_unlocked(path, rendered)
        payload.update({"revision": rendered["revision"], "updated_by": rendered["updated_by"]})
        return rendered


def update_versioned_json(
    path: Path,
    updates: dict[str, Any],
    *,
    schema_name: str,
) -> dict[str, Any]:
    with artifact_lock(path):
        current: dict[str, Any] = {}
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError("artifact_json_invalid", f"invalid JSON in {path}: {exc}") from exc
        current.update(updates)
        current["revision"] = int(current.get("revision") or 0) + 1
        current["updated_by"] = writer_identity()
        _validate_json_payload(current, schema_name)
        _write_json_unlocked(path, current)
        return current


@dataclass
class SuiteResult:
    suite_id: str
    row_ids: list[str]
    status: str
    validity: str = "valid"
    counts: dict[str, int] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    redacted_command: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    failure_summary: str | None = None
    runner_fingerprint: dict[str, str] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def validate(self) -> None:
        if not self.suite_id:
            raise ConfigError("suite_result_invalid", "suite_id is required")
        if self.status not in STATUSES:
            raise ConfigError("suite_result_invalid", f"invalid suite status: {self.status}")
        if self.validity not in VALIDITIES:
            raise ConfigError("suite_result_invalid", f"invalid suite validity: {self.validity}")
        if not all(isinstance(item, str) for item in self.row_ids):
            raise ConfigError("suite_result_invalid", "row_ids must be strings")
        secret = find_secret(self.to_dict())
        if secret:
            raise ConfigError("credential_material_forbidden", secret)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "suite_id": self.suite_id,
            "row_ids": self.row_ids,
            "status": self.status,
            "validity": self.validity,
            "counts": self.counts,
            "metrics": self.metrics,
            "redacted_command": self.redacted_command,
            "artifact_paths": self.artifact_paths,
            "failure_summary": self.failure_summary,
            "runner_fingerprint": self.runner_fingerprint,
            "coverage": self.coverage,
            "reason": self.reason,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuiteResult":
        result = cls(
            suite_id=data.get("suite_id", ""),
            row_ids=list(data.get("row_ids") or []),
            status=data.get("status", ""),
            validity=data.get("validity", "valid"),
            counts=dict(data.get("counts") or {}),
            metrics=dict(data.get("metrics") or {}),
            redacted_command=list(data.get("redacted_command") or []),
            artifact_paths=list(data.get("artifact_paths") or []),
            failure_summary=data.get("failure_summary"),
            runner_fingerprint=dict(data.get("runner_fingerprint") or {}),
            coverage=dict(data.get("coverage") or {}),
            reason=data.get("reason"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
        )
        result.validate()
        return result

    @classmethod
    def load(cls, path: Path) -> "SuiteResult":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ConfigError("suite_result_invalid", f"cannot load {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError("suite_result_invalid", f"{path} must contain a JSON object")
        schema_error = validation_error(data, "suite-result.schema.json")
        if schema_error:
            raise ConfigError("suite_result_invalid", schema_error)
        return cls.from_dict(data)

    def write(self, path: Path) -> None:
        result_path = str(path.resolve())
        if result_path not in self.artifact_paths:
            self.artifact_paths.append(result_path)
        self.validate()
        write_json(path, self.to_dict(), schema_name="suite-result.schema.json")


def _same_members(expected: list[Any], observed: Any) -> bool:
    return isinstance(observed, list) and len(observed) == len(expected) and set(observed) == set(expected)


def enforce_completeness(result: SuiteResult, contract: dict[str, Any]) -> SuiteResult:
    if result.status != "passed":
        return result
    failures: list[str] = []
    expected_cases = contract.get("expected_cases")
    if isinstance(expected_cases, list) and not _same_members(
        expected_cases, result.coverage.get("observed_cases")
    ):
        failures.append(
            f"expected cases {len(expected_cases)}, observed "
            f"{len(result.coverage.get('observed_cases') or [])}"
        )
    expected_spec_items = contract.get("expected_spec_items")
    if isinstance(expected_spec_items, list) and not _same_members(
        expected_spec_items, result.coverage.get("observed_spec_items")
    ):
        failures.append(
            f"expected spec items {len(expected_spec_items)}, observed "
            f"{len(result.coverage.get('observed_spec_items') or [])}"
        )
    expected_counts = contract.get("expected_counts")
    if isinstance(expected_counts, dict):
        mismatches = {
            key: (value, result.counts.get(key))
            for key, value in expected_counts.items()
            if result.counts.get(key) != value
        }
        if mismatches:
            failures.append(f"count mismatch: {mismatches}")
    for key in ("tasks", "attempts", "trials", "max_images", "repeats", "rounds", "max_reruns"):
        expected = contract.get(f"expected_{key}")
        if expected is not None and result.coverage.get(f"observed_{key}") != expected:
            failures.append(
                f"expected {key} {expected}, observed {result.coverage.get(f'observed_{key}')}"
            )
    required_artifacts = contract.get("required_artifacts") or []
    missing_artifacts = [
        path for path in required_artifacts if not any(Path(item).name == path for item in result.artifact_paths)
    ]
    if missing_artifacts:
        failures.append(f"missing artifacts: {', '.join(missing_artifacts)}")
    required_patterns = contract.get("required_artifact_patterns") or []
    missing_patterns = [
        pattern
        for pattern in required_patterns
        if not any(
            Path(item).match(str(pattern)) or Path(item).name == str(pattern)
            for item in result.artifact_paths
        )
    ]
    if missing_patterns:
        failures.append(f"missing artifact patterns: {', '.join(missing_patterns)}")
    if failures:
        result.status = "failed"
        result.validity = "execution_invalid"
        result.reason = "artifact_contract_incomplete"
        result.failure_summary = "; ".join(failures)
    return result
