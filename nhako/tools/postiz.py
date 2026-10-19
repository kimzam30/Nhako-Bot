"""Postiz (self-hosted social media scheduler): what's scheduled, and drafting posts.

Uses Postiz's public API (Settings → Public API → API key). The key can do anything
in the organisation, so the bot only ever lists posts and creates DRAFTS: a draft never
publishes until you schedule it yourself in Postiz.
"""
import datetime as dt
import html
import logging
import re
from dataclasses import dataclass

import httpx

from ..timeparse import parse_range, parse_when
from . import Context, NeedsConfirm, Result, Tool

log = logging.getLogger(__name__)

NOT_SET_UP = "Postiz isn't set up. Put POSTIZ_URL and POSTIZ_API_KEY in .env (see .env.example)."
STATE_ICON = {"QUEUE": "⏳", "PUBLISHED": "✅", "ERROR": "❌", "DRAFT": "📝"}
# What people call a network -> Postiz provider identifiers it covers.
ALIASES = {"twitter": "x", "tweet": "x", "ig": "instagram", "insta": "instagram", "fb": "facebook",
           "yt": "youtube", "linked": "linkedin", "bsky": "bluesky"}
DRAFT_HOUR = 9  # "draft a post for friday" with no time


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


class Postiz:
    def __init__(self, base_url: str, api_key: str, transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/public/v1", transport=transport,
                                        timeout=20, headers={"Authorization": api_key})

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


def post_line(post: dict, tz) -> str:
    at = dt.datetime.fromisoformat(post["publishDate"].replace("Z", "+00:00")).astimezone(tz)
    who = (post.get("integration") or {}).get("name") or (post.get("integration") or {}).get("providerIdentifier", "?")
    return f"{STATE_ICON.get(post.get('state'), '•')} {at:%a %d %b %H:%M} · {who}: {plain(post.get('content', ''), 90)}"


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
    ]
