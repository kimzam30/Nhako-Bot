"""Google Calendar: list, create, edit and delete events, plus the daily agenda.

Auth: run `python scripts/google_auth.py` once; it writes data/token.json.
"""
import asyncio
import contextlib
import datetime as dt
import logging
import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from ..timeparse import DATE_WORDS, has_date, parse_duration, parse_range, parse_when, pick_when
from . import Context, NeedsConfirm, Result, Tool

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
NO_TOKEN = ("My Google Calendar access isn't set up. "
            "Run `python scripts/google_auth.py` on the host, then try again.")

# Words that say nothing about which event is meant: "remove the dentist thing".
FILLER = {"the", "my", "a", "an", "thing", "things", "event", "events", "stuff", "that", "this", "one",
          "it", "on", "at", "for", "with", "in", "of", "and", "acara", "yang", "tu", "itu", "ni", "ini"}
EVERYTHING = {"all", "everything", "every", "semua", "everything's"}
MATCH_THRESHOLD = 0.5


class CalendarAuthError(Exception):
    pass


class GoogleCalendar:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._service = None
        self._creds: Credentials | None = None
        # googleapiclient's http (httplib2) isn't thread-safe: one call at a time.
        self._lock = asyncio.Lock()

    def service(self):
        """Blocking - call through asyncio.to_thread."""
        path = self.ctx.settings.google_token_path
        if self._creds is None:
            if not path.exists():
                raise CalendarAuthError(NO_TOKEN)
            self._creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        if not self._creds.valid:
            if not (self._creds.expired and self._creds.refresh_token):
                raise CalendarAuthError(NO_TOKEN)
            self._creds.refresh(Request())
            path.write_text(self._creds.to_json(), encoding="utf-8")  # keep the refreshed token
            with contextlib.suppress(OSError):  # owner-only; a no-op on Windows
                os.chmod(path, 0o600)
            self._service = None
        if self._service is None:
            self._service = build("calendar", "v3", credentials=self._creds, cache_discovery=False)
        return self._service

    async def _call(self, fn):
        async with self._lock:
            return await asyncio.to_thread(lambda: fn(self.service()))

    async def list(self, start: dt.datetime, end: dt.datetime) -> list[dict]:
        resp = await self._call(lambda s: s.events().list(
            calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=250).execute())
        return resp.get("items", [])

    async def insert(self, body: dict) -> dict:
        return await self._call(lambda s: s.events().insert(calendarId="primary", body=body).execute())

    async def patch(self, event_id: str, body: dict) -> dict:
        return await self._call(lambda s: s.events().patch(
            calendarId="primary", eventId=event_id, body=body).execute())

    async def delete(self, event_id: str) -> None:
        await self._call(lambda s: s.events().delete(calendarId="primary", eventId=event_id).execute())


# --- event helpers --------------------------------------------------------------------

def event_start(event: dict, tz) -> tuple[dt.datetime, bool]:
    """(start, has_time). All-day events start at midnight local time."""
    s = event["start"]
    if "dateTime" in s:
        return dt.datetime.fromisoformat(s["dateTime"]).astimezone(tz), True
    return dt.datetime.combine(dt.date.fromisoformat(s["date"]), dt.time(), tz), False


def event_end(event: dict, tz) -> dt.datetime:
    e = event["end"]
    if "dateTime" in e:
        return dt.datetime.fromisoformat(e["dateTime"]).astimezone(tz)
    return dt.datetime.combine(dt.date.fromisoformat(e["date"]), dt.time(), tz)


def describe(event: dict, tz) -> str:
    """'Tue 21 Oct 15:00-16:00  Dentist' or 'Sat 25 Oct (all day)  Futsal'."""
    title = event.get("summary", "(no title)")
    start, has_time = event_start(event, tz)
    if has_time:
        return f"{start:%a %d %b %H:%M}-{event_end(event, tz):%H:%M}  {title}"
    return f"{start:%a %d %b} (all day)  {title}"


