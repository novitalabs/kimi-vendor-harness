#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <run-contract.json>" >&2
  exit 2
fi

contract=$1
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -f "$contract" ]; then
  echo "run contract not found: $contract" >&2
  exit 2
fi

for tool in jq rg shasum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "$tool is required" >&2
    exit 2
  fi
done

jq empty "$contract"
jq -e '
  def nonempty: type == "string" and length > 0;
  def sha256: type == "string" and test("^[0-9a-f]{64}$");
  def git_sha: type == "string" and test("^[0-9a-f]{40}$");
  def version_token: type == "string" and test("^[0-9A-Za-z][0-9A-Za-z._+-]*$");
  def safe_relpath: nonempty and (startswith("/") | not) and (contains("..") | not);
  def string_array: type == "array" and length > 0 and all(.[]; nonempty);
  def selected_plan:
    if .task_selection.value == "extended" then
      .run.selected_task_count == 89
      and .run.fixed_denominator == 89
      and .task_selection.expected_count == 89
    elif .task_selection.value == "smoke" then
      .run.selected_task_count == 15
      and .run.fixed_denominator == 15
      and .task_selection.expected_count == 15
    else false end;
  def valid_concurrency:
    if (.execution_profile | has("concurrency")) then
      (.execution_profile.concurrency | type == "number" and . >= 1 and . == floor)
      and (.run.concurrency == .execution_profile.concurrency)
    else .run.concurrency == 8 end;
  def official_harbor_timeout_multipliers:
    .execution_profile.harbor_timeout_multipliers == {
      "timeout_multiplier": 1,
      "agent_timeout_multiplier": 10,
      "verifier_timeout_multiplier": 10,
      "agent_setup_timeout_multiplier": 1,
      "environment_build_timeout_multiplier": 10
    };
  def official_kimi_loop_control:
    .execution_profile.kimi_loop_control == {
      "max_retries_per_step": 10,
      "max_steps_per_run": 500
    };
  def valid_execution_profile:
    if .execution_profile.kind == "official_observed" then
      (.run.attempts_per_task == 3)
      and (.run.rounds == 3)
      and valid_concurrency
      and (.service.preserve_thinking == true)
      and (.task_selection.value == "extended" or .task_selection.value == "smoke")
      and (.acceptance == null)
      and (.execution_profile.concurrency == null
           or (.execution_profile.concurrency | type == "number" and . >= 1 and . == floor))
      and (if .execution_profile.concurrency == null
           then .run.concurrency >= 1
           else .run.concurrency == .execution_profile.concurrency
           end)
      and (.execution_profile.schedule == "task_pipelines")
      and (.execution_profile.concurrency_scope == "task_count")
      and (.execution_profile.per_task_concurrency | type == "number" and . == 1)
      and (.execution_profile.recovery == {"enabled": true, "attempts_per_abnormal_task": 1})
      and (.execution_profile.harbor_n_attempts == 1)
      and official_harbor_timeout_multipliers
      and official_kimi_loop_control
    else false end;

  .run.tag as $tag
  | .run.evidence_dir as $evidence_dir
  | .remote.runtime_cache_root as $cache_root
  | .task_selection as $selection
  | .schema_version == 2
  and ($tag | test("^[a-z0-9][a-z0-9._-]*$") and length <= 128)
  and (.run.kind == "benchmark")
  and (.run.attempts_per_task | type == "number" and . >= 3)
  and (.run.rounds | type == "number" and . >= 3 and . == floor)
  and (.run.rounds == .run.attempts_per_task)
  and (.run.required_trial_count == (.run.selected_task_count * .run.attempts_per_task))
  and (.run.concurrency == .run.selected_task_count)
  and (.run.concurrency | type == "number" and . >= 1 and . == floor)
  and (.run.detached | type == "boolean")
  and ($evidence_dir | nonempty and contains($tag))
  and (.run.status_path | nonempty and startswith($evidence_dir + "/"))
  and .run.source_run_tag == null
  and (.runner.root | safe_relpath)
  and (.runner.files | type == "array" and length >= 4)
  and (all(.runner.files[]; (.path | safe_relpath) and (.sha256 | sha256)))
  and (.runner.files as $files | ([$files[].path] | unique | length) == ($files | length))
  and ([.runner.files[].path] | index("kbench") != null)
  and ([.runner.files[].path] | index("release-manifest.json") != null)
  and (.runner.semantics.task_execution == "task-pipelines-with-recovery")
  and (.runner.semantics.schedule == .execution_profile.schedule)
  and (.runner.semantics.concurrency_scope == .execution_profile.concurrency_scope)
  and (.runner.semantics.rounds == .run.rounds)
  and (.runner.semantics.pull_timeout_seconds | type == "number" and . >= 1)
  and (.runner.semantics.attempts_per_task == .run.attempts_per_task)
  and (.runner.semantics.per_task_concurrency == .execution_profile.per_task_concurrency)
  and (.runner.semantics.harbor_n_attempts == .execution_profile.harbor_n_attempts)
  and (.runner.semantics.harbor_timeout_multipliers == .execution_profile.harbor_timeout_multipliers)
  and (.runner.semantics.kimi_loop_control == .execution_profile.kimi_loop_control)
  and (.runner.semantics.required_markers | type == "array" and length >= 4)
  and (all(.runner.semantics.required_markers[]; (.file | safe_relpath) and (.text | nonempty)))
  and (.runner.semantics.forbidden_markers | type == "array")
  and (all(.runner.semantics.forbidden_markers[]; (.file | safe_relpath) and (.text | nonempty)))
  and (.task_selection.kind == "builtin" or .task_selection.kind == "file")
  and (.task_selection.kind == "builtin")
  and (if (.task_selection | has("tasks")) then
         ($selection.tasks | type == "array" and length == $selection.expected_count)
         and ([$selection.tasks[]] | unique | length == $selection.expected_count)
         and (all($selection.tasks[]; nonempty))
       else true end)
  and selected_plan
  and valid_execution_profile
  and (.service.base_url | test("^https?://"))
  and (.service.model | nonempty)
  and (.service.headers | type == "object")
  and (all(.service.headers | to_entries[]; (.key | nonempty) and (.value | nonempty)))
  and (all(.service.headers | keys[]; test("authorization|api[-_]?key|token|password|secret"; "i") | not))
  and (.service.preserve_thinking | type == "boolean")
  and (.authentication.kind == "env" or .authentication.kind == "oauth_file" or .authentication.kind == "none")
  and (if .authentication.kind == "none" then .authentication.env_name == null else (.authentication.env_name | test("^[A-Z_][A-Z0-9_]*$")) end)
  and (.pins.kimi_cli_ref | git_sha)
  and (.pins.harbor_version | version_token)
  and (.pins.harbor_python | test("^[0-9]+\\.[0-9]+$"))
  and (.pins.terminal_bench_2_ref | git_sha)
  and (.pins.uv_version | version_token)
  and (.preflight.kind == "remote_recording_proxy_streaming")
  and (.preflight.attempts | type == "number" and . >= 1 and . <= 10 and . == floor)
  and (.preflight.require_done == true)
  and (.preflight.require_tool_call == "get_weather")
  and (.remote.host | nonempty)
  and (.remote.port | type == "number" and . >= 1 and . <= 65535)
  and (.remote.workdir | startswith("/") and contains($tag))
  and (.remote.container_proxy | test("^https?://[^/]+:[0-9]+$"))
  and ($cache_root | startswith("/") and contains(".kbench-cache"))
  and (.remote.workdir | startswith($cache_root + "/") | not)
  and (.duplicate_guard.identity_fields | string_array)
  and (.duplicate_guard.on_exact_match == "stop-and-inspect")
  and (.duplicate_guard.identity_fields | index("pins.terminal_bench_2_ref") != null)
  and (.duplicate_guard.identity_fields | index("pins.harbor_python") != null)
  and (.duplicate_guard.identity_fields | index("pins.uv_version") != null)
  and (.duplicate_guard.identity_fields | index("remote.runtime_cache_root") != null)
  and (.secret_policy.rendered_artifacts_must_be_secret_free == true)
  and (.secret_policy.archives_must_be_secret_free == true)
  and (.secret_policy.logs_and_reports_must_be_secret_free == true)
  and (.secret_policy.command_arguments_must_be_secret_free == true)
  and (.secret_policy.remote_secret_cleanup_required == true)
  and ([paths as $p | ($p[-1] | tostring | ascii_downcase) | select(. == "api_key" or . == "token" or . == "authorization" or . == "password" or . == "secret_value" or . == "credential")] | length == 0)
