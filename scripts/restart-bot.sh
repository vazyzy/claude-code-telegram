#!/usr/bin/env bash
# Restart the Telegram bot in a tmux session.
# Safe to call repeatedly — kills existing instance first.

set -euo pipefail

SESSION_NAME="claude-bot"
PORT=8080
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# 1. Kill existing tmux session
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Killing existing tmux session '$SESSION_NAME'..."
    tmux kill-session -t "$SESSION_NAME"
    sleep 1
fi

# 2. Kill any leftover process on the API port
PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
if [ -n "$PID" ]; then
    echo "Killing leftover process on port $PORT (PID $PID)..."
    kill "$PID" 2>/dev/null || true
    sleep 1
fi

# 3. Start new tmux session with clean environment
#    - unset CLAUDECODE so the bot's Claude SDK doesn't think it's nested
tmux new-session -d -s "$SESSION_NAME" -c "$PROJECT_DIR" \
    "unset CLAUDECODE; poetry run claude-telegram-bot"

echo "Bot started in tmux session '$SESSION_NAME'"
echo "  Attach:  tmux attach -t $SESSION_NAME"
echo "  Logs:    tmux capture-pane -t $SESSION_NAME -p"
echo "  Stop:    tmux kill-session -t $SESSION_NAME"
