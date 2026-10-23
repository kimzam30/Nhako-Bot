"""Notion tasks against a fake API shaped like the real "My Tasks" template database."""
import dataclasses
import json

import httpx
import pytest

from nhako.tools import NeedsConfirm, notion
from nhako.tools.notion import Notion, TaskSchema

from .conftest import NOW

DB = "f5bdd02c50314287af363ef22f183d90"
SOURCE = "ds-1"
OWNER = "user-hakim"

# Properties as GET /data_sources/{id} returns them for the template.
PROPS = {
    "Task name": {"type": "title", "title": {}},
    "Due": {"type": "date", "date": {}},
    "Assignee": {"type": "people", "people": {}},
    "Source": {"type": "select", "select": {"options": []}},
    "Status": {"type": "status", "status": {
        "options": [{"id": "a", "name": "Not started"}, {"id": "b", "name": "In progress"},
                    {"id": "c", "name": "Done"}],
        "groups": [{"name": "To-do", "option_ids": ["a"]}, {"name": "In progress", "option_ids": ["b"]},
                   {"name": "Complete", "option_ids": ["c"]}]}},
}


def page(pid, title, due=None, status="Not started"):
    return {"id": pid, "url": f"https://www.notion.so/{pid}", "properties": {
        "Task name": {"title": [{"plain_text": title}]},
        "Due": {"date": {"start": due} if due else None},
        "Status": {"status": {"name": status}},
    }}


PAGES = [
    page("p1", "Pay internet bill", "2026-10-17"),                 # overdue (Sat)
    page("p2", "Renew passport", "2026-10-19"),                    # today
    page("p3", "Send invoice to client", "2026-10-21T15:00:00.000+08:00", "In progress"),
    page("p4", "Send invoice reminder", "2026-11-02"),
    page("p5", "Clean the NAS fans"),                               # no due date
]


class FakeNotion:
    def __init__(self, pages=PAGES, status=200):
        self.pages, self.status, self.requests = pages, status, []

    def __call__(self, req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, body))
        assert req.headers["Notion-Version"] == notion.VERSION
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "nope"})
        path = req.url.path
        if path == f"/v1/databases/{DB}":
            return httpx.Response(200, json={"data_sources": [{"id": SOURCE}]})
        if path == f"/v1/data_sources/{SOURCE}":
            return httpx.Response(200, json={"properties": PROPS})
        if path == f"/v1/data_sources/{SOURCE}/query":
            return httpx.Response(200, json={"results": self.pages})
        if path == "/v1/users":
            return httpx.Response(200, json={"results": [{"id": OWNER, "type": "person"},
                                                         {"id": "bot-1", "type": "bot"}]})
        if path == "/v1/pages" and req.method == "POST":
            return httpx.Response(200, json={"id": "new", "url": "https://www.notion.so/new"})
        if path.startswith("/v1/pages/") and req.method == "PATCH":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        return httpx.Response(404)


@pytest.fixture
def api():
    return FakeNotion()


@pytest.fixture
def tools(ctx, api):
    client = Notion("secret", f"https://app.notion.com/p/{DB}?pvs=204", transport=httpx.MockTransport(api))
    return {t.name: t for t in notion.tools(ctx, client)}


def test_database_id_from_url_or_id():
    assert notion.database_id(f"https://app.notion.com/p/My-Tasks-{DB}?v=1") == DB
    assert notion.database_id("f5bdd02c-5031-4287-af36-3ef22f183d90") == DB
    assert notion.database_id("nonsense") == ""


def test_schema_from_template():
    s = TaskSchema.from_properties(PROPS)
    assert (s.title, s.due, s.status.name, s.status.kind, s.assignee) == \
        ("Task name", "Due", "Status", "status", "Assignee")
    assert s.status.done == ["Done"] and s.status.start == "Not started" and s.status.progress == "In progress"
    assert s.open_filter() == {"property": "Status", "status": {"does_not_equal": "Done"}}
    assert s.project is None and s.note is None  # "Source" is a select, not a relation


def test_schema_with_checkbox():
    s = TaskSchema.from_properties({"Name": {"type": "title"}, "Done": {"type": "checkbox"},
                                    "Deadline": {"type": "date"}})
    assert s.open_filter() == {"property": "Done", "checkbox": {"equals": False}}
    assert s.done_value() == {"Done": {"checkbox": True}}
    assert s.due == "Deadline"


async def test_list_all_open(tools, ctx, api):
    text = (await tools["list_tasks"].run({}, ctx)).text
    lines = text.splitlines()
    assert lines[0] == "📝 5 tasks open:"
    assert "• Pay internet bill  (⚠️ overdue, due Sat 17 Oct)" in lines
    assert "• Send invoice to client  (due Wed 21 Oct 15:00)  · In progress" in lines
    assert "• Clean the NAS fans" in lines
    method, path, body = api.requests[-1]
    assert body["filter"] == {"property": "Status", "status": {"does_not_equal": "Done"}}
    assert body["sorts"] == [{"property": "Due", "direction": "ascending"}]


