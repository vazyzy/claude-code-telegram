---
title: "Tasks: TTS Voice Responses"
status: draft
created: 2026-03-14
---

# Task Breakdown

## Summary

| Metric | Value |
|--------|-------|
| Total tasks | 10 |
| Critical path length | 4 tasks (T-001 → T-003 → T-004 → T-005) |
| Phase A (MVP) | 5 tasks |
| Phase B (Full) | 3 tasks |
| Phase C (Polish) | 2 tasks |

## Phase A — MVP

Voice-in → voice-out works end-to-end.

### T-001: Add edge-tts dependency and tts extras group
**Requirements:** FR-1
**Component:** `pyproject.toml`
**Complexity:** S
**Dependencies:** none
**Description:** Add `edge-tts` as an optional dependency. Create a `tts` extras group (`[tool.poetry.extras] tts = ["edge-tts"]`). Keep it separate from the existing `voice` extras (STT) to allow independent installation.

### T-002: Add TTS config fields to Settings
**Requirements:** FR-5
**Component:** `src/config/settings.py`
**Complexity:** S
**Dependencies:** none
**Description:** Add fields mirroring existing voice config pattern:
- `enable_voice_responses: bool = False` — feature flag
- `tts_provider: Literal["edge-tts", "openai"] = "edge-tts"` — engine selection
- `tts_voice: str = "en-US-AriaNeural"` — default edge-tts voice
- `tts_max_text_length: int = 4000` — max chars before chunking

Add cross-field validator: if `tts_provider == "openai"`, require `openai_api_key`.

### T-003: Create TTSHandler class
**Requirements:** FR-1, FR-2
**Component:** `src/bot/features/tts_handler.py` (new file)
**Complexity:** M
**Dependencies:** T-001
**Description:** Create `TTSHandler` class following `VoiceHandler` pattern:
- `__init__(self, config: Settings)` — store config, lazy-init clients
- `async def synthesize(self, text: str) -> bytes` — convert text to OGG Opus bytes
- Edge-tts implementation: use `edge_tts.Communicate` with output format `audio-24khz-48kbitrate-mono-opus` (native Opus, no ffmpeg needed)
- Return raw bytes suitable for `bot.send_voice()`
- Structured logging with `structlog`

