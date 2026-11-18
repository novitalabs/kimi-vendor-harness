from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import yaml

from novita_self_test.artifacts import SuiteResult
from novita_self_test.cli import _list
from novita_self_test.config import ModelProfile
from novita_self_test.reporting import build_report


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_ROWS = ["1.0", "1.1", "1.2", *[str(index) for index in range(2, 19)]]
EXPECTED_SUPPLEMENTARY_ROWS = ["K3-CB"]
EXPECTED_SUITES = [
    "stream-spec-p0",
    "stream-spec-p1",
    "stream-spec-p2",
    "image-test",
    "kvv-params",
    "walle",
    "kvv-k3-features",
    "kvv-prompt-tokens",
    "toolcall-thinking-low",
    "toolcall-thinking-high",
    "toolcall-thinking-max",
    "inspect-ocr-low",
    "inspect-ocr-high",
    "inspect-ocr-max",
    "inspect-mmmu-low",
    "inspect-mmmu-high",
    "inspect-mmmu-max",
    "beam-1m",
    "deepswe",
]
EXPECTED_SUPPLEMENTARY_SUITES = ["kimi-code-bench"]


def test_k3_rows_follow_feishu_table_order() -> None:
    profile = ModelProfile.load("kimi-k3")
    rows = [row for _, row in profile.official_rows()]

    assert profile.snapshot_revision == (
        "k3-vendor-test-result-docx-444-stream-244-toolcall-docx-1200-sheet-170-kvv-6ec0eaad"
    )
    assert [row["row_id"] for row in rows] == EXPECTED_ROWS
    assert [row["table_index"] for row in rows] == list(range(20))
    assert rows[0]["job"] == "Stream spec / p0"
    assert rows[1]["job"] == "Stream spec / p1"
    assert rows[2]["job"] == "Stream spec / p2"
    assert rows[8]["job"] == "Toolcall thinking effort / low"
    assert rows[10]["job"] == "Toolcall thinking effort / max"
    assert rows[11]["job"] == "OCRBench / effort low"
    assert rows[14]["job"] == "MMMU Pro / effort low"
    assert rows[17]["job"] == "BEAM 1M"
    assert rows[-2]["job"] == "DeepSWE"
    assert rows[-1]["job"] == "Toolcall arguments recovery / invalid and truncated arguments"
    assert [row["row_id"] for _, row in profile.supplementary_rows()] == EXPECTED_SUPPLEMENTARY_ROWS


def test_k3_suites_are_one_to_one_and_follow_table_order() -> None:
    profile = ModelProfile.load("kimi-k3")

    official_suites = [suite for suite in profile.suites if suite.row_ids[0] in EXPECTED_ROWS]
    assert [suite.suite_id for suite in official_suites] == EXPECTED_SUITES + ["toolcall-arguments-recovery"]
    assert [suite.suite_id for suite in profile.suites if suite.row_ids[0] in EXPECTED_SUPPLEMENTARY_ROWS] == EXPECTED_SUPPLEMENTARY_SUITES
    assert [list(suite.row_ids) for suite in official_suites] == [
        [row_id] for row_id in EXPECTED_ROWS
    ]
    assert all(row_id in suite.selectors for row_id, suite in zip(EXPECTED_ROWS, official_suites))
    assert [suite.long_test for suite in profile.suites if suite.long_test] == [
        "deepswe",
        "code-bench",
    ]
    expected_runner_owned = {
        "1.0": 2,
        "1.1": 2,
        "1.2": 2,
        "4": 3,
        "5": 5,
        **{str(row): 3 for row in range(7, 16)},
        "16": 8,
        "18": 0,
    }
    for suite in profile.suites:
        row_id = suite.row_ids[0]
        policy = suite.config["rerun_policy"]
        if row_id == "K3-CB":
            assert policy == {
                "mode": "runner_owned",
                "max_reruns": 1,
                "scheduling": "immediate",
            }
            continue
        if row_id in expected_runner_owned:
            assert policy == {
                "mode": "runner_owned",
                "max_reruns": expected_runner_owned[row_id],
                "scheduling": "immediate",
            }
        elif row_id == "17":
            assert policy == {
                "mode": "error_only",
                "max_reruns": 1,
                "scheduling": "batch_after_formal",
            }
        else:
            assert policy == {
                "mode": "any_pass",
                "max_reruns": 3,
                "scheduling": "batch_after_formal",
            }
    assert "stress-24h" not in {suite.suite_id for suite in profile.suites}


