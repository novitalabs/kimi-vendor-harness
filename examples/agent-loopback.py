#!/usr/bin/env python3
"""Bounded agent-protocol exercise: two loopback requests, expected contract failure.

Accepts no endpoint input, performs no repairs, executes no generated tool calls.
Run with the checkout's external environment: python examples/agent-loopback.py
--out /new/task/output. A successful exercise reports an intentionally failed
endpoint case with code tool_name_undeclared.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def exercise(out: Path) -> dict:
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=False)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("invalid request length")
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
            except (ValueError, OSError):
                self.send_error(400)
                return
            requests.append(request)
            if self.path != "/v1/chat/completions" or len(requests) > 2:
                self.send_error(429)
                return
            call = {"id": "call_0", "type": "function", "function": {"name": "unexpected", "arguments": "{}"}}
            if request["stream"]:
                frames = [
                    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, **call, "function": {"name": "unexpected", "arguments": ""}}]}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                ]
                payload = ("".join("data: " + json.dumps({"id": "chatcmpl-demo", "model": "loopback-fake", "created": 0, "object": "chat.completion.chunk", **frame}) + "\n\n" for frame in frames) + "data: [DONE]\n\n").encode()
            else:
                payload = json.dumps({"id": "chatcmpl-demo", "model": "loopback-fake", "created": 0, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "", "tool_calls": [call]}, "finish_reason": "tool_calls"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if request["stream"] else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    env = {key: value for key, value in os.environ.items() if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    env.update(NO_PROXY="127.0.0.1,localhost", KIMI_DEMO_KEY="synthetic-loopback-only-key", NVST_RESULTS_ROOT=str(out / "runs"))
    access = out / "service-access.json"
    access.write_text(json.dumps({"model_family": "kimi-k3", "base_url": f"http://127.0.0.1:{server.server_port}/v1", "served_model": "loopback-fake",
                                 "api_key_env": "KIMI_DEMO_KEY", "headers": {}, "think_mode": "opensource"}, indent=2) + "\n")
    plan_path = out / "plan.json"

    def invoke(filename: str, args: list[str], expected_exit: int) -> dict:
        process = subprocess.run([sys.executable, "-m", "novita_self_test", *args], env=env,
                                 capture_output=True, text=True, timeout=30)
        (out / filename).write_text(process.stdout)
        (out / (filename + ".stderr")).write_text(process.stderr)
        try:
            value = json.loads(process.stdout)
        except ValueError as exc:
            raise RuntimeError(f"CLI did not return a JSON result: see {filename}") from exc
        if process.returncode != expected_exit:
            raise RuntimeError(f"{args[0]} returned unexpected exit {process.returncode}: see {filename}")
        return value

    try:
        plan = invoke("plan-stdout.json", ["plan", "--select", "tool_call.choice.required", "--service-access", str(access), "--max-requests", "2", "--timeout", "5", "--out", str(plan_path)], 0)
        if not plan["executable"] or plan["request_budget"] != 2:
            raise RuntimeError("unexpected plan")
        doctor = invoke("doctor.json", ["doctor", "--plan", str(plan_path), "--service-access", str(access)], 0)
        if doctor["status"] != "passed" or requests:
            raise RuntimeError("preflight generated traffic or failed")
        report = invoke("report.json", ["run", "--plan", str(plan_path), "--service-access", str(access), "--execute"], 1)
        codes = {problem["code"] for row in report["results"] for problem in row["problems"]}
        if report["status"] != "failed" or "tool_name_undeclared" not in codes or len(requests) != report["actual_requests"] or len(requests) != 2:
            raise RuntimeError("negative control was not detected within the two-request ceiling")
        summary = {"status": "exercise_passed", "endpoint_verdict": report["status"], "detected_codes": sorted(codes),
                   "actual_loopback_requests": len(requests), "plan_sha256": plan["plan_sha256"],
                   "run_dir": report["run_dir"], "evidence": [row["evidence"] for row in report["results"]],
                   "warning": "Synthetic loopback exercise, not real-engine evidence or permission to run live traffic."}
        (out / "agent-flow.json").write_text(json.dumps(summary, indent=2) + "\n")
        return summary
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="new output directory; existing results are never overwritten")
    args = parser.parse_args()
    print(json.dumps(exercise(args.out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
