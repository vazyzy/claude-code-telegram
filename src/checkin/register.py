"""Register daily morning/evening mental health check-in scheduled jobs.

Run once to add the jobs to the bot's scheduler database.

Usage:
    poetry run python -m src.checkin.register --chat-id 163907541
"""

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "bot.db"

# The journalling skill handles all questions, file paths, and git commits.
# The bot only needs to know: which skill, and which session argument.
SKILL_NAME = "journalling"
MORNING_PROMPT = "morning"
EVENING_PROMPT = "evening"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            job_id TEXT PRIMARY KEY,
            job_name TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            prompt TEXT NOT NULL,
            target_chat_ids TEXT DEFAULT '',
            working_directory TEXT DEFAULT '',
            skill_name TEXT,
            created_by INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def register(chat_id: int,
             morning_cron: str = "30 2 * * *",
             evening_cron: str = "0 15 * * *") -> None:
    """Insert/update morning and evening check-in jobs."""
    bot_dir = str(Path(__file__).parent.parent.parent)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        _ensure_table(conn)

        for job_id, name, cron, prompt in [
            (f"checkin-morning-{chat_id}", "checkin-morning", morning_cron, MORNING_PROMPT),
            (f"checkin-evening-{chat_id}", "checkin-evening", evening_cron, EVENING_PROMPT),
        ]:
            conn.execute("DELETE FROM scheduled_jobs WHERE job_name = ?", (name,))
            conn.execute("""
                INSERT INTO scheduled_jobs
                    (job_id, job_name, cron_expression, prompt,
                     target_chat_ids, working_directory, skill_name, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (job_id, name, cron, prompt, str(chat_id), bot_dir, SKILL_NAME))

        conn.commit()
        print(f"✅ Morning check-in registered: cron='{morning_cron}'")
        print(f"✅ Evening check-in registered: cron='{evening_cron}'")
        print(f"   chat_id={chat_id}, skill={SKILL_NAME}, DB={DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register daily check-in jobs")
    parser.add_argument("--chat-id", type=int, default=163907541)
    parser.add_argument("--morning-cron", default="30 2 * * *",
                        help="Morning cron (UTC). Default: 30 2 * * * = 9:30 AM Bangkok")
    parser.add_argument("--evening-cron", default="0 15 * * *",
                        help="Evening cron (UTC). Default: 0 15 * * * = 10:00 PM Bangkok")
    args = parser.parse_args()
    register(args.chat_id, args.morning_cron, args.evening_cron)
