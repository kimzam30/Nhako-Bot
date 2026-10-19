"""Plain-text reports, no model involved: /tasks, /projects, stats, the morning agenda,
the weekly review and overdue nudges."""
import datetime as dt
import json
import logging

import httpx

from ...timeparse import parse_range
from .schema import Project, Task

log = logging.getLogger(__name__)

NOT_SET_UP = ("Notion isn't set up. Put NOTION_TOKEN and NOTION_TASKS_DB in .env "
              "(see .env.example) and share the database with the integration.")
NO_PROJECTS = "Your tasks database isn't linked to a projects database in Notion, so I can't see projects."
XP_PER_LEVEL = 100
SOON_DAYS = 3


def due_in(tasks: list[Task], text: str, now: dt.datetime) -> tuple[list[Task], str]:
    """Tasks due within the range the user named, overdue ones included. No range: everything open."""
    t = (text or "").lower().strip()
    if not t or t in ("all", "open", "everything", "semua"):
        return tasks, "open"
    if "overdue" in t or "lewat" in t:
        return [x for x in tasks if x.overdue(now)], "overdue"
    if "progress" in t or "doing" in t or "working on" in t:
        return [x for x in tasks if x.in_progress], "in progress"
    _, end, label = parse_range(t, now)
    return [x for x in tasks if x.due and x.due < end], f"due {label}"


def format_tasks(tasks: list[Task], label: str, now: dt.datetime) -> str:
    if not tasks:
        return f"✅ No tasks {label}." if label != "open" else "✅ No open tasks. Nice."
    head = f"📝 {len(tasks)} task{'s' * (len(tasks) != 1)} {label}:"
    more = f"\n…and {len(tasks) - 30} more" if len(tasks) > 30 else ""
    return "\n".join([head, *(t.line(now) for t in tasks[:30])]) + more


def format_projects(projects: list[Project], everything: bool = False) -> str:
    shown = projects if everything else [p for p in projects if not p.done]
    if not shown:
        return "No projects yet." if not projects else "🏆 Every project is cleared."
    hidden = len(projects) - len(shown)
    tail = f"\n(+{hidden} cleared)" if hidden else ""
    return "\n".join([f"🗺️ {len(shown)} project{'s' * (len(shown) != 1)}:", *(p.line() for p in shown)]) + tail


async def tasks_report(notion, now: dt.datetime, when: str = "") -> str:
    """Plain task list for /tasks."""
    if notion is None:
        return NOT_SET_UP
    try:
        return format_tasks(*due_in(await notion.open_tasks(now.tzinfo), when, now), now)
    except httpx.HTTPError as e:
        log.warning("notion failed: %r", e)
        return "⚠️ I couldn't read your Notion tasks."


async def projects_report(notion, which: str = "") -> str:
    """Plain project list for /projects."""
    if notion is None:
        return NOT_SET_UP
    try:
        if await notion.project_schema() is None:
            return NO_PROJECTS
        return format_projects(await notion.projects(), everything=which.strip().lower() in ("all", "semua"))
    except httpx.HTTPError as e:
        log.warning("notion failed: %r", e)
        return "⚠️ I couldn't read your Notion projects."


# --- stats ------------------------------------------------------------------------------

def period(text: str, now: dt.datetime) -> tuple[dt.datetime | None, dt.datetime, str]:
    """A past window [start, end) for stats. start None: all time. Empty text: this week."""
    t = (text or "").lower().strip()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - dt.timedelta(days=now.weekday())
    first = today.replace(day=1)
    end = now + dt.timedelta(seconds=1)
    if any(w in t for w in ("all", "ever", "total", "overall")):
        return None, end, "all time"
    if "today" in t:
        return today, end, "today"
    if "last week" in t:
        return monday - dt.timedelta(days=7), monday, "last week"
    if "last month" in t:
        prev = (first - dt.timedelta(days=1)).replace(day=1)
        return prev, first, "last month"
    if "month" in t:
        return first, end, "this month"
    if "year" in t:
        return today.replace(month=1, day=1), end, "this year"
    return monday, end, "this week"


