"""CLI interface for the reminders system.

Allows Claude (via skills) to list, add, cancel, suppress, and inspect
reminders without going through the Telegram bot.

Usage (from project root):
    python -m src.reminders.cli list
    python -m src.reminders.cli add --hint "Pack passport" --time "2026-03-16T08:00" --urgency high
    python -m src.reminders.cli cancel <id>
    python -m src.reminders.cli suppress --topic "gym" --scope week
    python -m src.reminders.cli log [--n 20]

All times are interpreted as UTC+7 (Bangkok) on input and displayed in UTC+7 on output.
All storage is UTC ISO-8601 strings, consistent with the rest of the project.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

TZ_BKK = timezone(timedelta(hours=7))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    """Resolve SQLite DB path from DATABASE_URL env var or default."""
    url = os.getenv("DATABASE_URL", "sqlite:///data/database.db")
    if url.startswith("sqlite:///"):
        raw = url[len("sqlite:///"):]
    elif url.startswith("sqlite://"):
        raw = url[len("sqlite://"):]
    else:
        raw = url
    p = Path(raw)
    if not p.is_absolute():
        # Resolve relative to the project root (two dirs above this file)
        project_root = Path(__file__).resolve().parent.parent.parent
        p = project_root / p
    return p


def _connect() -> sqlite3.Connection:
    path = _db_path()
    if not path.exists():
        _err(f"Database not found: {path}")
    conn = sqlite3.connect(str(path), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _to_utc(dt_local: datetime) -> datetime:
    """Convert a UTC+7 naive datetime to UTC-aware datetime."""
    aware_bkk = dt_local.replace(tzinfo=TZ_BKK)
    return aware_bkk.astimezone(UTC)


def _to_bkk(dt_utc: datetime) -> datetime:
    """Convert a UTC-aware datetime to UTC+7."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(TZ_BKK)


def _parse_input_time(raw: str) -> datetime:
    """Parse user-supplied datetime string (UTC+7 input) → UTC-aware datetime."""
    fmt_candidates = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ]
    for fmt in fmt_candidates:
        try:
            naive = datetime.strptime(raw, fmt)
            return _to_utc(naive)
        except ValueError:
            continue
    _err(f"Cannot parse time '{raw}'. Use format: YYYY-MM-DDTHH:MM")


def _fmt_dt(dt: datetime | None) -> str | None:
    """Format a UTC datetime as UTC+7 ISO string for output."""
    if dt is None:
        return None
    return _to_bkk(dt).strftime("%Y-%m-%dT%H:%M:%S%z")


