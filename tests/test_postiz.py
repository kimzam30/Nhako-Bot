"""Postiz against a fake public API shaped like the self-hosted backend (apps/backend public-api v1)."""
import datetime as dt
import json

import httpx
import pytest

from nhako.tools import NeedsConfirm, postiz
from nhako.tools.postiz import Channel, Postiz

from .conftest import NOW

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


def when(post):
    return dt.datetime.fromisoformat(post["publishDate"].replace("Z", "+00:00"))


class FakePostiz:
    def __init__(self, status=200, channels=None, posts=None, analytics=None):
        self.status, self.requests = status, []
        self.channels, self.posts = channels or CHANNELS, posts or POSTS
        self.analytics = analytics or {}  # path after /analytics/ -> metrics; anything missing is a 404

    def __call__(self, req: httpx.Request):
        assert req.headers["Authorization"] == "key-123"
        body = json.loads(req.content) if req.content else None
        self.requests.append((req.method, req.url.path, dict(req.url.params), body))
        if self.status != 200:
            return httpx.Response(self.status, json={"msg": "Invalid API key"})
        if req.url.path == "/api/public/v1/integrations":
            return httpx.Response(200, json=self.channels)
        if req.url.path == "/api/public/v1/posts" and req.method == "GET":
            start, end = (dt.datetime.fromisoformat(req.url.params[k]) for k in ("startDate", "endDate"))
            return httpx.Response(200, json={"posts": [p for p in self.posts if start <= when(p) <= end]})
        if req.url.path.startswith("/api/public/v1/analytics/"):
            found = self.analytics.get(req.url.path.removeprefix("/api/public/v1/analytics/"))
            return httpx.Response(404, json={"msg": "nope"}) if found is None else httpx.Response(200, json=found)
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


# --- stats and the TikTok inbox reminder ---

def metric(label, *totals, change=None):
    return {"label": label, "data": [{"total": str(t), "date": f"2026-10-{12 + i}"} for i, t in enumerate(totals)],
            "percentageChange": change}


SOCIAL = [
    {"id": "ch-tt", "name": "getgud.tt", "identifier": "tiktok", "disabled": False},
    {"id": "ch-x", "name": "kimzam", "identifier": "x", "disabled": False},
]
TT = {"id": "ch-tt", "name": "getgud.tt", "providerIdentifier": "tiktok"}
X = {"id": "ch-x", "name": "kimzam", "providerIdentifier": "x"}
SOCIAL_POSTS = [  # NOW is Sun 18 Oct 23:30 UTC
    {"id": "t1", "publishDate": "2026-10-18T22:00:00.000Z", "state": "PUBLISHED", "releaseId": "missing",
     "content": "<p>Season 2 trailer</p>", "integration": TT},
    {"id": "t2", "publishDate": "2026-10-18T10:00:00.000Z", "state": "ERROR", "content": "<p>Wipe clip</p>",
     "integration": TT},
    {"id": "t3", "publishDate": "2026-10-18T23:33:00.000Z", "state": "QUEUE", "content": "<p>Later</p>",
     "integration": TT},
    {"id": "x1", "publishDate": "2026-10-17T12:00:00.000Z", "state": "PUBLISHED", "releaseId": "190",
     "content": "<p>Server wipe this Friday!</p>", "integration": X},
    {"id": "x2", "publishDate": "2026-10-16T12:00:00.000Z", "state": "PUBLISHED", "releaseId": "191",
     "content": "<p>New spawn build</p>", "integration": X},
]
ANALYTICS = {
    "post/x1": [metric("Likes", 150, 175), metric("Comments", 25, 30), metric("Impressions", 4000, 5200)],
    "post/x2": [],
    "ch-x": [metric("Followers", 1250, 1280, change=2.4), metric("Impressions", 5000, 5200, change=4.0)],
    "ch-tt": [metric("Views", 900, 1100, change=-12.0)],
}


@pytest.fixture
def social():
    return FakePostiz(channels=SOCIAL, posts=SOCIAL_POSTS, analytics=ANALYTICS)


def client_for(api):
    return Postiz("https://postiz.example.com/api", "key-123", transport=httpx.MockTransport(api))


@pytest.fixture
def stats(ctx, social):
    ctx.settings.postiz_url = "https://postiz.example.com/api"
    return {t.name: t for t in postiz.tools(ctx, client_for(social))}


def analytics_calls(api):
    return [path for _, path, _, _ in api.requests if "/analytics/" in path]


def test_metrics_line():
    m = [metric("Likes", 150, 175), metric("Impressions", 5000, 5200, change=4.0), metric("Empty")]
    assert postiz.metrics_line(m) == "❤️ 175 likes · 👁 5.2k impressions"  # post stats: running totals
    channel = [metric("Followers", 1250, 1280, change=2.4), metric("Impressions", 5000, 5200, change=4.0)]
    assert postiz.metrics_line(channel, running=False, change=True) == \
        "👥 1.3k followers (+2%) · 👁 10.2k impressions (+4%)"  # followers: latest; impressions: summed
    assert postiz.short_number(42) == "42" and postiz.short_number(1_260_000) == "1.3M"


