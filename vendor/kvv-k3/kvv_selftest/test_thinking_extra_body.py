"""Tests for get_thinking_extra_body: thinking.effort / reasoning_effort coexistence.

Contract: when an effort is given, BOTH the legacy form (thinking object for
kimi mode, chat_template_kwargs for opensource) and the top-level
reasoning_effort are sent. "none" disables thinking in both forms.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval import get_thinking_extra_body

EFFORTS = ["low", "high", "max"]


def test_kimi_compat_without_effort() -> None:
    assert get_thinking_extra_body(True, "kimi") == {
        "thinking": {"type": "enabled"}
    }
    assert get_thinking_extra_body(False, "kimi") == {
        "thinking": {"type": "disabled"}
    }


@pytest.mark.parametrize("effort", EFFORTS)
def test_kimi_effort_sends_both_forms(effort: str) -> None:
    body = get_thinking_extra_body(True, "kimi", effort)
    assert body == {
        "thinking": {"type": "enabled", "keep": "all", "effort": effort},
        "reasoning_effort": effort
    }


@pytest.mark.parametrize("thinking", [True, False])
def test_kimi_effort_none_disables_thinking(thinking: bool) -> None:
    # "none" is equivalent to non-thinking in both forms; thinking.effort has
    # no "none" value, so the legacy object expresses it with type=disabled.
    assert get_thinking_extra_body(thinking, "kimi", "none") == {
        "thinking": {"type": "disabled"},
        "reasoning_effort": "none",
    }


def test_kimi_effort_without_thinking_flag() -> None:
    # An explicit effort implies thinking is enabled in both forms,
    # so the --thinking flag is not needed.
    assert get_thinking_extra_body(False, "kimi", "max") == {
        "thinking": {"type": "enabled", "keep": "all", "effort": "max"},
        "reasoning_effort": "max",
    }


def test_opensource_k2_legacy_without_effort() -> None:
    assert get_thinking_extra_body(True, "opensource") == {
        "chat_template_kwargs": {"thinking": True}
    }
    assert get_thinking_extra_body(False, "opensource") == {
        "chat_template_kwargs": {"thinking": False}
    }


@pytest.mark.parametrize("effort", EFFORTS)
def test_opensource_k3_sends_both_forms(effort: str) -> None:
    body = get_thinking_extra_body(True, "opensource", effort)
    assert body == {
        "reasoning_effort": effort,
    }


def test_opensource_k3_effort_none_disables_thinking() -> None:
    assert get_thinking_extra_body(False, "opensource", "none") == {
        "reasoning_effort": "none",
    }


@pytest.mark.parametrize("thinking", [True, False])
@pytest.mark.parametrize("mode", ["kimi", "opensource"])
@pytest.mark.parametrize("effort", [None] + EFFORTS)
def test_coexistence_invariants(thinking: bool, mode: str, effort: str | None) -> None:
    body = get_thinking_extra_body(thinking, mode, effort)
    legacy_key = "thinking" if mode == "kimi" else "chat_template_kwargs"
    # reasoning_effort is present if an effort was given, and never null.
    if effort is None:
        assert "reasoning_effort" not in body
        legacy=body.get(legacy_key, {})
        if thinking:
            assert legacy.get("type") == "enabled" or legacy.get("thinking") is True
        else:
            assert legacy.get("type") == "disabled" or legacy.get("thinking") is False
    else:
        assert body["reasoning_effort"] == effort
