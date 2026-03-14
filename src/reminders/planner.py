"""Nightly planner for the reminders module.

NightlyPlanner runs once per night (via APScheduler) to:
1. Validate Google Calendar auth
2. Fetch upcoming calendar events
3. Build a planning prompt from events, lifestyle, suppression rules,
   and communication preferences
4. Call Claude with the ``schedule_reminders`` structured-output tool
   (tool_choice="any" forces a single tool_use block in the response)
5. Parse and validate the tool_use block — raise PlannerParseError on
   any structural problem, which aborts the run without DB writes
6. Deduplicate by (source_event_id, reminder_type, trigger_day) before
   inserting new ReminderRecord rows
7. On any unhandled exception: log to planner_log and send a Telegram
   failure notification to target_chat_id

T-011 behaviour (GoogleAuthError):
  If self.scheduler is available, schedule a one-shot retry 5 min later
  using APScheduler's "date" trigger; then send the failure notification.
  On any other exception: notify immediately.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import anthropic
import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.reminders.calendar_client import GoogleAuthError
from src.reminders.config import ReminderConfig
from src.reminders.models import CalendarEvent, PlannerLogEntry, ReminderRecord
from src.storage.database import DatabaseManager

logger = structlog.get_logger()

# UTC+7 offset used for all display / prompt strings
_UTC7 = timezone(timedelta(hours=7))

# Maximum seconds to wait for the Claude planning call before giving up
_LLM_TIMEOUT_SECONDS: int = 120

# Maximum characters of lifestyle context sent to Claude in the planning prompt.
# Keeps large personal files from being sent to the Anthropic API wholesale.
_LIFESTYLE_MAX_CHARS: int = 2_000

# Starter template sent to the user during cold-start onboarding when
# lifestyle.md is absent.  Mirrors the schema defined in specs/20-product/schemas.md.
_LIFESTYLE_TEMPLATE = """\
# My Lifestyle

> This file is read by your AI personal assistant to make smarter reminder decisions.
> Update it when your routines change. All sections are optional but more = smarter.

## Sleep Schedule

- Usually asleep by: 11pm
- Wake up: 7am
- Deep work hours: (e.g. 9am–12pm — avoid scheduling calls)

## Regular Commitments

- (e.g. Gym: Mon/Wed/Fri 7–8am)
- (e.g. Haircut: every 3 weeks, ~45 min)
- (e.g. Weekly team call: Tuesday 10am)

## Calls Policy

- OK during: (e.g. commute, haircut, light errands)
- Not OK during: (e.g. gym, meals, focused work blocks)

## Do Not Disturb Conditions

