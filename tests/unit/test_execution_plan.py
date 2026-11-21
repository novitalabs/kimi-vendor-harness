"""Frozen-plan integrity/drift tests never send HTTP or depend on a real service."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from novita_self_test.execution_plan import create_plan, load_plan, verify_plan
from novita_self_test.config import ConfigError, ServiceAccess, canonical_hash
from novita_self_test.contracts import validate_contract

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def checkout(tmp_path):
    for name in ("src", "schemas", "capabilities", "knowledge", "profiles", "sources"):
        shutil.copytree(ROOT / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    return tmp_path


@pytest.fixture
def service():
    return ServiceAccess.from_dict({"model_family": "kimi-k3", "base_url": "http://127.0.0.1:1/v1",
        "served_model": "fake", "api_key_env": "PLAN_TEST_KEY", "headers": {}, "think_mode": "opensource",
        "deployment_id": "fake-deployment", "runtime_id": "fake-runtime", "source_targets": []})


def reseal(plan):
    plan["plan_sha256"] = canonical_hash({key: value for key, value in plan.items() if key != "plan_sha256"})
    return plan


def test_target_bound_plan_roundtrips_and_preserves_exact_wire_units(checkout, service):
    plan = create_plan(model="kimi-k3", selectors=["smoke"], service=service, root=checkout, max_requests=13)
    validate_contract(plan, "plan.schema.json")
    path = checkout / "plan.json"
    path.write_text(json.dumps(plan))
    assert load_plan(path) == plan
    prepared = verify_plan(plan, service, root=checkout)
    assert len(prepared) == plan["request_budget"] == len(plan["units"]) == 13
    for unit, row in zip(plan["units"], prepared):
        assert unit["request_sha256"] == canonical_hash(row[-2])
        assert unit["expect_sha256"] == canonical_hash(row[-1])


def test_oversized_target_bound_plan_rejects_before_expanding_requests(checkout, service, monkeypatch):
    from novita_self_test import execution_plan
    def forbidden(*args, **kwargs):
        pytest.fail("oversized plan materialized requests")
    monkeypatch.setattr(execution_plan, "prepare_steps", forbidden)
    with pytest.raises(ConfigError) as exc:
        create_plan(model="kimi-k3", service=service, root=checkout, repeat=10**8, max_requests=2)
    assert exc.value.code == "request_budget_exceeded"


def test_discovery_plan_requires_explicit_target_before_execution(checkout, service):
    plan = create_plan(model="kimi-k3", root=checkout)
    assert plan["executable"] is False
    assert plan["target"] is None
    with pytest.raises(ConfigError) as exc:
        verify_plan(plan, service, root=checkout)
    assert exc.value.code == "plan_target_required"


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(request_budget=0),
    lambda p: p["cases"][0]["steps"][0]["expect"].update(status=500),
    lambda p: p["execution"].update(max_requests=1000),
    lambda p: p["units"][0].update(request_sha256="0" * 64),
])
def test_modified_plan_content_fails_integrity_without_trusting_budget(checkout, service, mutate):
    plan = create_plan(model="kimi-k3", service=service, root=checkout)
    mutate(plan)
    with pytest.raises(ConfigError) as exc:
        verify_plan(plan, service, root=checkout)
    assert exc.value.code == "plan_integrity_invalid"


def test_rehashed_plan_cannot_replace_catalog_requests_or_oracles(checkout, service):
    plan = create_plan(model="kimi-k3", service=service, root=checkout)
    plan["cases"][0]["steps"][0]["request"]["messages"][0]["content"] = "changed request"
    with pytest.raises(ConfigError) as exc:
        verify_plan(reseal(plan), service, root=checkout)
    assert exc.value.code == "plan_drift"


@pytest.mark.parametrize("path", [
    "src/novita_self_test/capability_runtime.py", "schemas/capability-case.schema.json",
    "profiles/kimi-k3.yaml", "sources/provenance.yaml", "capabilities/tool_call/choice/cases.jsonl",
    "capabilities/conversation_state/fixtures/case-a-invalid-arguments.jsonl",
])
def test_file_byte_drift_is_rejected_even_if_semantics_are_identical(checkout, service, path):
    plan = create_plan(model="kimi-k3", selectors=["tool_call.choice.required", "conversation_state.arguments_recovery_a"], service=service, root=checkout)
    file = checkout / path
    file.write_bytes(file.read_bytes() + b"\n")
    with pytest.raises(ConfigError) as exc:
        verify_plan(plan, service, root=checkout)
    assert exc.value.code == "plan_drift"


@pytest.mark.parametrize("field,value", [
    ("base_url", "http://127.0.0.1:2/v1"), ("served_model", "different"), ("think_mode", "kimi"),
    ("headers", {"x-route": "changed"}), ("api_key_env", "OTHER_KEY"),
    ("deployment_id", "new-deployment"), ("runtime_id", "new-runtime"),
])
def test_public_target_identity_is_bound(checkout, service, field, value):
    plan = create_plan(model="kimi-k3", service=service, root=checkout)
    with pytest.raises(ConfigError) as exc:
        verify_plan(plan, replace(service, **{field: value}), root=checkout)
    assert exc.value.code == "plan_target_mismatch"


def test_credentials_can_rotate_without_being_embedded_in_plan(checkout, service, monkeypatch):
    monkeypatch.setenv("PLAN_TEST_KEY", "synthetic-before-key")
    plan = create_plan(model="kimi-k3", service=service, root=checkout)
    monkeypatch.setenv("PLAN_TEST_KEY", "synthetic-after-key")
    verify_plan(plan, service, root=checkout)
    assert "synthetic-before-key" not in json.dumps(plan)
    assert "synthetic-after-key" not in json.dumps(plan)


@pytest.mark.parametrize("variable,value", [("NVST_TOKEN_PARAM", "max_completion_tokens"), ("NO_PROXY", "different")])
def test_wire_or_transport_config_drift_is_rejected(checkout, service, monkeypatch, variable, value):
    monkeypatch.setenv("NVST_TOKEN_PARAM", "max_tokens")
    plan = create_plan(model="kimi-k3", service=service, root=checkout)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigError) as exc:
        verify_plan(plan, service, root=checkout)
    assert exc.value.code == "plan_drift"


def test_plan_relocation_between_identical_checkouts_is_supported(checkout, service, tmp_path):
    plan = create_plan(model="kimi-k3", service=service, root=checkout)
    clone = tmp_path.parent / (tmp_path.name + "-relocated")
    shutil.copytree(checkout, clone)
    assert len(verify_plan(plan, service, root=clone)) == 13


def test_loaded_plan_does_not_allow_non_json_constants_or_unknown_fields(tmp_path):
    path = tmp_path / "invalid.json"
    for text in ('{"request_budget":NaN}', '{"kind":"endpoint_plan","unknown":true}'):
        path.write_text(text)
        with pytest.raises(ConfigError) as exc:
            load_plan(path)
        assert exc.value.code == "plan_invalid"
