"""Minimal per-trial OpenAI/SSE recording proxy.

The proxy forwards bytes unchanged and writes request JSON plus response
headers/body/[DONE] evidence. It intentionally has no credentials of its own.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as _ThreadingHTTPServer
from pathlib import Path


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


class ThreadingHTTPServer(_ThreadingHTTPServer):
    """HTTP server with a bounded drain for in-flight recordings."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._recording_condition = threading.Condition()
        self._active_recordings = 0
        self._accepting_recordings = True

    def begin_recording(self) -> bool:
        with self._recording_condition:
            if not self._accepting_recordings:
                return False
            self._active_recordings += 1
            return True

    def finish_recording(self) -> None:
        with self._recording_condition:
            self._active_recordings -= 1
            self._recording_condition.notify_all()

    def drain_recordings(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        with self._recording_condition:
            self._accepting_recordings = False
            while self._active_recordings:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._recording_condition.wait(remaining)
            return self._active_recordings


class RecordingHandler(BaseHTTPRequestHandler):
    target_base_url = ""
    output_dir = Path(".")
    identity: dict = {}
    counter = 0
    lock = threading.Lock()

    def log_message(self, *_args):
        return

    def _write_client(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/__kbench_health":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if parsed.path == "/__kbench_drain":
            try:
                timeout = float(parse_qs(parsed.query).get("timeout", ["120"])[0])
            except (TypeError, ValueError):
                self.send_error(400, "invalid drain timeout")
                return
            active = self.server.drain_recordings(max(0.0, min(timeout, 900.0)))
            payload = json.dumps({"drained": active == 0, "active_requests": active}).encode()
            self.send_response(200 if active == 0 else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)

    def do_POST(self):
        if not self.server.begin_recording():
            payload = b'{"error":"recording proxy is draining"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write_client(payload)
            return
        try:
            self._record_post()
        finally:
            self.server.finish_recording()

    def _record_post(self):
        request_started = time.monotonic_ns()
        request_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        length = int(self.headers.get("content-length", "0"))
        payload = self.rfile.read(length)
        with self.lock:
            self.__class__.counter += 1
            number = self.__class__.counter
        root = self.output_dir
        (root / "requests").mkdir(parents=True, exist_ok=True)
        (root / "responses").mkdir(parents=True, exist_ok=True)
        headers = dict(self.headers)
        for name in list(headers):
            if name.lower() in {"authorization", "x-api-key", "api-key"}:
                headers[name] = "<redacted>"
        try:
            body = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            body = {"raw_bytes": len(payload)}
        request = {
            "method": "POST", "path": self.path, "headers": headers, "body": body,
            "timestamp": request_started_at, "started_at": request_started_at,
        }
        _write_json_atomic(root / "requests" / f"{number:04d}.json", request)
        # The route path identifies this local runner session and must not be
        # sent to the vendor endpoint.
        request_path = self.path
        route_prefix = self.identity.get("route_path", "")
        if route_prefix and request_path.startswith(route_prefix):
            request_path = request_path[len(route_prefix):] or "/"
        url = self.target_base_url.rstrip("/") + request_path
        forward_headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "content-length"}}
        try:
            upstream_request = urllib.request.Request(url, data=payload, headers=forward_headers, method="POST")
            try:
                response = urllib.request.urlopen(upstream_request, timeout=900)
            except urllib.error.HTTPError as exc:
                response = exc
            status = response.status
            response_headers = dict(response.headers)
            response_headers_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self.send_response(status)
            for k, v in response_headers.items():
                if k.lower() not in {"content-length", "transfer-encoding", "connection"}:
                    self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            body_path = root / "responses" / f"{number:04d}.body"
            arrival_path = root / "responses" / f"{number:04d}.arrival.jsonl"
            meta_path = root / "responses" / f"{number:04d}.json"
            total_bytes = 0
            socket_chunk_count = 0
            response_meta = {
                "id": f"{number:04d}", "timestamp": response_headers_at,
                "status": status, "status_code": status, "headers": response_headers,
                "is_streaming": response_headers.get("Content-Type", "").startswith("text/event-stream"),
                "total_bytes": 0, "socket_chunk_count": 0,
                "response_headers_at": response_headers_at,
                "response_finished_at": None,
                "arrival_journal": f"responses/{number:04d}.arrival.jsonl",
                "complete": False,
                "client_disconnected": False,
                "latency_ms": None,
            }
            _write_json_atomic(meta_path, response_meta)
            client_connected = True
            read_chunk = getattr(response, "read1", response.read)
            with body_path.open("wb") as body_file, arrival_path.open("w") as arrival_file:
                while True:
                    chunk = read_chunk(8192)
                    if not chunk:
                        break
                    now_ns = time.monotonic_ns()
                    now_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    total_bytes += len(chunk)
                    socket_chunk_count += 1
                    body_file.write(chunk)
                    body_file.flush()
                    arrival_file.write(json.dumps({
                        "timestamp": now_at,
                        "monotonic_ns": now_ns,
                        "bytes": len(chunk),
                        "cumulative_bytes": total_bytes,
                    }) + "\n")
                    arrival_file.flush()
                    if client_connected:
                        client_connected = self._write_client(
                            f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n"
                        )
            response_finished = time.monotonic_ns()
            response_finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            response_meta.update({
                "timestamp": response_finished_at,
                "total_bytes": total_bytes,
                "socket_chunk_count": socket_chunk_count,
                "response_finished_at": response_finished_at,
                "complete": True,
                "client_disconnected": not client_connected,
                "latency_ms": (response_finished - request_started) / 1_000_000,
            })
            _write_json_atomic(meta_path, response_meta)
            if client_connected and not self._write_client(b"0\r\n\r\n"):
                response_meta["client_disconnected"] = True
                _write_json_atomic(meta_path, response_meta)
            return
        except Exception as exc:
            status, body, response_headers = 502, str(exc).encode(), {}
        response_finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        _write_json_atomic(root / "responses" / f"{number:04d}.json", {
            "id": f"{number:04d}", "timestamp": response_finished_at,
            "status": status, "status_code": status, "headers": response_headers,
            "is_streaming": False, "total_bytes": len(body), "socket_chunk_count": 1,
            "response_finished_at": response_finished_at,
            "arrival_journal": None,
            "complete": True,
            "client_disconnected": False,
            "latency_ms": (time.monotonic_ns() - request_started) / 1_000_000,
        })
        (root / "responses" / f"{number:04d}.body").write_bytes(body)
        self.send_response(status)
        for k, v in response_headers.items():
            if k.lower() not in {"content-length", "transfer-encoding", "connection"}:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._write_client(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-base-url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    for name in ("job-id", "attempt-id", "container-id", "task-id", "task-label", "route-path", "phase", "round", "attempt"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    identity = {
        "job_id": args.job_id,
        "attempt_id": args.attempt_id,
        "container_id": args.container_id,
        "task_id": args.task_id,
        "task_label": args.task_label,
        "route_path": args.route_path,
        "phase": args.phase,
        "round": args.round,
        "attempt": args.attempt,
        "identity_source": "local_runner",
    }
    root = Path(args.output_dir)
    root.mkdir(parents=True, exist_ok=True)
    session = f"{args.job_id}-{args.attempt_id}-openai-{args.container_id}"
    target = urlsplit(args.target_base_url)
    _write_json_atomic(root / "meta.json", {
        "session_id": session,
        "provider": "openai",
        "recording_schema_version": 2,
        "target_url": args.target_base_url,
        "target_scheme": target.scheme,
        "target_host": target.netloc,
        "target_path": target.path,
        **identity,
    })
    RecordingHandler.target_base_url, RecordingHandler.output_dir, RecordingHandler.identity = args.target_base_url, root, identity
    server = ThreadingHTTPServer((args.host, args.port), RecordingHandler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