@pytest.mark.parametrize("text, label, start", [
    ("", "the last 7 days", "2026-10-12T07:30"),
    ("this week", "this week", "2026-10-19T00:00"),
    ("last week", "last week", "2026-10-12T00:00"),
    ("minggu lepas", "last week", "2026-10-12T00:00"),
    ("this month", "the last 30 days", "2026-09-19T07:30"),
    ("yesterday", "yesterday", "2026-10-18T00:00"),
    ("14 days", "the last 14 days", "2026-10-05T07:30"),
])
def test_past_range(text, label, start):
    start_at, end_at, got = postiz.past_range(text, NOW)
    assert got == label and start_at.isoformat().startswith(start) and end_at <= NOW


async def test_post_stats(stats, social, ctx):
    text = (await stats["post_stats"].run({}, ctx)).text
    lines = text.splitlines()
    assert lines[0] == "📊 Post stats, the last 7 days:"
    assert lines[1] == "✅ Mon 19 Oct 06:00 · getgud.tt: Season 2 trailer"  # newest first
    assert lines[2].strip() == "no stats: Postiz isn't linked to the live post"
    assert "Server wipe" in lines[3] and lines[4].strip() == "❤️ 175 likes · 💬 30 comments · 👁 5.2k impressions"
    assert "New spawn" in lines[5] and lines[6].strip() == "no stats from this network"
    assert "Wipe clip" not in text and "Later" not in text  # failed and queued posts have no stats
    assert analytics_calls(social) == ["/api/public/v1/analytics/post/x1", "/api/public/v1/analytics/post/x2"]


async def test_post_stats_by_channel_and_words(stats, ctx):
    text = (await stats["post_stats"].run({"channel": "twitter", "about": "server wipe"}, ctx)).text
    assert "Server wipe" in text and "Season 2" not in text and "spawn" not in text
    assert "called “facebook”" in (await stats["post_stats"].run({"channel": "facebook"}, ctx)).text


async def test_post_stats_looks_back_a_month_when_the_week_is_empty(stats, social, ctx):
    social.posts = [{**SOCIAL_POSTS[3], "publishDate": "2026-10-01T12:00:00.000Z"}]
    assert (await stats["post_stats"].run({}, ctx)).text.startswith("📊 Post stats, the last 30 days:")
    assert "📭" in (await stats["post_stats"].run({"when": "this week"}, ctx)).text  # a period was asked for


async def test_post_stats_are_capped_and_cached(stats, social, ctx):
    social.posts = [{**SOCIAL_POSTS[3], "id": f"p{i}", "publishDate": f"2026-10-1{i}T12:00:00.000Z"}
                    for i in range(2, 9)]
    assert "(showing the newest 5 of 7)" in (await stats["post_stats"].run({}, ctx)).text
    assert len(analytics_calls(social)) == 5
    await stats["post_stats"].run({}, ctx)
    assert len(analytics_calls(social)) == 5  # the second answer came from the cache


async def test_channel_stats(stats, social, ctx):
    text = (await stats["channel_stats"].run({"period": "this month"}, ctx)).text
    assert text.splitlines() == ["📈 Channels, last 30 days:",
                                 "getgud.tt (tiktok): 👁 2k views (-12%)",
                                 "kimzam (x): 👥 1.3k followers (+2%) · 👁 10.2k impressions (+4%)"]
    assert social.requests[-1][2] == {"date": "30"}


async def test_channel_without_analytics(ctx, social):
    social.analytics = {}
    t = {t.name: t for t in postiz.tools(ctx, client_for(social))}
    assert "no analytics for getgud.tt" in (await t["channel_stats"].run({"channel": "tiktok"}, ctx)).text


async def test_rate_limited(ctx):
    t = {t.name: t for t in postiz.tools(ctx, client_for(FakePostiz(status=429)))}
    assert "limit is used up" in (await t["channel_stats"].run({}, ctx)).text


async def test_tiktok_watch_reminds_once(social, store):
    client = client_for(social)
    assert await postiz.TikTokWatch(client, store).check(NOW) == [
        "❌ TikTok upload failed for “Wipe clip” (Sun 18:00). Check it in Postiz.",
        "📲 TikTok: “Season 2 trailer” is waiting in your TikTok inbox. Open TikTok and post it before "
        "Tue 20 Oct 06:00.",
    ]  # the queued TikTok and the X posts: nothing
    _, _, params, _ = social.requests[-1]
    assert params == {"startDate": "2026-10-17T23:30:00+00:00", "endDate": "2026-10-18T23:35:00+00:00"}
    assert await postiz.TikTokWatch(client, store).check(NOW) == []  # not again, even after a restart


async def test_tiktok_watch_forgets_old_posts(social, store):
    await store.set(postiz.NOTIFIED_KEY, '{"old": "2026-10-01T00:00:00.000Z"}')
    social.posts = []
    await postiz.TikTokWatch(client_for(social), store).check(NOW)
    assert await store.get(postiz.NOTIFIED_KEY) == "{}"


async def test_weekly_digest(social):
    lines = (await postiz.weekly_digest(client_for(social), NOW)).splitlines()
    assert lines[0] == "📣 Social media, last 7 days: 3 post(s) published (2 on kimzam, 1 on getgud.tt)"
    assert lines[-3:] == ["Channels:", "getgud.tt (tiktok): 👁 2k views (-12%)",
                          "kimzam (x): 👥 1.3k followers (+2%) · 👁 10.2k impressions (+4%)"]
    social.posts, social.analytics = [], {}
    assert await postiz.weekly_digest(client_for(social), NOW) is None
