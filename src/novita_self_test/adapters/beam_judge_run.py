from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from openai import AsyncOpenAI

from novita_self_test.artifacts import append_jsonl
from novita_self_test.events import emit_event
from novita_self_test.stability import classify_failure


class JudgeCallError(RuntimeError):
    pass


class RequestRateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self.interval = 60.0 / requests_per_minute
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            if delay:
                await asyncio.sleep(delay)
            self.next_at = max(now, self.next_at) + self.interval


def load_official_judge(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("nvst_official_beam_judge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load official BEAM judge: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row_key(row: dict[str, Any]) -> str:
    return f"{row['chat_id']}|{row['question_type']}|{row['question_index']}"


def load_completed_scores(path: Path) -> dict[str, float]:
    scores: dict[str, float] = {}
    if not path.is_file():
        return scores
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        score = row.get("score")
        if isinstance(score, (int, float)):
            scores[row_key(row)] = float(score)
    return scores


def retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def parse_score(response: str, *, event_ordering: bool) -> float | None:
    try:
        payload = json.loads(response)
        value = float(payload["score"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        match = re.search(r'"?score"?\s*:\s*([\d.]+)', response)
        if match is None:
            return None
        try:
            value = float(match.group(1))
        except ValueError:
            return None
    if event_ordering:
        return 1.0 if value >= 1.0 else 0.0
    return float(int(value))


async def call_judge(
    client: AsyncOpenAI,
    limiter: RequestRateLimiter,
    args: argparse.Namespace,
    prompt: str,
) -> str:
    kwargs: dict[str, Any] = {
        "model": args.judge_model,
        "messages": [{"role": "user", "content": prompt.strip()}],
        "temperature": args.judge_temperature,
        "max_tokens": args.judge_max_tokens,
    }
    if args.judge_reasoning_effort:
        kwargs["reasoning_effort"] = args.judge_reasoning_effort
    last_error: Exception | None = None
    for attempt in range(args.max_retries):
        await limiter.wait()
        try:
            completion = await client.chat.completions.create(
                **kwargs,
                timeout=args.timeout,
            )
            if not completion.choices:
                raise JudgeCallError("judge response has no choices")
            content = completion.choices[0].message.content
            if not isinstance(content, str) or not content.strip():
                raise JudgeCallError("judge response content is empty")
            return content
        except Exception as exc:  # Retry classification is recorded after exhaustion.
            last_error = exc
            if attempt + 1 >= args.max_retries:
                break
            retry_after = retry_after_seconds(exc)
            delay = retry_after if retry_after is not None else min(2**attempt, 60)
            run_dir = getattr(args, "_run_dir", None)
            if isinstance(run_dir, Path) and (run_dir / "run.json").is_file():
                emit_event(
                    run_dir,
                    "retry_scheduled",
                    suite_id="beam-1m",
                    phase="judge",
                    data={
                        "attempt": attempt + 1,
                        "error_class": classify_failure(f"{type(exc).__name__}: {exc}"),
                        "delay_seconds": delay,
                    },
                )
            await asyncio.sleep(delay)
    raise JudgeCallError(
        f"judge call failed after {args.max_retries} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    )


async def score_row(
    client: AsyncOpenAI,
    limiter: RequestRateLimiter,
    args: argparse.Namespace,
    official: ModuleType,
    row: dict[str, Any],
) -> float:
    response = row.get("response") or ""
    question_type = row["question_type"]
    rubric = row.get("rubric") or []
    if isinstance(rubric, str):
        try:
            rubric = json.loads(rubric)
        except json.JSONDecodeError:
            rubric = [rubric]
    if not rubric:
        rubric = ["The response should be relevant and correct."]
    if question_type == "event_ordering":
        reference = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(rubric))
        prompt = official.EVENT_ORDERING_JUDGE_TEMPLATE.format(
            question=row["question"],
            reference_order=reference,
            llm_response=response,
        )
        parsed = parse_score(
            await call_judge(client, limiter, args, prompt),
            event_ordering=True,
        )
        if parsed is None:
            raise JudgeCallError("judge response score is unparseable")
        return parsed
    scores = []
    for item in rubric:
        prompt = official.JUDGE_PROMPT_TEMPLATE.format(
            question=row["question"],
            rubric_item=item,
            llm_response=response,
        )
        parsed = parse_score(
            await call_judge(client, limiter, args, prompt),
            event_ordering=False,
        )
        if parsed is None:
            raise JudgeCallError("judge response score is unparseable")
        scores.append(parsed)
    return sum(scores) / len(scores)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    official = load_official_judge(args.official_judge)
    args._run_dir = args.output.parent.parents[1]
    rows = [
        json.loads(line)
        for line in args.answers.read_text(encoding="utf-8").splitlines()
        if line
    ]
    completed = load_completed_scores(args.output)
    pending = [row for row in rows if row_key(row) not in completed]
    client = AsyncOpenAI(
        base_url=args.judge_base_url.rstrip("/"),
        api_key=args.judge_api_key,
        max_retries=0,
    )
    limiter = RequestRateLimiter(args.requests_per_minute)
    semaphore = asyncio.Semaphore(args.concurrency)
    errors = 0

    async def worker(row: dict[str, Any]) -> None:
        nonlocal errors
        async with semaphore:
            try:
                score = await score_row(client, limiter, args, official, row)
            except Exception as exc:
                errors += 1
                text = f"{type(exc).__name__}: {exc}"
                record = {
                    "chat_id": row["chat_id"],
                    "question_type": row["question_type"],
                    "question_index": row["question_index"],
                    "score": None,
                    "error_class": classify_failure(text),
                    "error": text[:2000],
                }
            else:
                record = {
                    "chat_id": row["chat_id"],
                    "question_type": row["question_type"],
                    "question_index": row["question_index"],
                    "score": score,
                    "error_class": None,
                    "error": None,
                }
            append_jsonl(
                args.output,
                record,
                schema_name="beam-judge-checkpoint.schema.json",
            )
            run_dir = args.output.parent.parents[1]
            if (run_dir / "run.json").is_file():
                emit_event(
                    run_dir,
                    "checkpoint_written",
                    suite_id="beam-1m",
                    phase="judge",
                    data={
                        "key": row_key(row),
                        "score_present": isinstance(record.get("score"), (int, float)),
                        "error_class": record.get("error_class"),
                    },
                )

    await asyncio.gather(*(worker(row) for row in pending))
    final_scores = load_completed_scores(args.output)
    return {
        "total": len(rows),
        "scored": len(final_scores),
        "errors": errors,
        "remaining": len(rows) - len(final_scores),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Stable resumable BEAM judge")
    result.add_argument("--answers", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--official-judge", required=True, type=Path)
    result.add_argument("--judge-model", required=True)
    result.add_argument("--judge-base-url", required=True)
    result.add_argument("--judge-api-key", default=os.environ.get("JUDGE_API_KEY", ""))
    result.add_argument("--judge-reasoning-effort", default="")
    result.add_argument("--judge-max-tokens", type=int, default=16384)
    # 1.0 is the fixed sampling temperature for the k2 reasoning model family;
    # the upstream default of 0.3 only suits a non-thinking judge.
    result.add_argument("--judge-temperature", type=float, default=1.0)
    result.add_argument("--concurrency", type=int, default=64)
    result.add_argument("--requests-per-minute", type=int, required=True)
    result.add_argument("--max-retries", type=int, default=8)
    result.add_argument("--timeout", type=float, default=600)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.judge_api_key:
        raise RuntimeError("JUDGE_API_KEY is unset")
    summary = asyncio.run(run(args))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
