from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


_MODULE_PATH = Path(__file__).resolve().parents[2] / "suites/novita-api/toolcall_arguments_recovery.py"
_SPEC = importlib.util.spec_from_file_location("toolcall_arguments_recovery", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_toolcall_arguments_recovery = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_toolcall_arguments_recovery)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "suites/novita-api/fixtures"


class FakeCompletions:
    def __init__(
        self,
        tool_name: str,
        arguments: str,
        extra_tool_calls: list[tuple[str, str]] | None = None,
    ):
        self.tool_name = tool_name
        self.arguments = arguments
        self.extra_tool_calls = extra_tool_calls or []

    def create(self, **kwargs):
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        calls = [
            SimpleNamespace(
                index=0,
                function=SimpleNamespace(name=self.tool_name, arguments=""),
            ),
            SimpleNamespace(
                index=0,
                function=SimpleNamespace(name=None, arguments=self.arguments),
            ),
        ]
        for index, (name, arguments) in enumerate(self.extra_tool_calls, start=1):
            calls.extend(
                [
                    SimpleNamespace(
                        index=index,
                        function=SimpleNamespace(name=name, arguments=""),
                    ),
                    SimpleNamespace(
                        index=index,
                        function=SimpleNamespace(name=None, arguments=arguments),
                    ),
                ]
            )
        delta = SimpleNamespace(tool_calls=calls)
        yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(tool_calls=[]), finish_reason="tool_calls"
                )
            ]
        )


class FakeClient:
    def __init__(
        self,
        tool_name: str,
        arguments: str,
        extra_tool_calls: list[tuple[str, str]] | None = None,
    ):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(tool_name, arguments, extra_tool_calls)
        )


def test_row18_prepares_the_two_exact_replays() -> None:
    case_a = _toolcall_arguments_recovery.load_sample(
        FIXTURE_DIR / _toolcall_arguments_recovery.CASE_FILES["a"]
    )
    case_b = _toolcall_arguments_recovery.load_sample(
        FIXTURE_DIR / _toolcall_arguments_recovery.CASE_FILES["b"]
    )
    request_a = _toolcall_arguments_recovery.prepare_request(case_a, "a")
    request_b = _toolcall_arguments_recovery.prepare_request(case_b, "b")

    assert (len(case_a["messages"]), len(case_a["tools"])) == (157, 59)
    assert (len(case_b["messages"]), len(case_b["tools"])) == (21, 15)
    assert len(request_a["messages"]) == 143
    assert len(request_b["messages"]) == 19
    assert request_a["forbidden"] == case_a["forbidden_arguments"][0]


def test_row18_judges_official_tool_name_contract() -> None:
    case_a = _toolcall_arguments_recovery.load_sample(
        FIXTURE_DIR / _toolcall_arguments_recovery.CASE_FILES["a"]
    )
    case_b = _toolcall_arguments_recovery.load_sample(
        FIXTURE_DIR / _toolcall_arguments_recovery.CASE_FILES["b"]
    )
    request_a = _toolcall_arguments_recovery.prepare_request(case_a, "a")
    request_b = _toolcall_arguments_recovery.prepare_request(case_b, "b")

    assert not _toolcall_arguments_recovery.judge(
        "a", request_a, "tool_calls", "another_tool", '{"command":"pwd"}'
    )
    assert not _toolcall_arguments_recovery.judge(
        "b", request_b, "tool_calls", "WriteFile", '{"path":"x","content":"y"}'
    )
    assert not _toolcall_arguments_recovery.judge(
        "b", request_b, "tool_calls", "SetTodoList", '{"todos":[]}'
    )
    assert "tool_call arguments are empty" in _toolcall_arguments_recovery.judge(
        "b", request_b, "tool_calls", "WriteFile", ""
    )
    assert "expected one of" in " ".join(
        _toolcall_arguments_recovery.judge(
            "b", request_b, "tool_calls", "exec", '{"path":"x"}'
        )
    )


def test_row18_stream_aggregates_by_index_and_selects_index_zero() -> None:
    finish_reason, tool_name, arguments = _toolcall_arguments_recovery.call_model(
        FakeClient(
            "WriteFile",
            '{"path":"first","content":"valid"}',
            [("WriteFile", '{"path":"second","content":"also valid"}')],
        ),
        "model",
        {"messages": [], "tools": [], "thinking": {"type": "enabled"}},
    )

    assert finish_reason == "tool_calls"
    assert tool_name == "WriteFile"
    assert arguments == '{"path":"first","content":"valid"}'
    assert json.loads(arguments)["path"] == "first"


def test_row18_runner_writes_case_evidence(tmp_path: Path) -> None:
    case_a, passed_a = _toolcall_arguments_recovery.run_case(
        FakeClient("exec", '{"command":"pwd"}'),
        "model",
        "a",
        FIXTURE_DIR,
        tmp_path,
        1,
    )
    case_b, passed_b = _toolcall_arguments_recovery.run_case(
        FakeClient("WriteFile", '{"path":"x","content":"y"}'),
        "model",
        "b",
        FIXTURE_DIR,
        tmp_path,
        1,
    )

    assert passed_a and passed_b
    assert case_a["passed"] == 1
    assert case_b["passed"] == 1
    assert json.loads((tmp_path / "case-a.json").read_text())["model_failures"] == 0
    assert json.loads((tmp_path / "case-b.json").read_text())["model_failures"] == 0
