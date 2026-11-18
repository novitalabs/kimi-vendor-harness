from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

QUOTA_EXHAUSTED = "quota_exhausted"
CREDENTIAL_INVALID = "credential_invalid"
RATE_LIMITED = "rate_limited"
ENGINE_OVERLOADED = "engine_overloaded"
SERVICE_UNAVAILABLE = "service_unavailable"
TIMEOUT = "timeout"
CONNECTION_ERROR = "connection_error"
EXPLICIT_FAILURE = "explicit_failure"

_QUOTA_RE = re.compile(
    r"(?i)not enough (?:member )?(?:budget|balance)|"
    r"insufficient (?:quota|credits|balance)|quota exhausted|billing quota"
)
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:\bHTTP(?:StatusError)?\b[^\n]{0,80}\b(?:401|403|407)\b|"
    r"\b(?:status|status_code|code)\s*[:=]\s*(?:401|403|407)\b|"
    r"\b(?:401|403|407)\s+(?:Forbidden|Unauthorized)\b|"
    r"authentication[_ ](?:error|failed)|invalid api[_ -]?key|FAILED_TO_AUTH)"
)
_RATE_LIMIT_RE = re.compile(
    r"(?i)HTTP(?:StatusError)?\s*429\b|Error code:\s*429\b|"
    r"\b429\b.*(?:rate|overload)|rate limit"
)
_OVERLOAD_RE = re.compile(r"(?i)engine_overloaded|server[_ ]overload|server[_ ]interrupted")
_SERVICE_RE = re.compile(
    r"(?i)HTTP(?:StatusError)?\s*5\d\d\b|Error code:\s*5\d\d\b|"
    r"service unavailable|bad gateway|gateway timeout"
)
_TIMEOUT_RE = re.compile(
    r"(?i)ConnectTimeout|ReadTimeout|TimeoutError|APITimeoutError|timed out"
)
_CONNECTION_RE = re.compile(
    r"(?i)ConnectError|APIConnectionError|RemoteProtocolError|Connection reset|"
    r"Connection refused|Connection error|Name or service not known|Temporary failure in name resolution|"
    r"nodename nor servname|certificate verify failed|Network is unreachable"
)

RETRYABLE_FAILURES = {
    RATE_LIMITED,
    ENGINE_OVERLOADED,
    SERVICE_UNAVAILABLE,
    TIMEOUT,
    CONNECTION_ERROR,
}


def classify_failure(text: str) -> str:
    if _QUOTA_RE.search(text):
        return QUOTA_EXHAUSTED
    if _CREDENTIAL_RE.search(text):
        return CREDENTIAL_INVALID
    if _RATE_LIMIT_RE.search(text):
        return RATE_LIMITED
    if _OVERLOAD_RE.search(text):
        return ENGINE_OVERLOADED
    if _SERVICE_RE.search(text):
        return SERVICE_UNAVAILABLE
    if _TIMEOUT_RE.search(text):
        return TIMEOUT
    if _CONNECTION_RE.search(text):
        return CONNECTION_ERROR
    return EXPLICIT_FAILURE


def is_retryable_failure(text: str) -> bool:
    return classify_failure(text) in RETRYABLE_FAILURES


def service_invalid_reason(texts: Iterable[str]) -> str | None:
    classes = {classify_failure(text) for text in texts}
    if QUOTA_EXHAUSTED in classes:
        return QUOTA_EXHAUSTED
    if CREDENTIAL_INVALID in classes:
        return CREDENTIAL_INVALID
    return None


def failure_class_counts(texts: Iterable[str]) -> dict[str, int]:
    return dict(Counter(classify_failure(text) for text in texts))