def level(total_xp: float) -> tuple[int, float]:
    """(level, XP still needed for the next one)."""
    return 1 + int(total_xp // XP_PER_LEVEL), XP_PER_LEVEL - total_xp % XP_PER_LEVEL


async def stats_text(notion, now: dt.datetime, when: str = "") -> str:
    start, end, label = period(when, now)
    done = await notion.done_tasks(now.tzinfo)
    cleared = [t for t in done if t.cleared_on and (start is None or start <= t.cleared_on) and t.cleared_on < end]
    cleared.sort(key=lambda t: t.cleared_on)
    gained = sum(t.xp for t in cleared)
    total = sum(t.xp for t in done)
    lvl, to_next = level(total)
    n = len(cleared)
    lines = [f"🏆 {label.capitalize()}: {n} quest{'s' * (n != 1)} cleared, +{gained:g} XP"]
    lines += [f"• {t.title}" + (f"  +{t.xp:g} XP" if t.xp else "") for t in cleared[:15]]
    if n > 15:
        lines.append(f"…and {n - 15} more")
    lines.append(f"⭐ Total: {total:g} XP · Level {lvl} ({to_next:g} XP to level {lvl + 1})")
    by_project: dict[str, float] = {}
    for t in cleared:
        for pid in t.projects:
            by_project[pid] = by_project.get(pid, 0) + t.xp
    if by_project and max(by_project.values()) > 0:
        names = {p.id: p.title for p in await notion.projects()}
        pid = max(by_project, key=by_project.get)
        if pid in names:
            lines.append(f"🗺️ Most XP {label}: {names[pid]} ({by_project[pid]:g} XP)")
    return "\n".join(lines)


# --- proactive messages -----------------------------------------------------------------

def sections(tasks: list[Task], now: dt.datetime, *, soon: bool = True) -> str:
    """Overdue / due today / next few days / in progress, each task once, empty sections skipped."""
    today_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
    soon_end = today_end + dt.timedelta(days=SOON_DAYS)
    groups = [("⚠️ Overdue", lambda t: t.overdue(now)),
              ("📌 Due today", lambda t: t.due is not None and t.due < today_end)]
    if soon:
        groups.append((f"📅 Next {SOON_DAYS} days", lambda t: t.due is not None and t.due < soon_end))
    groups.append(("⚔️ In progress", lambda t: t.in_progress))
    shown: set[str] = set()
    out = []
    for title, wanted in groups:
        picked = [t for t in tasks if t.id not in shown and wanted(t)]
        shown.update(t.id for t in picked)
        if picked:
            out.append(f"{title}:\n" + "\n".join(t.line(now) for t in picked[:12]))
    return "\n\n".join(out)


async def morning_tasks(notion, now: dt.datetime) -> str:
    """The Notion part of the morning agenda; empty when there's nothing to say."""
    return sections(await notion.open_tasks(now.tzinfo), now)


async def weekly_review(notion, now: dt.datetime) -> str:
    parts = ["📊 Weekly review", await stats_text(notion, now, "this week")]
    if await notion.project_schema() is not None:
        parts.append(format_projects(await notion.projects()))
    if left := sections(await notion.open_tasks(now.tzinfo), now, soon=False):
        parts.append(left)
    return "\n\n".join(parts)


def nudge_key(t: Task) -> str:
    """Moving the due date makes it a new overdue: worth one more nudge."""
    return f"{t.id}@{t.due.isoformat() if t.due else ''}"


async def overdue_nudge(notion, now: dt.datetime, store) -> str | None:
    """Tasks that went overdue since the last nudge. The store remembers only what's overdue now,
    so finished or rescheduled tasks drop out by themselves."""
    overdue = [t for t in await notion.open_tasks(now.tzinfo) if t.overdue(now)]
    seen = set(json.loads(await store.get("nudged") or "[]"))
    new = [t for t in overdue if nudge_key(t) not in seen]
    await store.set("nudged", json.dumps(sorted(nudge_key(t) for t in overdue)))
    if not new:
        return None
    return "⏰ Just went overdue:\n" + "\n".join(t.line(now) for t in new[:12])