@pytest.mark.parametrize("when, titles", [
    ("today", ["Pay internet bill", "Renew passport"]),
    ("this week", ["Pay internet bill", "Renew passport", "Send invoice to client"]),
    ("overdue", ["Pay internet bill"]),
])
async def test_list_due(tools, ctx, when, titles):
    text = (await tools["list_tasks"].run({"when": when}, ctx)).text
    assert [line[2:].split("  (")[0] for line in text.splitlines()[1:]] == titles


async def test_add_task_with_due_date_and_owner(tools, ctx, api):
    out = await tools["add_task"].run({"title": "Renew road tax", "due": "friday"}, ctx)
    assert out.text == "📝 Added to Notion: Renew road tax, due Fri 23 Oct"
    assert out.footer == "🔗 https://www.notion.so/new"
    _, _, body = next(r for r in api.requests if r[1] == "/v1/pages")
    assert body["parent"] == {"type": "data_source_id", "data_source_id": SOURCE}
    props = body["properties"]
    assert props["Task name"]["title"][0]["text"]["content"] == "Renew road tax"
    assert props["Due"] == {"date": {"start": "2026-10-23"}}
    assert props["Status"] == {"status": {"name": "Not started"}}
    assert props["Assignee"] == {"people": [{"id": OWNER}]}  # so it shows in "My Tasks"


async def test_add_task_with_time(tools, ctx, api):
    await tools["add_task"].run({"title": "Call bank", "due": "tomorrow 3pm"}, ctx)
    _, _, body = next(r for r in api.requests if r[1] == "/v1/pages")
    assert body["properties"]["Due"] == {"date": {"start": "2026-10-20T15:00:00+08:00"}}


async def test_add_task_bad_date_asks(tools, ctx, api):
    out = await tools["add_task"].run({"title": "x", "due": "when pigs fly"}, ctx)
    assert "couldn't work out" in out.text
    assert not any(r[1] == "/v1/pages" for r in api.requests)


async def test_complete_asks_first_then_patches(tools, ctx, api):
    out = await tools["complete_task"].run({"task": "the passport task"}, ctx)
    assert isinstance(out, NeedsConfirm) and "Renew passport" in out.prompt
    assert not any(r[0] == "PATCH" for r in api.requests)
    done = await tools["complete_task"].execute(out.args, ctx)
    assert done.text == "✅ Done: Renew passport"
    assert api.requests[-1] == ("PATCH", "/v1/pages/p2", {"properties": {"Status": {"status": {"name": "Done"}}}})


async def test_complete_ambiguous_asks_which(tools, ctx):
    out = await tools["complete_task"].run({"task": "invoice"}, ctx)
    assert out.text.startswith("Which one?") and "Send invoice to client" in out.text \
        and "Send invoice reminder" in out.text


async def test_complete_unknown(tools, ctx):
    out = await tools["complete_task"].run({"task": "walk the dog"}, ctx)
    assert "can't find an open task" in out.text


async def test_not_set_up(ctx):
    t = {t.name: t for t in notion.tools(ctx)}
    assert "isn't set up" in (await t["list_tasks"].run({}, ctx)).text


async def test_unshared_database_explains(ctx):
    client = Notion("secret", DB, transport=httpx.MockTransport(FakeNotion(status=404)))
    t = {t.name: t for t in notion.tools(ctx, client)}
    assert "shared with the integration" in (await t["list_tasks"].run({}, ctx)).text


async def test_report_for_morning_agenda():
    client = Notion("secret", DB, transport=httpx.MockTransport(FakeNotion()))
    text = await notion.tasks_report(client, NOW, "today")
    assert text.startswith("📝 2 tasks due today:")


async def test_open_tasks_follow_pagination(api):
    first, second = PAGES[:3], PAGES[3:]

    def paged(req: httpx.Request):
        if req.url.path.endswith("/query"):
            cursor = json.loads(req.content).get("start_cursor")
            batch, more = (second, False) if cursor == "c2" else (first, True)
            return httpx.Response(200, json={"results": batch, "has_more": more, "next_cursor": "c2" if more else None})
        return api(req)
    client = Notion("secret", DB, transport=httpx.MockTransport(paged))
    assert [t.id for t in await client.open_tasks(NOW.tzinfo)] == ["p1", "p2", "p3", "p4", "p5"]


# --- "I finished X" when the model doesn't help ---

def test_guess_only_for_completions():
    assert notion.guess("I finished the passport renewal") == ("complete_task", {"task": "I finished the passport renewal"})
    assert notion.guess("dah siap invoice tu") is not None
    assert notion.guess("what's due this week?") is None


@pytest.mark.parametrize("text, expected", [
    ("I finished the passport renewal", "notion"),          # an open task matches
    ("I finished my workout, any stretching tips?", None),  # nothing matches: stays chat
    ("tell me a joke", None),                               # not a completion at all
])
async def test_rescue_needs_a_matching_open_task(api, ctx, text, expected):
    client = Notion("secret", DB, transport=httpx.MockTransport(api))
    assert await notion.rescue(client)(text, ctx) == expected


async def test_rescue_without_notion_does_nothing(ctx):
    assert await notion.rescue(None)("I finished the passport renewal", ctx) is None


