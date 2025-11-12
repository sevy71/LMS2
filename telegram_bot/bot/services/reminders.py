"""Placeholder for reminder automation surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass(slots=True)
class ReminderPlan:
    """Represents reminders ready to send through Telegram."""

    round_id: int
    reminders: List[Dict[str, Any]]


def prepare_reminders(payload: Dict[str, Any]) -> ReminderPlan:
    """Transform LMS reminder payloads into bot-specific structures."""
    return ReminderPlan(
        round_id=payload.get("round_id", 0),
        reminders=payload.get("due_reminders", []),
    )
