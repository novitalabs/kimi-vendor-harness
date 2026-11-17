from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..artifacts import SuiteResult
from ..config import ServiceAccess, SuiteDefinition


@dataclass(frozen=True)
class AdapterContext:
    suite: SuiteDefinition
    service: ServiceAccess
    run_dir: Path
    suite_dir: Path
    repository_root: Path
    selector: str | None = None
    selector_args: tuple[str, ...] = ()
    concurrency_override: int | None = None
    acceptance_mode: str = "develop"
    deepswe_rounds: int | None = None
    deepswe_supplemental_plan: dict[str, object] | None = None
    deepswe_tasks: tuple[str, ...] | None = None
    # Set only by an explicit operator continuation whose purpose is to finalize
    # existing execution records into a terminal result without new traffic.
    finalize_only: bool = False


class ProcessRunner(Protocol):
    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]: ...


class CommandRunner:
    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float | None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **env},
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )


class Adapter:
    def run(self, context: AdapterContext) -> SuiteResult:
        raise NotImplementedError

    def has_pending_work(self, result: SuiteResult) -> bool:
        return False

    def finalize(self, context: AdapterContext, result: SuiteResult) -> SuiteResult:
        return result