def _err(msg: str) -> None:
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    """List unsent, non-cancelled, non-expired reminders for the next N days."""
    days = getattr(args, "days", 7)
    now_utc = _now_utc()
    cutoff = now_utc + timedelta(days=days)

    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT id, hint, scheduled_time, expires_at, urgency,
                   trigger_day, source_type, reminder_type, snooze_until,
                   retry_count, created_at
            FROM   reminders
            WHERE  sent      = 0
              AND  cancelled = 0
              AND  expired   = 0
              AND  failed    = 0
              AND  (scheduled_time IS NULL OR scheduled_time <= ?)
            ORDER  BY
                   CASE urgency
                       WHEN 'critical' THEN 0
                       WHEN 'high'     THEN 1
                       WHEN 'normal'   THEN 2
                       WHEN 'low'      THEN 3
                       ELSE 4
                   END,
                   scheduled_time ASC
            LIMIT  50
            """,
            (cutoff.isoformat(),),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        scheduled_raw = row["scheduled_time"]
        scheduled_dt = datetime.fromisoformat(scheduled_raw) if scheduled_raw else None
        if scheduled_dt and scheduled_dt.tzinfo is None:
            scheduled_dt = scheduled_dt.replace(tzinfo=UTC)

        expires_raw = row["expires_at"]
        expires_dt = datetime.fromisoformat(expires_raw) if expires_raw else None
        if expires_dt and expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=UTC)

        snooze_raw = row["snooze_until"]
        snooze_dt = datetime.fromisoformat(snooze_raw) if snooze_raw else None
        if snooze_dt and snooze_dt.tzinfo is None:
            snooze_dt = snooze_dt.replace(tzinfo=UTC)

        result.append(
            {
                "id": row["id"],
                "hint": row["hint"],
                "scheduled_time": _fmt_dt(scheduled_dt),
                "expires_at": _fmt_dt(expires_dt),
                "urgency": row["urgency"],
                "trigger_day": row["trigger_day"],
                "source_type": row["source_type"],
                "reminder_type": row["reminder_type"],
                "snooze_until": _fmt_dt(snooze_dt),
                "retry_count": row["retry_count"],
            }
        )

    if not result:
        print(json.dumps({"reminders": [], "message": "No upcoming reminders."}))
    else:
        print(json.dumps({"reminders": result}))


def cmd_add(args: argparse.Namespace) -> None:
    """Insert a new reminder into the DB."""
    hint = args.hint.strip()
    if not hint:
        _err("--hint cannot be empty")

    urgency = args.urgency or "normal"
    if urgency not in ("critical", "high", "normal", "low"):
        _err("--urgency must be one of: critical, high, normal, low")

    # Parse scheduled time (UTC+7 input → UTC stored)
    scheduled_utc: datetime | None = None
    trigger_day: str

    if args.time:
        scheduled_utc = _parse_input_time(args.time)
        trigger_day = _to_bkk(scheduled_utc).strftime("%Y-%m-%d")
    elif args.day:
        trigger_day = args.day
        # Default time: 9:00 AM Bangkok
        naive = datetime.strptime(f"{trigger_day}T09:00", "%Y-%m-%dT%H:%M")
        scheduled_utc = _to_utc(naive)
    else:
        # Default: tomorrow 9:00 AM Bangkok
        tomorrow = (_now_utc() + timedelta(days=1)).astimezone(TZ_BKK)
        trigger_day = tomorrow.strftime("%Y-%m-%d")
        naive = datetime.strptime(f"{trigger_day}T09:00", "%Y-%m-%dT%H:%M")
        scheduled_utc = _to_utc(naive)

    # expires_at: end of trigger_day in UTC+7 → UTC
    expires_naive = datetime.strptime(f"{trigger_day}T23:59:59", "%Y-%m-%dT%H:%M:%S")
    expires_utc = _to_utc(expires_naive)

    now_utc = _now_utc()
    reminder_id = str(uuid4())

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO reminders
                (id, hint, urgency, scheduled_time, expires_at, trigger_day,
                 source_type, reminder_type, sent, failed, cancelled, expired,
                 retry_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'conversation', 'primary',
                    0, 0, 0, 0, 0, ?)
            """,
            (
                reminder_id,
                hint,
                urgency,
                scheduled_utc.isoformat(),
                expires_utc.isoformat(),
                trigger_day,
                now_utc.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "id": reminder_id,
                "status": "created",
                "hint": hint,
                "scheduled_time": _fmt_dt(scheduled_utc),
                "trigger_day": trigger_day,
                "urgency": urgency,
            }
        )
    )


def cmd_cancel(args: argparse.Namespace) -> None:
    """Mark a reminder as cancelled."""
    reminder_id = args.id.strip()
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE reminders SET cancelled = 1 WHERE id = ?", (reminder_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            _err(f"Reminder not found: {reminder_id}")
    finally:
        conn.close()

    print(json.dumps({"id": reminder_id, "status": "cancelled"}))


