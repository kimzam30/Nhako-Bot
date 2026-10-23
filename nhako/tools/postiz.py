"""Postiz (self-hosted social media scheduler): what's scheduled, post and channel stats,
drafting posts, and a ping at each TikTok post's time to publish it from the TikTok app.

Uses Postiz's public API (Settings → Public API → API key). The key can do anything
in the organisation, so the bot only ever reads posts and analytics and creates DRAFTS:
a draft never publishes until you schedule it yourself in Postiz.
"""
import datetime as dt
import html
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from ..timeparse import parse_range, parse_when
from . import Context, NeedsConfirm, Result, Tool

log = logging.getLogger(__name__)

NOT_SET_UP = "Postiz isn't set up. Put POSTIZ_URL and POSTIZ_API_KEY in .env (see .env.example)."
STATE_ICON = {"QUEUE": "⏳", "PUBLISHED": "✅", "ERROR": "❌", "DRAFT": "📝"}
# What people call a network -> Postiz provider identifiers it covers.
ALIASES = {"twitter": "x", "tweet": "x", "ig": "instagram", "insta": "instagram", "fb": "facebook",
           "yt": "youtube", "linked": "linkedin", "bsky": "bluesky", "tt": "tiktok", "tiktoks": "tiktok"}
DRAFT_HOUR = 9  # "draft a post for friday" with no time
# Analytics endpoints are rate limited (30/hour on Postiz cloud), so answers are cached and capped.
STATS_TTL = 30 * 60
MAX_POST_STATS = 5
METRIC_ICON = {"like": "❤️", "comment": "💬", "share": "🔁", "repost": "🔁", "view": "👁", "impression": "👁",
               "follower": "👥", "subscriber": "👥", "save": "🔖", "click": "🖱", "reach": "📡"}
# Channel metrics that are running totals (show the latest value); the others are per day (summed).
RUNNING_TOTALS = ("follower", "subscriber", "fan", "following")
INBOX_HOURS = 24  # TikTok throws away an inbox upload that isn't published within a day
REFRESH_EVERY = dt.timedelta(minutes=5)  # how often the TikTok schedule is fetched from Postiz
LOOKAHEAD = dt.timedelta(hours=6)        # how far ahead it is fetched
NOTIFIED_KEY = "tiktok_notified"


@dataclass
class Channel:
    id: str
    name: str
    identifier: str  # provider: x, linkedin, linkedin-page, instagram, ...
    disabled: bool = False

    def label(self) -> str:
        return f"{self.name} ({self.identifier})"


def plain(content: str, limit: int = 0) -> str:
    """Postiz stores post text as HTML. -> one readable line."""
    text = re.sub(r"<br\s*/?>|</p>\s*<p[^>]*>", " ", content or "")
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()
    return text[: limit - 1] + "…" if limit and len(text) > limit else text


def to_html(text: str) -> str:
    """Plain text -> the <p> paragraphs the Postiz editor uses."""
    return "".join(f"<p>{html.escape(line)}</p>" for line in text.strip().splitlines() if line.strip())


def pick_channels(channels: list[Channel], wanted: str) -> list[Channel]:
    """'x and linkedin' -> those channels. Empty or 'all' -> every active channel; no match -> []."""
    active = [c for c in channels if not c.disabled]
    words = {ALIASES.get(w, w) for w in re.findall(r"[\w.-]+", (wanted or "").lower())}
    words -= {"and", "dan", "on", "to", "for", "my", "the", "channel", "channels", "page", "account"}
    if not words or words & {"all", "semua", "everywhere", "every"}:
        return active
    return [c for c in active
            if any(w == c.identifier or c.identifier.startswith(w + "-") or (len(w) > 2 and w in c.name.lower())
                   for w in words)]


def short_number(n: float) -> str:
    """5200 -> '5.2k', 1250000 -> '1.3M', 42 -> '42'."""
    for size, suffix in ((1_000_000, "M"), (1_000, "k")):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{n:g}"


