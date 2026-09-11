from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentAnswer:
    agent: str
    answer: str
    sources: list[dict[str, Any]] = field(default_factory=list)
