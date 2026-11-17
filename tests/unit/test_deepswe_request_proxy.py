from __future__ import annotations

import json
import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

_PROXY_PATH = Path(__file__).resolve().parents[2] / "suites" / "deepswe" / "pier_kimi" / "request_proxy.py"
_SPEC = importlib.util.spec_from_file_location("deepswe_request_proxy", _PROXY_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
Handler = _MODULE.Handler
Recorder = _MODULE.Recorder


class TargetHandler(BaseHTTPRequestHandler):
    body = b""
    authorization = ""
    request_path = ""

    def log_message(self, *_args) -> None:
        return

    def do_POST(self) -> None:
        type(self).request_path = self.path
        type(self).body = self.rfile.read(int(self.headers.get("content-length", "0")))
        type(self).authorization = self.headers.get("Authorization", "")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Request-ID", "upstream-request-7")
        self.send_header("X-Replica-Rank", "replica-2/rank-1")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


def _start(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_request_proxy_records_secret_safe_wire_evidence(tmp_path: Path) -> None:
    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = _start(target)
    output = tmp_path / "request-evidence"
    proxy = Recorder(("127.0.0.1", 0), Handler)
    proxy.target = f"http://127.0.0.1:{target.server_port}"
    proxy.api_key = "runtime-secret"
    proxy.output_dir = output
    proxy.extra_headers = {"X-Route": "pool-a"}
    proxy.sequence = 0
    output.mkdir()
    proxy_thread = _start(proxy)
    try:
        request = Request(
            f"http://127.0.0.1:{proxy.server_port}/chat/completions",
            data=b'{"model":"test","messages":[]}',
            headers={"Authorization": "Bearer client-placeholder", "X-Trace-ID": "trace-1"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert response.read() == b'{"ok":true}'
    finally:
        proxy.shutdown()
        target.shutdown()
        proxy_thread.join(timeout=2)
        target_thread.join(timeout=2)

    assert TargetHandler.authorization == "Bearer runtime-secret"
    assert TargetHandler.body == b'{"model":"test","messages":[]}'
    request_meta = json.loads(next((output / "requests").glob("*.json")).read_text())
    response_meta_path = next((output / "responses").glob("*.json"))
    response_meta = json.loads(response_meta_path.read_text())
    assert request_meta["headers"]["Authorization"] == "[REDACTED]"
    assert Path(request_meta["body_path"]).read_bytes() == b'{"model":"test","messages":[]}'
    assert next(
        value
        for key, value in request_meta["identity_headers"].items()
        if key.lower() == "x-trace-id"
    ) == "trace-1"
    assert response_meta["status"] == 200
    assert next(
        value
        for key, value in response_meta["identity_headers"].items()
        if key.lower() == "x-request-id"
    ) == "upstream-request-7"
    assert next(
        value
        for key, value in response_meta["identity_headers"].items()
        if key.lower() == "x-replica-rank"
    ) == "replica-2/rank-1"
    assert Path(response_meta["body_path"]).read_bytes() == b'{"ok":true}'
    for path in output.rglob("*"):
        if path.is_file():
            assert "runtime-secret" not in path.read_text(encoding="utf-8", errors="replace")


def test_request_proxy_does_not_duplicate_v1_endpoint_prefix(tmp_path: Path) -> None:
    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = _start(target)
    output = tmp_path / "request-evidence"
    proxy = Recorder(("127.0.0.1", 0), Handler)
    proxy.target = f"http://127.0.0.1:{target.server_port}/v1"
    proxy.api_key = "runtime-secret"
    proxy.output_dir = output
    proxy.extra_headers = {}
    proxy.sequence = 0
    output.mkdir()
    proxy_thread = _start(proxy)
    try:
        request = Request(
            f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions",
            data=b"{}",
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        proxy.shutdown()
        target.shutdown()
        proxy_thread.join(timeout=2)
        target_thread.join(timeout=2)
    assert TargetHandler.request_path == "/v1/chat/completions"
