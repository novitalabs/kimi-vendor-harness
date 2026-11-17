from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from novita_self_test.response_artifacts import write_response_artifacts
from tools.replay_fixture import load_records, parse_body, request_for


FIXTURE = Path(__file__).parents[2] / "fixtures" / "9.openclaw_cases12.jsonl"
CASES = load_records("openclaw", FIXTURE)


def _case_id(record: dict[str, Any], index: int) -> str:
    return f"line-{record.get('line', index)}"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case_index", "record"),
    list(enumerate(CASES, start=1)),
    ids=[_case_id(record, index) for index, record in enumerate(CASES, start=1)],
)
def test_openclaw_tool_call_replay(
    case_index: int,
    record: dict[str, Any],
    hclient: httpx.Client,
    model: str,
    is_vendor: bool,
    pytestconfig: pytest.Config,
) -> None:
    if not is_vendor:
        pytest.skip("OpenClaw replay targets the selected vendor endpoint")

    payload = request_for("openclaw", record, model)
    response = hclient.post("/chat/completions", json=payload, timeout=900)
    case_id = _case_id(record, case_index)
    artifact_dir = str(pytestconfig.getoption("artifact_dir", default="") or "").strip()
    if artifact_dir:
        write_response_artifacts(
            Path(artifact_dir),
            group="openclaw-responses",
            case_id=case_id,
            response=response,
            api_key=str(pytestconfig.getoption("api_key", default="") or ""),
        )
    assert response.status_code == 200, (
        f"OpenClaw {case_id}: expected HTTP 200, got {response.status_code}"
    )

    parsed, observation = parse_body(response.content)
    if observation["stream"]:
        finish_reason = observation.get("finish_reason")
        tool_calls = observation.get("tool_calls") or []
        assert observation.get("done") is True, (
            f"OpenClaw {case_id}: streaming response did not terminate with [DONE]"
        )
    else:
        choices = (parsed or {}).get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")
        tool_calls = message.get("tool_calls") or []

    assert finish_reason == "tool_calls", (
        f"OpenClaw {case_id}: expected finish_reason='tool_calls', got {finish_reason!r}"
    )
    assert tool_calls, f"OpenClaw {case_id}: response contained no tool call"
    for tool_call in tool_calls:
        function = tool_call.get("function") or tool_call
        name = function.get("name")
        arguments = function.get("arguments")
        assert isinstance(name, str) and name, (
            f"OpenClaw {case_id}: tool call is missing a function name"
        )
        assert isinstance(arguments, str), (
            f"OpenClaw {case_id}: {name} arguments must be a JSON string"
        )
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"OpenClaw {case_id}: {name} arguments are invalid JSON at byte {exc.pos}"
            )
        assert isinstance(parsed_arguments, dict), (
            f"OpenClaw {case_id}: {name} arguments must decode to an object"
        )
