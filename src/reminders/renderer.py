"""Message renderer for the reminders module.

Calls Claude (Haiku by default) to produce a short, friendly, contextual
notification message for a given :class:`ReminderRecord`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Protocol, runtime_checkable

import anthropic
import structlog

from .models import CalendarEvent, ReminderRecord

logger = structlog.get_logger()

# UTC+7 offset used for display in the prompt
_TZ_UTC7 = timezone(timedelta(hours=7))

# Maximum characters of lifestyle context sent to the model
_LIFESTYLE_MAX_CHARS = 1_500

# Prefix stripped from any model reply that accidentally echoes it back
_UNWANTED_PREFIXES = ("reminder:", "Reminder:")


@runtime_checkable
class LLMCallCounter(Protocol):
    """Minimal interface for circuit-breaker / rate-limit counter objects."""

    def can_call(self) -> bool:
        """Return ``True`` when an LLM call is permitted."""
        ...

    def increment(self) -> None:
        """Register that a call was (attempted to be) made."""
        ...


class LLMRenderer:
    """Render a contextual notification message for a reminder using Claude.

    Parameters
    ----------
    anthropic_client:
        An ``anthropic.AsyncAnthropic`` instance (injected by the caller so
        that a single shared client can be reused across the application).
    model:
        Claude model ID to use for rendering.  Defaults to Haiku for
        speed and cost efficiency.
    """

    RENDER_TIMEOUT_SECONDS: int = 30

    def __init__(
        self,
        anthropic_client: anthropic.AsyncAnthropic,
        model: str = "claude-haiku-3-5-20241022",
    ) -> None:
        self._client = anthropic_client
        self._model = model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def render(
        self,
        reminder: ReminderRecord,
        today_events: list[CalendarEvent],
        lifestyle: str,
        current_time: datetime,
        *,
        llm_call_counter: Optional[LLMCallCounter] = None,
    ) -> str:
        """Produce a contextual notification message for *reminder*.

        Parameters
        ----------
        reminder:
            The reminder record to render.
        today_events:
            Calendar events for today (used as context in the prompt).
        lifestyle:
            Content of lifestyle.md (may be empty string).
        current_time:
            The current moment as a timezone-aware UTC ``datetime``.
        llm_call_counter:
            Optional circuit-breaker object.  When provided and
            ``can_call()`` returns ``False`` the LLM call is skipped and
            the fallback string is returned immediately.

        Returns
        -------
        str
            The rendered notification message (no "Reminder:" prefix).
            On timeout or API error the fallback string is returned.
        """
        if llm_call_counter is not None:
            if not llm_call_counter.can_call():
                logger.warning(
                    "reminders.renderer.circuit_breaker_open",
                    reminder_id=reminder.id,
                )
                return self._fallback(reminder.hint)
            llm_call_counter.increment()

        prompt = self._build_prompt(reminder, today_events, lifestyle, current_time)

        try:
            message = await asyncio.wait_for(
                self._client.messages.create(
                    model=self._model,
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=self.RENDER_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "reminders.renderer.timeout",
                reminder_id=reminder.id,
                timeout_seconds=self.RENDER_TIMEOUT_SECONDS,
            )
            return self._fallback(reminder.hint)
        except anthropic.APIError as exc:
            logger.warning(
                "reminders.renderer.api_error",
                reminder_id=reminder.id,
                error=str(exc),
            )
            return self._fallback(reminder.hint)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reminders.renderer.unexpected_error",
                reminder_id=reminder.id,
                error=str(exc),
            )
            return self._fallback(reminder.hint)

        rendered = self._extract_text(message)
        rendered = self._strip_prefix(rendered)

        logger.info(
            "reminders.renderer.rendered",
            reminder_id=reminder.id,
            model=self._model,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

        return rendered

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback(hint: str) -> str:
        """Return the standard fallback string."""
        return f"Quick reminder: {hint} — couldn't add context right now"

    @staticmethod
    def _extract_text(message: anthropic.types.Message) -> str:
        """Pull the plain text out of an Anthropic ``Message``."""
        parts: list[str] = []
        for block in message.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return " ".join(parts).strip()

    @staticmethod
    def _strip_prefix(text: str) -> str:
        """Remove an accidental 'Reminder:' prefix from the model output."""
        for prefix in _UNWANTED_PREFIXES:
            if text.startswith(prefix):
                return text[len(prefix) :].lstrip()
        return text

    def _build_prompt(
        self,
        reminder: ReminderRecord,
        today_events: list[CalendarEvent],
        lifestyle: str,
        current_time: datetime,
    ) -> str:
        """Assemble the user prompt sent to Claude."""
        # Display time in UTC+7
        local_time = current_time.astimezone(_TZ_UTC7)
        time_str = local_time.strftime("%A, %d %B %Y %H:%M (UTC+7)")

        # Calendar summary
        events_summary = self._summarise_events(today_events, current_time)

        # Truncate lifestyle to keep the prompt focused
        lifestyle_snippet = (lifestyle or "").strip()
        if len(lifestyle_snippet) > _LIFESTYLE_MAX_CHARS:
            lifestyle_snippet = (
                lifestyle_snippet[:_LIFESTYLE_MAX_CHARS] + "\n[truncated]"
            )

        # Urgency label
        urgency_label = {
            "critical": "CRITICAL",
            "high": "High",
            "normal": "Normal",
            "low": "Low",
        }.get(reminder.urgency, reminder.urgency.capitalize())

        lines: list[str] = [
            "You are a personal assistant sending a reminder notification.",
            "Write a SHORT, FRIENDLY, CONTEXTUAL message (1-3 sentences) for the",
            "reminder below. Do NOT start with 'Reminder:'. Be natural and warm.",
            "",
            f"Current time: {time_str}",
            "",
            "--- REMINDER ---",
            f"Hint: {reminder.hint}",
            f"Urgency: {urgency_label}",
        ]

        if reminder.context_notes:
            lines.append(f"Context notes: {reminder.context_notes}")

        lines += [
            "",
            "--- TODAY'S CALENDAR ---",
            events_summary or "(no events found)",
        ]

        if lifestyle_snippet:
            lines += [
                "",
                "--- USER LIFESTYLE CONTEXT ---",
                lifestyle_snippet,
            ]

        lines += [
            "",
            "Write only the notification message text, nothing else.",
        ]

        return "\n".join(lines)

    @staticmethod
    def _summarise_events(
        events: list[CalendarEvent],
        current_time: datetime,
    ) -> str:
        """Return a concise bullet-list summary of today's calendar events."""
        if not events:
            return ""

        lines: list[str] = []
        for event in events:
            start_local = event.start.astimezone(_TZ_UTC7)
            end_local = event.end.astimezone(_TZ_UTC7)

            if event.all_day:
                time_range = "all day"
            else:
                time_range = (
                    f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}"
                )

            parts = [f"• {event.title} ({time_range})"]
            if event.location:
                parts.append(f"@ {event.location}")
            lines.append(" ".join(parts))

        return "\n".join(lines)