def test_k3_acceptance_modes_pin_scope_and_deepswe_rounds() -> None:
    profile = ModelProfile.load("kimi-k3")
    assert profile.profile["test_groups"] == {
        "precision": ["7", "8", "9", "10", "11", "12", "13", "14", "15", "17"],
        "feature": ["1.0", "1.1", "1.2", "2", "3", "4", "5", "6", "18"],
        "optional_precision": ["16", "kimi-code-bench:extended"],
    }
    simple = profile.acceptance_mode_config("simple")
    full = profile.acceptance_mode_config("full")
    sop = profile.acceptance_mode_config("k3-sop")
    assert simple["selectors"] == [
        "10", "11", "12", "17", "1.0", "1.1", "1.2", "2", "3", "4", "5", "6", "18"
    ]
    assert simple["deepswe_rounds"] == 1
    assert full["selectors"] == [
        "7", "8", "9", "10", "11", "12", "13", "14", "15", "17",
        "1.0", "1.1", "1.2", "2", "3", "4", "5", "6", "18"
    ]
    assert full["deepswe_rounds"] == 3
    assert simple["include_long"] == ["deepswe"]
    assert full["include_long"] == ["deepswe"]
    assert "kimi-code-bench:extended" not in simple["selectors"]
    assert "kimi-code-bench:extended" not in full["selectors"]
    assert "16" not in simple["selectors"]
    assert "16" not in full["selectors"]
    assert sop["source"] == "moonshot-k3-sop"
    assert sop["source_revision"] == 9
    assert sop["selectors"] == [
        "7", "8", "9", "10", "11", "12", "13", "14", "15",
        "5", "6", "3", "4", "1.0", "1.1", "2", "17", "18",
    ]
    assert sop["include_long"] == ["deepswe"]
    assert sop["deepswe_rounds"] == 3


def test_k3_precision_core_mode_scopes_required_rows_7_to_9_and_deepswe() -> None:
    profile = ModelProfile.load("kimi-k3")
    core = profile.acceptance_mode_config("precision-core")
    assert core["selectors"] == ["7", "8", "9", "17"]
    assert core["include_long"] == ["deepswe"]
    assert core["deepswe_rounds"] == 3
    selected = [suite.suite_id for selector in core["selectors"] for suite, _ in profile.select([selector])]
    assert selected == [
        "toolcall-thinking-low",
        "toolcall-thinking-high",
        "toolcall-thinking-max",
        "deepswe",
    ]


