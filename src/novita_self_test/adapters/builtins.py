from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..artifacts import SuiteResult, enforce_completeness, utc_now, write_json
from ..config import ConfigError
from ..stability import (
    CREDENTIAL_INVALID,
    failure_class_counts,
    is_retryable_failure,
    service_invalid_reason,
)
from .base import Adapter, AdapterContext, CommandRunner, ProcessRunner

_SECRET_TEXT_RE = re.compile(
    r"(?i)(authorization|proxy-authorization|cookie)\s*[:=]\s*[^\r\n]+|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9_-]{8,}"
)


def _redact_text(value: str) -> str:
    return _SECRET_TEXT_RE.sub("[REDACTED]", value)


def _write_log(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_redact_text(value), encoding="utf-8")


def _format_tokens(tokens: list[Any], context: AdapterContext) -> list[str]:
    values = {
        "base_url": context.service.base_url,
        "served_model": context.service.served_model,
        "think_mode": context.service.think_mode,
        "run_dir": str(context.run_dir.resolve()),
        "suite_dir": str(context.suite_dir.resolve()),
        "repository_root": str(context.repository_root.resolve()),
    }
    return [str(token).format_map(values) for token in tokens]


def _resolve_cwd(context: AdapterContext) -> Path:
    configured = context.suite.config.get("cwd", ".")
    candidate = Path(str(configured))
    if not candidate.is_absolute():
        candidate = context.repository_root / candidate
    return candidate.resolve()


def _runner_fingerprint(context: AdapterContext) -> dict[str, str]:
    raw = context.suite.config.get("runner_fingerprint") or {}
    return {str(key): str(value) for key, value in raw.items()}


def _apply_concurrency_override(command: list[str], context: AdapterContext) -> list[str]:
    """Replace supported runner concurrency flags for an explicit run override."""
    override = context.concurrency_override
    if override is None:
        return command

    updated = list(command)
    flags = {"-n", "--max-connections", "--concurrency"}
    for index, token in enumerate(updated[:-1]):
        if token in flags:
            updated[index + 1] = str(override)
        elif token.startswith("--max-connections="):
            updated[index] = f"--max-connections={override}"
        elif token.startswith("--concurrency="):
            updated[index] = f"--concurrency={override}"
    return updated


class FakeAdapter(Adapter):
    def run(self, context: AdapterContext) -> SuiteResult:
        payload = dict(context.suite.config.get("fake_result") or {})
        return SuiteResult(
            suite_id=context.suite.suite_id,
            row_ids=list(context.suite.row_ids),
            status=str(payload.get("status", "passed")),
            validity=str(payload.get("validity", "valid")),
            counts=dict(payload.get("counts") or {}),
            metrics=dict(payload.get("metrics") or {}),
            artifact_paths=list(payload.get("artifact_paths") or []),
            failure_summary=payload.get("failure_summary"),
            runner_fingerprint=_runner_fingerprint(context),
            coverage=dict(payload.get("coverage") or {}),
            reason=payload.get("reason"),
            started_at=utc_now(),
            finished_at=utc_now(),
        )


class NotImplementedAdapter(Adapter):
    def run(self, context: AdapterContext) -> SuiteResult:
        return SuiteResult(
            suite_id=context.suite.suite_id,
            row_ids=list(context.suite.row_ids),
            status="not_implemented",
            reason=str(context.suite.config.get("reason") or "runner_not_implemented"),
            runner_fingerprint=_runner_fingerprint(context),
            started_at=utc_now(),
            finished_at=utc_now(),
        )


