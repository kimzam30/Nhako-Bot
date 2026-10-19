"""Postiz against a fake public API shaped like the self-hosted backend (apps/backend public-api v1)."""
import json

import httpx
import pytest

from nhako.tools import NeedsConfirm, postiz
from nhako.tools.postiz import Channel, Postiz

CHANNELS = [
    {"id": "ch-x", "name": "kimzam", "identifier": "x", "disabled": False},
    {"id": "ch-li", "name": "Hakim K", "identifier": "linkedin", "disabled": False},
    {"id": "ch-lip", "name": "getgud MC", "identifier": "linkedin-page", "disabled": False},
    {"id": "ch-ig", "name": "getgud.mc", "identifier": "instagram", "disabled": True},
]

POSTS = [  # out of order on purpose; times are UTC as the API returns them
    {"id": "2", "publishDate": "2026-10-21T12:00:00.000Z", "state": "QUEUE",
     "content": "<p>Server wipe this Friday!</p><p>Back up your bases &amp; loot.</p>",
     "integration": {"id": "ch-x", "name": "kimzam", "providerIdentifier": "x"}},
    {"id": "1", "publishDate": "2026-10-19T01:00:00.000Z", "state": "PUBLISHED",
     "content": "<p>Good morning</p>", "integration": {"id": "ch-li", "name": "Hakim K", "providerIdentifier": "linkedin"}},
    {"id": "3", "publishDate": "2026-10-22T01:00:00.000Z", "state": "ERROR",
     "content": "<p>" + "long " * 40 + "</p>", "integration": {"id": "ch-li", "name": "Hakim K"}},
]


class FakePostiz:
    def __init__(self, status=200):
        self.status, self.requests = status, []

    def __call__(self, req: httpx.Request):
        assert req.headers["Authorization"] == "key-123"
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, dict(req.url.params), body))
        if self.status != 200:
            return httpx.Response(self.status, json={"msg": "Invalid API key"})
        if req.url.path == "/api/public/v1/integrations":
            return httpx.Response(200, json=CHANNELS)
        if req.url.path == "/api/public/v1/posts" and req.method == "GET":
            return httpx.Response(200, json={"posts": POSTS})
        if req.url.path == "/api/public/v1/posts" and req.method == "POST":
            return httpx.Response(201, json=[{"postId": "new", "integration": "ch-x"}])
        return httpx.Response(404)


@pytest.fixture
def api():
    return FakePostiz()


@pytest.fixture
def tools(ctx, api):
    ctx.settings.postiz_url = "https://postiz.example.com/api"
    client = Postiz("https://postiz.example.com/api/", "key-123", transport=httpx.MockTransport(api))
    return {t.name: t for t in postiz.tools(ctx, client)}


def test_plain_and_html():
    assert postiz.plain("<p>Hi &amp; bye</p><p>line two</p>") == "Hi & bye line two"
    assert postiz.plain("<p>" + "a" * 50 + "</p>", 10) == "a" * 9 + "…"
    assert postiz.to_html("Wipe Friday!\n\nBring <loot>") == "<p>Wipe Friday!</p><p>Bring &lt;loot&gt;</p>"


@pytest.mark.parametrize("wanted, ids", [
    ("", ["ch-x", "ch-li", "ch-lip"]),               # disabled instagram left out
    ("all", ["ch-x", "ch-li", "ch-lip"]),
    ("twitter", ["ch-x"]),
    ("x", ["ch-x"]),
    ("linkedin", ["ch-li", "ch-lip"]),
    ("getgud", ["ch-lip"]),
    ("x and linkedin", ["ch-x", "ch-li", "ch-lip"]),
    ("instagram", []),
    ("tiktok", []),
])
def test_pick_channels(wanted, ids):
    channels = [Channel(c["id"], c["name"], c["identifier"], c["disabled"]) for c in CHANNELS]
    assert [c.id for c in postiz.pick_channels(channels, wanted)] == ids


async def test_list_posts(tools, api, ctx):
    text = (await tools["list_posts"].run({"when": "this week"}, ctx)).text
    lines = text.splitlines()
    assert lines[0] == "📣 Postiz, this week: 1 scheduled, 1 published, 1 failed"
    assert lines[1] == "✅ Mon 19 Oct 09:00 · Hakim K: Good morning"          # UTC -> KL, sorted
    assert lines[2] == "⏳ Wed 21 Oct 20:00 · kimzam: Server wipe this Friday! Back up your bases & loot."
    assert lines[3].startswith("❌ Thu 22 Oct 09:00 · Hakim K: long long") and lines[3].endswith("…")
    _, _, params, _ = api.requests[-1]
    assert params == {"startDate": "2026-10-18T23:30:00+00:00", "endDate": "2026-10-25T16:00:00+00:00"}


async def test_draft_asks_first_then_creates_draft_only(tools, api, ctx):
    out = await tools["draft_post"].run({"content": "Server wipe this Friday!\nBack up your loot.",
                                         "channel": "twitter", "when": "thursday 8pm"}, ctx)
    assert isinstance(out, NeedsConfirm)
    assert "Thu 22 Oct 20:00 on kimzam (x)" in out.prompt and "doesn't publish" in out.prompt
    assert not any(r[0] == "POST" for r in api.requests)

    done = await tools["draft_post"].execute(out.args, ctx)
    assert "Draft saved" in done.text and "https://postiz.example.com/launches" in done.text
    _, path, _, body = api.requests[-1]
    assert path == "/api/public/v1/posts"
    assert body["type"] == "draft" and body["date"] == "2026-10-22T12:00:00Z"
    assert body["posts"] == [{"type": "draft", "integration": {"id": "ch-x"}, "settings": {},
                              "value": [{"content": "<p>Server wipe this Friday!</p><p>Back up your loot.</p>",
                                         "image": []}]}]


async def test_draft_defaults_to_tomorrow_9am_on_all_channels(tools, ctx):
    out = await tools["draft_post"].run({"content": "hello"}, ctx)
    assert "Tue 20 Oct 09:00 on kimzam (x), Hakim K (linkedin), getgud MC (linkedin-page)" in out.prompt


async def test_draft_unknown_channel_lists_options(tools, ctx):
    out = await tools["draft_post"].run({"content": "hi", "channel": "tiktok"}, ctx)
    assert "called “tiktok”" in out.text and "kimzam (x)" in out.text and "getgud.mc" not in out.text


async def test_draft_in_the_past_refused(tools, ctx):
    out = await tools["draft_post"].run({"content": "hi", "when": "today 6am"}, ctx)
    assert "already passed" in out.text


async def test_bad_key(ctx):
    client = Postiz("https://p/api", "key-123", transport=httpx.MockTransport(FakePostiz(status=401)))
    t = {t.name: t for t in postiz.tools(ctx, client)}
    assert "rejected the API key" in (await t["list_posts"].run({}, ctx)).text


async def test_not_set_up(ctx):
    t = {t.name: t for t in postiz.tools(ctx)}
    assert "isn't set up" in (await t["list_posts"].run({}, ctx)).text