def time_body(start: dt.datetime, end: dt.datetime | None, timezone: str) -> dict:
    """start/end fields for the API. end=None means an all-day event on start's date."""
    if end is None:
        day = start.date()
        return {"start": {"date": day.isoformat()}, "end": {"date": (day + dt.timedelta(days=1)).isoformat()}}
    return {"start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone}}


def split_target(phrase: str) -> tuple[str, list[str]]:
    """'tomorrow's dentist thing' -> ('tomorrow', ['dentist']): the when-part and the name words."""
    p = re.sub(r"['’]s\b", "", (phrase or "").lower())
    pattern = rf"\b(?:(?:this|next)\s+)?(?:{DATE_WORDS})\b"
    dates = " ".join(m.group(0) for m in re.finditer(pattern, p))
    rest = re.sub(pattern, " ", p)
    words = [w for w in re.findall(r"[\w-]+", rest) if w not in FILLER]
    return dates, words


def match_score(title: str, words: list[str]) -> float:
    """Share of the name words that appear in the title (plurals forgiven)."""
    if not words:
        return 1.0
    t = title.lower()
    return sum(1 for w in words if w in t or (len(w) > 3 and w.rstrip("s") in t)) / len(words)


async def find_events(cal, phrase: str, now: dt.datetime, date_text: str = "") -> tuple[list[dict], bool, str]:
    """Events matching a vague description, best first.
    Returns (events, wants_all, label). wants_all: the user said 'all'/'everything'."""
    dates, words = split_target(phrase)
    wants_all = any(w in EVERYTHING for w in words)
    words = [w for w in words if w not in EVERYTHING]
    when = " ".join(x for x in (date_text, dates) if x).strip()
    if when:
        start, end, label = event_range(when, now)
    else:
        start, end, label = now - dt.timedelta(days=1), now + dt.timedelta(days=90), "the next 90 days"
    events = await cal.list(start, end)
    scored = [(match_score(e.get("summary", ""), words), e) for e in events]
    scored = [(s, e) for s, e in scored if s >= MATCH_THRESHOLD]
    best = max((s for s, _ in scored), default=0)
    # Keep only the best-scoring events: 'dentist' shouldn't pull in 'dentist reminder call' twice over.
    matches = [e for s, e in scored if s == best] if not wants_all else [e for _, e in scored]
    name = " ".join(words)
    label = f"{label}" + (f" matching “{name}”" if name else "")
    return matches, wants_all or (not words and bool(when)), label


def pick_new_time(new_when: str, event: dict, now: dt.datetime, tz) -> tuple[dt.datetime, dt.datetime | None] | None:
    """New (start, end) for an event. 'to 5pm' keeps the event's day; 'to friday' keeps its time;
    the duration stays the same. end=None keeps it all-day."""
    old_start, old_has_time = event_start(event, tz)
    old_len = event_end(event, tz) - old_start if old_has_time else dt.timedelta(hours=1)
    when = parse_when(new_when, now)
    if when is None:
        return None
    if not has_date(new_when):  # only a time: same day as before
        start = old_start.replace(hour=when.at.hour, minute=when.at.minute)
    elif not when.has_time:  # only a day: same time as before
        if not old_has_time:
            return when.at, None
        start = when.at.replace(hour=old_start.hour, minute=old_start.minute)
    else:
        start = when.at
    return start, start + old_len


# --- tools ----------------------------------------------------------------------------

def event_range(text: str | None, now: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """parse_range, except "today" means the whole day: "what do I have today?" at 14:00 still
    shows the morning's events."""
    start, end, label = parse_range(text, now)
    if label == "today":
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end, label


async def agenda(cal, ctx: Context, range_text: str = "today") -> str:
    """Plain list of events, no model involved: used by /today, list_events and the morning message."""
    start, end, label = event_range(range_text, ctx.now())
    try:
        events = await cal.list(start, end)
    except CalendarAuthError as e:
        return str(e)
    if not events:
        return f"Nothing on your calendar for {label}. 🎉"
    return f"📅 {label.capitalize()}:\n" + "\n".join(f"• {describe(e, ctx.settings.tz)}" for e in events)


def which_one(events: list[dict], tz, verb: str) -> Result:
    lines = "\n".join(f"• {describe(e, tz)}" for e in events[:8])
    return Result(f"I found {len(events)} events like that. Which one should I {verb}? "
                  f"Add the day or more of the name.\n{lines}")


def tools(ctx: Context, cal=None) -> list[Tool]:
    cal = cal or GoogleCalendar(ctx)
    tz = ctx.settings.tz

    async def guarded(coro):
        try:
            return await coro
        except CalendarAuthError as e:
            return Result(str(e))

    async def list_events(args: dict, ctx: Context) -> Result:
        # Sent as-is, like /today: a model rewrite cost another 2-5 s and could garble the events.
        return Result(await agenda(cal, ctx, args.get("range") or ""))

    async def create_event(args: dict, ctx: Context) -> Result:
        title = (args.get("title") or "").strip()
        when = parse_when(pick_when(args.get("when") or "", ctx.text, title, args.get("duration") or ""), ctx.now())
        if not title:
            return Result("What should I call the event?")
        if when is None:
            return Result(f"I couldn't work out when “{args.get('when')}” is. "
                          "Try something like “tomorrow 3pm” or “21 Oct 9am”.")
        end = when.at + parse_duration(args.get("duration")) if when.has_time else None
        event = await cal.insert({"summary": title, **time_body(when.at, end, ctx.settings.timezone)})
        # The parsed time is echoed back so a wrong guess is obvious straight away.
        return Result(f"📅 Added: {describe(event, tz)}\n{event.get('htmlLink', '')}".strip())

    async def update_event(args: dict, ctx: Context) -> Result | NeedsConfirm:
        new_when, new_title = (args.get("new_when") or "").strip(), (args.get("new_title") or "").strip()
        if not (new_when or new_title):
            return Result("What should I change: the time or the name?")
        matches, _, label = await find_events(cal, args.get("event") or "", ctx.now())
        if not matches:
            return Result(f"I couldn't find any events {label}.")
        if len(matches) > 1:
            return which_one(matches, tz, "change")
        event = matches[0]
        body = {}
        if new_title:
            body["summary"] = new_title
        if new_when:
            times = pick_new_time(new_when, event, ctx.now(), tz)
            if times is None:
                return Result(f"I couldn't work out when “{new_when}” is.")
            body.update(time_body(*times, ctx.settings.timezone))
        after = {**event, **body}
        return NeedsConfirm(f"Change this event?\n• {describe(event, tz)}\n→ {describe(after, tz)}",
                            {"id": event["id"], "body": body})

    async def do_update(args: dict, ctx: Context) -> Result:
        event = await cal.patch(args["id"], args["body"])
        return Result(f"✏️ Updated: {describe(event, tz)}")

    async def delete_event(args: dict, ctx: Context) -> Result | NeedsConfirm:
        phrase, day = (args.get("event") or "").strip(), (args.get("date") or "").strip()
        if not (phrase or day):
            return Result("Which event should I delete? Give me its name or a day.")
        matches, wants_all, label = await find_events(cal, phrase, ctx.now(), day)
        if not matches:
            return Result(f"I couldn't find any events {label}.")
        if len(matches) > 1 and not wants_all:
            return which_one(matches, tz, "delete")
        matches = matches[:20]
        lines = "\n".join(f"• {describe(e, tz)}" for e in matches)
        noun = "this event" if len(matches) == 1 else f"these {len(matches)} events"
        return NeedsConfirm(f"Delete {noun}?\n{lines}", {"ids": [e["id"] for e in matches], "lines": lines})

    async def do_delete(args: dict, ctx: Context) -> Result:
        for event_id in args["ids"]:
            await cal.delete(event_id)
        return Result(f"🗑️ Deleted:\n{args['lines']}")

    return [
        Tool("list_events", "List calendar events in a time range ('what do I have', 'am I free').",
             {"range": "time range in the user's words, e.g. 'today', 'next week', 'friday afternoon'"},
             lambda a, c: guarded(list_events(a, c)), required=("range",)),
        Tool("create_event", "Put an event or appointment on the calendar.",
             {"title": "event title", "when": "start time in the user's words, e.g. 'next tuesday 3pm'",
              "duration": "optional length, e.g. '2 hours'"},
             lambda a, c: guarded(create_event(a, c)), required=("title", "when")),
        Tool("update_event", "Move an existing event to a new time, or rename it.",
             {"event": "which event, in the user's words", "new_when": "new time in the user's words, if moving",
              "new_title": "new name, if renaming"},
             lambda a, c: guarded(update_event(a, c)), required=("event",),
             execute=lambda a, c: guarded(do_update(a, c))),
        Tool("delete_event", "Delete or cancel an event, or every event on a day.",
             {"event": "which event, in the user's words", "date": "a whole day to clear, in the user's words"},
             lambda a, c: guarded(delete_event(a, c)),
             execute=lambda a, c: guarded(do_delete(a, c))),
    ]
