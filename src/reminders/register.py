"""Registration entry point for the reminders module."""
from __future__ import annotations

from typing import Optional

import anthropic
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from src.reminders.calendar_client import GoogleCalendarClient
from src.reminders.config import ReminderConfig
from src.reminders.delivery import ReminderDelivery
from src.reminders.planner import NightlyPlanner
from src.reminders.renderer import LLMRenderer
from src.reminders.scheduler import ReminderScheduler

logger = structlog.get_logger()


def register_reminders(
    scheduler: Optional[AsyncIOScheduler],
    db,          # DatabaseManager
    notification,  # NotificationService
    bot,           # telegram.Bot
    config,        # Settings
) -> None:
    """Wire reminder module into the bot. Called from main.py after all services are initialized.

    Instantiates all reminder sub-components and registers two APScheduler jobs:
    - nightly_reminder_planner: CronTrigger at 16:00 UTC (23:00 UTC+7) with 5-min misfire grace
    - reminder_delivery_check: IntervalTrigger every 60 seconds

    If scheduler is None (scheduler feature flag disabled), this function is a no-op
    so that the reminder module does not crash startup when the scheduler is absent.
    """
    if scheduler is None:
        logger.warning(
            "reminders.register.no_scheduler",
            reason="APScheduler instance is None — reminder jobs not registered",
        )
        return

    # 1. ReminderConfig — reads lifestyle.md and communication-patterns.toml
    reminder_config = ReminderConfig(
        lifestyle_md_path=config.lifestyle_md_path,
        comms_patterns_path=config.reminder_comms_patterns_path,
    )

    # 2. GoogleCalendarClient — wraps gws CLI binary
    calendar = GoogleCalendarClient(
        gws_binary=config.gws_binary_path,
        calendar_id=config.google_calendar_id,
    )

    # 3. SuppressionService — suppression.py is a stub in this version;
    #    pass None so NightlyPlanner falls back to its defensive hasattr guard.
    suppression = None  # TODO: replace once SuppressionService is implemented

    # 4. Shared Anthropic client — reads ANTHROPIC_API_KEY from environment automatically
    anthropic_client = anthropic.AsyncAnthropic()

    # 5. LLMRenderer — renders contextual reminder messages via Claude Haiku
    renderer = LLMRenderer(anthropic_client=anthropic_client)

    # 6. ReminderDelivery — renders and sends individual reminders to Telegram
    delivery = ReminderDelivery(
        db=db,
        renderer=renderer,
        notification=notification,
        target_chat_id=config.reminder_target_chat_id,
        calendar_client=calendar,
        config=reminder_config,
    )

    # 7. ReminderScheduler — polls DB every 60 s and hands due reminders to delivery
    scheduler_svc = ReminderScheduler(
        db=db,
        delivery=delivery,
        config=reminder_config,
    )

    # 8. NightlyPlanner — runs once per night to fetch events and plan reminders via LLM
    planner = NightlyPlanner(
        db=db,
        calendar=calendar,
        config=reminder_config,
        suppression=suppression,
        bot=bot,
        anthropic_client=anthropic_client,
        target_chat_id=config.reminder_target_chat_id,
        scheduler=scheduler,
    )

    # Register job 1: nightly planner at 16:00 UTC (= 23:00 UTC+7)
    # misfire_grace_time=300 allows the job to still fire up to 5 minutes late
    scheduler.add_job(
        planner.run,
        trigger=CronTrigger(hour=16, minute=0, timezone="UTC"),
        id="nightly_reminder_planner",
        misfire_grace_time=300,
        replace_existing=True,
    )

    # Register job 2: delivery poll every 60 seconds
    scheduler.add_job(
        scheduler_svc.check_due,
        trigger=IntervalTrigger(seconds=60),
        id="reminder_delivery_check",
        replace_existing=True,
    )

    logger.info(
        "reminders.register.jobs_registered",
        nightly_planner_cron="16:00 UTC",
        delivery_poll_interval_seconds=60,
        target_chat_id=config.reminder_target_chat_id,
        suppression_active=False,
    )
