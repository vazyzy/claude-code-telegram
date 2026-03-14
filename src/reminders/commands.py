"""Telegram bot commands for the reminders module."""
from __future__ import annotations

from datetime import timedelta, timezone
from pathlib import Path
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


class RemindersCommandHandler:
    """Handles /reminders command, reminder_cancel: callbacks, and onboarding."""

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
            await query.edit_message_text("Failed to cancel reminder. Please try again.")
            return

        logger.info("reminders.commands.cancelled", reminder_id=reminder_id)
        await query.edit_message_text("Reminder cancelled.")

    async def handle_onboarding_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        lifestyle_path: Optional[Path] = None,
    ) -> None:
        """Handle reminder_onboarding:send_template and reminder_onboarding:skip callbacks.

        When the user taps "Yes, send me the template":
          - Send the lifestyle.md template as a code block message.
          - Remove the inline keyboard from the original offer message.

        When the user taps "Not now":
          - Acknowledge silently and remove the keyboard.
        """
        from src.reminders.planner import _LIFESTYLE_TEMPLATE

        query = update.callback_query
        await query.answer()

        action = query.data.split(":", 1)[1] if ":" in (query.data or "") else ""

        if action == "send_template":
            path_hint = str(lifestyle_path) if lifestyle_path else "lifestyle.md"
            template_msg = (
                f"Here is your lifestyle.md template \u2014 save it to `{path_hint}`:\n\n"
                "```\n" + _LIFESTYLE_TEMPLATE + "\n```"
            )
            await query.message.reply_text(
                template_msg,
                parse_mode="Markdown",
            )
            logger.info(
                "reminders.commands.onboarding_template_sent",
                lifestyle_path=path_hint,
            )
        else:
            # "skip" or any unknown action -- acknowledge silently
            logger.info("reminders.commands.onboarding_skipped")

        # Remove the inline keyboard from the offer message in all cases
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reminders.commands.onboarding_keyboard_remove_failed",
                error=str(exc),
            )
