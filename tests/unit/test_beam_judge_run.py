from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from novita_self_test.adapters import beam_judge_run


def _args(tmp_path: Path) -> argparse.Namespace:
    answers = tmp_path / "answers.jsonl"
    answers.write_text(
        '{"chat_id":"1","question_type":"knowledge_update",'
        '"question_index":0,"question":"q1","rubric":["r"],'
        '"response":"a1"}\n'
        '{"chat_id":"2","question_type":"summarization",'
        '"question_index":0,"question":"q2","rubric":["r"],'
        '"response":"a2"}\n',
        encoding="utf-8",
    )
    return argparse.Namespace(
        answers=answers,
        output=tmp_path / "scores.jsonl",
        official_judge=tmp_path / "official.py",
        judge_model="judge",
        judge_base_url="https://judge.example/v1",
        judge_api_key="secret",
        judge_reasoning_effort="",
        judge_max_tokens=16,
        judge_temperature=0.3,
        concurrency=2,
        requests_per_minute=60000,
        max_retries=2,
        timeout=1,
    )


def test_parse_score_rejects_unparseable_judge_output() -> None:
    assert beam_judge_run.parse_score('{"score": 1.0}', event_ordering=False) == 1.0
    assert beam_judge_run.parse_score('{"score": 0.5}', event_ordering=False) == 0.0
    assert beam_judge_run.parse_score("not json", event_ordering=False) is None


def test_judge_errors_are_checkpointed_but_not_counted_as_scores(
    tmp_path: Path, monkeypatch
) -> None:
    args = _args(tmp_path)
    monkeypatch.setattr(beam_judge_run, "load_official_judge", lambda _: SimpleNamespace())
    monkeypatch.setattr(beam_judge_run, "AsyncOpenAI", lambda **_: object())

    async def first_score(_client, _limiter, _args, _official, row):
        if row["chat_id"] == "2":
            raise beam_judge_run.JudgeCallError("Error code: 429 rate limit")
        return 1.0

    monkeypatch.setattr(beam_judge_run, "score_row", first_score)
    first = asyncio.run(beam_judge_run.run(args))

    assert first == {"total": 2, "scored": 1, "errors": 1, "remaining": 1}
    assert beam_judge_run.load_completed_scores(args.output) == {
        "1|knowledge_update|0": 1.0
    }
    records = [json.loads(line) for line in args.output.read_text().splitlines()]
    assert records[1]["score"] is None
    assert records[1]["error_class"] == "rate_limited"

    observed: list[str] = []

    async def resumed_score(_client, _limiter, _args, _official, row):
        observed.append(row["chat_id"])
        return 0.5

    monkeypatch.setattr(beam_judge_run, "score_row", resumed_score)
    resumed = asyncio.run(beam_judge_run.run(args))

    assert observed == ["2"]
    assert resumed == {"total": 2, "scored": 2, "errors": 0, "remaining": 0}
    assert beam_judge_run.load_completed_scores(args.output) == {
        "1|knowledge_update|0": 1.0,
        "2|summarization|0": 0.5,
    }
