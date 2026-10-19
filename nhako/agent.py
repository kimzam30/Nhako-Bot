"""One user message in, one reply out: route -> (tool call -> run tool) -> answer."""
import dataclasses
import datetime as dt
import logging
from dataclasses import dataclass

from . import router
from .llm import LLM
from .store import Store
from .tools import Context, NeedsConfirm, Registry, Tool
from .tools.memory import facts_prompt

log = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str
    confirm_id: int | None = None  # set: show Confirm/Cancel buttons for this pending action


class Agent:
    def __init__(self, llm: LLM, store: Store, registry: Registry, ctx: Context, history_turns: int = 6):
        self.llm, self.store, self.registry, self.ctx = llm, store, registry, ctx
        self.history_turns = history_turns

    def _now_text(self) -> str:
        return self.ctx.now().strftime("%A, %d %B %Y, %I:%M %p")

    async def _system(self, chat_id: int | None = None) -> dict:
        """With chat_id: the user's saved facts too. Chat only: in Phase 6 tests even one related fact
        ("prefers meetings after 10am") made the 2B model list events instead of moving one.
        The wording is tuned: moving the clock to the end cost two live routing/tool tests."""
        content = ("You are Nhako, a friendly personal assistant made by Hakim (kimzam). The user is in "
                   f"Malaysia (GMT+8). It is now {self._now_text()}. Reply in the user's language "
                   "(English or Malay), briefly and plainly. Never invent calendar events or facts.")
        if self.store is not None and chat_id is not None:
            if known := facts_prompt(await self.store.facts(chat_id)):
                content += "\n\n" + known
        return {"role": "system", "content": content}

    async def handle(self, chat_id: int, text: str) -> Reply:
        category = await router.classify(self.llm, text, self._now_text())
        if category == "chat":
            category = await self._rescue(text) or category
        log.info("route %r -> %s", text[:60], category)
        if category == "chat":
            reply = Reply(await self._chat(chat_id, text))
        elif category not in self.registry:
            reply = Reply("I can't do that yet.")
        else:
            reply = await self._use_tools(chat_id, category, text)
        await self.store.add_message(chat_id, "user", text)
        await self.store.add_message(chat_id, "assistant", reply.text)
        return reply

    async def _rescue(self, text: str) -> str | None:
        """A category code is sure about, for a message the router called chat."""
        for rescue in self.registry.rescues:
            try:
                if category := await rescue(text, self.ctx):
                    log.info("router said chat for %r; rescued to %s", text[:60], category)
                    return category
            except Exception:
                log.exception("rescue check failed for %r", text[:60])
        return None

    async def _chat(self, chat_id: int, text: str) -> str:
        history = await self.store.history(chat_id, self.history_turns)
        msg = await self.llm.chat([await self._system(chat_id), *history, {"role": "user", "content": text}])
        return msg.content.strip() or "🤔"

    async def choose_tool(self, category: str, text: str, history: list[dict] = ()):
        """Ask the model which of the category's tools to call. Returns (tool or None, args, reply, messages)."""
        messages = [await self._system(), *history, {"role": "user", "content": text}]
        msg = await self.llm.chat(messages, tools=self.registry.schemas(category))
        call = (msg.tool_calls or [None])[0]
        tool = self.registry.get(call.function.name) if call else None
        args = clean_args(tool, call.function.arguments) if tool else {}
        return tool, args, msg, messages

    async def _use_tools(self, chat_id: int, category: str, text: str, rescued: bool = False) -> Reply:
        # Only the last exchange as context: enough for "move it to 5pm", small enough for a 2B model.
        history = await self.store.history(chat_id, 2)
        tool, args, msg, messages = await self.choose_tool(category, text, history)
        if tool is None and category in self.registry.fallbacks:
            if guess := self.registry.fallbacks[category](text):
                name, args = guess
                tool = self.registry.get(name)
                log.info("no tool call for %r; fallback %s(%s)", text[:60], name, args)
        if tool is None and not rescued:
            # Wrong category ("dah siap X" routed to schedule): same second opinion as for chat.
            other = await self._rescue(text)
            if other and other != category and other in self.registry:
                return await self._use_tools(chat_id, other, text, rescued=True)
        if tool is None:
            log.info("no usable tool call for %r; answering as chat", text[:60])
            return Reply(await self._chat(chat_id, text))
        log.info("tool %s(%s)", tool.name, args)
        try:
            result = await tool.run(args, dataclasses.replace(self.ctx, chat_id=chat_id, text=text))
        except Exception:
            log.exception("tool %s failed", tool.name)
            return Reply(f"⚠️ Something went wrong while running {tool.name}. Details are in the log.")

        if isinstance(result, NeedsConfirm):
            action_id = await self.store.save_pending(chat_id, tool.name, result.args)
            return Reply(result.prompt, confirm_id=action_id)
        text = result.text
        if result.summarize:
            final = await self.llm.chat([*messages, msg,
                                         {"role": "tool", "tool_name": tool.name, "content": result.text}])
            text = final.content.strip() or result.text
        return Reply(f"{text}\n\n{result.footer}" if result.footer else text)

    async def confirm(self, chat_id: int, action_id: int, accepted: bool) -> str:
        pending = await self.store.pop_pending(action_id, chat_id)
        if pending is None:
            return "That request has expired. Ask me again."
        if not accepted:
            return "Cancelled. Nothing was changed."
        name, args = pending
        tool = self.registry.get(name)
        if tool is None or tool.execute is None:
            return "I can't do that anymore."
        try:
            result = await tool.execute(args, dataclasses.replace(self.ctx, chat_id=chat_id))
        except Exception:
            log.exception("confirmed %s failed", name)
            return f"⚠️ {name} failed. Details are in the log."
        await self.store.add_message(chat_id, "assistant", result.text)
        return result.text


def clean_args(tool: Tool, raw: dict | None) -> dict:
    """Small models sometimes invent keys or nest values; keep only known string arguments."""
    return {k: str(v) for k, v in (raw or {}).items()
            if k in tool.params and isinstance(v, (str, int, float)) and str(v).strip()}


def make_context(settings, store: Store | None = None) -> Context:
    tz = settings.tz
    return Context(settings=settings, now=lambda: dt.datetime.now(tz), store=store)
