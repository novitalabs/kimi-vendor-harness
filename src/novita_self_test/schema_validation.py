from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    path = Path(__file__).resolve().parents[2] / "schemas" / schema_name
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validation_error(payload: Any, schema_name: str) -> str | None:
    errors = sorted(_validator(schema_name).iter_errors(payload), key=lambda item: list(item.path))
    if not errors:
        return None
    error = errors[0]
    location = ".".join(str(item) for item in error.absolute_path) or "$"
    return f"{schema_name} {location}: {error.message}"
