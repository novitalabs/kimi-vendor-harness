from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .config import ServiceAccess
from .response_artifacts import safe_response_headers
from .stability import (
    CREDENTIAL_INVALID,
    QUOTA_EXHAUSTED,
    RATE_LIMITED,
    SERVICE_UNAVAILABLE,
    classify_failure,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_service(service: ServiceAccess, *, timeout: float = 20) -> dict[str, Any]:
    started_at = _now()
    hostname = (urlsplit(service.base_url).hostname or "").lower()
    if hostname.endswith((".example", ".invalid")):
        return {
            "schema_version": 1,
            "status": "passed",
            "validity": "valid",
            "reason": "reserved_example_endpoint",
            "http_status": None,
            "response_headers": {},
            "started_at": started_at,
            "finished_at": _now(),
        }

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {service.credential()}",
        **service.headers,
    }
    request = urllib.request.Request(
        f"{service.base_url.rstrip('/')}/models",
        headers=headers,
        method="GET",
    )
    response_headers: dict[str, str] = {}
    listed_models: set[str] | None = None
    response_body = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            response_headers = safe_response_headers(dict(response.headers.items()))
            response_body = response.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(response_body)
                listed_models = {
                    str(item["id"])
                    for item in payload.get("data", [])
                    if isinstance(item, dict) and item.get("id")
                }
            except (json.JSONDecodeError, AttributeError, TypeError):
                listed_models = None
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        response_headers = safe_response_headers(dict(exc.headers.items()))
        response_body = exc.read().decode("utf-8", errors="replace")[:4096]
        listed_models = None
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        return {
            "schema_version": 1,
            "status": "blocked",
            "validity": "service_invalid",
            "reason": "endpoint_unreachable",
            "http_status": None,
            "response_headers": {},
            "failure_summary": f"{type(exc).__name__}: endpoint probe failed",
            "started_at": started_at,
            "finished_at": _now(),
        }

    failure_class = classify_failure(f"HTTP {status} {response_body}")
    if failure_class in {QUOTA_EXHAUSTED, CREDENTIAL_INVALID}:
        outcome = ("blocked", "service_invalid", failure_class)
    elif status == 429 or failure_class == RATE_LIMITED:
        outcome = ("blocked", "service_invalid", "rate_limited")
    elif 500 <= status <= 599 or failure_class == SERVICE_UNAVAILABLE:
        outcome = ("blocked", "service_invalid", "service_unavailable")
    elif 200 <= status <= 299 and listed_models is not None and service.served_model not in listed_models:
        # A gateway may expose a chat-completions alias while /models lists only
        # its canonical model ID. The endpoint probe must not reject that valid
        # routing contract; the selected model is validated by the actual suite.
        outcome = ("passed", "valid", "served_model_catalog_alias")
    elif 200 <= status <= 299:
        outcome = ("passed", "valid", None)
    else:
        outcome = ("passed", "valid", "models_probe_inconclusive")

    if outcome[0] == "passed":
        chat_payload = json.dumps(
            {
                "model": service.served_model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
                "stream": False,
            }
        ).encode("utf-8")
        chat_request = urllib.request.Request(
            f"{service.base_url.rstrip('/')}/chat/completions",
            data=chat_payload,
            headers={**headers, "Content-Type": "application/json"},
            method="POST",
        )
        chat_body = ""
        try:
            with urllib.request.urlopen(chat_request, timeout=timeout) as response:
                chat_status = int(response.status)
                chat_headers = safe_response_headers(dict(response.headers.items()))
                chat_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            chat_status = int(exc.code)
            chat_headers = safe_response_headers(dict(exc.headers.items()))
            chat_body = exc.read().decode("utf-8", errors="replace")[:4096]
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            return {
                "schema_version": 1,
                "status": "blocked",
                "validity": "service_invalid",
                "reason": "endpoint_unreachable",
                "http_status": None,
                "response_headers": {},
                "failure_summary": f"{type(exc).__name__}: chat preflight failed",
                "started_at": started_at,
                "finished_at": _now(),
            }
        failure_class = classify_failure(f"HTTP {chat_status} {chat_body}")
        if failure_class in {QUOTA_EXHAUSTED, CREDENTIAL_INVALID}:
            outcome = ("blocked", "service_invalid", failure_class)
        elif failure_class == RATE_LIMITED or chat_status == 429:
            outcome = ("blocked", "service_invalid", "rate_limited")
        elif failure_class == SERVICE_UNAVAILABLE or 500 <= chat_status <= 599:
            outcome = ("blocked", "service_invalid", "service_unavailable")
        elif not 200 <= chat_status <= 299:
            outcome = ("blocked", "service_invalid", "chat_probe_failed")
        if outcome[0] == "blocked":
            status = chat_status
            response_headers = chat_headers
    else:
        chat_status = None

    return {
        "schema_version": 1,
        "status": outcome[0],
        "validity": outcome[1],
        "reason": outcome[2],
        "http_status": status,
        "chat_http_status": chat_status,
        "served_model_listed": None
        if listed_models is None
        else service.served_model in listed_models,
        "failure_summary": None
        if outcome[0] == "passed"
        else f"service preflight returned HTTP {status}",
        "model_catalog_alias_mismatch": bool(
            listed_models is not None and service.served_model not in listed_models
        ),
        "started_at": started_at,
        "finished_at": _now(),
    }
