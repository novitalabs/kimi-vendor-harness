from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

import pytest

from scripts import nvst_credentials


POLARIS_AUTH = "Authorization: " + "Bearer " + "polaris-runtime-secret"
PPIO_AUTH = "Authorization: " + "Bearer " + "ppio-runtime-secret"


def set_args(path: Path, name: str, **overrides) -> argparse.Namespace:
    values = {
        "registry": path,
        "name": name,
        "role": ["endpoint"],
        "exact_host": [],
        "host_suffix": [],
        "path_prefix": [],
        "default_judge": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_registry_is_0600_and_lists_no_secret(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config/credentials.json"
    monkeypatch.setattr(nvst_credentials.sys, "stdin", io.StringIO(POLARIS_AUTH))
    result = nvst_credentials.set_credential(
        set_args(
            path,
            "polaris-endpoint",
            host_suffix=["polaris.ppio.com"],
        )
    )

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert result["fingerprint"] == nvst_credentials.fingerprint(POLARIS_AUTH)
    public = nvst_credentials.list_registry(argparse.Namespace(registry=path))
    rendered = json.dumps(public)
    assert "polaris-runtime-secret" not in rendered
    assert public["mode"] == "0o600"


def test_registry_resolves_by_host_path_and_role(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "credentials.json"
    monkeypatch.setattr(nvst_credentials.sys, "stdin", io.StringIO(POLARIS_AUTH))
    nvst_credentials.set_credential(
        set_args(
            path,
            "polaris-endpoint",
            host_suffix=[".polaris.ppio.com"],
        )
    )
    monkeypatch.setattr(nvst_credentials.sys, "stdin", io.StringIO(PPIO_AUTH))
    nvst_credentials.set_credential(
        set_args(
            path,
            "ppio-openai",
            role=["endpoint", "judge"],
            exact_host=["api.ppio.com"],
            path_prefix=["/openai/v1"],
            default_judge=True,
        )
    )

    with nvst_credentials.locked_registry(path) as registry:
        name, item = nvst_credentials.resolve_credential(
            registry,
            url="https://tenant-a.polaris.ppio.com/v1/chat/completions",
            role="endpoint",
            name=None,
        )
        assert name == "polaris-endpoint"
        assert item["authorization"] == POLARIS_AUTH
        name, item = nvst_credentials.resolve_credential(
            registry,
            url="https://api.ppio.com/openai/v1/chat/completions",
            role="endpoint",
            name=None,
        )
        assert name == "ppio-openai"
        assert item["authorization"] == PPIO_AUTH
        name, _ = nvst_credentials.resolve_credential(
            registry,
            url=None,
            role="judge",
            name=None,
        )
        assert name == "ppio-openai"


def test_registry_rejects_insecure_permissions(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "credentials.json"
    monkeypatch.setattr(nvst_credentials.sys, "stdin", io.StringIO(POLARIS_AUTH))
    nvst_credentials.set_credential(
        set_args(path, "polaris-endpoint", host_suffix=["polaris.ppio.com"])
    )
    path.chmod(0o644)

    with pytest.raises(PermissionError, match="chmod 0600"):
        with nvst_credentials.locked_registry(path):
            pass


def test_registry_rejects_ambiguous_matches(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "credentials.json"
    for name, authorization in (("first-token", POLARIS_AUTH), ("second-token", PPIO_AUTH)):
        monkeypatch.setattr(nvst_credentials.sys, "stdin", io.StringIO(authorization))
        nvst_credentials.set_credential(
            set_args(path, name, exact_host=["api.example.com"])
        )

    with nvst_credentials.locked_registry(path) as registry:
        with pytest.raises(ValueError, match="ambiguous"):
            nvst_credentials.resolve_credential(
                registry,
                url="https://api.example.com/v1/chat/completions",
                role="endpoint",
                name=None,
            )
