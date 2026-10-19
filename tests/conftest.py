import datetime as dt
import json
from zoneinfo import ZoneInfo

import ollama
import pytest

from nhako.agent import Agent
from nhako.config import Settings
from nhako.store import Store
from nhako.tools import Context, NeedsConfirm, Registry, Result, Tool

KL = ZoneInfo("Asia/Kuala_Lumpur")
# A Monday morning, so weekday maths is easy to check by hand.
NOW = dt.datetime(2026, 10, 19, 7, 30, tzinfo=KL)


class FakeLLM:
    """Answers like Ollama would: router call -> category JSON, tools call -> tool_calls, else text."""

    def __init__(self, category="chat", tool=None, args=None, text="fake reply"):
        self.category, self.tool, self.args, self.text = category, tool, args or {}, text
        self.calls = []

    async def chat(self, messages, *, tools=None, format=None):
        self.calls.append({"messages": messages, "tools": tools, "format": format})
        if format:
            return ollama.Message(role="assistant", content=json.dumps({"category": self.category}))
        if tools and self.tool:
            return ollama.Message.model_validate({"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": self.tool, "arguments": self.args}}]})
        return ollama.Message(role="assistant", content=self.text)


@pytest.fixture
def ctx():
    return Context(settings=Settings(_env_file=None), now=lambda: NOW)


@pytest.fixture
async def store():
    s = await Store(":memory:").open()
    yield s
    await s.close()


@pytest.fixture
def registry():
    """Stand-in tools that record what ran, so no Google or web access is needed."""
    ran = []

    async def lookup(args, ctx):
        ran.append(("lookup", args))
        return Result("raw data: 3 events", summarize=True)

    async def add(args, ctx):
        ran.append(("add", args))
        return Result(f"added {args.get('title')}")

    async def remove(args, ctx):
        ran.append(("remove-prepare", args))
        return NeedsConfirm("Delete dentist?", {"ids": ["abc"]})

    async def remove_execute(args, ctx):
        ran.append(("remove-execute", args))
        return Result("deleted")

    reg = Registry()
    reg.add("schedule", [
        Tool("list_events", "list", {"range": "r"}, lookup),
        Tool("create_event", "create", {"title": "t", "when": "w"}, add),
        Tool("delete_event", "delete", {"event": "e"}, remove, execute=remove_execute),
    ])
    reg.ran = ran
    return reg


@pytest.fixture
def make_agent(store, registry, ctx):
    def make(llm):
        return Agent(llm, store, registry, ctx)
    return make