class CommandAdapter(Adapter):
    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or CommandRunner()

    def build_command(self, context: AdapterContext) -> list[str]:
        raw = context.suite.config.get("command")
        if not isinstance(raw, list) or not raw:
            raise ConfigError(
                "runner_contract_invalid", f"{context.suite.suite_id} requires config.command"
            )
        command = _format_tokens(raw, context)
        command.extend(context.selector_args)
        return _apply_concurrency_override(command, context)

    def run(self, context: AdapterContext) -> SuiteResult:
        context.suite_dir.mkdir(parents=True, exist_ok=True)
        started_at = utc_now()
        try:
            command = self.build_command(context)
            configured_env = {
                str(key): str(value).format_map(
                    {
                        "base_url": context.service.base_url,
                        "served_model": context.service.served_model,
                        "think_mode": context.service.think_mode,
                        "run_dir": str(context.run_dir.resolve()),
                        "suite_dir": str(context.suite_dir.resolve()),
                        "repository_root": str(context.repository_root.resolve()),
                    }
                )
                for key, value in (context.suite.config.get("env") or {}).items()
            }
            product_src = str((context.repository_root / "src").resolve())
            pythonpath = configured_env.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
            configured_env["PYTHONPATH"] = os.pathsep.join(
                item for item in (product_src, pythonpath) if item
            )
            if context.service.headers and context.suite.config.get("extra_headers") == "unsupported":
                raise ConfigError(
                    "extra_headers_unsupported",
                    f"{context.suite.suite_id} cannot apply service-access headers",
                )
            if context.service.headers and len(command) > 1 and command[1].endswith(".py"):
                command = [
                    command[0],
                    "-m",
                    "novita_self_test.run_with_headers",
                    *command[1:],
                ]
            process = self.runner.run(
                command,
                cwd=_resolve_cwd(context),
                env={
                    "NVST_BASE_URL": context.service.base_url,
                    "NVST_SERVED_MODEL": context.service.served_model,
                    "NVST_API_KEY_ENV": context.service.api_key_env,
                    "NVST_THINK_MODE": context.service.think_mode,
                    "KIMI_BASE_URL": context.service.base_url,
                    "KIMI_MODEL": context.service.served_model,
                    "MODEL_NAME": context.service.served_model,
                    "THINK_MODE": context.service.think_mode,
                    "KIMI_API_KEY": os.environ[context.service.api_key_env],
                    "KIMI_EXTRA_HEADERS_JSON": json.dumps(context.service.headers),
                    "BASE_URL": context.service.base_url,
                    "API_KEY": os.environ[context.service.api_key_env],
                    "SMODEL": context.service.served_model,
                    **configured_env,
                },
                timeout=context.suite.config.get("timeout_seconds"),
            )
        except (ConfigError, FileNotFoundError) as exc:
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="blocked",
                validity="execution_invalid",
                failure_summary=str(exc),
                reason=getattr(exc, "code", "runner_not_found"),
                runner_fingerprint=_runner_fingerprint(context),
                started_at=started_at,
                finished_at=utc_now(),
            )
        except subprocess.TimeoutExpired as exc:
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="failed",
                validity="execution_invalid",
                failure_summary=f"runner timed out after {exc.timeout}s",
                reason="runner_timeout",
                runner_fingerprint=_runner_fingerprint(context),
                started_at=started_at,
                finished_at=utc_now(),
            )

        stdout_path = context.suite_dir / "stdout.log"
        stderr_path = context.suite_dir / "stderr.log"
        _write_log(stdout_path, process.stdout or "")
        _write_log(stderr_path, process.stderr or "")
        artifact_paths = [str(stdout_path.resolve()), str(stderr_path.resolve())]
        standard_result = context.suite_dir / "adapter-result.json"
        if standard_result.is_file():
            try:
                payload = json.loads(standard_result.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("adapter-result.json must be an object")
                payload["suite_id"] = context.suite.suite_id
                payload["row_ids"] = list(context.suite.row_ids)
                payload["redacted_command"] = command
                payload["artifact_paths"] = list(
                    dict.fromkeys(
                        list(payload.get("artifact_paths") or [])
                        + artifact_paths
                        + [str(standard_result.resolve())]
                    )
                )
                payload["runner_fingerprint"] = {
                    **_runner_fingerprint(context),
                    **dict(payload.get("runner_fingerprint") or {}),
                }
                payload.setdefault("started_at", started_at)
                payload.setdefault("finished_at", utc_now())
                return SuiteResult.from_dict(payload)
            except (json.JSONDecodeError, ValueError, ConfigError) as exc:
                return SuiteResult(
                    suite_id=context.suite.suite_id,
                    row_ids=list(context.suite.row_ids),
                    status="failed",
                    validity="execution_invalid",
                    redacted_command=command,
                    artifact_paths=artifact_paths + [str(standard_result.resolve())],
                    failure_summary=f"invalid adapter-result.json: {exc}",
                    reason="runner_contract_invalid",
                    runner_fingerprint=_runner_fingerprint(context),
                    started_at=started_at,
                    finished_at=utc_now(),
                )
        valid_failure = bool(context.suite.config.get("failure_exit_is_valid", False))
        structured_contract_missing = process.returncode != 0 and not valid_failure
        return SuiteResult(
            suite_id=context.suite.suite_id,
            row_ids=list(context.suite.row_ids),
            status="passed" if process.returncode == 0 else "failed",
            validity="valid" if process.returncode == 0 or valid_failure else "execution_invalid",
            redacted_command=command,
            artifact_paths=artifact_paths,
            failure_summary=None
            if process.returncode == 0
            else (
                f"runner exited {process.returncode} without adapter-result.json"
                if structured_contract_missing
                else f"runner exit code {process.returncode}"
            ),
            reason=None
            if process.returncode == 0
            else "runner_contract_invalid"
            if structured_contract_missing
            else "runner_failed",
            runner_fingerprint=_runner_fingerprint(context),
            started_at=started_at,
            finished_at=utc_now(),
        )


class PytestAdapter(CommandAdapter):
    def build_command(self, context: AdapterContext) -> list[str]:
        executable = _format_tokens(
            [context.suite.config.get("python", "python3")], context
        )[0]
        paths = list(context.selector_args) or list(context.suite.config.get("paths") or [])
        if not paths:
            raise ConfigError(
                "runner_contract_invalid", f"{context.suite.suite_id} requires pytest paths"
            )
        command = [
            str(executable),
            "-m",
            "pytest",
            "-p",
            "novita_self_test.pytest_extra_headers",
        ]
        plugin = context.suite.config.get("artifact_plugin")
        if plugin:
            command.extend(["-p", str(plugin)])
        command.extend(_format_tokens(paths, context))
        command.extend(
            [
                "--base-url",
                context.service.base_url,
                "--smoke-model",
                context.service.served_model,
                "--think-mode",
                context.service.think_mode,
            ]
        )
        if plugin:
            command.extend(["--artifact-dir", str(context.suite_dir.resolve())])
        command.extend(
            _format_tokens(list(context.suite.config.get("pytest_args") or []), context)
        )
        return _apply_concurrency_override(command, context)

    def _run_once(
        self, context: AdapterContext, completeness: dict[str, Any]
    ) -> SuiteResult:
        result = super().run(context)
        cases = context.suite_dir / "cases.jsonl"
        if not cases.is_file():
            return enforce_completeness(result, completeness)
        final_by_nodeid: dict[str, tuple[str, str]] = {}
        failure_by_nodeid: dict[str, str] = {}
        for line in cases.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            nodeid = record.get("nodeid")
            outcome = record.get("outcome")
            phase = record.get("phase")
            if isinstance(nodeid, str) and isinstance(outcome, str) and outcome != "rerun":
                normalized = "error" if outcome == "failed" and phase != "call" else outcome
                final_by_nodeid[nodeid] = (normalized, str(phase or "call"))
                if outcome == "failed":
                    failure_by_nodeid[nodeid] = "\n".join(
                        str(record.get(key) or "")
                        for key in ("longrepr", "caplog", "sections")
                    )
        counts = {name: 0 for name in ("total", "passed", "failed", "skipped", "error")}
        for outcome, _ in final_by_nodeid.values():
            counts["total"] += 1
            counts[outcome if outcome in counts else "error"] += 1
        result.counts = counts
        result.coverage["observed_cases"] = sorted(final_by_nodeid)
        result.coverage["case_outcomes"] = {
            nodeid: outcome for nodeid, (outcome, _) in final_by_nodeid.items()
        }
        failure_texts = list(failure_by_nodeid.values())
        retryable_cases = sorted(
            nodeid
            for nodeid, text in failure_by_nodeid.items()
            if is_retryable_failure(text)
        )
        result.coverage["retryable_cases"] = retryable_cases
        result.coverage["failure_class_counts"] = failure_class_counts(failure_texts)
        if str(cases.resolve()) not in result.artifact_paths:
            result.artifact_paths.append(str(cases.resolve()))
        summary = context.suite_dir / "summary.json"
        if summary.is_file() and str(summary.resolve()) not in result.artifact_paths:
            result.artifact_paths.append(str(summary.resolve()))
        for name in completeness.get("required_artifacts") or []:
            candidate = context.suite_dir / str(name)
            if candidate.is_file() and str(candidate.resolve()) not in result.artifact_paths:
                result.artifact_paths.append(str(candidate.resolve()))
        for pattern in context.suite.config.get("artifact_globs") or []:
            for candidate in sorted(context.suite_dir.glob(str(pattern))):
                if candidate.is_file() and str(candidate.resolve()) not in result.artifact_paths:
                    result.artifact_paths.append(str(candidate.resolve()))
        if counts["failed"] or counts["error"]:
            result.status = "failed"
            result.validity = "execution_invalid" if counts["error"] else "valid"
            result.reason = None
            result.failure_summary = (
                f"pytest: {counts['failed']} failed, {counts['error']} errors, "
                f"{counts['skipped']} skipped"
            )
            non_blocking_patterns = [
                str(pattern)
                for pattern in context.suite.config.get(
                    "acceptance_non_blocking_case_patterns", []
                )
            ]
            failed_nodes = set(failure_by_nodeid)
            known_nodes = {
                nodeid
                for nodeid in failed_nodes
                if any(pattern in nodeid for pattern in non_blocking_patterns)
            }
            if known_nodes and known_nodes == failed_nodes:
                result.coverage["acceptance_non_blocking"] = True
                result.coverage["acceptance_non_blocking_failures"] = sorted(known_nodes)
            service_reason = service_invalid_reason(failure_texts)
            if service_reason is not None:
                result.status = "blocked"
                result.validity = "service_invalid"
                result.reason = service_reason
                result.failure_summary = (
                    "endpoint credential is invalid"
                    if service_reason == CREDENTIAL_INVALID
                    else "endpoint quota or balance is exhausted"
                )
            elif counts["error"]:
                result.reason = "pytest_case_error"
            elif retryable_cases:
                result.status = "failed"
                result.validity = "execution_invalid"
                result.reason = "retryable_case_failure"
                result.failure_summary = (
                    f"{len(retryable_cases)} cases require rerun after stability failures"
                )
        elif counts["total"] and result.validity == "valid":
            result.status = "passed"
        return enforce_completeness(result, completeness)

    @staticmethod
    def _set_rerun_cases(result: SuiteResult, policy: dict[str, Any]) -> None:
        retryable = set(result.coverage.get("retryable_cases") or [])
        outcomes = dict(result.coverage.get("case_outcomes") or {})
        if policy.get("mode") == "any_pass":
            retryable.update(
                nodeid
                for nodeid, outcome in outcomes.items()
                if outcome in {"failed", "error"}
            )
        result.coverage["rerun_cases"] = sorted(retryable)

    @classmethod
    def _needs_batch_rerun(
        cls, result: SuiteResult, policy: dict[str, Any]
    ) -> bool:
        cls._set_rerun_cases(result, policy)
        return bool(result.coverage.get("rerun_cases")) or (
            result.validity == "execution_invalid"
            and result.reason in {
                "runner_failed",
                "runner_timeout",
                "runner_not_found",
                "pytest_case_error",
            }
        )

    @classmethod
    def _merge_case_rerun(
        cls, base: SuiteResult, retry: SuiteResult, policy: dict[str, Any]
    ) -> SuiteResult:
        if retry.status == "blocked" or retry.validity == "service_invalid":
            return retry
        base_outcomes = dict(base.coverage.get("case_outcomes") or {})
        retry_outcomes = dict(retry.coverage.get("case_outcomes") or {})
        targets = set(base.coverage.get("rerun_cases") or [])
        if not targets:
            return retry
        for nodeid in targets:
            if retry_outcomes.get(nodeid) == "passed":
                base_outcomes[nodeid] = "passed"
            elif nodeid in retry_outcomes and base_outcomes.get(nodeid) != "passed":
                base_outcomes[nodeid] = retry_outcomes[nodeid]
        retryable = sorted(
            nodeid
            for nodeid in targets
            if nodeid not in retry_outcomes
            or nodeid in set(retry.coverage.get("retryable_cases") or [])
        )
        counts = {name: 0 for name in ("total", "passed", "failed", "skipped", "error")}
        for outcome in base_outcomes.values():
            counts["total"] += 1
            counts[outcome if outcome in counts else "error"] += 1
        base.counts = counts
        base.coverage["case_outcomes"] = base_outcomes
        base.coverage["observed_cases"] = sorted(base_outcomes)
        base.coverage["retryable_cases"] = retryable
        cls._set_rerun_cases(base, policy)
        base.artifact_paths = list(dict.fromkeys(base.artifact_paths + retry.artifact_paths))
        base.finished_at = retry.finished_at
        if base.coverage["rerun_cases"]:
            base.status = "failed"
            base.validity = "execution_invalid"
            base.reason = "rerun_pending"
            base.failure_summary = (
                f"{len(base.coverage['rerun_cases'])} cases remain after this rerun wave"
            )
        else:
            base.status = "passed"
            base.validity = "valid"
            base.reason = None
            base.failure_summary = None
        return base

    @classmethod
    def _finalize_case_reruns(
        cls, result: SuiteResult, policy: dict[str, Any]
    ) -> SuiteResult:
        cls._set_rerun_cases(result, policy)
        retryable = set(result.coverage.get("retryable_cases") or [])
        if retryable:
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "rerun_exhausted_invalid"
            result.failure_summary = f"invalid cases exhausted reruns: {sorted(retryable)}"
        elif result.counts.get("error", 0):
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "pytest_case_error"
            result.failure_summary = f"pytest retained {result.counts['error']} case errors"
        elif result.counts.get("failed", 0):
            result.status = "failed"
            result.validity = "valid"
            result.reason = "runner_failed"
            result.failure_summary = f"pytest: {result.counts['failed']} failed, 0 errors"
        else:
            result.status = "passed"
            result.validity = "valid"
            result.reason = None
            result.failure_summary = None
        return result

    @staticmethod
    def _pytest_worker_budget(context: AdapterContext, rounds: int) -> int | None:
        total = context.concurrency_override
        if total is None:
            args = list(context.suite.config.get("pytest_args") or [])
            try:
                total = int(args[args.index("-n") + 1])
            except (ValueError, IndexError, TypeError):
                return None
        if context.suite.config.get("repeat_schedule") == "round_barrier":
            return max(1, int(total))
        return max(1, int(total) // max(1, rounds))

    def _run_repeat_wave(
        self,
        context: AdapterContext,
        round_indexes: list[int],
        *,
        wave: int,
        previous: dict[int, SuiteResult] | None = None,
    ) -> dict[int, SuiteResult]:
        worker_budget = self._pytest_worker_budget(context, len(round_indexes))
        futures = {}
        with ThreadPoolExecutor(max_workers=len(round_indexes)) as executor:
            for index in round_indexes:
                prior = (previous or {}).get(index)
                rerun_cases = list((prior.coverage if prior else {}).get("rerun_cases") or [])
                selector_args = tuple(rerun_cases) if rerun_cases else context.selector_args
                name = "formal" if wave == 0 else f"rerun-{wave:02d}"
                attempt_context = replace(
                    context,
                    suite_dir=context.suite_dir / f"repeat-{index:02d}" / name,
                    selector_args=selector_args,
                    concurrency_override=worker_budget,
                )
                futures[executor.submit(self._run_once, attempt_context, {})] = index
            return {futures[future]: future.result() for future in as_completed(futures)}

    def _run_repeat_rounds(
        self,
        context: AdapterContext,
        round_indexes: list[int],
        *,
        wave: int,
        previous: dict[int, SuiteResult] | None = None,
    ) -> dict[int, SuiteResult]:
        results: dict[int, SuiteResult] = {}
        for index in round_indexes:
            results.update(
                self._run_repeat_wave(
                    context, [index], wave=wave, previous=previous
                )
            )
        return results

    def _aggregate_repeats(
        self,
        context: AdapterContext,
        results: list[SuiteResult],
        requested: int,
    ) -> SuiteResult:
        counts: dict[str, int] = {}
        artifact_paths: list[str] = []
        iterations = []
        for index, result in enumerate(results, start=1):
            for key, value in result.counts.items():
                counts[key] = counts.get(key, 0) + int(value)
            for path in result.artifact_paths:
                if path not in artifact_paths:
                    artifact_paths.append(path)
            iterations.append(
                {
                    "iteration": index,
                    "status": result.status,
                    "validity": result.validity,
                    "counts": result.counts,
                    "reason": result.reason,
                    "failure_summary": result.failure_summary,
                    "artifact_paths": result.artifact_paths,
                }
            )

        first_invalid = next(
            (result for result in results if result.validity != "valid"), None
        )
        failed = [
            (index, result)
            for index, result in enumerate(results, start=1)
            if result.status != "passed"
        ]
        if first_invalid is not None:
            status = "blocked" if first_invalid.status == "blocked" else "failed"
            validity = first_invalid.validity
            reason = first_invalid.reason or "repeat_runner_invalid"
        elif failed:
            status = "failed"
            validity = "valid"
            reason = "repeat_failed"
        else:
            status = "passed"
            validity = "valid"
            reason = None

        failure_summary = None
        if len(results) != requested:
            failure_summary = f"repeat execution stopped after {len(results)}/{requested} iterations"
        elif failed:
            failure_summary = "; ".join(
                f"repeat-{index:02d}: {result.failure_summary or result.reason or result.status}"
                for index, result in failed
            )

        summary_path = context.suite_dir / "repeat-summary.json"
        write_json(
            summary_path,
            {
                "schema_version": 1,
                "suite_id": context.suite.suite_id,
                "requested_repeats": requested,
                "observed_repeats": len(results),
                "iterations": iterations,
            },
        )
        artifact_paths.append(str(summary_path.resolve()))
        passed_repeats = sum(result.status == "passed" for result in results)
        return SuiteResult(
            suite_id=context.suite.suite_id,
            row_ids=list(context.suite.row_ids),
            status=status,
            validity=validity,
            counts=counts,
            metrics={
                "repeat_passed": passed_repeats,
                "repeat_failed": len(results) - passed_repeats,
            },
            redacted_command=results[0].redacted_command if results else [],
            artifact_paths=artifact_paths,
            failure_summary=failure_summary,
            runner_fingerprint={
                **_runner_fingerprint(context),
                "repeat_count": str(requested),
            },
            coverage={
                "observed_repeats": len(results),
                "repeat_results": iterations,
            },
            reason=reason,
            started_at=results[0].started_at if results else utc_now(),
            finished_at=results[-1].finished_at if results else utc_now(),
        )

    def run(self, context: AdapterContext) -> SuiteResult:
        repeat_count = context.suite.config.get("repeat", 1)
        if context.acceptance_mode == "develop":
            repeat_count = context.suite.config.get("develop_repeat", repeat_count)
        effective_completeness = dict(context.suite.completeness)
        configured_repeat = context.suite.config.get("repeat", 1)
        if (
            context.suite.config.get("develop_repeat") == 1
            and repeat_count == 1
        ):
            per_repeat_contract = context.suite.completeness.get("per_repeat")
            if isinstance(per_repeat_contract, dict):
                effective_completeness = dict(per_repeat_contract)
        elif (
            isinstance(configured_repeat, int)
            and not isinstance(configured_repeat, bool)
            and configured_repeat > 1
            and isinstance(repeat_count, int)
            and not isinstance(repeat_count, bool)
        ):
            effective_completeness["expected_repeats"] = repeat_count
        if (
            not isinstance(repeat_count, int)
            or isinstance(repeat_count, bool)
            or repeat_count < 1
        ):
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="blocked",
                validity="execution_invalid",
                reason="runner_contract_invalid",
                failure_summary="pytest config.repeat must be a positive integer",
                runner_fingerprint=_runner_fingerprint(context),
                started_at=utc_now(),
                finished_at=utc_now(),
            )
        single_develop = (
            context.suite.config.get("develop_repeat") == 1
            and repeat_count == 1
        )
        if repeat_count == 1:
            result = self._run_once(context, {})
            policy = dict(context.suite.config.get("rerun_policy") or {})
            if not policy or result.status == "blocked" or result.validity == "service_invalid":
                return result if single_develop else enforce_completeness(result, effective_completeness)
            if policy.get("mode") == "runner_owned":
                result.coverage["rerun_policy"] = policy
                return result if single_develop else enforce_completeness(result, effective_completeness)
            self._set_rerun_cases(result, policy)
            initial_reruns = len(result.coverage.get("rerun_cases") or [])
            for wave in range(1, int(policy.get("max_reruns", 0)) + 1):
                if not self._needs_batch_rerun(result, policy):
                    break
                rerun_cases = list(result.coverage.get("rerun_cases") or [])
                retry_context = replace(
                    context,
                    suite_dir=context.suite_dir / f"rerun-{wave:02d}",
                    selector_args=tuple(rerun_cases) if rerun_cases else context.selector_args,
                )
                retry = self._run_once(retry_context, {})
                result = self._merge_case_rerun(result, retry, policy)
                if not self._needs_batch_rerun(result, policy):
                    break
            result = self._finalize_case_reruns(result, policy)
            result.coverage["rerun_policy"] = policy
            result.coverage["rerun_recovered_cases"] = max(
                initial_reruns - len(result.coverage.get("rerun_cases") or []), 0
            )
            return result if single_develop else enforce_completeness(result, effective_completeness)

        per_repeat = context.suite.completeness.get("per_repeat")
        if not isinstance(per_repeat, dict) or not per_repeat:
            return SuiteResult(
                suite_id=context.suite.suite_id,
                row_ids=list(context.suite.row_ids),
                status="blocked",
                validity="execution_invalid",
                reason="runner_contract_invalid",
                failure_summary="repeated pytest suites require completeness.per_repeat",
                runner_fingerprint=_runner_fingerprint(context),
                started_at=utc_now(),
                finished_at=utc_now(),
            )

        run_rounds = (
            self._run_repeat_rounds
            if context.suite.config.get("repeat_schedule") == "round_barrier"
            else self._run_repeat_wave
        )
        formal = run_rounds(context, list(range(1, repeat_count + 1)), wave=0)
        results = dict(formal)
        policy = dict(context.suite.config.get("rerun_policy") or {})
        if policy.get("mode") == "runner_owned":
            ordered_results = [
                enforce_completeness(results[index], per_repeat)
                for index in range(1, repeat_count + 1)
            ]
            aggregate = self._aggregate_repeats(context, ordered_results, repeat_count)
            aggregate.coverage["rerun_policy"] = policy
            aggregate.coverage["rerun_recovered_rounds"] = 0
            return aggregate
        for result in results.values():
            self._set_rerun_cases(result, policy)
        max_reruns = int(policy.get("max_reruns", 0))
        rerun_recovered = 0
        for wave in range(1, max_reruns + 1):
            pending = [
                index
                for index, result in sorted(results.items())
                if self._needs_batch_rerun(result, policy)
            ]
            if not pending:
                break
            retries = run_rounds(context, pending, wave=wave, previous=results)
            for index, retry in retries.items():
                was_retryable = self._needs_batch_rerun(results[index], policy)
                results[index] = self._merge_case_rerun(results[index], retry, policy)
                if was_retryable and not self._needs_batch_rerun(results[index], policy):
                    rerun_recovered += 1
        ordered_results = [
            enforce_completeness(
                self._finalize_case_reruns(results[index], policy), per_repeat
            )
            for index in range(1, repeat_count + 1)
        ]
        aggregate = self._aggregate_repeats(context, ordered_results, repeat_count)
        aggregate.coverage["rerun_policy"] = policy
        aggregate.coverage["rerun_recovered_rounds"] = rerun_recovered
        return aggregate


class ReplayAdapter(CommandAdapter):
    @staticmethod
    def _recover_complete_replay(
        context: AdapterContext, replay_dir: Path
    ) -> bool:
        """Finalize a replay after the process timeout if every case response exists.

        Large image requests can finish all vendor calls while the child process
        is still serializing its aggregate files. The parent timeout must not
        discard complete case evidence.
        """
        kind = str(context.suite.config.get("kind") or "")
        if kind != "image" or context.suite.config.get("timeout_seconds") is None:
            return False
        fixture = Path(str(context.suite.config.get("fixture") or ""))
        if not fixture.is_absolute():
            fixture = context.repository_root / fixture
        try:
            records = [
                json.loads(line)
                for line in fixture.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError):
            return False
        results: list[dict[str, Any]] = []
        for index, record in enumerate(records, start=1):
            case_dir = replay_dir / f"{index:04d}-image-{index:04d}"
            body_path = case_dir / "response.body"
            if not body_path.is_file() or body_path.stat().st_size == 0:
                return False
            body = body_path.read_bytes()
            try:
                payload = json.loads(body)
                choices = payload.get("choices") or []
                finish_reason = (choices[0].get("finish_reason") if choices else None)
                error = payload.get("error") if isinstance(payload, dict) else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                finish_reason = None
                error = None
                for line in body.splitlines():
                    if not line.startswith(b"data: ") or line == b"data: [DONE]":
                        continue
                    try:
                        event = json.loads(line[6:])
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    choices = event.get("choices") or []
                    if choices:
                        finish_reason = choices[0].get("finish_reason") or finish_reason
                if finish_reason is None:
                    return False
            expected = record.get("expected_finish_reason")
            passed = error is None and finish_reason == expected
            results.append(
                {
                    "case": f"image-{index:04d}",
                    "index": index,
                    "http_status": 200 if error is None else None,
                    "expected_finish_reason": expected,
                    "finish_reason": finish_reason,
                    "passed": passed,
                    "error": error,
                    "recovered_after_runner_timeout": True,
                }
            )
        passed_count = sum(bool(item["passed"]) for item in results)
        verdict = {
            "schema_version": 1,
            "status": "passed" if passed_count == len(results) else "failed",
            "completed": True,
            "passed": passed_count,
            "failed": len(results) - passed_count,
            "total": len(results),
            "finish_reason_contract": "image fixture expected per-record",
            "recovered_after_runner_timeout": True,
            "ended_at": utc_now(),
        }
        replay_dir.mkdir(parents=True, exist_ok=True)
        (replay_dir / "summary.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
            encoding="utf-8",
        )
        (replay_dir / "verdict.json").write_text(
            json.dumps(verdict, indent=2) + "\n", encoding="utf-8"
        )
        (replay_dir / "summary.md").write_text(
            "\n".join(
                [
                    "# K3 Fixture Replay",
                    "",
                    f"Status: **{verdict['status']}**",
                    f"Passed: **{passed_count}/{len(results)}**",
                    "Recovered aggregate from complete case response artifacts after runner timeout.",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return True

    def build_command(self, context: AdapterContext) -> list[str]:
        kind = str(context.suite.config.get("kind") or "")
        if kind not in {"image", "openclaw"}:
            raise ConfigError(
                "runner_contract_invalid",
                f"{context.suite.suite_id} requires config.kind image or openclaw",
            )
        fixture_value = context.suite.config.get("fixture")
        if not isinstance(fixture_value, str) or not fixture_value:
            raise ConfigError(
                "runner_contract_invalid",
                f"{context.suite.suite_id} requires config.fixture",
            )
        fixture = Path(_format_tokens([fixture_value], context)[0])
        if not fixture.is_absolute():
            fixture = context.repository_root / fixture
        runner = context.repository_root / "suites" / "novita-api" / "tools" / "replay_fixture.py"
        command = [
            sys.executable,
            str(runner.resolve()),
            kind,
            "--fixture",
            str(fixture.resolve()),
            "--base-url",
            context.service.base_url,
            "--model",
            context.service.served_model,
            "--output-dir",
            str((context.suite_dir / "replay").resolve()),
        ]
        timeout = context.suite.config.get("timeout_seconds")
        if timeout is not None:
            command.extend(["--timeout", str(timeout)])
        command.extend(context.selector_args)
        return command

    def run(self, context: AdapterContext) -> SuiteResult:
        result = super().run(context)
        replay_dir = context.suite_dir / "replay"
        if result.reason == "runner_timeout":
            self._recover_complete_replay(context, replay_dir)
        verdict_path = replay_dir / "verdict.json"
        if not verdict_path.is_file():
            if result.validity == "valid":
                result.status = "failed"
                result.validity = "execution_invalid"
                result.reason = "replay_verdict_missing"
                result.failure_summary = "fixture replay did not write verdict.json"
            return result
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            total = int(verdict["total"])
            passed = int(verdict["passed"])
            failed = int(verdict["failed"])
            if verdict.get("recovered_after_runner_timeout"):
                result.coverage["recovered_after_runner_timeout"] = True
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "replay_verdict_invalid"
            result.failure_summary = f"invalid replay verdict: {exc}"
            return result

        result.status = "passed" if verdict.get("status") == "passed" else "failed"
        result.validity = "valid"
        result.counts = {"total": total, "passed": passed, "failed": failed}
        result.coverage["observed_cases"] = total
        if result.status == "failed":
            result.reason = "fixture_replay_failed"
            result.failure_summary = f"fixture replay: {passed}/{total} passed"

        summaries: dict[int, dict[str, Any]] = {}
        summary_path = replay_dir / "summary.jsonl"
        if summary_path.is_file():
            for line in summary_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                    summaries[int(item["index"])] = item
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
        def stability_text(item: dict[str, Any]) -> str:
            return json.dumps(
                {
                    "http_status": item.get("http_status"),
                    "finish_reason": item.get("finish_reason"),
                    "transport_error": item.get("transport_error"),
                    "error": item.get("error"),
                },
                ensure_ascii=False,
            )

        summary_texts = [stability_text(item) for item in summaries.values()]
        service_reason = service_invalid_reason(summary_texts)
        if service_reason is not None:
            result.status = "blocked"
            result.validity = "service_invalid"
            result.reason = service_reason
            result.failure_summary = (
                "endpoint credential is invalid"
                if service_reason == CREDENTIAL_INVALID
                else "endpoint quota or balance is exhausted"
            )
            return result

        policy = dict(context.suite.config.get("rerun_policy") or {})

        def invalid_indices() -> list[int]:
            return sorted(
                index
                for index, item in summaries.items()
                if is_retryable_failure(stability_text(item))
            )

        def retryable_indices() -> list[int]:
            retryable = set(invalid_indices())
            if policy.get("mode") == "any_pass":
                retryable.update(
                    index for index, item in summaries.items() if not item.get("passed")
                )
            return sorted(retryable)

        initial_retryable = set(retryable_indices())
        for wave in range(1, int(policy.get("max_reruns", 0)) + 1):
            pending = retryable_indices()
            if not pending:
                break
            selector_args = tuple(
                token for index in pending for token in ("--case-index", str(index))
            )
            retry_context = replace(
                context,
                suite_dir=context.suite_dir / f"rerun-{wave:02d}",
                selector_args=selector_args,
            )
            retry_result = CommandAdapter.run(self, retry_context)
            retry_summary = retry_context.suite_dir / "replay" / "summary.jsonl"
            if retry_summary.is_file():
                for line in retry_summary.read_text(encoding="utf-8").splitlines():
                    try:
                        item = json.loads(line)
                        summaries[int(item["index"])] = item
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
            service_reason = service_invalid_reason(
                stability_text(item) for item in summaries.values()
            )
            if service_reason is not None:
                result.status = "blocked"
                result.validity = "service_invalid"
                result.reason = service_reason
                result.failure_summary = (
                    "endpoint credential is invalid"
                    if service_reason == CREDENTIAL_INVALID
                    else "endpoint quota or balance is exhausted"
                )
                return result
            result.artifact_paths = list(
                dict.fromkeys(result.artifact_paths + retry_result.artifact_paths)
            )
            for name in ("run.json", "summary.jsonl", "summary.md", "verdict.json"):
                path = retry_context.suite_dir / "replay" / name
                if path.is_file():
                    result.artifact_paths.append(str(path.resolve()))

        remaining_retryable = retryable_indices()
        remaining_invalid = invalid_indices()
        summary_texts = [stability_text(item) for item in summaries.values()]
        passed = sum(bool(item.get("passed")) for item in summaries.values())
        total = len(summaries)
        result.counts = {"total": total, "passed": passed, "failed": total - passed}
        result.coverage.update(
            {
                "observed_cases": total,
                "rerun_policy": policy,
                "failure_class_counts": failure_class_counts(summary_texts),
                "initial_retryable_cases": sorted(initial_retryable),
                "retryable_cases": remaining_retryable,
                "rerun_recovered_cases": len(
                    initial_retryable - set(remaining_retryable)
                ),
            }
        )
        if remaining_invalid:
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "replay_retry_exhausted"
            result.failure_summary = f"invalid replay cases exhausted: {remaining_invalid}"
        elif passed != total:
            result.status = "failed"
            result.validity = "valid"
            result.reason = "fixture_replay_failed"
            result.failure_summary = f"fixture replay: {passed}/{total} passed"
        else:
            result.status = "passed"
            result.validity = "valid"
            result.reason = None
            result.failure_summary = None

        for name in ("run.json", "summary.jsonl", "summary.md", "verdict.json"):
            path = replay_dir / name
            if path.is_file() and str(path.resolve()) not in result.artifact_paths:
                result.artifact_paths.append(str(path.resolve()))

        if context.suite.config.get("kind") == "image":
            fixture = Path(str(context.suite.config["fixture"]))
            if not fixture.is_absolute():
                fixture = context.repository_root / fixture
            records = [
                json.loads(line)
                for line in fixture.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            image_counts = []
            for record in records:
                count = 0
                for message in record["transformed_request"].get("messages", []):
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    count += sum(
                        isinstance(item, dict) and item.get("type") == "image_url"
                        for item in content
                    )
                image_counts.append(count)
            max_images = max(image_counts, default=0)
            result.metrics["max_images"] = max_images
            result.coverage["observed_max_images"] = max_images
        return result


class InspectAdapter(CommandAdapter):
    @staticmethod
    def _load_eval_log(directory: Path):
        from inspect_ai.log import read_eval_log

        logs = sorted(directory.glob("**/*.eval"))
        if len(logs) != 1:
            raise ValueError(f"expected one .eval log, found {len(logs)}")
        log = read_eval_log(logs[0])
        if log.status != "success" or log.results is None or not log.results.scores:
            raise ValueError(f"Inspect status is {log.status}")
        if not isinstance(getattr(log, "samples", None), list):
            score_result = log.results.scores[0]
            accuracy = float(score_result.metrics["accuracy"].value)
            total = int(log.results.total_samples)
            log.samples = [
                SimpleNamespace(
                    id=f"aggregate-{index}",
                    error=None,
                    scores={"score": SimpleNamespace(value=accuracy)},
                    output=SimpleNamespace(metadata={}),
                )
                for index in range(total)
            ]
        return log, logs[0]

    @staticmethod
    def _sample_stability_text(sample: Any) -> str:
        error = getattr(sample, "error", None)
        output = getattr(sample, "output", None)
        metadata = getattr(output, "metadata", None)
        finish_reason = (
            metadata.get("original_finish_reason")
            if isinstance(metadata, dict)
            else getattr(metadata, "original_finish_reason", None)
        )
        return json.dumps(
            {
                "error": str(getattr(error, "message", error) or ""),
                "finish_reason": finish_reason,
            },
            ensure_ascii=False,
        )

    @classmethod
    def _sample_needs_rerun(cls, sample: Any) -> bool:
        error = getattr(sample, "error", None)
        return (
            error is not None
            or not getattr(sample, "scores", None)
            or is_retryable_failure(cls._sample_stability_text(sample))
        )

    def run(self, context: AdapterContext) -> SuiteResult:
        policy = dict(context.suite.config.get("rerun_policy") or {})
        result = CommandAdapter.run(self, context)
        active_context = context
        runner_reruns = 0
        if result.validity != "valid" or result.status != "passed":
            if result.validity == "service_invalid" or result.status == "blocked":
                return result
            if policy.get("mode") == "runner_owned":
                result.coverage["rerun_policy"] = policy
                return result
            artifacts = list(result.artifact_paths)
            for wave in range(1, int(policy.get("max_reruns", 0)) + 1):
                retry_context = replace(
                    context,
                    suite_dir=context.suite_dir / f"runner-rerun-{wave:02d}",
                )
                retry = CommandAdapter.run(self, retry_context)
                artifacts.extend(retry.artifact_paths)
                runner_reruns = wave
                result = retry
                active_context = retry_context
                if retry.validity == "valid" and retry.status == "passed":
                    break
                if retry.validity == "service_invalid" or retry.status == "blocked":
                    return retry
            result.artifact_paths = list(dict.fromkeys(artifacts))
            if result.validity != "valid" or result.status != "passed":
                result.coverage["rerun_policy"] = policy
                result.coverage["runner_rerun_waves"] = runner_reruns
                return result
        try:
            log, formal_log_path = self._load_eval_log(active_context.suite_dir)
            total = int(log.results.total_samples)
            formal_accuracy = float(log.results.scores[0].metrics["accuracy"].value)
            trials_by_key = {
                (str(sample.id), int(getattr(sample, "epoch", 0) or 0)): sample
                for sample in log.samples
            }
        except (KeyError, TypeError, ValueError) as exc:
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "inspect_log_invalid"
            result.failure_summary = str(exc)
            return result

        sample_texts = [
            self._sample_stability_text(sample) for sample in trials_by_key.values()
        ]
        service_reason = service_invalid_reason(sample_texts)
        if service_reason is not None:
            result.status = "blocked"
            result.validity = "service_invalid"
            result.reason = service_reason
            result.failure_summary = (
                "endpoint credential is invalid"
                if service_reason == CREDENTIAL_INVALID
                else "endpoint quota or balance is exhausted"
            )
            result.coverage["failure_class_counts"] = failure_class_counts(sample_texts)
            return result

        policy = dict(context.suite.config.get("rerun_policy") or {})
        initial_retryable = sorted({
            str(sample.id)
            for sample in trials_by_key.values()
            if self._sample_needs_rerun(sample)
        })
        pending = list(initial_retryable)
        profile_reruns = (
            0 if policy.get("mode") == "runner_owned" else int(policy.get("max_reruns", 0))
        )
        for wave in range(1, profile_reruns + 1):
            if not pending:
                break
            selector_args = tuple(
                token for sample_id in pending for token in ("--sample-id", sample_id)
            )
            retry_context = replace(
                context,
                suite_dir=context.suite_dir / f"rerun-{wave:02d}",
                selector_args=selector_args,
            )
            retry_result = CommandAdapter.run(self, retry_context)
            if retry_result.status == "blocked" or retry_result.validity == "service_invalid":
                return retry_result
            try:
                retry_log, retry_log_path = self._load_eval_log(retry_context.suite_dir)
            except (KeyError, TypeError, ValueError) as exc:
                result.failure_summary = f"Inspect rerun wave {wave} is invalid: {exc}"
                continue
            for sample in retry_log.samples:
                trials_by_key[(str(sample.id), int(getattr(sample, "epoch", 0) or 0))] = sample
            sample_texts = [
                self._sample_stability_text(sample) for sample in trials_by_key.values()
            ]
            service_reason = service_invalid_reason(sample_texts)
            if service_reason is not None:
                result.status = "blocked"
                result.validity = "service_invalid"
                result.reason = service_reason
                result.failure_summary = (
                    "endpoint credential is invalid"
                    if service_reason == CREDENTIAL_INVALID
                    else "endpoint quota or balance is exhausted"
                )
                result.coverage["failure_class_counts"] = failure_class_counts(sample_texts)
                return result
            result.artifact_paths = list(
                dict.fromkeys(
                    result.artifact_paths
                    + retry_result.artifact_paths
                    + [str(retry_log_path.resolve())]
                )
            )
            pending = sorted({
                str(sample.id)
                for sample in trials_by_key.values()
                if self._sample_needs_rerun(sample)
            })

        from inspect_ai.scorer import value_to_float

        samples = list(trials_by_key.values())
        scored_values = []
        for sample in trials_by_key.values():
            if self._sample_needs_rerun(sample):
                continue
            scores = getattr(sample, "scores", None) or {}
            score = next(iter(scores.values()), None)
            if score is not None:
                value = getattr(score, "value", None)
                scored_values.append(
                    value_to_float()(value) if value is not None else formal_accuracy
                )
        completed = len(scored_values)
        scored = completed
        unscored = 0
        accuracy = sum(scored_values) / scored if scored else 0.0
        result.coverage.update(
            {
                "rerun_policy": policy,
                "runner_rerun_waves": runner_reruns,
                "failure_class_counts": failure_class_counts(sample_texts),
                "initial_retryable_sample_ids": initial_retryable,
                "retryable_sample_ids": pending,
                "rerun_recovered_samples": len(set(initial_retryable) - set(pending)),
            }
        )
        result.counts = {
            "total": total,
            "completed": completed,
            "scored": scored,
            "unscored": unscored,
        }
        result.metrics = {"score": accuracy}
        result.coverage["observed_trials"] = completed
        result.artifact_paths.append(str(formal_log_path.resolve()))
        allowed_error_ids = {
            str(item)
            for item in (
                context.suite.completeness.get("allowed_error_sample_ids") or []
            )
        }
        observed_error_ids: set[str] = set()
        if allowed_error_ids:
            if not isinstance(samples, list):
                result.status = "failed"
                result.validity = "execution_invalid"
                result.reason = "inspect_log_invalid"
                result.failure_summary = "Inspect samples are required for allowed-error validation"
                return result
            observed_error_ids = {
                str(sample.id)
                for sample in samples
                if getattr(sample, "error", None) is not None
                or not getattr(sample, "scores", None)
            }
            result.coverage["observed_error_sample_ids"] = sorted(observed_error_ids)

        observed_invalid_finish_reasons: dict[str, list[str]] = {}
        invalid_scored_count = 0
        invalid_error_count = 0
        if isinstance(samples, list):
            for sample in samples:
                output = getattr(sample, "output", None)
                metadata = getattr(output, "metadata", None)
                original_finish_reason = (
                    metadata.get("original_finish_reason")
                    if isinstance(metadata, dict)
                    else getattr(metadata, "original_finish_reason", None)
                )
                error = getattr(sample, "error", None)
                error_text = str(getattr(error, "message", error) or "")
                is_engine_overloaded = (
                    original_finish_reason == "engine_overloaded"
                    or "engine_overloaded" in error_text
                )
                if not is_engine_overloaded:
                    continue
                sample_id = str(sample.id)
                observed_invalid_finish_reasons.setdefault(
                    "engine_overloaded", []
                ).append(sample_id)
                if error is None and getattr(sample, "scores", None):
                    invalid_scored_count += 1
                else:
                    invalid_error_count += 1
        if observed_invalid_finish_reasons:
            invalid_ids = [
                sample_id
                for sample_ids in observed_invalid_finish_reasons.values()
                for sample_id in sample_ids
            ]
            result.counts["completed"] = completed
            result.counts["scored"] = scored
            result.counts["unscored"] = (
                unscored + invalid_scored_count + invalid_error_count
            )
            result.coverage["observed_invalid_finish_reason_sample_ids"] = sorted(
                invalid_ids
            )
            result.coverage["invalid_finish_reason_counts"] = {
                reason: len(sample_ids)
                for reason, sample_ids in observed_invalid_finish_reasons.items()
            }
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "inspect_invalid_finish_reason"
            result.failure_summary = (
                "Inspect returned non-success finish reasons that must not be "
                "scored as model output: "
                + ", ".join(
                    f"{reason}={len(sample_ids)}"
                    for reason, sample_ids in sorted(
                        observed_invalid_finish_reasons.items()
                    )
                )
            )
            return result

        official_values = context.suite.config.get("official_scores")
        if not isinstance(official_values, list):
            official_values = [context.suite.config.get("official_score")]
        official_values = [
            float(item) for item in official_values if isinstance(item, (int, float))
        ]
        tolerance = float(context.suite.config.get("tolerance_pp", 2.0)) / 100
        tolerance_mode = str(context.suite.config.get("tolerance_mode") or "absolute")
        if tolerance_mode not in {"absolute", "lower_bound"}:
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "runner_contract_invalid"
            result.failure_summary = f"unsupported tolerance_mode: {tolerance_mode}"
            return result
        if allowed_error_ids and (
            not observed_error_ids.issubset(allowed_error_ids)
            or completed != total - len(observed_error_ids)
            or scored != completed
            or unscored
        ):
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "inspect_samples_incomplete"
            result.failure_summary = (
                f"total={total}, completed={completed}, scored={scored}, unscored={unscored}, "
                f"allowed_error_ids={sorted(allowed_error_ids)}, "
                f"observed_error_ids={sorted(observed_error_ids)}"
            )
        elif not allowed_error_ids and (completed != total or scored != total or unscored):
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "inspect_samples_incomplete"
            result.failure_summary = (
                f"total={total}, completed={completed}, scored={scored}, unscored={unscored}"
            )
        elif (
            official_values
            and tolerance_mode == "lower_bound"
            and float(accuracy) < min(official_values) - tolerance - 1e-12
        ):
            result.status = "failed"
            result.validity = "valid"
            result.reason = "benchmark_below_tolerance"
            result.failure_summary = (
                f"score {accuracy:.6f} is more than {tolerance * 100:.2f}pp below "
                f"official lower bound {min(official_values):.6f}"
            )
        elif (
            official_values
            and tolerance_mode == "absolute"
            and min(abs(float(accuracy) - official) for official in official_values)
            > tolerance + 1e-12
        ):
            result.status = "failed"
            result.validity = "valid"
            result.reason = "benchmark_below_tolerance"
            result.failure_summary = (
                f"score {accuracy:.6f} differs from official observations "
                f"{official_values} by more than {tolerance * 100:.2f}pp"
            )
        else:
            result.status = "passed"
            result.validity = "valid"
            result.reason = None
            result.failure_summary = None
        return result


class CodeBenchAdapter(CommandAdapter):
    @staticmethod
    def _crashed_finalize_state(suite_dir: Path) -> bool:
        return (
            (suite_dir / "config.snapshot.json").is_file()
            and (suite_dir / "archive").is_dir()
            and not (suite_dir / "finalization-manifest.json").is_file()
        )

    def _materialize_contract(self, context: AdapterContext, mode: str) -> Path:
        source = (
            context.repository_root
            / "suites"
            / "kimi-code-bench"
            / "examples"
            / "run-contract.example.json"
        )
        contract = json.loads(source.read_text(encoding="utf-8"))
        count = 15 if mode == "smoke" else 89
        tag = context.run_dir.name.lower()
        evidence_dir = str(context.suite_dir.resolve())
        contract["run"].update(
            {
                "tag": tag,
                "selected_task_count": count,
                "fixed_denominator": count,
                "required_trial_count": count * 3,
                "evidence_dir": evidence_dir,
                "status_path": f"{evidence_dir}/status.json",
                "detached": False,
                "concurrency": count,
            }
        )
        contract["task_selection"] = {
            "kind": "builtin",
            "value": mode,
            "expected_count": count,
        }
        contract["execution_profile"].update(
            {
                "concurrency": count,
                "schedule": "task_pipelines",
                "concurrency_scope": "task_count",
                "recovery": {"enabled": True, "attempts_per_abnormal_task": 1},
            }
        )
        contract["service"] = {
            "base_url": context.service.base_url,
            "model": context.service.served_model,
            "headers": context.service.headers,
            "preserve_thinking": True,
        }
        contract["authentication"] = {
            "kind": "env",
            "env_name": context.service.api_key_env,
        }
        remote = context.suite.config.get("remote") or {}
        contract["remote"] = {
            "host": str(remote.get("host", "build-server")),
            "port": int(remote.get("port", 22)),
            "workdir": str(
                remote.get("workdir_root", "/srv/kvh/kbench-runs")
            ).rstrip("/")
            + f"/{tag}",
            "container_proxy": str(
                remote.get("container_proxy", "http://172.17.0.1:1081")
            ),
            "runtime_cache_root": str(
                remote.get("runtime_cache_root", "/srv/kvh/.kbench-cache")
            ),
        }
        path = context.suite_dir / "code-bench-run-contract.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def build_command(self, context: AdapterContext) -> list[str]:
        mode = (
            context.selector.split(":", 1)[1]
            if context.selector and ":" in context.selector
            else "extended"
        )
        expected_concurrency = 15 if mode == "smoke" else 89
        if context.concurrency_override not in (None, expected_concurrency):
            raise ValueError(
                "Code Bench concurrency is fixed to the selected task count "
                f"({expected_concurrency})"
            )
        if self._crashed_finalize_state(context.suite_dir):
            # A previous attempt completed run/fetch and then crashed in the
            # local finalize step. Repair it in place with the original
            # materialized contract and without new traffic.
            contract = context.suite_dir / "code-bench-run-contract.json"
            if not contract.is_file():
                raise ConfigError(
                    "runner_contract_invalid",
                    "finalize repair requires the original materialized contract",
                )
        else:
            contract = self._materialize_contract(context, mode)
        command = [
            sys.executable,
            "-m",
            "novita_self_test.adapters.codebench_run",
            "--contract",
            str(contract),
            "--run-dir",
            str(context.suite_dir.resolve()),
        ]
        if self._crashed_finalize_state(context.suite_dir):
            command.append("--finalize-only")
        minimum = context.suite.config.get("min_score")
        if minimum is not None:
            command.extend(["--min-score", str(minimum)])
        return command


def _reread_beam_judge_result(context: AdapterContext, result: SuiteResult) -> SuiteResult:
    """Re-read the judge phase's own adapter-result.json after a detached wait."""
    final_path = context.suite_dir / "adapter-result.json"
    try:
        payload = json.loads(final_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result.status = "failed"
        result.validity = "execution_invalid"
        result.reason = "beam_judge_result_missing"
        result.failure_summary = f"background BEAM judge did not produce a valid result: {exc}"
        return result
    payload["suite_id"] = context.suite.suite_id
    payload["row_ids"] = list(context.suite.row_ids)
    payload["artifact_paths"] = list(
        dict.fromkeys(
            list(payload.get("artifact_paths") or []) + result.artifact_paths + [str(final_path.resolve())]
        )
    )
    payload["runner_fingerprint"] = {
        **result.runner_fingerprint,
        **dict(payload.get("runner_fingerprint") or {}),
    }
    return SuiteResult.from_dict(payload)


class BeamAdapter(CommandAdapter):
    def __init__(self, runner: ProcessRunner | None = None) -> None:
        super().__init__(runner=runner)
        self._judge_process: subprocess.Popen[str] | None = None
        self._judge_handles: tuple[Any, Any] | None = None
        self._judge_command: list[str] = []
        self._judge_meta_path: Path | None = None

    def _wait_for_detached_judge(self, context: AdapterContext) -> bool:
        timeout = float(context.suite.config.get("judge_timeout_seconds", 86400))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._recorded_judge_is_running():
                return True
            time.sleep(5.0)
        return not self._recorded_judge_is_running()

    def build_command(self, context: AdapterContext) -> list[str]:
        command = CommandAdapter.build_command(self, context)
        if context.suite.config.get("beam_phase") != "judge":
            command.append("--generation-only")
        return command

    def _judge_command_for(self, context: AdapterContext) -> list[str]:
        judge_suite = replace(
            context.suite,
            config={**context.suite.config, "beam_phase": "judge"},
        )
        judge_context = replace(context, suite=judge_suite)
        command = CommandAdapter.build_command(self, judge_context)
        command.append("--reuse-answers")
        return command

    def _judge_environment(self, context: AdapterContext) -> dict[str, str]:
        configured_env = {
            str(key): str(value).format_map(
                {
                    "base_url": context.service.base_url,
                    "served_model": context.service.served_model,
                    "think_mode": context.service.think_mode,
                    "run_dir": str(context.run_dir.resolve()),
                    "suite_dir": str(context.suite_dir.resolve()),
                    "repository_root": str(context.repository_root.resolve()),
                }
            )
            for key, value in (context.suite.config.get("env") or {}).items()
        }
        product_src = str((context.repository_root / "src").resolve())
        pythonpath = configured_env.get("PYTHONPATH", os.environ.get("PYTHONPATH", ""))
        configured_env["PYTHONPATH"] = os.pathsep.join(
            item for item in (product_src, pythonpath) if item
        )
        return {
            **os.environ,
            "NVST_BASE_URL": context.service.base_url,
            "NVST_SERVED_MODEL": context.service.served_model,
            "NVST_API_KEY_ENV": context.service.api_key_env,
            "NVST_THINK_MODE": context.service.think_mode,
            "KIMI_BASE_URL": context.service.base_url,
            "KIMI_MODEL": context.service.served_model,
            "MODEL_NAME": context.service.served_model,
            "THINK_MODE": context.service.think_mode,
            "KIMI_EXTRA_HEADERS_JSON": json.dumps(context.service.headers),
            "BASE_URL": context.service.base_url,
            "SMODEL": context.service.served_model,
            **configured_env,
        }

    def run(self, context: AdapterContext) -> SuiteResult:
        policy = dict(context.suite.config.get("rerun_policy") or {})
        if policy.get("mode") != "any_pass":
            result = CommandAdapter.run(self, context)
            if result.reason != "beam_judge_pending":
                return result
            generation_result = context.suite_dir / "adapter-result.json"
            generation_copy = context.suite_dir / "generation-result.json"
            if generation_result.is_file():
                shutil.copyfile(generation_result, generation_copy)
                result.artifact_paths.append(str(generation_copy.resolve()))
            self._judge_command = self._judge_command_for(context)
            judge_stdout = context.suite_dir / "judge.stdout.log"
            judge_stderr = context.suite_dir / "judge.stderr.log"
            try:
                stdout_handle = judge_stdout.open("w", encoding="utf-8")
                stderr_handle = judge_stderr.open("w", encoding="utf-8")
                self._judge_process = subprocess.Popen(
                    self._judge_command,
                    cwd=_resolve_cwd(context),
                    env=self._judge_environment(context),
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    # Detach into its own session so the judge survives an
                    # orchestrator restart; resume re-adopts it by recorded pid.
                    start_new_session=True,
                )
                self._judge_handles = (stdout_handle, stderr_handle)
            except (OSError, ConfigError) as exc:
                for handle in (locals().get("stdout_handle"), locals().get("stderr_handle")):
                    if handle is not None:
                        handle.close()
                result.status = "failed"
                result.validity = "execution_invalid"
                result.reason = "beam_judge_start_failed"
                result.failure_summary = str(exc)
                return result
            process_meta = context.suite_dir / "beam-judge-process.json"
            self._judge_meta_path = process_meta
            process_meta.write_text(
                json.dumps(
                    {
                        "pid": self._judge_process.pid,
                        "command": self._judge_command,
                        "phase": "judge",
                        "status": "running",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            result.artifact_paths.extend(
                [str(process_meta.resolve()), str(judge_stdout.resolve()), str(judge_stderr.resolve())]
            )
            result.coverage["background_judge_pending"] = True
            result.coverage["generation_complete"] = True
            result.runner_fingerprint["phase"] = "generation_then_background_judge"
            return result
        attempts: list[SuiteResult] = []
        for attempt in range(1, int(policy.get("max_reruns", 0)) + 2):
            attempt_context = replace(
                context,
                suite_dir=context.suite_dir / f"attempt-{attempt:02d}",
            )
            result = CommandAdapter.run(self, attempt_context)
            attempts.append(result)
            if result.status == "passed" and result.validity == "valid":
                break
            if result.validity == "service_invalid" or result.status == "blocked":
                break
        passing = next(
            (
                result
                for result in attempts
                if result.status == "passed" and result.validity == "valid"
            ),
            None,
        )
        valid_scored = [
            result
            for result in attempts
            if result.validity == "valid" and isinstance(result.metrics.get("score"), (int, float))
        ]
        selected = passing or (
            max(valid_scored, key=lambda item: float(item.metrics["score"]))
            if valid_scored
            else attempts[-1]
        )
        selected.artifact_paths = list(
            dict.fromkeys(path for result in attempts for path in result.artifact_paths)
        )
        selected.coverage.update(
            {
                "rerun_policy": policy,
                "observed_attempts": len(attempts),
                "attempt_scores": [result.metrics.get("score") for result in attempts],
                "attempt_statuses": [result.status for result in attempts],
                "any_pass_recovered": passing is not None and attempts.index(passing) > 0,
            }
        )
        selected.runner_fingerprint.update(
            {"any_pass_max_reruns": str(policy.get("max_reruns", 0))}
        )
        return selected

    def has_pending_work(self, result: SuiteResult) -> bool:
        if result.reason != "beam_judge_pending":
            return False
        if self._judge_process is not None:
            return True
        # A resumed orchestrator holds no Popen handle. The judge was detached
        # into its own session, so it may still be running from the previous
        # process; re-adopt it by recorded pid instead of reporting a verdict
        # over evidence that is still being written.
        return self._recorded_judge_is_running()

    def _recorded_judge_is_running(self) -> bool:
        meta = getattr(self, "_judge_meta_path", None)
        if meta is None or not Path(meta).is_file():
            return False
        try:
            pid = int(json.loads(Path(meta).read_text(encoding="utf-8")).get("pid"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Owned by another user but alive; still pending.
            return True
        return True

    def finalize(self, context: AdapterContext, result: SuiteResult) -> SuiteResult:
        process = self._judge_process
        if process is None and not self._recorded_judge_is_running():
            return result
        if process is None:
            # Detached survivor: wait for it by polling the recorded pid. Its
            # scores are read back from the checkpoint either way.
            if not self._wait_for_detached_judge(context):
                result.status = "failed"
                result.validity = "execution_invalid"
                result.reason = "beam_judge_timeout"
                result.failure_summary = "detached BEAM judge exceeded its timeout"
            return _reread_beam_judge_result(context, result)
        process_status: str | None = None
        try:
            return_code = process.wait(timeout=float(context.suite.config.get("judge_timeout_seconds", 86400)))
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait()
            process_status = "timed_out"
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "beam_judge_timeout"
            result.failure_summary = "background BEAM judge exceeded its timeout"
            return result
        finally:
            if self._judge_handles:
                for handle in self._judge_handles:
                    handle.close()
            process_meta = context.suite_dir / "beam-judge-process.json"
            if process_meta.is_file():
                payload = json.loads(process_meta.read_text(encoding="utf-8"))
                payload.update(
                    {
                        "status": process_status
                        or ("completed" if process.returncode == 0 else "failed"),
                        "returncode": process.returncode,
                        "finished_at": utc_now(),
                    }
                )
                process_meta.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
        final_path = context.suite_dir / "adapter-result.json"
        try:
            payload = json.loads(final_path.read_text(encoding="utf-8"))
            payload["suite_id"] = context.suite.suite_id
            payload["row_ids"] = list(context.suite.row_ids)
            payload["artifact_paths"] = list(
                dict.fromkeys(
                    list(payload.get("artifact_paths") or [])
                    + result.artifact_paths
                    + [str(final_path.resolve())]
                )
            )
            payload["runner_fingerprint"] = {
                **result.runner_fingerprint,
                **dict(payload.get("runner_fingerprint") or {}),
            }
            if return_code != 0 and payload.get("validity") == "valid":
                payload["validity"] = "execution_invalid"
                payload["status"] = "failed"
                payload["reason"] = "beam_judge_runner_failed"
            return SuiteResult.from_dict(payload)
        except (OSError, json.JSONDecodeError, ConfigError, TypeError, ValueError) as exc:
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "beam_judge_result_missing"
            result.failure_summary = f"background BEAM judge did not produce a valid result: {exc}"
            return result


class DeepSWEAdapter(CommandAdapter):
    def build_command(self, context: AdapterContext) -> list[str]:
        configured_concurrency = context.suite.config.get("concurrency", 113)
        max_concurrency = context.suite.config.get(
            "max_concurrency", configured_concurrency
        )
        if (
            isinstance(configured_concurrency, bool)
            or not isinstance(configured_concurrency, int)
            or configured_concurrency < 1
            or isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
            or configured_concurrency > max_concurrency
        ):
            raise ConfigError(
                "runner_contract_invalid",
                "DeepSWE concurrency must define a positive default not above max_concurrency",
            )
        concurrency = configured_concurrency
        if (
            context.deepswe_supplemental_plan is not None
            and context.concurrency_override is not None
        ):
            # Supplemental recovery is operator-authorized and gated on an
            # explicit low --concurrency (see the orchestrator check); the
            # manifest's formal concurrency must not silently replace it.
            concurrency = context.concurrency_override
        rounds = context.deepswe_rounds or context.suite.config.get("rounds", 1)
        transport = str(context.suite.config.get("transport", "local"))
        if transport != "local":
            raise ConfigError(
                "runner_contract_invalid",
                f"{context.suite.suite_id} requires local transport on build-server",
            )
        command = [
            sys.executable,
            "-m",
            "novita_self_test.adapters.deepswe_remote",
            "--output-dir",
            str(context.suite_dir.resolve()),
            "--mode",
            "full",
            "--concurrency",
            str(concurrency),
            "--rounds",
            str(rounds),
        ]
        for key, flag in (
            ("runtime_root", "--runtime-root"),
            ("pier_bin", "--pier-bin"),
            ("expected_deep_swe_commit", "--expected-deep-swe-commit"),
            ("expected_pier_version", "--expected-pier-version"),
            ("expected_tasks", "--expected-tasks"),
            ("max_reruns", "--max-reruns"),
            ("kimi_code_version", "--kimi-code-version"),
        ):
            value = context.suite.config.get(key)
            if value is None:
                raise ConfigError(
                    "runner_contract_invalid",
                    f"{context.suite.suite_id} requires config.{key}",
                )
            if key == "max_reruns" and value != 1:
                raise ConfigError(
                    "runner_contract_invalid",
                    "DeepSWE max_reruns is fixed at 1",
                )
            command.extend([flag, str(value)])
        command.extend(
            [
                "--agent-timeout-multiplier",
                str(context.suite.config.get("agent_timeout_multiplier", 1.0)),
            ]
        )
        retry_policy = context.suite.config.get("rerun_policy") or {}
        if retry_policy != {
            "mode": "error_only",
            "max_reruns": 1,
            "scheduling": "batch_after_formal",
        }:
            raise ConfigError(
                "runner_contract_invalid",
                "DeepSWE retry policy is fixed to error_only/1/batch_after_formal",
            )
        command.extend(["--max-concurrency", str(max_concurrency)])
        if context.deepswe_tasks:
            for task in context.deepswe_tasks:
                command.extend(["--task", task])
        if context.deepswe_supplemental_plan is not None:
            context.suite_dir.mkdir(parents=True, exist_ok=True)
            plan_path = context.suite_dir / "deepswe-supplemental-plan.json"
            plan_path.write_text(
                json.dumps(context.deepswe_supplemental_plan, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            command.extend(["--supplemental-plan", str(plan_path.resolve())])
        if context.finalize_only:
            command.append("--finalize-only")
        return command

    def run(self, context: AdapterContext) -> SuiteResult:
        result = super().run(context)
        if result.status != "passed" or result.validity != "valid":
            return result
        minimum = context.suite.config.get("min_score")
        score = result.metrics.get("score")
        if minimum is not None and not isinstance(score, (int, float)):
            result.status = "failed"
            result.validity = "execution_invalid"
            result.reason = "deepswe_score_missing"
            result.failure_summary = "complete deepSWE result does not contain a numeric score"
        elif isinstance(minimum, (int, float)) and float(score) < float(minimum):
            result.status = "failed"
            result.validity = "valid"
            result.reason = "benchmark_below_tolerance"
            result.failure_summary = (
                f"score {float(score):.6f} is below minimum {float(minimum):.6f}"
            )
        return result


ADAPTERS: dict[str, type[Adapter]] = {
    "fake": FakeAdapter,
    "not_implemented": NotImplementedAdapter,
    "command": CommandAdapter,
    "kvv": PytestAdapter,
    "novita_api": PytestAdapter,
    "walle": PytestAdapter,
    "replay": ReplayAdapter,
    "inspect": InspectAdapter,
    "beam": BeamAdapter,
    "code_bench": CodeBenchAdapter,
    "deepswe": DeepSWEAdapter,
    "stress": CommandAdapter,
}


def adapter_for(name: str, *, runner: ProcessRunner | None = None) -> Adapter:
    adapter_type = ADAPTERS.get(name)
    if adapter_type is None:
        raise ConfigError("adapter_unknown", f"unknown adapter: {name}")
    if issubclass(adapter_type, CommandAdapter):
        return adapter_type(runner=runner)
    return adapter_type()


def command_string(result: SuiteResult) -> str:
    return shlex.join(result.redacted_command)