async def test_router_says_chat_and_model_makes_no_call_still_asks_to_complete(api, ctx, store):
    """The worst case seen live: the router picks chat and the model makes no tool call."""
    from nhako.agent import Agent
    from nhako.tools import default_registry

    from .conftest import FakeLLM
    client = Notion("secret", DB, transport=httpx.MockTransport(api))
    ctx = dataclasses.replace(ctx, store=store)
    agent = Agent(FakeLLM("chat", tool=None), store, default_registry(ctx, notion=client), ctx)
    reply = await agent.handle(1, "I finished the passport renewal")
    assert reply.confirm_id and "Renew passport" in reply.text


async def test_wrong_category_with_no_tool_call_is_rescued(api, ctx, store):
    """Seen live: 'dah siap renew passport' routed to schedule, then no tool call."""
    from nhako.agent import Agent
    from nhako.tools import default_registry

    from .conftest import FakeLLM
    client = Notion("secret", DB, transport=httpx.MockTransport(api))
    ctx = dataclasses.replace(ctx, store=store)
    agent = Agent(FakeLLM("schedule", tool=None), store, default_registry(ctx, notion=client), ctx)
    reply = await agent.handle(1, "dah siap renew passport")
    assert reply.confirm_id and "Renew passport" in reply.text


# --- Hakim's Hub: Quests (select status with emoji) linked to Campaigns ---

HUB_DB = "9c215ed9baee4071ae4133a46acf029a"
QUESTS, CAMPAIGNS = "ds-quests", "ds-campaigns"
QUEST_PROPS = {
    "Quest": {"type": "title", "title": {}},
    "Due": {"type": "date", "date": {}},
    "Status": {"type": "select", "select": {"options": [
        {"name": "📜 To do"}, {"name": "⚔️ In progress"}, {"name": "✅ Done"}]}},
    "Difficulty": {"type": "select", "select": {"options": [{"name": "🟢 Easy"}, {"name": "🔴 Boss"}]}},
    "Where I'm at": {"type": "rich_text", "rich_text": {}},
    "GitHub": {"type": "url", "url": {}},
    "Campaign": {"type": "relation", "relation": {"data_source_id": CAMPAIGNS, "type": "dual_property"}},
    "XP": {"type": "formula", "formula": {}},
    "Realm": {"type": "select", "select": {"options": [{"name": "🏠 Personal"}, {"name": "🏢 Omni Work"}]}},
    "Cleared on": {"type": "date", "date": {}},
}
CAMPAIGN_PROPS = {
    "Campaign": {"type": "title", "title": {}},
    "About": {"type": "rich_text", "rich_text": {}},
    "Status": {"type": "select", "select": {"options": [
        {"name": "🌱 Idea"}, {"name": "⚔️ Active"}, {"name": "💤 Paused"}, {"name": "🏆 Cleared"}]}},
    "Progress": {"type": "formula", "formula": {}},
    "Manual %": {"type": "number", "number": {}},
    "Quest list": {"type": "relation", "relation": {"data_source_id": QUESTS}},
    "XP earned": {"type": "rollup", "rollup": {}},
}


def quest(pid, title, status="📜 To do", due=None, note="", campaign=None, xp=0, cleared=None,
          edited="2026-10-01T00:00:00.000Z"):
    return {"id": pid, "url": f"https://app.notion.com/p/{pid}", "last_edited_time": edited, "properties": {
        "Quest": {"title": [{"plain_text": title}]},
        "Due": {"date": {"start": due} if due else None},
        "Status": {"select": {"name": status} if status else None},
        "Where I'm at": {"rich_text": [{"plain_text": note}] if note else []},
        "Campaign": {"relation": [{"id": campaign}] if campaign else []},
        "XP": {"type": "formula", "formula": {"type": "number", "number": xp}},
        "Cleared on": {"date": {"start": cleared} if cleared else None},
    }}


def campaign(pid, title, status, progress):
    return {"id": pid, "url": f"https://app.notion.com/p/{pid}", "properties": {
        "Campaign": {"title": [{"plain_text": title}]},
        "Status": {"select": {"name": status}},
        "Progress": {"type": "formula", "formula": {"type": "string", "string": progress}},
        "Manual %": {"type": "number", "number": None},
    }}


QUEST_PAGES = [
    quest("q1", "Octomations Landing Page", due="2026-10-31", campaign="c-octo"),
    quest("q2", "Octomations Bug Fixes", due="2026-10-31", note="Planning", campaign="c-octo"),
    quest("q3", "Review Gacha Contract", "⚔️ In progress", due="2026-10-17"),
    quest("q4", "File Thumbnail", due="2026-10-31", campaign="c-beam"),
    # Done: one last week, one this week (NOW is Monday 19 Oct), one with no "Cleared on" (edited 5 Oct).
    quest("q5", "Pre-release nhako-beam v0.1.0", "✅ Done", campaign="c-beam", xp=25, cleared="2026-10-17"),
    quest("q6", "Fix login bug", "✅ Done", campaign="c-octo", xp=50, cleared="2026-10-19"),
    quest("q7", "Old chore", "✅ Done", xp=10, edited="2026-10-05T03:00:00.000Z"),
]
DEPOT_PAGE = {"id": "pg-depot", "object": "page", "url": "https://app.notion.com/p/pg-depot", "properties": {
    "Quest": {"type": "title", "title": [{"plain_text": "Nhako Depot"}]},
    "Status": {"type": "select", "select": {"name": "⚔️ In progress"}}}}