def cmd_suppress(args: argparse.Namespace) -> None:
    """Add a suppression rule for a topic."""
    topic = args.topic.strip()
    if not topic:
        _err("--topic cannot be empty")

    scope_input = (args.scope or "permanent").lower()

    now_utc = _now_utc()
    now_bkk = _to_bkk(now_utc)
    rule_id = str(uuid4())
    expires_utc: datetime | None = None
    scope_db: str

    if scope_input in ("permanent", "never", "forever"):
        scope_db = "permanent"
        expires_utc = None
    elif scope_input in ("week", "this_week"):
        scope_db = "until"
        # Next Sunday 23:59:59 UTC+7
        days_until_sunday = (6 - now_bkk.weekday()) % 7 or 7
        sunday_naive = datetime.strptime(
            (now_bkk + timedelta(days=days_until_sunday)).strftime("%Y-%m-%d")
            + "T23:59:59",
            "%Y-%m-%dT%H:%M:%S",
        )
        expires_utc = _to_utc(sunday_naive)
    elif scope_input in ("day", "today"):
        scope_db = "until"
        today_naive = datetime.strptime(
            now_bkk.strftime("%Y-%m-%d") + "T23:59:59", "%Y-%m-%dT%H:%M:%S"
        )
        expires_utc = _to_utc(today_naive)
    else:
        _err("--scope must be one of: permanent, week, day")

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO suppression_rules
                (id, topic, scope, expires_at, trigger_count, added_at, added_by)
            VALUES (?, ?, ?, ?, 0, ?, 'user')
            """,
            (
                rule_id,
                topic,
                scope_db,
                expires_utc.isoformat() if expires_utc else None,
                now_utc.isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "id": rule_id,
                "status": "added",
                "topic": topic,
                "scope": scope_db,
                "expires_at": _fmt_dt(expires_utc),
            }
        )
    )


def cmd_log(args: argparse.Namespace) -> None:
    """Show recent planner log entries."""
    n = getattr(args, "n", 20)
    conn = _connect()
    try:
        cur = conn.execute(
            """
            SELECT id, event_time, action, reminder_id, source_event_id, reason
            FROM   planner_log
            ORDER  BY event_time DESC
            LIMIT  ?
            """,
            (n,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    entries = []
    for row in rows:
        et_raw = row["event_time"]
        et_dt = datetime.fromisoformat(et_raw) if et_raw else None
        if et_dt and et_dt.tzinfo is None:
            et_dt = et_dt.replace(tzinfo=UTC)
        entries.append(
            {
                "id": row["id"],
                "event_time": _fmt_dt(et_dt),
                "action": row["action"],
                "reminder_id": row["reminder_id"],
                "source_event_id": row["source_event_id"],
                "reason": row["reason"],
            }
        )

    print(json.dumps({"log": entries}))


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.reminders.cli",
        description="Reminders CLI — manage reminders directly in the DB.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List upcoming unsent reminders")
    p_list.add_argument(
        "--days",
        type=int,
        default=7,
        help="How many days ahead to look (default: 7)",
    )

    # add
    p_add = sub.add_parser("add", help="Add a reminder")
    p_add.add_argument("--hint", required=True, help="Reminder text")
    p_add.add_argument(
        "--time",
        help="Scheduled time in UTC+7, e.g. 2026-03-16T08:00",
    )
    p_add.add_argument(
        "--day",
        help="Trigger day YYYY-MM-DD (defaults to 9:00 AM UTC+7 if --time not given)",
    )
    p_add.add_argument(
        "--urgency",
        default="normal",
        choices=["critical", "high", "normal", "low"],
        help="Urgency level (default: normal)",
    )

    # cancel
    p_cancel = sub.add_parser("cancel", help="Cancel a reminder by ID")
    p_cancel.add_argument("id", help="Reminder UUID")

    # suppress
    p_sup = sub.add_parser("suppress", help="Add a suppression rule")
    p_sup.add_argument("--topic", required=True, help="Topic to suppress (plain text)")
    p_sup.add_argument(
        "--scope",
        default="permanent",
        choices=["permanent", "week", "day"],
        help="How long to suppress (default: permanent)",
    )

    # log
    p_log = sub.add_parser("log", help="Show recent planner log entries")
    p_log.add_argument(
        "--n", type=int, default=20, help="Number of entries to show (default: 20)"
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    dispatch = {
        "list": cmd_list,
        "add": cmd_add,
        "cancel": cmd_cancel,
        "suppress": cmd_suppress,
        "log": cmd_log,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
