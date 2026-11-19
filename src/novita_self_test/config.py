from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .schema_validation import validation_error

STATUSES = {
    "passed",
    "failed",
    "blocked",
    "not_run",
    "not_implemented",
    "deferred",
    "running",
    "pausing",
    "paused",
    "interrupted",
}
VALIDITIES = {"valid", "service_invalid", "execution_invalid"}
MODES = {"develop", "accept"}
SOURCE_STATES = {"ready", "blocked"}
SECRET_HEADER_NAMES = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?<![A-Za-z0-9])sk[-_][A-Za-z0-9_-]{8,}|"
    r"(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,}]{6,})"
)


class ConfigError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Mappings merge recursively; any other value in ``override`` replaces the base value."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _compose_suites(raw_suites: list[Any], catalog_path: Path | None) -> list[Any]:
    """Expand ``use: <id>`` manifest entries from the shared suite catalog.

    A model manifest lists which shared test sets it runs (``use``), for which official rows
    (``row_ids``, ``selectors``), and only the values where this model differs from the shared
    definition. The composed suite's ``suite_id`` defaults to the catalog id.
    """
    if not any(isinstance(item, dict) and "use" in item for item in raw_suites):
        return raw_suites
    if catalog_path is None:
        raise ConfigError("suite_manifest_invalid", "manifest uses shared suites but the profile has no suite_catalog")
    catalog = _load_yaml(catalog_path).get("suites")
    if not isinstance(catalog, dict):
        raise ConfigError("suite_manifest_invalid", f"{catalog_path} needs a 'suites' mapping")
    composed = []
    for item in raw_suites:
        if not isinstance(item, dict) or "use" not in item:
            composed.append(item)
            continue
        name = item["use"]
        if not isinstance(catalog.get(name), dict):
            raise ConfigError("suite_manifest_invalid", f"unknown shared suite {name!r} in {catalog_path.name}")
        suite = _deep_merge(catalog[name], {key: value for key, value in item.items() if key != "use"})
        suite.setdefault("suite_id", name)
        composed.append(suite)
    return composed


# Machine-specific locations of external runtimes. They describe where a suite runs, not what it
# tests, so a host overrides them with an environment variable instead of editing the shared suite
# definitions. The suite definition keeps the default.
HOST_SETTING_ENV = {
    "runtime_root": "NVST_DEEPSWE_ROOT",
    "pier_bin": "NVST_PIER_BIN",
}


def host_setting(key: str, default: Any = None) -> Any:
    """Return the host override, or the supplied default when no override is set."""
    return os.environ.get(HOST_SETTING_ENV[key]) or default


