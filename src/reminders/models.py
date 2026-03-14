"""Data models for the reminders module."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Optional
from uuid import uuid4

import structlog

logger = structlog.get_logger()

UrgencyLevel = Literal["critical", "high", "normal", "low"]
SourceType = Literal["calendar", "task", "conversation", "pending_proposal"]
ReminderType = Literal["primary", "advance_prep"]
ScopeType = Literal["permanent", "until"]


def _parse_dt(v: object) -> Optional[datetime]:
    """Parse a datetime value from a SQLite row field.

    Handles three cases:
    - None / falsy  -> None
    - Already a datetime (sqlite3 PARSE_DECLTYPES converter active) -> returned as-is
    - ISO-format string -> parsed via fromisoformat
    """
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        return datetime.fromisoformat(v)
    logger.warning(
        "reminders.models.unexpected_datetime_type",
        value=v,
        type=type(v).__name__,
    )
    return None


@dataclass
class ReminderRecord:
    """A single scheduled reminder, persisted in the ``reminders`` table."""

    hint: str
    trigger_day: str  # YYYY-MM-DD
    id: str = field(default_factory=lambda: str(uuid4()))
    scheduled_time: Optional[datetime] = None  # UTC, timezone-aware
    expires_at: Optional[datetime] = None
    urgency: UrgencyLevel = "normal"
    context_notes: Optional[str] = None  # <=500 chars
    source_event_id: Optional[str] = None
    source_type: SourceType = "calendar"
    reminder_type: ReminderType = "primary"
    reason: Optional[str] = None  # <=300 chars
    sent: bool = False
    sent_at: Optional[datetime] = None
    failed: bool = False
    retry_count: int = 0
    snooze_until: Optional[datetime] = None
    cancelled: bool = False
    expired: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_row(cls, row: dict) -> ReminderRecord:
        """Hydrate from a SQLite row dict."""
        return cls(
            id=row["id"],
            hint=row["hint"],
            trigger_day=row["trigger_day"],
            scheduled_time=_parse_dt(row.get("scheduled_time")),
            expires_at=_parse_dt(row.get("expires_at")),
            urgency=row.get("urgency", "normal"),
            context_notes=row.get("context_notes"),
            source_event_id=row.get("source_event_id"),
            source_type=row.get("source_type", "calendar"),
            reminder_type=row.get("reminder_type", "primary"),
            reason=row.get("reason"),
            sent=bool(row.get("sent", 0)),
            sent_at=_parse_dt(row.get("sent_at")),
            failed=bool(row.get("failed", 0)),
            retry_count=row.get("retry_count", 0),
            snooze_until=_parse_dt(row.get("snooze_until")),
            cancelled=bool(row.get("cancelled", 0)),
            expired=bool(row.get("expired", 0)),
            created_at=_parse_dt(row.get("created_at")) or datetime.now(UTC),
        )


@dataclass
class SuppressionRule:
    """A user-defined suppression rule, persisted in ``suppression_rules``."""

    topic: str  # Natural language; passed verbatim to LLM
    id: str = field(default_factory=lambda: str(uuid4()))
    scope: ScopeType = "permanent"
    expires_at: Optional[datetime] = None  # None for permanent rules
    trigger_count: int = 0
    added_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    added_by: str = "user"

    @classmethod
    def from_row(cls, row: dict) -> SuppressionRule:
        """Hydrate from a SQLite row dict."""
        return cls(
            id=row["id"],
            topic=row["topic"],
            scope=row.get("scope", "permanent"),
            expires_at=_parse_dt(row.get("expires_at")),
            trigger_count=row.get("trigger_count", 0),
            added_at=_parse_dt(row.get("added_at")) or datetime.now(UTC),
            added_by=row.get("added_by", "user"),
        )


@dataclass
class PlannerLogEntry:
    """One entry in the nightly planner audit log, persisted in ``planner_log``."""

    action: str  # planned|skipped|sent|failed|expired|cancelled|parse_error|review_sent
    id: str = field(default_factory=lambda: str(uuid4()))
    event_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    reminder_id: Optional[str] = None
    source_event_id: Optional[str] = None
    reason: Optional[str] = None
    raw_llm_output: Optional[str] = None  # Populated only on parse_error
    notification_sent: Optional[bool] = None
    metadata: Optional[dict] = None

    @classmethod
    def from_row(cls, row: dict) -> PlannerLogEntry:
        """Hydrate from a SQLite row dict.

        The ``metadata`` column is stored as a JSON string in SQLite.
        Callers are responsible for deserialising it before passing the
        row to this method if a ``dict`` is required.
        """
        raw_notif = row.get("notification_sent")
        return cls(
            id=row["id"],
            action=row["action"],
            event_time=_parse_dt(row.get("event_time")) or datetime.now(UTC),
            reminder_id=row.get("reminder_id"),
            source_event_id=row.get("source_event_id"),
            reason=row.get("reason"),
            raw_llm_output=row.get("raw_llm_output"),
            notification_sent=bool(raw_notif) if raw_notif is not None else None,
            metadata=row.get("metadata"),
        )


@dataclass
class CalendarEvent:
    """A single Google Calendar event, as returned by ``GoogleCalendarClient``."""

    event_id: str
    title: str
    start: datetime  # UTC, timezone-aware
    end: datetime  # UTC, timezone-aware
    description: Optional[str] = None
    location: Optional[str] = None
    all_day: bool = False


@dataclass
class ObsidianTask:
    """A single incomplete task extracted from an Obsidian vault ``.md`` file."""

    file_path: str  # Relative to vault root
    line_number: int
    text: str
    due_date: Optional[str] = None  # YYYY-MM-DD
    overdue: bool = False

    @property
    def source_event_id(self) -> str:
        """Stable unique identifier used for deduplication against DB records."""
        return f"obsidian::{self.file_path}::{self.line_number}"


@dataclass
class CommPatterns:
    """Communication/delivery preferences loaded from ``communication-patterns.toml``."""

    quiet_start: str = "22:00"  # HH:MM local (UTC+7)
    quiet_end: str = "07:00"  # HH:MM local (UTC+7)
    critical_categories: list = field(
        default_factory=lambda: [
            "flight",
            "doctor",
            "hospital",
            "visa",
            "passport",
            "surgery",
        ]
    )
    max_per_hour: int = 5
    min_gap_minutes: int = 15
    proactive_enabled: bool = True
    max_proactive_per_session: int = 3
