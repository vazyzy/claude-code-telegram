"""Personal context loader for system prompt injection.

Reads Obsidian lifestyle context files and builds a <personal_context> block
injected into every Claude session. This gives the assistant grounded knowledge
of who the user is before any interaction — implementing the "Know Before You Ask"
principle (P1) from assistant-principles.md.

Files loaded:
  - lifestyle.md            — identity, routine, tone (16,000 char limit)
  - now.md                  — current situation, travel, open decisions (8,000 char limit)
  - struggles.md            — open loops, what's hard, blockers (8,000 char limit)
  - Character/Principals.md — core identity, philosophy (4,000 char limit)
  - AI Assistant/communication-style.md — learned comms patterns (4,000 char limit)
  - Goals/*.md              — active goal files for priority hierarchy (P7) (6,000 char total)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_LIFESTYLE_MAX_CHARS = 16_000
_NOW_MAX_CHARS = 8_000
_STRUGGLES_MAX_CHARS = 8_000
_PRINCIPALS_MAX_CHARS = 4_000
_COMMS_STYLE_MAX_CHARS = 4_000
_GOALS_MAX_CHARS = 6_000
_TRUNCATION_SUFFIX = "\n[truncated]"

_LOAD_FAILURE_BLOCK = (
    "<personal_context>\n"
    "[Context files could not be loaded — respond on memory only. "
    "Flag this to the user at the start of your response.]\n"
    "</personal_context>"
)


class PersonalContextService:
    """Load Obsidian context files and build a system prompt block."""

    def __init__(
        self,
        lifestyle_md_path: Optional[str],
        now_md_path: Optional[str],
        struggles_md_path: Optional[str],
        communication_style_md_path: Optional[str],
        goals_dir_path: Optional[str] = None,
        principals_md_path: Optional[str] = None,
    ) -> None:
        self._lifestyle_path = Path(lifestyle_md_path) if lifestyle_md_path else None
        self._now_path = Path(now_md_path) if now_md_path else None
        self._struggles_path = Path(struggles_md_path) if struggles_md_path else None
        self._principals_path = Path(principals_md_path) if principals_md_path else None
        self._comms_path = (
            Path(communication_style_md_path) if communication_style_md_path else None
        )
        self._goals_dir = Path(goals_dir_path) if goals_dir_path else None

    def _has_any_path_configured(self) -> bool:
        """Return True if at least one context file path is configured."""
        return any(
            [
                self._lifestyle_path,
                self._now_path,
                self._struggles_path,
                self._principals_path,
                self._comms_path,
                self._goals_dir,
            ]
        )

    async def build_context_prompt(self) -> Optional[str]:
        """Build the <personal_context> block for system prompt injection.

        Returns None if no context files are configured at all.
        Returns a load-failure block if paths are configured but all reads fail
        (so the assistant knows to flag the issue to the user).
        """
        if not self._has_any_path_configured():
            return None

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

        principals = self._read_file(
            self._principals_path, _PRINCIPALS_MAX_CHARS, "Principals.md"
        )
        if principals:
            sections.append(f"## Core Identity & Philosophy\n{principals}")

        comms = self._read_file(
            self._comms_path, _COMMS_STYLE_MAX_CHARS, "communication-style.md"
        )
        if comms:
            sections.append(f"## Communication Style (learned)\n{comms}")

        goals = self._load_goals()
        if goals:
            sections.append(f"## Active Goals\n{goals}")

        if not sections:
            # Paths were configured but every file read failed — signal the LLM
            logger.warning("All personal context files failed to load")
            return _LOAD_FAILURE_BLOCK

        body = "\n\n---\n\n".join(sections)
        return f"<personal_context>\n{body}\n</personal_context>"

    def _load_goals(self) -> str:
        """Read all .md files from the Goals/ directory and concatenate.

        Implements P7 (Priority Hierarchy) — gives the assistant awareness of
        active goals so it can rank suggestions against the priority hierarchy.
        Total output is capped at _GOALS_MAX_CHARS.
        """
        if self._goals_dir is None:
            return ""

        if not self._goals_dir.is_dir():
            logger.debug("Goals directory not found", path=str(self._goals_dir))
            return ""

        goal_files = sorted(self._goals_dir.glob("*.md"))
        if not goal_files:
            return ""

        parts: list[str] = []
        total_chars = 0

        for goal_file in goal_files:
            try:
                content = goal_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning(
                    "Failed to read goal file",
                    path=str(goal_file),
                    error=str(exc),
                )
                continue

            if not content:
                continue

            header = f"### {goal_file.stem}\n"
            entry = header + content + "\n"

            if total_chars + len(entry) > _GOALS_MAX_CHARS:
                remaining = _GOALS_MAX_CHARS - total_chars - len(header)
                if remaining > 100:
                    parts.append(header + content[:remaining] + _TRUNCATION_SUFFIX)
                    logger.warning(
                        "Goals context truncated",
                        at_file=goal_file.name,
                        limit=_GOALS_MAX_CHARS,
                    )
                break

            parts.append(entry)
            total_chars += len(entry)

        return "\n".join(parts)

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
