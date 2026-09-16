"""行为回执的数据契约。原始值必须留，标签只是派生。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OutcomeVerdict:
    outcome: str
    ent_before: float
    ent_after: float
    after_window_minutes: float
