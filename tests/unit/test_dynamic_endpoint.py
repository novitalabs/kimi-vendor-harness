from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from novita_self_test.config import ConfigError, ServiceAccess
from scripts.nvst_dynamic_endpoint import (
    JUDGE_SECRET_ENV,
    SECRET_ENV,
    main,
    materialize_service_access,
    parse_authorization,
    parse_secret_input,
)


AUTHORIZATION = "Authorization: " + "Bearer " + "runtime-secret"
JUDGE_AUTHORIZATION = "Authorization: " + "Bearer " + "judge-secret"


def test_parse_authorization_requires_complete_bearer_header() -> None:
    assert parse_authorization(AUTHORIZATION + "\n") == "runtime-secret"
    with pytest.raises(ConfigError) as missing_name:
        parse_authorization("Bearer runtime-secret")
    assert missing_name.value.code == "authorization_header_invalid"
    with pytest.raises(ConfigError):
        parse_authorization("Authorization: Basic runtime-secret")


def test_parse_secret_input_keeps_endpoint_and_judge_credentials_separate() -> None:
    endpoint, judge = parse_secret_input(AUTHORIZATION + "\n" + JUDGE_AUTHORIZATION)
    assert endpoint == "runtime-secret"
    assert judge == "judge-secret"
    assert parse_secret_input(AUTHORIZATION) == ("runtime-secret", None)


def test_materialized_service_access_is_deterministic_and_secret_free(tmp_path: Path) -> None:
    first = materialize_service_access(
        base_url="https://vendor.example/v1/",
        served_model="kimi-k3",
        model_family="kimi-k3",
        think_mode="opensource",
        headers_json='{"X-Route":"pool-a"}',
        directory=tmp_path,
    )
    second = materialize_service_access(
        base_url="https://vendor.example/v1",
        served_model="kimi-k3",
        model_family="kimi-k3",
        think_mode="opensource",
        headers_json='{"X-Route":"pool-a"}',
        directory=tmp_path,
    )

    assert first == second
    assert first.stat().st_mode & 0o777 == 0o600
    payload = json.loads(first.read_text())
    assert payload["api_key_env"] == SECRET_ENV
    assert payload["base_url"] == "https://vendor.example/v1"
    assert "runtime-secret" not in first.read_text()
    assert ServiceAccess.load(first).served_model == "kimi-k3"


def test_materialized_service_access_rejects_secret_headers(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        materialize_service_access(
            base_url="https://vendor.example/v1",
            served_model="kimi-k3",
            model_family="kimi-k3",
            think_mode="opensource",
            headers_json=json.dumps({"Authorization": "Bearer " + "forbidden"}),
            directory=tmp_path,
        )
    assert error.value.code == "credential_material_forbidden"


def test_dynamic_endpoint_injects_service_access_and_process_secret(
    tmp_path: Path, monkeypatch
) -> None:
    observed: list[str] = []
    monkeypatch.setattr(
        "scripts.nvst_dynamic_endpoint.sys.stdin", io.StringIO(AUTHORIZATION + "\n")
    )
    monkeypatch.setattr(
        "scripts.nvst_dynamic_endpoint.nvst_main",
        lambda argv: observed.extend(argv) or 0,
    )

    assert main(
        [
            "--base-url",
            "https://vendor.example/v1",
            "--served-model",
            "kimi-k3",
            "--headers-json",
            '{"X-Route":"pool-a"}',
            "--config-dir",
            str(tmp_path),
            "--",
            "develop",
            "--model",
            "kimi-k3",
            "--select",
            "3",
        ]
    ) == 0
    assert observed[0] == "develop"
    service_path = Path(observed[observed.index("--service-access") + 1])
    assert service_path.parent == tmp_path
    assert os.environ[SECRET_ENV] == "runtime-secret"
    assert "runtime-secret" not in service_path.read_text()


def test_dynamic_endpoint_doctor_injects_service_access(
    tmp_path: Path, monkeypatch
) -> None:
    observed: list[str] = []
    monkeypatch.setattr(
        "scripts.nvst_dynamic_endpoint.sys.stdin",
        io.StringIO(AUTHORIZATION + "\n" + JUDGE_AUTHORIZATION + "\n"),
    )
    monkeypatch.setattr(
        "scripts.nvst_dynamic_endpoint.nvst_main",
        lambda argv: observed.extend(argv) or 0,
    )

    assert main(
        [
            "--base-url",
            "https://vendor.example/v1",
            "--served-model",
            "kimi-k3",
            "--config-dir",
            str(tmp_path / "endpoints"),
            "--judge-config-dir",
            str(tmp_path / "judges"),
            "--",
            "doctor",
            "--model",
            "kimi-k3",
        ]
    ) == 0
    assert observed[0] == "doctor"
    assert "--service-access" in observed
    assert "--judge-access" in observed
    judge_path = Path(observed[observed.index("--judge-access") + 1])
    assert judge_path.parent == tmp_path / "judges"
    assert os.environ[JUDGE_SECRET_ENV] == "judge-secret"
    assert "judge-secret" not in judge_path.read_text()


def test_dynamic_endpoint_resume_does_not_override_manifest_service(
    tmp_path: Path, monkeypatch
) -> None:
    observed: list[str] = []
    monkeypatch.setattr(
        "scripts.nvst_dynamic_endpoint.sys.stdin", io.StringIO(AUTHORIZATION + "\n")
    )
    monkeypatch.setattr(
        "scripts.nvst_dynamic_endpoint.nvst_main",
        lambda argv: observed.extend(argv) or 0,
    )

    assert main(
        [
            "--base-url",
            "https://vendor.example/v1",
            "--served-model",
            "kimi-k3",
            "--config-dir",
            str(tmp_path),
            "--",
            "resume",
            "--run",
            "/server/run",
        ]
    ) == 0
    assert observed == ["resume", "--run", "/server/run"]
