"""Thin async wrapper around Ollama."""
import logging

import ollama

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, host: str, model: str, num_ctx: int = 4096):
        self.client = ollama.AsyncClient(host=host)
        self.model = model
        self.options = {"temperature": 0, "num_ctx": num_ctx}
        # Thinking makes small models write hundreds of tokens before answering
        # (see Phase 0 results). Turned off unless the model rejects the flag.
        self.think: bool | None = False

    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
                   format: dict | None = None) -> ollama.Message:
        try:
            resp = await self.client.chat(self.model, messages, tools=tools, format=format,
                                          options=self.options, think=self.think)
        except ollama.ResponseError as e:
            if self.think is not None and "think" in str(e).lower():
                log.info("%s has no thinking mode; retrying without the think flag", self.model)
                self.think = None
                return await self.chat(messages, tools=tools, format=format)
            raise
        return resp.message

    async def health(self) -> str | None:
        """None when Ollama is reachable and the model is pulled, otherwise what's wrong."""
        try:
            models = [m.model for m in (await self.client.list()).models]
        except Exception as e:  # connection refused, DNS, ...
            return f"can't reach Ollama: {e}"
        if self.model not in models and f"{self.model}:latest" not in models:
            return f"model {self.model} isn't pulled (ollama pull {self.model})"
        return None
