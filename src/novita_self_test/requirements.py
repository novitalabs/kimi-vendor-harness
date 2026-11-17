from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigError, ModelProfile, find_secret, workspace_root

SOURCE_TAG_RE = re.compile(r"<source\s+([^>]+?)/?>")
ATTRIBUTE_RE = re.compile(r"([A-Za-z0-9_-]+)=\"([^\"]*)\"")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _attachments(content: str) -> list[dict[str, Any]]:
    locked = []
    for match in SOURCE_TAG_RE.finditer(content):
        attrs = dict(ATTRIBUTE_RE.findall(match.group(1)))
        if not attrs.get("token") or not attrs.get("name"):
            continue
        item: dict[str, Any] = {"name": attrs["name"], "token": attrs["token"]}
        if attrs.get("size", "").isdigit():
            item["size"] = int(attrs["size"])
        if attrs.get("mime"):
            item["mime"] = attrs["mime"]
        locked.append(item)
    return locked


def _markdown_tables(content: str) -> list[dict[str, Any]]:
    lines = content.splitlines()
    tables = []
    index = 0
    while index + 1 < len(lines):
        header = lines[index].strip()
        separator = lines[index + 1].strip()
        if not (
            header.startswith("|")
            and separator.startswith("|")
            and all(
                re.fullmatch(r":?-{3,}:?", cell.strip())
                for cell in separator.strip("|").split("|")
            )
        ):
            index += 1
            continue
        rows = []
        cursor = index + 2
        while cursor < len(lines) and lines[cursor].strip().startswith("|"):
            rows.append(
                [
                    re.sub(r"\s+", " ", cell.strip())
                    for cell in lines[cursor].strip().strip("|").split("|")
                ]
            )
            cursor += 1
        tables.append(
            {
                "context": "\n".join(lines[max(0, index - 3) : index]),
                "header": [
                    re.sub(r"\s+", " ", cell.strip())
                    for cell in header.strip("|").split("|")
                ],
                "rows": rows,
            }
        )
        index = cursor
    return tables


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict[str, Any]]]] = []
        self._table: list[list[dict[str, Any]]] | None = None
        self._row: list[dict[str, Any]] | None = None
        self._cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table" and self._table is None:
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            values = dict(attrs)
            self._cell = {
                "text": "",
                "rowspan": int(values.get("rowspan") or 1),
                "colspan": int(values.get("colspan") or 1),
            }
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += "\n"
        elif tag == "del" and self._cell is not None:
            self._cell["text"] += "~~"

    def handle_endtag(self, tag: str) -> None:
        if tag == "del" and self._cell is not None:
            self._cell["text"] += "~~"
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data


def _html_tables(content: str) -> list[dict[str, Any]]:
    parser = _HTMLTableParser()
    parser.feed(content)
    tables = []
    for raw_rows in parser.tables:
        grid: dict[tuple[int, int], str] = {}
        width = 0
        for row_index, raw_row in enumerate(raw_rows):
            column = 0
            for cell in raw_row:
                while (row_index, column) in grid:
                    column += 1
                text = re.sub(r"\s+", " ", str(cell["text"]).strip())
                rowspan = max(1, int(cell["rowspan"]))
                colspan = max(1, int(cell["colspan"]))
                for row_offset in range(rowspan):
                    for column_offset in range(colspan):
                        grid[(row_index + row_offset, column + column_offset)] = text
                column += colspan
                width = max(width, column)
        expanded = [
            [grid.get((row_index, column), "") for column in range(width)]
            for row_index in range(len(raw_rows))
        ]
        if expanded:
            tables.append({"context": "", "header": expanded[0], "rows": expanded[1:]})
    return tables


def _structured_rows_sha256(content: str) -> str | None:
    tables = [*_markdown_tables(content), *_html_tables(content)]
    selected = next(
        (
            table
            for table in tables
            if "K3-vendor-test-result" in table["context"]
            or "K3-vendor-test-result" in " ".join(table["header"])
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                table
                for table in tables
                if {row[0].strip("~ ") for row in table["rows"] if row}
                >= {str(index) for index in range(15)}
            ),
            None,
        )
    if selected is None:
        return None
    canonical = {"header": selected["header"], "rows": selected["rows"]}
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _python_snapshot_sha256(content: str) -> str | None:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", content, flags=re.DOTALL | re.IGNORECASE)
    candidates = [block for block in blocks if "pytest" in block and "test_stream_spec" in block]
    if not candidates:
        return None
    snapshot = "".join(block if block.endswith("\n") else f"{block}\n" for block in candidates)
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def _xml_python_snapshot_sha256(content: str) -> str | None:
    blocks = []
    for attrs, body in re.findall(
        r"<pre\b([^>]*)>\s*<code>(.*?)</code>\s*</pre>",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        if not re.search(r'\blang=["\'](?:python|py)["\']', attrs, flags=re.IGNORECASE):
            continue
        block = unescape(re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE))
        if "pytest" in block and "test_stream_spec" in block:
            blocks.append(block)
    if not blocks:
        return None
    snapshot = "".join(block if block.endswith("\n") else f"{block}\n" for block in blocks)
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


