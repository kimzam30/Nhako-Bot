"""Reminder tool, reminder storage and the background scheduler."""
import dataclasses
import datetime as dt

import pytest
from pydantic import ValidationError

from nhako import scheduler
from nhako.config import Settings, parse_agenda_time
from nhako.tools.reminders import set_reminder

from .conftest import KL  # Monday 19 Oct 2026, 07:30

CHAT = 7


def at(day, hour, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=KL)


async def test_set_reminder_stores_it(ctx, store):
    c = dataclasses.replace(ctx, store=store, chat_id=CHAT)
    out = await set_reminder({"what": "take the laundry out", "when": "in 20 minutes"}, c)
    assert "Mon 19 Oct at 07:50" in out.text
    [r] = await store.upcoming_reminders(CHAT)
    assert r.text == "take the laundry out" and r.due_at == at(19, 7, 50)


async def test_reminder_without_time_defaults_to_9am(ctx, store):
    c = dataclasses.replace(ctx, store=store, chat_id=CHAT)
    await set_reminder({"what": "pay bills", "when": "friday"}, c)
    [r] = await store.upcoming_reminders(CHAT)
    assert r.due_at == at(23, 9)


async def test_reminder_in_the_past_is_refused(ctx, store):
    c = dataclasses.replace(ctx, store=store, chat_id=CHAT)
    out = await set_reminder({"what": "x", "when": "today 7am"}, c)
    assert "already passed" in out.text and await store.upcoming_reminders(CHAT) == []


async def test_cancel_reminder_only_in_own_chat(store):
    rid = await store.add_reminder(CHAT, "stretch", at(19, 9))
    assert await store.cancel_reminder(rid, CHAT + 1) is None
    assert (await store.cancel_reminder(rid, CHAT)).text == "stretch"
    assert await store.upcoming_reminders(CHAT) == []


class Outbox:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


async def no_agenda():
    return "unused"


async def tick(store, out, now, **kwargs):
    kwargs.setdefault("agenda_time", None)
    kwargs.setdefault("agenda_chat", CHAT)
    kwargs.setdefault("agenda", no_agenda)
    await scheduler.tick(store, out, now, **kwargs)


async def test_due_reminder_is_sent_once(store):
    await store.add_reminder(CHAT, "stretch", at(19, 9))
    out = Outbox()
    await tick(store, out, at(19, 8, 59))
    assert out.sent == []
    await tick(store, out, at(19, 9, 0))
    await tick(store, out, at(19, 9, 1))
    assert out.sent == [(CHAT, "⏰ Reminder: stretch")]


async def test_reminder_missed_while_offline_is_sent_late(store):
    await store.add_reminder(CHAT, "stretch", at(19, 9))
    out = Outbox()
    await tick(store, out, at(19, 11, 30))
    assert out.sent == [(CHAT, "⏰ Reminder: stretch (late by 2 h, I was offline)")]


async def test_failed_send_is_retried(store):
    await store.add_reminder(CHAT, "stretch", at(19, 9))

    async def broken(chat_id, text):
        raise RuntimeError("telegram down")
    await tick(store, broken, at(19, 9))
    out = Outbox()
    await tick(store, out, at(19, 9, 1))
    assert len(out.sent) == 1


async def test_morning_agenda_once_a_day(store):
    out = Outbox()

    async def agenda():
        return "📅 Today: nothing"
    kwargs = dict(agenda_time=dt.time(8, 0), agenda=agenda)
    await tick(store, out, at(19, 7, 59), **kwargs)
    await tick(store, out, at(19, 8, 0), **kwargs)
    await tick(store, out, at(19, 8, 30), **kwargs)
    assert out.sent == [(CHAT, "☀️ Good morning!\n📅 Today: nothing")]
    await tick(store, out, at(20, 8, 0), **kwargs)  # next day: again
    assert len(out.sent) == 2


async def test_agenda_skipped_when_bot_starts_late(store):
    out = Outbox()
    await tick(store, out, at(19, 11, 30), agenda_time=dt.time(8, 0))
    assert out.sent == []


