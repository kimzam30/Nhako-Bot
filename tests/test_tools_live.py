"""Tool choice against the real local model, with the bot's real tool schemas.

Covers the step after routing: given the category, does the model pick the right tool
with usable arguments? Skipped when Ollama isn't reachable.  pytest -m live
"""
import json
from pathlib import Path

import pytest

from nhako.agent import Agent
from nhako.config import Settings
from nhako.llm import LLM
from nhako.tools import default_registry

CASES = [c for c in json.loads(
    (Path(__file__).parent.parent / "scripts" / "bench_cases.json").read_text(encoding="utf-8"))
    if c["category"] in ("schedule", "web", "homelab", "postiz", "notion")]
# The benchmark predates the real tools; same intent, final names.
RENAMED = {"postiz_list_posts": "list_posts", "postiz_draft_post": "draft_post",
           "notion_add_task": "add_task", "notion_query_tasks": "list_tasks"}
CASES = [{**c, "tool": RENAMED.get(c["tool"], c["tool"])} for c in CASES]

# Phase 2 additions: edits with vague names, and reminders vs events.
CASES += [
    {"id": "p2-move", "text": "push tomorrow's meeting to 4pm", "tool": "update_event",
     "args": {"event": ["meeting"], "new_when": ["4"]}},
    {"id": "p2-rename", "text": "rename the futsal thing to futsal + makan", "tool": "update_event",
     "args": {"event": ["futsal"], "new_title": ["makan"]}},
    {"id": "p2-remind", "text": "remind me at 6pm to buy milk", "tool": "set_reminder",
     "args": {"what": ["milk"], "when": ["6"]}},
    {"id": "p2-event", "text": "I have a haircut on friday at 11am, put it in", "tool": "create_event",
     "args": {"title": ["haircut"], "when": ["friday"]}},
]
CASES = [{"category": "schedule", **c} if "category" not in c else c for c in CASES]

# Phase 3: reading pages, including a follow-up that only works through the history.
SEARCH_REPLY = ("Paper 26.3 is the latest build.\n\nSources:\n1. PaperMC downloads\nhttps://papermc.io/downloads\n"
                "2. Paper 26.3 changelog\nhttps://forums.papermc.io/threads/paper-26-3.1234/")
CASES += [
    {"id": "p3-read", "category": "web", "text": "what does https://docs.docker.com/compose/how-tos/startup-order/ say "
     "about healthchecks?", "tool": "read_url", "args": {"url": ["docs.docker.com"]}},
    {"id": "p3-follow", "category": "web", "text": "open the second link and tell me what changed",
     "tool": "read_url", "args": {"url": ["forums.papermc.io"]},
     "history": [{"role": "user", "content": "search for the latest paper release"},
                 {"role": "assistant", "content": SEARCH_REPLY}]},
]

# Phase 5: tasks and social posts.
CASES += [
    {"id": "p5-done", "category": "notion", "text": "I finished the passport renewal", "tool": "complete_task",
     "args": {"task": ["passport"]}},
    {"id": "p5-overdue", "category": "notion", "text": "what's overdue on my to-do list?", "tool": "list_tasks",
     "args": {"when": ["overdue"]}},
    {"id": "p5-add-due", "category": "notion", "text": "add buy new SSD to my to-do list, due friday",
     "tool": "add_task", "args": {"title": ["ssd"], "due": ["friday"]}},
    {"id": "p5-linkedin", "category": "postiz", "text": "write a linkedin post about my new homelab dashboard",
     "tool": "draft_post", "args": {"channel": ["linkedin"], "content": ["dashboard"]}},
    {"id": "p5-posts-tmr", "category": "postiz", "text": "anything going out on social media tomorrow?",
     "tool": "list_posts", "args": {"when": ["tomorrow"]}},
    {"id": "pz-post-stats", "category": "postiz", "text": "how did my tiktoks do last week?",
     "tool": "post_stats", "args": {"channel": ["tiktok"]}},
    {"id": "pz-channel-stats", "category": "postiz", "text": "how many followers did I gain on x this month?",
     "tool": "channel_stats", "args": {"channel": ["x"]}},
]

