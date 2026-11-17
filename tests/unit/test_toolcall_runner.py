from __future__ import annotations

from pathlib import Path

from inspect_ai.scorer import CORRECT, INCORRECT
from inspect_ai.tool import ToolCall

from novita_self_test.adapters.toolcall_run import _tool_info, load_fixture, score_tool_calls


ROOT = Path(__file__).resolve().parents[2]


def _tools(*, required: bool = True) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"] if required else [],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _call(arguments: dict) -> ToolCall:
    return ToolCall(
        id="read:0",
        function="read",
        arguments=arguments,
        type="function",
    )


def test_toolcall_fixture_preserves_official_inventory() -> None:
    dataset, source = load_fixture(
        ROOT / "suites/novita-api/fixtures/toolcall-thinking-samples.v1.json.gz"
    )
    assert len(dataset) == 113
    assert source["completed_samples"] == 110
    assert source["accuracy"] == 0.8363636363636363
    assert set(source["error_sample_ids"]) == {
        "05563b28-45fe-11f1-99f6-f220bead0468",
        "275906fd-447e-11f1-bed6-0a5b122d427a",
        "c87be7c6-486c-11f1-aac7-4e1eae6eddbd",
    }


def test_toolcall_tool_info_matches_official_inspect_normalization() -> None:
    raw = {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Execute a command",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "env": {
                        "type": "object",
                        "patternProperties": {"^(.*)$": {"type": "string"}},
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    }

    info = _tool_info(raw)

    assert info.name == "exec"
    assert info.parameters.model_dump(exclude_none=True) == {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "env": {"type": "object"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }


def test_toolcall_scorer_reconstructs_observed_format_checks() -> None:
    passed = score_tool_calls(
        [_call({"path": "/tmp/example"})],
        _tools(),
        target_arguments_valid_non_empty=False,
    )
    assert passed.value == CORRECT

    empty = score_tool_calls(
        [_call({})],
        _tools(required=False),
        target_arguments_valid_non_empty=False,
    )
    assert empty.value == INCORRECT
    assert "empty_or_meaningless" in empty.explanation

    schema = score_tool_calls(
        [_call({})],
        _tools(required=True),
        target_arguments_valid_non_empty=False,
    )
    assert schema.value == INCORRECT
    assert "schema_validation" in schema.explanation

    semantic = score_tool_calls(
        [_call({"path": ""})],
        _tools(),
        target_arguments_valid_non_empty=False,
    )
    assert semantic.value == INCORRECT
    assert "semantically_empty" in semantic.explanation


def test_toolcall_scorer_allows_text_only_recovery_only_for_bad_target() -> None:
    recovered = score_tool_calls(
        [],
        _tools(),
        target_arguments_valid_non_empty=False,
    )
    missing = score_tool_calls(
        [],
        _tools(),
        target_arguments_valid_non_empty=True,
    )
    assert recovered.value == CORRECT
    assert missing.value == INCORRECT
