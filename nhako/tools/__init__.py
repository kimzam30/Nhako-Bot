"""Tool plumbing. Each integration module exposes `tools(ctx) -> list[Tool]`.

A tool either answers straight away (Result) or, for anything that deletes or
changes data, returns NeedsConfirm. The bot then shows Confirm/Cancel buttons and
only calls `tool.execute` once the user presses Confirm.
"""
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

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
    # None from a fallback: no safe guess, answer as chat.
    fallbacks: dict[str, Callable[[str], tuple[str, dict] | None]] = field(default_factory=dict)
    # fn(text, ctx) -> category or None: a second opinion when the router says "chat", for
    # messages code can recognise with certainty (e.g. "I finished X" where X is an open task).
    rescues: list[Callable[[str, Context], Awaitable[str | None]]] = field(default_factory=list)
    # fn(text, ctx) -> category or None, asked BEFORE the router: for messages code can place with
    # certainty that the small router gets wrong ("pause NeraOS" -> homelab, "how much XP" -> chat).
    claims: list[Callable[[str, Context], Awaitable[str | None]]] = field(default_factory=list)

    def add(self, category: str, tools: list[Tool], fallback=None, rescue=None, claim=None) -> None:
        self.by_category.setdefault(category, []).extend(tools)
        if fallback:
            self.fallbacks[category] = fallback
        if rescue:
            self.rescues.append(rescue)
        if claim:
            self.claims.append(claim)

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
    if docker is None and ctx.settings.docker_proxy_url:
        docker = homelab.Docker(ctx.settings.docker_proxy_url)
    # Before Notion's claim: "pause nextcloud" is a container, "pause NeraOS" (no such container) a project.
    reg.add("homelab", homelab.tools(ctx, docker), fallback=homelab.guess, claim=homelab.claim(docker))
    # One category for tasks and projects: a separate "projects" category made the 2B router send
    # task messages to schedule/memory/homelab (live tests, Oct 2026).
    reg.add("notion", notion_tools.tools(ctx, notion) + notion_tools.project_tools(ctx, notion),
            fallback=notion_tools.guess, rescue=notion_tools.rescue(notion), claim=notion_tools.claim(notion))
    reg.add("postiz", postiz_tools.tools(ctx, postiz))
    reg.add("memory", memory.tools(ctx))
    return reg
