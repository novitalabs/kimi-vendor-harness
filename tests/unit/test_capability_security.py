from __future__ import annotations

from novita_self_test.config import find_secret, redact


def test_case_values_cannot_contain_credentials() -> None:
    assert find_secret({"headers": {"Authorization": "Bearer secret-token-value"}})
    assert "[REDACTED]" in redact("Authorization: Bearer secret-token-value", "secret-token-value")