DEPOT_BLOCKS = [
    {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "Plan"}]}},
    {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "A self-hosted app store for my homelab."}]}},
    {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Pick a database"}], "checked": True}},
    {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "Docker images"}]}},
    {"type": "image", "image": {}},
]


def keep(page: dict, f: dict | None) -> bool:
    """The two filters the client sends: status does_not_equal / equals one option."""
    if not f:
        return True
    status = (page["properties"]["Status"]["select"] or {}).get("name")
    cond = f["select"]
    return status == cond["equals"] if "equals" in cond else status != cond["does_not_equal"]
CAMPAIGN_PAGES = [
    campaign("c-beam", "Nhako Beam", "⚔️ Active", "⚔️ ▰▰▱▱▱ 33%"),
    campaign("c-octo", "Octomations", "⚔️ Active", "⚔️ ▱▱▱▱▱ 0%"),
    campaign("c-nera", "Project Nera", "🏆 Cleared", "⚔️ ▰▰▰▰▰ 100%"),
]


class FakeHub(FakeNotion):
    def __call__(self, req: httpx.Request):
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, body))
        path = req.url.path
        if path == f"/v1/databases/{HUB_DB}":
            return httpx.Response(200, json={"data_sources": [{"id": QUESTS}]})
        if path == f"/v1/data_sources/{QUESTS}":
            return httpx.Response(200, json={"properties": QUEST_PROPS})
        if path == f"/v1/data_sources/{CAMPAIGNS}":
            return httpx.Response(200, json={"properties": CAMPAIGN_PROPS})
        if path == f"/v1/data_sources/{QUESTS}/query":
            return httpx.Response(200, json={"results": [p for p in QUEST_PAGES if keep(p, body.get("filter"))]})
        if path == "/v1/search":
            return httpx.Response(200, json={"results": [DEPOT_PAGE] if "depot" in body["query"].lower() else []})
        if path == "/v1/blocks/pg-depot/children" and req.method == "GET":
            return httpx.Response(200, json={"results": DEPOT_BLOCKS})
        if path.startswith("/v1/blocks/") and req.method == "PATCH":
            return httpx.Response(200, json={"results": []})
        if path == f"/v1/data_sources/{CAMPAIGNS}/query":
            return httpx.Response(200, json={"results": CAMPAIGN_PAGES})
        if path == "/v1/users":
            return httpx.Response(200, json={"results": [{"id": OWNER, "type": "person"}]})
        if path == "/v1/pages" and req.method == "POST":
            return httpx.Response(200, json={"id": "new", "url": "https://app.notion.com/p/new"})
        if path.startswith("/v1/pages/") and req.method == "PATCH":
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1]})
        return httpx.Response(404)


@pytest.fixture
def hub():
    return FakeHub()


@pytest.fixture
def hub_tools(ctx, hub):
    client = Notion("secret", f"https://app.notion.com/p/{HUB_DB}", transport=httpx.MockTransport(hub))
    return {t.name: t for t in notion.tools(ctx, client) + notion.project_tools(ctx, client)}


def patches(api):
    return [(path, body) for method, path, body in api.requests
            if method == "PATCH" and path.startswith("/v1/pages/")]


def test_hub_schema_reads_emoji_options_and_the_campaign_link():
    s = TaskSchema.from_properties(QUEST_PROPS)
    assert (s.status.name, s.status.kind) == ("Status", "select")  # not Difficulty
    assert s.status.done == ["✅ Done"] and s.status.start == "📜 To do" and s.status.progress == "⚔️ In progress"
    assert s.open_filter() == {"property": "Status", "select": {"does_not_equal": "✅ Done"}}
    assert (s.note, s.project, s.project_source) == ("Where I'm at", "Campaign", CAMPAIGNS)


@pytest.mark.parametrize("words, option", [
    ("in progress", "⚔️ In progress"), ("start working on it", "⚔️ In progress"),
    ("back to to do", "📜 To do"), ("not started", "📜 To do"), ("done", "✅ Done"), ("banana", None),
])
def test_status_words_map_to_options(words, option):
    assert TaskSchema.from_properties(QUEST_PROPS).status.option_for(words) == option


async def test_hub_list_shows_in_progress_and_notes(hub_tools, ctx):
    text = (await hub_tools["list_tasks"].run({}, ctx)).text
    assert "• Review Gacha Contract  (⚠️ overdue, due Sat 17 Oct)  · ⚔️ In progress" in text
    assert "• Octomations Bug Fixes  (due Sat 31 Oct)  · 🗺️ Octomations\n   ↳ Planning" in text
    assert "• File Thumbnail  (due Sat 31 Oct)  · 🗺️ Nhako Beam" in text
    assert "• Review Gacha Contract  (⚠️ overdue, due Sat 17 Oct)  · ⚔️ In progress\n" in text  # no project
    only = (await hub_tools["list_tasks"].run({"when": "in progress"}, ctx)).text
    assert only.startswith("📝 1 task in progress:") and "Gacha" in only