def test_acceptance_mode_enum_is_consistent_across_schema_and_cli() -> None:
    """The run schema, CLI choices, and profile modes must agree."""
    import argparse

    from novita_self_test.cli import _parser

    schema = json.loads((ROOT / "schemas" / "run.schema.json").read_text())
    schema_modes = set(schema["properties"]["acceptance_mode"]["enum"])
    subparsers = next(
        action
        for action in _parser()._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    accept = subparsers.choices["accept"]
    cli_modes = {
        tuple(action.choices)
        for action in accept._actions
        if action.dest == "acceptance_mode"
    }
    profile = ModelProfile.load("kimi-k3")
    profile_modes = set(profile.profile["acceptance_modes"])
    assert len(cli_modes) == 1
    cli_choices = set(cli_modes.pop())
    # The schema also carries the develop mode used by the develop command.
    assert cli_choices <= schema_modes
    assert schema_modes - cli_choices == {"develop"}
    assert {"simple", "full", "precision-core", "k3-sop"} <= profile_modes


def test_k3_code_bench_is_optional_precision() -> None:
    profile = ModelProfile.load("kimi-k3")
    suite = next(item for item in profile.suites if item.suite_id == "kimi-code-bench")
    assert suite.test_class == "precision"
    assert suite.mandatory is False
    assert suite.row_ids == ("K3-CB",)
    assert suite.long_test == "code-bench"
    assert suite.config["concurrency"] == 89
    assert suite.config["runner_fingerprint"]["per_task_concurrency"] == "1"
    assert suite.config["runner_fingerprint"]["concurrency_scope"] == "task_count"
    assert suite.config["runner_fingerprint"]["schedule"] == "task_pipelines"
    assert suite.config["runner_fingerprint"]["round_scheduling"] == "task_pipelines"
    assert suite.completeness["selector_completeness"]["kimi-code-bench:extended"] == {
        "expected_tasks": 89,
        "expected_trials": 267,
        "required_artifacts": ["finalization-manifest.json", "adapter-result.json"],
    }
    beam = next(item for item in profile.suites if item.suite_id == "beam-1m")
    assert beam.mandatory is False


def test_k3_suites_have_complete_feature_precision_classification() -> None:
    profile = ModelProfile.load("kimi-k3")
    by_class = {
        "feature": {suite.suite_id for suite in profile.suites if suite.test_class == "feature"},
        "precision": {suite.suite_id for suite in profile.suites if suite.test_class == "precision"},
    }
    assert by_class == {
        "feature": {
            "stream-spec-p0",
            "stream-spec-p1",
            "stream-spec-p2",
            "image-test",
            "kvv-params",
            "walle",
            "kvv-k3-features",
            "kvv-prompt-tokens",
            "toolcall-arguments-recovery",
        },
        "precision": {
            "toolcall-thinking-low",
            "toolcall-thinking-high",
            "toolcall-thinking-max",
            "inspect-ocr-low",
            "inspect-ocr-high",
            "inspect-ocr-max",
            "inspect-mmmu-low",
            "inspect-mmmu-high",
            "inspect-mmmu-max",
            "beam-1m",
            "deepswe",
            "kimi-code-bench",
        },
    }


def test_k3_source_pins_feishu_revision_444() -> None:
    source = yaml.safe_load(
        (ROOT / "models/kimi-k3/requirements/source.yaml").read_text(encoding="utf-8")
    )

    assert source["sop_contract"] == {
        "wiki_url": "https://moonshot.feishu.cn/wiki/VtHAwlYP7imapik8C4scELvgnxc",
        "doc_revision": 9,
        "content_sha256": "387d7d7490df14f200a02e77dace7b58578661bbf8fba487bbb57909ef1ad561",
        "fetched_at": "2026-09-10T00:00:00+08:00",
        "required_checks": [
            "toolcall_thinking_effort",
            "ocrbench",
            "MMMU_pro",
            "k3_new_feature",
            "prompt_tokens",
            "params",
            "tool_call_json_schema",
            "streaming_output_spec_p0_p1",
            "vendor_own_tests",
        ],
    }

    assert source["wiki_node_token"] == "IMc2wtkCkit5c0kGRTfcuYDnn8d"
    assert source["doc_token"] == "NGZxd8CYgosLA3xkPfjcbfnlnIf"
    assert source["doc_revision"] == 444
    assert source["stream_spec_revision"] == 244
    assert source["table_contract"] == {
        "title": "K3-vendor-test-result",
        "numbered_items": 15,
        "executable_rows": 15,
        "first_item": "0 Stream spec / p0",
        "last_item": "14 DeepSWE",
        "active_supplementary_items": ["Kimi Code bench"],
        "structured_rows_sha256": "aa4018242c7ecc963cbb08c514f7a02395d23730418c3e557f358675e8a6d1ec",
        "rows_file_sha256": "d58f71fd8764ff49a0b363981d5e7847bc5b5786ad7244e1f69b371a32807e75",
    }
    assert source["verifier_repository"] == {
        "remote": "https://github.com/MoonshotAI/kvv-k3.git",
        "branch": "k3",
        "commit": "6ec0eaad1ca98933327cf571c0d8b78ab8476551",
        "checkout": "vendor/kvv-k3",
        "git_lfs_required": True,
        "lfs_paths": [
            "beam/data/beam_1m_chats.jsonl.gz",
            "beam/data/beam_1m_questions.jsonl",
            "testdata/prompt_token_cases/*.jsonl",
            "testdata/prompt_token_cases/images/*.png",
            "testdata/toolcall_reject_cases/rejected_case.jsonl",
            "toolcall_benchmark/toolcall_thinking_samples.stable2.jsonl",
        ],
        "observed_source_inventory": {
            "prompt_token_collected_cases": 74,
            "toolcall_stable2_samples": 71,
            "toolcall_stable2_epochs": 5,
            "toolcall_stable2_trials": 355,
            "params_collected_cases": 19,
            "walle_collected_cases": 428,
            "k3_features_collected_cases": 118,
            "toolcall_reject_cases": 3,
        },
    }
    assert source["stream_spec_contract"] == {
        "wiki_node_token": "LP9BwrmUBiAjYakQ3RdcnpFknLh",
        "doc_token": "SgIHdan76oGuidxoYxrcrgrvnqY",
        "doc_revision": 244,
        "document_content_sha256": "4d90c41a1bbc76e87840eca3a62d51ad4df31a7209813ebd77afc1d9f3ad1a93",
        "snapshot_path": "suites/novita-api/tests/streaming/test_stream_spec.py",
        "sha256": "74edfb0d85a022714151c4da6600ba5adf472c243a3146d7a03b59f35e0b6a4e",
        "uncovered_policy": "default_pass",
    }
    assert source["beam_judge_contract"] == {
        "base_url": "https://api.ppio.com/openai/v1",
        "model": "moonshotai/kimi-k2.7-code",
        "api_key_env": "PPIO_JUDGE_API_KEY",
        "requests_per_minute": 500,
    }
    assert source["toolcall_thinking_contract"] == {
        "title": "K3 流量外采要求/K3 Serving Requirements",
        "wiki_node_token": "KYmQwcaibiBYxWkWjP3cun83n5e",
        "doc_token": "I2aKd9572oWM4cxXUVpcDdOanBg",
        "doc_revision": 1200,
        "document_content_sha256": (
            "9742f88420716e7fc64b39e60a5a94f3e38f95e35a62399a61c1bdbb0b8410ec"
        ),
        "spreadsheet_token": "Gii3sbMA1hAGIhtYtxxcgd3Zn3g",
        "sheet_id": "cPoUBI",
        "sheet_revision": 170,
        "sheet_range": "A1:C15",
        "sheet_annotated_csv_sha256": (
            "4175320678666174120352c0aedb5e101994be2429497e3e55918912dfc05b4e"
        ),
        "source_rows": [2, 3, 4],
        "row_order": ["low", "high", "max"],
        "fetched_at": "2026-08-25T01:59:21+08:00",
        "source_artifact_sha256": (
            "981e50da6b5971bb34279d96da5b45ef20387b33a7c2336585480ff4f018d44e"
        ),
        "comparison_artifact_sha256": (
            "df49110facae1df2ea15ab16ae77e3e4201032006ec4ec068f756991a80d7e2d"
        ),
    }


def test_stream_snapshot_and_priority_contract_are_official() -> None:
    profile = ModelProfile.load("kimi-k3")
    path = ROOT / "suites/novita-api/tests/streaming/test_stream_spec.py"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "74edfb0d85a022714151c4da6600ba5adf472c243a3146d7a03b59f35e0b6a4e"
    )

    expected_counts = {
        "P0": {"total": 25, "passed": 9, "skipped": 16, "failed": 0, "error": 0},
        "P1": {"total": 25, "passed": 12, "skipped": 13, "failed": 0, "error": 0},
        "P2": {"total": 25, "passed": 3, "skipped": 22, "failed": 0, "error": 0},
    }
    for suite_id, priority in (
        ("stream-spec-p0", "P0"),
        ("stream-spec-p1", "P1"),
        ("stream-spec-p2", "P2"),
    ):
        suite = next(item for item in profile.suites if item.suite_id == suite_id)
        assert suite.config["paths"] == [
            "suites/novita-api/tests/streaming/test_stream_spec.py"
        ]
        assert suite.config["env"]["STREAM_SPEC_PRIORITY"] == priority
        assert suite.completeness["expected_counts"] == expected_counts[priority]
        assert suite.config["runner_fingerprint"]["uncovered_policy"] == "default_pass"
        assert suite.completeness["required_artifacts"] == ["cases.jsonl", "summary.json"]


