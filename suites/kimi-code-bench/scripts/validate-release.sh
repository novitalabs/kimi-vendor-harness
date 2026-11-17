#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
runner_root=$repo_root/runner/v2
manifest=$runner_root/release-manifest.json
example=$repo_root/examples/run-contract.example.json

for tool in jq python3 bash; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required" >&2
    exit 2
  fi
done

jq -e '
  .release == "novita-kimi-code-bench-v1.1.0"
  and .runner_version == "1.1.0"
  and .stability == "stable"
  and .release_date == "2026-09-10"
  and .run_contract_schema == 2
  and .recording_schema_version == 2
  and .runtime_cache_schema_version == 1
  and .execution_profile == "official_observed"
  and .official_observed_execution == {
    "schedule": "task_pipelines",
    "concurrency_scope": "task_count",
    "per_task_concurrency": 1,
    "harbor_n_attempts": 1,
    "harbor_timeout_multipliers": {
      "timeout_multiplier": 1,
      "agent_timeout_multiplier": 10,
      "verifier_timeout_multiplier": 10,
      "agent_setup_timeout_multiplier": 1,
      "environment_build_timeout_multiplier": 10
    },
    "kimi_loop_control": {
      "max_retries_per_step": 10,
      "max_steps_per_run": 500
    }
  }
  and .supported_modes == ["smoke", "extended"]
' "$manifest" >/dev/null

[ "$($runner_root/kbench --version)" = "kbench 1.1.0" ]
"$script_dir/validate-run-contract.sh" "$example" >/dev/null

for path in "$script_dir"/*.sh "$runner_root/src/runner/templates/setup_and_smoke.sh.tmpl"; do
  bash -n "$path"
done

PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s "$runner_root/tests" -v
printf 'release=novita-kimi-code-bench-v1.1.0\nrelease_status=stable\nrelease_gate=passed\n'
