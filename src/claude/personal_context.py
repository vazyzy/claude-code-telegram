"""Personal context loader for system prompt injection.

Reads Obsidian lifestyle context files and builds a <personal_context> block
injected into every Claude session. This gives the assistant grounded knowledge
of who the user is before any interaction — implementing the "Know Before You Ask"
principle (P1) from assistant-principles.md.

Files loaded:
  - lifestyle.md  — identity, routine, tone (16,000 char limit)
  - now.md        — current situation, travel, open decisions (8,000 char limit)
  - struggles.md  — open loops, what's hard, blockers (8,000 char limit)
  - AI Assistant/communication-style.md — learned comms patterns (4,000 char limit)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_LIFESTYLE_MAX_CHARS = 16_000
_NOW_MAX_CHARS = 8_000
_STRUGGLES_MAX_CHARS = 8_000
_COMMS_STYLE_MAX_CHARS = 4_000
_TRUNCATION_SUFFIX = "\n[truncated]"


class PersonalContextService:
    """Load Obsidian context files and build a system prompt block."""

    def __init__(
        self,
        lifestyle_md_path: Optional[str],
        now_md_path: Optional[str],
        struggles_md_path: Optional[str],
        communication_style_md_path: Optional[str],
    ) -> None:
        self._lifestyle_path = Path(lifestyle_md_path) if lifestyle_md_path else None
        self._now_path = Path(now_md_path) if now_md_path else None
        self._struggles_path = Path(struggles_md_path) if struggles_md_path else None
        self._comms_path = (
            Path(communication_style_md_path) if communication_style_md_path else None
        )

    async def build_context_prompt(self) -> Optional[str]:
        """Build the <personal_context> block for system prompt injection.

        Returns None if no context files are configured or all are missing.
        """
        sections: list[str] = []

        lifestyle = self._read_file(
            self._lifestyle_path, _LIFESTYLE_MAX_CHARS, "lifestyle.md"
        )
        if lifestyle:
            sections.append(f"## Lifestyle\n{lifestyle}")

        now = self._read_file(self._now_path, _NOW_MAX_CHARS, "now.md")
        if now:
            sections.append(f"## Current Situation\n{now}")

        struggles = self._read_file(
            self._struggles_path, _STRUGGLES_MAX_CHARS, "struggles.md"
        )
        if struggles:
            sections.append(f"## Struggles & Open Loops\n{struggles}")

        comms = self._read_file(
            self._comms_path, _COMMS_STYLE_MAX_CHARS, "communication-style.md"
        )
        if comms:
            sections.append(f"## Communication Style (learned)\n{comms}")

        if not sections:
            return None

        body = "\n\n---\n\n".join(sections)
        return f"<personal_context>\n{body}\n</personal_context>"

    def _read_file(
        self,
        path: Optional[Path],
        max_chars: int,
        label: str,
    ) -> str:
        """Read a file, truncate if needed, return empty string if missing."""
        if path is None:
            return ""

        try:
            content = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.debug("Personal context file not found", path=str(path), label=label)
            return ""
        except OSError as exc:
            logger.warning(
                "Failed to read personal context file",
                path=str(path),
                label=label,
                error=str(exc),
            )
            return ""

        if len(content) > max_chars:
            content = content[:max_chars] + _TRUNCATION_SUFFIX
            logger.warning(
                "Personal context file truncated",
                label=label,
                limit=max_chars,
            )

        return content
