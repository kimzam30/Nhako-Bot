"""Background loop: deliver due reminders, the morning agenda, daily/weekly jobs (weekly review,
overdue nudges) and homelab alerts.

Everything it needs is in SQLite, so a restart (or a crash) loses nothing: reminders
that came due while the bot was down are sent, marked late, as soon as it is back.
"""
import asyncio
import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)

TICK_SECONDS = 20
MONITOR_EVERY_TICKS = 3  # homelab check once a minute
LATE_AFTER = dt.timedelta(minutes=5)
AGENDA_GRACE = dt.timedelta(hours=3)  # bot started at 11:30 -> skip that day's 8:00 agenda
WARM_AHEAD = dt.timedelta(minutes=5)  # load the model just before the agenda, when replies are likely

Send = Callable[[int, str], Awaitable[None]]


@dataclass
class Job:
    """A message sent once a day (or once a week on `weekday`, 0 = Monday) at `at`, if `build`
    has something to say. `key` remembers the last day it ran, so a restart never repeats it."""
    key: str
    at: dt.time
    build: Callable[[], Awaitable[str | None]]
    weekday: int | None = None


@dataclass
class Watcher:
    """Polled every `every_ticks` ticks; each line `check(now)` returns goes to the alert chat."""
    every_ticks: int
    check: Callable[[dt.datetime], Awaitable[list[str]]]


async def run_job(job: Job, store: Store, send: Send, chat_id: int, now: dt.datetime) -> None:
    if job.weekday is not None and now.weekday() != job.weekday:
        return
    due = dt.datetime.combine(now.date(), job.at, now.tzinfo)
    today = now.date().isoformat()
    if not (due <= now < due + AGENDA_GRACE) or await store.get(job.key) == today:
        return
    await store.set(job.key, today)  # set first: never twice, even if building or sending fails
    try:
        text = await job.build()
    except Exception:
        log.exception("building %s failed", job.key)
        return
    if text:
        await send(chat_id, text)


async def tick(store: Store, send: Send, now: dt.datetime, *, agenda_time: dt.time | None,
               agenda_chat: int, agenda: Callable[[], Awaitable[str]],
               warm: Callable[[], Awaitable[None]] | None = None, history_keep: int = 200,
               jobs: Sequence[Job] = ()) -> None:
    for r in await store.due_reminders(now):
        late = now - r.due_at
        note = f" (late by {_ago(late)}, I was offline)" if late > LATE_AFTER else ""
        try:
            await send(r.chat_id, f"⏰ Reminder: {r.text}{note}")
        except Exception:
            log.exception("sending reminder %s failed; will retry", r.id)
            continue
        await store.mark_reminder_sent(r.id)

    today = now.date().isoformat()
    if agenda_time is not None:
        due = dt.datetime.combine(now.date(), agenda_time, now.tzinfo)
        if warm is not None and due - WARM_AHEAD <= now < due and await store.get("warmed") != today:
            await store.set("warmed", today)
            await warm()
        if due <= now < due + AGENDA_GRACE and await store.get("agenda_sent") != today:
            await store.set("agenda_sent", today)  # set first: never twice, even if sending fails
            try:
                text = await agenda()
            except Exception:  # Google/Notion down: still say good morning, and say why it's short
                log.exception("building the morning agenda failed")
                text = "⚠️ I couldn't load today's agenda. Details are in the log; try /today later."
            await send(agenda_chat, "☀️ Good morning!\n" + text)

    for job in jobs:
        try:
            await run_job(job, store, send, agenda_chat, now)
        except Exception:  # one broken job never blocks the others or the reminders
            log.exception("job %s failed", job.key)

    if await store.get("pruned") != today:
        await store.set("pruned", today)
        await store.prune(history_keep)


async def watch(monitor, send: Send, chat_id: int) -> None:
    """One homelab check; all alerts from it go out as a single message."""
    alerts = await monitor.check()
    if alerts:
        await send(chat_id, "🏠 Homelab:\n" + "\n".join(alerts))


async def poll(watchers: Sequence[Watcher], send: Send, chat_id: int, now: dt.datetime, ticks: int) -> None:
    for w in watchers:
        if ticks % w.every_ticks:
            continue
        try:
            if alerts := await w.check(now):
                await send(chat_id, "\n".join(alerts))
        except Exception as e:  # the service being down is not worth a traceback every few minutes
            log.warning("watcher %s failed: %r", getattr(w.check, "__qualname__", w.check), e)


async def run(store: Store, send: Send, now: Callable[[], dt.datetime], *, monitor=None,
              alert_chat: int = 0, heartbeat: Path | None = None, watchers: Sequence[Watcher] = (),
              **kwargs) -> None:
    ticks = 0
    while True:
        try:
            await tick(store, send, now(), **kwargs)
        except Exception:
            log.exception("scheduler tick failed")
        if monitor is not None and ticks % MONITOR_EVERY_TICKS == 0:
            try:
                await watch(monitor, send, alert_chat)
            except Exception:
                log.exception("homelab check failed")
        await poll(watchers, send, alert_chat, now(), ticks)
        if heartbeat is not None:  # the Docker healthcheck looks at this file's age
            try:
                heartbeat.touch()
            except OSError as e:
                log.warning("can't write heartbeat %s: %r", heartbeat, e)
        ticks += 1
        await asyncio.sleep(TICK_SECONDS)


def _ago(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"
