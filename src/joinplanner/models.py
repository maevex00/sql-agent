"""Structured JOIN planner output.

Separated from string rendering on purpose: "how tables connect" (this module,
algorithmic) and "how that becomes SQL text" (compiler, sqlglot-based) were
conflated in the original prototype and are now independently testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class JoinStep:
    table: str
    alias: str
    join_type: Literal["FROM", "LEFT JOIN"]
    on_left_alias: str | None = None
    on_left_key: str | None = None
    on_right_key: str | None = None


@dataclass
class JoinPlan:
    steps: list[JoinStep] = field(default_factory=list)
    table_alias: dict[str, str] = field(default_factory=dict)
