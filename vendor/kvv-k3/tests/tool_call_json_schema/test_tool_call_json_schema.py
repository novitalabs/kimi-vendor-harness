"""Tool-call JSON schema validation tests.

Each selected walle-valid MFJS schema is sent as a tool's `parameters`, the
model is forced to call the tool, and the returned `function.arguments` are
validated against the schema. Both non-streaming and streaming modes are
exercised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openai
import pytest

from tests.tool_call_json_schema.validator import (
    REQUEST_MODES,
    ValidatorCase,
    load_cases,
    select_cases,
    send_tool_schema,
    validate_arguments,
)


def _load_selected_cases(config: pytest.Config) -> list[tuple[ValidatorCase, Any, str]]:
    case_dir = Path(config.getoption("case_dir"))
    selection = config.getoption("selection")
    max_cases = config.getoption("max_cases")
    cases = load_cases(case_dir)
    return select_cases(
        cases,
        selection=selection,
        requested_cases=set(),
        max_cases=max_cases,
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "case_info" not in metafunc.fixturenames:
        return
    selected = _load_selected_cases(metafunc.config)
    params = [
        pytest.param(
            (case, schema, reason, mode),
            id=f"{case.suite}:{case.line}:{mode}",
        )
        for case, schema, reason in selected
        for mode in REQUEST_MODES
    ]
    metafunc.parametrize("case_info", params)


def test_tool_call_schema_matches_case_schema(
    client: openai.Client,
    model: str,
    thinking_flag: bool,
    think_mode: str,
    max_tokens_for_tool_schema: int,
    case_info: tuple[ValidatorCase, Any, str, str],
) -> None:
    """Send a single walle schema as tool parameters and validate arguments."""
    case, schema, selection_reason, mode = case_info
    response = send_tool_schema(
        client,
        model,
        schema,
        max_tokens_for_tool_schema,
        thinking_flag,
        think_mode,
        stream=mode == "stream",
    )

    assert response.accepted, (
        f"{case.suite}:{case.line} [{mode}] ({selection_reason}) "
        f"tool schema rejected: {response.message}"
    )

    valid, message = validate_arguments(schema, response.arguments)
    assert valid, (
        f"{case.suite}:{case.line} [{mode}] ({selection_reason}) "
        f"arguments validation failed: {message}; response: {response.message}"
    )


# ---------------------------------------------------------------------------
# $ref with sibling constraint keywords (type etc.). JSON Schema 2020-12
# evaluates sibling keywords alongside $ref, and the official implementation
# was adjusted to allow this shape (MoonshotAI/walle e5c4ecf: refmerge.go
# inlines conflicting $ref siblings -- Canonical() strips the redundant
# sibling type here, and the canonicalized schema validates at walle lite).
# So the endpoint must accept it (not 400) and the called arguments must
# validate against the schema.
# ---------------------------------------------------------------------------

_REF_WITH_SIBLING_TYPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            # sibling of $ref; evaluated per JSON Schema 2020-12
            "type": "string",
            "$ref": "#/$defs/uuid_str",
        }
    },
    "$defs": {"uuid_str": {"type": "string", "minLength": 1}},
    "required": ["id"],
    "additionalProperties": False,
}


def test_tool_schema_ref_with_sibling_type_accepted(
    client: openai.Client,
    model: str,
    thinking_flag: bool,
    think_mode: str,
    max_tokens_for_tool_schema: int,
) -> None:
    """A tool schema with a constraint keyword next to $ref is accepted.

    Upstream evidence (MoonshotAI/walle commit e5c4ecf): Canonical() of this
    exact schema strips the redundant sibling `type` from properties.id
    (refmerge.go), and the canonicalized schema passes walle lite."""
    tool_response = send_tool_schema(
        client,
        model,
        _REF_WITH_SIBLING_TYPE_SCHEMA,
        max_tokens_for_tool_schema,
        thinking_flag,
        think_mode,
    )
    assert tool_response.accepted, (
        f"$ref-with-sibling-type schema rejected: {tool_response.message}"
    )
    valid, message = validate_arguments(
        _REF_WITH_SIBLING_TYPE_SCHEMA, tool_response.arguments
    )
    assert valid, f"arguments validation failed: {message}"


# ---------------------------------------------------------------------------
# $ref carrying only a description (no constraint keywords). Annotations next
# to $ref are valid everywhere -- this is the shape of 7 of the 8 offending
# nodes in the codex_app.automation_update schema (minimized from it below).
# The endpoint must accept it and return arguments that validate.
# ---------------------------------------------------------------------------

_REF_WITH_DESCRIPTION_ONLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "automationId": {"$ref": "#/$defs/__schema1"},
    },
    "$defs": {
        "__schema1": {
            "description": (
                "Automation id. Required for mode=view, mode=update, "
                "mode=delete, and mode=suggested_update. Omit for mode=create "
                "and mode=suggested_create."
            ),
            "$ref": "#/$defs/__schema2",
        },
        "__schema2": {"type": "string", "minLength": 1},
    },
    "required": ["automationId"],
    "additionalProperties": False,
}


def test_tool_schema_ref_with_description_only_accepted(
    client: openai.Client,
    model: str,
    thinking_flag: bool,
    think_mode: str,
    max_tokens_for_tool_schema: int,
) -> None:
    """A $ref carrying only a description is accepted (automation_update shape)."""
    tool_response = send_tool_schema(
        client,
        model,
        _REF_WITH_DESCRIPTION_ONLY_SCHEMA,
        max_tokens_for_tool_schema,
        thinking_flag,
        think_mode,
    )
    assert tool_response.accepted, (
        f"$ref-with-description-only schema rejected: {tool_response.message}"
    )
    valid, message = validate_arguments(
        _REF_WITH_DESCRIPTION_ONLY_SCHEMA, tool_response.arguments
    )
    assert valid, f"arguments validation failed: {message}"
