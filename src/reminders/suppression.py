"""Reminder suppression logic for the reminders module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import structlog

from src.reminders.models import ScopeType, SuppressionRule
from src.storage.database import DatabaseManager

UTC = timezone.utc
_UTC7 = timezone(timedelta(hours=7))

logger = structlog.get_logger()


class SuppressionService:
    """Manage topic-based suppression rules that prevent reminder delivery."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_active_rules(self) -> list[SuppressionRule]:
        """Return rules that are currently active.

        A rule is active when:
        - scope = 'permanent', OR
        - scope = 'until' AND (expires_at IS NULL OR expires_at > now)
        """
        now_iso = datetime.now(UTC).isoformat()
        sql = """
            SELECT * FROM suppression_rules
            WHERE scope = 'permanent'
               OR (scope = 'until' AND (expires_at IS NULL OR expires_at > ?))
            ORDER BY added_at DESC
        """
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(sql, (now_iso,))
            rows = await cursor.fetchall()

        rules = [SuppressionRule.from_row(dict(row)) for row in rows]
        logger.debug(
            "suppression.get_active_rules",
            count=len(rules),
        )
        return rules

    async def get_all_for_review(self) -> list[SuppressionRule]:
        """Return all active rules with their trigger counts for monthly review.

        Semantically identical to get_active_rules — trigger_count is always
        included in the model; this method is a named alias for review callers.
        """
        rules = await self.get_active_rules()
        logger.debug("suppression.get_all_for_review", count=len(rules))
        return rules

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def add_rule(
        self,
        topic: str,
        scope: ScopeType,
        expires_at: Optional[datetime] = None,
    ) -> SuppressionRule:
        """Insert a new suppression rule and return the created record."""
        rule_id = str(uuid4())
        now_utc = datetime.now(UTC)
        expires_at_iso: Optional[str] = expires_at.isoformat() if expires_at else None

        sql = """
            INSERT INTO suppression_rules
                (id, topic, scope, expires_at, trigger_count, added_at, added_by)
            VALUES (?, ?, ?, ?, 0, ?, 'user')
        """
        async with self.db.get_connection() as conn:
            await conn.execute(
                sql,
                (rule_id, topic, scope, expires_at_iso, now_utc.isoformat()),
            )
            await conn.commit()

        rule = SuppressionRule(
            id=rule_id,
            topic=topic,
            scope=scope,
            expires_at=expires_at,
            trigger_count=0,
            added_at=now_utc,
            added_by="user",
        )
        logger.info(
            "suppression.add_rule",
            rule_id=rule_id,
            topic=topic,
            scope=scope,
            expires_at=expires_at_iso,
        )
        return rule

    async def remove_rule(self, rule_id: str) -> bool:
        """Delete a suppression rule by ID.

        Returns True if a row was found and deleted, False otherwise.
        """
        sql = "DELETE FROM suppression_rules WHERE id = ?"
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(sql, (rule_id,))
            await conn.commit()
            deleted = cursor.rowcount > 0

        logger.info("suppression.remove_rule", rule_id=rule_id, deleted=deleted)
        return deleted

    async def increment_trigger(self, rule_id: str) -> None:
        """Increment trigger_count for the given rule ID."""
        sql = "UPDATE suppression_rules SET trigger_count = trigger_count + 1 WHERE id = ?"
        async with self.db.get_connection() as conn:
            await conn.execute(sql, (rule_id,))
            await conn.commit()

        logger.debug("suppression.increment_trigger", rule_id=rule_id)

    # ------------------------------------------------------------------
    # Expiry helpers
    # ------------------------------------------------------------------

    @staticmethod
    def week_expiry() -> datetime:
        """Return next Sunday at 23:59:59 UTC+7, converted to UTC.

        If today is Sunday, returns the *following* Sunday (7 days ahead).
        """
        now_utc7 = datetime.now(_UTC7)
        # weekday(): Monday=0 … Sunday=6
        days_until_sunday = (6 - now_utc7.weekday()) % 7
        if days_until_sunday == 0:
            days_until_sunday = 7  # Today is Sunday → use next Sunday
        next_sunday = now_utc7.replace(
            hour=23, minute=59, second=59, microsecond=0
        ) + timedelta(days=days_until_sunday)
        return next_sunday.astimezone(UTC)

    @staticmethod
    def day_expiry() -> datetime:
        """Return today at 23:59:59 UTC+7, converted to UTC."""
        now_utc7 = datetime.now(_UTC7)
        end_of_day = now_utc7.replace(hour=23, minute=59, second=59, microsecond=0)
        return end_of_day.astimezone(UTC)