def metrics_line(metrics: list[dict], running: bool = True, change: bool = False) -> str:
    """Postiz analytics ([{label, data: [{total, date}], percentageChange}]) -> one line.

    running=True: every metric is a running total (post analytics), so the latest point counts.
    running=False: channel analytics, where only follower-type metrics are running totals and the
    rest are per-day numbers, added up over the period."""
    parts = []
    for m in metrics or []:
        values = []
        for point in m.get("data") or []:
            try:
                values.append(float(point.get("total") or 0))
            except (TypeError, ValueError):
                continue
        if not values:
            continue
        label = (m.get("label") or "?").strip()
        low = label.lower()
        value = values[-1] if running or any(w in low for w in RUNNING_TOTALS) else sum(values)
        icon = next((i for word, i in METRIC_ICON.items() if word in low), "•")
        part = f"{icon} {short_number(value)} {low}"
        if change and isinstance(m.get("percentageChange"), (int, float)) and m["percentageChange"]:
            part += f" ({m['percentageChange']:+.0f}%)"
        parts.append(part)
    return " · ".join(parts)


def past_range(text: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """A window that has already happened, for stats: 'this week', 'last week', 'yesterday',
    'this month', '14 days'. Empty or unknown -> the last 7 days."""
    t = (text or "").lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = midnight - dt.timedelta(days=now.weekday())
    if m := re.search(r"(\d+)\s*(days?|hari)", t):
        days = max(1, min(int(m.group(1)), 90))
        return now - dt.timedelta(days=days), now, f"the last {days} days"
    if re.search(r"\b(today|hari ini|harini)\b", t):
        return midnight, now, "today"
    if re.search(r"\b(yesterday|semalam)\b", t):
        return midnight - dt.timedelta(days=1), midnight, "yesterday"
    if re.search(r"\b(last|previous|lepas)\b", t) and re.search(r"\b(week|minggu)\b", t):
        return monday - dt.timedelta(days=7), monday, "last week"
    if re.search(r"\b(month|bulan|30)\b", t):
        return now - dt.timedelta(days=30), now, "the last 30 days"
    if re.search(r"\b(week|minggu)\b", t):
        return monday, now, "this week"
    return now - dt.timedelta(days=7), now, "the last 7 days"


def is_tiktok(post: dict) -> bool:
    return ((post.get("integration") or {}).get("providerIdentifier") or "").startswith("tiktok")


def published_at(post: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(post["publishDate"].replace("Z", "+00:00"))


class Postiz:
    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/public/v1", transport=transport,
                                        timeout=20, headers={"Authorization": api_key})
        self._cache: dict[str, tuple[float, list]] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, path: str, **kw):
        r = await self.client.request(method, path, **kw)
        if r.status_code >= 400:
            log.warning("postiz %s %s -> %s %s", method, path, r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json()

    async def channels(self) -> list[Channel]:
        return [Channel(i["id"], i.get("name") or i["identifier"], i["identifier"], bool(i.get("disabled")))
                for i in await self._req("GET", "/integrations")]

    async def posts(self, start: dt.datetime, end: dt.datetime) -> list[dict]:
        utc = dt.UTC
        data = await self._req("GET", "/posts", params={"startDate": start.astimezone(utc).isoformat(),
                                                         "endDate": end.astimezone(utc).isoformat()})
        return sorted(data.get("posts", []), key=lambda p: p.get("publishDate", ""))

    async def create_draft(self, content: str, channel_ids: list[str], at: dt.datetime) -> list:
        body = {
            "type": "draft", "shortLink": False, "tags": [],
            "date": at.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
            # "type": "draft" per post too: Postiz then skips each network's settings checks.
            "posts": [{"type": "draft", "integration": {"id": cid}, "settings": {},
                       "value": [{"content": to_html(content), "image": []}]} for cid in channel_ids],
        }
        return await self._req("POST", "/posts", json=body)

    async def _analytics(self, path: str, days: int) -> list[dict]:
        """Cached. A channel or post with no analytics (unsupported network, not linked yet) -> []."""
        key = f"{path}?{days}"
        hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            data = await self._req("GET", path, params={"date": days})
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 404):
                raise
            data = []
        data = data if isinstance(data, list) else []
        self._cache[key] = (time.monotonic() + STATS_TTL, data)
        return data

    async def post_stats(self, post_id: str, days: int = 7) -> list[dict]:
        return await self._analytics(f"/analytics/post/{post_id}", days)

    async def channel_stats(self, integration_id: str, days: int = 7) -> list[dict]:
        return await self._analytics(f"/analytics/{integration_id}", days)


