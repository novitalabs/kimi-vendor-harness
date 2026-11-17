"""Tests for the max_tokens / max_completion_tokens wire-parameter switch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from inspect_ai.model import GenerateConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kimi_model import KimiAPI


def _api(monkeypatch: pytest.MonkeyPatch, use_max_completion_tokens: bool) -> KimiAPI:
    monkeypatch.setenv("KIMI_API_KEY", "dummy-key")
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.example.com/v1")
    return KimiAPI(
        "kimi/test-model", use_max_completion_tokens=use_max_completion_tokens
    )


def test_max_tokens_default_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(monkeypatch, False)
    params = api.completion_params(GenerateConfig(max_tokens=123), tools=False)
    assert params["max_tokens"] == 123
    assert "max_completion_tokens" not in params


def test_max_completion_tokens_replaces_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(monkeypatch, True)
    params = api.completion_params(GenerateConfig(max_tokens=123), tools=False)
    assert params["max_completion_tokens"] == 123
    assert "max_tokens" not in params


def test_max_completion_tokens_noop_without_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api(monkeypatch, True)
    params = api.completion_params(GenerateConfig(), tools=False)
    assert "max_tokens" not in params
    assert "max_completion_tokens" not in params
