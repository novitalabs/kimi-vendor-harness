#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <run-contract.json> <run-directory>" >&2
  exit 2
fi

contract=$1
run_dir=$2
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

"$script_dir/validate-run-contract.sh" "$contract" >/dev/null
if [ ! -d "$run_dir" ]; then
  echo "run directory not found: $run_dir" >&2
  exit 2
fi

snapshot=$run_dir/config.snapshot.json
if [ ! -f "$snapshot" ]; then
  echo "config.snapshot.json is required: $snapshot" >&2
  exit 1
fi

jq empty "$snapshot"
contract_sha=$(shasum -a 256 "$contract" | awk '{print $1}')

jq -e --slurpfile contracts "$contract" --arg contract_sha "$contract_sha" '
  $contracts[0] as $contract
  | .tag == $contract.run.tag
  and .base_url == $contract.service.base_url
  and .model == $contract.service.model
  and .headers == $contract.service.headers
  and .preserve_thinking == $contract.service.preserve_thinking
  and .remote_workdir == $contract.remote.workdir
  and .runtime_cache.root == $contract.remote.runtime_cache_root
  and .runtime_cache.uv_version == $contract.pins.uv_version
  and .runtime_cache.harbor_python == $contract.pins.harbor_python
  and .runner_contract_sha256 == $contract_sha
  and .authentication.kind == $contract.authentication.kind
  and (.authentication.env_name // null) == ($contract.authentication.env_name // null)
  and .task_selection.kind == $contract.task_selection.kind
  and .task_selection.value == $contract.task_selection.value
  and .task_selection.count == $contract.run.selected_task_count
  and .attempts_per_task == $contract.run.attempts_per_task
  and .rounds == $contract.run.rounds
  and .expected_trial_count == $contract.run.required_trial_count
  and .concurrency == $contract.run.concurrency
  and .schedule == $contract.execution_profile.schedule
  and .concurrency_scope == $contract.execution_profile.concurrency_scope
  and .per_task_concurrency == $contract.execution_profile.per_task_concurrency
  and .recovery == $contract.execution_profile.recovery
  and .harbor_n_attempts == $contract.execution_profile.harbor_n_attempts
  and .harbor_timeout_multipliers == $contract.execution_profile.harbor_timeout_multipliers
  and .kimi_loop_control == $contract.execution_profile.kimi_loop_control
  and ([paths as $p | ($p[-1] | tostring | ascii_downcase) | select(. == "api_key" or . == "token" or . == "authorization" or . == "password" or . == "secret_value" or . == "credential")] | length == 0)
' "$snapshot" >/dev/null || {
  echo "config.snapshot.json does not match the run contract" >&2
  exit 1
}

runtime=$run_dir/runtime-versions.json
if [ ! -f "$runtime" ]; then
  echo "runtime-versions.json is required: $runtime" >&2
  exit 1
fi
jq empty "$runtime"
jq -e --slurpfile contracts "$contract" '
  $contracts[0] as $contract
  | .cache_schema_version == 1
  and .cache_root == $contract.remote.runtime_cache_root
  and (.cache_hits | type == "object")
  and (all(.cache_hits[]; type == "boolean"))
  and (.uv_version_output | startswith("uv " + $contract.pins.uv_version + " "))
  and (.harbor_version_output | contains($contract.pins.harbor_version))
  and (.harbor_python_output | startswith("Python " + $contract.pins.harbor_python + "."))
  and .kimi_cli_ref == $contract.pins.kimi_cli_ref
  and .terminal_bench_2_ref == $contract.pins.terminal_bench_2_ref
  and (.kimi_cli_wheel_sha256 | test("^[0-9a-f]{64}$"))
  and (.paths | type == "object")
  and (all(.paths[]; startswith($contract.remote.runtime_cache_root + "/")))
' "$runtime" >/dev/null || {
  echo "runtime-versions.json does not match the pinned runtime cache contract" >&2
  exit 1
}

"$script_dir/audit-run-secrets.sh" "$contract" "$run_dir"
printf 'run_artifacts=valid\n'