def post_line(post: dict, tz) -> str:
    at = published_at(post).astimezone(tz)
    who = (post.get("integration") or {}).get("name") or (post.get("integration") or {}).get("providerIdentifier", "?")
    return f"{STATE_ICON.get(post.get('state'), '•')} {at:%a %d %b %H:%M} · {who}: {plain(post.get('content', ''), 90)}"


async def post_stats_lines(postiz: Postiz, posts: list[dict], tz) -> list[str]:
    """Up to MAX_POST_STATS published posts, newest first, each with its numbers underneath."""
    lines = []
    for p in sorted(posts, key=lambda p: p.get("publishDate", ""), reverse=True)[:MAX_POST_STATS]:
        if p.get("releaseId") == "missing":  # TikTok inbox uploads: Postiz never learns the live post's id
            note = "no stats: Postiz isn't linked to the live post"
        else:
            note = metrics_line(await postiz.post_stats(p["id"])) or "no stats from this network"
        lines.append(f"{post_line(p, tz)}\n      {note}")
    return lines


async def channel_stats_lines(postiz: Postiz, channels: list[Channel], days: int) -> tuple[list[str], list[str]]:
    """-> (one line per channel with numbers, names of channels without analytics)."""
    lines, without = [], []
    for c in channels:
        if line := metrics_line(await postiz.channel_stats(c.id, days), running=False, change=True):
            lines.append(f"{c.label()}: {line}")
        else:
            without.append(c.name)
    return lines, without


async def weekly_digest(postiz: Postiz, now: dt.datetime) -> str | None:
    """Sunday digest: what went out in the last 7 days, how those posts did, and each channel's week."""
    posts = [p for p in await postiz.posts(now - dt.timedelta(days=7), now) if p.get("state") == "PUBLISHED"]
    channels = [c for c in await postiz.channels() if not c.disabled]
    channel_lines, _ = await channel_stats_lines(postiz, channels, 7)
    if not posts and not channel_lines:
        return None
    per_channel: dict[str, int] = {}
    for p in posts:
        name = (p.get("integration") or {}).get("name") or "?"
        per_channel[name] = per_channel.get(name, 0) + 1
    out = [f"📣 Social media, last 7 days: {len(posts)} post(s) published"
           + (f" ({', '.join(f'{n} on {name}' for name, n in per_channel.items())})" if per_channel else "")]
    if posts:
        out += await post_stats_lines(postiz, posts, now.tzinfo)
    if channel_lines:
        out += ["", "Channels:"] + channel_lines
    return "\n".join(out)


class TikTokWatch:
    """Postiz sends TikTok posts with the UPLOAD method to the TikTok app's inbox, and nothing is live
    until the user publishes it there (within 24 h). So ping at each TikTok post's scheduled time, once.

    Postiz's API is rate limited, so the schedule is fetched every REFRESH_EVERY and checked against
    the clock on every scheduler tick: the ping lands within a tick of the post time."""

    def __init__(self, postiz: Postiz, store):
        self.postiz, self.store = postiz, store
        self.schedule: list[dict] = []
        self.refreshed: dt.datetime | None = None

    async def refresh(self, now: dt.datetime) -> None:
        self.refreshed = now  # first: a Postiz outage is retried at the next refresh, not every tick
        posts = await self.postiz.posts(now - dt.timedelta(hours=INBOX_HOURS), now + LOOKAHEAD)
        self.schedule = [p for p in posts if is_tiktok(p) and p.get("state") in ("QUEUE", "PUBLISHED", "ERROR")]

    async def check(self, now: dt.datetime) -> list[str]:
        if self.refreshed is None or now - self.refreshed >= REFRESH_EVERY:
            await self.refresh(now)
        notified: dict[str, str] = json.loads(await self.store.get(NOTIFIED_KEY) or "{}")
        alerts = []
        for p in self.schedule:
            at = published_at(p).astimezone(now.tzinfo)
            if at > now:
                continue
            text = plain(p.get("content", ""), 90) or "(no caption)"
            if p["state"] == "ERROR":
                if p["id"] + ":error" not in notified:
                    alerts.append(f"❌ TikTok upload failed for “{text}” ({at:%a %H:%M}). Check it in Postiz.")
                    notified[p["id"] + ":error"] = p["publishDate"]
            elif p["id"] not in notified:
                deadline = at + dt.timedelta(hours=INBOX_HOURS)
                alerts.append(f"📲 TikTok post time ({at:%H:%M}): “{text}”. Open TikTok and post it from your "
                              f"inbox (TikTok keeps it until {deadline:%a %H:%M}).")
            notified.setdefault(p["id"], p["publishDate"])  # a failed post needs no "post it now" ping too
        cutoff = now - dt.timedelta(days=3)
        notified = {k: v for k, v in notified.items()
                    if dt.datetime.fromisoformat(v.replace("Z", "+00:00")) > cutoff}
        await self.store.set(NOTIFIED_KEY, json.dumps(notified))
        return alerts


