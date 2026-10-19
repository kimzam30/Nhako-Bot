"""Reminder tool, reminder storage and the background scheduler."""
import dataclasses
import datetime as dt

from nhako import scheduler
from nhako.tools.reminders import set_reminder

from .conftest import KL, NOW  # Monday 19 Oct 2026, 07:30

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


def test_parse_agenda_time():
    assert scheduler.parse_agenda_time("08:00") == dt.time(8, 0)
    assert scheduler.parse_agenda_time("7") == dt.time(7, 0)
    assert scheduler.parse_agenda_time("off") is None
    assert scheduler.parse_agenda_time("") is None
