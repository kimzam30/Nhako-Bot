"""Notion tasks against a fake API shaped like the real "My Tasks" template database."""
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
    assert (s.title, s.due, s.status, s.status_type, s.assignee) == ("Task name", "Due", "Status", "status", "Assignee")
    assert s.done == ["Done"] and s.start == "Not started"
    assert s.open_filter() == {"property": "Status", "status": {"does_not_equal": "Done"}}


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
    assert "• Send invoice to client  (due Wed 21 Oct 15:00)" in lines
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