def test_image_fixture_exercises_the_documented_1000_image_case() -> None:
    path = ROOT / "suites/novita-api/fixtures/vendor-img-testcases-inhouse-3.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    image_counts = []
    for record in records:
        count = 0
        for message in record["transformed_request"].get("messages", []):
            content = message.get("content")
            if isinstance(content, list):
                count += sum(
                    isinstance(item, dict) and item.get("type") == "image_url"
                    for item in content
                )
        image_counts.append(count)

    assert len(records) == 25
    assert max(image_counts) == 1000


def test_k3_serial_runner_timeouts_are_explicit() -> None:
    profile = ModelProfile.load("kimi-k3")
    image = next(suite for suite in profile.suites if suite.suite_id == "image-test")
    assert image.config["timeout_seconds"] == 900
    assert image.config["runner_fingerprint"]["request_timeout_seconds"] == "900"

    recovery = next(
        suite for suite in profile.suites if suite.suite_id == "toolcall-arguments-recovery"
    )
    command = recovery.config["command"]
    assert command[command.index("--timeout") + 1] == "300"
    assert recovery.config["runner_fingerprint"]["request_timeout_seconds"] == "300"


def test_table_expected_results_match_runner_contracts() -> None:
    profile = ModelProfile.load("kimi-k3")
    rows = {row["row_id"]: row for _, row in profile.official_rows()}
    suites = {suite.suite_id: suite for suite in profile.suites}

    assert rows["2"]["official_result"] == {
        "kind": "count_only",
        "value": {"passed": 25, "skipped": 0},
    }
    assert suites["image-test"].completeness["expected_max_images"] == 1000
    assert rows["3"]["official_result"]["value"] == {"passed": 18, "skipped": 0}
    assert rows["4"]["official_result"]["value"] == {"passed": 408, "skipped": 0}
    assert rows["5"]["official_result"]["value"] == {"passed": 108, "skipped": 18}
    assert rows["6"]["official_result"]["value"] == {"passed": 59, "skipped": 0}
    assert rows["17"]["official_result"]["value"] == 0.667
    assert rows["7"]["official_result"]["value"] == [0.82, 0.78]
    assert rows["8"]["official_result"]["value"] == [0.84, 0.82]
    assert rows["9"]["official_result"]["value"] == [0.87, 0.83]
    assert suites["deepswe"].config["min_score"] == 0.647
    assert suites["deepswe"].config["transport"] == "local"
    assert suites["deepswe"].config["concurrency"] == 113
    assert suites["deepswe"].config["max_concurrency"] == 113
    assert suites["deepswe"].config["max_reruns"] == 1
    assert suites["deepswe"].config["rerun_policy"] == {
        "mode": "error_only",
        "max_reruns": 1,
        "scheduling": "batch_after_formal",
    }
    assert suites["beam-1m"].config["env"] == {
        "NVST_BEAM_JUDGE_BASE_URL": "https://api.ppio.com/openai/v1",
        "NVST_BEAM_JUDGE_MODEL": "moonshotai/kimi-k2.7-code",
        "NVST_BEAM_JUDGE_API_KEY_ENV": "PPIO_JUDGE_API_KEY",
    }
    assert {
        key: suites["deepswe"].config[key]
        for key in (
            "expected_deep_swe_commit",
            "expected_pier_version",
            "expected_tasks",
        )
    } == {
        "expected_deep_swe_commit": "435ee89ec2f2e2289f33b0da4f992f0b7b7266b9",
        "expected_pier_version": "0.3.1",
        "expected_tasks": 113,
    }


