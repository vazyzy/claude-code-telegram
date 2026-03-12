"""
Handle voice message uploads — transcribe via OpenAI Whisper API.

Flow: Telegram voice (OGG/Opus) -> download -> Whisper API -> text transcript
"""

import io
from dataclasses import dataclass
from typing import Optional

import structlog
from telegram import Voice

logger = structlog.get_logger()


@dataclass
class TranscribedVoice:
    """Result of voice transcription."""

    text: str
    duration_seconds: int
    file_size: int


class VoiceHandler:
    """Transcribe Telegram voice messages using OpenAI Whisper API."""

    def __init__(self, openai_api_key: str):
        self._api_key = openai_api_key

    async def transcribe(
        self, voice: Voice, caption: Optional[str] = None
    ) -> TranscribedVoice:
        """Download and transcribe a Telegram voice message."""
        import openai

        file = await voice.get_file()
        voice_bytes = await file.download_as_bytearray()

        logger.info(
            "Transcribing voice message",
            duration=voice.duration,
            file_size=len(voice_bytes),
        )

        client = openai.AsyncOpenAI(api_key=self._api_key)

        # Whisper expects a file-like object with a name ending in a supported extension
        audio_file = io.BytesIO(voice_bytes)
        audio_file.name = "voice.ogg"

        transcription = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )

        text = transcription.text.strip()

        if caption:
            text = f"{caption}\n\n{text}"

        logger.info(
            "Voice transcription complete",
            duration=voice.duration,
            text_length=len(text),
        )

        return TranscribedVoice(
            text=text,
            duration_seconds=voice.duration if isinstance(voice.duration, int) else 0,
            file_size=len(voice_bytes),
        )
