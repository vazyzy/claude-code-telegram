"""Register daily morning/evening mental health check-in scheduled jobs.

Run once to add the jobs to the bot's scheduler database.

Usage:
    poetry run python -m src.checkin.register --chat-id 163907541
"""

import argparse
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent.parent / "data" / "bot.db"
OBSIDIAN_DAILY = "/home/vazyzy/projects/obsidian/Health/Daily"

MORNING_PROMPT = f"""Morning mental health check-in. Today is {{TODAY}}.

Ask the user the following questions ONE BY ONE using AskUserQuestion. After ALL answers are collected, write the results to Obsidian and commit.

QUESTIONS (ask in this exact order):

1. header="Sleep" question="How many hours did you sleep last night?"
   options: "<4h", "4–5h", "5–6h", "6–7h", "7–8h", "8h+"

2. header="Sleep need" question="Did you sleep LESS than usual but still feel energetic — not tired? (Early mania warning)"
   options: "Yes ⚠️", "No"

3. header="Depression" question="Depression level right now:"
   options: "0 – None", "1 – Mild", "2 – Moderate", "3 – Severe"

4. header="Mania" question="Elevated mood / mania level right now:"
   options: "0 – None", "1 – Mild", "2 – Moderate", "3 – Severe"

5. header="Meds" question="Did you take your medications today?"
   options: "Yes ✓", "No ✗"

After collecting all answers, write this file:
Path: {OBSIDIAN_DAILY}/{{TODAY}}.md

If the file already exists, ADD a ## Morning section. If not, create it:

---
date: {{TODAY}}
type: health-checkin
---

# Daily Check-In — {{TODAY}}

## Morning

| Variable | Value |
|----------|-------|
| Sleep hours | <answer> |
| Reduced sleep need | <answer> |
| Mood – depression | <answer> |
| Mood – mania/elevation | <answer> |
| Medications taken | <answer> |

Then run:
git -C /home/vazyzy/projects/obsidian -c user.name="Claude Code" -c user.email="claude@anthropic.com" add Health/Daily/ && git -C /home/vazyzy/projects/obsidian -c user.name="Claude Code" -c user.email="claude@anthropic.com" commit -m "health check-in: {{TODAY}} morning" && git -C /home/vazyzy/projects/obsidian push
"""

EVENING_PROMPT = f"""Evening mental health check-in. Today is {{TODAY}}.

Ask the user the following questions ONE BY ONE using AskUserQuestion. After ALL answers are collected, write the results to Obsidian and commit.

QUESTIONS (ask in this exact order):

1. header="Irritability" question="Irritability level today:"
   options: "0 – None", "1 – Mild", "2 – Moderate", "3 – High"

2. header="Depression" question="Depression level right now (end of day):"
   options: "0 – None", "1 – Mild", "2 – Moderate", "3 – Severe"

3. header="Mania" question="Elevated mood / mania level right now (end of day):"
   options: "0 – None", "1 – Mild", "2 – Moderate", "3 – Severe"

4. header="Event?" question="Did anything significant happen today? (stressor, trigger, big event)"
   options: "Yes", "No"

5. ONLY if the user answered "Yes" to question 4:
   header="Note" question="Briefly describe what happened (one line):"
   (free text — wait for user to type and send)

After collecting all answers, open {OBSIDIAN_DAILY}/{{TODAY}}.md.
If it exists, ADD a ## Evening section at the end. If not, create the file with the full header first.

## Evening

| Variable | Value |
|----------|-------|
| Irritability | <answer> |
| Mood – depression | <answer> |
| Mood – mania/elevation | <answer> |
| Significant event | <answer> |
| Event note | <answer or —> |

Then run:
git -C /home/vazyzy/projects/obsidian -c user.name="Claude Code" -c user.email="claude@anthropic.com" add Health/Daily/ && git -C /home/vazyzy/projects/obsidian -c user.name="Claude Code" -c user.email="claude@anthropic.com" commit -m "health check-in: {{TODAY}} evening" && git -C /home/vazyzy/projects/obsidian push
"""


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
             morning_cron: str = "5 3 * * *",
             evening_cron: str = "0 15 * * *") -> None:
    """Insert/update morning and evening check-in jobs."""
    today_placeholder = "{TODAY}"
    morning_prompt = MORNING_PROMPT.replace("{TODAY}", "$(date +%Y-%m-%d)")
    evening_prompt = EVENING_PROMPT.replace("{TODAY}", "$(date +%Y-%m-%d)")

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
                     target_chat_ids, working_directory, is_active)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            """, (job_id, name, cron, prompt, str(chat_id), bot_dir))

        conn.commit()
        print(f"✅ Morning check-in registered: cron='{morning_cron}'")
        print(f"✅ Evening check-in registered: cron='{evening_cron}'")
        print(f"   chat_id={chat_id}, DB={DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register daily check-in jobs")
    parser.add_argument("--chat-id", type=int, default=163907541)
    parser.add_argument("--morning-cron", default="5 3 * * *",
                        help="Morning cron (UTC). Default: 5 3 * * * = 10:05 AM Bangkok")
    parser.add_argument("--evening-cron", default="0 15 * * *",
                        help="Evening cron (UTC). Default: 0 15 * * * = 10:00 PM Bangkok")
    args = parser.parse_args()
    register(args.chat_id, args.morning_cron, args.evening_cron)
