"""Fixtures for the tool-call contract suite.

The official kvv-k3 conftest (loaded by suites/novita-api/conftest.py) provides the
``--base-url / --api-key / --smoke-model / --think-mode`` options; this file only adds the
suite knobs and an Endpoint built from them.

Environment knobs (all optional):
  TC_SAMPLES      requests per sampled scenario            (default 8)
  TC_CONCURRENCY  parallel requests inside one test        (default 4)
  TC_EFFORT       thinking effort for thinking-on tests    (default low; K3 accepts low/high/max)
  TC_MAX_TOKENS   output cap for sampled requests          (default 4096)
  TC_MARKERS      comma list of structural tokens          (default K3's; re-verify for K3.1)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tc_contract import Endpoint


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@pytest.fixture(scope="session")
def ep(pytestconfig: pytest.Config) -> Endpoint:
    return Endpoint(
        str(pytestconfig.getoption("base_url")),
        str(pytestconfig.getoption("api_key")),
        str(pytestconfig.getoption("smoke_model")),
        str(pytestconfig.getoption("think_mode")),
    )


@pytest.fixture(scope="session")
def samples() -> int:
    return env_int("TC_SAMPLES", 8)


@pytest.fixture(scope="session")
def concurrency() -> int:
    return env_int("TC_CONCURRENCY", 4)


@pytest.fixture(scope="session")
def effort() -> str:
    return os.environ.get("TC_EFFORT", "low")


@pytest.fixture(scope="session")
def max_tokens() -> int:
    return env_int("TC_MAX_TOKENS", 4096)


@pytest.fixture(scope="session")
def observe(pytestconfig: pytest.Config):
    """Append one JSON line to toolcall-contract-observations.jsonl (next to cases.jsonl).

    Observation tests never fail: they record behaviour that is not (yet) a hard contract."""
    try:
        directory = str(pytestconfig.getoption("artifact_dir") or "")
    except ValueError:
        directory = ""
    target = directory or os.environ.get("TC_OBSERVATIONS_DIR", "")
    if not target:
        pytest.skip("observations need --artifact-dir or TC_OBSERVATIONS_DIR")
    path = Path(target) / "toolcall-contract-observations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(name: str, data: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"observation": name, **data}, ensure_ascii=False) + "\n")

    return write
