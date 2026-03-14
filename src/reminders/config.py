"""Configuration helpers for the reminders module."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Default TOML content written on first run
# ---------------------------------------------------------------------------

_DEFAULT_COMMS_PATTERNS_TOML = """\
# Communication patterns for the Smart Reminder System
# Edit this file to customise delivery behaviour

[quiet_hours]
# Times in HH:MM format, UTC+7
start = "22:00"
end   = "07:00"

[urgency]
# Urgency tiers affect delivery behaviour
# critical  → always fires, even during quiet hours
# high      → fires normally, no batching
# normal    → fires normally
# low       → batched with other low-urgency reminders (future feature)
default_tier = "normal"

# Event categories that are always treated as critical
critical_categories = ["flight", "doctor", "hospital", "surgery", "visa", "passport"]

[delivery]
# Max reminders to send per hour (circuit breaker)
max_per_hour = 5

# Minimum gap between reminders in minutes
min_gap_minutes = 15

[proactive_suggestions]
# Whether the bot suggests reminders proactively during conversation
enabled = true

# Max proactive suggestions per conversation session
max_per_session = 3
"""

# Maximum characters read from lifestyle.md before truncating
_LIFESTYLE_MAX_CHARS = 8_000
_LIFESTYLE_TRUNCATION_SUFFIX = "\n[lifestyle.md truncated]"


# ---------------------------------------------------------------------------
# CommPatterns data model (inline — models.py is still a stub)
# ---------------------------------------------------------------------------


@dataclass
class QuietHours:
    start: str = "22:00"  # HH:MM
    end: str = "07:00"  # HH:MM


@dataclass
class UrgencyConfig:
    default_tier: str = "normal"
    critical_categories: list[str] = field(
        default_factory=lambda: [
            "flight",
            "doctor",
            "hospital",
            "surgery",
            "visa",
            "passport",
        ]
    )


@dataclass
class DeliveryConfig:
    max_per_hour: int = 5
    min_gap_minutes: int = 15


@dataclass
class ProactiveSuggestions:
    enabled: bool = True
    max_per_session: int = 3


@dataclass
class CommPatterns:
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    urgency: UrgencyConfig = field(default_factory=UrgencyConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    proactive_suggestions: ProactiveSuggestions = field(
        default_factory=ProactiveSuggestions
    )


# ---------------------------------------------------------------------------
# ReminderConfig
# ---------------------------------------------------------------------------


class ReminderConfig:
    """Loads and exposes reminder-related configuration files.

    Parameters
    ----------
    lifestyle_md_path:
        Filesystem path to lifestyle.md.  May be ``None`` when the setting is
        not configured; :meth:`load_lifestyle` will return an empty string.
    comms_patterns_path:
        Filesystem path to ``communication-patterns.toml``.  If the file does
        not exist it is created with the default content.
    """

    def __init__(
        self,
        lifestyle_md_path: Optional[str | Path],
        comms_patterns_path: str | Path,
    ) -> None:
        self._lifestyle_path: Optional[Path] = (
            Path(lifestyle_md_path) if lifestyle_md_path else None
        )
        self._comms_path: Path = Path(comms_patterns_path)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def load_lifestyle(self) -> str:
        """Read lifestyle.md and return its text content.

        Returns an empty string if the path is not configured or the file is
        missing.  Content is truncated to ``_LIFESTYLE_MAX_CHARS`` characters;
        a ``[lifestyle.md truncated]`` suffix is appended when truncation
        occurs.
        """
        if self._lifestyle_path is None:
            logger.warning(
                "lifestyle_md_path not configured — skipping lifestyle.md load"
            )
            return ""

        if not self._lifestyle_path.exists():
            logger.warning(
                "lifestyle.md not found", path=str(self._lifestyle_path)
            )
            return ""

        try:
            text = self._lifestyle_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read lifestyle.md", exc=str(exc))
            return ""

        if len(text) > _LIFESTYLE_MAX_CHARS:
            text = text[:_LIFESTYLE_MAX_CHARS] + _LIFESTYLE_TRUNCATION_SUFFIX

        return text

    def load_comms_patterns(self) -> CommPatterns:
        """Read ``communication-patterns.toml`` and return a :class:`CommPatterns`.

        * If the file is missing it is created with the default content and
          the defaults are returned.
        * If the file cannot be parsed a warning is logged and the defaults are
          returned.
        """
        if not self._comms_path.exists():
            logger.warning(
                "communication-patterns.toml not found — creating default",
                path=str(self._comms_path),
            )
            self._create_default_comms_patterns()
            return CommPatterns()

        try:
            raw = self._comms_path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001  (tomllib raises various errors)
            logger.warning(
                "Failed to parse communication-patterns.toml — using defaults",
                exc=str(exc),
            )
            return CommPatterns()

        return self._parse_comms_patterns(data)

    def is_within_quiet_hours(self, dt: datetime) -> bool:
        """Return ``True`` if *dt* falls inside the configured quiet window.

        *dt* is interpreted as already being in UTC+7 (or whichever timezone
        was attached to it by the caller).  The comparison is done on the
        time-of-day component only.

        Overnight windows (e.g. 22:00–07:00 that cross midnight) are handled
        correctly.
        """
        patterns = self.load_comms_patterns()
        quiet_start = self._parse_hhmm(patterns.quiet_hours.start)
        quiet_end = self._parse_hhmm(patterns.quiet_hours.end)
        current = dt.time().replace(second=0, microsecond=0)

        if quiet_start <= quiet_end:
            # Same-day window (e.g. 08:00–20:00)
            return quiet_start <= current < quiet_end
        else:
            # Overnight window (e.g. 22:00–07:00)
            return current >= quiet_start or current < quiet_end

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _create_default_comms_patterns(self) -> None:
        """Write the default TOML content to :attr:`_comms_path`."""
        try:
            self._comms_path.parent.mkdir(parents=True, exist_ok=True)
            self._comms_path.write_text(_DEFAULT_COMMS_PATTERNS_TOML, encoding="utf-8")
            logger.info(
                "Created default communication-patterns.toml",
                path=str(self._comms_path),
            )
        except OSError as exc:
            logger.warning(
                "Could not write default communication-patterns.toml",
                exc=str(exc),
            )

    @staticmethod
    def _parse_comms_patterns(data: dict) -> CommPatterns:  # type: ignore[type-arg]
        """Convert raw TOML dict into a :class:`CommPatterns` dataclass."""
        qh_raw = data.get("quiet_hours", {})
        urgency_raw = data.get("urgency", {})
        delivery_raw = data.get("delivery", {})
        proactive_raw = data.get("proactive_suggestions", {})

        return CommPatterns(
            quiet_hours=QuietHours(
                start=qh_raw.get("start", "22:00"),
                end=qh_raw.get("end", "07:00"),
            ),
            urgency=UrgencyConfig(
                default_tier=urgency_raw.get("default_tier", "normal"),
                critical_categories=urgency_raw.get(
                    "critical_categories",
                    ["flight", "doctor", "hospital", "surgery", "visa", "passport"],
                ),
            ),
            delivery=DeliveryConfig(
                max_per_hour=delivery_raw.get("max_per_hour", 5),
                min_gap_minutes=delivery_raw.get("min_gap_minutes", 15),
            ),
            proactive_suggestions=ProactiveSuggestions(
                enabled=proactive_raw.get("enabled", True),
                max_per_session=proactive_raw.get("max_per_session", 3),
            ),
        )

    @staticmethod
    def _parse_hhmm(value: str) -> time:
        """Parse a ``HH:MM`` string into a :class:`datetime.time`."""
        try:
            h, m = value.split(":")
            return time(int(h), int(m))
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                f"Invalid HH:MM time value {value!r}: {exc}"
            ) from exc
