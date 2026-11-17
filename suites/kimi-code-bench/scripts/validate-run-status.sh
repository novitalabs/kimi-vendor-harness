#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <run-contract.json> <status.json>" >&2
  exit 2
fi

contract=$1
status=$2
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

"$script_dir/validate-run-contract.sh" "$contract" >/dev/null
if [ ! -f "$status" ]; then
  echo "status file not found: $status" >&2
  exit 2
fi

jq empty "$status"
contract_sha=$(shasum -a 256 "$contract" | awk '{print $1}')

jq -e --slurpfile contracts "$contract" --arg contract_sha "$contract_sha" '
  def nonempty: type == "string" and length > 0;
  def nonnegative: type == "number" and . >= 0;

  $contracts[0] as $contract
  | .schema_version == 2
  and (.observed_at | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T"))
  and (.tag == $contract.run.tag)
  and (.runner_contract_sha256 == $contract_sha)
  and (.state == "planned" or .state == "preparing" or .state == "running" or .state == "completed" or .state == "failed" or .state == "paused" or .state == "unknown" or .state == "finalized")
  and (.phase == "preflight" or .phase == "remote_setup" or .phase == "pull_and_test" or .phase == "fetch" or .phase == "triage" or .phase == "metrics" or .phase == "done")
  and (.remote.workdir == $contract.remote.workdir)
  and (.remote.marker == "none" or .remote.marker == "running" or .remote.marker == "done" or .remote.marker == "paused" or .remote.marker == "failed" or .remote.marker == "unknown")
  and (.remote.pid == null or (.remote.pid | type == "number" and . >= 1))
  and (.remote.exit_code == null or (.remote.exit_code | type == "number"))
  and (.local_run_dir == $contract.run.evidence_dir)
  and (.progress.selected_tasks == $contract.run.selected_task_count)
  and (.progress.required_trials == $contract.run.required_trial_count)
  and (.progress.started_trials | nonnegative)
  and (.progress.finished_trials | nonnegative)
  and (.progress.valid_pass | nonnegative)
  and (.progress.valid_zero | nonnegative)
  and (.progress.error_zero | nonnegative)
  and (.progress.unclassified | nonnegative)
  and (.progress.not_started_trials | nonnegative)
  and (.progress.started_trials + .progress.not_started_trials == .progress.required_trials)
  and (.progress.finished_trials <= .progress.started_trials)
  and (.progress.valid_pass + .progress.valid_zero + .progress.error_zero + .progress.unclassified == .progress.finished_trials)
  and (.activity.setup_log_bytes | nonnegative)
  and (.activity.last_progress_at == null or (.activity.last_progress_at | nonempty))
  and (.evidence | type == "object")
  and (.blockers | type == "array")
  and (if .state == "running" then .remote.marker == "running" and .remote.exit_code == null and .headline_result == null else true end)
  and (if .state == "failed" then .remote.marker == "failed" or .remote.marker == "unknown" else true end)
  and (if .state == "paused" then .remote.marker == "paused" and .headline_result == null else true end)
  and (if .state == "finalized" then .phase == "done" and .progress.selected_tasks == $contract.run.fixed_denominator and .progress.finished_trials == $contract.run.required_trial_count and .progress.unclassified == 0 and (.headline_result | nonempty) else .headline_result == null end)
  and ([paths as $p | ($p[-1] | tostring | ascii_downcase) | select(. == "api_key" or . == "token" or . == "authorization" or . == "password" or . == "secret_value" or . == "credential")] | length == 0)
' "$status" >/dev/null || {
  echo "run status does not satisfy the contract or its progress counts are inconsistent" >&2
  exit 1
}

jq -r '
  [
    "run_status=valid",
    "tag=" + .tag,
    "state=" + .state,
    "phase=" + .phase,
    "trials=" + (.progress.finished_trials | tostring) + "/" + (.progress.required_trials | tostring),
    "error_zero=" + (.progress.error_zero | tostring),
    "not_started_trials=" + (.progress.not_started_trials | tostring)
  ] | .[]
' "$status"
