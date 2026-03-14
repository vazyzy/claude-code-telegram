"""Telegram bot commands for the reminders module."""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import List, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.reminders.models import ReminderRecord
from src.storage.database import DatabaseManager

logger = structlog.get_logger()

UTC7 = timezone(timedelta(hours=7))

_URGENCY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "normal": "🟡",
    "low": "🟢",
}

_LIST_QUERY = """
SELECT * FROM reminders
WHERE sent = 0 AND cancelled = 0 AND expired = 0 AND failed = 0
  AND trigger_day >= date('now')
  AND trigger_day <= date('now', '+7 days')
ORDER BY trigger_day ASC, scheduled_time ASC
"""


def _noop() -> Optional[str]:
    """Keep Optional imported; resolved by type annotation usage below."""
    return None


class RemindersCommandHandler:
    """Handles /reminders command and reminder_cancel: callbacks."""

    def __init__(self, db: DatabaseManager, target_chat_id: int) -> None:
        self.db = db
        self.target_chat_id = target_chat_id

    async def handle_reminders_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List unsent reminders for next 7 days with [Cancel] buttons."""
        logger.info("reminders.commands.list_requested")

        reminders: List[ReminderRecord] = []
        try:
            async with self.db.get_connection() as conn:
                cursor = await conn.execute(_LIST_QUERY)
                rows = await cursor.fetchall()
                reminders = [ReminderRecord.from_row(dict(row)) for row in rows]
        except Exception as exc:
            logger.error("reminders.commands.db_error", error=str(exc))
            await update.message.reply_text(
                "Failed to load reminders. Please try again."
            )
            return

        if not reminders:
            await update.message.reply_text(
                "No reminders scheduled for today or tomorrow 🗓"
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]

        for reminder in reminders:
            urgency_emoji = _URGENCY_EMOJI.get(reminder.urgency, "🟡")

            time_str: str
            if reminder.scheduled_time is not None:
                # Convert UTC-aware datetime to UTC+7 for display
                local_time = reminder.scheduled_time.astimezone(UTC7)
                time_str = local_time.strftime("%H:%M")
            else:
                time_str = "–"

            lines.append(
                f"📅 {reminder.trigger_day} — {reminder.hint}\n"
                f"⏰ {time_str} · {urgency_emoji}"
            )

            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        f"Cancel — {reminder.hint[:30]}",
                        callback_data=f"reminder_cancel:{reminder.id}",
                    )
                ]
            )

        text = "\n\n".join(lines)
        reply_markup = InlineKeyboardMarkup(keyboard_rows)
        await update.message.reply_text(text, reply_markup=reply_markup)

    async def handle_reminder_cancel_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Mark a reminder as cancelled and confirm in the message."""
        query = update.callback_query
        await query.answer()

        reminder_id = query.data.split(":", 1)[1]
        logger.info("reminders.commands.cancel_requested", reminder_id=reminder_id)

        try:
            async with self.db.get_connection() as conn:
                await conn.execute(
                    "UPDATE reminders SET cancelled = 1 WHERE id = ?",
                    (reminder_id,),
                )
                await conn.commit()
        except Exception as exc:
            logger.error(
                "reminders.commands.cancel_db_error",
                reminder_id=reminder_id,
                error=str(exc),
            )
            await query.edit_message_text(
                "Failed to cancel reminder. Please try again."
            )
            return

        logger.info("reminders.commands.cancelled", reminder_id=reminder_id)
        await query.edit_message_text("Reminder cancelled.")