def test_k3_rows_3_through_6_repeat_ten_times() -> None:
    profile = ModelProfile.load("kimi-k3")
    suites = {suite.row_ids[0]: suite for suite in profile.suites}
    expected = {
        "3": {"total": 19},
        "4": {"total": 428},
        "5": {"total": 118},
        "6": {"total": 74},
    }
    for row_id, expected_counts in expected.items():
        suite = suites[row_id]
        assert suite.config["repeat"] == 10
        assert suite.completeness["expected_repeats"] == 10
        assert suite.completeness["required_artifacts"] == ["repeat-summary.json"]
        assert suite.completeness["per_repeat"]["expected_counts"] == expected_counts
        assert "cases.jsonl" in suite.completeness["per_repeat"]["required_artifacts"]

    listed = {item["row_ids"][0]: item for item in _list("kimi-k3")["suites"]}
    assert {row_id: listed[row_id]["repeat"] for row_id in expected} == {
        row_id: 10 for row_id in expected
    }
    assert listed["3"]["entrypoint"] == "MoonshotAI/kvv-k3 pytest tests/params"
    assert listed["3"]["concurrency"]["concurrency"] == "19"
    assert listed["3"]["concurrency"]["concurrency_scope"] == "per_repeat_case_count"
    assert listed["3"]["round_scheduling"] == "round_barrier"
    assert listed["3"]["retry_policy"]["max_reruns"] == 3
    assert "summary.json" in listed["3"]["required_artifacts"]


