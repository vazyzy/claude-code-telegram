# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram bot providing remote access to Claude Code. Python 3.10+, built with Poetry, using `python-telegram-bot` for Telegram and `claude-agent-sdk` for Claude Code integration.

## Commands

```bash
make dev              # Install all deps (including dev)
make install          # Production deps only
make run              # Run the bot
make run-debug        # Run with debug logging
make test             # Run tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format with black + isort

# Run a single test
poetry run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
poetry run mypy src
```

## Architecture

### Claude SDK Integration

`ClaudeIntegration` (facade in `src/claude/facade.py`) wraps `ClaudeSDKManager` (`src/claude/sdk_integration.py`), which uses `claude-agent-sdk` with `ClaudeSDKClient` for async streaming. Session IDs come from Claude's `ResultMessage`, not generated locally.

Sessions auto-resume: per user+directory, persisted in SQLite.

### Request Flow

**Agentic mode** (default, `AGENTIC_MODE=true`):

```
Telegram message -> Security middleware (group -3) -> Auth middleware (group -2)
-> Rate limit (group -1) -> MessageOrchestrator.agentic_text() (group 10)
-> ClaudeIntegration.run_command() -> SDK
-> Response parsed -> Stored in SQLite -> Sent back to Telegram
```

**External triggers** (webhooks, scheduler):

```
Webhook POST /webhooks/{provider} -> Signature verification -> Deduplication
-> Publish WebhookEvent to EventBus -> AgentHandler.handle_webhook()
-> ClaudeIntegration.run_command() -> Publish AgentResponseEvent
-> NotificationService -> Rate-limited Telegram delivery
```

**Classic mode** (`AGENTIC_MODE=false`): Same middleware chain, but routes through full command/message handlers in `src/bot/handlers/` with 13 commands and inline keyboards.

### Dependency Injection

Bot handlers access dependencies via `context.bot_data`:
```python
context.bot_data["auth_manager"]
context.bot_data["claude_integration"]
context.bot_data["storage"]
context.bot_data["security_validator"]
```

### Key Directories

- `src/config/` -- Pydantic Settings v2 config with env detection, feature flags (`features.py`), YAML project loader (`loader.py`)
- `src/bot/handlers/` -- Telegram command, message, and callback handlers (classic mode + project thread commands)
- `src/bot/middleware/` -- Auth, rate limit, security input validation
- `src/bot/features/` -- Git integration, file handling, quick actions, session export
- `src/bot/orchestrator.py` -- MessageOrchestrator: routes to agentic or classic handlers, project-topic routing
- `src/claude/` -- Claude integration facade, SDK/CLI managers, session management, tool monitoring
- `src/projects/` -- Multi-project support: `registry.py` (YAML project config), `thread_manager.py` (Telegram topic sync/routing)
- `src/storage/` -- SQLite via aiosqlite, repository pattern (users, sessions, messages, tool_usage, audit_log, cost_tracking, project_threads)
- `src/security/` -- Multi-provider auth (whitelist + token), input validators (with optional `disable_security_patterns`), rate limiter, audit logging
- `src/events/` -- EventBus (async pub/sub), event types, AgentHandler, EventSecurityMiddleware
- `src/api/` -- FastAPI webhook server, GitHub HMAC-SHA256 + Bearer token auth
- `src/scheduler/` -- APScheduler cron jobs, persistent storage in SQLite
- `src/notifications/` -- NotificationService, rate-limited Telegram delivery

### Security Model

5-layer defense: authentication (whitelist/token) -> directory isolation (APPROVED_DIRECTORY + path traversal prevention) -> input validation (blocks `..`, `;`, `&&`, `$()`, etc.) -> rate limiting (token bucket) -> audit logging.

`SecurityValidator` blocks access to secrets (`.env`, `.ssh`, `id_rsa`, `.pem`) and dangerous shell patterns. Can be relaxed with `DISABLE_SECURITY_PATTERNS=true` (trusted environments only).

`ToolMonitor` validates Claude's tool calls against allowlist/disallowlist, file path boundaries, and dangerous bash patterns. Tool name validation can be bypassed with `DISABLE_TOOL_VALIDATION=true`.

Webhook authentication: GitHub HMAC-SHA256 signature verification, generic Bearer token for other providers, atomic deduplication via `webhook_events` table.

### Configuration

Settings loaded from environment variables via Pydantic Settings. Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `APPROVED_DIRECTORY`. Key optional: `ALLOWED_USERS` (comma-separated Telegram IDs), `ANTHROPIC_API_KEY`, `ENABLE_MCP`, `MCP_CONFIG_PATH`.

Agentic platform settings: `AGENTIC_MODE` (default true), `ENABLE_API_SERVER`, `API_SERVER_PORT` (default 8080), `GITHUB_WEBHOOK_SECRET`, `WEBHOOK_API_SECRET`, `ENABLE_SCHEDULER`, `NOTIFICATION_CHAT_IDS`.

Security relaxation (trusted environments only): `DISABLE_SECURITY_PATTERNS` (default false), `DISABLE_TOOL_VALIDATION` (default false).

Multi-project topics: `ENABLE_PROJECT_THREADS` (default false), `PROJECT_THREADS_MODE` (`private`|`group`), `PROJECT_THREADS_CHAT_ID` (required for group mode), `PROJECTS_CONFIG_PATH` (path to YAML project registry), `PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS` (default `1.1`, set `0` to disable pacing). See `config/projects.example.yaml`.

