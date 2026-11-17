from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KNOWN_LEAK_PREFIX = b"a3f8b1c7e9d2a5f6" + b"0b8c3d4e7f9a1b2c"
FORBIDDEN = re.compile(
    rb"(?i)(authorization\s*[:=]\s*bearer\s+(?!REDACTED\b)[A-Za-z0-9._~+/=-]{8,}|"
    rb"sk-(?:kimi-[A-Za-z0-9_-]{16,}|-[A-Za-z0-9_-]{16,}|[0-9a-f]{16,})|"
    + re.escape(KNOWN_LEAK_PREFIX)
    + rb")"
)


def test_current_tree_has_no_literal_credentials() -> None:
    files = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    findings = []
    for raw in files:
        if not raw:
            continue
        path = ROOT / raw.decode()
        if path.is_file() and FORBIDDEN.search(path.read_bytes()):
            findings.append(str(path.relative_to(ROOT)))
    assert findings == []
