"""Archive one Harbor result into the observed Code Bench leaf contract."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


STABLE_FILES = {
    "artifact.json": {},
    "image-mounts.json": {"mounts": []},
    "agentgym-meta.json": {},
    "artifacts/manifest.json": {"files": []},
    "mangrove-meta.json": {},
    "agent/.kimi/config.json.bak": {},
    "agent/.kimi/config.toml": "",
    "agent/kimi-cli.txt": "",
    "agent/kimi-stream.jsonl": "",
    "agent/execution-meta.json": {"profile": "official_observed"},
    "agent/agent_prepare/agent_prepare.log": "",
    "docker-compose.yaml": "",
    "agentgym-compose-config/00.yaml": "",
    "agentgym-compose-config/01.yaml": "",
    "trial.log": "",
    "verifier/reward.txt": "0\n",
    "verifier/test-stdout.txt": "",
}

POST_VERIFIER_AGENT_EXIT_TYPES = frozenset({"NonZeroAgentExitCodeError"})


def _write_if_missing(path: Path, value: object) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, dict):
        path.write_text(json.dumps(value, indent=2) + "\n")
    else:
        path.write_text(str(value))


def _find_leaf(root: Path, task: str) -> tuple[Path, dict] | None:
    candidates: list[tuple[float, Path, dict]] = []
    for result_path in root.rglob("result.json"):
        leaf = result_path.parent
        if not (leaf / "agent").is_dir() and not (leaf / "exception.txt").is_file():
            continue
        try:
            result = json.loads(result_path.read_text())
        except Exception:
            continue
        if result.get("task_name") != task and not leaf.name.startswith(task + "__"):
            continue
        if (leaf / "kbench-attempt.json").is_file():
            continue
        candidates.append((result_path.stat().st_mtime, leaf, result))
    if not candidates:
        return None
    _, leaf, result = max(candidates, key=lambda item: item[0])
    return leaf, result


def _post_verifier_agent_exit(result: dict) -> bool:
    exception = result.get("exception_info") or {}
    if exception.get("exception_type") not in POST_VERIFIER_AGENT_EXIT_TYPES:
        return False
    agent_finished = (result.get("agent_execution") or {}).get("finished_at")
    verifier_started = (result.get("verifier") or {}).get("started_at")
    if not agent_finished or not verifier_started:
        return False
    try:
        agent_time = datetime.fromisoformat(agent_finished.replace("Z", "+00:00"))
        verifier_time = datetime.fromisoformat(verifier_started.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return agent_time <= verifier_time


def classify_outcome(result: dict, leaf: Path) -> dict:
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    exception_present = bool((leaf / "exception.txt").exists() or result.get("exception_info"))
    post_verifier_warning = _post_verifier_agent_exit(result) and float(reward or 0) == 1.0
    invalid = reward is None or (exception_present and not post_verifier_warning)
    outcome = {"reward": reward, "execution_valid": not invalid}
    if post_verifier_warning:
        outcome.update(
            {
                "termination_reason": "agent_process_error_after_verifier",
                "execution_warning": "NonZeroAgentExitCodeError",
            }
        )
    elif invalid:
        outcome["termination_reason"] = "execution_invalid"
    else:
        outcome["termination_reason"] = "clean_reward_one" if float(reward or 0) == 1.0 else "clean_reward_zero"
    return outcome


def archive(args: argparse.Namespace) -> int:
    root = Path(args.jobs_root)
    found = _find_leaf(root, args.task)
    leaves = 0
    result: dict = {}
    if found is not None:
        leaf, result = found
        leaves = 1
        (leaf / "agent").mkdir(parents=True, exist_ok=True)
        attempt = {
            "profile": "official_observed",
            "job_id": args.job_id,
            "attempt_id": args.attempt_id,
            "container_id": args.container_id,
            "task_id": args.task_id,
            "task_label": args.task,
            "route_path": args.route_path,
            "phase": args.phase,
            "round": int(args.round),
            "attempt": int(args.attempt),
        }
        (leaf / "kbench-attempt.json").write_text(json.dumps(attempt, indent=2) + "\n")
        nested = leaf / "agent/llmproxy/openai"
        top = leaf / "llmproxy/openai"
        if nested.is_dir() and not top.exists():
            shutil.copytree(nested, top)
        proxy = nested if nested.is_dir() else top
        proxy.mkdir(parents=True, exist_ok=True)
        _write_if_missing(proxy / "meta.json", {
            "session_id": f"{args.job_id}-{args.attempt_id}-openai-{args.container_id}",
            "provider": "openai",
            "job_id": args.job_id,
            "attempt_id": args.attempt_id,
            "container_id": args.container_id,
            "task_id": args.task_id,
            "task_label": args.task,
            "route_path": args.route_path,
            "phase": args.phase,
            "round": str(args.round),
            "attempt": str(args.attempt),
            "identity_source": "local_runner",
        })
        mcp = leaf / "agent/.kimi/mcp.json"
        _write_if_missing(mcp, {"mcpServers": {}})
        for relative, value in STABLE_FILES.items():
            _write_if_missing(leaf / relative, value)
        outcome = classify_outcome(result, leaf)
        contract = {
            "contract": "novita-kimi-code-bench-official-observed",
            "profile": "official_observed",
            "phase": args.phase,
            "round": int(args.round),
            "attempt": int(args.attempt),
            "identity": {
                "task": args.task,
                "job_id": args.job_id,
                "attempt_id": args.attempt_id,
                "container_id": args.container_id,
                "route_path": args.route_path,
                "proxy_session_id": f"{args.job_id}-{args.attempt_id}-openai-{args.container_id}",
                "identity_source": "local_runner",
            },
            "execution": _read_json(leaf / "agent/execution-meta.json", {}),
            "outcome": outcome,
            "mcp": _read_json(mcp, {"mcpServers": {}}),
            "proxy": {
                "request_count": len(list(proxy.glob("requests/*.json"))),
                "response_count": len(list(proxy.glob("responses/*.body"))),
            },
        }
        (leaf / "trial-record.json").write_text(json.dumps(contract, indent=2) + "\n")
        ok = int(float(outcome["reward"] or 0) > 0)
        bad = int(not outcome["execution_valid"])
    else:
        ok = 0
        bad = 1
    Path(args.output).write_text(
        f"{args.task}\t{args.phase}\t{args.round}\t{args.attempt}\t{leaves}\t{ok}\t{bad}\t{root}\n"
    )
    return 0


def _read_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else default
    except Exception:
        return default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True)
    parser.add_argument("--jobs-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--round", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--route-path", required=True)
    return archive(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
