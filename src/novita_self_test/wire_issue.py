"""Internal wire evidence categories are stable; diagnostic prose is not control flow."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class WireIssue:
    code: str
    message: str
    category: Literal["protocol", "incomplete", "transport"] = "protocol"
