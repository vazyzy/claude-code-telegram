"""Data models for the reminders module."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class CalendarEvent:
    """A single Google Calendar event."""

    event_id: str
    title: str
    start: datetime
    end: datetime
    description: Optional[str] = field(default=None)
    location: Optional[str] = field(default=None)
    all_day: bool = field(default=False)