async def test_hub_add_sets_to_do_and_campaign(hub_tools, ctx, hub):
    out = await hub_tools["add_task"].run({"title": "Write release notes", "project": "nhako beam"}, ctx)
    assert out.text == "📝 Added to Notion: Write release notes, in Nhako Beam"
    _, _, body = next(r for r in hub.requests if r[1] == "/v1/pages")
    assert body["properties"]["Status"] == {"select": {"name": "📜 To do"}}  # so it lands on the board
    assert body["properties"]["Campaign"] == {"relation": [{"id": "c-beam"}]}


async def test_hub_add_unknown_project_adds_nothing(hub_tools, ctx, hub):
    out = await hub_tools["add_task"].run({"title": "x", "project": "moon base"}, ctx)
    assert "can't find a project" in out.text
    assert not any(r[1] == "/v1/pages" for r in hub.requests)


async def test_move_to_in_progress_asks_first(hub_tools, ctx, hub):
    out = await hub_tools["update_task"].run({"task": "landing page", "status": "in progress"}, ctx)
    assert isinstance(out, NeedsConfirm) and "Octomations Landing Page" in out.prompt
    assert "status → ⚔️ In progress" in out.prompt and not patches(hub)
    done = await hub_tools["update_task"].execute(out.args, ctx)
    assert done.text == "📝 Updated Octomations Landing Page: ⚔️ In progress"
    assert patches(hub) == [("/v1/pages/q1", {"properties": {"Status": {"select": {"name": "⚔️ In progress"}},
                                                             "Cleared on": {"date": None}}})]


async def test_update_note(hub_tools, ctx, hub):
    out = await hub_tools["update_task"].run({"task": "bug fixes", "note": "half the bugs fixed"}, ctx)
    await hub_tools["update_task"].execute(out.args, ctx)
    assert patches(hub) == [("/v1/pages/q2", {"properties": {
        "Where I'm at": {"rich_text": [{"text": {"content": "half the bugs fixed"}}]}}})]


async def test_update_unknown_status_lists_choices(hub_tools, ctx):
    out = await hub_tools["update_task"].run({"task": "landing page", "status": "banana"}, ctx)
    assert "📜 To do, ⚔️ In progress, ✅ Done" in out.text


async def test_update_needs_a_change(hub_tools, ctx):
    assert "What should I change" in (await hub_tools["update_task"].run({"task": "landing page"}, ctx)).text


async def test_remove_moves_to_trash_after_confirm(hub_tools, ctx, hub):
    out = await hub_tools["remove_task"].run({"task": "thumbnail"}, ctx)
    assert isinstance(out, NeedsConfirm) and "File Thumbnail" in out.prompt and not patches(hub)
    done = await hub_tools["remove_task"].execute(out.args, ctx)
    assert done.text.startswith("🗑 Removed: File Thumbnail")
    assert patches(hub) == [("/v1/pages/q4", {"in_trash": True})]


async def test_list_projects_hides_cleared(hub_tools, ctx):
    text = (await hub_tools["list_projects"].run({}, ctx)).text
    assert text.splitlines() == ["🗺️ 2 projects:", "• Nhako Beam  ⚔️ ▰▰▱▱▱ 33%",
                                 "• Octomations  ⚔️ ▱▱▱▱▱ 0%", "(+1 cleared)"]  # "Active" isn't news
    assert "Project Nera" in (await hub_tools["list_projects"].run({"project": "all"}, ctx)).text


async def test_one_project_shows_its_open_tasks(hub_tools, ctx):
    out = await hub_tools["list_projects"].run({"project": "octomations"}, ctx)
    assert out.text.startswith("🗺️ Octomations  ⚔️ ▱▱▱▱▱ 0%")
    assert "Octomations Landing Page" in out.text and "Bug Fixes" in out.text and "Thumbnail" not in out.text
    assert "🗺️" not in out.text.split("\n\n", 1)[1]  # the heading already names the project


async def test_project_names_are_read_once_per_ttl(hub):
    clock = [0.0]
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub), clock=lambda: clock[0])

    def reads():
        return len([r for r in hub.requests if r[1] == f"/v1/data_sources/{CAMPAIGNS}/query"])
    await client.open_tasks(NOW.tzinfo)
    await client.open_tasks(NOW.tzinfo)
    assert reads() == 1
    clock[0] = 601
    await client.open_tasks(NOW.tzinfo)
    assert reads() == 2


async def test_tasks_still_list_when_project_names_fail():
    class NoCampaigns(FakeHub):
        def __call__(self, req):
            if req.url.path == f"/v1/data_sources/{CAMPAIGNS}/query":
                return httpx.Response(500)
            return super().__call__(req)
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(NoCampaigns()))
    text = await notion.tasks_report(client, NOW)
    assert text.startswith("📝 4 tasks open:") and "🗺️" not in text


async def test_project_named_in_the_message_when_the_model_leaves_it_out(hub_tools, ctx):
    """Seen live: "what's left in Octomations?" -> list_projects with no project."""
    ctx = dataclasses.replace(ctx, text="what's left in Octomations?")
    out = await hub_tools["list_projects"].run({}, ctx)
    assert out.text.startswith("🗺️ Octomations") and "Landing Page" in out.text


