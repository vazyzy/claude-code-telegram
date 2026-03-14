"""Google Calendar client for the reminders module."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog

from src.reminders.models import CalendarEvent

logger = structlog.get_logger()

UTC = timezone.utc


class GoogleAuthError(Exception):
    """Raised when gws auth fails or token is invalid."""


class GoogleCalendarClient:
    def __init__(self, gws_binary: str = "gws", calendar_id: str = "primary"):
        self.gws_binary = gws_binary
        self.calendar_id = calendar_id

    async def validate_auth(self) -> None:
        """Check that gws is authenticated. Raises GoogleAuthError if not.

        gws auth status always exits 0, so we parse its JSON output and
        check that auth_method is not "none" to determine whether
        real credentials are present.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self.gws_binary, "auth", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode != 0:
                raise GoogleAuthError(
                    f"gws auth check failed: {stderr.decode().strip()}"
                )

            # gws auth status always exits 0; inspect auth_method to detect
            # unauthenticated state.
            try:
                status = json.loads(stdout.decode())
                auth_method = status.get("auth_method", "none")
                if auth_method == "none":
                    raise GoogleAuthError(
                        "gws is not authenticated (auth_method=none). "
                        "Run 'gws auth login' to authenticate."
                    )
            except json.JSONDecodeError:
                # If we cannot parse the output, assume auth is OK and let
                # a real API call surface the problem.
                pass

        except FileNotFoundError:
            raise GoogleAuthError(
                f"gws binary not found at '{self.gws_binary}'. "
                "Install gws and run 'gws auth login'."
            )
        except asyncio.TimeoutError:
            raise GoogleAuthError("gws auth check timed out")

    async def fetch_events(self, days_ahead: int = 7) -> list[CalendarEvent]:
        """Fetch calendar events for the next N days.

        Returns an empty list on any error so the caller is never blocked.

        gws calendar events list accepts query parameters via
        --params <JSON>, so we pass calendarId, timeMin, and
        timeMax in a single JSON object.
        """
        now = datetime.now(UTC)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days_ahead)).isoformat()

        params = json.dumps({
            "calendarId": self.calendar_id,
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
        })

        try:
            proc = await asyncio.create_subprocess_exec(
                self.gws_binary, "calendar", "events", "list",
                "--params", params,
                "--format", "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode != 0:
                logger.warning(
                    "gws_calendar_fetch_failed",
                    returncode=proc.returncode,
                    stderr=stderr.decode().strip()[:200],
                )
                return []

            data = json.loads(stdout.decode())
            return self._parse_events(data)

        except FileNotFoundError:
            logger.warning("gws_binary_not_found", gws_binary=self.gws_binary)
            return []
        except asyncio.TimeoutError:
            logger.warning("gws_calendar_fetch_timeout")
            return []
        except json.JSONDecodeError as e:
            logger.warning("gws_calendar_output_parse_error", error=str(e))
            return []
        except Exception as e:
            logger.warning("gws_calendar_unexpected_error", error=str(e))
            return []

    def _parse_events(self, data: list | dict) -> list[CalendarEvent]:
        """Parse gws JSON output into CalendarEvent list."""
        items: list = data if isinstance(data, list) else data.get("items", [])
        events: list[CalendarEvent] = []
        for item in items:
            try:
                start_raw = item.get("start", {})
                end_raw = item.get("end", {})
                all_day = "date" in start_raw and "dateTime" not in start_raw

                if all_day:
                    start = datetime.fromisoformat(start_raw["date"]).replace(
                        tzinfo=UTC
                    )
                    end = datetime.fromisoformat(
                        end_raw.get("date", start_raw["date"])
                    ).replace(tzinfo=UTC)
                else:
                    start = datetime.fromisoformat(start_raw.get("dateTime", ""))
                    end = datetime.fromisoformat(end_raw.get("dateTime", ""))
                    # Ensure timezone-aware
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=UTC)
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=UTC)

                events.append(CalendarEvent(
                    event_id=item.get("id", ""),
                    title=item.get("summary", "(no title)"),
                    start=start,
                    end=end,
                    description=item.get("description"),
                    location=item.get("location"),
                    all_day=all_day,
                ))
            except Exception as e:
                logger.debug("skipping_unparseable_calendar_event", error=str(e))
                continue
        return events

    async def write_busy_block(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str,
        source_event_id: Optional[str] = None,
    ) -> bool:
        """Create or update a busy block on Google Calendar.

        Non-blocking -- returns False on failure.
        Implemented in T-020.
        """
        # TODO: T-020 will implement this
        return True
