"""Voice notes. fixtures/voice_en.ogg is a Telegram-style OGG/Opus clip (48 kHz mono) saying
"Remind me tomorrow at 3pm to pay the electricity bill." (Windows TTS)."""
import io
from pathlib import Path

import pytest
from faster_whisper.audio import decode_audio

from nhako.speech import Transcriber

CLIP = (Path(__file__).parent / "fixtures" / "voice_en.ogg").read_bytes()


def test_telegram_voice_note_decodes():
    """The step that broke in the container (PyAV 19 vs faster-whisper 1.2.1). No model needed."""
    samples = decode_audio(io.BytesIO(CLIP), sampling_rate=16000)
    assert 3.0 < len(samples) / 16000 < 6.0


def test_language_is_english_unless_auto():
    assert Transcriber("base").language == "en"
    assert Transcriber("base", language=" MS ").language == "ms"
    assert Transcriber("base", language="auto").language is None


@pytest.mark.live
async def test_transcribe_voice_note():
    """Downloads whisper base (~145 MB) on first run. Not data/whisper: that cache belongs to
    the container and holds Linux symlinks Windows can't open."""
    text = await Transcriber("base", str(Path(".pytest_cache") / "whisper")).transcribe(io.BytesIO(CLIP))
    assert "electricity bill" in text.lower() and "3" in text