async def test_set_progress_writes_manual_percent(hub_tools, ctx, hub):
    out = await hub_tools["update_project"].run({"project": "nhako beam", "percent": "50%"}, ctx)
    assert isinstance(out, NeedsConfirm) and "progress → 50%" in out.prompt and "33%" in out.prompt
    assert not patches(hub)
    done = await hub_tools["update_project"].execute(out.args, ctx)
    assert done.text == "🗺️ Nhako Beam: progress 50%"
    assert patches(hub) == [("/v1/pages/c-beam", {"properties": {"Manual %": {"number": 50.0}}})]


async def test_set_progress_auto_clears_the_override(hub_tools, ctx, hub):
    out = await hub_tools["update_project"].run({"project": "beam", "percent": "auto"}, ctx)
    await hub_tools["update_project"].execute(out.args, ctx)
    assert patches(hub) == [("/v1/pages/c-beam", {"properties": {"Manual %": {"number": None}}})]


@pytest.mark.parametrize("words, option", [
    ("pause", "💤 Paused"), ("paused", "💤 Paused"), ("resume", "⚔️ Active"), ("cleared", "🏆 Cleared"),
    ("finished", "🏆 Cleared"), ("idea", "🌱 Idea"),
])
async def test_project_status_words(hub_tools, ctx, hub, words, option):
    out = await hub_tools["update_project"].run({"project": "octomations", "status": words}, ctx)
    assert isinstance(out, NeedsConfirm), out
    await hub_tools["update_project"].execute(out.args, ctx)
    assert patches(hub) == [("/v1/pages/c-octo", {"properties": {"Status": {"select": {"name": option}}}})]


async def test_add_project_starts_as_an_idea(hub_tools, ctx, hub):
    out = await hub_tools["add_project"].run({"name": "Nhako Radio", "about": "internet radio bot"}, ctx)
    assert out.text == "🗺️ New project: Nhako Radio (🌱 Idea)"
    _, _, body = next(r for r in hub.requests if r[1] == "/v1/pages")
    assert body["parent"] == {"type": "data_source_id", "data_source_id": CAMPAIGNS}
    assert body["properties"]["Status"] == {"select": {"name": "🌱 Idea"}}
    assert body["properties"]["About"] == {"rich_text": [{"text": {"content": "internet radio bot"}}]}


async def test_add_project_active_when_asked(hub_tools, ctx, hub):
    await hub_tools["add_project"].run({"name": "Nhako Radio", "status": "active"}, ctx)
    _, _, body = next(r for r in hub.requests if r[1] == "/v1/pages")
    assert body["properties"]["Status"] == {"select": {"name": "⚔️ Active"}}


@pytest.mark.parametrize("text, value", [
    ("50%", 50.0), ("70", 70.0), ("0.5", 50.0), ("100 percent", 100.0), ("auto", None), ("reset", None),
])
def test_parse_percent(text, value):
    assert notion.parse_percent(text) == value


@pytest.mark.parametrize("text", ["150%", "half"])
def test_parse_percent_rejects(text):
    assert isinstance(notion.parse_percent(text), str)


async def test_projects_without_a_linked_database(ctx, api):
    client = Notion("secret", DB, transport=httpx.MockTransport(api))
    t = {t.name: t for t in notion.project_tools(ctx, client)}
    assert (await t["list_projects"].run({}, ctx)).text == notion.NO_PROJECTS
    out = await t["update_project"].run({"project": "x", "percent": "50"}, ctx)
    assert out.text == notion.NO_PROJECTS


async def test_projects_report_for_the_command(hub):
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub))
    assert (await notion.projects_report(client)).startswith("🗺️ 2 projects:")
    assert "Project Nera" in await notion.projects_report(client, "all")


# --- round 2: more fields, done quests, stats, pages, proactive messages ---

def test_hub_schema_extras():
    s = TaskSchema.from_properties(QUEST_PROPS)
    assert (s.due, s.cleared_on, s.xp) == ("Due", "Cleared on", "XP")  # "Cleared on" is not the due date
    assert s.tags == {"Difficulty": ["🟢 Easy", "🔴 Boss"], "Realm": ["🏠 Personal", "🏢 Omni Work"]}
    assert s.tag_for("boss") == ("Difficulty", "🔴 Boss")
    assert s.tag_for("omni work") == ("Realm", "🏢 Omni Work")
    assert s.tag_for("banana") is None


async def test_update_due_rename_and_tag_in_one_go(hub_tools, ctx, hub):
    out = await hub_tools["update_task"].run({"task": "bug fixes", "due": "friday", "new_name": "Octomations bugs",
                                              "tag": "boss"}, ctx)
    assert isinstance(out, NeedsConfirm)
    assert "due Fri 23 Oct" in out.prompt and "rename → “Octomations bugs”" in out.prompt
    assert "Difficulty → 🔴 Boss" in out.prompt
    done = await hub_tools["update_task"].execute(out.args, ctx)
    assert done.text == "📝 Updated Octomations bugs: due Fri 23 Oct, renamed to “Octomations bugs”, 🔴 Boss"
    assert patches(hub) == [("/v1/pages/q2", {"properties": {
        "Due": {"date": {"start": "2026-10-23"}},
        "Quest": {"title": [{"text": {"content": "Octomations bugs"}}]},
        "Difficulty": {"select": {"name": "🔴 Boss"}}}})]