def required_host_setting(key: str, configured: Any = None) -> str:
    """Resolve a host setting; fail clearly when a public profile has no local value."""
    value = host_setting(key, configured)
    if value is None or not str(value).strip():
        raise ConfigError(
            "host_setting_missing",
            f"set {HOST_SETTING_ENV[key]} before running this host-dependent suite",
        )
    return str(value)


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def results_root(root: Path | None = None) -> Path:
    """Return the per-user result root; defaults to the ignored repository ``runs/`` directory."""
    configured = os.environ.get("NVST_RESULTS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return (root or workspace_root()).resolve() / "runs"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError("profile_file_missing", f"required file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("profile_yaml_invalid", f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("profile_shape_invalid", f"expected mapping in {path}")
    return data


def _canonical_hash(data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_secret(value: Any, *, path: str = "$") -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in SECRET_HEADER_NAMES and item not in (None, ""):
                return f"{path}.{key_text} contains a forbidden credential header"
            found = find_secret(item, path=f"{path}.{key_text}")
            if found:
                return found
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = find_secret(item, path=f"{path}[{index}]")
            if found:
                return found
        return None
    if isinstance(value, str) and SECRET_VALUE_RE.search(value):
        return f"{path} appears to contain credential material"
    return None


@dataclass(frozen=True)
class SourceTarget:
    repo: str
    worktree: str
    branch: str | None
    commit: str | None
    log_entrypoints: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int) -> "SourceTarget":
        repo = data.get("repo")
        worktree = data.get("worktree")
        if not isinstance(repo, str) or not repo:
            raise ConfigError("service_access_invalid", f"source_targets[{index}].repo is required")
        if not isinstance(worktree, str) or not worktree:
            raise ConfigError(
                "service_access_invalid", f"source_targets[{index}].worktree is required"
            )
        entries = data.get("log_entrypoints") or []
        if not isinstance(entries, list) or not all(isinstance(item, str) for item in entries):
            raise ConfigError(
                "service_access_invalid",
                f"source_targets[{index}].log_entrypoints must be a string list",
            )
        return cls(
            repo=repo,
            worktree=worktree,
            branch=data.get("branch"),
            commit=data.get("commit"),
            log_entrypoints=tuple(entries),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "worktree": self.worktree,
            "branch": self.branch,
            "commit": self.commit,
            "log_entrypoints": list(self.log_entrypoints),
        }


@dataclass(frozen=True)
class ServiceAccess:
    model_family: str
    base_url: str
    served_model: str
    api_key_env: str
    headers: dict[str, str]
    think_mode: str
    deployment_id: str
    runtime_id: str
    source_targets: tuple[SourceTarget, ...]
    source_path: Path
    sha256: str

    @classmethod
    def load(cls, path: Path) -> "ServiceAccess":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError("service_access_missing", f"service access not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError("service_access_invalid", f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("service_access_invalid", "service access must be a JSON object")
        schema_error = validation_error(raw, "service-access.schema.json")
        if schema_error:
            raise ConfigError("service_access_invalid", schema_error)
        secret = find_secret(raw)
        if secret:
            raise ConfigError("credential_material_forbidden", secret)

        required = (
            "model_family",
            "base_url",
            "served_model",
            "api_key_env",
            "think_mode",
            "deployment_id",
            "runtime_id",
        )
        missing = [key for key in required if not isinstance(raw.get(key), str) or not raw[key]]
        if missing:
            raise ConfigError(
                "service_access_invalid", f"missing required string fields: {', '.join(missing)}"
            )
        base_url = raw["base_url"]
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError("service_access_invalid", "base_url must use http or https")
        headers = raw.get("headers") or {}
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        ):
            raise ConfigError("service_access_invalid", "headers must be a string mapping")
        bad_headers = sorted(set(key.lower() for key in headers) & SECRET_HEADER_NAMES)
        if bad_headers:
            raise ConfigError(
                "credential_material_forbidden",
                f"secret-bearing headers are forbidden: {', '.join(bad_headers)}",
            )
        targets = raw.get("source_targets") or []
        if not isinstance(targets, list) or not all(isinstance(item, dict) for item in targets):
            raise ConfigError("service_access_invalid", "source_targets must be an object list")
        api_key_env = raw["api_key_env"]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", api_key_env):
            raise ConfigError(
                "service_access_invalid", "api_key_env must be an uppercase environment name"
            )
        return cls(
            model_family=raw["model_family"],
            base_url=base_url.rstrip("/"),
            served_model=raw["served_model"],
            api_key_env=api_key_env,
            headers=dict(headers),
            think_mode=raw["think_mode"],
            deployment_id=raw["deployment_id"],
            runtime_id=raw["runtime_id"],
            source_targets=tuple(
                SourceTarget.from_dict(item, index=index) for index, item in enumerate(targets)
            ),
            source_path=path.resolve(),
            sha256=_canonical_hash(raw),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "model_family": self.model_family,
            "base_url": self.base_url,
            "served_model": self.served_model,
            "api_key_env": self.api_key_env,
            "headers": self.headers,
            "think_mode": self.think_mode,
            "deployment_id": self.deployment_id,
            "runtime_id": self.runtime_id,
            "source_targets": [item.to_dict() for item in self.source_targets],
        }

    def credential(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass(frozen=True)
class JudgeAccess:
    base_url: str
    model: str
    api_key_env: str
    requests_per_minute: int
    source_path: Path
    sha256: str

    @classmethod
    def load(cls, path: Path) -> "JudgeAccess":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigError("judge_access_missing", f"judge access not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError("judge_access_invalid", f"invalid JSON in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("judge_access_invalid", "judge access must be a JSON object")
        schema_error = validation_error(raw, "judge-access.schema.json")
        if schema_error:
            raise ConfigError("judge_access_invalid", schema_error)
        secret = find_secret(raw)
        if secret:
            raise ConfigError("credential_material_forbidden", secret)
        return cls(
            base_url=str(raw["base_url"]).rstrip("/"),
            model=str(raw["model"]),
            api_key_env=str(raw["api_key_env"]),
            requests_per_minute=int(raw["requests_per_minute"]),
            source_path=path.resolve(),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def credential(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass(frozen=True)
class SuiteDefinition:
    suite_id: str
    adapter: str
    row_ids: tuple[str, ...]
    test_class: str | None
    mandatory: bool
    long_test: str | None
    selectors: dict[str, list[str]]
    config: dict[str, Any]
    completeness: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int) -> "SuiteDefinition":
        suite_id = data.get("suite_id")
        adapter = data.get("adapter")
        if not isinstance(suite_id, str) or not suite_id:
            raise ConfigError("suite_manifest_invalid", f" suites[{index}].suite_id is required")
        if not isinstance(adapter, str) or not adapter:
            raise ConfigError("suite_manifest_invalid", f"{suite_id}.adapter is required")
        test_class = data.get("test_class")
        if test_class is not None and test_class not in {"feature", "precision"}:
            raise ConfigError(
                "suite_manifest_invalid",
                f"{suite_id}.test_class must be feature or precision",
            )
        row_ids = data.get("row_ids") or []
        if not isinstance(row_ids, list) or not all(isinstance(item, str) for item in row_ids):
            raise ConfigError("suite_manifest_invalid", f"{suite_id}.row_ids must be a list")
        selectors = data.get("selectors") or {}
        if not isinstance(selectors, dict):
            raise ConfigError("suite_manifest_invalid", f"{suite_id}.selectors must be a mapping")
        normalized_selectors: dict[str, list[str]] = {}
        for key, value in selectors.items():
            values = value if isinstance(value, list) else [value]
            if not isinstance(key, str) or not all(isinstance(item, str) for item in values):
                raise ConfigError(
                    "suite_manifest_invalid", f"{suite_id}.selectors values must be strings"
                )
            normalized_selectors[key] = list(values)
        config = data.get("config") or {}
        completeness = data.get("completeness") or {}
        if not isinstance(config, dict) or not isinstance(completeness, dict):
            raise ConfigError(
                "suite_manifest_invalid", f"{suite_id}.config/completeness must be mappings"
            )
        repeat_schedule = config.get("repeat_schedule")
        if repeat_schedule is not None and repeat_schedule not in {"round_barrier"}:
            raise ConfigError(
                "suite_manifest_invalid",
                f"{suite_id}.config.repeat_schedule must be round_barrier",
            )
        rerun_policy = config.get("rerun_policy")
        if rerun_policy is not None:
            if not isinstance(rerun_policy, dict):
                raise ConfigError(
                    "suite_manifest_invalid", f"{suite_id}.config.rerun_policy must be a mapping"
                )
            mode = rerun_policy.get("mode")
            max_reruns = rerun_policy.get("max_reruns")
            scheduling = rerun_policy.get("scheduling")
            if mode not in {"invalid_only", "any_pass", "runner_owned", "error_only"}:
                raise ConfigError(
                    "suite_manifest_invalid", f"{suite_id} has invalid rerun policy mode"
                )
            if (
                not isinstance(max_reruns, int)
                or isinstance(max_reruns, bool)
                or max_reruns < 0
            ):
                raise ConfigError(
                    "suite_manifest_invalid", f"{suite_id} max_reruns must be non-negative"
                )
            if mode == "runner_owned":
                if scheduling != "immediate":
                    raise ConfigError(
                        "suite_manifest_invalid",
                        f"{suite_id} runner-owned reruns must be immediate",
                    )
            elif mode == "error_only":
                if scheduling not in {"after_each_round", "batch_after_formal"}:
                    raise ConfigError(
                        "suite_manifest_invalid",
                        f"{suite_id} error-only reruns must be after_each_round or batch_after_formal",
                    )
            elif scheduling != "batch_after_formal":
                raise ConfigError(
                    "suite_manifest_invalid", f"{suite_id} reruns must be batch_after_formal"
                )
        return cls(
            suite_id=suite_id,
            adapter=adapter,
            row_ids=tuple(row_ids),
            test_class=test_class,
            mandatory=bool(data.get("mandatory", True)),
            long_test=data.get("long_test"),
            selectors=normalized_selectors,
            config=dict(config),
            completeness=dict(completeness),
        )

    def matches(self, selector: str) -> bool:
        return selector == self.suite_id or selector.startswith(f"{self.suite_id}:") or selector in self.selectors


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    directory: Path
    profile: dict[str, Any]
    source: dict[str, Any]
    rows: dict[str, Any]
    suites: tuple[SuiteDefinition, ...]
    snapshot_revision: str

    @classmethod
    def load(
        cls,
        model_id: str,
        *,
        root: Path | None = None,
        allow_blocked: bool = False,
    ) -> "ModelProfile":
        base = (root or workspace_root()) / "models" / model_id
        profile = _load_yaml(base / "profile.yaml")
        source = _load_yaml(base / "requirements" / "source.yaml")
        rows = _load_yaml(base / "requirements" / "rows.yaml")
        manifest = _load_yaml(base / "suite-manifest.yaml")
        if profile.get("model_id") != model_id:
            raise ConfigError("profile_model_mismatch", f"{base}/profile.yaml model_id mismatch")
        repository_root = base.parents[1]
        catalog_value = profile.get("suite_catalog")
        catalog_path = repository_root / str(catalog_value) if catalog_value else None
        pinned_paths = []
        stream = source.get("stream_spec_contract") or {}
        if isinstance(stream, dict) and stream.get("snapshot_path") and stream.get("sha256"):
            pinned_paths.append(
                (str(stream["snapshot_path"]), str(stream["sha256"]), "Stream snapshot")
            )
        for fixture in source.get("fixture_lock") or []:
            if (
                isinstance(fixture, dict)
                and isinstance(fixture.get("path"), str)
                and isinstance(fixture.get("sha256"), str)
            ):
                pinned_paths.append((fixture["path"], fixture["sha256"], fixture.get("name", "fixture")))
        for relative, expected_sha, label in pinned_paths:
            path = repository_root / relative
            if not path.is_file():
                raise ConfigError("profile_file_missing", f"pinned {label} not found: {path}")
            observed_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            if observed_sha != expected_sha:
                raise ConfigError(
                    "requirements_snapshot_invalid",
                    f"pinned {label} SHA mismatch: expected {expected_sha}, observed {observed_sha}",
                )
        source_state = source.get("status")
        if source_state not in SOURCE_STATES:
            raise ConfigError("requirements_snapshot_invalid", "requirements source status is invalid")
        if source_state == "blocked" and not allow_blocked:
            raise ConfigError(
                str(source.get("reason") or "requirements_snapshot_missing"),
                str(source.get("message") or "requirements snapshot is blocked"),
            )
        revision = source.get("snapshot_revision")
        if not isinstance(revision, str) or not revision:
            raise ConfigError(
                "requirements_snapshot_invalid", "source.yaml needs snapshot_revision"
            )
        row_sections = profile.get("reporting", {}).get("sections") or []
        if not isinstance(row_sections, list) or not row_sections:
            raise ConfigError("profile_invalid", "profile reporting.sections must be a list")
        row_ids: list[str] = []
        for section in row_sections:
            section_rows = rows.get(section)
            if not isinstance(section_rows, list):
                raise ConfigError(
                    "requirements_snapshot_invalid", f"rows.yaml missing section {section}"
                )
            for row in section_rows:
                row_id = row.get("row_id") if isinstance(row, dict) else None
                if not isinstance(row_id, str) or not row_id:
                    raise ConfigError(
                        "requirements_snapshot_invalid", f"invalid row in section {section}"
                    )
                row_ids.append(row_id)
        if len(row_ids) != len(set(row_ids)):
            raise ConfigError("requirements_snapshot_invalid", "duplicate official row_id")
        raw_suites = manifest.get("suites")
        if not isinstance(raw_suites, list) or not raw_suites:
            raise ConfigError("suite_manifest_invalid", "suite-manifest needs suites")
        raw_suites = _compose_suites(raw_suites, catalog_path)
        suites = tuple(
            SuiteDefinition.from_dict(item, index=index)
            for index, item in enumerate(raw_suites)
            if isinstance(item, dict)
        )
        if len(suites) != len(raw_suites):
            raise ConfigError("suite_manifest_invalid", "suite entries must be mappings")
        if manifest.get("suite_contract_version") == 1:
            required_fingerprint = (
                "entrypoint",
                "concurrency_scope",
                "retry_owner",
                "retry_max_reruns",
                "retry_schedule",
                "artifact_contract",
            )
            for suite in suites:
                fingerprint = suite.config.get("runner_fingerprint") or {}
                missing = [key for key in required_fingerprint if not fingerprint.get(key)]
                if not missing:
                    has_artifacts = bool(
                        suite.completeness.get("required_artifacts")
                        or suite.completeness.get("required_artifact_patterns")
                    )
                    if not has_artifacts and suite.adapter != "deepswe":
                        missing.append("completeness.required_artifacts")
                if missing:
                    raise ConfigError(
                        "suite_manifest_invalid",
                        f"{suite.suite_id} missing normalized suite contract fields: {missing}",
                    )
        suite_ids = [item.suite_id for item in suites]
        if len(suite_ids) != len(set(suite_ids)):
            raise ConfigError("suite_manifest_invalid", "duplicate suite_id")
        unknown_rows = sorted({row for suite in suites for row in suite.row_ids} - set(row_ids))
        supplementary = {
            row.get("row_id")
            for row in rows.get("supplementary", [])
            if isinstance(row, dict)
        }
        unknown_rows = sorted(set(unknown_rows) - supplementary)
        if unknown_rows:
            raise ConfigError(
                "suite_manifest_invalid", f"suite manifest references unknown rows: {unknown_rows}"
            )
        official_counts = {
            row_id: sum(row_id in suite.row_ids for suite in suites) for row_id in row_ids
        }
        missing_rows = [row_id for row_id, count in official_counts.items() if count == 0]
        duplicate_rows = [row_id for row_id, count in official_counts.items() if count > 1]
        if missing_rows or duplicate_rows:
            raise ConfigError(
                "suite_manifest_invalid",
                f"official row coverage must be exactly once; missing={missing_rows}, "
                f"duplicate={duplicate_rows}",
            )
        execution = profile.get("execution") or {}
        if not isinstance(execution, dict):
            raise ConfigError("profile_invalid", "profile execution must be a mapping")
        execution_site = execution.get("site")
        execution_marker = execution.get("site_marker")
        if (execution_site is None) != (execution_marker is None):
            raise ConfigError(
                "profile_invalid", "execution.site and execution.site_marker must be declared together"
            )
        if execution_site is not None and (
            not isinstance(execution_site, str) or not execution_site.strip()
        ):
            raise ConfigError("profile_invalid", "execution.site must be a non-empty string")
        if execution_marker is not None and (
            not isinstance(execution_marker, str)
            or not Path(execution_marker).is_absolute()
        ):
            raise ConfigError("profile_invalid", "execution.site_marker must be an absolute path")
        acceptance_control = profile.get("acceptance_control")
        if not isinstance(acceptance_control, dict):
            raise ConfigError("profile_invalid", "profile acceptance_control must be a mapping")
        global_stop = acceptance_control.get("global_stop")
        if not isinstance(global_stop, dict):
            raise ConfigError(
                "profile_invalid", "acceptance_control.global_stop must be a mapping"
            )
        for key, allowed in (
            ("validities", VALIDITIES),
            ("statuses", STATUSES),
            ("reasons", None),
        ):
            values = global_stop.get(key)
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item for item in values
            ):
                raise ConfigError(
                    "profile_invalid", f"acceptance_control.global_stop.{key} must be strings"
                )
            if allowed is not None and not set(values) <= allowed:
                raise ConfigError(
                    "profile_invalid",
                    f"acceptance_control.global_stop.{key} contains unknown values",
                )
        if execution.get("row_order") == "exact":
            flattened = [row_id for suite in suites for row_id in suite.row_ids if row_id in row_ids]
            if flattened != row_ids:
                raise ConfigError(
                    "suite_manifest_invalid",
                    "suite row order must exactly match committed official row order",
                )
        return cls(model_id, base, profile, source, rows, suites, revision)

    def require_execution_site(self) -> str | None:
        execution = self.profile.get("execution") or {}
        site = execution.get("site")
        marker_value = execution.get("site_marker")
        if site is None:
            return None
        marker = Path(str(marker_value))
        try:
            observed = marker.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(
                "execution_site_mismatch",
                f"{self.model_id} execution requires site {site!r} with marker {marker}",
            ) from exc
        if observed != site:
            raise ConfigError(
                "execution_site_mismatch",
                f"{self.model_id} execution requires site {site!r}; marker reports {observed!r}",
            )
        return str(site)

    def official_rows(self) -> list[tuple[str, dict[str, Any]]]:
        result: list[tuple[str, dict[str, Any]]] = []
        for section in self.profile["reporting"]["sections"]:
            result.extend((section, row) for row in self.rows[section])
        return result

    def supplementary_rows(self) -> list[tuple[str, dict[str, Any]]]:
        return [("supplementary", row) for row in self.rows.get("supplementary", [])]

    def acceptance_mode_config(self, mode: str) -> dict[str, Any]:
        """Return the profile-owned selector policy for an acceptance mode."""
        modes = self.profile.get("acceptance_modes") or {}
        if not isinstance(modes, dict):
            raise ConfigError("profile_invalid", "profile acceptance_modes must be a mapping")
        configured = modes.get(mode)
        if configured is None:
            if mode == "full":
                return {"selectors": [suite.suite_id for suite in self.suites]}
            if mode == "develop":
                return {"selectors": [], "include_long": [], "deepswe_rounds": 1}
            raise ConfigError(
                "acceptance_mode_invalid",
                f"profile does not define acceptance mode: {mode}",
            )
        if not isinstance(configured, dict):
            raise ConfigError(
                "profile_invalid", f"acceptance_modes.{mode} must be a mapping"
            )
        selectors = configured.get("selectors")
        if selectors is not None and (
            not isinstance(selectors, list) or not all(isinstance(item, str) for item in selectors)
        ):
            raise ConfigError(
                "profile_invalid", f"acceptance_modes.{mode}.selectors must be a string list"
            )
        include_long = configured.get("include_long") or []
        if not isinstance(include_long, list) or not all(
            isinstance(item, str) for item in include_long
        ):
            raise ConfigError(
                "profile_invalid", f"acceptance_modes.{mode}.include_long must be a string list"
            )
        rounds = configured.get("deepswe_rounds")
        if rounds is not None and (
            isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1
        ):
            raise ConfigError(
                "profile_invalid", f"acceptance_modes.{mode}.deepswe_rounds must be positive"
            )
        return dict(configured)

    def content_hashes(self) -> dict[str, str]:
        root = self.directory.parents[1]
        paths = [
            self.directory / "profile.yaml",
            self.directory / "requirements" / "source.yaml",
            self.directory / "requirements" / "rows.yaml",
            self.directory / "suite-manifest.yaml",
        ]
        if self.profile.get("suite_catalog"):
            paths.append(root / str(self.profile["suite_catalog"]))
        stream = self.source.get("stream_spec_contract") or {}
        if isinstance(stream, dict) and isinstance(stream.get("snapshot_path"), str):
            paths.append(root / stream["snapshot_path"])
        for fixture in self.source.get("fixture_lock") or []:
            if isinstance(fixture, dict) and isinstance(fixture.get("path"), str):
                paths.append(root / fixture["path"])
        result = {}
        for path in paths:
            if not path.is_file():
                raise ConfigError("profile_file_missing", f"pinned profile input not found: {path}")
            result[str(path.resolve().relative_to(root.resolve()))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        return result

    def suite_input_hashes(self) -> dict[str, str]:
        root = self.directory.parents[1]
        rows = {
            row["row_id"]: row
            for _, row in self.official_rows() + self.supplementary_rows()
        }
        pins: list[dict[str, str]] = []
        stream = self.source.get("stream_spec_contract") or {}
        if isinstance(stream, dict) and stream.get("snapshot_path") and stream.get("sha256"):
            pins.append(
                {"path": str(stream["snapshot_path"]), "sha256": str(stream["sha256"])}
            )
        for fixture in self.source.get("fixture_lock") or []:
            if isinstance(fixture, dict) and fixture.get("path") and fixture.get("sha256"):
                pins.append(
                    {"path": str(fixture["path"]), "sha256": str(fixture["sha256"])}
                )
        result: dict[str, str] = {}
        for suite in self.suites:
            suite_payload = {
                "suite_id": suite.suite_id,
                "adapter": suite.adapter,
                "row_ids": list(suite.row_ids),
                "test_class": suite.test_class,
                "mandatory": suite.mandatory,
                "long_test": suite.long_test,
                "selectors": suite.selectors,
                "config": suite.config,
                "completeness": suite.completeness,
            }
            rendered = json.dumps(suite_payload, ensure_ascii=True, sort_keys=True)
            matched_pins = [
                pin
                for pin in pins
                if pin["path"] in rendered
                or pin["sha256"] in rendered
                or str(Path(pin["path"]).parent) in rendered
            ]
            payload = {
                "snapshot_revision": self.snapshot_revision,
                "suite": suite_payload,
                "rows": [rows[row_id] for row_id in suite.row_ids],
                "pinned_inputs": matched_pins,
                "pinned_files": {
                    pin["path"]: hashlib.sha256((root / pin["path"]).read_bytes()).hexdigest()
                    for pin in matched_pins
                },
            }
            result[suite.suite_id] = _canonical_hash(payload)
        return result

    def select(self, selectors: list[str]) -> list[tuple[SuiteDefinition, list[str]]]:
        selected: list[tuple[SuiteDefinition, list[str]]] = []
        for selector in selectors:
            matches = [suite for suite in self.suites if suite.matches(selector)]
            if not matches:
                raise ConfigError("selector_not_found", f"selector did not match any suite: {selector}")
            for suite in matches:
                args = suite.selectors.get(selector, [])
                selected.append((suite, list(args)))
        return selected
