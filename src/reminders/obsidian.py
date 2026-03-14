"""Obsidian vault reader for the reminders module."""
from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path

import structlog

from src.reminders.models import ObsidianTask

logger = structlog.get_logger()

# Due-date regex patterns checked in priority order per task line
_DATAVIEW_DUE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_TASKS_DUE = re.compile(r"due::\s*(\d{4}-\d{2}-\d{2})")
_FRONTMATTER_DUE = re.compile(r"^due:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)


class ObsidianScanner:
    """Walk an Obsidian vault and extract incomplete (`- [ ]`) tasks."""

    MAX_FILES = 500

    def __init__(self, vault_path: str) -> None:
        self.vault_path = vault_path

    async def extract_tasks(self) -> list[ObsidianTask]:
        """Walk vault_path for .md files (max 500), extract all '- [ ]' items.

        Parse due dates from Dataview inline fields, Tasks plugin inline
        fields, and YAML frontmatter. Returns [] on OSError (logged,
        non-blocking).
        """
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._extract_sync
            )
        except OSError as exc:
            logger.warning(
                "reminders.obsidian.vault_error",
                error=str(exc),
                path=self.vault_path,
            )
            return []

    def _extract_sync(self) -> list[ObsidianTask]:
        vault = Path(self.vault_path)
        tasks: list[ObsidianTask] = []
        today = date.today()
        file_count = 0

        for md_file in vault.rglob("*.md"):
            if file_count >= self.MAX_FILES:
                break
            file_count += 1
            try:
                self._process_file(md_file, vault, tasks, today)
            except OSError:
                continue  # Skip unreadable files silently

        return tasks

    def _process_file(
        self,
        md_file: Path,
        vault: Path,
        tasks: list[ObsidianTask],
        today: date,
    ) -> None:
        content = md_file.read_text(encoding="utf-8", errors="ignore")
        frontmatter_due = self._extract_frontmatter_due(content)
        relative_path = str(md_file.relative_to(vault))

        for line_number, line in enumerate(content.splitlines(), start=1):
            if "- [ ]" not in line:
                continue

            due_date = self._extract_due_date(line, frontmatter_due)
            overdue = (
                due_date is not None
                and date.fromisoformat(due_date) < today
            )

            tasks.append(
                ObsidianTask(
                    file_path=relative_path,
                    line_number=line_number,
                    text=line.strip(),
                    due_date=due_date,
                    overdue=overdue,
                )
            )

    def _extract_frontmatter_due(self, content: str) -> str | None:
        """Return the due date from YAML frontmatter, or None."""
        if not content.startswith("---"):
            return None
        # Find the closing '---' of the frontmatter block
        end = content.find("\n---", 3)
        if end == -1:
            return None
        frontmatter = content[3:end]
        match = _FRONTMATTER_DUE.search(frontmatter)
        return match.group(1) if match else None

    def _extract_due_date(
        self, line: str, frontmatter_due: str | None
    ) -> str | None:
        """Return the first due date found, checking in priority order.

        Priority:
        1. Dataview inline:  📅 YYYY-MM-DD
        2. Tasks plugin:     due:: YYYY-MM-DD
        3. Frontmatter:      due: YYYY-MM-DD  (from the file's YAML block)
        """
        match = _DATAVIEW_DUE.search(line)
        if match:
            return match.group(1)

        match = _TASKS_DUE.search(line)
        if match:
            return match.group(1)

        return frontmatter_due