async def test_clear_due_date(hub_tools, ctx, hub):
    out = await hub_tools["update_task"].run({"task": "landing page", "due": "no due date"}, ctx)
    await hub_tools["update_task"].execute(out.args, ctx)
    assert patches(hub) == [("/v1/pages/q1", {"properties": {"Due": {"date": None}}})]


async def test_unknown_tag_lists_what_can_be_set(hub_tools, ctx):
    out = await hub_tools["update_task"].run({"task": "landing page", "tag": "legendary"}, ctx)
    assert "Difficulty: 🟢 Easy, 🔴 Boss" in out.text


async def test_note_also_logs_to_the_page_body(hub_tools, ctx, hub):
    out = await hub_tools["update_task"].run({"task": "bug fixes", "note": "found the race condition"}, ctx)
    await hub_tools["update_task"].execute(out.args, ctx)
    method, path, body = hub.requests[-1]
    assert (method, path) == ("PATCH", "/v1/blocks/q2/children")
    assert body["children"][0]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == \
        "19 Oct 2026: found the race condition"


async def test_reopen_a_done_quest(hub_tools, ctx, hub):
    """Done quests aren't in the open list, but update can still find them; reopening clears 'Cleared on'."""
    out = await hub_tools["update_task"].run({"task": "login bug", "status": "back to to do"}, ctx)
    assert isinstance(out, NeedsConfirm) and "Fix login bug" in out.prompt
    await hub_tools["update_task"].execute(out.args, ctx)
    assert patches(hub) == [("/v1/pages/q6", {"properties": {"Status": {"select": {"name": "📜 To do"}},
                                                             "Cleared on": {"date": None}}})]


async def test_complete_only_looks_at_open_quests(hub_tools, ctx):
    out = await hub_tools["complete_task"].run({"task": "pre-release nhako-beam"}, ctx)
    assert "can't find an open task" in out.text


async def test_completing_sets_cleared_on(hub_tools, ctx, hub):
    out = await hub_tools["complete_task"].run({"task": "landing page"}, ctx)
    await hub_tools["complete_task"].execute(out.args, ctx)
    assert patches(hub) == [("/v1/pages/q1", {"properties": {"Status": {"select": {"name": "✅ Done"}},
                                                             "Cleared on": {"date": {"start": "2026-10-19"}}}})]


async def test_marking_done_through_update_also_sets_cleared_on(hub_tools, ctx, hub):
    out = await hub_tools["update_task"].run({"task": "landing page", "status": "done"}, ctx)
    await hub_tools["update_task"].execute(out.args, ctx)
    assert patches(hub)[0][1]["properties"]["Cleared on"] == {"date": {"start": "2026-10-19"}}


@pytest.mark.parametrize("when, head, xp", [
    ("", "🏆 This week: 1 quest cleared, +50 XP", "Fix login bug  +50 XP"),
    ("last week", "🏆 Last week: 1 quest cleared, +25 XP", "Pre-release nhako-beam v0.1.0  +25 XP"),
    ("this month", "🏆 This month: 3 quests cleared, +85 XP", "Old chore  +10 XP"),  # by last edit
    ("all time", "🏆 All time: 3 quests cleared, +85 XP", "Fix login bug  +50 XP"),
])
async def test_quest_stats(hub_tools, ctx, when, head, xp):
    text = (await hub_tools["quest_stats"].run({"period": when}, ctx)).text
    assert text.startswith(head) and xp in text
    assert "⭐ Total: 85 XP · Level 1 (15 XP to level 2)" in text


async def test_stats_name_the_top_project(hub_tools, ctx):
    text = (await hub_tools["quest_stats"].run({}, ctx)).text
    assert "🗺️ Most XP this week: Octomations (50 XP)" in text


def test_levels():
    assert notion.reports.level(0) == (1, 100)
    assert notion.reports.level(250) == (3, 50)


async def test_read_notion_page(hub_tools, ctx):
    out = await hub_tools["read_notion"].run({"page": "the Nhako Depot page"}, ctx)
    assert out.summarize and out.footer == "🔗 https://app.notion.com/p/pg-depot"
    assert "# Nhako Depot" in out.text and "Status: ⚔️ In progress" in out.text
    assert "## Plan" in out.text and "A self-hosted app store" in out.text
    assert "[x] Pick a database" in out.text and "- Docker images" in out.text


async def test_read_notion_nothing_found(hub_tools, ctx):
    assert "can't find a Notion page" in (await hub_tools["read_notion"].run({"page": "moon base"}, ctx)).text


async def test_morning_tasks_sections(hub):
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub))
    text = await notion.morning_tasks(client, NOW)
    assert text.startswith("⚠️ Overdue:\n• Review Gacha Contract")
    assert "📅 Next 3 days" not in text  # everything else is due on the 31st
    assert "⚔️ In progress:" not in text  # the in-progress quest is already listed as overdue
    assert "Landing Page" not in text and "Fix login bug" not in text


async def test_morning_tasks_soon_and_in_progress(hub):
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub))
    text = await notion.morning_tasks(client, NOW.replace(day=29))
    assert "📅 Next 3 days:\n• Octomations Landing Page" in text
    assert "⚠️ Overdue:\n• Review Gacha Contract" in text


