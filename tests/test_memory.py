"""Phase 6 memory: the facts table, the remember/forget tools and the facts in the system prompt."""
import dataclasses

import pytest

from nhako.agent import Agent
from nhako.tools import NeedsConfirm, Registry, memory

from .conftest import FakeLLM

CHAT = 42


@pytest.fixture
def mctx(ctx, store):
    return dataclasses.replace(ctx, store=store, chat_id=CHAT)


@pytest.fixture
def tools(mctx):
    return {t.name: t for t in memory.tools(mctx)}


async def test_store_dedupes_and_scopes_by_chat(store):
    assert await store.add_fact(CHAT, "Sister is Aina")
    assert not await store.add_fact(CHAT, "sister is aina")
    await store.add_fact(7, "someone else's fact")
    assert [t for _, t in await store.facts(CHAT)] == ["Sister is Aina"]
    fid = (await store.facts(7))[0][0]
    assert await store.delete_fact(fid, CHAT) is None  # not this chat's
    assert await store.delete_fact(fid, 7) == "someone else's fact"


@pytest.mark.parametrize("raw, saved", [
    ("that I prefer meetings after 10am.", "I prefer meetings after 10am"),
    ("remember that my sister is Aina", "my sister is Aina"),
    ("allergic to prawns", "allergic to prawns"),
])
async def test_remember(tools, mctx, raw, saved):
    out = await tools["remember"].run({"fact": raw}, mctx)
    assert out.text.startswith("🧠 Got it") and saved in out.text
    assert [t for _, t in await mctx.store.facts(CHAT)] == [saved]


async def test_remember_twice(tools, mctx):
    await tools["remember"].run({"fact": "likes teh tarik"}, mctx)
    assert "already know" in (await tools["remember"].run({"fact": "Likes teh tarik"}, mctx)).text


@pytest.mark.parametrize("fact", [
    "my wifi password is hunter2", "bank PIN is 4821", "my IC number is 990101-14-5678",
    "card 4111111111111111", "the API key is abc", "kata laluan email saya ialah x",
    "token fake_key_0123456789abcdef0123456789abcdef",
])
async def test_secrets_are_refused(tools, mctx, fact):
    assert "don't store" in (await tools["remember"].run({"fact": fact}, mctx)).text
    assert await mctx.store.facts(CHAT) == []


async def test_phone_numbers_are_fine(tools, mctx):
    out = await tools["remember"].run({"fact": "mum's number is 0123456789"}, mctx)
    assert out.text.startswith("🧠")


async def test_cap(tools, mctx, monkeypatch):
    monkeypatch.setattr(memory, "MAX_FACTS", 2)
    for f in ("a fact", "b fact"):
        await tools["remember"].run({"fact": f}, mctx)
    assert "/memory" in (await tools["remember"].run({"fact": "c fact"}, mctx)).text


async def test_forget_asks_first(tools, mctx):
    for f in ("I prefer meetings after 10am", "allergic to prawns", "sister is Aina"):
        await mctx.store.add_fact(CHAT, f)
    out = await tools["forget"].run({"fact": "forget the prawns thing"}, mctx)
    assert isinstance(out, NeedsConfirm) and "allergic to prawns" in out.prompt
    assert len(await mctx.store.facts(CHAT)) == 3  # nothing deleted before Confirm
    done = await tools["forget"].execute(out.args, mctx)
    assert "allergic to prawns" in done.text
    assert [t for _, t in await mctx.store.facts(CHAT)] == ["I prefer meetings after 10am", "sister is Aina"]


async def test_forget_unknown(tools, mctx):
    await mctx.store.add_fact(CHAT, "sister is Aina")
    assert "don't remember anything" in (await tools["forget"].run({"fact": "my car"}, mctx)).text


async def test_list(tools, mctx):
    assert "anything saved" in (await tools["list_memories"].run({}, mctx)).text
    await mctx.store.add_fact(CHAT, "sister is Aina")
    assert (await tools["list_memories"].run({}, mctx)).text == "🧠 What I remember (1):\n• sister is Aina"