# Hakim's Hub: moving, removing, and project progress.
CASES += [
    {"id": "hub-progress", "category": "notion", "text": "move the landing page task to in progress",
     "tool": "update_task", "args": {"task": ["landing"], "status": ["progress"]}},
    {"id": "hub-remove", "category": "notion", "text": "remove the thumbnail task", "tool": "remove_task",
     "args": {"task": ["thumbnail"]}},
    {"id": "hub-percent", "category": "notion", "text": "set Nhako Beam progress to 50%",
     "tool": "update_project", "args": {"project": ["beam"], "percent": ["50"]}},
    {"id": "hub-projects", "category": "notion", "text": "how far along are my projects?", "tool": "list_projects"},
    {"id": "hub-due", "category": "notion", "text": "push the bug fixes quest to friday", "tool": "update_task",
     "args": {"task": ["bug"], "due": ["friday"]}},
    {"id": "hub-tag", "category": "notion", "text": "make the bug fixes quest a boss quest", "tool": "update_task",
     "args": {"task": ["bug"], "tag|status": ["boss"]}},
    {"id": "hub-pause", "category": "notion", "text": "pause NeraOS", "tool": "update_project",
     "args": {"project": ["nera"], "status": ["pause"]}},
    {"id": "hub-new", "category": "notion", "text": "start a new project called Nhako Radio",
     "tool": "add_project", "args": {"name": ["radio"]}},
    {"id": "hub-xp", "category": "notion", "text": "how much XP did I get this week?", "tool": "quest_stats",
     "args": {"period": ["week"]}},
    {"id": "hub-read", "category": "notion", "text": "what's in my Nhako Depot page?", "tool": "read_notion",
     "args": {"page": ["depot"]}},
]

# Phase 6: memory.
CASES += [
    {"id": "p6-remember", "category": "memory", "text": "remember that I prefer meetings after 10am",
     "tool": "remember", "args": {"fact": ["10"]}},
    {"id": "p6-forget", "category": "memory", "text": "forget what I told you about prawns",
     "tool": "forget", "args": {"fact": ["prawn"]}},
    {"id": "p6-list", "category": "memory", "text": "what do you remember about me?", "tool": "list_memories"},
]

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
async def agent():
    from nhako.agent import make_context
    s = Settings(_env_file=None)
    llm = LLM(s.ollama_host, s.ollama_model, s.ollama_num_ctx)
    if problem := await llm.health():
        pytest.skip(problem)
    ctx = make_context(s)
    return Agent(llm, None, default_registry(ctx), ctx)


@pytest.fixture(scope="module")
async def chosen(agent):
    out = {}
    for c in CASES:
        tool, args, _, _ = await agent.choose_tool(c["category"], c["text"], c.get("history", []))
        out[c["id"]] = (tool.name if tool else None, args)
    return out


def args_ok(expected, actual):
    actual = {k: v.lower() for k, v in actual.items()}
    return all(any(n.lower() in actual.get(k, "") for k in keys.split("|") for n in needles)
               for keys, needles in expected.items())


async def test_tool_choice(chosen):
    wrong = {}
    for c in CASES:
        name, args = chosen[c["id"]]
        if name not in [c["tool"], *c.get("alt_tools", [])] or not args_ok(c.get("args", {}), args):
            wrong[c["id"]] = (c["tool"], name, args)
    accuracy = 1 - len(wrong) / len(CASES)
    assert accuracy >= 0.85, f"tool accuracy {accuracy:.0%}; wrong (expected, got, args): {wrong}"


@pytest.mark.parametrize("case_id", ["cal-del-1", "cal-edit-1", "p2-move", "p3-follow", "p5-done"])
async def test_edits_and_deletes_pick_the_right_tool(chosen, case_id):
    """Deleting instead of moving (or the reverse) is the mistake that matters most."""
    expected = next(c["tool"] for c in CASES if c["id"] == case_id)
    assert chosen[case_id][0] == expected, chosen[case_id]


async def facts_agent(agent, facts):
    from nhako.store import Store
    store = await Store(":memory:").open()
    for f in facts:
        await store.add_fact(1, f)
    return Agent(agent.llm, store, agent.registry, agent.ctx), store


async def test_chat_uses_a_fact(agent):
    with_facts, store = await facts_agent(agent, ["my sister is Aina"])
    reply = await with_facts._chat(1, "what's my sister's name?")
    await store.close()
    assert "Aina" in reply, reply


@pytest.mark.parametrize("text, completes", [
    ("I finished the passport renewal", True),
    ("dah siap renew passport", True),
    ("I finished my workout, any stretching tips?", False),  # must stay a chat
])
async def test_task_completion_end_to_end(agent, text, completes):
    """The whole path (router, rescue, tool choice, fallback) with the real model and a fake
    Notion. The tests above measure the model alone; this is what the user actually gets."""
    import dataclasses

    import httpx

    from nhako.store import Store
    from nhako.tools.notion import Notion

    from .test_notion import DB, FakeNotion
    store = await Store(":memory:").open()
    ctx = dataclasses.replace(agent.ctx, store=store)
    notion = Notion("secret", DB, transport=httpx.MockTransport(FakeNotion()))
    full = Agent(agent.llm, store, default_registry(ctx, notion=notion), ctx)
    reply = await full.handle(1, text)
    await store.close()
    assert bool(reply.confirm_id) == completes, reply.text
    if completes:
        assert "Renew passport" in reply.text
