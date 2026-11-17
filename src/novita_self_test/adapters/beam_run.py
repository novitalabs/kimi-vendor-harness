from __future__ import annotations

import argparse
import gzip
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from novita_self_test.stability import (
    CREDENTIAL_INVALID,
    failure_class_counts,
    service_invalid_reason,
)


def row_key(row: dict[str, Any]) -> str:
    return f"{row.get('chat_id')}|{row.get('question_type')}|{row.get('question_index')}"


def is_terminal_empty_response(row: dict[str, Any]) -> bool:
    response = row.get("response")
    return (
        not row.get("error")
        and isinstance(response, str)
        and not response.strip()
        and row.get("finish_reason") in {"length", "stop"}
    )


def is_unjudged_zero_response(row: dict[str, Any]) -> bool:
    """Official judge guard: only non-abstention empties score zero unjudged.

    ``beam_judge.py`` returns 0.0 without calling the model for an empty
    non-abstention response, but an empty or refusing abstention response may be
    correct, so it must still reach the LLM judge.
    """
    return is_terminal_empty_response(row) and str(row.get("question_type")) != "abstention"


def is_generation_error(row: dict[str, Any]) -> bool:
    response = row.get("response")
    return bool(row.get("error")) or not isinstance(response, str) or (
        not response.strip() and not is_terminal_empty_response(row)
    )


def invoke(command: list[str], *, env: dict[str, str]) -> None:
    process = subprocess.run(command, env={**os.environ, **env}, text=True, check=False)
    if process.returncode:
        raise RuntimeError(f"BEAM command failed with exit {process.returncode}")


def write_blocked_result(
    output_dir: Path, *, reason: str, summary: str, expected_trials: int
) -> None:
    result: dict[str, Any] = {
        "schema_version": 1,
        "suite_id": "beam-1m",
        "row_ids": [],
        "status": "blocked",
        "validity": "execution_invalid",
        "counts": {},
        "metrics": {},
        "redacted_command": [],
        "artifact_paths": [],
        "failure_summary": summary,
        "runner_fingerprint": {
            "runner": "KVV/beam",
            "questions": f"beam-1m-{expected_trials}",
        },
        "coverage": {},
        "reason": reason,
    }
    (output_dir / "adapter-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_generation_invalid_result(
    output_dir: Path,
    *,
    expected_trials: int,
    answers: list[dict[str, Any]],
    retry_artifacts: list[str] | None = None,
    retry_attempts: dict[str, int] | None = None,
    retry_max: int = 3,
) -> None:
    invalid = [row for row in answers if is_generation_error(row)]
    failure_texts = [str(row.get("error") or "empty generation response") for row in invalid]
    service_reason = service_invalid_reason(failure_texts)
    status = "blocked" if service_reason is not None else "failed"
    validity = "service_invalid" if service_reason is not None else "execution_invalid"
    reason = service_reason or "beam_generation_invalid"
    result = {
        "schema_version": 1,
        "suite_id": "beam-1m",
        "row_ids": [],
        "status": status,
        "validity": validity,
        "counts": {
            "total": len(answers),
            "completed": len(answers) - len(invalid),
            "errored": len(invalid),
        },
        "metrics": {},
        "redacted_command": [],
        "artifact_paths": list(
            dict.fromkeys(
                [str((output_dir / "answers.jsonl").resolve())]
                + list(retry_artifacts or [])
            )
        ),
        "failure_summary": (
            f"BEAM generation produced {len(invalid)} invalid answers; "
            "invalid generations must be rerun before judging"
        ),
        "runner_fingerprint": {
            "runner": "KVV/beam",
            "questions": f"beam-1m-{expected_trials}",
        },
        "coverage": {
            "observed_trials": len(answers),
            "invalid_generation_keys": [
                f"{row.get('chat_id')}:{row.get('question_type')}:{row.get('question_index')}"
                for row in invalid
            ],
            "failure_class_counts": failure_class_counts(failure_texts),
            "task_retry_attempts": retry_attempts or {},
            "task_retry_max": retry_max,
        },
        "reason": reason,
    }
    (output_dir / "adapter-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_generation_pending_result(
    output_dir: Path,
    *,
    expected_trials: int,
    answers: list[dict[str, Any]],
    retry_artifacts: list[str] | None = None,
    retry_attempts: dict[str, int] | None = None,
    retry_max: int = 3,
) -> None:
    """Checkpoint generation so acceptance can run judge work in parallel."""
    result = {
        "schema_version": 1,
        "suite_id": "beam-1m",
        "row_ids": [],
        "status": "deferred",
        "validity": "valid",
        "counts": {"total": len(answers), "completed": len(answers), "errored": 0},
        "metrics": {},
        "redacted_command": [],
        "artifact_paths": list(
            dict.fromkeys(
                [str((output_dir / "answers.jsonl").resolve())]
                + list(retry_artifacts or [])
            )
        ),
        "failure_summary": None,
        "runner_fingerprint": {
            "runner": "KVV/beam",
            "questions": f"beam-1m-{expected_trials}",
            "phase": "generation",
        },
        "coverage": {
            "observed_trials": len(answers),
            "generation_complete": True,
            "background_judge_pending": True,
            "task_retry_attempts": retry_attempts or {},
            "task_retry_max": retry_max,
        },
        "reason": "beam_judge_pending",
    }
    (output_dir / "adapter-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_answers(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _write_filtered_generation_inputs(
    source_chats: Path,
    source_questions: Path,
    target_keys: set[str],
    output_dir: Path,
) -> tuple[Path, Path]:
    chats_by_id: dict[str, dict[str, Any]] = {}
    with gzip.open(source_chats, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                chats_by_id[str(record["chat_id"])] = record
    selected_questions: list[dict[str, Any]] = []
    selected_chat_ids: set[str] = set()
    with source_questions.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            question = json.loads(line)
            key = f"{question['chat_id']}|{question['question_type']}|{question['question_index']}"
            if key in target_keys:
                selected_questions.append(question)
                selected_chat_ids.add(str(question["chat_id"]))
    chats_path = output_dir / "chats.jsonl.gz"
    questions_path = output_dir / "questions.jsonl"
    with gzip.open(chats_path, "wt", encoding="utf-8") as handle:
        for chat_id in sorted(selected_chat_ids):
            handle.write(json.dumps(chats_by_id[chat_id], ensure_ascii=False) + "\n")
    questions_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in selected_questions),
        encoding="utf-8",
    )
    return chats_path, questions_path


