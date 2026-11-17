from __future__ import annotations

from novita_self_test.stability import (
    CONNECTION_ERROR,
    CREDENTIAL_INVALID,
    ENGINE_OVERLOADED,
    QUOTA_EXHAUSTED,
    RATE_LIMITED,
    SERVICE_UNAVAILABLE,
    TIMEOUT,
    classify_failure,
    failure_class_counts,
    is_retryable_failure,
    service_invalid_reason,
)


def test_stability_failure_classes_are_disjoint_and_ordered() -> None:
    assert classify_failure("HTTP 403 not enough balance") == QUOTA_EXHAUSTED
    assert classify_failure("HTTP 401 Unauthorized") == CREDENTIAL_INVALID
    assert classify_failure('{"data":[{"created":1784217407,"id":"kimi-k3"}]}') == "explicit_failure"
    assert classify_failure("Error code: 429 rate limit exceeded") == RATE_LIMITED
    assert classify_failure("finish_reason=engine_overloaded") == ENGINE_OVERLOADED
    assert classify_failure("HTTPStatusError 503 service unavailable") == SERVICE_UNAVAILABLE
    assert classify_failure("httpx.ReadTimeout: timed out") == TIMEOUT
    assert classify_failure("APIConnectionError: Network is unreachable") == CONNECTION_ERROR


def test_retryable_stability_failures_exclude_auth_and_quota() -> None:
    for text in (
        "Error code: 429 rate limit exceeded",
        "finish_reason=engine_overloaded",
        "HTTP 502 bad gateway",
        "ReadTimeout",
        "Connection reset",
    ):
        assert is_retryable_failure(text)
    assert not is_retryable_failure("HTTP 403 not enough balance")
    assert not is_retryable_failure("HTTP 401 unauthorized")


def test_service_invalid_reason_prioritizes_quota_then_credential() -> None:
    assert service_invalid_reason(["some pass", "HTTP 401 unauthorized"]) == CREDENTIAL_INVALID
    assert service_invalid_reason(
        ["HTTP 401 unauthorized", "HTTP 403 not enough member budget"]
    ) == QUOTA_EXHAUSTED
    assert service_invalid_reason(["HTTP 429 rate limit"]) is None
    assert failure_class_counts(
        ["HTTP 429 rate limit", "engine_overloaded", "HTTP 429 rate limit"]
    ) == {RATE_LIMITED: 2, ENGINE_OVERLOADED: 1}