def tools(ctx: Context, postiz: Postiz | None = None) -> list[Tool]:
    s = ctx.settings
    postiz = postiz or (Postiz(s.postiz_url, s.postiz_api_key) if s.postiz_url and s.postiz_api_key else None)
    web_ui = re.sub(r"/api/?$", "", s.postiz_url.rstrip("/"))

    def guard(fn):
        async def run(args: dict, ctx: Context):
            if postiz is None:
                return Result(NOT_SET_UP)
            try:
                return await fn(args, ctx)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    return Result("Postiz rejected the API key (POSTIZ_API_KEY).")
                if e.response.status_code == 429:
                    return Result("Postiz's API limit is used up for now. Try again in an hour or so.")
                raise
            except httpx.HTTPError as e:
                log.warning("postiz unreachable: %r", e)
                return Result("I couldn't reach Postiz just now. Is it up? (try /docker postiz)")
        return run

    async def list_posts(args: dict, ctx: Context) -> Result:
        now = ctx.now()
        start, end, label = parse_range(args.get("when") or "", now)
        posts = await postiz.posts(start, end)
        if not posts:
            return Result(f"📭 Nothing in Postiz for {label}.")
        counts = {}
        for p in posts:
            counts[p.get("state")] = counts.get(p.get("state"), 0) + 1
        states = (("QUEUE", "scheduled"), ("PUBLISHED", "published"), ("DRAFT", "draft"), ("ERROR", "failed"))
        summary = ", ".join(f"{n} {name}" for state, name in states if (n := counts.get(state)))
        lines = [post_line(p, now.tzinfo) for p in posts[:25]]
        more = f"\n…and {len(posts) - 25} more" if len(posts) > 25 else ""
        return Result(f"📣 Postiz, {label}: {summary}\n" + "\n".join(lines) + more)

    async def draft_post(args: dict, ctx: Context) -> Result | NeedsConfirm:
        content = (args.get("content") or "").strip()
        if not content:
            return Result("What should the post say?")
        channels = await postiz.channels()
        chosen = pick_channels(channels, args.get("channel") or "")
        if not chosen:
            names = ", ".join(c.label() for c in channels if not c.disabled) or "none connected"
            return Result(f"I don't have a Postiz channel called “{args.get('channel')}”. Channels: {names}")
        now = ctx.now()
        when = parse_when(args.get("when") or "", now) if (args.get("when") or "").strip() else None
        if when is None:
            at = (now + dt.timedelta(days=1)).replace(hour=DRAFT_HOUR, minute=0, second=0, microsecond=0)
        else:
            at = when.at if when.has_time else when.at.replace(hour=DRAFT_HOUR)
        if at <= now:
            return Result(f"{at:%a %d %b %H:%M} has already passed. When should the post go out?")
        prompt = (f"Save this as a Postiz draft for {at:%a %d %b %H:%M} on "
                  f"{', '.join(c.label() for c in chosen)}?\n\n{content}\n\n"
                  "(A draft doesn't publish. You schedule it in Postiz.)")
        return NeedsConfirm(prompt, {"content": content, "channels": [c.id for c in chosen],
                                     "names": [c.name for c in chosen], "at": at.isoformat()})

    async def draft_execute(args: dict, ctx: Context) -> Result:
        at = dt.datetime.fromisoformat(args["at"])
        await postiz.create_draft(args["content"], args["channels"], at)
        return Result(f"📝 Draft saved in Postiz for {at:%a %d %b %H:%M} ({', '.join(args['names'])}).\n"
                      f"Review and schedule it: {web_ui}/launches")

    async def post_stats(args: dict, ctx: Context) -> Result:
        now = ctx.now()
        when, wanted = (args.get("when") or "").strip(), (args.get("channel") or "").strip()
        about = [w for w in re.findall(r"\w+", (args.get("about") or "").lower()) if len(w) > 2]
        chosen = {c.id for c in pick_channels(await postiz.channels(), wanted)} if wanted else None
        if chosen == set():
            return Result(f"I don't have a Postiz channel called “{wanted}”.")

        async def find(text: str):
            start, end, label = past_range(text, now)
            found = [p for p in await postiz.posts(start, end) if p.get("state") == "PUBLISHED"
                     and (chosen is None or (p.get("integration") or {}).get("id") in chosen)
                     and all(w in plain(p.get("content", "")).lower() for w in about)]
            return found, label

        posts, label = await find(when)
        if not posts and not when:  # "how did my last tiktok do?" -> look further back
            posts, label = await find("30 days")
        if not posts:
            return Result(f"📭 No published posts{' matching that' if about or wanted else ''} in {label}.")
        lines = await post_stats_lines(postiz, posts, now.tzinfo)
        more = f"\n(showing the newest {MAX_POST_STATS} of {len(posts)})" if len(posts) > MAX_POST_STATS else ""
        return Result(f"📊 Post stats, {label}:\n" + "\n".join(lines) + more)

    async def channel_stats(args: dict, ctx: Context) -> Result:
        period = (args.get("period") or "").lower()
        days = 30 if re.search(r"month|bulan|30", period) else 7
        channels = await postiz.channels()
        chosen = pick_channels(channels, args.get("channel") or "")
        if not chosen:
            names = ", ".join(c.label() for c in channels if not c.disabled) or "none connected"
            return Result(f"I don't have a Postiz channel called “{args.get('channel')}”. Channels: {names}")
        lines, without = await channel_stats_lines(postiz, chosen, days)
        if not lines:
            return Result(f"Postiz has no analytics for {', '.join(without)} right now.")
        note = f"\n(no analytics for {', '.join(without)})" if without else ""
        return Result(f"📈 Channels, last {days} days:\n" + "\n".join(lines) + note)

    return [
        Tool("list_posts", "List the user's Postiz social media posts (scheduled, published, drafts) in a period.",
             {"when": "period in the user's words: 'this week', 'tomorrow', 'next week'; empty for the next 7 days"},
             guard(list_posts)),
        Tool("draft_post", "Write a social media post and save it as a Postiz DRAFT (never published directly). "
             "Write the full post text yourself from what the user wants.",
             {"content": "the complete post text, ready to publish",
              "channel": "optional network or channel name, e.g. 'x', 'linkedin'; empty for all channels",
              "when": "optional planned time in the user's words, e.g. 'friday 8pm'"},
             guard(draft_post), required=("content",), execute=guard(draft_execute)),
        Tool("post_stats", "How the user's published social media posts did: likes, comments, views, shares.",
             {"when": "optional period in the user's words: 'this week', 'last week', 'this month'",
              "channel": "optional network or channel name, e.g. 'tiktok', 'x'; empty for all",
              "about": "optional words from the post's text to find one post, e.g. 'server wipe'"},
             guard(post_stats)),
        Tool("channel_stats", "Growth of the user's social media channels/accounts: followers, views, "
             "impressions over the last 7 or 30 days.",
             {"channel": "optional network or channel name, e.g. 'tiktok'; empty for all",
              "period": "'week' or 'month'; empty for a week"},
             guard(channel_stats)),
    ]
