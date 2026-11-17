from __future__ import annotations

import json
import hashlib
import os
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import DEFAULT, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from artifact.observed_contract import write_observed_contract
from artifact.validate import validate
from archive_trial import classify_outcome
from config import (
    EXTENDED_TASKS,
    OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS,
    OFFICIAL_KIMI_LOOP_CONTROL,
    OFFICIAL_CONCURRENCY_SCOPE,
    OFFICIAL_PER_TASK_CONCURRENCY,
    OFFICIAL_SCHEDULE,
    SMOKE_TASKS,
    load_contract,
)
from e2e import analyze_run, render_markdown
from metrics import aggregate
from itl import analyze_trial
from recording_proxy import RecordingHandler, ThreadingHTTPServer as RecordingHTTPServer
from remote_preflight import run_preflight
import runtime_cache
from runner import build_round_major_queue, fetch_remote, prepare_run, recovery_queue, stop_remote
from status import initialize_status, refresh_remote_status, write_status
from triage.triage_all import run as triage_all


class RunnerContractTests(unittest.TestCase):
    @staticmethod
    def _minimal_contract(root: Path) -> dict:
        return {
            "execution_profile": {
                "kind": "official_observed",
                "schedule": OFFICIAL_SCHEDULE,
                "concurrency_scope": OFFICIAL_CONCURRENCY_SCOPE,
                "recovery": {"enabled": True, "attempts_per_abnormal_task": 1},
                "harbor_n_attempts": 1,
                "per_task_concurrency": OFFICIAL_PER_TASK_CONCURRENCY,
                "harbor_timeout_multipliers": OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS.copy(),
                "kimi_loop_control": OFFICIAL_KIMI_LOOP_CONTROL.copy(),
            },
            "runner": {"root": "runner/v2"},
            "run": {
                "tag": "demo", "evidence_dir": str(root), "selected_task_count": 15,
                "required_trial_count": 45, "attempts_per_task": 3, "rounds": 3,
                "concurrency": 15, "detached": True,
            },
            "task_selection": {"kind": "builtin", "value": "smoke", "expected_count": 15},
            "service": {
                "base_url": "https://service.example/v1", "model": "demo-model",
                "headers": {"X-Infer-ID": "demo"}, "preserve_thinking": True,
            },
            "authentication": {"kind": "env", "env_name": "TEST_API_KEY"},
            "pins": {
                "kimi_cli_ref": "1e45df06da698151d2dc29a700722c37432e86ce",
                "harbor_version": "0.5.0", "harbor_python": "3.12",
                "terminal_bench_2_ref": "53ff2b87d621bdb97b455671f2bd9728b7d86c11",
                "uv_version": "0.11.24",
            },
            "remote": {
                "host": "bench@example", "port": 2222, "workdir": "/remote/demo",
                "container_proxy": "http://172.17.0.1:1081",
                "runtime_cache_root": "/remote/.kbench-cache",
            },
        }

    @staticmethod
    def _example_contract() -> dict:
        return json.loads((ROOT.parents[1] / "examples/run-contract.example.json").read_text())

    @staticmethod
    def _validate_contract(path: Path) -> subprocess.CompletedProcess[str]:
        validator = ROOT.parents[1] / "scripts/validate-run-contract.sh"
        return subprocess.run([str(validator), str(path)], capture_output=True, text=True)

    def test_builtin_task_sets_and_mixed_queue(self):
        self.assertEqual(len(SMOKE_TASKS), 15)
        self.assertEqual(len(EXTENDED_TASKS), 89)
        queue = build_round_major_queue(SMOKE_TASKS)
        self.assertEqual(len(queue), 45)
        self.assertEqual(queue[0].round, 1)
        self.assertEqual(queue[14].round, 1)
        self.assertEqual(queue[15].round, 2)
        self.assertEqual(queue[-1].round, 3)
        self.assertEqual([item.task for item in queue[:15]], SMOKE_TASKS)

    def test_recovery_ignores_clean_reward_zero(self):
        queue = recovery_queue([
            {"task": "zero", "execution_valid": True, "reward": 0},
            {"task": "error", "execution_valid": False, "reward": 0},
            {"task": "error", "execution_valid": False, "reward": 0},
        ])
        self.assertEqual([item.task for item in queue], ["error"])

    def test_post_verifier_agent_exit_with_numeric_reward_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            leaf = Path(directory)
            (leaf / "exception.txt").write_text("agent cleanup warning\n")
            result = {
                "exception_info": {"exception_type": "NonZeroAgentExitCodeError"},
                "agent_execution": {"finished_at": "2026-09-09T01:00:01Z"},
                "verifier": {
                    "started_at": "2026-09-09T01:00:02Z",
                    "finished_at": "2026-09-09T01:00:03Z",
                },
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
            outcome = classify_outcome(result, leaf)
            self.assertEqual(outcome["reward"], 1.0)
            self.assertTrue(outcome["execution_valid"])
            self.assertEqual(outcome["termination_reason"], "agent_process_error_after_verifier")

    def test_agent_exit_before_verifier_remains_execution_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            leaf = Path(directory)
            (leaf / "exception.txt").write_text("agent failed\n")
            result = {
                "exception_info": {"exception_type": "NonZeroAgentExitCodeError"},
                "agent_execution": {"finished_at": "2026-09-09T01:00:03Z"},
                "verifier": {
                    "started_at": "2026-09-09T01:00:02Z",
                    "finished_at": "2026-09-09T01:00:03Z",
                },
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
            self.assertFalse(classify_outcome(result, leaf)["execution_valid"])

    def test_post_verifier_agent_exit_with_reward_zero_remains_execution_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            leaf = Path(directory)
            (leaf / "exception.txt").write_text("agent failed\n")
            result = {
                "exception_info": {"exception_type": "NonZeroAgentExitCodeError"},
                "agent_execution": {"finished_at": "2026-09-09T01:00:01Z"},
                "verifier": {
                    "started_at": "2026-09-09T01:00:02Z",
                    "finished_at": "2026-09-09T01:00:03Z",
                },
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
            self.assertFalse(classify_outcome(result, leaf)["execution_valid"])

    def test_prepare_run_resolves_builtin_tasks_and_headers_into_self_contained_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            contract_path = Path(directory) / "source.json"
            contract_path.write_text(json.dumps(self._minimal_contract(root)))
            _, prepared = prepare_run(contract_path, root)
            snapshot = json.loads((prepared / "config.snapshot.json").read_text())
            rendered = (prepared / "setup_and_smoke.sh.rendered").read_text()
            self.assertEqual(snapshot["tasks"], SMOKE_TASKS)
            self.assertEqual(snapshot["headers"], {"X-Infer-ID": "demo"})
            self.assertEqual(snapshot["harbor_n_attempts"], 1)
            self.assertEqual(snapshot["per_task_concurrency"], 1)
            self.assertEqual(
                snapshot["harbor_timeout_multipliers"],
                OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS,
            )
            self.assertEqual(snapshot["kimi_loop_control"], OFFICIAL_KIMI_LOOP_CONTROL)
            self.assertEqual((prepared / "run-contract.json").read_bytes(), contract_path.read_bytes())
            self.assertTrue((prepared / "release-manifest.json").is_file())
            self.assertTrue((prepared / "runtime_cache.py").is_file())
            self.assertIn('json.load(open("config.snapshot.json"))["tasks"]', rendered)
            self.assertIn('KIMI_VENDOR_EXTRA_HEADERS_JSON="$HEADERS_JSON"', rendered)
            self.assertIn('CONTAINER_PROXY=$(python3', rendered)
            self.assertIn('python3 "$WORKDIR/runtime_cache.py"', rendered)
            self.assertIn('"$HARBOR_BIN" run', rendered)
            self.assertIn('--n-attempts "$HARBOR_N_ATTEMPTS"', rendered)
            self.assertIn('--timeout-multiplier "$TIMEOUT_MULTIPLIER"', rendered)
            self.assertIn('--agent-timeout-multiplier "$AGENT_TIMEOUT_MULTIPLIER"', rendered)
            self.assertIn('--verifier-timeout-multiplier "$VERIFIER_TIMEOUT_MULTIPLIER"', rendered)
            self.assertIn(
                '--agent-setup-timeout-multiplier "$AGENT_SETUP_TIMEOUT_MULTIPLIER"',
                rendered,
            )
            self.assertIn(
                '--environment-build-timeout-multiplier "$ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER"',
                rendered,
            )
            self.assertIn('worktree add --detach "$BENCH_ROOT" "$TERMINAL_REF"', rendered)
            self.assertNotIn("uv tool install --force", rendered)
            agent = (prepared / "local_kimi_cli_vendor_agent.py.rendered").read_text()
            compile(agent, "local_kimi_cli_vendor_agent.py", "exec")
            self.assertIn('cfg["loop_control"]', agent)
            self.assertIn('"max_retries_per_step": int(os.environ["KIMI_MAX_RETRIES_PER_STEP"])', agent)
            self.assertIn('"max_steps_per_run": int(os.environ["KIMI_MAX_STEPS_PER_RUN"])', agent)
            self.assertIn("/logs/agent/.kimi/config.json.bak", agent)
            self.assertIn("/__kbench_drain?timeout=120", agent)
            self.assertIn("drain_rc=0", agent)

    def test_example_contract_validates_and_dry_renders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "novita-kimi-code-bench-extended-c8-example"
            contract = self._example_contract()
            contract["run"]["evidence_dir"] = str(root)
            contract["run"]["status_path"] = str(root / "status.json")
            path = Path(directory) / "run-contract.json"
            path.write_text(json.dumps(contract))
            validated = self._validate_contract(path)
            self.assertEqual(validated.returncode, 0, validated.stderr)
            _, prepared = prepare_run(path, root)
            self.assertTrue((prepared / "run-contract.json").is_file())
            self.assertTrue((prepared / "config.snapshot.json").is_file())
            self.assertTrue((prepared / "setup_and_smoke.sh.rendered").is_file())

    def test_contract_validator_rejects_missing_pins_proxy_sensitive_headers_and_extra_recovery(self):
        mutations = {
            "missing-terminal-ref": lambda contract: contract["pins"].pop("terminal_bench_2_ref"),
            "missing-container-proxy": lambda contract: contract["remote"].pop("container_proxy"),
            "missing-runtime-cache": lambda contract: contract["remote"].pop("runtime_cache_root"),
            "missing-harbor-python": lambda contract: contract["pins"].pop("harbor_python"),
            "missing-uv-version": lambda contract: contract["pins"].pop("uv_version"),
            "sensitive-header": lambda contract: contract["service"]["headers"].update({"Authorization": "secret"}),
            "extra-recovery": lambda contract: contract["execution_profile"]["recovery"].update(
                {"attempts_per_abnormal_task": 2}
            ),
            "harbor-internal-retry": lambda contract: contract["execution_profile"].update(
                {"harbor_n_attempts": 2}
            ),
            "wrong-agent-timeout": lambda contract: contract["execution_profile"][
                "harbor_timeout_multipliers"
            ].update({"agent_timeout_multiplier": 4.0}),
            "wrong-step-retries": lambda contract: contract["execution_profile"][
                "kimi_loop_control"
            ].update({"max_retries_per_step": 3}),
            "missing-timeout-contract": lambda contract: contract["execution_profile"].pop(
                "harbor_timeout_multipliers"
            ),
            "missing-per-task-concurrency": lambda contract: contract["execution_profile"].pop(
                "per_task_concurrency"
            ),
            "wrong-per-task-concurrency": lambda contract: contract["execution_profile"].update(
                {"per_task_concurrency": 2}
            ),
            "wrong-concurrency-scope": lambda contract: contract["execution_profile"].update(
                {"concurrency_scope": "unbounded"}
            ),
            "wrong-global-concurrency": lambda contract: contract["run"].update(
                {"concurrency": 75}
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    contract = self._example_contract()
                    mutate(contract)
                    path = root / f"{name}.json"
                    path.write_text(json.dumps(contract))
                    validated = self._validate_contract(path)
                    self.assertNotEqual(validated.returncode, 0)
                    self.assertIn("does not satisfy", validated.stderr)

    def test_load_contract_rejects_non_official_timeout_and_retry_settings(self):
        mutations = (
            lambda contract: contract["execution_profile"]["harbor_timeout_multipliers"].update(
                {"verifier_timeout_multiplier": 1.0}
            ),
            lambda contract: contract["execution_profile"]["kimi_loop_control"].update(
                {"max_retries_per_step": 3}
            ),
            lambda contract: contract["execution_profile"].update({"harbor_n_attempts": 2}),
            lambda contract: contract["execution_profile"].pop("per_task_concurrency"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, mutate in enumerate(mutations):
                contract = self._minimal_contract(root)
                mutate(contract)
                path = root / f"invalid-{index}.json"
                path.write_text(json.dumps(contract))
                with self.assertRaises(ValueError):
                    load_contract(path)

    def test_post_fetch_gate_requires_runtime_cache_evidence_to_match_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "novita-kimi-code-bench-extended-c8-example"
            contract = self._example_contract()
            contract["run"]["evidence_dir"] = str(root)
            contract["run"]["status_path"] = str(root / "status.json")
            contract["authentication"]["env_name"] = "TEST_API_KEY"
            contract["remote"]["runtime_cache_root"] = "/remote/.kbench-cache"
            path = Path(directory) / "run-contract.json"
            path.write_text(json.dumps(contract))
            prepare_run(path, root)
            runtime = {
                "cache_schema_version": 1,
                "cache_root": "/remote/.kbench-cache",
                "cache_hits": {
                    "kimi_cli_source": True, "terminal_bench_2_source": True,
                    "kimi_cli_wheel": True, "harbor": True,
                },
                "uv_version_output": "uv 0.11.24 (x86_64-unknown-linux-gnu)",
                "harbor_version_output": "0.5.0",
                "harbor_python_output": "Python 3.12.12",
                "kimi_cli_ref": contract["pins"]["kimi_cli_ref"],
                "terminal_bench_2_ref": contract["pins"]["terminal_bench_2_ref"],
                "kimi_cli_wheel_sha256": "a" * 64,
                "paths": {
                    "harbor": "/remote/.kbench-cache/tools/harbor/bin/harbor",
                    "kimi_cli_source": "/remote/.kbench-cache/sources/kimi-cli",
                    "terminal_bench_2_source": "/remote/.kbench-cache/sources/terminal-bench-2",
                    "kimi_cli_wheel": "/remote/.kbench-cache/wheels/kimi-cli.whl",
                },
            }
            runtime_path = root / "runtime-versions.json"
            runtime_path.write_text(json.dumps(runtime))
            validator = ROOT.parents[1] / "scripts/validate-run-artifacts.sh"
            environment = {**os.environ, "TEST_API_KEY": "test-key-not-stored"}
            valid = subprocess.run(
                [str(validator), str(path), str(root)],
                env=environment, capture_output=True, text=True,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            runtime["uv_version_output"] = "uv 9.9.9 (wrong)"
            runtime_path.write_text(json.dumps(runtime))
            invalid = subprocess.run(
                [str(validator), str(path), str(root)],
                env=environment, capture_output=True, text=True,
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("does not match", invalid.stderr)

    def test_runtime_source_cache_can_seed_clean_copy_from_matching_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = root / "seed"
            seed.mkdir()
            subprocess.run(["git", "init", "-q", str(seed)], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
            (seed / "tracked.txt").write_text("tracked\n")
            subprocess.run(["git", "-C", str(seed), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
            revision = subprocess.run(
                ["git", "-C", str(seed), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            (seed / "untracked.txt").write_text("must not be copied\n")
            cache = root / ".kbench-cache"
            environment = os.environ.copy()
            source, first_hit = runtime_cache._ensure_source(
                cache, "demo", "https://example.invalid/demo", revision, environment, seed
            )
            self.assertFalse(first_hit)
            self.assertFalse((source / "untracked.txt").exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(source), "status", "--porcelain=v1", "--untracked-files=all"],
                    check=True, capture_output=True, text=True,
                ).stdout,
                "",
            )
            _, second_hit = runtime_cache._ensure_source(
                cache, "demo", "https://example.invalid/demo", revision, environment, None
            )
            self.assertTrue(second_hit)

    def test_runtime_cache_quarantines_invalid_entry_before_atomic_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / ".kbench-cache"
            target = cache / "objects/demo"
            target.mkdir(parents=True)
            (target / "state").write_text("bad")

            def valid(path: Path) -> bool:
                return (path / "state").is_file() and (path / "state").read_text() == "good"

            hit = runtime_cache._ensure_cached_directory(
                cache, target, "demo", valid, lambda stage: (stage / "state").write_text("good")
            )
            self.assertFalse(hit)
            self.assertEqual((target / "state").read_text(), "good")
            quarantined = list(target.parent.glob("demo.invalid-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual((quarantined[0] / "state").read_text(), "bad")

    def test_harbor_cache_builder_uses_relocatable_venv(self):
        source = (ROOT / "src/runtime_cache.py").read_text()
        self.assertIn('"venv", "--no-project", "--relocatable"', source)

    def test_prepare_runtime_uses_fully_valid_warm_cache_without_building(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / ".kbench-cache"

            def source(name: str, repository: str) -> tuple[Path, str]:
                path = cache / "sources" / name / "pending"
                path.mkdir(parents=True)
                subprocess.run(["git", "init", "-q", str(path)], check=True)
                subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
                subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
                (path / "tracked.txt").write_text(name)
                subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
                subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", name], check=True)
                revision = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "HEAD"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                final = path.with_name(revision)
                path.rename(final)
                (final / ".git/kbench-cache-manifest.json").write_text(json.dumps({
                    "cache_schema_version": 1, "kind": "git_source",
                    "repository": repository, "revision": revision,
                }))
                return final, revision

            kimi_source, kimi_ref = source("kimi-cli", runtime_cache.KIMI_REPOSITORY)
            _, terminal_ref = source("terminal-bench-2", runtime_cache.TERMINAL_BENCH_REPOSITORY)
            wheel_root = cache / "wheels/kimi-cli" / kimi_ref
            (wheel_root / "dist").mkdir(parents=True)
            wheel = wheel_root / "dist/kimi_cli-test.whl"
            wheel.write_bytes(b"wheel")
            (wheel_root / "kbench-cache-manifest.json").write_text(json.dumps({
                "cache_schema_version": 1, "kind": "kimi_cli_wheel", "revision": kimi_ref,
                "wheel": "dist/kimi_cli-test.whl", "sha256": hashlib.sha256(b"wheel").hexdigest(),
            }))
            harbor = cache / "tools/harbor/0.5.0-python3.12"
            (harbor / "bin").mkdir(parents=True)
            (harbor / "bin/harbor").write_text("#!/bin/sh\necho 'harbor 0.5.0'\n")
            (harbor / "bin/python").write_text("#!/bin/sh\necho 'Python 3.12.12'\n")
            (harbor / "bin/harbor").chmod(0o755)
            (harbor / "bin/python").chmod(0o755)
            (harbor / "kbench-cache-manifest.json").write_text(json.dumps({
                "cache_schema_version": 1, "kind": "harbor_venv",
                "harbor_version": "0.5.0", "python_spec": "3.12",
            }))
            uv = root / "uv"
            uv.write_text("#!/bin/sh\necho 'uv 0.11.24 (x86_64-unknown-linux-gnu)'\n")
            uv.chmod(0o755)
            output = root / "runtime-versions.json"
            with patch.multiple(
                runtime_cache,
                _build_source=DEFAULT,
                _build_wheel=DEFAULT,
                _build_harbor=DEFAULT,
            ) as builders:
                for builder in builders.values():
                    builder.side_effect = AssertionError("warm cache must not build")
                result = runtime_cache.prepare_runtime(
                    cache_root=cache, uv_path=uv, uv_version="0.11.24",
                    harbor_version="0.5.0", harbor_python="3.12",
                    kimi_ref=kimi_ref, terminal_ref=terminal_ref,
                    proxy="http://127.0.0.1:1081", output_path=output,
                )
            self.assertTrue(all(result["cache_hits"].values()))
            self.assertEqual(result["paths"]["kimi_cli_source"], str(kimi_source))
            self.assertEqual(json.loads(output.read_text())["kimi_cli_wheel_sha256"], hashlib.sha256(b"wheel").hexdigest())

    def test_writer_produces_valid_compatibility_leaf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text(json.dumps({"task_name": "demo"}))
            write_observed_contract(root)
            self.assertEqual(validate(root), [])
            self.assertEqual(json.loads((root / "agent/.kimi/mcp.json").read_text())["mcpServers"], {})

    def test_historical_official_leaf_does_not_require_local_schema_v2_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text(json.dumps({"task_name": "demo"}))
            write_observed_contract(root)
            (root / "agent/kimi-stream.jsonl").unlink()
            (root / "agent/execution-meta.json").unlink()
            self.assertEqual(validate(root), [])

    def test_recording_schema_v2_requires_complete_clean_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text(json.dumps({"task_name": "demo"}))
            write_observed_contract(root)
            proxy_root = root / "llmproxy/openai"
            meta_path = proxy_root / "meta.json"
            meta = json.loads(meta_path.read_text())
            meta.update({
                "recording_schema_version": 2,
                "identity_source": "local_runner",
                "target_url": "https://service.example/v1",
                "target_scheme": "https",
                "target_host": "service.example",
                "target_path": "/v1",
            })
            meta_path.write_text(json.dumps(meta))
            (root / "config.json").write_text(json.dumps(OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS))
            (root / "agent/.kimi/config.json.bak").write_text(json.dumps({
                "loop_control": OFFICIAL_KIMI_LOOP_CONTROL,
            }))
            (proxy_root / "requests").mkdir()
            (proxy_root / "responses").mkdir()
            (proxy_root / "requests/0001.json").write_text("{}")
            (proxy_root / "responses/0001.body").write_text("data: [DONE]\n\n")
            (proxy_root / "responses/0001.arrival.jsonl").write_text("{}\n")
            (proxy_root / "responses/0001.json").write_text(json.dumps({
                "status_code": 200,
                "client_disconnected": False,
                "arrival_journal": "responses/0001.arrival.jsonl",
            }))
            self.assertIn("proxy:response-incomplete:0001", validate(root))

    def test_recording_schema_v2_requires_official_timeout_and_retry_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "result.json").write_text(json.dumps({"task_name": "demo"}))
            write_observed_contract(root)
            meta_path = root / "llmproxy/openai/meta.json"
            meta = json.loads(meta_path.read_text())
            meta.update({
                "recording_schema_version": 2,
                "identity_source": "local_runner",
                "target_url": "https://service.example/v1",
                "target_scheme": "https",
                "target_host": "service.example",
                "target_path": "/v1",
            })
            meta_path.write_text(json.dumps(meta))
            wrong_timeouts = {**OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS, "agent_timeout_multiplier": 4.0}
            (root / "config.json").write_text(json.dumps(wrong_timeouts))
            (root / "agent/.kimi/config.json.bak").write_text(json.dumps({
                "loop_control": {**OFFICIAL_KIMI_LOOP_CONTROL, "max_retries_per_step": 3},
            }))
            errors = validate(root)
            self.assertIn("config:harbor-timeout-multipliers", errors)
            self.assertIn("config:kimi-loop-control", errors)

    def test_round_mean_aggregation_requires_every_baseline_slot(self):
        records = [{"task": "a", "phase": "baseline", "round": 1, "reward": 0, "execution_valid": True}]
        result = aggregate(records, ["a", "b"], baseline_trials=1)
        self.assertFalse(result["publishable"])
        self.assertIsNone(result["accuracy"])
        self.assertEqual(result["missing_baseline_tasks"], ["b"])

    def test_round_mean_excludes_recovery_from_official_score(self):
        records = []
        for round_number in range(1, 6):
            records.extend([
                {"task": "a", "phase": "baseline", "round": round_number, "reward": 1, "execution_valid": True},
                {"task": "b", "phase": "baseline", "round": round_number, "reward": 0, "execution_valid": True},
            ])
        records.append({"task": "b", "phase": "recovery", "round": 0, "reward": 1, "execution_valid": True})
        result = aggregate(records, ["a", "b"], baseline_trials=5)
        self.assertTrue(result["publishable"])
        self.assertEqual(result["aggregation"], "baseline_round_mean")
        self.assertEqual(result["accuracy"], 0.5)
        self.assertEqual(result["recovery_trials"], 1)
        self.assertEqual(result["recovery_passes"], 1)
        self.assertEqual(result["diagnostic_any_baseline_passed_tasks"], 1)

    def test_triage_discovers_nested_archived_leaf_and_keeps_attempt_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf = root / "jobs/demo/date/demo__id"
            leaf.mkdir(parents=True)
            (leaf / "result.json").write_text(json.dumps({
                "task_name": "demo",
                "verifier_result": {"rewards": {"reward": 1}},
            }))
            (leaf / "kbench-attempt.json").write_text(json.dumps({
                "task_label": "demo", "phase": "baseline", "round": 4, "attempt": 4,
            }))
            (leaf / "trial-record.json").write_text(json.dumps({
                "outcome": {"reward": 1, "execution_valid": True},
            }))
            records = triage_all(root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["phase"], "baseline")
            self.assertEqual(records[0]["round"], 4)

    def test_itl_uses_proxy_arrival_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            (root / "responses").mkdir()
            body = (
                b'data: {"sla_metrics":{"ts_us":1000000},"choices":[{"delta":{"reasoning_content":"."}}]}\n\n'
                b'data: {"sla_metrics":{"ts_us":2000000},"choices":[{"delta":{"reasoning_content":"x"}}]}\n\n'
                b'data: {"sla_metrics":{"ts_us":8000000},"choices":[{"delta":{"tool_calls":[{}]}}]}\n\n'
            )
            (root / "responses/0001.body").write_bytes(body)
            (root / "responses/0001.json").write_text(json.dumps({"status_code": 200}))
            arrivals = [
                {"timestamp": "2026-01-01T00:00:00Z", "monotonic_ns": 0, "bytes": 100, "cumulative_bytes": 100},
                {"timestamp": "2026-01-01T00:00:01Z", "monotonic_ns": 1_000_000_000, "bytes": 100, "cumulative_bytes": 200},
                {"timestamp": "2026-01-01T00:00:07Z", "monotonic_ns": 7_000_000_000, "bytes": len(body) - 200, "cumulative_bytes": len(body)},
            ]
            (root / "responses/0001.arrival.jsonl").write_text("\n".join(json.dumps(item) for item in arrivals) + "\n")
            (root / "requests/0001.json").write_text(json.dumps({"started_at": "2026-01-01T00:00:00Z"}))
            result = analyze_trial(root)
            self.assertEqual(result["long_gap_count"], 1)
            self.assertEqual(result["long_gap_tail_20pct_count"], 1)
            self.assertEqual(result["responses"][0]["service_minus_proxy_ms"], 0.0)

    def test_itl_falls_back_to_arrival_without_service_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "requests").mkdir()
            (root / "responses").mkdir()
            first = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            second = b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
            body = first + second
            (root / "responses/0001.body").write_bytes(body)
            (root / "responses/0001.json").write_text(json.dumps({"status_code": 200}))
            arrivals = [
                {"timestamp": "2026-01-01T00:00:00Z", "monotonic_ns": 0, "bytes": len(first), "cumulative_bytes": len(first)},
                {"timestamp": "2026-01-01T00:00:06Z", "monotonic_ns": 6_000_000_000, "bytes": len(second), "cumulative_bytes": len(body)},
            ]
            (root / "responses/0001.arrival.jsonl").write_text("\n".join(json.dumps(item) for item in arrivals) + "\n")
            (root / "requests/0001.json").write_text(json.dumps({"started_at": "2026-01-01T00:00:00Z"}))
            result = analyze_trial(root)
            self.assertEqual(result["long_gap_count"], 1)
            self.assertEqual(result["responses"][0]["service_timed_event_count"], 0)
            self.assertNotIn("service_clock_ms", result["responses"][0])

    def test_itl_accepts_archived_leaf_and_uses_result_e2e(self):
        with tempfile.TemporaryDirectory() as directory:
            leaf = Path(directory)
            proxy = leaf / "agent/llmproxy/openai"
            (proxy / "requests").mkdir(parents=True)
            (proxy / "responses").mkdir()
            body = b'data: {"sla_metrics":{"ts_us":1000000}}\n\n'
            (proxy / "responses/0001.body").write_bytes(body)
            (proxy / "responses/0001.json").write_text(json.dumps({"status_code": 200}))
            (proxy / "responses/0001.arrival.jsonl").write_text(json.dumps({
                "timestamp": "2026-01-01T00:00:01Z", "monotonic_ns": 1,
                "bytes": len(body), "cumulative_bytes": len(body),
            }) + "\n")
            (proxy / "requests/0001.json").write_text(json.dumps({"started_at": "2026-01-01T00:00:00Z"}))
            (leaf / "result.json").write_text(json.dumps({
                "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:02Z",
            }))
            result = analyze_trial(leaf)
            self.assertEqual(result["responses_analyzed"], 1)
            self.assertEqual(result["task_total_e2e_ms"], 2000.0)
            self.assertIsNone(result["agent_execution_e2e_ms"])

    def test_itl_distinguishes_task_total_from_agent_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            leaf = Path(directory)
            proxy = leaf / "agent/llmproxy/openai"
            (proxy / "requests").mkdir(parents=True)
            (proxy / "responses").mkdir()
            body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            (proxy / "responses/0001.body").write_bytes(body)
            (proxy / "responses/0001.json").write_text(json.dumps({"status_code": 200}))
            (proxy / "responses/0001.arrival.jsonl").write_text(json.dumps({
                "timestamp": "2026-01-01T00:00:03Z", "monotonic_ns": 3_000_000_000,
                "bytes": len(body), "cumulative_bytes": len(body),
            }) + "\n")
            (proxy / "requests/0001.json").write_text(json.dumps({"started_at": "2026-01-01T00:00:02Z"}))
            (leaf / "result.json").write_text(json.dumps({
                "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:10Z",
                "agent_execution": {
                    "started_at": "2026-01-01T00:00:02Z", "finished_at": "2026-01-01T00:00:08Z",
                },
            }))
            result = analyze_trial(leaf)
            self.assertEqual(result["task_total_e2e_ms"], 10_000.0)
            self.assertEqual(result["agent_execution_e2e_ms"], 6_000.0)

    def test_e2e_phase_report_uses_only_archived_leaves_and_groups_trials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            leaf = root / "jobs/demo/date/demo__id"
            proxy = leaf / "agent/llmproxy/openai"
            (proxy / "requests").mkdir(parents=True)
            (proxy / "responses").mkdir()
            for number in range(2):
                (proxy / "requests" / f"{number:04d}.json").write_text("{}")
            (proxy / "responses/0000.json").write_text(json.dumps({"status_code": 502}))
            (leaf / "result.json").write_text(json.dumps({
                "task_name": "demo", "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:10Z",
                "environment_setup": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z"},
                "agent_setup": {"started_at": "2026-01-01T00:00:01Z", "finished_at": "2026-01-01T00:00:02Z"},
                "agent_execution": {"started_at": "2026-01-01T00:00:02Z", "finished_at": "2026-01-01T00:00:08Z"},
                "verifier": {"started_at": "2026-01-01T00:00:08Z", "finished_at": "2026-01-01T00:00:09Z"},
            }))
            (leaf / "kbench-attempt.json").write_text(json.dumps({"task_label": "demo", "phase": "baseline", "round": 1, "attempt": 1}))
            (leaf / "trial-record.json").write_text(json.dumps({"outcome": {"reward": 0, "execution_valid": False}}))
            # Harbor also writes a job-level result; it must not be counted as a trial leaf.
            job_result = root / "jobs/demo/date/result.json"
            job_result.parent.mkdir(parents=True, exist_ok=True)
            job_result.write_text("{}")
            report = analyze_run(root, high_step_threshold=2)
            self.assertEqual(report["trial_count"], 1)
            self.assertEqual(report["groups"]["baseline_with_proxy_502"]["trial_count"], 1)
            self.assertEqual(report["groups"]["high_step_ge_threshold"]["trial_count"], 1)
            self.assertEqual(report["groups"]["all"]["timings"]["agent_execution_ms"]["total_ms"], 6000.0)
            self.assertIn("Groups are overlapping", render_markdown(report))

    def test_fetch_uses_snapshot_bound_contract_without_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "demo"
            root.mkdir()
            contract_path = root / "run-contract.json"
            contract = self._minimal_contract(root)
            contract["authentication"]["env_name"] = "DO_NOT_READ_THIS"
            contract_path.write_text(json.dumps(contract))
            (root / "config.snapshot.json").write_text(json.dumps({
                "tag": "demo", "remote_workdir": "/remote/demo",
                "runner_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            }))
            payload = parent / "payload.txt"
            payload.write_text("ok")
            with tarfile.open(root / "remote.tar.gz", "w:gz") as archive:
                archive.add(payload, arcname="payload.txt")
            with patch("runner.subprocess.run") as run:
                destination = fetch_remote(root)
            self.assertEqual(destination, root.resolve() / "remote.tar.gz")
            self.assertEqual((root / "archive/payload.txt").read_text(), "ok")
            run.assert_called_once_with([
                "rsync", "-a", "-e", "ssh -p 2222",
                "bench@example:/remote/demo/remote.tar.gz", str(root.resolve() / "remote.tar.gz"),
            ], check=True)
            self.assertNotIn("DO_NOT_READ_THIS", " ".join(run.call_args.args[0]))

    def test_fetch_rejects_snapshot_workdir_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "demo"
            root.mkdir()
            contract_path = root / "run-contract.json"
            contract_path.write_text(json.dumps(self._minimal_contract(root)))
            (root / "config.snapshot.json").write_text(json.dumps({
                "tag": "demo", "remote_workdir": "/remote/other",
                "runner_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            }))
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                fetch_remote(root)

    def test_stop_uses_only_the_snapshot_bound_remote_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            root.mkdir()
            contract_path = root / "run-contract.json"
            contract = self._minimal_contract(root)
            contract_path.write_text(json.dumps(contract))
            (root / "config.snapshot.json").write_text(json.dumps({
                "tag": "demo", "remote_workdir": "/remote/demo",
                "runner_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            }))
            stopped = {"state": "paused", "remote": {"marker": "paused", "exit_code": 143}}
            completed = subprocess.CompletedProcess([], 0, stdout='{"state":"paused"}\n', stderr="")
            with patch("runner.subprocess.run", return_value=completed) as run, patch(
                "runner.refresh_remote_status", return_value=stopped
            ) as refresh:
                result = stop_remote(root)
            self.assertEqual(result, stopped)
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["ssh", "-p", "2222", "bench@example"])
            self.assertIn("/remote/demo", command[4])
            self.assertIn("setup_and_smoke.sh", command[4])
            refresh.assert_called_once_with(root.resolve())

    def test_stop_rejects_non_detached_run_before_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            root.mkdir()
            contract_path = root / "run-contract.json"
            contract = self._minimal_contract(root)
            contract["run"]["detached"] = False
            contract_path.write_text(json.dumps(contract))
            (root / "config.snapshot.json").write_text(json.dumps({
                "remote_workdir": "/remote/demo",
                "runner_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
            }))
            with patch("runner.subprocess.run") as run, self.assertRaisesRegex(RuntimeError, "detached"):
                stop_remote(root)
            run.assert_not_called()

    def test_remote_status_refresh_persists_baseline_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            root.mkdir()
            contract = self._minimal_contract(root)
            (root / "run-contract.json").write_text(json.dumps(contract))
            initialize_status(root)
            observed = {
                "state": "running", "phase": "baseline", "marker": "running", "pid": 123,
                "exit_code": None, "started": 12, "finished": 9, "valid_pass": 4,
                "valid_zero": 3, "error_zero": 2, "unclassified": 0,
                "setup_log_bytes": 100, "last_progress_at": "2026-01-01T00:00:00Z",
                "recovery_trials": 0, "remote_archive_present": False,
            }
            completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(observed), stderr="")
            with patch("status.subprocess.run", return_value=completed):
                status = refresh_remote_status(root)
            self.assertEqual(status["phase"], "pull_and_test")
            self.assertEqual(status["progress"]["started_trials"], 12)
            self.assertEqual(status["progress"]["error_zero"], 2)
            self.assertEqual(json.loads((root / "status.json").read_text())["remote"]["pid"], 123)

    def test_remote_failed_marker_is_not_reported_as_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            root.mkdir()
            (root / "run-contract.json").write_text(json.dumps(self._minimal_contract(root)))
            initialize_status(root)
            observed = {
                "state": "failed", "phase": "baseline", "marker": "failed", "pid": 123,
                "exit_code": 1, "started": 12, "finished": 9, "valid_pass": 4,
                "valid_zero": 3, "error_zero": 2, "unclassified": 0,
                "setup_log_bytes": 100, "last_progress_at": "2026-01-01T00:00:00Z",
                "recovery_trials": 0, "remote_archive_present": False,
            }
            completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(observed), stderr="")
            with patch("status.subprocess.run", return_value=completed):
                status = refresh_remote_status(root)
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["remote"]["marker"], "failed")
            self.assertEqual(status["blockers"], ["remote-run-failed"])

    def test_remote_paused_marker_is_preserved_as_human_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            root.mkdir()
            (root / "run-contract.json").write_text(json.dumps(self._minimal_contract(root)))
            initialize_status(root)
            observed = {
                "state": "paused", "phase": "baseline", "marker": "paused", "pid": 123,
                "exit_code": 143, "started": 12, "finished": 9, "valid_pass": 4,
                "valid_zero": 3, "error_zero": 2, "unclassified": 0,
                "setup_log_bytes": 100, "last_progress_at": "2026-01-01T00:00:00Z",
                "recovery_trials": 0, "remote_archive_present": False,
            }
            completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(observed), stderr="")
            with patch("status.subprocess.run", return_value=completed):
                status = refresh_remote_status(root)
            self.assertEqual(status["state"], "paused")
            self.assertEqual(status["remote"]["marker"], "paused")
            self.assertEqual(status["blockers"], ["human-paused"])

    def test_status_gate_accepts_an_incomplete_human_paused_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "novita-kimi-code-bench-extended-c8-example"
            contract = self._example_contract()
            contract["run"]["evidence_dir"] = str(root)
            contract["run"]["status_path"] = str(root / "status.json")
            root.mkdir()
            contract_path = root / "run-contract.json"
            contract_path.write_text(json.dumps(contract))
            status = initialize_status(root)
            status.update({"state": "paused", "phase": "pull_and_test", "blockers": ["human-paused"]})
            status["remote"].update({"marker": "paused", "pid": 123, "exit_code": 143})
            status["progress"].update({
                "started_trials": 12, "finished_trials": 9, "valid_pass": 4,
                "valid_zero": 3, "error_zero": 2, "unclassified": 0,
                "not_started_trials": 255,
            })
            write_status(root, status)
            validator = ROOT.parents[1] / "scripts/validate-run-status.sh"
            validated = subprocess.run(
                [str(validator), str(contract_path), str(root / "status.json")],
                capture_output=True,
                text=True,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertIn("state=paused", validated.stdout)

    def test_rendered_run_queue_honors_concurrency_limit(self):
        template = (ROOT / "src/runner/templates/setup_and_smoke.sh.tmpl").read_text()
        function = template[template.index("run_task_pipeline() {"):template.index("\nset_phase baseline")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.tsv"
            queue.write_text("".join(f"task-{i}\t1\t1\n" for i in range(12)))
            script = root / "fixture.sh"
            script.write_text(
                "set -euo pipefail\nCONCURRENCY=3\nPER_TASK_CONCURRENCY=1\nROUNDS=1\nSTATE=$1\nSTATE_ROOT=$STATE\n"
                "printf '0\\n' > \"$STATE/count\"; printf '0\\n' > \"$STATE/max\"\n"
                "lock() { while ! mkdir \"$STATE/lock\" 2>/dev/null; do sleep .005; done; }\n"
                "unlock() { rmdir \"$STATE/lock\"; }\n"
                "run_one() { local task=\"$1\"; lock; n=$(cat \"$STATE/count\"); n=$((n+1)); printf '%s\\n' \"$n\" > \"$STATE/count\"; m=$(cat \"$STATE/max\"); [ \"$n\" -le \"$m\" ] || printf '%s\\n' \"$n\" > \"$STATE/max\"; unlock; sleep .05; lock; n=$(cat \"$STATE/count\"); printf '%s\\n' \"$((n-1))\" > \"$STATE/count\"; unlock; }\n"
                + function + "\nrun_queue baseline \"$2\"\n"
            )
            subprocess.run(["bash", str(script), str(root), str(queue)], check=True)
            self.assertEqual((root / "count").read_text().strip(), "0")
            self.assertEqual((root / "max").read_text().strip(), "3")

    def test_rendered_run_queue_serializes_rounds_for_each_task(self):
        template = (ROOT / "src/runner/templates/setup_and_smoke.sh.tmpl").read_text()
        function = template[template.index("run_task_pipeline() {"):template.index("\nset_phase baseline")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.tsv"
            queue.write_text(
                "same-task\t1\t1\nother-task\t1\t1\n"
                "same-task\t2\t2\nother-task\t2\t2\n"
                "same-task\t3\t3\nother-task\t3\t3\n"
            )
            script = root / "fixture.sh"
            script.write_text(
                "set -euo pipefail\nCONCURRENCY=6\nPER_TASK_CONCURRENCY=1\nROUNDS=3\nSTATE_ROOT=$1\n"
                "run_one() {\n"
                "  local task=$1 round=$2\n"
                "  if [ -e \"$STATE_ROOT/active-$task\" ]; then touch \"$STATE_ROOT/violation\"; fi\n"
                "  touch \"$STATE_ROOT/active-$task\"\n"
                "  printf '%s\\n' \"$round\" >> \"$STATE_ROOT/rounds-$task\"\n"
                "  sleep .03\n"
                "  rm -f \"$STATE_ROOT/active-$task\"\n"
                "}\n"
                + function
                + "\nrun_queue baseline \"$2\"\n"
            )
            subprocess.run(["bash", str(script), str(root), str(queue)], check=True)
            self.assertFalse((root / "violation").exists())
            self.assertEqual((root / "rounds-same-task").read_text(), "1\n2\n3\n")
            self.assertEqual((root / "rounds-other-task").read_text(), "1\n2\n3\n")

    def test_rendered_run_queue_pipelines_tasks_without_a_round_barrier(self):
        template = (ROOT / "src/runner/templates/setup_and_smoke.sh.tmpl").read_text()
        function = template[template.index("run_task_pipeline() {"):template.index("\nset_phase baseline")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.tsv"
            queue.write_text(
                "slow-task\t1\t1\n" "fast-task\t1\t1\n"
                "slow-task\t2\t2\n" "fast-task\t2\t2\n"
            )
            script = root / "fixture.sh"
            script.write_text(
                "set -euo pipefail\nCONCURRENCY=2\nROUNDS=2\nSTATE_ROOT=$1\n"
                "run_one() {\n"
                "  local task=$1 round=$2\n"
                "  printf 'start\\t%s\\t%s\\n' \"$task\" \"$round\" >> \"$STATE_ROOT/events\"\n"
                "  if [ \"$task\" = slow-task ] && [ \"$round\" = 1 ]; then sleep .2; else sleep .01; fi\n"
                "  printf 'finish\\t%s\\t%s\\n' \"$task\" \"$round\" >> \"$STATE_ROOT/events\"\n"
                "}\n"
                + function
                + "\nrun_queue baseline \"$2\"\n"
            )
            subprocess.run(["bash", str(script), str(root), str(queue)], check=True)
            events = [line.split() for line in (root / "events").read_text().splitlines()]
            positions = {(event[0], event[1], event[2]): index for index, event in enumerate(events)}
            # The fast task reaches round 2 before the slow task finishes round 1.
            self.assertLess(positions[("start", "fast-task", "2")], positions[("finish", "slow-task", "1")])
            # Each task's own rounds remain strictly serialized.
            for task in ("slow-task", "fast-task"):
                self.assertLess(positions[("finish", task, "1")], positions[("start", task, "2")])

    def test_recording_proxy_swallows_client_disconnect(self):
        class BrokenWriter:
            def write(self, _data):
                raise BrokenPipeError("closed")

            def flush(self):
                raise AssertionError("flush should not run after write failure")

        handler = object.__new__(RecordingHandler)
        handler.wfile = BrokenWriter()
        self.assertFalse(handler._write_client(b"data"))

    def test_recording_proxy_streams_and_records_incremental_arrivals(self):
        first = b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        second = b'data: {"choices":[{"delta":{"content":"b"}}]}\n\ndata: [DONE]\n\n'
        first_sent = threading.Event()
        release_second = threading.Event()

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                first_sent.set()
                release_second.wait(5)
                self.wfile.write(second)
                self.wfile.flush()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)

            class ProxyHandler(RecordingHandler):
                target_base_url = f"http://127.0.0.1:{upstream.server_port}"
                output_dir = root
                identity = {"route_path": "/routes/test"}
                counter = 0

            proxy = RecordingHTTPServer(("127.0.0.1", 0), ProxyHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            upstream_thread.start()
            proxy_thread.start()
            response_body: list[bytes] = []

            def request() -> None:
                response = urllib.request.urlopen(urllib.request.Request(
                    f"http://127.0.0.1:{proxy.server_port}/routes/test/v1/chat/completions",
                    data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                ), timeout=5)
                response_body.append(response.read())

            client = threading.Thread(target=request)
            client.start()
            try:
                self.assertTrue(first_sent.wait(2))
                meta_path = root / "responses/0001.json"
                for _ in range(100):
                    if meta_path.is_file() and (root / "responses/0001.arrival.jsonl").is_file():
                        break
                    time.sleep(0.01)
                self.assertFalse(json.loads(meta_path.read_text())["complete"])
                release_second.set()
                client.join(5)
                self.assertFalse(client.is_alive())
                arrivals = (root / "responses/0001.arrival.jsonl").read_text().splitlines()
                self.assertGreaterEqual(len(arrivals), 2)
                self.assertEqual((root / "responses/0001.body").read_bytes(), first + second)
                complete = json.loads(meta_path.read_text())
                self.assertTrue(complete["complete"])
                self.assertFalse(complete["client_disconnected"])
                self.assertEqual(response_body, [first + second])
            finally:
                release_second.set()
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()

    def test_recording_proxy_drain_waits_for_in_flight_response(self):
        response_payload = b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
        request_started = threading.Event()
        release_response = threading.Event()

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                request_started.set()
                release_response.wait(5)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(response_payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)

            class ProxyHandler(RecordingHandler):
                target_base_url = f"http://127.0.0.1:{upstream.server_port}"
                output_dir = root
                identity = {"route_path": "/routes/test"}
                counter = 0

            proxy = RecordingHTTPServer(("127.0.0.1", 0), ProxyHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            upstream_thread.start()
            proxy_thread.start()
            client = threading.Thread(target=lambda: urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/routes/test/v1/chat/completions",
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
            ), timeout=5).read())
            client.start()
            drain_result: list[dict] = []

            def drain() -> None:
                response = urllib.request.urlopen(
                    f"http://127.0.0.1:{proxy.server_port}/__kbench_drain?timeout=2",
                    timeout=3,
                )
                drain_result.append(json.loads(response.read()))

            drain_client = threading.Thread(target=drain)
            try:
                self.assertTrue(request_started.wait(2))
                drain_client.start()
                time.sleep(0.05)
                self.assertTrue(drain_client.is_alive())
                release_response.set()
                client.join(5)
                drain_client.join(5)
                self.assertFalse(client.is_alive())
                self.assertFalse(drain_client.is_alive())
                self.assertEqual(drain_result, [{"drained": True, "active_requests": 0}])
                self.assertEqual(
                    {path.stem for path in (root / "requests").glob("*.json")},
                    {path.stem for path in (root / "responses").glob("*.json")},
                )
                self.assertTrue(json.loads((root / "responses/0001.json").read_text())["complete"])
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(urllib.request.Request(
                        f"http://127.0.0.1:{proxy.server_port}/routes/test/v1/chat/completions",
                        data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                    ), timeout=2)
                self.assertEqual(caught.exception.code, 503)
            finally:
                release_response.set()
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()

    def test_recording_proxy_drain_timeout_reports_active_request(self):
        request_started = threading.Event()
        release_response = threading.Event()

        class UpstreamHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                request_started.set()
                release_response.wait(5)
                self.send_response(200)
                self.end_headers()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)

            class ProxyHandler(RecordingHandler):
                target_base_url = f"http://127.0.0.1:{upstream.server_port}"
                output_dir = root
                identity = {"route_path": "/routes/test"}
                counter = 0

            proxy = RecordingHTTPServer(("127.0.0.1", 0), ProxyHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            upstream_thread.start()
            proxy_thread.start()
            client = threading.Thread(target=lambda: urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{proxy.server_port}/routes/test/v1/chat/completions",
                data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
            ), timeout=5).read())
            client.start()
            try:
                self.assertTrue(request_started.wait(2))
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{proxy.server_port}/__kbench_drain?timeout=0.05",
                        timeout=2,
                    )
                self.assertEqual(caught.exception.code, 503)
                self.assertEqual(
                    json.loads(caught.exception.read()),
                    {"drained": False, "active_requests": 1},
                )
            finally:
                release_response.set()
                client.join(5)
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()

    def test_recording_proxy_preserves_upstream_http_error(self):
        error_body = b'{"error":"rate limited"}'

        class ErrorHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_body)))
                self.end_headers()
                self.wfile.write(error_body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), ErrorHandler)

            class ProxyHandler(RecordingHandler):
                target_base_url = f"http://127.0.0.1:{upstream.server_port}"
                output_dir = root
                identity = {"route_path": "/routes/test"}
                counter = 0

            proxy = RecordingHTTPServer(("127.0.0.1", 0), ProxyHandler)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            upstream_thread.start()
            proxy_thread.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(urllib.request.Request(
                        f"http://127.0.0.1:{proxy.server_port}/routes/test/v1/chat/completions",
                        data=b"{}", headers={"Content-Type": "application/json"}, method="POST",
                    ), timeout=5)
                self.assertEqual(caught.exception.code, 429)
                self.assertEqual(caught.exception.read(), error_body)
                meta = json.loads((root / "responses/0001.json").read_text())
                self.assertEqual(meta["status_code"], 429)
                self.assertTrue(meta["complete"])
                self.assertEqual((root / "responses/0001.body").read_bytes(), error_body)
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()

    def test_remote_preflight_forwards_contract_headers_and_records_stream(self):
        seen_headers: dict[str, str] = {}

        class ToolCallHandler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                seen_headers["authorization"] = self.headers.get("Authorization", "")
                seen_headers["x-infer-id"] = self.headers.get("X-Infer-ID", "")
                body = (
                    b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"get_weather","arguments":"{\\"city\\":\\"Beijing\\"}"}}]}}]}\n\n'
                    b'data: [DONE]\n\n'
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), ToolCallHandler)
            thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            thread.start()
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps({
                "tag": "demo", "base_url": f"http://127.0.0.1:{upstream.server_port}",
                "model": "demo", "headers": {"X-Infer-ID": "route-demo"},
            }))
            auth = root / "auth"
            auth.write_text("secret-value")
            try:
                with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}):
                    result = run_preflight(snapshot, auth, root / "preflight", attempts=1)
                self.assertTrue(result["passed"])
                self.assertEqual(seen_headers["authorization"], "Bearer secret-value")
                self.assertEqual(seen_headers["x-infer-id"], "route-demo")
                request = json.loads((root / "preflight/llmproxy/openai/requests/0001.json").read_text())
                self.assertEqual(request["headers"]["Authorization"], "<redacted>")
                meta = json.loads((root / "preflight/llmproxy/openai/meta.json").read_text())
                self.assertEqual(meta["identity_source"], "local_runner")
                self.assertEqual(meta["recording_schema_version"], 2)
            finally:
                upstream.shutdown()
                upstream.server_close()


class ItlTimestampTests(unittest.TestCase):
    def test_time_accepts_nanosecond_fractions(self):
        from itl import _time

        self.assertEqual(
            _time("2026-09-16T07:48:37.521662158+00:00").isoformat(),
            "2026-09-16T07:48:37.521662+00:00",
        )
        self.assertEqual(
            _time("2026-09-16T07:48:37.521662158Z").isoformat(),
            "2026-09-16T07:48:37.521662+00:00",
        )
        self.assertEqual(
            _time("2026-09-16T07:48:37Z").isoformat(),
            "2026-09-16T07:48:37+00:00",
        )


if __name__ == "__main__":
    unittest.main()