def test_inspect_benchmark_commands_match_official_k3_parameters() -> None:
    profile = ModelProfile.load("kimi-k3")
    suites = {suite.suite_id: suite for suite in profile.suites}
    cases = [
        ("inspect-ocr-low", "ocrbench", "16384", "low", 0.90, 1000),
        ("inspect-ocr-high", "ocrbench", "16384", "high", 0.89, 1000),
        ("inspect-ocr-max", "ocrbench", "16384", "max", 0.89, 1000),
        ("inspect-mmmu-low", "mmmu", "98304", "low", 0.79, 1730),
        ("inspect-mmmu-high", "mmmu", "98304", "high", 0.81, 1730),
        ("inspect-mmmu-max", "mmmu", "98304", "max", 0.82, 1730),
    ]
    for suite_id, benchmark, max_tokens, effort, score, trials in cases:
        suite = suites[suite_id]
        command = suite.config["command"]
        assert command[1:4] == ["-m", "novita_self_test.adapters.inspect_benchmark_run", benchmark]
        assert command[command.index("--max-tokens") + 1] == max_tokens
        assert command[command.index("--temperature") + 1] == "1.0"
        assert command[command.index("--top-p") + 1] == "0.95"
        assert command[command.index("--thinking-effort") + 1] == effort
        assert command[command.index("--max-connections") + 1] == "900"
        assert command[command.index("--retry-on-error") + 1] == "3"
        assert command[command.index("--client-timeout") + 1] == "86400"
        assert suite.config["official_score"] == score
        assert suite.config["tolerance_mode"] == "lower_bound"
        assert suite.completeness["expected_trials"] == trials
        assert suite.completeness["required_artifact_patterns"] == ["**/*.eval"]


def test_k3_rows_have_explicit_entrypoint_concurrency_retry_and_artifacts() -> None:
    profile = ModelProfile.load("kimi-k3")
    for suite in profile.suites:
        fingerprint = suite.config.get("runner_fingerprint") or {}
        for key in ("entrypoint", "retry_owner", "retry_max_reruns", "retry_schedule", "artifact_contract"):
            assert fingerprint.get(key), f"{suite.suite_id} missing {key}"
        assert fingerprint.get("concurrency_scope"), f"{suite.suite_id} missing concurrency scope"
        assert suite.completeness.get("required_artifacts") or suite.completeness.get(
            "required_artifact_patterns"
        ) or suite.adapter == "deepswe"


def test_beam_declared_context_keeps_headroom_below_the_served_window() -> None:
    """The declared context must stay below 1048576 or the endpoint rejects prompts.

    BEAM truncates the chat content to ``max_context - max_tokens - RESERVE``
    using the bundled tokenizer, so the declared value moves the real prompt
    1:1. Raising it to the official 1048576 produced prompt_tokens of 1048452
    and deterministic HTTP 400s for the two longest chats, while 1048064 keeps
    the longest prompt at 1048045 and completes all 700 questions.
    """
    profile = ModelProfile.load("kimi-k3")
    beam = next(suite for suite in profile.suites if suite.suite_id == "beam-1m")
    declared = int(beam.config["command"][beam.config["command"].index("--max-context-tokens") + 1])
    assert declared == 1048064
    assert declared < 1048576


