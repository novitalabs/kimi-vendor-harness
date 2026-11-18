import json
from pathlib import Path


def _records(name: str) -> list[dict]:
    path = Path(__file__).parents[2] / "fixtures" / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_official_k3_fixture_counts_and_finish_reasons() -> None:
    image = _records("vendor-img-testcases-inhouse-3.jsonl")
    assert len(image) == 25
    assert {row["expected_finish_reason"] for row in image} == {"stop", "tool_calls"}


def test_official_image_fixture_covers_schema_and_message_roles() -> None:
    schemas = set()
    roles = set()
    max_images = 0
    for row in _records("vendor-img-testcases-inhouse-3.jsonl"):
        request = row["transformed_request"]
        for message in request["messages"]:
            roles.add(message["role"])
            content = message.get("content")
            if not isinstance(content, list):
                continue
            count = 0
            for part in content:
                if part.get("type") != "image_url":
                    continue
                count += 1
                value = part.get("image_url")
                schemas.add("string" if isinstance(value, str) else "object")
            max_images = max(max_images, count)
    assert schemas == {"string", "object"}
    assert {"user", "tool"}.issubset(roles)
    assert max_images > 1


def test_image_limit_probe_can_find_an_image_part() -> None:
    parts = (
        part
        for row in _records("vendor-img-testcases-inhouse-3.jsonl")
        for message in row["transformed_request"]["messages"]
        for part in (
            message.get("content") if isinstance(message.get("content"), list) else []
        )
    )
    assert any(part.get("type") == "image_url" for part in parts)
