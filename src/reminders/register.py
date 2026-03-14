"""Registration entry point for the reminders module."""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]


def register_reminders(
    scheduler: AsyncIOScheduler,
    db,          # DatabaseManager
    notification,  # NotificationService
    bot,           # telegram.Bot
    config,        # Settings
) -> None:
    """Wire reminder module into the bot. Called from main.py after all services are initialized."""
    # Jobs registered in T-010 after NightlyPlanner and ReminderScheduler are implemented
    pass
