"""Calendar tools against an in-memory fake of the Google API."""
import datetime as dt
import itertools

import pytest

from nhako.tools import NeedsConfirm, Result
from nhako.tools import calendar as cal_mod
from nhako.tools.calendar import CalendarAuthError, agenda, find_events, split_target

from .conftest import KL, NOW  # Monday 19 Oct 2026, 07:30


def timed(title, day, hour, minutes=60):
    start = dt.datetime(2026, 10, day, hour, tzinfo=KL)
    return {"summary": title, "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + dt.timedelta(minutes=minutes)).isoformat()}}


def all_day(title, day):
    d = dt.date(2026, 10, day)
    return {"summary": title, "start": {"date": d.isoformat()}, "end": {"date": (d + dt.timedelta(days=1)).isoformat()}}


class FakeCalendar:
    def __init__(self, *events):
        ids = itertools.count(1)
        self.events = {str(next(ids)): {**e, "id": None} for e in events}
        for k, e in self.events.items():
            e["id"] = k
        self.deleted, self.patched, self.inserted = [], [], []
        self.auth_error = False

    def _start(self, e):
        return cal_mod.event_start(e, KL)[0]

    async def list(self, start, end):
        if self.auth_error:
            raise CalendarAuthError(cal_mod.NO_TOKEN)
        return sorted((e for e in self.events.values() if start <= self._start(e) < end), key=self._start)

    async def insert(self, body):
        self.inserted.append(body)
        return {**body, "id": "new", "htmlLink": "https://calendar/new"}

    async def patch(self, event_id, body):
        self.patched.append((event_id, body))
        return {**self.events[event_id], **body}

    async def delete(self, event_id):
        self.deleted.append(event_id)


@pytest.fixture
def cal():
    return FakeCalendar(
        timed("Dentist", 21, 15),              # id 1, Wed
        timed("Team meeting", 20, 10),         # id 2, Tue (tomorrow)
        all_day("Futsal with the boys", 24),   # id 3, Sat
        timed("Lunch with Aiman", 23, 13),     # id 4, Fri
        timed("Lunch with Mum", 25, 13),       # id 5, Sun
        timed("Call bank", 25, 9),             # id 6, Sun
    )


@pytest.fixture
def tools(ctx, cal):
    return {t.name: t for t in cal_mod.tools(ctx, cal)}


def test_split_target():
    assert split_target("tomorrow's meeting") == ("tomorrow", ["meeting"])
    assert split_target("the dentist thing") == ("", ["dentist"])
    assert split_target("next friday's lunch with Aiman") == ("next friday", ["lunch", "aiman"])


async def test_find_by_vague_name(cal):
    events, wants_all, _ = await find_events(cal, "the dentist thing", NOW)
    assert [e["id"] for e in events] == ["1"] and not wants_all


async def test_find_uses_the_day_in_the_phrase(cal):
    events, _, _ = await find_events(cal, "sunday's lunch", NOW)
    assert [e["id"] for e in events] == ["5"]


# --- delete ---

async def test_delete_one_asks_first(tools, cal, ctx):
    out = await tools["delete_event"].run({"event": "the dentist thing"}, ctx)
    assert isinstance(out, NeedsConfirm) and "Dentist" in out.prompt
    assert cal.deleted == []
    await tools["delete_event"].execute(out.args, ctx)
    assert cal.deleted == ["1"]


async def test_delete_ambiguous_asks_which_one(tools, cal, ctx):
    out = await tools["delete_event"].run({"event": "lunch"}, ctx)
    assert isinstance(out, Result) and "Which one" in out.text
    assert "Aiman" in out.text and "Mum" in out.text and cal.deleted == []


async def test_clear_a_whole_day(tools, ctx):
    out = await tools["delete_event"].run({"event": "everything", "date": "sunday"}, ctx)
    assert isinstance(out, NeedsConfirm) and sorted(out.args["ids"]) == ["5", "6"]


async def test_delete_nothing_found(tools, ctx):
    out = await tools["delete_event"].run({"event": "haircut"}, ctx)
    assert isinstance(out, Result) and "couldn't find" in out.text


# --- update ---

async def test_move_to_a_time_keeps_the_day(tools, cal, ctx):
    out = await tools["update_event"].run({"event": "dentist", "new_when": "5pm"}, ctx)
    assert isinstance(out, NeedsConfirm)
    assert "Wed 21 Oct 15:00-16:00" in out.prompt and "Wed 21 Oct 17:00-18:00" in out.prompt
    await tools["update_event"].execute(out.args, ctx)
    event_id, body = cal.patched[0]
    assert event_id == "1" and body["start"]["dateTime"].startswith("2026-10-21T17:00")


async def test_move_to_a_day_keeps_time_and_length(tools, ctx):
    out = await tools["update_event"].run({"event": "tomorrow's meeting", "new_when": "thursday"}, ctx)
    assert "Thu 22 Oct 10:00-11:00" in out.prompt


async def test_move_all_day_event_to_a_time(tools, ctx):
    out = await tools["update_event"].run({"event": "futsal", "new_when": "saturday 8pm"}, ctx)
    assert "Sat 24 Oct 20:00-21:00" in out.prompt


async def test_rename(tools, cal, ctx):
    out = await tools["update_event"].run({"event": "tomorrow's meeting", "new_title": "Postiz sync"}, ctx)
    assert "→ Tue 20 Oct 10:00-11:00  Postiz sync" in out.prompt
    assert out.args["body"] == {"summary": "Postiz sync"}


async def test_update_ambiguous_asks_which_one(tools, ctx):
    out = await tools["update_event"].run({"event": "lunch", "new_when": "2pm"}, ctx)
    assert isinstance(out, Result) and "Which one" in out.text


async def test_update_needs_a_change(tools, ctx):
    out = await tools["update_event"].run({"event": "dentist"}, ctx)
    assert isinstance(out, Result) and "What should I change" in out.text


# --- create / list ---

async def test_create_timed_event(tools, cal, ctx):
    out = await tools["create_event"].run({"title": "Dentist", "when": "next tuesday 3pm", "duration": "30 min"}, ctx)
    body = cal.inserted[0]
    assert body["start"]["dateTime"] == "2026-10-27T15:00:00+08:00"
    assert body["end"]["dateTime"] == "2026-10-27T15:30:00+08:00"
    assert "Tue 27 Oct 15:00-15:30" in out.text


async def test_create_all_day_event(tools, cal, ctx):
    await tools["create_event"].run({"title": "Holiday", "when": "friday"}, ctx)
    assert cal.inserted[0]["start"] == {"date": "2026-10-23"}


async def test_create_with_unknown_time(tools, cal, ctx):
    out = await tools["create_event"].run({"title": "x", "when": "whenever"}, ctx)
    assert "couldn't work out" in out.text and cal.inserted == []


async def test_list_is_summarized_by_the_model(tools, ctx):
    out = await tools["list_events"].run({"range": "this weekend"}, ctx)
    assert out.summarize and "Futsal" in out.text and "Lunch with Mum" in out.text


async def test_missing_token_gives_setup_hint(tools, cal, ctx):
    cal.auth_error = True
    out = await tools["list_events"].run({"range": "today"}, ctx)
    assert "google_auth.py" in out.text


# --- agenda (no model) ---

async def test_agenda_lists_the_day(cal, ctx):
    text = await agenda(cal, ctx, "tomorrow")
    assert text == "📅 Tomorrow:\n• Tue 20 Oct 10:00-11:00  Team meeting"


async def test_agenda_empty_day(cal, ctx):
    assert "Nothing on your calendar for today" in await agenda(cal, ctx, "today")
