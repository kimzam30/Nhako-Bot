"""Agent flow with a fake model: routing, tool dispatch, confirm buttons, fallbacks."""
from nhako.agent import clean_args
from nhako.bot import split_message, to_html
from nhako.router import CATEGORIES, parse_category, router_prompt

from .conftest import FakeLLM

CHAT = 42


async def test_chat_goes_straight_to_the_model(make_agent, registry):
    agent = make_agent(FakeLLM("chat", text="hello there"))
    reply = await agent.handle(CHAT, "hi")
    assert reply.text == "hello there" and reply.confirm_id is None
    assert registry.ran == []


async def test_chat_reply_and_question_are_saved(make_agent, store):
    agent = make_agent(FakeLLM("chat", text="hello there"))
    await agent.handle(CHAT, "hi")
    assert await store.history(CHAT, 10) == [{"role": "user", "content": "hi"},
                                             {"role": "assistant", "content": "hello there"}]


async def test_tool_category_only_offers_its_own_tools(make_agent):
    llm = FakeLLM("schedule", tool="create_event", args={"title": "dentist", "when": "tue 3pm"})
    await make_agent(llm).handle(CHAT, "add dentist tue 3pm")
    offered = [t["function"]["name"] for t in llm.calls[1]["tools"]]
    assert offered == ["list_events", "create_event", "delete_event"]


async def test_plain_tool_result_is_sent_as_is(make_agent, registry):
    llm = FakeLLM("schedule", tool="create_event", args={"title": "dentist", "when": "tue 3pm"})
    reply = await make_agent(llm).handle(CHAT, "add dentist tue 3pm")
    assert reply.text == "added dentist"
    assert registry.ran == [("add", {"title": "dentist", "when": "tue 3pm"})]
    assert len(llm.calls) == 2  # router + tool call, no summary call


async def test_summarized_result_goes_back_to_the_model(make_agent):
    llm = FakeLLM("schedule", tool="list_events", args={"range": "today"}, text="You have 3 things today.")
    reply = await make_agent(llm).handle(CHAT, "what do I have today")
    assert reply.text == "You have 3 things today."
    tool_msg = llm.calls[2]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"] == "raw data: 3 events"


async def test_delete_waits_for_confirmation(make_agent, registry):
    llm = FakeLLM("schedule", tool="delete_event", args={"event": "dentist"})
    agent = make_agent(llm)
    reply = await agent.handle(CHAT, "remove the dentist thing")
    assert reply.confirm_id is not None and "Delete dentist?" in reply.text
    assert registry.ran == [("remove-prepare", {"event": "dentist"})]  # nothing deleted yet

    assert await agent.confirm(CHAT, reply.confirm_id, accepted=True) == "deleted"
    assert registry.ran[-1] == ("remove-execute", {"ids": ["abc"]})


async def test_cancel_runs_nothing(make_agent, registry):
    agent = make_agent(FakeLLM("schedule", tool="delete_event", args={"event": "dentist"}))
    reply = await agent.handle(CHAT, "remove the dentist thing")
    assert "Cancelled" in await agent.confirm(CHAT, reply.confirm_id, accepted=False)
    assert ("remove-execute", {"ids": ["abc"]}) not in registry.ran


async def test_confirm_button_works_once_and_only_in_its_chat(make_agent, registry):
    agent = make_agent(FakeLLM("schedule", tool="delete_event", args={"event": "dentist"}))
    reply = await agent.handle(CHAT, "remove the dentist thing")
    assert "expired" in await agent.confirm(CHAT + 1, reply.confirm_id, accepted=True)
    assert "expired" in await agent.confirm(CHAT, reply.confirm_id, accepted=True)  # popped above
    assert not any(name == "remove-execute" for name, _ in registry.ran)


async def test_no_tool_call_falls_back_to_chat(make_agent, registry):
    reply = await make_agent(FakeLLM("schedule", tool=None, text="Sure, tell me more")).handle(CHAT, "hmm")
    assert reply.text == "Sure, tell me more" and registry.ran == []


async def test_unknown_tool_name_falls_back_to_chat(make_agent, registry):
    reply = await make_agent(FakeLLM("schedule", tool="format_disk", text="ok")).handle(CHAT, "x")
    assert reply.text == "ok" and registry.ran == []


async def test_category_without_tools_says_so(make_agent):
    reply = await make_agent(FakeLLM("notion")).handle(CHAT, "add to my notion tasks")
    assert reply.text == "I can't do that yet."


def test_clean_args_drops_unknown_and_nested_values(registry):
    tool = registry.get("create_event")
    # What qwen3:1.7b actually produced in Phase 0
    assert clean_args(tool, {"title when": {"title": "x"}, "title": "dentist", "when": " ", "extra": 1}) \
        == {"title": "dentist"}


def test_router_falls_back_to_chat_on_bad_output():
    assert parse_category('{"category": "schedule"}') == "schedule"
    for bad in [None, "", "not json", '{"category": "rm -rf"}', "[1, 2]"]:
        assert parse_category(bad) == "chat"


def test_router_prompt_lists_every_category():
    prompt = router_prompt("Monday")
    assert all(f"- {c}:" in prompt for c in CATEGORIES)


def test_to_html_escapes_then_formats():
    assert to_html("**PaperMC 26.3** <b>not a tag</b> & `ollama ps`") == \
        "<b>PaperMC 26.3</b> &lt;b&gt;not a tag&lt;/b&gt; &amp; <code>ollama ps</code>"


def test_to_html_bullets_and_headings():
    assert to_html("## Summary\n*   **Health:** use `depends_on`\n- second\n  * nested") == \
        "<b>Summary</b>\n• <b>Health:</b> use <code>depends_on</code>\n• second\n  • nested"


def test_split_message():
    assert split_message("short") == ["short"]
    long = ("para " * 300 + "\n\n") * 5
    parts = split_message(long, limit=2000)
    assert all(len(p) <= 2000 for p in parts)
    assert "".join(parts).replace(" ", "").replace("\n", "") == long.replace(" ", "").replace("\n", "")
    assert split_message("x" * 5000, limit=4096) == ["x" * 4096, "x" * 904]


def test_to_html_links():
    assert to_html("see [the docs](https://example.com/a?b=1&c=2)") == \
        'see <a href="https://example.com/a?b=1&amp;c=2">the docs</a>'
    assert to_html('[x](javascript:alert(1))') == "[x](javascript:alert(1))"  # only http(s)
