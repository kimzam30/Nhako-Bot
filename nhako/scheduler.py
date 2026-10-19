"""Background loop: deliver due reminders, the morning agenda and homelab alerts.

Everything it needs is in SQLite, so a restart (or a crash) loses nothing: reminders
that came due while the bot was down are sent, marked late, as soon as it is back.
"""
import asyncio
import datetime as dt
import logging
from typing import Awaitable, Callable

from .store import Store

log = logging.getLogger(__name__)

TICK_SECONDS = 20
MONITOR_EVERY_TICKS = 3  # homelab check once a minute
LATE_AFTER = dt.timedelta(minutes=5)
AGENDA_GRACE = dt.timedelta(hours=3)  # bot started at 11:30 -> skip that day's 8:00 agenda

Send = Callable[[int, str], Awaitable[None]]


async def tick(store: Store, send: Send, now: dt.datetime, *, agenda_time: dt.time | None,
               agenda_chat: int, agenda: Callable[[], Awaitable[str]]) -> None:
    for r in await store.due_reminders(now):
        late = now - r.due_at
        note = f" (late by {_ago(late)}, I was offline)" if late > LATE_AFTER else ""
        try:
            await send(r.chat_id, f"⏰ Reminder: {r.text}{note}")
        except Exception:
            log.exception("sending reminder %s failed; will retry", r.id)
            continue
        await store.mark_reminder_sent(r.id)

    if agenda_time is not None:
        due = dt.datetime.combine(now.date(), agenda_time, now.tzinfo)
        today = now.date().isoformat()
        if due <= now < due + AGENDA_GRACE and await store.get("agenda_sent") != today:
            await store.set("agenda_sent", today)  # set first: never twice, even if sending fails
            await send(agenda_chat, "☀️ Good morning!\n" + await agenda())


async def watch(monitor, send: Send, chat_id: int) -> None:
    """One homelab check; all alerts from it go out as a single message."""
    alerts = await monitor.check()
    if alerts:
        await send(chat_id, "🏠 Homelab:\n" + "\n".join(alerts))


async def run(store: Store, send: Send, now: Callable[[], dt.datetime], *, monitor=None,
              alert_chat: int = 0, **kwargs) -> None:
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
        ticks += 1
        await asyncio.sleep(TICK_SECONDS)


def _ago(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def parse_agenda_time(text: str) -> dt.time | None:
    """'08:00' -> time(8, 0); '' or 'off' disables the morning agenda."""
    if not text or text.lower() in ("off", "none", "no", "false"):
        return None
    hour, _, minute = text.partition(":")
    return dt.time(int(hour), int(minute or 0))
