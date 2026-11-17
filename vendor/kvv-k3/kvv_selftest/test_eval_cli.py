"""Tests for the eval.py CLI plumbing of max_tokens / max_completion_tokens
and reasoning_effort (the new eval command surface)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval as eval_module  # noqa: E402  (repo's eval.py, not the builtin)


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict:
    """Run eval.py main() with a fake inspect_ai eval; return captured kwargs."""
    captured: dict = {}

    def fake_eval(tasks, model, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(eval_module, "eval", fake_eval)
    monkeypatch.setattr(sys, "argv", ["eval.py"] + argv)
    eval_module.main()
    return captured


def test_cli_rejects_both_token_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval.py",
            "ocrbench",
            "--model",
            "kimi/x",
            "--max-tokens",
            "100",
            "--max-completion-tokens",
            "100",
        ],
    )
    with pytest.raises(SystemExit):
        eval_module.main()


def test_cli_rejects_missing_token_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["eval.py", "ocrbench", "--model", "kimi/x"])
    with pytest.raises(SystemExit):
        eval_module.main()


def test_cli_max_tokens_keeps_legacy_wire_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _run_main(
        monkeypatch, ["ocrbench", "--model", "kimi/x", "--max-tokens", "100"]
    )
    assert captured["max_tokens"] == 100
    assert captured["model_args"]["use_max_completion_tokens"] is False


def test_cli_max_completion_tokens_switches_wire_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _run_main(
        monkeypatch,
        ["ocrbench", "--model", "kimi/x", "--max-completion-tokens", "100"],
    )
    # inspect-side still sees max_tokens; the wire rename happens in KimiAPI.
    assert captured["max_tokens"] == 100
    assert captured["model_args"]["use_max_completion_tokens"] is True


def test_cli_reasoning_effort_flows_to_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _run_main(
        monkeypatch,
        [
            "ocrbench",
            "--model",
            "kimi/x",
            "--max-completion-tokens",
            "100",
            "--think-mode",
            "kimi",
            "--thinking-effort",
            "none",
        ],
    )
    assert captured["extra_body"] == {
        "thinking": {"type": "disabled"},
        "reasoning_effort": "none",
    }
