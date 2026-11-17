from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from novita_self_test.header_injection import configured_extra_headers


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def execute(*, concurrency: int, hours: float, output_dir: Path) -> dict[str, Any]:
    base_url = os.environ["KIMI_BASE_URL"].rstrip("/")
    model = os.environ["KIMI_MODEL"]
    api_key = os.environ["KIMI_API_KEY"]
    deadline = time.monotonic() + hours * 3600
    counts = {"requests": 0, "passed": 0, "failed": 0, "credential_errors": 0}
    latencies: list[float] = []
    stop = asyncio.Event()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(configured_extra_headers())
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 8,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        async def worker() -> None:
            while time.monotonic() < deadline and not stop.is_set():
                started = time.monotonic()
                status = None
                try:
                    response = await client.post(
                        f"{base_url}/chat/completions", headers=headers, json=payload
                    )
                    status = response.status_code
                except httpx.HTTPError:
                    pass
                latencies.append((time.monotonic() - started) * 1000)
                counts["requests"] += 1
                if status == 200:
                    counts["passed"] += 1
                else:
                    counts["failed"] += 1
                    if status in {401, 403}:
                        counts["credential_errors"] += 1
                        stop.set()

        await asyncio.gather(*(worker() for _ in range(concurrency)))

    elapsed = hours * 3600 - max(0.0, deadline - time.monotonic())
    completed_duration = elapsed >= hours * 3600 * 0.999
    credential_invalid = bool(counts["credential_errors"])
    service_invalid = counts["requests"] > 0 and counts["passed"] == 0
    status = "passed" if completed_duration and not counts["failed"] else "failed"
    validity = "service_invalid" if credential_invalid or service_invalid else "valid"
    result = {
        "schema_version": 1,
        "suite_id": "stress-24h",
        "row_ids": [],
        "status": status,
        "validity": validity,
        "counts": counts,
        "metrics": {
            "elapsed_seconds": round(elapsed, 3),
            "latency_ms_p50": statistics.median(latencies) if latencies else None,
            "latency_ms_max": max(latencies) if latencies else None,
            "success_rate": counts["passed"] / counts["requests"] if counts["requests"] else 0,
        },
        "redacted_command": [],
        "artifact_paths": [],
        "failure_summary": None if status == "passed" else "stress duration or request success contract failed",
        "runner_fingerprint": {
            "runner": "nvst-stress",
            "concurrency": str(concurrency),
            "hours": str(hours),
        },
        "coverage": {"observed_trials": counts["requests"]},
        "reason": None if status == "passed" else (
            "credential_invalid" if credential_invalid else "stress_contract_failed"
        ),
        "started_at": None,
        "finished_at": now(),
    }
    path = output_dir / "stress-summary.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["artifact_paths"] = [str(path.resolve())]
    (output_dir / "adapter-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args(argv)
    if args.concurrency < 50 or args.hours < 24:
        parser.error("formal K3 stress requires concurrency >= 50 and hours >= 24")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = asyncio.run(
        execute(concurrency=args.concurrency, hours=args.hours, output_dir=args.output_dir)
    )
    return 0 if result["validity"] == "valid" else 1


if __name__ == "__main__":
    raise SystemExit(main())
