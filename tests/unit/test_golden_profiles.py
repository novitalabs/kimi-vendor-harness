from __future__ import annotations

import json
from pathlib import Path

from novita_self_test.config import ModelProfile


ROOT = Path(__file__).resolve().parents[2]


def test_active_profile_golden_acceptance_contracts() -> None:
    for model_id in ("kimi-k3", "kimi-k3-flash"):
        golden = json.loads((ROOT / "tests/golden" / f"{model_id}.acceptance.json").read_text())
        profile = ModelProfile.load(model_id, allow_blocked=True)
        row_ids = [row["row_id"] for _, row in profile.official_rows()]
        assert profile.source["snapshot_revision"] == golden["snapshot_revision"]
        assert len(row_ids) == golden["official_row_count"]
        assert row_ids == golden["row_ids"]


def test_k3_flash_keeps_its_external_and_threshold_boundaries() -> None:
    profile = ModelProfile.load("kimi-k3-flash", allow_blocked=True)
    assert [row["row_id"] for _, row in profile.official_rows()] == [
        "1.0",
        "1.1",
        *[str(value) for value in range(2, 11)],
    ]
    suite_ids = {suite.suite_id for suite in profile.suites}
    assert {"stream-spec-p2", "toolcall-arguments-recovery", "beam-1m"}.isdisjoint(
        suite_ids
    )
    assert profile.source["verifier_repository"]["commit"] == (
        "6ec0eaad1ca98933327cf571c0d8b78ab8476551"
    )
    toolcall = next(suite for suite in profile.suites if suite.suite_id == "toolcall-thinking-max")
    assert toolcall.config["command"][toolcall.config["command"].index("--epochs") + 1] == "5"
    assert toolcall.completeness["expected_counts"] == {"total": 355, "unscored": 0}
    assert toolcall.config["runner_fingerprint"]["epochs"] == "5"
    deepswe = next(suite for suite in profile.suites if suite.suite_id == "deepswe")
    assert deepswe.config["min_score"] == 0.628
    assert profile.source["official_wiki_url"].endswith("Av7gw9Fg6il1VKkVFMEc84fRn9g")