def fetch_doc(doc: str) -> dict[str, Any]:
    def fetch_content(doc_format: str, *extra: str) -> dict[str, Any]:
        process = subprocess.run(
            [
                "lark-cli",
                "docs",
                "+fetch",
                "--doc",
                doc,
                "--doc-format",
                doc_format,
                "--as",
                "user",
                *extra,
                "--format",
                "json",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise ConfigError(
                "requirements_fetch_failed",
                process.stderr.strip() or "lark document fetch failed",
            )
        try:
            payload = json.loads(process.stdout)
            document = payload["data"]["document"]
            return {
                "content": str(document["content"]),
                "revision": int(document["revision_id"]),
                "token": str(document["document_id"]),
            }
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ConfigError(
                "requirements_fetch_invalid", f"unexpected lark response: {exc}"
            ) from exc

    document = fetch_content("markdown")
    content = document["content"]
    snapshot_sha256 = _python_snapshot_sha256(content)
    if snapshot_sha256 is None and "测试自助验证" in content:
        snapshot_document = fetch_content(
            "xml",
            "--scope",
            "keyword",
            "--keyword",
            "测试自助验证",
            "--context-after",
            "3",
            "--detail",
            "with-ids",
        )
        if (
            snapshot_document["token"] != document["token"]
            or snapshot_document["revision"] != document["revision"]
        ):
            raise ConfigError(
                "requirements_fetch_changed",
                "document revision changed while fetching the executable snapshot",
            )
        snapshot_sha256 = _xml_python_snapshot_sha256(snapshot_document["content"])

    return {
        "doc_token": document["token"],
        "doc_revision": document["revision"],
        "content_sha256": _sha256_text(content),
        "attachment_lock": _attachments(content),
        "structured_rows_sha256": _structured_rows_sha256(content),
        "snapshot_sha256": snapshot_sha256,
        "_content": content,
    }


def _rows_digest(profile: ModelProfile) -> str:
    path = profile.directory / "requirements" / "rows.yaml"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _merge_attachment_locks(
    current: list[dict[str, Any]], candidate: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_name = {
        item.get("name"): item
        for item in current
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    return [{**by_name.get(item["name"], {}), **item} for item in candidate]


def refresh_requirements(
    model_id: str,
    *,
    doc: str,
    stream_doc: str | None = None,
    accept_table_sha: str | None = None,
    apply: bool = False,
    root: Path | None = None,
    fetcher=fetch_doc,
) -> dict[str, Any]:
    root = (root or workspace_root()).resolve()
    profile = ModelProfile.load(model_id, root=root, allow_blocked=True)
    profile.require_execution_site()
    candidate = dict(fetcher(doc))
    source_path = profile.directory / "requirements" / "source.yaml"
    current = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    if not isinstance(current, dict):
        raise ConfigError("requirements_snapshot_invalid", f"invalid source file: {source_path}")
    if candidate.get("attachment_lock"):
        candidate["attachment_lock"] = _merge_attachment_locks(
            list(current.get("attachment_lock") or []), candidate["attachment_lock"]
        )
    changes = {}
    for key in ("doc_token", "doc_revision", "content_sha256", "attachment_lock"):
        before = current.get(key)
        after = candidate.get(key)
        if before != after and not (key == "attachment_lock" and not after):
            changes[key] = {"before": before, "after": after}

    rows_digest = _rows_digest(profile)
    table_contract = current.get("table_contract") or {}
    current_table_sha = table_contract.get("structured_rows_sha256")
    candidate_table_sha = candidate.get("structured_rows_sha256")
    if table_contract and candidate_table_sha is None and candidate.get("_content"):
        raise ConfigError(
            "requirements_table_missing",
            "the pinned acceptance table was not found in the fetched document",
        )
    if not table_contract:
        rows_changed: bool | None = False
    elif current_table_sha is None and candidate.get("content_sha256") == current.get("content_sha256"):
        current_table_sha = candidate_table_sha
        rows_changed = False
    elif current_table_sha is None:
        rows_changed = None
    else:
        rows_changed = candidate_table_sha != current_table_sha

    stream_result = None
    stream_contract = current.get("stream_spec_contract")
    stream_candidate = None
    if isinstance(stream_contract, dict):
        stream_token = (
            stream_doc
            or profile.profile.get("requirements", {}).get("stream_spec_doc_token")
            or stream_contract.get("doc_token")
        )
        if not isinstance(stream_token, str) or not stream_token:
            raise ConfigError("requirements_snapshot_invalid", "stream spec document token is missing")
        stream_candidate = dict(fetcher(stream_token))
        snapshot_path = root / str(stream_contract.get("snapshot_path") or "")
        if not snapshot_path.is_file():
            raise ConfigError(
                "profile_file_missing", f"stream spec snapshot not found: {snapshot_path}"
            )
        local_snapshot_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        pinned_snapshot_sha = stream_contract.get("sha256")
        if local_snapshot_sha != pinned_snapshot_sha:
            raise ConfigError(
                "stream_snapshot_pin_mismatch",
                "local Stream snapshot does not match the committed source pin",
            )
        candidate_snapshot_sha = stream_candidate.get("snapshot_sha256")
        same_revision = (
            stream_candidate.get("doc_revision") == stream_contract.get("doc_revision")
            and stream_candidate.get("doc_token") == stream_contract.get("doc_token")
        )
        if candidate_snapshot_sha is None and same_revision:
            candidate_snapshot_sha = local_snapshot_sha
        executable_changed = (
            None
            if candidate_snapshot_sha is None
            else candidate_snapshot_sha != pinned_snapshot_sha
        )
        stream_result = {
            "doc_token": {
                "before": stream_contract.get("doc_token"),
                "after": stream_candidate.get("doc_token"),
            },
            "doc_revision": {
                "before": stream_contract.get("doc_revision"),
                "after": stream_candidate.get("doc_revision"),
            },
            "document_content_sha256": {
                "before": stream_contract.get("document_content_sha256"),
                "after": stream_candidate.get("content_sha256"),
            },
            "snapshot_sha256": {
                "before": pinned_snapshot_sha,
                "after": candidate_snapshot_sha,
            },
            "executable_changed": executable_changed,
        }

    result = {
        "schema_version": 1,
        "model": model_id,
        "source_path": str(source_path.resolve()),
        "changes": changes,
        "rows": {
            "before_sha256": rows_digest,
            "candidate_sha256": rows_digest,
            "before_structured_sha256": current_table_sha,
            "candidate_structured_sha256": candidate_table_sha,
            "changed": rows_changed,
        },
        "stream_spec": stream_result,
        "applied": False,
    }
    if not apply:
        return result

    stored_rows_sha = table_contract.get("rows_file_sha256") if table_contract else None
    if rows_changed is None:
        raise ConfigError(
            "requirements_rows_unpinned",
            "cannot apply a changed document before bootstrapping its structured table pin",
        )
    if rows_changed:
        if accept_table_sha != candidate_table_sha:
            raise ConfigError(
                "requirements_rows_changed",
                "table rows changed; rerun --apply with the exact --accept-table-sha after review",
            )
        if stored_rows_sha is not None and stored_rows_sha == rows_digest:
            raise ConfigError(
                "requirements_rows_not_updated",
                "rows.yaml must be updated before accepting the changed table digest",
            )
    if stream_result and stream_result["executable_changed"] is None:
        raise ConfigError(
            "stream_snapshot_refresh_required",
            "Stream document changed but no authoritative executable snapshot was found",
        )
    if stream_result and stream_result["executable_changed"]:
        candidate_snapshot_sha = stream_result["snapshot_sha256"]["after"]
        snapshot_path = root / str(stream_contract["snapshot_path"])
        if hashlib.sha256(snapshot_path.read_bytes()).hexdigest() != candidate_snapshot_sha:
            raise ConfigError(
                "stream_snapshot_refresh_required",
                "update the local Stream snapshot to the fetched executable before --apply",
            )

    for key in ("doc_token", "doc_revision", "content_sha256", "attachment_lock"):
        if key in candidate and candidate[key] is not None:
            current[key] = candidate[key]
    if table_contract and candidate_table_sha:
        table_contract["structured_rows_sha256"] = candidate_table_sha
        table_contract["rows_file_sha256"] = rows_digest
        current["table_contract"] = table_contract
    if stream_result and stream_candidate is not None:
        stream_contract["doc_token"] = stream_candidate["doc_token"]
        stream_contract["doc_revision"] = stream_candidate["doc_revision"]
        stream_contract["document_content_sha256"] = stream_candidate.get("content_sha256")
        candidate_snapshot_sha = stream_result["snapshot_sha256"]["after"]
        if candidate_snapshot_sha:
            stream_contract["sha256"] = candidate_snapshot_sha
        current["stream_spec_contract"] = stream_contract
        current["stream_spec_revision"] = stream_candidate["doc_revision"]
    current["status"] = "ready"
    current["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    revision_format = current.get("snapshot_revision_format")
    if isinstance(revision_format, str):
        try:
            current["snapshot_revision"] = revision_format.format_map(current)
        except KeyError as exc:
            raise ConfigError(
                "requirements_snapshot_invalid", f"snapshot revision format needs {exc}"
            ) from exc
    else:
        current["snapshot_revision"] = f"{model_id}-docx-{candidate['doc_revision']}"
    secret = find_secret(current)
    if secret:
        raise ConfigError("credential_material_forbidden", secret)
    source_path.write_text(
        yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    result["applied"] = True
    result["snapshot_revision"] = current["snapshot_revision"]
    return result