Output verbosity: `VERBOSE_LEVEL` (default 1, range 0-2). Controls how much of Claude's background activity is shown to the user in real-time. 0 = quiet (only final response, typing indicator still active), 1 = normal (tool names + reasoning snippets shown during execution), 2 = detailed (tool names with input summaries + longer reasoning text). Users can override per-session via `/verbose 0|1|2`. A persistent typing indicator is refreshed every ~2 seconds at all levels.

Voice transcription: `ENABLE_VOICE_MESSAGES` (default true), `VOICE_PROVIDER` (`mistral`|`openai`, default `mistral`), `MISTRAL_API_KEY`, `OPENAI_API_KEY`, `VOICE_TRANSCRIPTION_MODEL`. Provider implementation is in `src/bot/features/voice_handler.py`.

Feature flags in `src/config/features.py` control: MCP, git integration, file uploads, quick actions, session export, image uploads, voice messages, conversation mode, agentic mode, API server, scheduler.

### DateTime Convention

All datetimes use timezone-aware UTC: `datetime.now(UTC)` (not `datetime.utcnow()`). SQLite adapters auto-convert TIMESTAMP/DATETIME columns to `datetime` objects via `detect_types=PARSE_DECLTYPES`. Model `from_row()` methods must guard `fromisoformat()` calls with `isinstance(val, str)` checks.

## Telegram Formatting Guidelines

Messages are sent to Telegram. You MUST follow these rules strictly — broken formatting looks unprofessional and confuses the user.

### Inline formatting rules

Telegram uses its own MarkdownV2 syntax. Key differences from standard markdown:

1. **Keep bold text simple.** Bold should only wrap plain words — never put backticks, parentheses, brackets, or special characters inside bold markers.
   - WRONG: `**Claude tool-use for structured output** — call \`messages.create()\``
   - RIGHT: `Claude tool-use for structured output — call messages.create()`
   - RIGHT: Bold only plain labels: `**Option 1** — Claude tool-use for structured output`

2. **Avoid mixing formatting.** Never nest bold inside italic, or code inside bold. Use one format per span.

3. **Use code blocks for technical content.** Anything with function names, code snippets, URLs, file paths, or special characters goes in a code block (triple backtick), not inline.
   - WRONG: `call \`messages.create()\` with \`tools=[PLANNER_TOOL]\``
   - RIGHT: put it in a code block:
     ```
     call messages.create() with tools=[PLANNER_TOOL]
     ```

4. **Bullet points.** Use `•` (bullet character) or `—` (em dash) for lists. Avoid `-` as a list marker (it needs escaping in MarkdownV2).

5. **Special characters that break MarkdownV2:** These must be escaped with `\` if used outside code blocks: `_ * [ ] ( ) ~ > # + - = | { } . !`
   Simplest rule: if your sentence has parentheses or brackets, put the whole technical phrase in a code block instead.

6. **Prefer plain text over heavy formatting.** A clean, readable message with minimal formatting is better than a heavily formatted one that breaks. When in doubt, use less formatting.

### Structural formatting rules

Telegram does NOT support markdown tables. Never use `| col | col |` syntax — it renders as broken plain text.

Instead, use one of these approaches:

**For small data (≤5 rows):** Use bold labels with line breaks
```
**Node.js** — v24.13.1
**Python** — 3.12.3
**Docker** — 29.2.1
```

**For larger structured data:** Use a monospace code block with manually aligned columns.
Rules:
- Pad every column value with spaces to the same width
- Use a `───` separator line after the header
- Use a blank line between every data row for readability
- Right-align numeric columns

```
Project      Status   Commits
─────────────────────────────
Pro Hub      active       156

Moral Code   active        84

Claude Bot   active        42

Arbitr       paused        12
```

**For comparison/matrix data:** Use grouped bullet lists
```
• Pro Hub
  Status: active | Commits: 156 | Priority: high

• Moral Code
  Status: active | Commits: 84 | Priority: highest
```

### General principle

When composing a Telegram message, ask: "Would this render cleanly if sent as-is?" If any line mixes bold/italic/code with special characters, simplify it. Plain text that reads well always beats broken rich formatting.

## Code Style

- Black (88 char line length), isort (black profile), flake8, mypy strict, autoflake for unused imports
- pytest-asyncio with `asyncio_mode = "auto"`
- structlog for all logging (JSON in prod, console in dev)
- Type hints required on all functions (`disallow_untyped_defs = true`)
- Use `datetime.now(UTC)` not `datetime.utcnow()` (deprecated)

## Adding a New Bot Command

### Agentic mode

Agentic mode commands: `/start`, `/new`, `/status`, `/verbose`, `/repo`. If `ENABLE_PROJECT_THREADS=true`: `/sync_threads`. To add a new command:

1. Add handler function in `src/bot/orchestrator.py`
2. Register in `MessageOrchestrator._register_agentic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()` for Telegram's command menu
4. Add audit logging for the command

### Classic mode

1. Add handler function in `src/bot/handlers/command.py`
2. Register in `MessageOrchestrator._register_classic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()` for Telegram's command menu
4. Add audit logging for the command
