"""Small, immutable configuration helpers for the official-observed runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SMOKE_TASKS = [
    "fix-git", "regex-log", "cancel-async-tasks", "sqlite-db-truncate",
    "build-cython-ext", "git-leak-recovery", "sanitize-git-repo",
    "fix-code-vulnerability", "configure-git-webserver", "query-optimize",
    "polyglot-c-py", "polyglot-rust-c", "nginx-request-logging",
    "headless-terminal", "pypi-server",
]
EXTENDED_TASKS = [
    "adaptive-rejection-sampler", "bn-fit-modify", "break-filter-js-from-html", "build-cython-ext", "build-pmars", "build-pov-ray", "caffe-cifar-10", "cancel-async-tasks", "chess-best-move", "circuit-fibsqrt", "cobol-modernization", "code-from-image", "compile-compcert", "configure-git-webserver", "constraints-scheduling", "count-dataset-tokens", "crack-7z-hash", "custom-memory-heap-crash", "db-wal-recovery", "distribution-search", "dna-assembly", "dna-insert", "extract-elf", "extract-moves-from-video", "feal-differential-cryptanalysis", "feal-linear-cryptanalysis", "filter-js-from-html", "financial-document-processor", "fix-code-vulnerability", "fix-git", "fix-ocaml-gc", "gcode-to-text", "git-leak-recovery", "git-multibranch", "gpt2-codegolf", "headless-terminal", "hf-model-inference", "install-windows-3.11", "kv-store-grpc", "large-scale-text-editing", "largest-eigenval", "llm-inference-batching-scheduler", "log-summary-date-ranges", "mailman", "make-doom-for-mips", "make-mips-interpreter", "mcmc-sampling-stan", "merge-diff-arc-agi-task", "model-extraction-relu-logits", "modernize-scientific-stack", "mteb-leaderboard", "mteb-retrieve", "multi-source-data-merger", "nginx-request-logging", "openssl-selfsigned-cert", "overfull-hbox", "password-recovery", "path-tracing", "path-tracing-reverse", "polyglot-c-py", "polyglot-rust-c", "portfolio-optimization", "protein-assembly", "prove-plus-comm", "pypi-server", "pytorch-model-cli", "pytorch-model-recovery", "qemu-alpine-ssh", "qemu-startup", "query-optimize", "raman-fitting", "regex-chess", "regex-log", "reshard-c4-data", "rstan-to-pystan", "sam-cell-seg", "sanitize-git-repo", "schemelike-metacircular-eval", "sparql-university", "sqlite-db-truncate", "sqlite-with-gcov", "torch-pipeline-parallelism", "torch-tensor-parallelism", "train-fasttext", "tune-mjcf", "video-processing", "vulnerable-secret", "winning-avg-corewars", "write-compressor",
]

OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS = {
    "timeout_multiplier": 1.0,
    "agent_timeout_multiplier": 10.0,
    "verifier_timeout_multiplier": 10.0,
    "agent_setup_timeout_multiplier": 1.0,
    "environment_build_timeout_multiplier": 10.0,
}
OFFICIAL_KIMI_LOOP_CONTROL = {
    "max_retries_per_step": 10,
    "max_steps_per_run": 500,
}
OFFICIAL_RECOVERY = {
    "enabled": True,
    "attempts_per_abnormal_task": 1,
}
OFFICIAL_HARBOR_N_ATTEMPTS = 1
OFFICIAL_PER_TASK_CONCURRENCY = 1
OFFICIAL_SCHEDULE = "task_pipelines"
OFFICIAL_CONCURRENCY_SCOPE = "task_count"


def load_contract(path: str | Path) -> dict[str, Any]:
    contract = json.loads(Path(path).read_text())
    profile = contract.get("execution_profile", {})
    if profile.get("kind") != "official_observed":
        raise ValueError("Novita Kimi Code Bench requires official_observed")
    if profile.get("harbor_timeout_multipliers") != OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS:
        raise ValueError("official_observed requires the official Harbor timeout multipliers")
    if profile.get("kimi_loop_control") != OFFICIAL_KIMI_LOOP_CONTROL:
        raise ValueError("official_observed requires the official Kimi loop control")
    if profile.get("harbor_n_attempts") != OFFICIAL_HARBOR_N_ATTEMPTS:
        raise ValueError("official_observed requires one Harbor attempt per trial")
    if profile.get("schedule") != OFFICIAL_SCHEDULE:
        raise ValueError("official_observed requires serialized per-task pipelines")
    if profile.get("concurrency_scope") != OFFICIAL_CONCURRENCY_SCOPE:
        raise ValueError("official_observed requires task-count concurrency")
    if profile.get("per_task_concurrency") != OFFICIAL_PER_TASK_CONCURRENCY:
        raise ValueError("official_observed requires per-task concurrency of one")
    if profile.get("recovery") != OFFICIAL_RECOVERY:
        raise ValueError("official_observed requires three recovery attempts per abnormal task")
    if contract.get("runner", {}).get("root") != "runner/v2":
        raise ValueError("Novita Kimi Code Bench requires runner/v2")
    return contract


def task_plan(contract: dict[str, Any]) -> tuple[list[str], int]:
    selection = contract["task_selection"]
    count = int(selection["expected_count"])
    if selection["value"] == "extended" and count != 89:
        raise ValueError("extended selection must contain 89 tasks")
    if selection["value"] == "smoke" and count != 15:
        raise ValueError("smoke selection must contain 15 tasks")
    tasks = selection.get("tasks")
    if tasks is None:
        tasks = EXTENDED_TASKS if selection["value"] == "extended" else SMOKE_TASKS
    if not isinstance(tasks, list) or len(tasks) != count:
        raise ValueError("selected task list does not match its declared count")
    return [str(t) for t in tasks], count