async def test_weekly_review(hub):
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub))
    text = await notion.weekly_review(client, NOW)
    assert text.startswith("📊 Weekly review\n\n🏆 This week: 1 quest cleared, +50 XP")
    assert "🗺️ 2 projects:" in text and "⚠️ Overdue:" in text


async def test_overdue_nudge_only_once_per_quest(hub, store):
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub))
    first = await notion.overdue_nudge(client, NOW, store)
    assert first.startswith("⏰ Just went overdue:\n• Review Gacha Contract")
    assert await notion.overdue_nudge(client, NOW, store) is None  # nothing new
    later = await notion.overdue_nudge(client, NOW.replace(month=11, day=2), store)
    assert "Gacha" not in later and "Octomations Landing Page" in later  # only the newly overdue ones


async def test_schema_is_reread_after_the_ttl(hub):
    clock = [0.0]
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub), clock=lambda: clock[0])
    await client.schema()
    await client.schema()
    clock[0] = 601
    await client.schema()
    reads = [r for r in hub.requests if r[1] == f"/v1/data_sources/{QUESTS}"]
    assert len(reads) == 2


@pytest.mark.parametrize("text, expected", [
    ("push the bug fixes quest to friday", "notion"),     # router said schedule
    ("make the bug fixes quest a boss quest", "notion"),  # router said schedule
    ("how much XP did I earn this week?", "notion"),      # router said chat
    ("pause NeraOS", None),                               # not a project in the fake hub...
    ("pause Octomations", "notion"),                      # ...this one is (router said homelab)
    ("what's in my Nhako Depot page?", None),             # no project called Nhako Depot in the fake
    ("set Nhako Beam progress to 50%", "notion"),
    ("what's left in Octomations?", "notion"),
    ("is postiz down?", None),
    ("pause the music", None),                            # an action word, but no project named
    ("tell me about Octomations", None),                  # a project, but no action: leave it to the router
])
async def test_claim(hub, ctx, text, expected):
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub))
    assert await notion.claim(client)(text, ctx) == expected


async def test_claim_without_notion_does_nothing(ctx):
    assert await notion.claim(None)("how much XP?", ctx) is None


async def test_claimed_message_skips_the_router(hub, ctx, store):
    from nhako.agent import Agent
    from nhako.tools import default_registry

    from .conftest import FakeLLM
    client = Notion("secret", HUB_DB, transport=httpx.MockTransport(hub))
    ctx = dataclasses.replace(ctx, store=store)
    llm = FakeLLM("homelab", tool="update_project", args={"project": "octomations", "status": "pause"})
    reply = await Agent(llm, store, default_registry(ctx, notion=client), ctx).handle(1, "pause Octomations")
    assert reply.confirm_id and "💤 Paused" in reply.text
    assert not any(c["format"] for c in llm.calls)  # the router (a JSON-format call) never ran


async def test_add_for_an_existing_task_becomes_an_update(hub_tools, ctx, hub):
    """Seen live: "push the bug fixes quest to friday" -> add_task(title="bug fixes", due="friday")."""
    ctx = dataclasses.replace(ctx, text="push the bug fixes quest to friday")
    out = await hub_tools["add_task"].run({"title": "bug fixes", "due": "friday"}, ctx)
    assert isinstance(out, NeedsConfirm) and "Octomations Bug Fixes" in out.prompt and "due Fri 23 Oct" in out.prompt
    assert not any(r[1] == "/v1/pages" for r in hub.requests)  # no duplicate
    await hub_tools["add_task"].execute(out.args, ctx)
    assert patches(hub) == [("/v1/pages/q2", {"properties": {"Due": {"date": {"start": "2026-10-23"}}}})]


async def test_add_with_a_tag_for_a_project_becomes_a_tag_update(hub_tools, ctx):
    """Seen live: "make the bug fixes quest a boss quest" -> add_task(title="Bug Fixes", project="Boss Quest")."""
    ctx = dataclasses.replace(ctx, text="make the bug fixes quest a boss quest")
    out = await hub_tools["add_task"].run({"title": "Bug Fixes", "project": "Boss Quest"}, ctx)
    assert isinstance(out, NeedsConfirm) and "Difficulty → 🔴 Boss" in out.prompt


async def test_add_for_an_existing_task_with_nothing_to_change_says_so(hub_tools, ctx, hub):
    ctx = dataclasses.replace(ctx, text="landing page")
    out = await hub_tools["add_task"].run({"title": "landing page"}, ctx)
    assert out.text.startswith("You already have that task:\n• Octomations Landing Page")
    assert not any(r[1] == "/v1/pages" for r in hub.requests)


async def test_explicit_add_still_adds_even_if_similar(hub_tools, ctx, hub):
    ctx = dataclasses.replace(ctx, text="add another landing page task for NeraOS")
    out = await hub_tools["add_task"].run({"title": "landing page"}, ctx)
    assert out.text == "📝 Added to Notion: landing page"


async def test_rate_limit_is_retried_once(monkeypatch):
    calls = []

    def limited(req):
        calls.append(req.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"results": []})
    client = Notion("secret", DB, transport=httpx.MockTransport(limited))
    assert await client._req("GET", "/users") == {"results": []}
    assert len(calls) == 2
