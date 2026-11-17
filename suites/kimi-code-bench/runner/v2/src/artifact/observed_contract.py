"""Write the stable official-compatible leaf contract."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from archive_trial import classify_outcome


OFFICIAL_STABLE_FILES = (
    "artifact.json", "config.json", "docker-compose.yaml", "image-mounts.json",
    "agentgym-meta.json", "agentgym-compose-config/00.yaml", "agentgym-compose-config/01.yaml",
    "artifacts/manifest.json", "result.json", "trial.log", "verifier/reward.txt",
    "verifier/test-stdout.txt", "agent/kimi-cli.txt", "agent/.kimi/config.json.bak",
    "agent/.kimi/config.toml",
    "agent/.kimi/mcp.json", "agent/agent_prepare/agent_prepare.log", "llmproxy/openai/meta.json",
    "mangrove-meta.json",
)
LOCAL_SCHEMA_V2_FILES = (
    "agent/kimi-stream.jsonl",
    "agent/execution-meta.json",
)
STABLE_FILES = OFFICIAL_STABLE_FILES + LOCAL_SCHEMA_V2_FILES


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def build_observed_contract(trial_dir: str | Path) -> dict[str, Any]:
    root = Path(trial_dir)
    proxy = _json(root / "llmproxy/openai/meta.json", {})
    identity = _json(root / "agent/execution-identity.json", {})
    job = proxy.get("job_id") or identity.get("job_id")
    attempt = proxy.get("attempt_id") or identity.get("attempt_id")
    container = proxy.get("container_id") or identity.get("container_db_id")
    route = proxy.get("route_path") or identity.get("route_path")
    session = proxy.get("session_id") or (f"{job}-{attempt}-openai-{container}" if all((job, attempt, container)) else None)
    result = _json(root / "result.json", {})
    execution = _json(root / "agent/execution-meta.json", {})
    return {"contract": "novita-kimi-code-bench-official-observed", "profile": "official_observed", "identity": {"task": result.get("task_name") or root.name, "job_id": job, "attempt_id": attempt, "container_id": container, "route_path": route, "proxy_session_id": session}, "execution": execution, "outcome": classify_outcome(result, root), "mcp": _json(root / "agent/.kimi/mcp.json", {}), "proxy": {"request_count": len(list((root / "llmproxy/openai/requests").glob("*.json"))), "response_count": len(list((root / "llmproxy/openai/responses").glob("*.body")))}}


def write_observed_contract(trial_dir: str | Path) -> Path:
    root = Path(trial_dir)
    # Harbor normally places llmproxy under agent; retain a top-level alias for
    # the supplied official artifact layout.
    nested = root / "agent/llmproxy/openai"
    top = root / "llmproxy/openai"
    if not top.exists() and nested.exists():
        shutil.copytree(nested, top)
    meta = top / "meta.json"
    if not meta.exists():
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({
            "identity_source": "local_synthetic",
            "provider": "openai",
            "job_id": "local-job",
            "attempt_id": "local-attempt",
            "container_id": "local-container",
            "route_path": "/routes/local-job-local-attempt-openai-local-container",
            "session_id": "local-job-local-attempt-openai-local-container",
        }, indent=2) + "\n")
    compatibility = {
        "artifact.json": {"identity_source": "local_synthetic", "contract": "official-compatible-import"},
        "config.json": {"identity_source": "local_synthetic"},
        "image-mounts.json": {"identity_source": "local_synthetic", "mounts": []},
        "agentgym-meta.json": {"identity_source": "local_synthetic"},
        "artifacts/manifest.json": {"identity_source": "local_synthetic", "files": []},
        "mangrove-meta.json": {"identity_source": "local_synthetic"},
        "agent/.kimi/mcp.json": {"identity_source": "local_synthetic", "mcpServers": {}},
    }
    for relative in STABLE_FILES:
        path = root / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".json"):
            payload = compatibility.get(relative, {"identity_source": "local_synthetic"})
            path.write_text(json.dumps(payload, indent=2) + "\n")
        elif relative.endswith(".yaml") or relative.endswith(".toml"):
            path.write_text("# identity_source=local_synthetic\n")
        else:
            path.write_text("identity_source=local_synthetic\n")
    (root / "trial-record.json").write_text(json.dumps(build_observed_contract(root), ensure_ascii=False, indent=2) + "\n")
    return root / "trial-record.json"
