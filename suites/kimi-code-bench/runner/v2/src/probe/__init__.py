"""Credential-safe OpenAI-compatible endpoint probe."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class ProbeCase:
    name: str
    expected: int
    actual: int


def _request(url: str, key: str, body: dict[str, Any] | None = None, extra_headers: dict[str, str] | None = None) -> int:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", **(extra_headers or {})}
    req = urllib.request.Request(url, headers=headers, method="POST" if body else "GET", data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def probe_endpoint(base_url: str, api_key: str, model: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    root = base_url.rstrip("/")
    cases = [ProbeCase("models", 200, _request(root + "/models", api_key, extra_headers=headers))]
    cases.append(ProbeCase("chat", 200, _request(root + "/chat/completions", api_key, {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}, headers)))
    return {"base_url": root, "model": model, "passed": all(c.actual == c.expected for c in cases), "cases": [asdict(c) for c in cases]}
