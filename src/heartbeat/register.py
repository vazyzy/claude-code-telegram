"""Register the heartbeat scheduled job.

Run this script to add/update the daily heartbeat cron job
in the bot's scheduler database.

Usage:
    poetry run python -m src.heartbeat.register [--chat-id YOUR_CHAT_ID] [--cron "0 9 * * *"]
"""

import argparse
import asyncio
import sqlite3
from pathlib import Path


DEFAULT_CRON = "0 9 * * *"  # 9 AM daily
JOB_NAME = "project-heartbeat"
DB_PATH = Path(__file__).parent.parent.parent / "data" / "bot.db"


def register_heartbeat_job(chat_id: int, cron: str = DEFAULT_CRON) -> None:
    """Insert/update the heartbeat job directly in the scheduler DB."""
    # The prompt tells Claude to run the heartbeat gather script
    # and interpret the results
    prompt = (
        "Run `python3 -c \""
        "import sys; sys.path.insert(0, '.'); "
        "from src.heartbeat.prompt import build_heartbeat_prompt; "
        "print(build_heartbeat_prompt())"
        "\"` in the bot directory to get current project data, "
        "then follow the instructions in the output to write the daily message."
    )

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """
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
            """
        )

        # Remove old heartbeat job if exists
        conn.execute(
            "DELETE FROM scheduled_jobs WHERE job_name = ?",
            (JOB_NAME,),
        )

        conn.execute(
            """
            INSERT INTO scheduled_jobs
            (job_id, job_name, cron_expression, prompt, target_chat_ids,
             working_directory, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                f"heartbeat-{chat_id}",
                JOB_NAME,
                cron,
                prompt,
                str(chat_id),
                str(Path(__file__).parent.parent.parent),
                ),
        )
        conn.commit()
        print(f"Heartbeat job registered: cron='{cron}', chat_id={chat_id}")
        print(f"DB: {DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register heartbeat scheduled job")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat ID")
    parser.add_argument("--cron", default=DEFAULT_CRON, help=f"Cron expression (default: {DEFAULT_CRON})")
    args = parser.parse_args()

    register_heartbeat_job(args.chat_id, args.cron)
