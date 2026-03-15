"""Open loop tracking service (P2 — Open Loops Are Sacred).

Open loops are unresolved items the user has mentioned but not yet acted on.
They are persisted in SQLite across sessions and injected into the system prompt
so the assistant remembers them without the user having to repeat themselves.

Claude signals open loops using XML tags in its response:
  <open_loop_add>description of the unresolved item</open_loop_add>
  <open_loop_close>loop_id</open_loop_close>

These tags are stripped before the response reaches the user.
"""

from __future__ import annotations

import re
from typing import List, Optional

import structlog

from ..storage.repositories import OpenLoopRepository

logger = structlog.get_logger()

_ADD_PATTERN = re.compile(r"<open_loop_add>(.*?)</open_loop_add>", re.DOTALL)
_CLOSE_PATTERN = re.compile(r"<open_loop_close>(.*?)</open_loop_close>", re.DOTALL)

OPEN_LOOP_INSTRUCTIONS = (
    "When the user mentions something unresolved — a decision to make, a task to follow "
    "up on, or an open question — and the conversation ends without resolving it, "
    "output at the END of your response:\n"
    "<open_loop_add>short description of the unresolved item</open_loop_add>\n"
    "When an open loop from the context is resolved in the conversation, output:\n"
    "<open_loop_close>loop_id_number</open_loop_close>\n"
    "Only add loops for genuinely unresolved items. Do not add loops for things "
    "already completed or decided. Do not add more than 1 loop per response."
)


class OpenLoopService:
    """Manage open loop tracking across sessions."""

    def __init__(self, open_loop_repo: OpenLoopRepository) -> None:
        self._repo = open_loop_repo

    async def build_open_loops_prompt(self, user_id: int) -> Optional[str]:
        """Build the <open_loops> system prompt block (3 most recent unresolved)."""
        loops = await self._repo.get_open(user_id, limit=3)

        if not loops:
            return None

        lines = [f"- [{loop.id}] {loop.text}" for loop in loops]
        body = "\n".join(lines)
        return (
            f"<open_loops>\n"
            f"Unresolved items from previous conversations (surface when relevant):\n"
            f"{body}\n"
            f"</open_loops>\n\n" + OPEN_LOOP_INSTRUCTIONS
        )

    def parse_add_tags(self, response_text: str) -> List[str]:
        """Extract open_loop_add texts from response."""
        return [m.group(1).strip() for m in _ADD_PATTERN.finditer(response_text)]

    def parse_close_tags(self, response_text: str) -> List[int]:
        """Extract loop IDs from open_loop_close tags."""
        ids: List[int] = []
        for m in _CLOSE_PATTERN.finditer(response_text):
            raw = m.group(1).strip()
            try:
                ids.append(int(raw))
            except ValueError:
                logger.warning("Invalid open_loop_close id", raw=raw)
        return ids

    async def apply_updates(self, user_id: int, response_text: str) -> None:
        """Parse and persist open loop add/close operations from a response."""
        for text in self.parse_add_tags(response_text):
            await self._repo.add(user_id, text)

        for loop_id in self.parse_close_tags(response_text):
            await self._repo.resolve(loop_id)

    def strip_tags(self, response_text: str) -> str:
        """Remove open loop tags from user-visible response."""
        text = _ADD_PATTERN.sub("", response_text)
        text = _CLOSE_PATTERN.sub("", text)
        return text.rstrip()
