"""Sequence deduplication and stale-context tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import Frame


@dataclass
class ContextTracker:
    stale_after: float = 8.0
    last_sequence: int | None = None
    last_context: dict[str, Any] | None = None
    last_advance: float | None = None
    stale_reported: bool = False

    def ingest(self, frame: Frame, now: float) -> dict[str, Any] | None:
        if frame.sequence == self.last_sequence:
            return None
        changed = frame.context != self.last_context
        self.last_sequence = frame.sequence
        self.last_advance = now
        self.stale_reported = False
        self.last_context = dict(frame.context)
        return dict(frame.context) if changed else None

    def stale_event(self, now: float) -> float | None:
        if self.last_advance is None:
            return None
        age = now - self.last_advance
        if age >= self.stale_after and not self.stale_reported:
            self.stale_reported = True
            return age
        return None

    def current_age(self, now: float) -> float | None:
        return None if self.last_advance is None else max(0.0, now - self.last_advance)
