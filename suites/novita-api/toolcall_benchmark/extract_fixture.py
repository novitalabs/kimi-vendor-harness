from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EXPECTED_TASK = "kimi_toolcall"
EXPECTED_DATASET_PATH = "toolcall_benchmark/toolcall_thinking_samples.jsonl"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sample_path(names: set[str], sample_id: str) -> str:
    suffix = f"/{sample_id}_epoch_1.json"
    matches = [name for name in names if name.startswith("samples/") and name.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f"expected one sample artifact for {sample_id}, found {len(matches)}")
    return matches[0]


def extract_fixture(source: Path) -> dict[str, Any]:
    with zipfile.ZipFile(source) as archive:
        header = json.loads(archive.read("header.json"))
        evaluation = header.get("eval") or {}
        if evaluation.get("task") != EXPECTED_TASK:
            raise ValueError(f"expected task {EXPECTED_TASK}, found {evaluation.get('task')}")
        if evaluation.get("task_args", {}).get("dataset_path") != EXPECTED_DATASET_PATH:
            raise ValueError("unexpected source dataset path")
        dataset = evaluation.get("dataset") or {}
        sample_ids = list(dataset.get("sample_ids") or [])
        if int(dataset.get("samples") or 0) != len(sample_ids):
            raise ValueError("source header sample count does not match sample_ids")

        names = set(archive.namelist())
        catalog: dict[str, dict[str, Any]] = {}
        samples = []
        error_sample_ids = []
        for sample_id in sample_ids:
            sample = json.loads(archive.read(_sample_path(names, str(sample_id))))
            metadata = sample.get("metadata") or {}
            tool_refs = []
            for tool in metadata.get("tools") or []:
                encoded = json.dumps(
                    tool, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
                reference = hashlib.sha256(encoded).hexdigest()
                catalog[reference] = tool
                tool_refs.append(reference)
            if sample.get("error") is not None or not sample.get("scores"):
                error_sample_ids.append(str(sample_id))
            samples.append(
                {
                    "id": str(sample_id),
                    "input": sample.get("input") or [],
                    "target": sample.get("target") or "",
                    "tool_refs": tool_refs,
                    "target_arguments_valid_non_empty": bool(
                        metadata.get("repeat_run_target_arguments_valid_non_empty", False)
                    ),
                }
            )

    results = header.get("results") or {}
    score = (results.get("scores") or [{}])[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "artifact_name": source.name,
            "artifact_sha256": sha256_file(source),
            "created": evaluation.get("created"),
            "task": evaluation.get("task"),
            "task_file": evaluation.get("task_file"),
            "dataset_path": EXPECTED_DATASET_PATH,
            "model": evaluation.get("model"),
            "model_generate_config": evaluation.get("model_generate_config"),
            "revision": evaluation.get("revision"),
            "inspect_ai": evaluation.get("packages", {}).get("inspect_ai"),
            "total_samples": results.get("total_samples"),
            "completed_samples": results.get("completed_samples"),
            "accuracy": score.get("metrics", {}).get("accuracy", {}).get("value"),
            "error_sample_ids": error_sample_ids,
        },
        "tool_catalog": catalog,
        "samples": samples,
    }


def write_fixture(payload: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                compressed.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                compressed.write(b"\n")
        os.replace(temporary, output)
        os.chmod(output, 0o644)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a pinned toolcall fixture from .eval")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = extract_fixture(args.source.resolve())
    write_fixture(payload, args.output.resolve())
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "samples": len(payload["samples"]),
                "tools": len(payload["tool_catalog"]),
                "sha256": sha256_file(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
