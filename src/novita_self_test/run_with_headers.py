from __future__ import annotations

import runpy
import sys
from pathlib import Path

from .header_injection import install_extra_headers


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m novita_self_test.run_with_headers SCRIPT [ARGS...]")
    script = Path(sys.argv[1]).resolve()
    install_extra_headers()
    script_parent = str(script.parent)
    if script_parent not in sys.path:
        sys.path.insert(0, script_parent)
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
