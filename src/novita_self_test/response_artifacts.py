from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

import httpx


SAFE_RESPONSE_HEADERS = {
    "content-type",
    "date",
    "server",
    "traceparent",
    "x-msh-providerdeployid",
    "x-msh-usage-cached-tokens",
    "x-msh-usage-prompt-tokens",
    "x-request-id",
    "x-trace-id",
}
_CREDENTIAL_RE = re.compile(
    rb"(?i)(authorization|proxy-authorization|cookie)\s*[:=]\s*[^\r\n]+|"
    rb"bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9_-]{8,}"
)


def safe_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in SAFE_RESPONSE_HEADERS
    }


def write_response_artifacts(
    artifact_root: Path,
    *,
    group: str,
    case_id: str,
    response: httpx.Response,
    api_key: str,
) -> list[Path]:
    case_dir = artifact_root / group / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    body = response.content
    if api_key:
        body = body.replace(api_key.encode("utf-8"), b"[REDACTED]")
    body = _CREDENTIAL_RE.sub(b"[REDACTED]", body)

    body_path = case_dir / "response.body"
    headers_path = case_dir / "response.headers.json"
    metadata_path = case_dir / "response.meta.json"
    body_path.write_bytes(body)
    headers_path.write_text(
        json.dumps(safe_response_headers(dict(response.headers)), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    metadata_path.write_text(
        json.dumps(
            {
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "case_id": case_id,
                "status_code": response.status_code,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return [body_path, headers_path, metadata_path]
