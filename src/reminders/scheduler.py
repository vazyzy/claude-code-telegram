"""Reminder scheduler for the reminders module.

ReminderScheduler is called every 60 seconds by APScheduler.  It fetches
due reminders from the database, applies quiet-hours logic, and hands each
eligible reminder off to the ReminderDelivery layer.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import structlog

from src.reminders.config import CommPatterns, ReminderConfig
from src.reminders.models import ReminderRecord
from src.storage.database import DatabaseManager

logger = structlog.get_logger()

# UTC+7 offset used for quiet-hours comparisons
_UTC7 = timezone(timedelta(hours=7))

# SQL that fetches the next batch of due, unprocessed reminders.
# Ordering: critical/high urgency first; within the same urgency tier,
# the earliest-scheduled reminder fires first.  Hard cap of 10 per tick
# prevents a backlog from creating a burst.
_FETCH_DUE_SQL = """
SELECT *
FROM reminders
WHERE sent = 0
  AND failed = 0
  AND cancelled = 0
  AND expired = 0
  AND scheduled_time <= datetime('now')
  AND (snooze_until IS NULL OR snooze_until <= datetime('now'))
ORDER BY
    CASE urgency
        WHEN 'critical' THEN 0
        WHEN 'high'     THEN 1
        WHEN 'normal'   THEN 2
        WHEN 'low'      THEN 3
        ELSE 4
    END ASC,
    scheduled_time ASC