def retry_invalid_generations(
    *,
    output_dir: Path,
    answers_path: Path,
    source_chats: Path | None,
    source_questions: Path | None,
    python: str,
    kvv_root: Path,
    model: str,
    base_url: str,
    concurrency: int,
    task_retries: int,
    request_max_retries: int,
    generation_timeout: float,
    max_context_tokens: int,
    env: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str], dict[str, int]]:
    answers = _read_answers(answers_path)
    retry_root = output_dir / "generation-retries"
    retry_summary = output_dir / "generation-retry-summary.json"
    if task_retries <= 0 or source_chats is None or source_questions is None:
        if retry_summary.is_file():
            # The judge phase re-invokes this module with --reuse-answers, which
            # clears both sources and would otherwise overwrite the generation
            # phase's real wave counters with zeros.
            return answers, [str(retry_summary.resolve())], {}
        retry_summary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "waves": 0,
                    "task_retry_max": task_retries,
                    "task_retry_attempts": {},
                    "remaining_invalid_keys": sorted(
                        row_key(row) for row in answers if is_generation_error(row)
                    ),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return answers, [str(retry_summary.resolve())], {}
    summary: dict[str, Any] = {}
    if retry_summary.is_file():
        try:
            summary = json.loads(retry_summary.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = {}
    retry_artifacts = [
        str(path.resolve()) for path in sorted(retry_root.rglob("*")) if path.is_file()
    ]
    retry_attempts = {
        str(key): int(value)
        for key, value in dict(summary.get("task_retry_attempts") or {}).items()
    }
    wave = int(summary.get("waves") or 0)
    by_key = {row_key(row): row for row in answers}
    while True:
        invalid = [row for row in by_key.values() if is_generation_error(row)]
        target_keys = {
            row_key(row)
            for row in invalid
            if retry_attempts.get(row_key(row), 0) < task_retries
        }
        if not target_keys:
            break
        service_reason = service_invalid_reason(
            [str(row.get("error") or "empty generation response") for row in invalid]
        )
        if service_reason is not None:
            break
        wave += 1
        retry_dir = retry_root / f"wave-{wave:02d}"
        retry_dir.mkdir(parents=True, exist_ok=True)
        for key in target_keys:
            retry_attempts[key] = retry_attempts.get(key, 0) + 1
        chats_path, questions_path = _write_filtered_generation_inputs(
            source_chats, source_questions, target_keys, retry_dir
        )
        retry_output = retry_dir / "answers.jsonl"
        invoke(
            [
                python,
                "-m",
                "novita_self_test.run_with_headers",
                str(kvv_root / "beam" / "beam_generate.py"),
                "--model",
                model,
                "--base-url",
                base_url,
                "--output",
                str(retry_output),
                "--chats-file",
                str(chats_path),
                "--questions-file",
                str(questions_path),
                "--thinking-json",
                '{"thinking":{"type":"enabled","keep":"all","effort":"max"}}',
                "--tokenizer",
                str(kvv_root / "beam" / "tokenizer"),
                "--concurrency",
                str(min(concurrency, len(target_keys))),
                "--max-context-tokens",
                str(max_context_tokens),
                "--max-retries",
                str(request_max_retries),
                "--timeout",
                str(generation_timeout),
            ],
            env=env,
        )
        retry_rows = _read_answers(retry_output) if retry_output.is_file() else []
        for path in (chats_path, questions_path, retry_output):
            if path.is_file():
                retry_artifacts.append(str(path.resolve()))
        for retry_row in retry_rows:
            key = row_key(retry_row)
            if key in by_key:
                by_key[key] = retry_row
        answers_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in by_key.values()),
            encoding="utf-8",
        )
        remaining = sorted(key for key, row in by_key.items() if is_generation_error(row))
        retry_summary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "waves": wave,
                    "task_retry_max": task_retries,
                    "task_retry_attempts": retry_attempts,
                    "remaining_invalid_keys": remaining,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if str(retry_summary.resolve()) not in retry_artifacts:
            retry_artifacts.append(str(retry_summary.resolve()))
    remaining = sorted(key for key, row in by_key.items() if is_generation_error(row))
    retry_summary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "waves": wave,
                "task_retry_max": task_retries,
                "task_retry_attempts": retry_attempts,
                "remaining_invalid_keys": remaining,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if str(retry_summary.resolve()) not in retry_artifacts:
        retry_artifacts.append(str(retry_summary.resolve()))
    return list(by_key.values()), retry_artifacts, retry_attempts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--kvv-root", required=True, type=Path)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--expected-trials", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=256)
    parser.add_argument("--max-context-tokens", type=int, required=True)
    parser.add_argument("--judge-requests-per-minute", type=int, default=300)
    parser.add_argument("--generation-max-retries", type=int, default=8)
    parser.add_argument("--generation-task-retries", type=int, default=3)
    parser.add_argument("--generation-timeout", type=float, default=3600)
    parser.add_argument("--judge-concurrency", type=int, default=64)
    parser.add_argument("--judge-max-retries", type=int, default=8)
    parser.add_argument("--judge-temperature", type=float, default=1.0)
    parser.add_argument("--judge-timeout", type=float, default=600)
    parser.add_argument("--reuse-answers", action="store_true")
    parser.add_argument("--generation-only", action="store_true")
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.max_context_tokens < 1:
        parser.error("--max-context-tokens must be positive")
    if args.generation_max_retries < 1 or args.judge_max_retries < 1:
        parser.error("generation/judge max retries must be positive")
    if args.generation_task_retries < 0:
        parser.error("generation task retries must be non-negative")
    if args.generation_timeout <= 0 or args.judge_timeout <= 0:
        parser.error("generation/judge timeout must be positive")
    if args.judge_concurrency < 1:
        parser.error("--judge-concurrency must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    judge_url = os.environ.get("NVST_BEAM_JUDGE_BASE_URL")
    judge_model = os.environ.get("NVST_BEAM_JUDGE_MODEL")
    judge_env = os.environ.get("NVST_BEAM_JUDGE_API_KEY_ENV")
    judge_key = os.environ.get(judge_env or "")
    if not judge_url or not judge_model or not judge_env or not judge_key:
        write_blocked_result(
            args.output_dir,
            reason="beam_judge_contract_missing",
            summary=(
                "BEAM judge requires NVST_BEAM_JUDGE_BASE_URL, "
                "NVST_BEAM_JUDGE_MODEL, and a populated "
                "NVST_BEAM_JUDGE_API_KEY_ENV"
            ),
            expected_trials=args.expected_trials,
        )
        return 2
    vendor_key = os.environ.get("KIMI_API_KEY")
    if not args.reuse_answers and not vendor_key:
        raise RuntimeError("KIMI_API_KEY is unset")
    answers = args.output_dir / "answers.jsonl"
    scores = args.output_dir / "scores.jsonl"
    judge_answers = args.output_dir / "judge-answers.jsonl"
    judge_scores = args.output_dir / "judge-scores.jsonl"
    env = {"JUDGE_API_KEY": judge_key}
    if vendor_key:
        env["BEAM_API_KEY"] = vendor_key
    if args.reuse_answers:
        if not answers.is_file():
            raise RuntimeError(f"BEAM answers file does not exist: {answers}")
    else:
        fallback = (
            Path(__file__).resolve().parents[3]
            / "suites"
            / "novita-api"
            / "resources"
            / "beam-1m"
        )
        invoke(
            [
                args.python,
                "-m",
                "novita_self_test.run_with_headers",
                str(args.kvv_root / "beam" / "beam_generate.py"),
                "--model",
                os.environ["KIMI_MODEL"],
                "--base-url",
                os.environ["KIMI_BASE_URL"],
                "--output",
                str(answers),
                "--chats-file",
                str(fallback / "beam_1m_chats.jsonl.gz"),
                "--questions-file",
                str(fallback / "beam_1m_questions.jsonl"),
                "--thinking-json",
                '{"thinking":{"type":"enabled","keep":"all","effort":"max"}}',
                "--tokenizer",
                str(args.kvv_root / "beam" / "tokenizer"),
                "--concurrency",
                str(args.concurrency),
                "--max-context-tokens",
                str(args.max_context_tokens),
                "--max-retries",
                str(args.generation_max_retries),
                "--timeout",
                str(args.generation_timeout),
            ],
            env=env,
        )
    source_chats = fallback / "beam_1m_chats.jsonl.gz" if not args.reuse_answers else None
    source_questions = fallback / "beam_1m_questions.jsonl" if not args.reuse_answers else None
    answer_rows, retry_artifacts, retry_attempts = retry_invalid_generations(
        output_dir=args.output_dir,
        answers_path=answers,
        source_chats=source_chats,
        source_questions=source_questions,
        python=args.python,
        kvv_root=args.kvv_root,
        model=os.environ.get("KIMI_MODEL", ""),
        base_url=os.environ.get("KIMI_BASE_URL", ""),
        concurrency=args.concurrency,
        task_retries=args.generation_task_retries,
        request_max_retries=args.generation_max_retries,
        generation_timeout=args.generation_timeout,
        max_context_tokens=args.max_context_tokens,
        env=env,
    )
    generation_errors = [row for row in answer_rows if is_generation_error(row)]
    zero_scored_answers = [row for row in answer_rows if is_unjudged_zero_response(row)]
    if len(answer_rows) != args.expected_trials or generation_errors:
        write_generation_invalid_result(
            args.output_dir,
            expected_trials=args.expected_trials,
            answers=answer_rows,
            retry_artifacts=retry_artifacts,
            retry_attempts=retry_attempts,
            retry_max=args.generation_task_retries,
        )
        return 2

    if args.generation_only:
        write_generation_pending_result(
            args.output_dir,
            expected_trials=args.expected_trials,
            answers=answer_rows,
            retry_artifacts=retry_artifacts,
            retry_attempts=retry_attempts,
            retry_max=args.generation_task_retries,
        )
        return 0

    judge_rows = [row for row in answer_rows if not is_unjudged_zero_response(row)]
    judge_answers.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in judge_rows),
        encoding="utf-8",
    )
    invoke(
        [
            sys.executable,
            "-m",
            "novita_self_test.adapters.beam_judge_run",
            "--answers",
            str(judge_answers),
            "--output",
            str(judge_scores),
            "--official-judge",
            str(args.kvv_root / "beam" / "beam_judge.py"),
            "--judge-model",
            judge_model,
            "--judge-base-url",
            judge_url,
            "--judge-temperature",
            str(args.judge_temperature),
            "--requests-per-minute",
            str(args.judge_requests_per_minute),
            "--concurrency",
            str(args.judge_concurrency),
            "--max-retries",
            str(args.judge_max_retries),
            "--timeout",
            str(args.judge_timeout),
        ],
        env=env,
    )
    judged_by_key: dict[str, float] = {}
    judge_errors: dict[str, str] = {}
    if judge_scores.is_file():
        for line in judge_scores.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            row = json.loads(line)
            key = row_key(row)
            if isinstance(row.get("score"), (int, float)):
                judged_by_key[key] = float(row["score"])
                judge_errors.pop(key, None)
            elif row.get("error"):
                judge_errors[key] = str(row.get("error_class") or "explicit_failure")

    zero_scored_keys = {row_key(row) for row in zero_scored_answers}
    score_rows: list[dict[str, Any]] = []
    for answer in answer_rows:
        key = row_key(answer)
        score = 0.0 if key in zero_scored_keys else judged_by_key.get(key)
        if not isinstance(score, (int, float)):
            continue
        score_rows.append(
            {
                "chat_id": answer["chat_id"],
                "question_type": answer["question_type"],
                "question_index": answer["question_index"],
                "score": float(score),
            }
        )
    scores.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in score_rows),
        encoding="utf-8",
    )
    complete = (
        len(score_rows) == args.expected_trials
        and len(judged_by_key) == len(judge_rows)
    )
    score = (
        sum(float(row["score"]) for row in score_rows) / args.expected_trials
        if complete
        else None
    )
    passed = complete and score is not None and (
        args.min_score is None or score >= args.min_score
    )
    result = {
        "schema_version": 1,
        "suite_id": "beam-1m",
        "row_ids": [],
        "status": "passed" if passed else "failed",
        "validity": "valid" if complete else "execution_invalid",
        "counts": {
            "total": len(answer_rows),
            "scored": len(score_rows),
            "judged": len(judge_rows),
            "zero_scored": len(zero_scored_answers),
            "judge_errors": len(judge_errors),
            "remaining": len(judge_rows) - len(judged_by_key),
        },
        "metrics": {"score": score},
        "redacted_command": [],
        "artifact_paths": [
            str(answers.resolve()),
            str(judge_answers.resolve()),
            str(judge_scores.resolve()),
            str(scores.resolve()),
        ],
        "failure_summary": None if passed else (
            (
                f"BEAM judge is incomplete: {len(judged_by_key)}/{len(judge_rows)} "
                f"responses scored, {len(judge_errors)} judge errors"
            )
            if not complete
            else f"score {score} is below {args.min_score}"
        ),
        "runner_fingerprint": {
            "runner": "KVV/beam",
            "questions": f"beam-1m-{args.expected_trials}",
            "terminal_empty_response": "score_zero_except_abstention",
            "generation_truncation_tokenizer": "kvv/beam/tokenizer",
            "judge_requests_per_minute": str(args.judge_requests_per_minute),
            "judge_retryable_errors": "execution_invalid",
        },
        "coverage": {
            "observed_trials": len(answer_rows),
            "judged_trials": len(judge_rows),
            "zero_scored_trials": len(zero_scored_answers),
            "judge_error_keys": sorted(judge_errors),
            "judge_failure_class_counts": dict(Counter(judge_errors.values())),
            "zero_scored_generation_keys": [
                f"{row.get('chat_id')}:{row.get('question_type')}:{row.get('question_index')}"
                for row in zero_scored_answers
            ],
        },
        "reason": None if passed else (
            "beam_judge_incomplete" if not complete else "benchmark_below_threshold"
        ),
    }
    (args.output_dir / "adapter-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
