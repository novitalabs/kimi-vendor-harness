"""Remote streaming preflight through the production recording proxy."""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from recording_proxy import RecordingHandler, ThreadingHTTPServer


def _payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a professional AI assistant."},
            {"role": "user", "content": "Call get_weather for Beijing. Do not answer directly."},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }],
        "tool_choice": "auto",
        "temperature": 1,
        "top_p": 0.95,
        "stream": True,
        "stream_options": {"include_usage": True, "continuous_usage_stats": True},
        "max_tokens": 1280,
    }


def run_preflight(
    snapshot_path: str | Path,
    auth_file: str | Path,
    output_dir: str | Path,
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    snapshot = json.loads(Path(snapshot_path).read_text())
    api_key = Path(auth_file).read_text().strip()
    if not api_key:
        raise RuntimeError("preflight authentication file is empty")
    root = Path(output_dir)
    proxy_root = root / "llmproxy/openai"

    class PreflightHandler(RecordingHandler):
        target_base_url = snapshot["base_url"]
        output_dir = proxy_root
        identity = {
            "job_id": f"job_{snapshot['tag']}_preflight",
            "attempt_id": "preflight",
            "container_id": "preflight",
            "task_id": "preflight",
            "task_label": "preflight",
            "route_path": f"/routes/job_{snapshot['tag']}_preflight-preflight-openai-preflight",
            "phase": "preflight",
            "round": "0",
            "attempt": "0",
            "identity_source": "local_runner",
        }
        counter = 0

    proxy_root.mkdir(parents=True, exist_ok=True)
    target = urlsplit(snapshot["base_url"])
    identity = PreflightHandler.identity
    (proxy_root / "meta.json").write_text(json.dumps({
        "session_id": f"{identity['job_id']}-{identity['attempt_id']}-openai-{identity['container_id']}",
        "provider": "openai",
        "recording_schema_version": 2,
        "target_url": snapshot["base_url"],
        "target_scheme": target.scheme,
        "target_host": target.netloc,
        "target_path": target.path,
        **identity,
    }, indent=2) + "\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), PreflightHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    route = PreflightHandler.identity["route_path"]
    url = f"http://127.0.0.1:{server.server_port}{route}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **snapshot.get("headers", {}),
    }
    encoded = json.dumps(_payload(snapshot["model"]), ensure_ascii=False).encode()
    observed: list[dict[str, Any]] = []
    success = False
    try:
        for number in range(1, attempts + 1):
            started = time.monotonic()
            status = 0
            response_headers: dict[str, str] = {}
            body = b""
            try:
                response = urllib.request.urlopen(
                    urllib.request.Request(url, data=encoded, headers=headers, method="POST"),
                    timeout=180,
                )
                headers_at = time.monotonic()
                status = response.status
                response_headers = dict(response.headers)
                body = response.read()
            except urllib.error.HTTPError as exc:
                headers_at = time.monotonic()
                status = exc.code
                response_headers = dict(exc.headers)
                body = exc.read()
            finished = time.monotonic()
            text = body.decode("utf-8", errors="replace")
            record = {
                "attempt": number,
                "status_code": status,
                "ttfb_ms": round((headers_at - started) * 1000, 3),
                "total_ms": round((finished - started) * 1000, 3),
                "total_bytes": len(body),
                "done_count": text.count("data: [DONE]"),
                "tool_call_observed": "get_weather" in text,
                "x_request_id": response_headers.get("x-request-id") or response_headers.get("X-Request-Id"),
            }
            observed.append(record)
            if status == 200 and record["done_count"] == 1 and record["tool_call_observed"]:
                success = True
                break
            retry_after = response_headers.get("Retry-After") or response_headers.get("retry-after")
            try:
                delay = min(30.0, max(1.0, float(retry_after))) if retry_after else min(10.0, number * 2.0)
            except ValueError:
                delay = min(10.0, number * 2.0)
            time.sleep(delay)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    summary = {
        "schema_version": 1,
        "kind": "remote_recording_proxy_streaming_preflight",
        "base_url": snapshot["base_url"],
        "model": snapshot["model"],
        "headers": snapshot.get("headers", {}),
        "attempts": observed,
        "passed": success,
        "credential_source": "remote_auth_file",
        "arrival_precision": "socket_read_upper_bound",
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "preflight-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--attempts", type=int, default=3)
    args = parser.parse_args()
    result = run_preflight(args.snapshot, args.auth_file, args.output_dir, attempts=args.attempts)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