async def test_failed_agenda_still_says_good_morning(store):
    out = Outbox()

    async def broken():
        raise TimeoutError("google down")
    await tick(store, out, at(19, 8, 0), agenda_time=dt.time(8, 0), agenda=broken)
    assert len(out.sent) == 1 and "couldn't load today's agenda" in out.sent[0][1]


async def test_model_is_warmed_once_before_the_agenda(store):
    warmed = []

    async def warm():
        warmed.append(1)
    for minute in (54, 56, 57):
        await tick(store, Outbox(), at(19, 7, minute), agenda_time=dt.time(8, 0), warm=warm)
    assert warmed == [1]


async def test_old_history_is_pruned_daily(store):
    for i in range(10):
        await store.add_message(CHAT, "user", f"m{i}")
    await tick(store, Outbox(), at(19, 9), history_keep=3)
    assert [m["content"] for m in await store.history(CHAT, 50)] == ["m7", "m8", "m9"]


def test_parse_agenda_time():
    assert parse_agenda_time("08:00") == dt.time(8, 0)
    assert parse_agenda_time("7") == dt.time(7, 0)
    assert parse_agenda_time("off") is None
    assert parse_agenda_time("") is None


@pytest.mark.parametrize("bad", ["8am", "8.30", "25:00"])
def test_bad_agenda_time_is_a_clear_config_error(bad):
    with pytest.raises(ValidationError, match="AGENDA_TIME"):
        Settings(_env_file=None, agenda_time=bad)


# --- daily / weekly jobs (weekly review, overdue nudges) ---

def job(key="review", hour=20, weekday=None, text="📊 review", log=None):
    async def build():
        if log is not None:
            log.append(key)
        if isinstance(text, Exception):
            raise text
        return text
    return scheduler.Job(key, dt.time(hour, 0), build, weekday)


async def test_weekly_job_only_on_its_day_and_once(store):
    out = Outbox()
    jobs = [job(weekday=6)]  # Sunday; the 19th is a Monday
    await tick(store, out, at(19, 20, 0), jobs=jobs)
    assert out.sent == []
    await tick(store, out, at(25, 19, 59), jobs=jobs)
    await tick(store, out, at(25, 20, 0), jobs=jobs)
    await tick(store, out, at(25, 20, 30), jobs=jobs)
    assert out.sent == [(CHAT, "📊 review")]


async def test_daily_job_with_nothing_to_say_sends_nothing_and_isnt_retried(store):
    out, ran = Outbox(), []
    jobs = [job("nudge", 18, text=None, log=ran)]
    await tick(store, out, at(19, 18, 0), jobs=jobs)
    await tick(store, out, at(19, 18, 1), jobs=jobs)
    assert out.sent == [] and ran == ["nudge"]
    await tick(store, out, at(20, 18, 0), jobs=jobs)  # next day: runs again
    assert ran == ["nudge", "nudge"]


async def test_job_skipped_when_bot_starts_late(store):
    out = Outbox()
    await tick(store, out, at(19, 23, 30), jobs=[job(hour=18)])
    assert out.sent == []


async def test_broken_job_doesnt_stop_the_others(store):
    out = Outbox()
    jobs = [job("broken", 18, text=RuntimeError("notion down")), job("fine", 18, text="ok")]
    await tick(store, out, at(19, 18, 0), jobs=jobs)
    assert out.sent == [(CHAT, "ok")]


def test_parse_weekly():
    from nhako.config import parse_weekly
    assert parse_weekly("sun 20:00") == (6, dt.time(20, 0))
    assert parse_weekly("Friday 9") == (4, dt.time(9, 0))
    assert parse_weekly("off") is None
    s = Settings(_env_file=None)
    assert s.weekly_review_at == (6, dt.time(20, 0)) and s.overdue_nudge_at == dt.time(18, 0)


@pytest.mark.parametrize("field, bad, name", [
    ("weekly_review", "sunday", "WEEKLY_REVIEW"), ("weekly_review", "someday 20:00", "WEEKLY_REVIEW"),
    ("overdue_nudge_time", "6pm", "OVERDUE_NUDGE_TIME"),
])
def test_bad_job_times_are_clear_config_errors(field, bad, name):
    with pytest.raises(ValidationError, match=name):
        Settings(_env_file=None, **{field: bad})