def test_beam_dataset_and_generation_contract_are_pinned() -> None:
    root = ROOT / "vendor/kvv-k3/beam/data"
    questions = root / "beam_1m_questions.jsonl"
    chats = root / "beam_1m_chats.jsonl.gz"
    assert hashlib.sha256(questions.read_bytes()).hexdigest() == (
        "0882ca1ea1dfffa4645463b94c44156277e4da082fb9a1a68ceeca852b4fc2f4"
    )
    digest = hashlib.sha256()
    with gzip.open(chats, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    assert digest.hexdigest() == (
        "d8afe1e676ec2b895ed368242e9ee227992ad1a4f8631ed1e5c60c8e46b7c0f0"
    )

    profile = ModelProfile.load("kimi-k3")
    beam = next(suite for suite in profile.suites if suite.suite_id == "beam-1m")
    command = beam.config["command"]
    assert command[command.index("--min-score") + 1] == "0.29"
    assert command[command.index("--concurrency") + 1] == "50"
    # The served K3 window is smaller than the declared 1048576; 1048064 keeps
    # the 512-token headroom the endpoint needs for the longest BEAM chats.
    assert command[command.index("--max-context-tokens") + 1] == "1048064"
    assert command[command.index("--generation-max-retries") + 1] == "3"
    assert command[command.index("--generation-task-retries") + 1] == "3"
    assert command[command.index("--generation-timeout") + 1] == "3600"
    assert command[command.index("--judge-concurrency") + 1] == "50"
    assert command[command.index("--judge-max-retries") + 1] == "8"
    assert command[command.index("--judge-timeout") + 1] == "600"
    assert beam.completeness["expected_trials"] == 700
    assert beam.config["runner_fingerprint"]["generation_concurrency"] == "50"
    assert beam.config["runner_fingerprint"]["generation_retry_on_error"] == "3"
    assert beam.config["runner_fingerprint"]["generation_task_retry_max"] == "3"
    assert beam.config["runner_fingerprint"]["generation_max_context_tokens"] == (
        "1048064"
    )
    assert beam.config["runner_fingerprint"]["generation_truncation_tokenizer"] == (
        "kvv-k3/beam/tokenizer"
    )
    assert beam.config["runner_fingerprint"]["judge_model"] == (
        "moonshotai/kimi-k2.7-code"
    )
    assert beam.config["runner_fingerprint"]["judge_requests_per_minute"] == "500"
    assert beam.config["runner_fingerprint"]["terminal_empty_response"] == (
        "score_zero_except_abstention"
    )


def test_toolcall_thinking_commands_and_fixture_are_pinned() -> None:
    fixture = ROOT / "suites/novita-api/fixtures/toolcall-thinking-samples.v1.json.gz"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == (
        "263567c9cf1affd8d37b5987f8b2a4a5e6ac42b95a262ee3b1c537c666b8af98"
    )
    profile = ModelProfile.load("kimi-k3")
    suites = {suite.suite_id: suite for suite in profile.suites}
    cases = [
        ("toolcall-thinking-low", "low", [0.82, 0.78]),
        ("toolcall-thinking-high", "high", [0.84, 0.82]),
        ("toolcall-thinking-max", "max", [0.87, 0.83]),
    ]
    known_errors = {
        "05563b28-45fe-11f1-99f6-f220bead0468",
        "275906fd-447e-11f1-bed6-0a5b122d427a",
        "c87be7c6-486c-11f1-aac7-4e1eae6eddbd",
    }
    for suite_id, effort, official_scores in cases:
        suite = suites[suite_id]
        command = suite.config["command"]
        assert command[command.index("--thinking-effort") + 1] == effort
        assert command[command.index("--max-tokens") + 1] == "32768"
        assert command[command.index("--max-connections") + 1] == "113"
        assert command[command.index("--temperature") + 1] == "1.0"
        assert command[command.index("--top-p") + 1] == "0.95"
        assert suite.config["official_scores"] == official_scores
        assert suite.config["tolerance_mode"] == "lower_bound"
        assert suite.completeness["expected_counts"] == {
            "total": 113,
            "unscored": 0,
        }
        assert set(suite.completeness["allowed_error_sample_ids"]) == known_errors


def test_report_verdict_distinguishes_failure_inconclusive_and_service_invalid() -> None:
    profile = ModelProfile.load("kimi-k3")

    def verdict(status: str, validity: str) -> str:
        return build_report(
            profile,
            [],
            {
                "run_id": "verdict-run",
                "suite_states": [
                    {
                        "mandatory": True,
                        "selected": True,
                        "status": status,
                        "validity": validity,
                    }
                ],
            },
        )["verdict"]

    assert verdict("failed", "valid") == "failed"
    assert verdict("failed", "execution_invalid") == "inconclusive"
    assert verdict("blocked", "service_invalid") == "service_invalid"
    assert verdict("paused", "valid") == "inconclusive"


def test_k3_report_preserves_table_order_and_image_completion() -> None:
    profile = ModelProfile.load("kimi-k3")
    results = [
        SuiteResult(
            suite_id=suite.suite_id,
            row_ids=list(suite.row_ids),
            status="passed",
            counts={"total": 25, "passed": 25, "failed": 0}
            if suite.suite_id == "image-test"
            else {},
            metrics={"max_images": 1000}
            if suite.suite_id == "image-test"
            else {"score": 0.8363636363636363}
            if suite.suite_id == "toolcall-thinking-max"
            else {},
        )
        for suite in profile.suites
    ]
    manifest = {
        "run_id": "test-run",
        "suite_states": [
            {"mandatory": True, "selected": True, "status": "passed", "validity": "valid"}
            for _ in profile.suites
        ],
    }

    report = build_report(profile, results, manifest)
    assert report["verdict"] == "passed"
    assert report["acceptance_mode"] is None
    assert report["official_row_count"] == 20
    assert report["supplementary_row_count"] == 1
    assert [row["row_id"] for row in report["supplementary"]] == ["K3-CB"]
    assert [row["row_id"] for row in report["official_rows"]] == EXPECTED_ROWS
    assert report["official_rows"][3]["actual"] == {
        "counts": {"total": 25, "passed": 25, "failed": 0},
        "metrics": {"max_images": 1000},
    }
    assert report["official_rows"][3]["test_file"] == (
        "vendor-img-testcases-inhouse-3.jsonl"
    )
    assert report["official_rows"][10]["difference"] == 0.00636364


def test_delivery_renderer_is_profile_driven_for_k3_and_flash() -> None:
    from novita_self_test.reporting import render_delivery_markdown

    for model_id in ("kimi-k3", "kimi-k3-flash"):
        profile = ModelProfile.load(model_id, allow_blocked=True)
        report = build_report(
            profile,
            [],
            {
                "run_id": f"{model_id}-review",
                "model": model_id,
                "served_model": model_id,
                "acceptance_mode": "full",
                "suite_states": [],
            },
        )
        markdown = render_delivery_markdown(
            report,
            profile,
            metadata={
                "title": f"{model_id} evidence",
                "scope_notes": ["optional suites are non-gating"],
            },
        )
        assert f"# {model_id} evidence" in markdown
        assert "Acceptance Matrix" in markdown
        assert "optional suites are non-gating" in markdown
        assert all(row["row_id"] in markdown for row in report["official_rows"])



    """A submission must say which deployment the numbers describe."""
    from novita_self_test.reporting import render_markdown

    profile = ModelProfile.load("kimi-k3")
    results = [
        SuiteResult(
            suite_id=suite.suite_id,
            row_ids=list(suite.row_ids),
            status="passed",
            counts={"total": 1, "passed": 1},
            metrics={"score": 1.0} if suite.suite_id == "toolcall-thinking-max" else {},
            coverage={},
        )
        for suite in profile.suites
    ]
    manifest = {
        "run_id": "test-run",
        "served_model": "moonshotai/kimi-k3-h",
        "started_at": "2026-09-11T16:06:08+00:00",
        "finished_at": "2026-09-11T22:47:02+00:00",
        "suite_states": [
            {"mandatory": True, "selected": True, "status": "passed", "validity": "valid"}
            for _ in profile.suites
        ],
    }

    report = build_report(profile, results, manifest)
    assert report["served_model"] == "moonshotai/kimi-k3-h"
    assert report["started_at"] == "2026-09-11T16:06:08+00:00"

    markdown = render_markdown(report, profile)
    assert "moonshotai/kimi-k3-h" in markdown
    assert "2026-09-11T16:06:08+00:00" in markdown