' "$contract" >/dev/null || {
  echo "run contract does not satisfy the Novita Kimi Code Bench contract" >&2
  exit 1
}

skill_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
runner_root_spec=$(jq -r '.runner.root' "$contract")
runner_root=$skill_root/$runner_root_spec
if [ "$runner_root_spec" != "runner/v2" ]; then
  echo "Novita Kimi Code Bench requires runner.root=runner/v2" >&2
  exit 1
fi
if [ ! -d "$runner_root" ]; then
  echo "bundled runner root not found: $runner_root" >&2
  exit 1
fi

jq -e '
  .release == "novita-kimi-code-bench-v1.1.0"
  and .runner_version == "1.1.0"
  and .stability == "stable"
  and .run_contract_schema == 2
  and .artifact_contract_version == "1.0"
  and .metrics_contract_version == "1.0"
  and .recording_schema_version == 2
  and .runtime_cache_schema_version == 1
  and .execution_profile == "official_observed"
  and .official_observed_execution.schedule == "task_pipelines"
  and .official_observed_execution.concurrency_scope == "task_count"
  and .official_observed_execution.per_task_concurrency == 1
  and .official_observed_execution.harbor_n_attempts == 1
  and .official_observed_execution.harbor_timeout_multipliers == {
    "timeout_multiplier": 1,
    "agent_timeout_multiplier": 10,
    "verifier_timeout_multiplier": 10,
    "agent_setup_timeout_multiplier": 1,
    "environment_build_timeout_multiplier": 10
  }
  and .official_observed_execution.kimi_loop_control == {
    "max_retries_per_step": 10,
    "max_steps_per_run": 500
  }
  and .supported_modes == ["smoke", "extended"]
