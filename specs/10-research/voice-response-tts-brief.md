# Discovery Brief: Voice Message Responses (TTS)

**Date:** 2026-03-14
**Author:** @vazyzy
**Confidence:** High
**Depth:** Thorough

## Research Overview

**Scope:** How can the claude-code-telegram bot send Claude's text responses back to users as voice messages (text-to-speech)?

**Sources consulted:**
- Internal: `src/bot/features/voice_handler.py` (existing STT pipeline)
- Internal: `src/bot/orchestrator.py` (response delivery architecture)
- Internal: `src/config/settings.py` (feature flags and config patterns)
- Internal: `pyproject.toml` (dependency stack)
- External: [Telegram Bot API — sendVoice](https://core.telegram.org/bots/api#sendvoice)
- External: [edge-tts on PyPI](https://pypi.org/project/edge-tts/)
- External: [OpenAI TTS docs](https://platform.openai.com/docs/guides/text-to-speech)
- External: [python-telegram-bot Voice docs](https://docs.python-telegram-bot.org/en/stable/telegram.voice.html)

## Sub-Problems

### 1. TTS Engine Selection

**What's happening:** The bot has no text-to-speech engine. It needs one to convert Claude's text output into spoken audio.

**Evidence:** `pyproject.toml` contains no TTS dependency. `voice_handler.py` only does speech-to-text (Mistral Voxtral / OpenAI Whisper). The return path from text→audio is entirely missing.

**Who it affects:** All users who want audio responses — especially mobile users sending voice messages.

**Why it matters:** This is the core missing capability. Without it, voice interaction is one-directional.

**Options (ranked):**

| Engine | Cost | Quality | Opus Native? | Async? | API Key? |
|--------|------|---------|-------------|--------|----------|
| **edge-tts** | Free | Neural (excellent) | Yes (`ogg-24khz-16bit-mono-opus`) | Yes | No |
| **OpenAI TTS** (`gpt-4o-mini-tts`) | $15/1M chars | Excellent + steerable | Yes (`opus` format) | Yes | Already in config |
| **gTTS** | Free | Decent | No (MP3 only) | No | No |
| **Coqui TTS** | Free | High | No (WAV) | No | No (but needs GPU) |
| **Amazon Polly** | $4.80-19.20/1M chars | Very good | No | Yes | New key needed |

### 2. Audio Format Pipeline

**What's happening:** Telegram requires OGG Opus for `sendVoice`. Wrong format = displayed as file attachment, not playable voice bubble.

**Evidence:** Telegram Bot API docs specify `audio/ogg` with Opus codec for voice messages. Max 50MB (≈138 minutes at 48kbps — not a practical concern).

**Who it affects:** Technical implementation correctness.

**Why it matters:** Both top candidates (edge-tts and OpenAI TTS) support native Opus output, potentially eliminating the need for ffmpeg as a system dependency.

**Pipeline (happy path — no conversion needed):**
```
Claude text → edge-tts (format: ogg-24khz-16bit-mono-opus) → bot.send_voice()
```

**Fallback pipeline (if MP3 output):**
```
Claude text → TTS → MP3 → ffmpeg/pydub/voicegram → OGG Opus → bot.send_voice()
```

### 3. Response Length & Content Filtering

**What's happening:** Claude responses vary wildly — from 1 sentence to multi-page with code blocks, file paths, terminal output. Voice is harmful for structured/code content.

**Evidence:** The bot is a *coding assistant*. Majority of responses contain code, which is nonsensical as audio.

**Who it affects:** UX quality for all users.

**Why it matters:** Naively voicing everything produces unusable audio. Need content-aware routing.

**Approaches:**
- Strip code blocks / structured data, voice only natural language paragraphs
- For responses that are mostly code, send text only (skip voice)
- Chunk long text at sentence boundaries (~4000 chars per chunk) for sequential voice messages
- For very long audio (>5 min), consider `sendAudio` (MP3, music-player UI) instead of `sendVoice`

### 4. UX Trigger — When to Send Voice?

**What's happening:** Need to decide when responses should be voice vs text.

**Who it affects:** All users — this is the primary UX decision.

**Why it matters:** Wrong default = annoying. Right default = delightful.

**Options:**

| Trigger | Pros | Cons |
|---------|------|------|
| **Voice-in → Voice-out** (symmetry) | Natural, zero config | Can't opt-in for text queries |
| **`/voice` toggle command** | User control | Extra step, easy to forget |
| **Inline "🔊 Listen" button** | On-demand, non-intrusive | Extra tap, generates audio on-demand (latency) |
| **Always voice** | Simple | Terrible for code responses |
| **Per-user setting** | Persistent preference | Needs storage (already have SQLite) |

### 5. Architecture Integration

**What's happening:** The bot uses `DraftStreamer` for real-time text streaming via `edit_text()`. Voice requires a complete audio file — can't stream incrementally.

**Evidence:** `orchestrator.py` streams partial text to the user during Claude processing. Voice messages need the full response first.

**Who it affects:** Perceived latency.

**Why it matters:** Users will wait longer for voice responses than text. Need to manage expectations.

**Approach:** Send text response first (streamed as usual), then follow up with voice message. Or show a "🎙 Generating voice..." indicator while TTS runs.

## Principles

Constraints any good solution must respect:

- **Voice-in → Voice-out symmetry** — When users send a voice message, they're on mobile with hands busy. Responding with a text wall defeats the purpose. Voice responses should at minimum trigger when the user sent voice.
- **Content-aware routing** — Code blocks, file listings, terminal output, and structured data must remain as text. Only natural language should become voice. A response that's 80% code should skip voice entirely.
- **Free-first, paid-fallback** — edge-tts (free, neural quality, no API key) before OpenAI TTS ($15/1M chars). The bot already has OpenAI API key in config as a natural fallback.
- **No new system dependencies if avoidable** — edge-tts native Opus output avoids adding ffmpeg as a required system dependency for the conversion step.
- **Opt-in, not forced** — Voice responses should be a feature flag (`ENABLE_VOICE_RESPONSES`), defaulting to a sensible trigger (voice symmetry), not changing existing behavior.

## User Job Hypothesis

When users **send a voice message to the bot from their phone**, they want to **hear the response spoken back**, so they can **continue their task hands-free without reading a screen**.

**Alternative framings considered:**
- "Users want all responses as audio" — rejected; code responses make this impractical
- "Users want a podcast-like experience" — too ambitious; this is a coding bot, not a content platform

## Evidence Map

### Evidence For
- Voice-in already works (Mistral/Whisper transcription) — *internal codebase*
- `sendVoice` API is well-documented and simple — *Telegram Bot API docs*
- edge-tts provides free neural TTS with native Opus — *PyPI, GitHub*
- python-telegram-bot supports `send_voice()` natively — *PTB docs*
- Bot already has async architecture (fits edge-tts async API) — *internal codebase*
- OpenAI API key already configured as fallback — *settings.py*

### Evidence Against
- ~90% of bot responses contain code — voice is harmful for this content — *domain knowledge*
- Voice adds latency (TTS generation) on top of Claude response time — *architectural constraint*
- edge-tts depends on Microsoft's service availability (no SLA) — *edge-tts GitHub issues*
- Additional dependency increases maintenance surface — *general principle*

### Confidence Assessment
**High** — All technical components are well-documented and proven. The bot architecture has clear integration points. The only medium-confidence area is UX trigger design (needs real user feedback).

## Competitive Landscape

**How others solve this:**
- **ChatGPT mobile app** — Built-in voice mode with real-time conversation (much more ambitious)
- **Telegram voice bots (generic)** — Typically use gTTS + ffmpeg pipeline; low quality
- **ElevenLabs Telegram bots** — Premium quality but expensive ($180/1M chars)

**Current user workarounds:**
- Read text responses on screen (defeats purpose of sending voice)
- Copy response text into a separate TTS app

**Gap/Opportunity:**
- Neural-quality voice responses for free (via edge-tts) is a differentiator vs. most Telegram bots
- Voice symmetry (voice-in → voice-out) is natural but rarely implemented

## Key Insights

1. **The pipeline is 80% built.** Incoming voice works. `send_voice()` is one API call. The only missing piece is the TTS engine in the middle.
2. **edge-tts is the clear winner.** Free, neural quality, async, native Opus output — checks every box. OpenAI TTS is a solid paid fallback.
3. **Content filtering is the hard UX problem**, not the technical implementation. Deciding *what* to voice matters more than *how* to voice it.

## Open Questions

- Should voice response also include the text version (send both)?
- What voice/language should be default? (edge-tts has 300+ voices)
- Should voice preference be per-user (stored in SQLite) or global config?
- How to handle multilingual users? (auto-detect language from response text?)
- Should the "🔊 Listen" button approach be combined with voice symmetry?

## Project Context

**Files consulted:**
- `src/bot/features/voice_handler.py` — Existing STT pipeline (Mistral/OpenAI)
- `src/bot/orchestrator.py` — Message routing, DraftStreamer, agentic_voice handler
- `src/config/settings.py` — Feature flags pattern (`ENABLE_VOICE_MESSAGES`, `VOICE_PROVIDER`)
- `pyproject.toml` — Dependencies, voice extras group

**Existing patterns to follow:**
- Feature flag: `ENABLE_VOICE_RESPONSES` (mirrors `ENABLE_VOICE_MESSAGES`)
- Provider config: `TTS_PROVIDER` with `edge-tts` | `openai` (mirrors `VOICE_PROVIDER`)
- Handler: extend `agentic_voice` in orchestrator to send voice response after text

## Recommendation

**Ready for hypotheses** — Problem validated, sub-problems concrete, principles clear.

The simplest high-value implementation:
1. Add `edge-tts` dependency
2. Create `TTSHandler` (mirrors `VoiceHandler` pattern)
3. In `agentic_voice` handler: after sending text response, generate voice and `send_voice()`
4. Strip code blocks before TTS, skip voice if response is mostly code
5. Feature flag `ENABLE_VOICE_RESPONSES` defaults to `true`

Estimated effort: ~2-3 hours for MVP (voice symmetry only).

---

*Next: Run `/hypotheses` to generate testable hypotheses from these findings.*
