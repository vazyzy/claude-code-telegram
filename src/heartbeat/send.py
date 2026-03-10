"""Standalone heartbeat sender.

Can be run directly via cron without the full bot running.
Gathers project data, sends to Claude CLI for interpretation,
delivers result via Telegram.

Usage:
    poetry run python -m src.heartbeat.send

Requires env vars: TELEGRAM_BOT_TOKEN
Optional: HEARTBEAT_CHAT_ID (or reads from .env)
          ANTHROPIC_API_KEY (if set, uses API directly; otherwise uses Claude CLI)
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx
import structlog

from .prompt import build_heartbeat_prompt, build_weekly_review_prompt

logger = structlog.get_logger()


async def send_telegram_message(token: str, chat_id: int, text: str) -> bool:
    """Send a message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        })
        if resp.status_code != 200:
            # Retry without markdown if formatting fails
            resp = await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
            })
        return resp.status_code == 200


async def _call_claude(prompt: str) -> str:
    """Call Claude via API or CLI."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        # Use API directly
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    else:
        # Use Claude CLI (uses OAuth session, no API key needed)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--model", "claude-sonnet-4-20250514",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Claude CLI failed: {stderr.decode()}")
        return stdout.decode().strip()


async def generate_heartbeat_message() -> str:
    """Generate daily heartbeat."""
    return await _call_claude(build_heartbeat_prompt())


async def generate_weekly_review() -> str:
    """Generate weekly review."""
    return await _call_claude(build_weekly_review_prompt())


def _load_env() -> tuple:
    """Load env and return (token, chat_id)."""
    try:
        from dotenv import load_dotenv
        env_file = Path(__file__).parent.parent.parent / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("HEARTBEAT_CHAT_ID") or os.environ.get("NOTIFICATION_CHAT_IDS", "").split(",")[0].strip()
    return token, chat_id


async def main() -> None:
    """Gather data, generate message, send via Telegram."""
    token, chat_id = _load_env()

    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or HEARTBEAT_CHAT_ID")
        sys.exit(1)

    # Check if weekly review mode
    is_weekly = "--weekly" in sys.argv

    if is_weekly:
        print("Generating weekly review...")
        message = await generate_weekly_review()
    else:
        print("Gathering project data...")
        message = await generate_heartbeat_message()

    print(f"Generated message ({len(message)} chars):")
    print(message)
    print()

    print(f"Sending to chat {chat_id}...")
    success = await send_telegram_message(token, int(chat_id), message)
    print("Sent!" if success else "Failed to send")


if __name__ == "__main__":
    asyncio.run(main())
