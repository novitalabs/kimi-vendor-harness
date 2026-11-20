from __future__ import annotations

from novita_self_test.config import find_secret, redact


def test_case_values_cannot_contain_credentials() -> None:
    assert find_secret({"headers": {"Authorization": "Bearer secret-token-value"}})
    assert "[REDACTED]" in redact("Authorization: Bearer secret-token-value", "secret-token-value")


def test_redaction_covers_secret_dictionary_keys_and_nested_values() -> None:
    import json
    key = "synthetic-review-key"
    result = redact({key: [{"Authorization": "Bearer " + key}, {"echo": key}]}, key)
    assert key not in json.dumps(result)
    assert result["[REDACTED]"][0]["Authorization"] == "[REDACTED]"
