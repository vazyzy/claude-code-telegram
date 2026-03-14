---
title: "Dev Plan: TTS Voice Responses"
status: draft
created: 2026-03-14
---

# Dev Plan: TTS Voice Responses

Add text-to-speech capability to the claude-code-telegram bot so that when users send voice messages, Claude's text response is also delivered as a Telegram voice message (OGG Opus). Uses edge-tts (free, neural quality, async, native Opus) as the primary engine with OpenAI TTS as a paid fallback. Content-aware filtering strips code blocks and skips voice for code-heavy responses.

## Documents

| File | Contents |
|------|----------|
| `tasks.md` | Full task breakdown with dependencies, estimates, and phases |

## Prior Art

| Phase | Document | Status |
|-------|----------|--------|
| Discovery | `specs/10-research/voice-response-tts-brief.md` | Complete |
| Product | — | Skipped (derived from discovery) |
| Architecture | — | Skipped (follows existing VoiceHandler pattern) |

## Gaps

- **No measurement plan (D13)** — No success criteria defined. Recommend adding post-ship: voice message send rate, error rate, user opt-out rate.
- **No product spec** — Requirements derived directly from discovery brief sub-problems.
- **No architecture spec** — Implementation follows existing `VoiceHandler` / `FeatureRegistry` patterns exactly.
