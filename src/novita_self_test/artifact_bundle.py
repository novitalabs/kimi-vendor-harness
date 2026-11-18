from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import ConfigError, find_secret


BUNDLE_DIRNAME = "test-artifacts"
_SECRET_NAME_PARTS = (
    "service-access",
    "judge-access",
    ".secret",
    "api-key",
    "apikey",
    "credentials",
)
_TEXT_SUFFIXES = {
    ".body",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".sh",
    ".stderr",
    ".stdout",
    ".tsv",
    ".txt",
}

# The delivery bundle carries the readable, structured evidence a reviewer and
# the vendor need. Bulk machine formats stay on the execution host: a DeepSWE
# `.eval` alone is ~1.6 GB per acceptance and adds nothing a JSON summary does
# not already show. Excluded files are recorded by path and size so the
# server-side original stays traceable.
_DELIVERABLE_SUFFIXES = {
    ".csv",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".sh",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
_NON_DELIVERABLE_SKIP = "non_deliverable_artifact"
_EXCLUDED_SUFFIXES = {
    ".body",
    ".eval",
    ".gz",
    ".tar",
    ".tgz",
    ".zip",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return (cleaned or "artifact")[:96]


def _secret_skip_reason(path: Path) -> str | None:
    lowered = path.name.lower()
    if any(part in lowered for part in _SECRET_NAME_PARTS):
        return "secret_bearing_filename"
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            carry = ""
            for chunk in iter(lambda: handle.read(1024 * 1024), ""):
                text = carry + chunk
                if find_secret(text):
                    return "secret_bearing_content"
                # Keep enough overlap for a credential marker split across chunks.
                carry = text[-256:]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _generated_file_record(
    source: Path,
    destination: Path,
    *,
    kind: str,
) -> dict[str, Any]:
    """Record a file created inside staging without exposing its transient path."""
    return {
        "source": f"generated://{destination.as_posix()}",
        "path": str(destination),
        "kind": kind,
        "exists": True,
        "copied": True,
        "bytes": source.stat().st_size,
        "sha256": _sha256(source),
    }


def _file_record(
    source: Path,
    destination: Path,
    *,
    kind: str,
    skipped: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "source": str(source.resolve()),
        "path": str(destination),
        "kind": kind,
        "exists": source.exists(),
    }
    if skipped:
        record.update({"copied": False, "skipped": skipped})
        return record
    if not source.exists():
        record.update({"copied": False, "missing": "source_not_found"})
        return record
    if not source.is_file():
        record.update({"copied": False, "missing": "source_not_file"})
        return record
    record.update(
        {
            "copied": True,
            "bytes": source.stat().st_size,
            "sha256": _sha256(source),
        }
    )
    return record


def _excluded_record(source: Path, destination: Path, *, kind: str) -> dict[str, Any]:
    """Record a bulky non-deliverable artifact without copying or hashing it."""
    record: dict[str, Any] = {
        "source": str(source.resolve()),
        "path": str(destination),
        "kind": kind,
        "exists": source.exists(),
        "copied": False,
        "skipped": _NON_DELIVERABLE_SKIP,
        "reason": "kept_on_execution_host",
    }
    try:
        record["bytes"] = source.stat().st_size
    except OSError:
        pass
    return record


def _copy_path(
    source: Path,
    destination: Path,
    *,
    kind: str,
    deliverable_only: bool = False,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if source.is_dir():
        for child in sorted(source.rglob("*")):
            relative = child.relative_to(source)
            target = destination / relative
            if child.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if child.is_symlink():
                records.append(
                    _file_record(child, target, kind=kind, skipped="symlink_not_copied")
                )
                continue
            skipped = _secret_skip_reason(child)
            if skipped:
                records.append(_file_record(child, target, kind=kind, skipped=skipped))
                continue
            if deliverable_only and child.suffix.lower() not in _DELIVERABLE_SUFFIXES:
                records.append(_excluded_record(child, target, kind=kind))
                continue
            try:
                shutil.copy2(child, target)
                records.append(_file_record(child, target, kind=kind))
            except OSError as exc:
                records.append(
                    _file_record(child, target, kind=kind, skipped=f"copy_error:{exc}")
                )
        return records
    destination.parent.mkdir(parents=True, exist_ok=True)
    skipped = _secret_skip_reason(source)
    if skipped:
        return [_file_record(source, destination, kind=kind, skipped=skipped)]
    if deliverable_only and source.suffix.lower() not in _DELIVERABLE_SUFFIXES:
        return [_excluded_record(source, destination, kind=kind)]
    try:
        if source.is_file():
            shutil.copy2(source, destination)
    except OSError as exc:
        return [_file_record(source, destination, kind=kind, skipped=f"copy_error:{exc}")]
    return [_file_record(source, destination, kind=kind)]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pruned(mapping: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in mapping.items() if value is not None}


def _values_match(expected: Any, observed: Any) -> bool:
    if expected == observed:
        return True
    if isinstance(expected, str) and isinstance(observed, str) and observed.startswith(expected):
        # Pins are often prefixes of the observed value: short commit SHAs,
        # or version specs like "3.12" against an observed "3.12.8".
        if len(expected) >= 6:
            return True
        return len(observed) > len(expected) and observed[len(expected)] in ".-+:_"
    return False


def _dependency_entry(
    kind: str,
    *,
    expected: dict[str, Any] | None = None,
    observed: dict[str, Any] | None = None,
    evidence: str | None = None,
) -> dict[str, Any]:
    """One normalized dependency record; observed is never invented from pins."""
    expected = _pruned(expected or {}) or None
    observed = _pruned(observed or {}) or None
    common = [key for key in (expected or {}) if key in (observed or {})]
    match = (
        all(_values_match(expected[key], observed[key]) for key in common)  # type: ignore[index]
        if expected and observed and common
        else None
    )
    return {
        "kind": kind,
        "expected": expected,
        "observed": observed,
        "match": match,
        "status": "observed" if observed else ("expected_only" if expected else "unresolved"),
        "evidence": evidence,
    }


def _repository_dependencies(run_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Both child repositories are the frozen execution environment of every suite."""
    pins = run_manifest.get("repo_pins")
    if not isinstance(pins, dict):
        return {}
    dependencies: dict[str, dict[str, Any]] = {}
    for key in ("novita_self_test", "kvv_k3"):
        pin = pins.get(key)
        if not isinstance(pin, dict):
            continue
        observed = _pruned(
            {
                "commit": pin.get("commit"),
                "branch": pin.get("branch"),
                "tracked_dirty": pin.get("tracked_dirty"),
                "untracked_count": pin.get("untracked_count"),
            }
        )
        dependencies[key] = _dependency_entry(
            "repository",
            observed=observed,
            evidence=f"run.json#repo_pins.{key}",
        )
    return dependencies


_REPOSITORY_ALIASES = {
    "kvv-k3": "kvv_k3",
    "kvv-k3-flash": "kvv_k3",
    "moonshotai/kvv-k3": "kvv_k3",
    "novitalabs-kvv-k3": "kvv_k3",
    "novita-self-test": "novita_self_test",
}


def _first_version(text: Any, pattern: str) -> str | None:
    if not isinstance(text, str):
        return None
    found = re.search(pattern, text)
    return found.group(0) if found else None


def _code_bench_dependencies(
    suite_dir: Path,
    fingerprint: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    contract = _load_json_object(suite_dir / "code-bench-run-contract.json")
    if not contract:
        contract = _load_json_object(suite_dir / "run-contract.json")
    pins = dict(contract.get("pins") or {})
    runtime: dict[str, Any] = {}
    runtime_evidence: str | None = None
    for candidate in (
        suite_dir / "archive" / "runtime-versions.json",
        suite_dir / "runtime-versions.json",
    ):
        if candidate.is_file():
            runtime = _load_json_object(candidate)
            runtime_evidence = str(candidate.relative_to(suite_dir).as_posix())
            break
    finalization = _load_json_object(suite_dir / "finalization-manifest.json")
    release = dict(finalization.get("release") or {})
    dependencies = {
        "harbor": _dependency_entry(
            "tool",
            expected={"version": pins.get("harbor_version"), "python": pins.get("harbor_python")},
            observed={
                "version": _first_version(runtime.get("harbor_version_output"), r"\d+\.\d+\.\d+"),
                "python": _first_version(runtime.get("harbor_python_output"), r"\d+\.\d+(?:\.\d+)?"),
                "version_output": runtime.get("harbor_version_output"),
                "python_output": runtime.get("harbor_python_output"),
            },
            evidence=runtime_evidence,
        ),
        "terminal_bench_2": _dependency_entry(
            "repository",
            expected={"ref": pins.get("terminal_bench_2_ref")},
            observed={"ref": runtime.get("terminal_bench_2_ref")},
            evidence=runtime_evidence,
        ),
        "kimi_cli": _dependency_entry(
            "tool",
            expected={"ref": pins.get("kimi_cli_ref")},
            observed={
                "ref": runtime.get("kimi_cli_ref"),
                "wheel_sha256": runtime.get("kimi_cli_wheel_sha256"),
            },
            evidence=runtime_evidence,
        ),
        "uv": _dependency_entry(
            "tool",
            expected={"version": pins.get("uv_version")},
            observed={
                "version": _first_version(runtime.get("uv_version_output"), r"\d+\.\d+\.\d+"),
                "version_output": runtime.get("uv_version_output"),
            },
            evidence=runtime_evidence,
        ),
        "kbench_runner": _dependency_entry(
            "runner",
            expected={"version": fingerprint.get("version")},
            observed={
                "version": release.get("runner_version"),
                "release": release.get("release"),
            },
            evidence="finalization-manifest.json" if release else None,
        ),
    }
    evidence_files = [
        suite_dir / relative
        for relative in (
            "code-bench-run-contract.json",
            "archive/runtime-versions.json",
            "finalization-manifest.json",
        )
        if (suite_dir / relative).is_file()
    ]
    return dependencies, evidence_files


def _deepswe_dependencies(
    suite_dir: Path,
    config: dict[str, Any],
    fingerprint: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    preflight_path = suite_dir / "local-preflight.json"
    preflight = _load_json_object(preflight_path)
    pinned = dict(preflight.get("expected") or {})
    observed = dict(preflight.get("observed") or {})
    evidence = "local-preflight.json" if preflight else None
    dependencies = {
        "pier": _dependency_entry(
            "tool",
            expected={"version": pinned.get("pier_version") or config.get("expected_pier_version")},
            observed={"version": observed.get("pier_version") or fingerprint.get("pier")},
            evidence=evidence,
        ),
        "deep_swe": _dependency_entry(
            "repository",
            expected={
                "commit": pinned.get("deep_swe_commit") or config.get("expected_deep_swe_commit")
            },
            observed={
                "commit": observed.get("deep_swe_commit") or fingerprint.get("deep_swe_commit"),
                "tracked_worktree_clean": observed.get("tracked_worktree_clean"),
                "task_count": observed.get("tasks"),
            },
            evidence=evidence,
        ),
        # The runtime does not report its Kimi Code version; the profile pin is
        # configured, never observed, so it stays expected-only.
        "kimi_code": _dependency_entry(
            "tool",
            expected={"version": config.get("kimi_code_version") or fingerprint.get("kimi_code")},
        ),
    }
    evidence_files = [preflight_path] if preflight_path.is_file() else []
    return dependencies, evidence_files


def _suite_dependencies(
    suite_dir: Path,
    *,
    adapter: str | None,
    config: dict[str, Any],
    fingerprint: dict[str, Any],
    run_manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    """Normalized dependency versions; observed beats pins, pins beat suite input."""
    dependencies = _repository_dependencies(run_manifest)
    declared_repository = _REPOSITORY_ALIASES.get(str(fingerprint.get("repository") or "").lower())
    if declared_repository in dependencies and fingerprint.get("upstream"):
        entry = dependencies[declared_repository]
        expected = dict(entry.get("expected") or {})
        expected["commit"] = fingerprint["upstream"]
        dependencies[declared_repository] = _dependency_entry(
            str(entry.get("kind") or "repository"),
            expected=expected,
            observed=entry.get("observed"),
            evidence=entry.get("evidence"),
        )
    evidence_files: list[Path] = []
    if adapter == "code_bench":
        extracted, evidence_files = _code_bench_dependencies(suite_dir, fingerprint)
        dependencies.update(extracted)
    elif adapter == "deepswe":
        extracted, evidence_files = _deepswe_dependencies(suite_dir, config, fingerprint)
        dependencies.update(extracted)
    if fingerprint.get("inspect_ai"):
        dependencies["inspect_ai"] = _dependency_entry(
            "tool",
            expected={"version": fingerprint.get("inspect_ai")},
        )
    if fingerprint.get("runner") and adapter != "code_bench":
        dependencies["runner"] = _dependency_entry(
            "runner",
            expected={
                "name": fingerprint.get("runner"),
                "version": fingerprint.get("version"),
            },
        )
    return dependencies, evidence_files


def _suite_parameters(
    input_payload: dict[str, Any],
    result: Any,
    run_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Concise effective-parameter index; suite-input.json stays the detail source."""
    config = dict(input_payload.get("config") or {})
    service = dict(input_payload.get("service") or {})
    fingerprint = dict(getattr(result, "runner_fingerprint", None) or {})
    if not fingerprint:
        fingerprint = {str(key): str(value) for key, value in (config.get("runner_fingerprint") or {}).items()}
    override = input_payload.get("concurrency_override")
    if override is None:
        override = run_manifest.get("concurrency_override")
    configured = config.get("concurrency")
    declared = fingerprint.get("concurrency")
    effective = override if override is not None else (configured if configured is not None else declared)
    concurrency = _pruned(
        {
            "configured": configured,
            "effective": effective,
            "scope": fingerprint.get("concurrency_scope"),
            "declared": declared,
        }
    )
    concurrency["override"] = override
    rounds = input_payload.get("deepswe_rounds")
    if rounds is None:
        rounds = config.get("rounds") or config.get("repeat") or fingerprint.get("rounds")
    dataset = _pruned(
        {
            "name": fingerprint.get("dataset") or fingerprint.get("corpus"),
            "fixture": config.get("fixture") or fingerprint.get("fixture"),
            "fixture_sha256": fingerprint.get("fixture_sha256"),
        }
    )
    judge = _pruned(
        {
            "base_url": fingerprint.get("judge_base_url"),
            "model": fingerprint.get("judge_model"),
            "api_key_env": fingerprint.get("judge_api_key_env"),
            "requests_per_minute": fingerprint.get("judge_requests_per_minute"),
        }
    )
    parameters: dict[str, Any] = {
        "adapter": input_payload.get("adapter"),
        "selector": input_payload.get("selector"),
        "selector_args": list(input_payload.get("selector_args") or []),
        "row_ids": list(input_payload.get("row_ids") or getattr(result, "row_ids", []) or []),
        "acceptance_mode": input_payload.get("acceptance_mode") or run_manifest.get("acceptance_mode"),
        "concurrency": concurrency,
        "retry_policy": config.get("rerun_policy"),
        "redacted_command": list(getattr(result, "redacted_command", None) or []),
    }
    service_index = _pruned(
        {
            "base_url": service.get("base_url"),
            "served_model": service.get("served_model"),
            "think_mode": service.get("think_mode"),
        }
    )
    if service_index:
        parameters["service"] = service_index
    if rounds is not None:
        parameters["rounds"] = rounds
    if config.get("timeout_seconds") is not None:
        parameters["timeout_seconds"] = config.get("timeout_seconds")
    if dataset:
        parameters["dataset"] = dataset
    if judge:
        parameters["judge"] = judge
    if fingerprint.get("effort"):
        parameters["thinking_effort"] = fingerprint.get("effort")
    return parameters


def _relative_records(records: list[dict[str, Any]], root: Path) -> list[dict[str, Any]]:
    root = root.resolve()
    normalized: list[dict[str, Any]] = []
    for item in records:
        copy = dict(item)
        try:
            copy["path"] = str(Path(str(item["path"])).resolve().relative_to(root))
        except ValueError:
            copy["path"] = str(item["path"])
        normalized.append(copy)
    return normalized


def _served_model(
    run_manifest: dict[str, Any],
    suite_manifests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Report which model the endpoint actually served for each suite.

    ``run_manifest["model"]`` is the profile family (for example ``kimi-k3``),
    not the served deployment: one K3 catalogue carries both
    ``moonshotai/kimi-k3-h`` and ``kimi-k3-flash``, and the two are different
    systems to score. Consolidating results across runs without this field
    silently mixes them, so the bundle states the observed value explicitly.
    """
    declared = run_manifest.get("served_model")
    observed: dict[str, list[str]] = {}
    for manifest in suite_manifests:
        service = (manifest.get("parameters") or {}).get("service") or {}
        model = service.get("served_model")
        if not model:
            continue
        observed.setdefault(str(model), []).append(str(manifest.get("suite_id")))
    served = declared or (next(iter(observed)) if len(observed) == 1 else None)
    resolved = _pruned(
        {
            "declared": declared,
            "family": run_manifest.get("model"),
            "distinct_observed": sorted(observed) or None,
            "mixed": len(observed) > 1 or None,
        }
    )
    # ``served`` stays present even when unresolved so a reader can tell "mixed
    # deployments" apart from "field not recorded".
    return {"served": served, **resolved}


def write_artifact_bundle(
    run_dir: Path,
    *,
    suite_results: list[Any],
    run_manifest: dict[str, Any],
    report: dict[str, Any],
) -> Path:
    """Build a self-contained, suite-partitioned delivery directory for a run."""
    run_dir = run_dir.resolve()
    staging = run_dir / f".{BUNDLE_DIRNAME}.staging-{uuid.uuid4().hex[:8]}"
    bundle = run_dir / BUNDLE_DIRNAME
    staging.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []
    suite_manifests: list[dict[str, Any]] = []
    result_by_suite = {str(result.suite_id): result for result in suite_results}
    states = {
        str(state.get("suite_id")): state
        for state in run_manifest.get("suite_states", [])
        if isinstance(state, dict)
    }

    root_files = (
        "run.json",
        "acceptance.json",
        "acceptance.md",
        "service-preflight.json",
        "events.jsonl",
        "diagnosis.json",
        "diagnosis.md",
        "reproduce.sh",
    )
    for name in root_files:
        source = run_dir / name
        if source.is_file():
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            copied = _copy_path(source, destination, kind="run")
            files.extend(_relative_records(copied, staging))

    readme = staging / "README.md"
    _write_text(
        readme,
        "# Test Artifact Bundle\n\n"
        "This directory is the delivery bundle for one nvst run.\n\n"
        "- Each numbered directory contains one test suite.\n"
        "- `suite-input.json` records non-secret inputs and parameters.\n"
        "- `suite-result.json` records the normalized result.\n"
        "- `suite-manifest.json` indexes copied task/case/evidence artifacts and\n"
        "  records the effective parameters plus expected/observed dependency\n"
        "  versions (dependency `evidence` paths are suite-directory relative and\n"
        "  copied under `artifacts/`).\n"
        "- `bundle.json` is the top-level inventory and completeness summary.\n"
        "- Source paths are retained for execution-host traceability.\n",
    )
    files.append(_generated_file_record(readme, Path("README.md"), kind="bundle"))

    suites_root = run_dir / "suites"
    for suite_dir in sorted(path for path in suites_root.glob("*") if path.is_dir()):
        execution_id = suite_dir.name
        result_path = suite_dir / "suite-result.json"
        input_path = suite_dir / "suite-input.json"
        result = None
        if result_path.is_file():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                suite_id = str(payload.get("suite_id") or execution_id)
            except (OSError, json.JSONDecodeError):
                suite_id = execution_id
        else:
            suite_id = execution_id.split("-", 1)[-1]
        result = result_by_suite.get(suite_id)
        input_payload = _load_json_object(input_path)
        suite_bundle = staging / execution_id
        suite_bundle.mkdir(parents=True, exist_ok=True)
        suite_files: list[dict[str, Any]] = []
        for source in (input_path, result_path):
            if source.is_file():
                destination = suite_bundle / source.name
                suite_files.extend(_relative_records(_copy_path(source, destination, kind="suite"), suite_bundle))

        artifact_paths = list(getattr(result, "artifact_paths", []) or []) if result else []
        copied_sources = {input_path.resolve(), result_path.resolve()}
        resolved_artifacts: list[Path] = []
        seen_artifacts: set[Path] = set()
        for raw_source in dict.fromkeys(artifact_paths):
            source = Path(str(raw_source)).expanduser()
            if not source.is_absolute():
                source = run_dir / source
            resolved = source.resolve()
            if resolved in seen_artifacts:
                continue
            seen_artifacts.add(resolved)
            resolved_artifacts.append(resolved)
        for index, source in enumerate(resolved_artifacts, start=1):
            if source in copied_sources:
                continue
            try:
                relative = source.relative_to(suite_dir.resolve())
                destination = suite_bundle / "artifacts" / relative
            except ValueError:
                destination = suite_bundle / "artifacts" / "external" / (
                    f"{index:04d}-{_safe_name(source.name)}"
                )
            copied = _relative_records(
                _copy_path(source, destination, kind="evidence", deliverable_only=True),
                suite_bundle,
            )
            suite_files.extend(copied)
            copied_sources.add(source)

        config = dict(input_payload.get("config") or {})
        fingerprint = dict(getattr(result, "runner_fingerprint", None) or {})
        if not fingerprint:
            fingerprint = {
                str(key): str(value)
                for key, value in (config.get("runner_fingerprint") or {}).items()
            }
        dependencies, dependency_evidence = _suite_dependencies(
            suite_dir,
            adapter=input_payload.get("adapter"),
            config=config,
            fingerprint=fingerprint,
            run_manifest=run_manifest,
        )
        parameters = _suite_parameters(input_payload, result, run_manifest)
        secret = find_secret({"parameters": parameters, "dependencies": dependencies})
        if secret:
            raise ConfigError("credential_material_forbidden", secret)
        for source in dependency_evidence:
            resolved = source.resolve()
            if resolved in copied_sources:
                continue
            try:
                relative = resolved.relative_to(suite_dir.resolve())
                destination = suite_bundle / "artifacts" / relative
            except ValueError:
                destination = (
                    suite_bundle / "artifacts" / "external" / f"dep-{_safe_name(source.name)}"
                )
            suite_files.extend(
                _relative_records(
                    _copy_path(source, destination, kind="evidence", deliverable_only=True),
                    suite_bundle,
                )
            )
            copied_sources.add(resolved)

        state = states.get(suite_id, {})
        suite_manifest = {
            "schema_version": 1,
            "execution_id": execution_id,
            "suite_id": suite_id,
            "row_ids": list(getattr(result, "row_ids", []) or []),
            "status": getattr(result, "status", state.get("status")),
            "validity": getattr(result, "validity", state.get("validity", "valid")),
            "reason": getattr(result, "reason", state.get("reason")),
            "counts": dict(getattr(result, "counts", {}) or {}),
            "metrics": dict(getattr(result, "metrics", {}) or {}),
            "coverage": dict(getattr(result, "coverage", {}) or {}),
            "parameters": parameters,
            "dependencies": dependencies,
            "artifacts": suite_files,
            "artifact_count": sum(1 for item in suite_files if item.get("copied")),
            "missing_artifact_count": sum(
                1
                for item in suite_files
                if not item.get("copied")
                and not item.get("skipped", "").startswith("secret_bearing")
                and item.get("skipped") != _NON_DELIVERABLE_SKIP
            ),
            "excluded_artifact_count": sum(
                1 for item in suite_files if item.get("skipped") == _NON_DELIVERABLE_SKIP
            ),
            "excluded_artifact_bytes": sum(
                int(item.get("bytes") or 0)
                for item in suite_files
                if item.get("skipped") == _NON_DELIVERABLE_SKIP
            ),
            "skipped_secret_artifact_count": sum(
                1
                for item in suite_files
                if str(item.get("skipped", "")).startswith("secret_bearing")
            ),
        }
        manifest_path = suite_bundle / "suite-manifest.json"
        _write_text(
            manifest_path,
            json.dumps(suite_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        suite_files.append(
            _generated_file_record(
                manifest_path,
                Path("suite-manifest.json"),
                kind="suite",
            )
        )
        files.extend(
            {
                **item,
                "path": str(Path(execution_id) / item["path"]),
            }
            for item in suite_files
        )
        suite_manifests.append(suite_manifest)

    missing = sum(
        1
        for item in files
        if not item.get("copied")
        and not str(item.get("skipped", "")).startswith("secret_bearing")
        and item.get("skipped") != _NON_DELIVERABLE_SKIP
    )
    skipped_secrets = sum(
        1 for item in files if str(item.get("skipped", "")).startswith("secret_bearing")
    )
    excluded = [item for item in files if item.get("skipped") == _NON_DELIVERABLE_SKIP]
    bundle_manifest = {
        "schema_version": 1,
        "run_id": run_manifest.get("run_id", run_dir.name),
        "model": run_manifest.get("model"),
        "served_model": _served_model(run_manifest, suite_manifests),
        "started_at": run_manifest.get("started_at"),
        "acceptance_mode": run_manifest.get("acceptance_mode"),
        "generated_at": report.get("generated_at"),
        "verdict": report.get("verdict"),
        "official_row_count": report.get("official_row_count"),
        "supplementary_row_count": report.get("supplementary_row_count", 0),
        "status": "complete" if missing == 0 else "partial",
        "summary": {
            "suite_count": len(suite_manifests),
            "artifact_file_count": sum(1 for item in files if item.get("copied")),
            "missing_artifact_count": missing,
            "skipped_secret_artifact_count": skipped_secrets,
            "excluded_artifact_count": len(excluded),
            "excluded_artifact_bytes": sum(int(item.get("bytes") or 0) for item in excluded),
            "excluded_artifact_policy": (
                "bulky machine formats (.eval/.body/.gz and other non-deliverable "
                "suffixes) stay on the execution host; "
                "source paths are recorded under files[] with skipped="
                + _NON_DELIVERABLE_SKIP
            ),
        },
        "suites": [
            {
                "execution_id": item["execution_id"],
                "suite_id": item["suite_id"],
                "row_ids": item["row_ids"],
                "status": item["status"],
                "validity": item["validity"],
                "artifact_count": item["artifact_count"],
                "missing_artifact_count": item["missing_artifact_count"],
                "excluded_artifact_count": item.get("excluded_artifact_count", 0),
            }
            for item in suite_manifests
        ],
        "files": files,
    }
    _write_text(
        staging / "bundle.json",
        json.dumps(bundle_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    if bundle.exists():
        shutil.rmtree(bundle)
    staging.rename(bundle)
    return bundle
