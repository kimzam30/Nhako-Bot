"""Routing against the real local model, using the Phase 0 benchmark sentences.

Skipped when Ollama isn't reachable. Run on its own with:  pytest -m live
"""
import json
from pathlib import Path

import pytest

from nhako.config import Settings
from nhako.llm import LLM
from nhako.router import classify

CASES = json.loads((Path(__file__).parent.parent / "scripts" / "bench_cases.json").read_text(encoding="utf-8"))

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
async def llm():
    s = Settings(_env_file=None)
    llm = LLM(s.ollama_host, s.ollama_model, s.ollama_num_ctx)
    if problem := await llm.health():
        pytest.skip(problem)
    return llm


@pytest.fixture(scope="module")
async def routed(llm):
    return {c["id"]: await classify(llm, c["text"]) for c in CASES}


async def test_overall_router_accuracy(routed):
    english = [c for c in CASES if c.get("lang", "en") == "en"]
    wrong = {c["id"]: (c["category"], routed[c["id"]]) for c in english if routed[c["id"]] != c["category"]}
    accuracy = 1 - len(wrong) / len(english)
    assert accuracy >= 0.9, f"router accuracy {accuracy:.0%}, wrong (expected, got): {wrong}"


@pytest.mark.parametrize("case_id", ["cal-del-1", "chat-1", "chat-2", "chat-3"])
async def test_safety_critical_routes(routed, case_id):
    """The old keyword router's worst bugs: 'remove a stain' deleting events, 'research' -> search,
    'put in my CV' -> calendar. Each must land in the right place every time."""
    case = next(c for c in CASES if c["id"] == case_id)
    assert routed[case_id] == case["category"], case["text"]


@pytest.mark.parametrize("text, category", [
    ("add buy new SSD to my to-do list by friday", "notion"),   # a task, not a calendar event
    ("remind me on friday to buy an SSD", "schedule"),          # a reminder, not a task
    ("I finished the passport renewal", "notion"),
    ("write a linkedin post about my new homelab dashboard", "postiz"),
    ("anything going out on social media tomorrow?", "postiz"),
    ("is postiz running?", "homelab"),                          # the container, not the posts
    ("remember that my car is a Myvi", "memory"),
    ("remind me to service my car on saturday", "schedule"),    # remind, not remember
    ("forget what I told you about prawns", "memory"),
    ("what do you remember about me?", "memory"),
])
async def test_phase5_routes(llm, text, category):
    assert await classify(llm, text) == category, text