' "$runner_root/release-manifest.json" >/dev/null || {
  echo "bundled runner release manifest is not the stable v1.1.0 contract" >&2
  exit 1
}

while IFS=$'\t' read -r relpath expected; do
  full_path=$runner_root/$relpath
  if [ ! -f "$full_path" ]; then
    echo "runner file not found: $full_path" >&2
    exit 1
  fi
  actual=$(shasum -a 256 "$full_path" | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then
    echo "runner fingerprint mismatch: $relpath" >&2
    exit 1
  fi
done < <(jq -r '.runner.files[] | [.path, .sha256] | @tsv' "$contract")

while IFS=$'\t' read -r relpath marker; do
  full_path=$runner_root/$relpath
  if [ ! -f "$full_path" ] || ! rg -Fq -- "$marker" "$full_path"; then
    echo "required runner semantic marker missing: $relpath" >&2
    exit 1
  fi
done < <(jq -r '.runner.semantics.required_markers[] | [.file, .text] | @tsv' "$contract")

while IFS=$'\t' read -r relpath marker; do
  full_path=$runner_root/$relpath
  if [ -f "$full_path" ] && rg -Fq -- "$marker" "$full_path"; then
    echo "forbidden runner semantic marker found: $relpath" >&2
    exit 1
  fi
done < <(jq -r '.runner.semantics.forbidden_markers[] | [.file, .text] | @tsv' "$contract")

contract_sha=$(shasum -a 256 "$contract" | awk '{print $1}')
jq -r --arg sha "$contract_sha" --arg runner_root "$runner_root" '
  [
    "run_contract=valid",
    "contract_sha256=" + $sha,
    "tag=" + .run.tag,
    "runner=" + $runner_root,
    "task_execution=" + .runner.semantics.task_execution,
    "schedule=" + .execution_profile.schedule,
    "concurrency_scope=" + .execution_profile.concurrency_scope,
    "global_concurrency=" + (.run.concurrency | tostring),
    "per_task_concurrency=" + (.execution_profile.per_task_concurrency | tostring),
    "selected_tasks=" + (.run.selected_task_count | tostring),
    "attempts_per_task=" + (.run.attempts_per_task | tostring),
    "rounds=" + (.run.rounds | tostring),
    "required_trials=" + (.run.required_trial_count | tostring),
    "fixed_denominator=" + (.run.fixed_denominator | tostring),
    "execution_profile=" + (.execution_profile.kind // "none"),
    "harbor_n_attempts=" + (.execution_profile.harbor_n_attempts | tostring),
    "harbor_timeout_multipliers=" + ([
      .execution_profile.harbor_timeout_multipliers.timeout_multiplier,
      .execution_profile.harbor_timeout_multipliers.agent_timeout_multiplier,
      .execution_profile.harbor_timeout_multipliers.verifier_timeout_multiplier,
      .execution_profile.harbor_timeout_multipliers.agent_setup_timeout_multiplier,
      .execution_profile.harbor_timeout_multipliers.environment_build_timeout_multiplier
    ] | map(tostring) | join("/")),
    "kimi_loop_control=" + ([
      .execution_profile.kimi_loop_control.max_retries_per_step,
      .execution_profile.kimi_loop_control.max_steps_per_run
    ] | map(tostring) | join("/")),
    "acceptance=" + (.acceptance.kind // "none"),
    "auth_source=" + .authentication.kind + (if .authentication.env_name then ":" + .authentication.env_name else "" end)
  ] | .[]
' "$contract"
