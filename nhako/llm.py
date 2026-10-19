"""Thin async wrapper around Ollama."""
import asyncio
import logging

import ollama

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, host: str, model: str, num_ctx: int = 4096, *, timeout: float = 120,
                 keep_alive: str | None = None):
        # Without a timeout a hung Ollama would leave the handler (and "typing...") waiting forever.
        self.client = ollama.AsyncClient(host=host, timeout=timeout)
        self.model = model
        self.options = {"temperature": 0, "num_ctx": num_ctx}
        self.keep_alive = keep_alive or None
        # Thinking makes small models write hundreds of tokens before answering
        # (see Phase 0 results). Turned off unless the model rejects the flag.
        self.think: bool | None = False
        # One GPU: with concurrent updates, model calls still run one at a time.
        self._lock = asyncio.Lock()

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
                   format: dict | None = None) -> ollama.Message:
        try:
            async with self._lock:
                resp = await self.client.chat(self.model, messages, tools=tools, format=format,
                                              options=self.options, think=self.think, keep_alive=self.keep_alive)
        except ollama.ResponseError as e:
            if self.think is not None and "think" in str(e).lower():
                log.info("%s has no thinking mode; retrying without the think flag", self.model)
                self.think = None
                return await self.chat(messages, tools=tools, format=format)
            raise
        return resp.message

    async def warm(self) -> None:
        """Load the model into VRAM ahead of time, so the next message skips the 10-60 s cold load."""
        try:
            async with self._lock:
                # Same options as chat: a different num_ctx would make Ollama reload the model.
                await self.client.generate(self.model, "", options=self.options, keep_alive=self.keep_alive)
        except Exception as e:
            log.warning("warming %s failed: %r", self.model, e)

    async def health(self) -> str | None:
        """None when Ollama is reachable and the model is pulled, otherwise what's wrong."""
        try:
            models = [m.model for m in (await self.client.list()).models]
        except Exception as e:  # connection refused, DNS, timeout...
            return f"can't reach Ollama: {e!r}"
        if self.model not in models and f"{self.model}:latest" not in models:
            return f"model {self.model} isn't pulled (ollama pull {self.model})"
        return None