def test_prompt_budget_keeps_newest():
    facts = [(i, f"fact number {i} " + "x" * 80) for i in range(40)]
    text = memory.facts_prompt(facts)
    assert "fact number 39" in text and "fact number 0 " not in text
    assert len(text) < memory.PROMPT_BUDGET + 200
    assert memory.facts_prompt([]) == ""


async def test_facts_reach_the_model(store, ctx):
    await store.add_fact(CHAT, "sister is Aina")
    llm = FakeLLM("chat", text="Aina!")
    await Agent(llm, store, Registry(), ctx).handle(CHAT, "what's my sister's name?")
    system = llm.calls[-1]["messages"][0]["content"]
    assert "sister is Aina" in system
    # ...but not another chat's facts, and not the router call
    await Agent(llm, store, Registry(), ctx).handle(7, "hi")
    assert "Aina" not in llm.calls[-1]["messages"][0]["content"]
    assert "Aina" not in llm.calls[-2]["messages"][0]["content"]


async def test_tool_calls_get_no_facts(store, ctx):
    """Facts distract a 2B model from choosing the right action (Phase 6 live tests)."""
    from nhako.tools import Result, Tool
    await store.add_fact(CHAT, "sister is Aina")

    async def run(args, ctx):
        return Result("ok")
    reg = Registry()
    reg.add("schedule", [Tool("create_event", "c", {"title": "t"}, run)])
    llm = FakeLLM("schedule", tool="create_event", args={"title": "lunch"})
    await Agent(llm, store, reg, ctx).handle(CHAT, "lunch with my sister on thursday")
    assert "Aina" not in llm.calls[1]["messages"][0]["content"]


@pytest.mark.parametrize("raw, saved", [
    ("tolong ingat yang saya alah udang", "saya alah udang"),
    ("my mum's birthday is 3 March, remember it", "my mum's birthday is 3 March"),
    ("don't forget that I take the LRT to work", "I take the LRT to work"),
])
def test_tidy(raw, saved):
    assert memory.tidy(raw) == saved


async def test_model_fact_unrelated_to_message_uses_the_message(tools, mctx):
    said = dataclasses.replace(mctx, text="ingat yang saya alah udang")
    out = await tools["remember"].run({"fact": "sister is Aina"}, said)  # copied from history
    assert "saya alah udang" in out.text
    assert [t for _, t in await mctx.store.facts(CHAT)] == ["saya alah udang"]


async def test_model_fact_rephrased_is_kept(tools, mctx):
    said = dataclasses.replace(mctx, text="remember that I like my meetings after 10am please")
    await tools["remember"].run({"fact": "prefers meetings after 10am"}, said)
    assert [t for _, t in await mctx.store.facts(CHAT)] == ["prefers meetings after 10am"]


async def test_forget_uses_the_message_when_the_model_drifts(tools, mctx):
    await mctx.store.add_fact(CHAT, "prefers meetings after 10am")
    await mctx.store.add_fact(CHAT, "allergic to prawns")
    said = dataclasses.replace(mctx, text="forget the prawns thing")
    out = await tools["forget"].run({"fact": "meetings"}, said)
    assert "allergic to prawns" in out.prompt and "meetings" not in out.prompt


@pytest.mark.parametrize("model, said, saved", [
    ("prefers meetings after 10am, allergy to prawns", "remember that I am allergic to prawns", "I am allergic to prawns"),
    ("allergy to prawns", "remember that I am allergic to prawns", "allergy to prawns"),
    ("adik nama Aina", "ingat yang adik saya nama Aina", "adik nama Aina"),
])
async def test_fact_must_mostly_come_from_the_message(tools, mctx, model, said, saved):
    await tools["remember"].run({"fact": model}, dataclasses.replace(mctx, text=said))
    assert [t for _, t in await mctx.store.facts(CHAT)] == [saved]


async def test_pin_without_a_number_is_not_a_secret(tools, mctx):
    out = await tools["remember"].run({"fact": "I collect pin badges"}, mctx)
    assert "remember" in out.text and await mctx.store.facts(CHAT) != []