- (e.g. When I'm in a meeting — check calendar)
- (e.g. After 10pm unless critical)
- (e.g. Before 8am)

## Health & Energy Patterns

- (e.g. Low energy after lunch — avoid scheduling hard tasks 1–3pm)
- (e.g. Best focus in the morning)

## Travel & Location

- Base: Bangkok, UTC+7
- (e.g. Travel frequently — always check if I have a flight before scheduling)

## Things I Often Forget

- (e.g. Replying to messages after meetings)
- (e.g. Preparing documents the day before important calls)

## Things I Never Need Reminders About

- (e.g. Daily gym — I go automatically, don't remind me)
- (e.g. Meals — I handle these myself)
"""

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

PLANNER_TOOL: dict[str, Any] = {
    "name": "schedule_reminders",
    "description": (
        "Output the list of reminders to schedule for the next day based on "
        "calendar events, tasks, lifestyle context, and suppression rules."
    ),
    "input_schema": {
        "type": "object",
        "required": ["reminders"],
        "properties": {
            "reminders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "hint",
                        "urgency",
                        "scheduled_time",
                        "source_event_id",
                        "source_type",
                        "reminder_type",
                        "trigger_day",
                        "reason",
                    ],
                    "properties": {
                        "hint": {"type": "string", "maxLength": 200},
                        "urgency": {
                            "type": "string",
                            "enum": ["critical", "high", "normal", "low"],
                        },
                        "scheduled_time": {
                            "type": "string",
                            "description": "ISO 8601 UTC",
                        },
                        "expires_at": {"type": ["string", "null"]},
                        "context_notes": {
                            "type": ["string", "null"],
                            "maxLength": 500,
                        },
                        "source_event_id": {"type": ["string", "null"]},
                        "source_type": {
                            "type": "string",
                            "enum": ["calendar", "task", "conversation"],
                        },
                        "reminder_type": {
                            "type": "string",
                            "enum": ["primary", "advance_prep"],
                        },
                        "trigger_day": {
                            "type": "string",
                            "pattern": r"^\d{4}-\d{2}-\d{2}$",
                        },
                        "reason": {"type": "string", "maxLength": 300},
                    },
                },
            }
        },
    },
}

# Required fields that must be present in every reminder item from the LLM.
# source_event_id is required as a key (a null VALUE is still valid — handled
# by the NULL-safe dedup query in _dedup_and_insert).
_REQUIRED_REMINDER_FIELDS = frozenset(
    [
        "hint",
        "urgency",
        "scheduled_time",
        "source_event_id",
        "source_type",
        "reminder_type",
        "trigger_day",
        "reason",
    ]
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _sanitize(text: str, max_len: int = 200) -> str:
    """Strip newlines and truncate to prevent prompt injection.

    User-controlled strings (event titles, locations, descriptions, suppression
    rule topics) must be sanitised before embedding in the planning prompt.
    Embedded newlines could break the prompt structure; truncation caps the
    surface area sent to the API.
    """
    return text.replace("\n", " ").replace("\r", " ")[:max_len]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlannerParseError(Exception):
    """Raised when the LLM output cannot be parsed into valid ReminderRecord list.

    This is an *expected* failure mode — it means the model produced output
    that doesn't conform to the PLANNER_TOOL schema.  The caller should log
    the raw LLM output, abort the run (no DB writes), and notify the user.
    """


# ---------------------------------------------------------------------------
# NightlyPlanner
# ---------------------------------------------------------------------------


class NightlyPlanner:
    """Runs the nightly planning cycle: fetch events → call LLM → insert reminders.

    Designed to be safe for APScheduler: ``run()`` never raises.  All errors
    are caught, logged, and surfaced via Telegram notification.

    Parameters
    ----------
    db:
        Shared DatabaseManager (aiosqlite-backed).
    calendar:
        GoogleCalendarClient for fetching upcoming events.
    config:
        ReminderConfig for lifestyle.md and communication-patterns.toml.
    suppression:
        SuppressionService for active suppression rules.
    bot:
        telegram.Bot instance — used directly for failure notifications.
    anthropic_client:
        anthropic.AsyncAnthropic instance (shared; injected by caller).
    target_chat_id:
        Telegram chat ID to which failure notifications are sent.
    scheduler:
        APScheduler AsyncIOScheduler.  When set, a GoogleAuthError triggers a
        one-shot retry job 5 minutes later before notifying the user.
    model:
        Claude model ID.  Defaults to claude-opus-4-5 for planning quality.
    """

    def __init__(
        self,
        db: DatabaseManager,
        calendar: Any,  # GoogleCalendarClient — typed as Any to avoid heavy import cycle
        config: ReminderConfig,
        suppression: Any,  # SuppressionService — typed as Any (stub module)
        bot: Any,  # telegram.Bot
        anthropic_client: anthropic.AsyncAnthropic,
        target_chat_id: int,
        scheduler: Optional[Any] = None,
        model: str = "claude-opus-4-5",
    ) -> None:
        self.db = db
        self.calendar = calendar
        self.config = config
        self.suppression = suppression
        self.bot = bot
        self._client = anthropic_client
        self.target_chat_id = target_chat_id
        self.scheduler = scheduler
        self._model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Execute the full nightly planning cycle.

        Flow
        ----
        1. Validate calendar auth (GoogleAuthError → schedule retry + notify).
        2. Fetch events and active suppression rules.
        3. Build planning prompt.
        4. Call Claude → parse tool_use block (PlannerParseError → log + notify).
        5. Dedup and insert reminders; log each inserted row.
        6. On any other unhandled exception → log + notify immediately.

        Never raises — all exceptions are caught at the outer level so
        APScheduler does not kill the job after a single failure.
        """
        log = logger.bind(method="run")
        log.info("reminders.planner.run_start", model=self._model)

        try:
            # Step 0: cold-start onboarding -- offer lifestyle.md template on
            # the very first run before doing any expensive calendar work.
            async with self.db.get_connection() as conn:
                if await self._is_first_run(conn):
                    await self._check_cold_start()

            # Step 1: validate Google auth before doing any expensive work
            await self.calendar.validate_auth()

            # Step 2: fetch data in parallel where possible
            events: list[CalendarEvent] = await self.calendar.fetch_events(days_ahead=7)
            log.info("reminders.planner.events_fetched", count=len(events))

            # Suppression rules — SuppressionService may be a stub; guard for missing method
            suppressions: list[Any] = []
            if hasattr(self.suppression, "get_active_rules"):
                suppressions = await self.suppression.get_active_rules()

            lifestyle: str = self.config.load_lifestyle()
            patterns = self.config.load_comms_patterns()

            # Step 3: build the planning prompt
            # tasks=[] until T-014 adds Obsidian integration
            prompt = await self._build_planning_prompt(
                events=events,
                tasks=[],
                lifestyle=lifestyle,
                suppressions=suppressions,
                patterns=patterns,
            )

            # Step 4: call Claude with forced tool use
            log.info("reminders.planner.llm_call_start", event_count=len(events))
            try:
                response = await asyncio.wait_for(
                    self._client.messages.create(
                        model=self._model,
                        max_tokens=4096,
                        tools=[PLANNER_TOOL],  # type: ignore[list-item]
                        tool_choice={"type": "any"},
                        messages=[{"role": "user", "content": prompt}],
                    ),
                    timeout=_LLM_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "reminders.planner.llm_timeout",
                    timeout_seconds=_LLM_TIMEOUT_SECONDS,
                )
                raise
            log.info(
                "reminders.planner.llm_call_complete",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )

            # Step 5: parse tool_use block — PlannerParseError aborts run
            raw_tool_input = self._extract_tool_input(response)
            try:
                reminders = await self._parse_llm_output(raw_tool_input)
            except PlannerParseError as exc:
                # Log raw output so we can diagnose what the model produced
                raw_repr = json.dumps(raw_tool_input, ensure_ascii=False, indent=2)
                async with self.db.get_connection() as conn:
                    log_entry = PlannerLogEntry(
                        action="parse_error",
                        reason=str(exc),
                        raw_llm_output=raw_repr[:4000],  # column has no length limit but be sensible
                    )
                    await conn.execute(
                        """INSERT INTO planner_log
                           (id, event_time, action, reminder_id, source_event_id,
                            reason, raw_llm_output, notification_sent)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            log_entry.id,
                            log_entry.event_time.isoformat(),
                            log_entry.action,
                            log_entry.reminder_id,
                            log_entry.source_event_id,
                            log_entry.reason,
                            log_entry.raw_llm_output,
                            None,
                        ),
                    )
                    await conn.commit()
                log.error(
                    "reminders.planner.parse_error",
                    error=str(exc),
                )
                await self._send_failure_notification(
                    f"LLM output could not be parsed: {exc}"
                )
                return

            # Step 6: dedup + insert; log each reminder
            async with self.db.get_connection() as conn:
                inserted = await self._dedup_and_insert(reminders, conn)

            log.info(
                "reminders.planner.run_complete",
                reminders_proposed=len(reminders),
                reminders_inserted=inserted,
            )

        except (asyncio.TimeoutError, TimeoutError):
            # Already logged as reminders.planner.llm_timeout above; just notify
            await self._send_failure_notification(
                f"LLM call timed out after {_LLM_TIMEOUT_SECONDS}s"
            )

        except GoogleAuthError as exc:
            log.error(
                "reminders.planner.auth_error",
                error=str(exc),
            )
            # T-011: schedule a one-shot retry 5 minutes from now
            if self.scheduler is not None:
                retry_time = datetime.now(UTC) + timedelta(minutes=5)
                self.scheduler.add_job(
                    self.run,
                    trigger="date",
                    run_date=retry_time,
                    id="nightly_planner_auth_retry",
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                log.info(
                    "reminders.planner.auth_retry_scheduled",
                    retry_at=retry_time.isoformat(),
                )
            # Always notify even if retry was scheduled
            await self._send_failure_notification(
                f"Google Calendar auth failed: {exc}"
            )

        except Exception as exc:  # noqa: BLE001
            # Catch-all: log and notify immediately; never crash APScheduler job
            log.error(
                "reminders.planner.unhandled_error",
                error=str(exc),
                exc_type=type(exc).__name__,
                exc_info=True,
            )
            await self._send_failure_notification(str(exc))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _build_planning_prompt(
        self,
        events: list[CalendarEvent],
        tasks: list[Any],
        lifestyle: str,
        suppressions: list[Any],
        patterns: Any,  # CommPatterns
    ) -> str:
        """Assemble the planning prompt sent to Claude.

        The prompt is structured as a series of labelled sections so that
        Claude can quickly orient itself without reading a wall of prose:
        time context → events → lifestyle → suppression rules → delivery
        preferences → explicit instructions.

        Parameters
        ----------
        events:
            CalendarEvent list for the next 7 days.
        tasks:
            Obsidian task list (empty until T-014 lands).
        lifestyle:
            Content of lifestyle.md (may be empty).
        suppressions:
            Active SuppressionRule objects.
        patterns:
            CommPatterns loaded from communication-patterns.toml.
        """
        now_utc = datetime.now(UTC)
        now_utc7 = now_utc.astimezone(_UTC7)
        today_str = now_utc7.strftime("%Y-%m-%d")
        end_date = (now_utc7 + timedelta(days=7)).strftime("%Y-%m-%d")

        now_display = now_utc7.strftime("%A, %d %B %Y %H:%M (UTC+7)")

        # --- Calendar Events section ---
        if events:
            event_lines: list[str] = []
            for ev in events:
                ev_local = ev.start.astimezone(_UTC7)
                ev_end_local = ev.end.astimezone(_UTC7)
                if ev.all_day:
                    time_str = "all day"
                    duration_str = ""
                else:
                    time_str = ev_local.strftime("%H:%M UTC+7")
                    delta = ev.end - ev.start
                    total_minutes = int(delta.total_seconds() // 60)
                    if total_minutes >= 60:
                        hours, mins = divmod(total_minutes, 60)
                        duration_str = f" ({hours}h{mins:02d}m)" if mins else f" ({hours}h)"
                    else:
                        duration_str = f" ({total_minutes}m)"
                date_str = ev_local.strftime("%Y-%m-%d")
                safe_title = _sanitize(ev.title, 150)
                line = f"- [{ev.event_id}] {safe_title} on {date_str} at {time_str}{duration_str}"
                if ev.location:
                    line += f" @ {_sanitize(ev.location, 100)}"
                if ev.description:
                    line += f" | {_sanitize(ev.description, 200)}"
                event_lines.append(line)
            events_section = "\n".join(event_lines)
        else:
            events_section = "(no events in next 7 days)"

        # --- Lifestyle section ---
        lifestyle_section = lifestyle.strip() or "(none provided)"
        if len(lifestyle_section) > _LIFESTYLE_MAX_CHARS:
            lifestyle_section = lifestyle_section[:_LIFESTYLE_MAX_CHARS] + "\n[truncated]"

        # --- Suppression rules section ---
        if suppressions:
            suppression_lines = [
                f"- {_sanitize(r.topic, 100)}" for r in suppressions if hasattr(r, "topic")
            ]
            suppression_section = "\n".join(suppression_lines) if suppression_lines else "(none)"
        else:
            suppression_section = "(none)"

        # --- Delivery preferences section ---
        # patterns may be a CommPatterns from config.py (has nested dataclasses)
        # or from models.py (has flat string fields) — handle both shapes.
        if hasattr(patterns, "quiet_hours") and hasattr(patterns.quiet_hours, "start"):
            quiet_start = patterns.quiet_hours.start
            quiet_end = patterns.quiet_hours.end
            critical_categories = patterns.urgency.critical_categories
        elif hasattr(patterns, "quiet_start"):
            quiet_start = patterns.quiet_start
            quiet_end = patterns.quiet_end
            critical_categories = patterns.critical_categories
        else:
            quiet_start = "22:00"
            quiet_end = "07:00"
            critical_categories = ["flight", "doctor", "hospital", "visa", "passport", "surgery"]

        cats_str = ", ".join(critical_categories) if critical_categories else "(none)"

        lines: list[str] = [
            "You are a personal assistant scheduling reminders.",
            f"Current time: {now_display}",
            f"Planning window: {today_str} to {end_date} (next 7 days)",
            "",
            "## Calendar Events",
            events_section,
            "",
            "## Lifestyle Context",
            lifestyle_section,
            "",
            "## Active Suppression Rules",
            suppression_section,
            "",
            "## Delivery Preferences",
            f"Quiet hours: {quiet_start}\u2013{quiet_end} UTC+7",
            f"Critical categories: {cats_str}",
            "",
            "## Instructions",
            "For each calendar event, decide whether reminders are needed. Create:",
            '- A "primary" reminder: fires close to event time (30\u201360 min before by default)',
            (
                '- An "advance_prep" reminder: fires 1\u20133 days before for events requiring '
                "preparation (travel, medical, visa, etc.)"
            ),
            "Skip reminders if the topic matches an active suppression rule.",
            (
                'Urgency: use "critical" for events in critical categories, otherwise '
                '"normal" or "high" as appropriate.'
            ),
            "Schedule reminders during non-quiet hours where possible (urgency=critical may fire anytime).",
            (
                "Set trigger_day to the UTC+7 date of scheduled_time "
                "(e.g., if scheduled_time is 2026-03-15T22:00:00Z, trigger_day is \"2026-03-16\" "
                "because 22:00 UTC = 05:00 UTC+7 next day)."
            ),
            "Use the schedule_reminders tool to output ALL reminders as a single call.",
        ]

        return "\n".join(lines)

    async def _parse_llm_output(self, raw_tool_input: dict[str, Any]) -> list[ReminderRecord]:
        """Convert the validated tool_use input dict into ReminderRecord objects.

        Raises PlannerParseError if:
        - ``reminders`` key is missing or not a list
        - Any item is missing a required field
        - Any field value cannot be coerced into the expected type

        Valid but optional fields (``expires_at``, ``context_notes``) are
        silently defaulted to None when absent.
        """
        if "reminders" not in raw_tool_input:
            raise PlannerParseError(
                "tool_use input missing 'reminders' key"
            )

        raw_list = raw_tool_input["reminders"]
        if not isinstance(raw_list, list):
            raise PlannerParseError(
                f"'reminders' must be a list, got {type(raw_list).__name__}"
            )

        records: list[ReminderRecord] = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, dict):
                raise PlannerParseError(
                    f"reminder[{idx}] is not a dict: {type(item).__name__}"
                )

            missing = _REQUIRED_REMINDER_FIELDS - item.keys()
            if missing:
                raise PlannerParseError(
                    f"reminder[{idx}] missing required fields: {sorted(missing)}"
                )

            # Parse scheduled_time — must be an ISO 8601 string
            try:
                scheduled_time = datetime.fromisoformat(
                    item["scheduled_time"].replace("Z", "+00:00")
                )
                # Ensure timezone-aware
                if scheduled_time.tzinfo is None:
                    scheduled_time = scheduled_time.replace(tzinfo=UTC)
            except (ValueError, AttributeError) as exc:
                raise PlannerParseError(
                    f"reminder[{idx}] invalid scheduled_time "
                    f"{item['scheduled_time']!r}: {exc}"
                ) from exc

            # Validate scheduled_time is in the future
            _now = datetime.now(UTC)
            if scheduled_time <= _now:
                raise PlannerParseError(
                    f"reminder[{idx}] scheduled_time is not in the future: "
                    f"{item['scheduled_time']!r}"
                )
            # Validate scheduled_time is within the 8-day planning window
            if scheduled_time >= _now + timedelta(days=8):
                raise PlannerParseError(
                    f"reminder[{idx}] scheduled_time exceeds 8-day planning window: "
                    f"{item['scheduled_time']!r}"
                )

            # Parse expires_at — optional, may be null
            expires_at: Optional[datetime] = None
            raw_expires = item.get("expires_at")
            if raw_expires:
                try:
                    expires_at = datetime.fromisoformat(
                        raw_expires.replace("Z", "+00:00")
                    )
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=UTC)
                except (ValueError, AttributeError) as exc:
                    raise PlannerParseError(
                        f"reminder[{idx}] invalid expires_at {raw_expires!r}: {exc}"
                    ) from exc

            # Validate enum fields
            urgency = item["urgency"]
            if urgency not in ("critical", "high", "normal", "low"):
                raise PlannerParseError(
                    f"reminder[{idx}] invalid urgency {urgency!r}"
                )

            source_type = item["source_type"]
            if source_type not in ("calendar", "task", "conversation"):
                raise PlannerParseError(
                    f"reminder[{idx}] invalid source_type {source_type!r}"
                )

            reminder_type = item["reminder_type"]
            if reminder_type not in ("primary", "advance_prep"):
                raise PlannerParseError(
                    f"reminder[{idx}] invalid reminder_type {reminder_type!r}"
                )

            # Validate string field lengths — raise rather than silently truncate
            hint = str(item["hint"])
            if len(hint) > 200:
                raise PlannerParseError(
                    f"reminder[{idx}] hint exceeds 200 chars ({len(hint)}): {hint[:50]}..."
                )
            reason_val = str(item["reason"])
            if len(reason_val) > 300:
                raise PlannerParseError(
                    f"reminder[{idx}] reason exceeds 300 chars ({len(reason_val)}): {reason_val[:50]}..."
                )
            context_notes = item.get("context_notes")
            if context_notes is not None:
                context_notes = str(context_notes)
                if len(context_notes) > 500:
                    raise PlannerParseError(
                        f"reminder[{idx}] context_notes exceeds 500 chars ({len(context_notes)})"
                    )

            records.append(
                ReminderRecord(
                    hint=hint,
                    urgency=urgency,
                    scheduled_time=scheduled_time,
                    expires_at=expires_at,
                    context_notes=context_notes,
                    source_event_id=item.get("source_event_id"),
                    source_type=source_type,
                    reminder_type=reminder_type,
                    trigger_day=str(item["trigger_day"]),
                    reason=reason_val,
                )
            )

        return records

    async def _dedup_and_insert(
        self,
        reminders: list[ReminderRecord],
        conn: Any,
    ) -> int:
        """Insert reminders that are not already present in the DB.

        Deduplication key: (source_event_id, reminder_type, trigger_day).
        A reminder is skipped if any row with the same triple already exists
        (regardless of sent/cancelled status) — this prevents re-scheduling
        reminders that were already planned by a previous run.

        Returns the number of rows actually inserted.
        """
        inserted = 0
        for r in reminders:
            # Check for existing row with the same dedup key.
            # NULL = NULL evaluates to NULL in SQLite (not TRUE), so we must use
            # IS NULL / IS NOT NULL for the source_event_id column when it is None.
            cursor = await conn.execute(
                """SELECT 1 FROM reminders
                   WHERE (source_event_id = ? OR (source_event_id IS NULL AND ? IS NULL))
                     AND reminder_type = ?
                     AND trigger_day = ?
                   LIMIT 1""",
                (r.source_event_id, r.source_event_id, r.reminder_type, r.trigger_day),
            )
            row = await cursor.fetchone()
            if row is not None:
                logger.debug(
                    "reminders.planner.dedup_skip",
                    source_event_id=r.source_event_id,
                    reminder_type=r.reminder_type,
                    trigger_day=r.trigger_day,
                )
                continue

            # Insert the new reminder
            await conn.execute(
                """INSERT INTO reminders
                   (id, scheduled_time, expires_at, hint, urgency, context_notes,
                    source_event_id, source_type, reminder_type, trigger_day, reason,
                    sent, failed, retry_count, cancelled, expired, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, ?)""",
                (
                    r.id,
                    r.scheduled_time.isoformat() if r.scheduled_time else None,
                    r.expires_at.isoformat() if r.expires_at else None,
                    r.hint,
                    r.urgency,
                    r.context_notes,
                    r.source_event_id,
                    r.source_type,
                    r.reminder_type,
                    r.trigger_day,
                    r.reason,
                    r.created_at.isoformat(),
                ),
            )

            # Write a planner_log row for each inserted reminder
            log_entry = PlannerLogEntry(
                action="planned",
                reminder_id=r.id,
                source_event_id=r.source_event_id,
                reason=r.reason,
            )
            await conn.execute(
                """INSERT INTO planner_log
                   (id, event_time, action, reminder_id, source_event_id,
                    reason, raw_llm_output, notification_sent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    log_entry.id,
                    log_entry.event_time.isoformat(),
                    log_entry.action,
                    log_entry.reminder_id,
                    log_entry.source_event_id,
                    log_entry.reason,
                    log_entry.raw_llm_output,
                    int(log_entry.notification_sent)
                    if log_entry.notification_sent is not None
                    else None,
                ),
            )

            logger.info(
                "reminders.planner.reminder_inserted",
                reminder_id=r.id,
                hint=r.hint[:60],
                urgency=r.urgency,
                trigger_day=r.trigger_day,
                reminder_type=r.reminder_type,
            )
            inserted += 1

        await conn.commit()
        return inserted

    # ------------------------------------------------------------------
    # Cold-start onboarding
    # ------------------------------------------------------------------

    async def _is_first_run(self, conn: Any) -> bool:
        """Return True if no 'planned' entries exist in planner_log.

        A fresh installation has an empty planner_log table.  Once at least
        one reminder has been planned the table will contain a 'planned' row,
        and we consider onboarding complete.
        """
        cursor = await conn.execute(
            "SELECT 1 FROM planner_log WHERE action = 'planned' LIMIT 1"
        )
        row = await cursor.fetchone()
        return row is None

    async def _check_cold_start(self) -> None:
        """On the first nightly run, offer a lifestyle.md template if the file is missing.

        Sends a Telegram message with Yes/Not-now inline buttons.  The
        callback handler ``handle_onboarding_callback`` in commands.py
        responds to the user's choice.  If lifestyle.md already exists (or
        the path is not configured), this method is a no-op.
        """
        lifestyle_path: Optional[Path] = self.config._lifestyle_path
        if lifestyle_path is None or lifestyle_path.exists():
            # Path not configured, or file already present -- nothing to do.
            return

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Yes, send me the template",
                        callback_data="reminder_onboarding:send_template",
                    ),
                    InlineKeyboardButton(
                        "Not now",
                        callback_data="reminder_onboarding:skip",
                    ),
                ]
            ]
        )
        await self.bot.send_message(
            chat_id=self.target_chat_id,
            text=(
                "Welcome to Smart Reminders!\n\n"
                "I notice you don't have a lifestyle.md file yet. "
                "This file helps me understand your preferences and schedule "
                "reminders intelligently.\n\n"
                "Would you like me to send you a template to get started?"
            ),
            reply_markup=keyboard,
        )
        logger.info(
            "reminders.planner.cold_start_onboarding_sent",
            lifestyle_path=str(lifestyle_path),
        )

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------

    def _extract_tool_input(self, response: Any) -> dict[str, Any]:
        """Pull the tool_use block's ``input`` dict out of a Claude response.

        Claude is called with ``tool_choice={"type": "any"}``, which guarantees
        at least one tool_use block.  We look for the first block whose type
        is ``"tool_use"`` and whose name is ``"schedule_reminders"``.

        Raises PlannerParseError if no matching block is found.
        """
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                if getattr(block, "name", None) == "schedule_reminders":
                    raw_input = getattr(block, "input", None)
                    if not isinstance(raw_input, dict):
                        raise PlannerParseError(
                            f"tool_use.input is not a dict: {type(raw_input).__name__}"
                        )
                    return raw_input
        raise PlannerParseError(
            "No 'schedule_reminders' tool_use block found in LLM response"
        )

    async def _send_failure_notification(self, brief_reason: str) -> None:
        """Send a Telegram failure notification to target_chat_id.

        If the send itself fails, write a planner_log entry with
        notification_sent=False so the failure is visible in the audit log.

        The message follows the T-011 format:
            ⚠️ Nightly planning failed — check your calendar manually. Error: {brief_reason}
        """
        text = (
            f"\u26a0\ufe0f Nightly planning failed \u2014 check your calendar manually. "
            f"Error: {brief_reason}"
        )
        notification_sent = True
        try:
            await self.bot.send_message(chat_id=self.target_chat_id, text=text)
            # Log successful notification to planner_log
            async with self.db.get_connection() as conn:
                await conn.execute(
                    "INSERT INTO planner_log (id, event_time, action, reason, notification_sent)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        datetime.now(UTC).isoformat(),
                        "failure_notification_sent",
                        brief_reason,
                        1,
                    ),
                )
                await conn.commit()
        except Exception as send_exc:  # noqa: BLE001
            notification_sent = False
            logger.error(
                "reminders.planner.notification_send_failed",
                error=str(send_exc),
            )

        if not notification_sent:
            # Best-effort: record that we tried but failed to notify the user
            try:
                async with self.db.get_connection() as conn:
                    entry = PlannerLogEntry(
                        action="failed",
                        reason=brief_reason,
                        notification_sent=False,
                    )
                    await conn.execute(
                        """INSERT INTO planner_log
                           (id, event_time, action, reminder_id, source_event_id,
                            reason, raw_llm_output, notification_sent)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            entry.id,
                            entry.event_time.isoformat(),
                            entry.action,
                            entry.reminder_id,
                            entry.source_event_id,
                            entry.reason,
                            entry.raw_llm_output,
                            int(entry.notification_sent)
                            if entry.notification_sent is not None
                            else None,
                        ),
                    )
                    await conn.commit()
            except Exception as db_exc:  # noqa: BLE001
                # Last resort: log to structlog; nothing else we can do
                logger.error(
                    "reminders.planner.failed_log_write_error",
                    error=str(db_exc),
                )
