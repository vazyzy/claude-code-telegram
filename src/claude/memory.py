"""Persistent user memory service.

Builds memory prompts for injection into Claude requests and
processes memory update blocks from Claude responses.
"""

import json
import re
from typing import Any, Dict, List, Optional

import structlog

from ..storage.models import UserMemoryEntry
from ..storage.repositories import UserMemoryRepository

logger = structlog.get_logger()

MEMORY_UPDATE_PATTERN = re.compile(r"<memory_update>(.*?)</memory_update>", re.DOTALL)

MEMORY_INSTRUCTIONS = (
    "If you learn new important facts about the user, or they ask you to "
    "remember/forget something, output at the END of your response:\n"
    '<memory_update>{"set": [{"category": "fact", "key": "slug", '
    '"value": "text"}], "delete": ["key"]}</memory_update>\n'
    "Keep facts concise (one sentence each). "
    "Use category 'profile' for preferences (timezone, language, name) "
    "and 'fact' for everything else."
)


class UserMemoryService:
    """Manage persistent user memory across sessions."""

    def __init__(
        self,
        memory_repo: UserMemoryRepository,
        max_facts: int = 20,
    ):
        self.memory_repo = memory_repo
        self.max_facts = max_facts

    async def build_memory_prompt(self, user_id: int) -> Optional[str]:
        """Build memory block for injection into system prompt."""
        entries = await self.memory_repo.get_user_memory(user_id)

        if not entries:
            return (
                "<user_memory>\nNo memories stored yet.\n</user_memory>\n\n"
                + MEMORY_INSTRUCTIONS
            )

        profile_lines: List[str] = []
        fact_lines: List[str] = []

        for entry in entries:
            line = f"- {entry.key}: {entry.value}"
            if entry.category == "profile":
                profile_lines.append(line)
            else:
                fact_lines.append(line)

        sections: List[str] = []
        if profile_lines:
            sections.append("## Profile\n" + "\n".join(profile_lines))
        if fact_lines:
            sections.append("## Facts\n" + "\n".join(fact_lines))

        memory_block = "<user_memory>\n" + "\n\n".join(sections) + "\n</user_memory>"
        return memory_block + "\n\n" + MEMORY_INSTRUCTIONS

    def parse_memory_updates(self, response_text: str) -> Optional[Dict[str, Any]]:
        """Parse <memory_update> JSON block from response."""
        match = MEMORY_UPDATE_PATTERN.search(response_text)
        if not match:
            return None

        try:
            return json.loads(match.group(1).strip())
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse memory update", error=str(e))
            return None

    async def apply_memory_updates(self, user_id: int, updates: Dict[str, Any]) -> None:
        """Apply parsed memory updates (set/delete)."""
        set_entries = updates.get("set", [])
        delete_keys = updates.get("delete", [])

        for item in set_entries:
            entry = UserMemoryEntry(
                user_id=user_id,
                category=item.get("category", "fact"),
                key=item["key"],
                value=item["value"],
            )
            await self.memory_repo.upsert_entry(entry)
            logger.info(
                "Memory entry set",
                user_id=user_id,
                category=entry.category,
                key=entry.key,
            )

        for key in delete_keys:
            # Try both categories
            await self.memory_repo.delete_entry(user_id, "profile", key)
            await self.memory_repo.delete_entry(user_id, "fact", key)
            logger.info("Memory entry deleted", user_id=user_id, key=key)

        # Enforce max facts limit
        if set_entries:
            await self.memory_repo.delete_oldest_facts(user_id, self.max_facts)

    def strip_memory_block(self, response_text: str) -> str:
        """Remove <memory_update> block from user-visible response."""
        return MEMORY_UPDATE_PATTERN.sub("", response_text).rstrip()
