"""Strict, local validation for one official-observed leaf."""

from __future__ import annotations

import json
from pathlib import Path
from .observed_contract import OFFICIAL_STABLE_FILES, STABLE_FILES, build_observed_contract
from config import OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS, OFFICIAL_KIMI_LOOP_CONTROL
from archive_trial import _post_verifier_agent_exit


def validate(trial_dir: str | Path) -> list[str]:
    root = Path(trial_dir)
    errors: list[str] = []
    proxy_path = root / "llmproxy/openai/meta.json"
    try:
        proxy = json.loads(proxy_path.read_text())
    except Exception as exc:
        errors.append(f"proxy-meta:{exc}")
        proxy = {}
    result = {}
    try:
        result = json.loads((root / "result.json").read_text())
    except Exception:
        pass
    abnormal = ((root / "exception.txt").exists() or bool(result.get("exception_info"))) and not _post_verifier_agent_exit(result)
    expected_session = f"{proxy.get('job_id')}-{proxy.get('attempt_id')}-openai-{proxy.get('container_id')}"
    for field in ("job_id", "attempt_id", "container_id", "route_path", "session_id"):
        if not proxy.get(field):
            errors.append(f"proxy-meta:{field}")
    if proxy.get("session_id") and proxy.get("session_id") != expected_session:
        errors.append("proxy-meta:session_id-mismatch")
    recording_schema = proxy.get("recording_schema_version")
    required_files = STABLE_FILES if recording_schema is not None and recording_schema >= 2 else OFFICIAL_STABLE_FILES
    errors.extend(f"missing:{path}" for path in required_files if not (root / path).is_file())
    if recording_schema is not None and recording_schema >= 2:
        if proxy.get("identity_source") != "local_runner":
            errors.append("proxy-meta:identity_source")
        for field in ("target_url", "target_scheme", "target_host", "target_path"):
            if not proxy.get(field):
                errors.append(f"proxy-meta:{field}")
        try:
            harbor_config = json.loads((root / "config.json").read_text())
            observed_timeouts = {
                field: harbor_config.get(field)
                for field in OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS
            }
            if observed_timeouts != OFFICIAL_HARBOR_TIMEOUT_MULTIPLIERS:
                errors.append("config:harbor-timeout-multipliers")
        except Exception as exc:
            errors.append(f"config:harbor-timeout-multipliers:{exc}")
        agent_execution_started = bool((result.get("agent_execution") or {}).get("started_at"))
        if not abnormal or agent_execution_started:
            try:
                kimi_config = json.loads((root / "agent/.kimi/config.json.bak").read_text())
                if kimi_config.get("loop_control") != OFFICIAL_KIMI_LOOP_CONTROL:
                    errors.append("config:kimi-loop-control")
            except Exception as exc:
                errors.append(f"config:kimi-loop-control:{exc}")
    try:
        mcp = json.loads((root / "agent/.kimi/mcp.json").read_text())
        if not isinstance(mcp, dict) or not isinstance(mcp.get("mcpServers"), dict):
            errors.append("mcp:mcpServers-must-be-object")
    except Exception as exc:
        errors.append(f"mcp:{exc}")
    if not (root / "exception.txt").exists() and not _post_verifier_agent_exit(result) and (
        result.get("exception_info") or result.get("agent_result", {}).get("exit_code", 0)
    ):
        errors.append("exception.txt-required-for-abnormal-termination")
    nested = root / "agent/llmproxy/openai"
    proxy_root = nested if nested.is_dir() else root / "llmproxy/openai"
    request_ids = {path.stem for path in (proxy_root / "requests").glob("*.json")}
    meta_ids = {path.stem for path in (proxy_root / "responses").glob("*.json")}
    body_ids = {path.stem for path in (proxy_root / "responses").glob("*.body")}
    if not abnormal and request_ids != meta_ids:
        errors.append(f"proxy:request-response-meta-mismatch:{len(request_ids)}:{len(meta_ids)}")
    if not abnormal and meta_ids != body_ids:
        errors.append(f"proxy:response-meta-body-mismatch:{len(meta_ids)}:{len(body_ids)}")
    for response_id in sorted(meta_ids):
        try:
            meta = json.loads((proxy_root / "responses" / f"{response_id}.json").read_text())
        except Exception as exc:
            errors.append(f"proxy:response-meta:{response_id}:{exc}")
            continue
        if not abnormal and recording_schema is not None and recording_schema >= 2:
            if meta.get("complete") is not True:
                errors.append(f"proxy:response-incomplete:{response_id}")
            if not isinstance(meta.get("client_disconnected"), bool):
                errors.append(f"proxy:client-disconnected-missing:{response_id}")
            status_code = meta.get("status_code", meta.get("status"))
            if status_code != 502 and not meta.get("arrival_journal"):
                errors.append(f"proxy:arrival-missing:{response_id}")
        elif not abnormal and meta.get("complete") is False:
            errors.append(f"proxy:response-incomplete:{response_id}")
        arrival = meta.get("arrival_journal")
        if arrival and not (proxy_root / Path(arrival).name).is_file() and not (proxy_root / arrival).is_file():
            errors.append(f"proxy:arrival-missing:{response_id}")
    return errors


def main(path: str) -> int:
    errors = validate(path)
    if errors:
        print("artifact-invalid")
        print("\n".join(errors))
        return 1
    print("artifact-valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1]))
