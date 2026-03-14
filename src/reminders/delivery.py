"""Reminder delivery for the reminders module.

ReminderDelivery renders a contextual notification message via LLMRenderer,
sends it to Telegram with an inline keyboard, and updates the reminder's
status in the database.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.reminders.models import PlannerLogEntry, ReminderRecord
from src.notifications.service import NotificationService
from src.storage.database import DatabaseManager

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------

_MARK_SENT_SQL = """
UPDATE reminders
SET sent = 1, sent_at = datetime('now')
WHERE id = ?
"""

_INCREMENT_RETRY_SQL = """
UPDATE reminders
SET retry_count = retry_count + 1
WHERE id = ?
"""

_MARK_FAILED_SQL = """
UPDATE reminders
SET failed = 1
WHERE id = ?
"""

_INSERT_LOG_SQL = """
INSERT INTO planner_log
    (id, event_time, action, reminder_id, reason)
VALUES
    (?, ?, ?, ?, ?)
"""

# ---------------------------------------------------------------------------
# Urgency emoji map
# ---------------------------------------------------------------------------

_URGENCY_EMOJI: dict[str, str] = {
    "critical": "\U0001f6a8",  # 🚨
    "high": "\u26a1",          # ⚡
    "normal": "\U0001f514",    # 🔔
    "low": "\U0001f514",       # 🔔
}


# ---------------------------------------------------------------------------
# ReminderDelivery
# ---------------------------------------------------------------------------


class ReminderDelivery:
    """Render and deliver a single :class:`ReminderRecord` to Telegram.

    Parameters
    ----------
    db:
        Shared :class:`~src.storage.database.DatabaseManager` instance used
        for updating reminder state and writing planner-log entries.
    renderer:
        ``LLMRenderer``-compatible object that produces the contextual
        notification message.  Typed as ``Any`` to avoid importing a stub
        module.
    notification:
        :class:`~src.notifications.service.NotificationService`; its
        underlying ``bot`` object is used directly so that an inline
        keyboard can be attached to the message.
    target_chat_id:
        Telegram chat ID to send notifications to.
    calendar_client:
        ``GoogleCalendarClient``-compatible object.  Typed as ``Any`` to
        avoid a circular import; only ``fetch_events`` is called.
    config:
        ``ReminderConfig``-compatible object.  Typed as ``Any`` to avoid a
        circular import; only ``load_lifestyle`` is called.
    """

    def __init__(
        self,
        db: DatabaseManager,
        renderer: Any,  # LLMRenderer — typed as Any to avoid import of stub module
        notification: NotificationService,
        target_chat_id: int,
        calendar_client: Any,
        config: Any,
    ) -> None:
        self._db = db
        self._renderer = renderer
        self._notification = notification
        self._target_chat_id = target_chat_id
        self._calendar_client = calendar_client
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def deliver(self, reminder: ReminderRecord) -> bool:
        """Render and send one reminder.

        Returns ``True`` on success, ``False`` on any failure.  Never raises.

        Steps
        -----
        1. Fetch today's calendar events for renderer context.
        2. Load lifestyle text.
        3. Call ``renderer.render(...)`` for a contextual message.
        4. Build the Telegram message text (urgency-prefixed).
        5. Build the inline keyboard (Done / Snooze 1h / Never again).
        6. Send via the bot's ``send_message`` API.
        7. On success: call ``_mark_sent``.
        8. On failure: increment ``retry_count``; if ``retry_count >= 3``
           call ``_mark_failed``.
        """
        log = logger.bind(reminder_id=reminder.id, urgency=reminder.urgency)

        try:
            # Step 1: calendar context
            try:
                today_events = await self._calendar_client.fetch_events(days_ahead=1)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "reminders.delivery.calendar_fetch_failed",
                    error=str(exc),
                )
                today_events = []

            # Step 2: lifestyle context
            try:
                lifestyle = self._config.load_lifestyle()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "reminders.delivery.lifestyle_load_failed",
                    error=str(exc),
                )
                lifestyle = ""

            # Step 3: render
            rendered_message = await self._renderer.render(
                reminder,
                today_events,
                lifestyle,
                datetime.now(UTC),
            )

            # Step 4: format text with urgency prefix emoji
            emoji = _URGENCY_EMOJI.get(reminder.urgency, "\U0001f514")
            text = f"{emoji} {rendered_message}"

            # Step 5: inline keyboard
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "\u2713 Done",
                            callback_data=f"reminder_cancel:{reminder.id}",
                        ),
                        InlineKeyboardButton(
                            "\u23f0 Snooze 1h",
                            callback_data=f"reminder_suppress:{reminder.id}:today",
                        ),
                        InlineKeyboardButton(
                            "\U0001f6ab Never again",
                            callback_data=f"reminder_suppress:{reminder.id}:permanent",
                        ),
                    ]
                ]
            )

            # Step 6: send — use bot directly so we can attach reply_markup
            await self._notification.bot.send_message(
                chat_id=self._target_chat_id,
                text=text,
                reply_markup=keyboard,
            )

            # Step 7: mark sent
            await self._mark_sent(reminder.id)

            log.info(
                "reminders.delivery.sent",
                hint=reminder.hint[:80],
            )
            return True

        except Exception as exc:  # noqa: BLE001
            error_str = str(exc)
            log.error(
                "reminders.delivery.failed",
                error=error_str,
                exc_type=type(exc).__name__,
            )

            # Step 8: increment retry_count, and mark permanently failed
            # once the threshold is reached.
            try:
                reminder.retry_count += 1
                await self._increment_retry(reminder.id)
                if reminder.retry_count >= 3:
                    await self._mark_failed(reminder.id, error_str)
            except Exception as db_exc:  # noqa: BLE001
                log.error(
                    "reminders.delivery.db_update_failed",
                    error=str(db_exc),
                )

            return False

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _mark_sent(self, reminder_id: str) -> None:
        """Set ``sent=1`` and ``sent_at=now`` for *reminder_id*."""
        async with self._db.get_connection() as conn:
            await conn.execute(_MARK_SENT_SQL, (reminder_id,))
            await conn.commit()

    async def _increment_retry(self, reminder_id: str) -> None:
        """Atomically increment ``retry_count`` in the database."""
        async with self._db.get_connection() as conn:
            await conn.execute(_INCREMENT_RETRY_SQL, (reminder_id,))
            await conn.commit()

    async def _mark_failed(self, reminder_id: str, error: str) -> None:
        """Set ``failed=1`` and append a ``delivery_failed`` planner-log entry."""
        now = datetime.now(UTC)
        log_entry = PlannerLogEntry(
            action="delivery_failed",
            reminder_id=reminder_id,
            reason=error[:300],  # keep within the reason column's soft limit
        )
        async with self._db.get_connection() as conn:
            await conn.execute(_MARK_FAILED_SQL, (reminder_id,))
            await conn.execute(
                _INSERT_LOG_SQL,
                (
                    log_entry.id,
                    now.isoformat(),
                    log_entry.action,
                    log_entry.reminder_id,
                    log_entry.reason,
                ),
            )
            await conn.commit()
