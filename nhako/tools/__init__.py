"""Tool plumbing. Each integration module exposes `tools(ctx) -> list[Tool]`.

A tool either answers straight away (Result) or, for anything that deletes or
changes data, returns NeedsConfirm. The bot then shows Confirm/Cancel buttons and
only calls `tool.execute` once the user presses Confirm.
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import Settings


@dataclass
class Result:
    text: str
    # True: the model rewrites `text` into a natural answer to the user's question.
    # False: `text` is sent to the user as-is.
    summarize: bool = False
    # Appended after the answer, untouched by the model (e.g. source links).
    footer: str = ""


@dataclass
class NeedsConfirm:
    prompt: str  # what will happen, shown above the buttons
    args: dict   # stored, then passed to Tool.execute on Confirm


@dataclass
class Context:
    settings: Settings
    now: Callable[[], dt.datetime]
    store: Any = None             # nhako.store.Store (Any avoids an import cycle)
    chat_id: int | None = None    # set per message by the agent
    text: str = ""                # the user's message, for tools that check the model's arguments


Handler = Callable[[dict, Context], Awaitable[Result | NeedsConfirm]]


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]  # name -> description; every parameter is a string
    run: Handler
    required: tuple[str, ...] = ()
    execute: Callable[[dict, Context], Awaitable[Result]] | None = None  # after Confirm

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {k: {"type": "string", "description": v} for k, v in self.params.items()},
                    "required": list(self.required),
                },
            },
        }


@dataclass
class Registry:
    by_category: dict[str, list[Tool]] = field(default_factory=dict)
    # category -> fn(text) -> (tool name, args): used when the model makes no tool call,
    # for categories where answering from the model alone would mean making things up.
    fallbacks: dict[str, Callable[[str], tuple[str, dict]]] = field(default_factory=dict)

    def add(self, category: str, tools: list[Tool], fallback=None) -> None:
        self.by_category.setdefault(category, []).extend(tools)
        if fallback:
            self.fallbacks[category] = fallback

    def schemas(self, category: str) -> list[dict]:
        return [t.schema() for t in self.by_category.get(category, [])]

    def get(self, name: str) -> Tool | None:
        return next((t for ts in self.by_category.values() for t in ts if t.name == name), None)

    def __contains__(self, category: str) -> bool:
        return category in self.by_category


def default_registry(ctx: Context, cal=None, docker=None, notion=None, postiz=None) -> Registry:
    from . import calendar, homelab, memory, reminders, web
    from . import notion as notion_tools
    from . import postiz as postiz_tools
    reg = Registry()
    reg.add("schedule", calendar.tools(ctx, cal) + reminders.tools(ctx))
    reg.add("web", web.tools(ctx))
    reg.add("homelab", homelab.tools(ctx, docker), fallback=homelab.guess)
    reg.add("notion", notion_tools.tools(ctx, notion))
    reg.add("postiz", postiz_tools.tools(ctx, postiz))
    reg.add("memory", memory.tools(ctx))
    return reg