LIMIT 10
"""

_UPDATE_SNOOZE_SQL = """
UPDATE reminders
SET snooze_until = ?
WHERE id = ?
"""

_MARK_EXPIRED_SQL = """
UPDATE reminders
SET expired = 1
WHERE id = ?
"""


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _next_after_quiet_end(quiet_end: str, now: datetime) -> datetime:
    """Return the next UTC datetime at which the quiet window ends.

    Parameters
    ----------
    quiet_end:
        Quiet-window end time as ``HH:MM`` in UTC+7.
    now:
        The current moment as a UTC-aware :class:`~datetime.datetime`.

    The function converts *now* to UTC+7, finds the next wall-clock
    occurrence of ``quiet_end`` in that timezone, and converts the result
    back to UTC.

    Examples
    --------
    If it is currently 23:30 UTC+7 and ``quiet_end`` is ``"07:00"``, the
    function returns the datetime for 07:00 the *next* calendar day in
    UTC+7 (converted to UTC).

    If it is currently 06:00 UTC+7 and ``quiet_end`` is ``"07:00"``, the
    function returns 07:00 *today* in UTC+7 (converted to UTC).
    """
    h, m = (int(part) for part in quiet_end.split(":"))

    now_utc7 = now.astimezone(_UTC7)

    # Candidate: quiet_end today (UTC+7)
    candidate = now_utc7.replace(hour=h, minute=m, second=0, microsecond=0)

    # If the candidate is not strictly in the future, push it to tomorrow.
    if candidate <= now_utc7:
        candidate += timedelta(days=1)

    return candidate.astimezone(UTC)


# ---------------------------------------------------------------------------
# ReminderScheduler
# ---------------------------------------------------------------------------


class ReminderScheduler:
    """Polls the database every 60 s for due reminders and fires delivery.

    Parameters
    ----------
    db:
        Shared :class:`~src.storage.database.DatabaseManager` instance.
    delivery:
        A :class:`~src.reminders.delivery.ReminderDelivery`-compatible
        object.  Typed as ``Any`` to avoid a circular import — only
        ``deliver(reminder)`` is called.
    config:
        Module-level :class:`~src.reminders.config.ReminderConfig`.
    """

    def __init__(
        self,
        db: DatabaseManager,
        delivery: Any,
        config: ReminderConfig,
    ) -> None:
        self._db = db
        self._delivery = delivery
        self._config = config

    # ------------------------------------------------------------------
    # Public API (called by APScheduler)
    # ------------------------------------------------------------------

    async def check_due(self) -> None:
        """Fetch due reminders and attempt delivery.

        Steps per reminder:
        1. Load communication patterns (cached on disk; cheap).
        2. Compute the current time in UTC+7.
        3. If the reminder is expired, mark it and skip.
        4. If within quiet hours AND urgency is not ``'critical'``,
           reschedule to after the quiet window ends and skip.
        5. Otherwise call ``delivery.deliver(reminder)``.  Any exception
           from delivery is caught and logged; the loop continues.
        """
        log = logger.bind(method="check_due")
        log.debug("reminders.scheduler.tick")

        try:
            reminders = await self._fetch_due_reminders()
        except Exception as exc:
            log.error("reminders.scheduler.fetch_failed", exc=str(exc))
            return

        if not reminders:
            log.debug("reminders.scheduler.nothing_due")
            return

        log.info("reminders.scheduler.due_count", count=len(reminders))

        patterns: CommPatterns = self._config.load_comms_patterns()
        now_utc: datetime = datetime.now(UTC)
        now_utc7: datetime = now_utc.astimezone(_UTC7)

        for reminder in reminders:
            await self._process_reminder(reminder, patterns, now_utc, now_utc7)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_due_reminders(self) -> list[ReminderRecord]:
        """Execute the due-reminder query and return hydrated records."""
        async with self._db.get_connection() as conn:
            cursor = await conn.execute(_FETCH_DUE_SQL)
            rows = await cursor.fetchall()

        # aiosqlite.Row supports key-based access but is not a plain dict;
        # convert so that ReminderRecord.from_row() receives a real mapping.
        return [ReminderRecord.from_row(dict(row)) for row in rows]

    async def _process_reminder(
        self,
        reminder: ReminderRecord,
        patterns: CommPatterns,
        now_utc: datetime,
        now_utc7: datetime,
    ) -> None:
        """Apply all business rules to a single reminder and deliver if eligible."""
        log = logger.bind(
            reminder_id=reminder.id,
            urgency=reminder.urgency,
            scheduled_time=reminder.scheduled_time.isoformat()
            if reminder.scheduled_time
            else None,
        )

        # Step 1: expiry check — mark expired and bail if past expires_at
        if reminder.expires_at is not None:
            # Normalise to UTC-aware for comparison
            expires_utc = self._ensure_utc(reminder.expires_at)
            if now_utc > expires_utc:
                log.info("reminders.scheduler.expired", expires_at=expires_utc.isoformat())
                await self._mark_expired(reminder.id)
                return

        # Step 2: quiet-hours check
        if self._config.is_within_quiet_hours(now_utc7) and reminder.urgency != "critical":
            next_delivery = _next_after_quiet_end(
                patterns.quiet_hours.end, now_utc
            )
            # T-008: if rescheduled time exceeds expires_at, mark expired instead of snoozing
            if reminder.expires_at is not None:
                expires_utc = self._ensure_utc(reminder.expires_at)
                if next_delivery > expires_utc:
                    log.info(
                        "reminders.scheduler.snooze_past_expiry",
                        next_delivery=next_delivery.isoformat(),
                        expires_at=expires_utc.isoformat(),
                    )
                    await self._mark_expired(reminder.id, reason="snooze would exceed expires_at")
                    return
            log.info(
                "reminders.scheduler.quiet_hours_snooze",
                snooze_until=next_delivery.isoformat(),
            )
            await self._snooze(reminder.id, next_delivery)
            return

        # Step 3: deliver
        try:
            await self._delivery.deliver(reminder)
            log.info("reminders.scheduler.delivered")
        except Exception as exc:
            log.error(
                "reminders.scheduler.delivery_error",
                exc=str(exc),
                exc_type=type(exc).__name__,
            )
            # Continue to next reminder — do not crash the scheduler tick.

    async def _snooze(self, reminder_id: str, until: datetime) -> None:
        """Persist a ``snooze_until`` timestamp for *reminder_id*."""
        # Store as UTC ISO-8601 string (matches the TEXT column type)
        until_str = until.isoformat()
        async with self._db.get_connection() as conn:
            await conn.execute(_UPDATE_SNOOZE_SQL, (until_str, reminder_id))
            # T-008: log every state transition to planner_log
            await conn.execute(
                "INSERT INTO planner_log (id, event_time, action, reminder_id, reason)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    datetime.now(UTC).isoformat(),
                    "snoozed",
                    reminder_id,
                    f"quiet hours until {until_str}",
                ),
            )
            await conn.commit()

    async def _mark_expired(self, reminder_id: str, reason: str = "past expires_at") -> None:
        """Set ``expired = 1`` for *reminder_id*."""
        async with self._db.get_connection() as conn:
            await conn.execute(_MARK_EXPIRED_SQL, (reminder_id,))
            # T-008: log every state transition to planner_log
            await conn.execute(
                "INSERT INTO planner_log (id, event_time, action, reminder_id, reason)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    datetime.now(UTC).isoformat(),
                    "expired",
                    reminder_id,
                    reason,
                ),
            )
            await conn.commit()

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _ensure_utc(dt: datetime) -> datetime:
        """Return *dt* as a UTC-aware datetime.

        If *dt* is naive (no tzinfo), it is assumed to already be UTC and
        is made aware by attaching the UTC timezone.  This matches the
        project convention where all datetimes are stored as UTC ISO-8601
        strings and ``from_row()`` parses them without an explicit offset.
        """
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
