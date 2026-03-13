"""MCP server exposing Telegram-specific tools to Claude.

Runs as a stdio transport server. The ``send_image_to_user`` tool validates
file existence and extension, then returns a success string. Actual Telegram
delivery is handled by the bot's stream callback which intercepts the tool
call.

The ``ask_user_via_telegram`` tool sends an inline-keyboard question to the
configured Telegram chat and waits for the user to tap a button.  The
running ``claude-code-telegram`` bot handles the tap and writes the answer
to a file that this tool reads back.  This replaces the broken terminal
``AskUserQuestion`` dialog when Claude Code is used standalone.
"""

import json
import os
import time
import uuid
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

# Directory used to exchange answers between this MCP tool and the bot.
# Both processes run as the same user so there are no permission issues.
PENDING_DIR = Path.home() / ".claude" / "ask-user"

DEFAULT_CHAT_ID = "163907541"
ENV_FILE = Path.home() / "projects" / "claude-code-telegram" / ".env"

mcp = FastMCP("telegram")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_bot_token() -> str:
    """Read TELEGRAM_BOT_TOKEN from the environment or .env file."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token and ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN=") and not line.startswith("#"):
                token = line.split("=", 1)[1].strip().strip("'\"")
                break
    return token


def _get_chat_id() -> str:
    """Return the target chat ID from env or fall back to the default."""
    return os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def send_image_to_user(file_path: str, caption: str = "") -> str:
    """Send an image file to the Telegram user.

    Args:
        file_path: Absolute path to the image file.
        caption: Optional caption to display with the image.

    Returns:
        Confirmation string when the image is queued for delivery.
    """
    path = Path(file_path)

    if not path.is_absolute():
        return f"Error: path must be absolute, got '{file_path}'"

    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return (
            f"Error: unsupported image extension '{path.suffix}'. "
            f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )

    if not path.is_file():
        return f"Error: file not found: {file_path}"

    return f"Image queued for delivery: {path.name}"


@mcp.tool()
async def ask_user_via_telegram(
    question: str,
    options: list,
    timeout: int = 120,
) -> str:
    """Ask the user a question via Telegram inline keyboard buttons.

    Sends a message with inline keyboard buttons to the configured Telegram
    chat and waits for the user to tap one.  The ``claude-code-telegram`` bot
    must be running — it handles the button tap and writes the answer back so
    this tool can return it.

    Use this instead of AskUserQuestion for interactive prompts; the native
    AskUserQuestion terminal dialog auto-submits immediately in some terminal
    configurations.

    Args:
        question: The question to ask.
        options: List of option label strings (become buttons).  Pass simple
                 strings, e.g. ``["Yes", "No", "Cancel"]``.
        timeout: Seconds to wait for a tap before falling back to the first
                 option (default 120).

    Returns:
        The label of the chosen option, or the first option on timeout.
    """
    token = _get_bot_token()
    if not token:
        return "Error: TELEGRAM_BOT_TOKEN not set — cannot send question."

    if not options:
        return "Error: options list is empty."

    chat_id = _get_chat_id()
    question_id = uuid.uuid4().hex[:12]

    # Ensure the pending-answers directory exists.
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    answer_file = PENDING_DIR / question_id

    # Build inline keyboard.  Each option becomes a button whose callback_data
    # is ``askmcp:<question_id>:<label>``.  Telegram limits callback_data to
    # 64 bytes, so we truncate long labels.
    keyboard = []
    for label in options:
        label_str = str(label)
        cb = f"askmcp:{question_id}:{label_str}"
        if len(cb.encode()) > 64:
            # Truncate the label to fit — keep prefix + separator
            max_label = 64 - len(f"askmcp:{question_id}:".encode())
            label_str = label_str[:max_label]
            cb = f"askmcp:{question_id}:{label_str}"
        keyboard.append([{"text": label_str, "callback_data": cb}])

    payload = {
        "chat_id": chat_id,
        "text": f"❓ {question}",
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": keyboard}),
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
            )
            resp.raise_for_status()
    except Exception as exc:
        return f"Error sending Telegram message: {exc}"

    # Poll the answer file until the bot writes to it or timeout expires.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if answer_file.exists():
            try:
                chosen = answer_file.read_text().strip()
                answer_file.unlink(missing_ok=True)
                return chosen if chosen else (str(options[0]) if options else "")
            except OSError:
                pass
        time.sleep(1)

    # Timed out — clean up and return the first option as default.
    answer_file.unlink(missing_ok=True)
    default = str(options[0]) if options else ""
    # Notify the chat that we timed out.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": f"⏰ No selection within {timeout}s — using default: <b>{default}</b>",
                    "parse_mode": "HTML",
                },
            )
    except Exception:
        pass

    return default


if __name__ == "__main__":
    mcp.run(transport="stdio")