### T-004: Add content filtering for TTS
**Requirements:** FR-3
**Component:** `src/bot/features/tts_handler.py`
**Complexity:** M
**Dependencies:** T-003
**Description:** Add content-aware filtering before TTS synthesis:
- `_strip_code_blocks(text: str) -> str` — remove ``` fenced code blocks and inline `code`
- `_is_mostly_code(text: str) -> bool` — return True if >60% of response is code/structured content
- `async def prepare_for_tts(self, text: str) -> Optional[str]` — returns cleaned text or None if response should skip voice
- Strip markdown formatting (headers, bold, links) into plain text

### T-005: Integrate TTS into agentic_voice flow
**Requirements:** FR-4, FR-5
**Component:** `src/bot/orchestrator.py`, `src/bot/features/registry.py`
**Complexity:** M
**Dependencies:** T-002, T-003, T-004
**Description:**
1. **Registry:** Add `TTSHandler` registration in `FeatureRegistry._initialize_features()` — check `enable_voice_responses` flag, instantiate if enabled, add `get_tts_handler()` accessor.
2. **Orchestrator:** In `agentic_voice` handler (and `_handle_agentic_media_message`), after sending text response:
   - Get TTSHandler from features
   - Call `prepare_for_tts(response_text)` — skip if None
   - Call `synthesize(cleaned_text)` to get OGG bytes
   - Call `await update.message.reply_voice(voice=bytes)` to send voice bubble
   - Wrap in try/except — TTS failure must never block text delivery
3. Show "🎙 Generating voice..." status during TTS.

## Phase B — Full Feature

Fallback provider, long text handling, tests.

### T-006: Add OpenAI TTS fallback provider
**Requirements:** FR-6
**Component:** `src/bot/features/tts_handler.py`
**Complexity:** M
**Dependencies:** T-003
**Description:** Add OpenAI TTS support in `TTSHandler`:
- `async def _synthesize_openai(self, text: str) -> bytes` — call `gpt-4o-mini-tts` with `response_format="opus"`
- Lazy-init `AsyncOpenAI` client (reuse existing pattern from `VoiceHandler._get_openai_client`)
- Provider selection in `synthesize()` based on `config.tts_provider`
- Auto-fallback: if edge-tts fails (service unavailable), try OpenAI if key is configured

### T-007: Text chunking for long responses
**Requirements:** FR-7
**Component:** `src/bot/features/tts_handler.py`
**Complexity:** S
**Dependencies:** T-003, T-004
**Description:** Add text chunking for responses exceeding `tts_max_text_length`:
- `_chunk_text(text: str, max_chars: int) -> List[str]` — split at sentence boundaries (`. `, `? `, `! `)
- In orchestrator: send multiple sequential voice messages for chunked responses
- Cap at 3 voice messages max to avoid spam

### T-008: Unit tests for TTSHandler
**Requirements:** —
**Component:** `tests/test_tts_handler.py` (new file)
**Complexity:** M
**Dependencies:** T-003, T-004
**Description:** Write unit tests covering:
- `_strip_code_blocks()` — various code block formats
- `_is_mostly_code()` — threshold detection
- `prepare_for_tts()` — returns None for code-heavy, cleaned text otherwise
- `_chunk_text()` — sentence boundary splitting
- `synthesize()` — mock edge-tts, verify OGG bytes returned
- Error handling — TTS failure returns gracefully

## Phase C — Polish

Documentation and language detection.

### T-009: Update docs and .env.example
**Requirements:** —
**Component:** `.env.example`, `docs/`
**Complexity:** S
**Dependencies:** T-002
**Description:** Add TTS config section to `.env.example`:
```
# === TTS VOICE RESPONSES ===
ENABLE_VOICE_RESPONSES=false
TTS_PROVIDER=edge-tts
TTS_VOICE=en-US-AriaNeural
TTS_MAX_TEXT_LENGTH=4000
```
Update any relevant docs files with the new feature flag and configuration options.

### T-010: Language auto-detection for voice selection
**Requirements:** FR-8
**Component:** `src/bot/features/tts_handler.py`
**Complexity:** M
**Dependencies:** T-003
**Risk:** UNCERTAIN
**Description:** Auto-detect response language and select matching edge-tts voice:
- Use simple heuristic (character set detection) or lightweight library
- Map detected language to edge-tts voice (edge-tts has 300+ voices across 70+ locales)
- Fall back to configured `tts_voice` default if detection fails
- **Why UNCERTAIN:** Scope may grow — multilingual users, per-user voice preferences (stored in SQLite). May need product input.

## Dependency Graph

```
T-001 (edge-tts dep)──────────┐
                               ├──▶ T-003 (TTSHandler) ──┬──▶ T-004 (content filter) ──┐
T-002 (config fields) ────────┤                          │                              │
                               │                          ├──▶ T-006 (OpenAI fallback)  │
                               │                          ├──▶ T-007 (chunking)         │
                               │                          ├──▶ T-008 (tests)            │
                               │                          └──▶ T-010 (lang detect)      │
                               │                                                        │
                               └──▶ T-009 (docs)                                       │
                                                                                        │
                               T-005 (orchestrator integration) ◀───────────────────────┘
                                    depends on: T-002, T-003, T-004
```

**Critical path:** T-001 → T-003 → T-004 → T-005 (4 tasks, all Phase A)

## Risk Register

| Task | Risk | Mitigation |
|------|------|-----------|
| T-003 | EXTERNAL — edge-tts depends on Microsoft's Edge Read Aloud service (no SLA, free tier) | OpenAI TTS fallback (T-006); edge-tts has been stable for 2+ years per GitHub |
| T-005 | Integration — TTS adds latency after Claude response | TTS runs after text is already delivered; failure never blocks text |
| T-010 | UNCERTAIN — language detection scope may grow | Keep MVP simple (heuristic only); defer per-user preferences to future iteration |

## Parallelization Opportunities

Tasks that can run in parallel within each phase:
- **Phase A:** T-001 and T-002 can run in parallel (no dependencies between them)
- **Phase B:** T-006, T-007, and T-008 can all run in parallel (all depend only on T-003/T-004)
- **Phase C:** T-009 and T-010 can run in parallel

Minimum serial depth: 4 steps (T-001 → T-003 → T-004 → T-005)
