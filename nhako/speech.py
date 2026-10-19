"""Voice notes -> text with faster-whisper on CPU (the GPU belongs to the LLM)."""
import asyncio
import logging
import threading

log = logging.getLogger(__name__)


class Transcriber:
    def __init__(self, model_size: str, download_root: str | None = None):
        self.model_size = model_size
        self.download_root = download_root
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        # Loaded on the first voice note, not at startup: most messages are text.
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel
                log.info("loading whisper %s", self.model_size)
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8",
                                           download_root=self.download_root)
        return self._model

    def _transcribe(self, audio) -> str:
        segments, info = self._load().transcribe(audio, beam_size=1, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        log.info("transcribed %.1fs of %s audio", info.duration, info.language)
        return text

    async def transcribe(self, audio) -> str:
        """`audio`: a file path or a file-like object with the voice note."""
        return await asyncio.to_thread(self._transcribe, audio)
