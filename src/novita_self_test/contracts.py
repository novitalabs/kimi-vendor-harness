"""Versioned machine-result helpers. Messages and response data are untrusted."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

from .config import ConfigError, redact, workspace_root

SCHEMA_VERSION = 1


def issue(code: str, message: str, *, expected: Any = None, actual: Any = None,
          path: list[str | int] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "expected": expected, "actual": actual, "path": path or []}


def error_result(command: str, code: str, message: str, *, endpoint_requests: int | None = 0) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "kind": "error", "command": command,
            "status": "blocked" if endpoint_requests == 0 else "execution_invalid",
            "error": issue(code, message), "endpoint_requests": endpoint_requests}


def exit_code(result: dict[str, Any]) -> int:
    return 0 if result["status"] in {"passed", "ready"} else 1 if result["status"] == "failed" else 2


def deny_retrieval(uri: str):
    raise NoSuchResource(ref=uri)


def oracle_validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(schema, registry=Registry(retrieve=deny_retrieval))


def validate_contract(value: Any, name: str, root: Path | None = None) -> None:
    directory = (root or workspace_root()) / "schemas"
    registry = Registry(retrieve=deny_retrieval)
    schemas = {path.name: json.loads(path.read_text(encoding="utf-8")) for path in directory.glob("*.schema.json")}
    for filename, schema in schemas.items():
        registry = registry.with_resource(filename, Resource.from_contents(schema))
    error = next(Draft202012Validator(schemas[name], registry=registry).iter_errors(value), None)
    if error:
        location = "/".join(str(part) for part in error.absolute_path)
        raise ConfigError("output_contract_invalid", f"{name} at {location}: {error.message}")


def json_text(value: Any, secret: str = "") -> str:
    return json.dumps(redact(value, secret), ensure_ascii=False, allow_nan=False, indent=2) + "\n"
