"""Secret-safe per-trial OpenAI-compatible request recorder for DeepSWE."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}
IDENTITY_HEADER_PARTS = ("request", "trace", "replica", "rank", "route", "engine", "pod")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: "[REDACTED]"
        if key.lower() in SENSITIVE_HEADERS
        or any(part in key.lower() for part in ("authorization", "api-key", "token"))
        else value
        for key, value in headers.items()
    }


def _identity_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if any(part in key.lower() for part in IDENTITY_HEADER_PARTS)
        and key.lower() not in SENSITIVE_HEADERS
    }


def _identity_values(headers: dict[str, str]) -> dict[str, str]:
    """Return identity headers under stable, easy-to-query names."""
    return _identity_headers(headers)


class Recorder(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server: Recorder

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/__health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self) -> None:
        self.server.sequence += 1
        request_id = f"{self.server.sequence:04d}-{uuid.uuid4().hex[:12]}"
        started = _now()
        body = self.rfile.read(int(self.headers.get("content-length", "0")))
        safe_headers = _safe_headers(dict(self.headers.items()))
        request_body_path = self.server.output_dir / "requests" / f"{request_id}.body"
        request_body_path.parent.mkdir(parents=True, exist_ok=True)
        request_body_path.write_bytes(body)
        request_record = {
            "request_id": request_id,
            "started_at": started,
            "method": "POST",
            "path": self.path,
            "headers": safe_headers,
            "identity_headers": _identity_headers(dict(self.headers.items())),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_path": str(request_body_path.resolve()),
            "body": _json_or_bytes(body),
        }
        _write_json(self.server.output_dir / "requests" / f"{request_id}.json", request_record)

        target_base = self.server.target.rstrip("/")
        request_path = self.path
        # Kimi Code is configured with the local proxy's `/v1` base URL while
        # the forwarded endpoint contract already ends in `/v1`. Avoid
        # producing `/v1/v1/...` when joining those two OpenAI-compatible URLs.
        if target_base.endswith("/v1") and request_path.startswith("/v1/"):
            request_path = request_path[3:]
        target = target_base + request_path
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "authorization"}
        }
        headers["Authorization"] = f"Bearer {self.server.api_key}"
        headers.update(self.server.extra_headers)
        upstream = urllib.request.Request(target, data=body, headers=headers, method="POST")
        response_body = bytearray()
        response_headers: dict[str, str] = {}
        status = 599
        error_text: str | None = None
        try:
            response = urllib.request.urlopen(upstream, timeout=900)
        except urllib.error.HTTPError as exc:
            response = exc
        except Exception as exc:  # pragma: no cover - exercised in live runtime
            error_text = f"{type(exc).__name__}: {exc}"
            self.send_error(502, "upstream request failed")
            _write_json(
                self.server.output_dir / "responses" / f"{request_id}.json",
                {"request_id": request_id, "status": status, "error": error_text},
            )
            return

        status = response.status
        response_headers = dict(response.headers.items())
        self.send_response(status)
        for key, value in response_headers.items():
            if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                self.send_header(key, value)
        self.end_headers()
        with response:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                response_body.extend(chunk)
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        identity = _identity_headers(response_headers)
        response_path = self.server.output_dir / "responses" / f"{request_id}.body"
        response_path.parent.mkdir(parents=True, exist_ok=True)
        response_path.write_bytes(response_body)
        _write_json(
            self.server.output_dir / "responses" / f"{request_id}.json",
            {
                "request_id": request_id,
                "finished_at": _now(),
                "status": status,
                "headers": _safe_headers(response_headers),
                "identity_headers": identity,
                "upstream_request_id": _identity_values(response_headers),
                "body_path": str(response_path.resolve()),
                "body_sha256": hashlib.sha256(response_body).hexdigest(),
                "body_bytes": len(response_body),
                "body": _json_or_bytes(bytes(response_body)),
                "upstream_error": error_text,
            },
        )


def _json_or_bytes(value: bytes):
    try:
        return json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"raw_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--extra-headers-json", default="{}")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not api_key:
        raise SystemExit("empty proxy api key")
    server = Recorder((args.host, args.port), Handler)
    server.target = args.target.rstrip("/")
    server.api_key = api_key
    server.output_dir = args.output_dir
    try:
        extra_headers = json.loads(args.extra_headers_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid extra headers JSON: {exc}") from exc
    if not isinstance(extra_headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in extra_headers.items()
    ):
        raise SystemExit("extra headers must be a JSON string mapping")
    server.extra_headers = {
        key: value
        for key, value in extra_headers.items()
        if key.lower() not in SENSITIVE_HEADERS
    }
    server.sequence = 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"host": args.host, "port": args.port, "target": server.target}), flush=True)
    server.serve_forever(poll_interval=0.2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
