"""Secret-safe endpoint access and repository-local configuration."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator


def validation_error(payload: Any, schema_name: str) -> str | None:
    path = Path(__file__).resolve().parents[2] / "schemas" / schema_name
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return f"{schema_name}: {exc}"
    if not errors:
        return None
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path) or "$"
    return f"{schema_name} {location}: {error.message}"

SECRET_HEADER_NAMES = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"}
SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?<![A-Za-z0-9])sk[-_][A-Za-z0-9_-]{8,}|"
    r"(?:api[_-]?key|password|secret)\s*[:=]\s*[^\s,}]{6,})"
)


class ConfigError(ValueError):
    def __init__(self, code: str, message: str, *, path: list[str | int] | None = None,
                 expected: Any = None, actual: Any = None) -> None:
        super().__init__(message)
        self.code, self.path = code, path or []
        self.expected, self.actual = expected, actual


def workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def results_root(root: Path | None = None) -> Path:
    configured = os.environ.get("NVST_RESULTS_ROOT")
    return Path(configured).expanduser().resolve() if configured else (root or workspace_root()).resolve() / "runs"


def strict_json(text: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-JSON constant: {value}")
    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON number exceeds finite range")
        return number
    return json.loads(text, parse_constant=reject_constant, parse_float=finite_float)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def find_secret(value: Any, *, path: str = "$", allowed_text: frozenset[str] = frozenset()) -> str | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in SECRET_HEADER_NAMES and item not in (None, ""):
                return f"{path}.{key} contains a forbidden credential header"
            if found := find_secret(item, path=f"{path}.{key}", allowed_text=allowed_text):
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if found := find_secret(item, path=f"{path}[{index}]", allowed_text=allowed_text):
                return found
    elif isinstance(value, str) and any(match.group() not in allowed_text for match in SECRET_VALUE_RE.finditer(value)):
        return f"{path} appears to contain credential material"
    return None


def redact(value: Any, secret: str = "") -> Any:
    """Redact before serialization, including escaped response bodies and errors."""
    if isinstance(value, dict):
        return {redact(key, secret) if isinstance(key, str) else key:
                "[REDACTED]" if str(key).lower() in SECRET_HEADER_NAMES else redact(item, secret)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, str):
        return SECRET_VALUE_RE.sub("[REDACTED]", value.replace(secret, "[REDACTED]") if secret else value)
    return value


def safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ConfigError("path_invalid", f"path must stay inside repository: {relative}")
    return path


@dataclass(frozen=True)
class ServiceAccess:
    model_family: str
    base_url: str
    served_model: str
    api_key_env: str
    headers: dict[str, str]
    think_mode: str
    deployment_id: str = ""
    runtime_id: str = ""
    source_targets: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ServiceAccess":
        if not isinstance(raw, dict):
            raise ConfigError("service_access_invalid", "access must be an object")
        if error := validation_error(raw, "service-access.schema.json"):
            raise ConfigError("service_access_invalid", error)
        if secret := find_secret(raw):
            raise ConfigError("credential_material_forbidden", secret)
        parsed = urlsplit(raw["base_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError("service_access_invalid", "base_url must be HTTP(S) without userinfo, query or fragment")
        return cls(
            model_family=raw["model_family"], base_url=raw["base_url"].rstrip("/"),
            served_model=raw["served_model"], api_key_env=raw["api_key_env"],
            headers=dict(raw["headers"]), think_mode=raw["think_mode"],
            deployment_id=raw.get("deployment_id", ""), runtime_id=raw.get("runtime_id", ""),
            source_targets=tuple(raw.get("source_targets", [])),
        )

    @classmethod
    def load(cls, path: Path) -> "ServiceAccess":
        try:
            value = strict_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("service_access_invalid", f"cannot read access contract: {path}") from exc
        return cls.from_dict(value)

    def credential(self) -> str:
        return os.environ.get(self.api_key_env, "")

    def public_dict(self) -> dict[str, Any]:
        return {"model_family": self.model_family, "base_url": self.base_url,
                "served_model": self.served_model, "api_key_env": self.api_key_env,
                "headers": self.headers, "think_mode": self.think_mode,
                "deployment_id": self.deployment_id, "runtime_id": self.runtime_id,
                "source_targets": list(self.source_targets)}
