"""Quest / task tools: list, add, finish, update, remove."""
import datetime as dt
import logging
import re

import httpx

from ...timeparse import parse_when
from .. import Context, NeedsConfirm, Result, Tool
from ..calendar import FILLER, MATCH_THRESHOLD, match_score
from .client import Notion
from .reports import NOT_SET_UP, due_in, format_tasks
from .schema import Task

log = logging.getLogger(__name__)

TASK_WORDS = FILLER | {"task", "tasks", "todo", "to-do", "mark", "as", "done", "complete", "completed", "finished",
                       "finish", "tugas", "i", "i've", "ive", "just", "already", "with", "have", "saya", "aku",
                       "dah", "sudah", "siap", "selesai", "quest", "quests", "project", "projects", "campaign",
                       "progress", "to", "page", "notion", "what's", "whats", "left", "how", "far", "along", "is",
                       "are", "what", "show", "me", "in"}
# "I finished the invoice", "dah siap invoice tu": a task marked done, said casually.
DONE = re.compile(r"\b(finish|finished|complete|completed|done|selesai|siap)\b", re.I)
NO_DUE = ("none", "no due", "no date", "clear", "remove", "nothing")
ADD_WORDS = re.compile(r"\b(add|new|create|put|another|tambah|letak)\b", re.I)


def task_words(phrase: str) -> list[str]:
    return [w for w in re.findall(r"[\w-]+", (phrase or "").lower()) if w not in TASK_WORDS]


def scored_matches(items: list, words: list[str]) -> tuple[float, list]:
    """(best score, the items with it), if any match well enough."""
    scored = [(match_score(t.title, words), t) for t in items]
    best = max((sc for sc, _ in scored), default=0)
    return best, [t for sc, t in scored if sc >= MATCH_THRESHOLD and sc == best]


def best_matches(items: list, words: list[str]) -> list:
    """The items whose titles match the words best, if any match well enough."""
    return scored_matches(items, words)[1]


def guarded(notion: Notion | None):
    """Wrap a tool so a missing setup or a Notion error becomes a plain answer."""
    def guard(fn):
        async def run(args: dict, ctx: Context):
            if notion is None:
                return Result(NOT_SET_UP)
            try:
                return await fn(args, ctx)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403, 404):
                    return Result("Notion refused that. Check NOTION_TOKEN, and that the database is "
                                  "shared with the integration (••• → Connections).")
                raise
            except httpx.HTTPError as e:
                log.warning("notion unreachable: %r", e)
                return Result("I couldn't reach Notion just now. Try again in a bit.")
        return run
    return guard


def guess(text: str) -> tuple[str, dict] | None:
    """No tool call from the model for a task message: 'I finished X' means complete_task.
    Anything else is left to the model (None)."""
    return ("complete_task", {"task": text}) if DONE.search(text) else None


def rescue(notion: Notion | None):
    """For a message the router sent to chat: 'I finished the passport renewal' is a task
    completion only if an open task really matches, so 'I finished my workout, any tips?'
    stays a chat."""
    async def check(text: str, ctx: Context) -> str | None:
        if notion is None or not DONE.search(text) or not (words := task_words(text)):
            return None
        try:
            tasks = await notion.open_tasks(ctx.now().tzinfo)
        except httpx.HTTPError as e:
            log.warning("notion unreachable while checking %r: %r", text[:60], e)
            return None
        return "notion" if best_matches(tasks, words) else None
    return check


def parse_due(text: str, now: dt.datetime) -> tuple[str, str] | str:
    """(ISO date for Notion, how to show it), ("", "no due date") to clear, or the error to show."""
    t = text.lower().strip()
    if t in NO_DUE or t.startswith("no "):
        return "", "no due date"
    when = parse_when(text, now)
    if when is None:
        return f"I couldn't work out when “{text}” is. Try “friday” or “tomorrow 5pm”."
    if when.has_time:
        return when.at.isoformat(), f"due {when.at:%a %d %b %H:%M}"
    return when.at.date().isoformat(), f"due {when.at:%a %d %b}"


def tools(ctx: Context, notion: Notion | None = None) -> list[Tool]:
    s = ctx.settings
    notion = notion or (Notion(s.notion_token, s.notion_tasks_db) if s.notion_token and s.notion_tasks_db else None)
    guard = guarded(notion)

    async def find_task(phrase: str, ctx: Context, ask: str, include_done: bool = False) -> Task | Result:
        """One task matching the user's words (open ones first), or the Result to send instead."""
        words = task_words(phrase)
        if not words:
            return Result(ask)
        tz = ctx.now().tzinfo
        score, matches = scored_matches(await notion.open_tasks(tz), words)
        if include_done and score < 1:  # "login bug": the done "Fix login bug" beats the open "Bug Fixes"
            done_score, done = scored_matches(await notion.done_tasks(tz), words)
            if done and done_score > score:
                matches = done
        if not matches:
            return Result(f"I can't find {'a' if include_done else 'an open'} task matching “{' '.join(words)}”.")
        if len(matches) > 1:
            listing = "\n".join(t.line(ctx.now()) for t in matches[:8])
            return Result(f"Which one?\n{listing}")
        return matches[0]

    async def list_tasks(args: dict, ctx: Context) -> Result:
        now = ctx.now()
        return Result(format_tasks(*due_in(await notion.open_tasks(now.tzinfo), args.get("when") or "", now), now))

    async def existing_instead(title: str, args: dict, ctx: Context) -> Result | NeedsConfirm | None:
        """Seen live: "push the bug fixes quest to friday" -> add_task(title="bug fixes", due="friday"),
        which would make a duplicate. When the title is an open task and the user never said add/new,
        treat it as a change to that task (or say it exists)."""
        if ADD_WORDS.search(ctx.text or ""):
            return None
        words = task_words(title)
        matches = [t for t in await notion.open_tasks(ctx.now().tzinfo)  # whole words: "x" isn't "Fixes"
                   if words and all(re.search(rf"\b{re.escape(w)}s?\b", t.title.lower()) for w in words)]
        if len(matches) != 1:
            return None
        t = matches[0]
        _, schema = await notion.schema()
        change = {"task": t.title, "due": args.get("due") or ""}
        project = (args.get("project") or "").strip()
        if project and schema.tag_for(project):  # "make the bug fixes quest a boss quest"
            change["tag"] = project
        if not change["due"] and "tag" not in change:
            return Result(f"You already have that task:\n{t.line(ctx.now())}")
        log.info("add_task for existing %r: updating it instead", t.title)
        return await update_task(change, ctx)

    async def add_task(args: dict, ctx: Context) -> Result | NeedsConfirm:
        from .projects import find_project  # projects build on tasks, not the other way round
        title = (args.get("title") or "").strip()
        if not title:
            return Result("What's the task?")
        if (instead := await existing_instead(title, args, ctx)) is not None:
            return instead
        due, due_text = None, ""
        if (when_text := (args.get("due") or "").strip()):
            parsed = parse_due(when_text, ctx.now())
            if isinstance(parsed, str):
                return Result(parsed)
            due, due_text = parsed[0] or None, f", {parsed[1]}" if parsed[0] else ""
        project, project_text = None, ""
        if (project_name := (args.get("project") or "").strip()):
            found = await find_project(notion, project_name)
            if isinstance(found, Result):
                return found
            project, project_text = found, f", in {found.title}"
        page = await notion.add(title, due, project.id if project else None)
        url = page.get("url", "")
        return Result(f"📝 Added to Notion: {title}{due_text}{project_text}", footer=f"🔗 {url}" if url else "")

    async def complete_task(args: dict, ctx: Context) -> Result | NeedsConfirm:
        t = await find_task(args.get("task") or "", ctx, "Which task did you finish?")
        if isinstance(t, Result):
            return t
        return NeedsConfirm(f"Mark this task as done?\n{t.line(ctx.now())}", {"id": t.id, "title": t.title})

    async def complete_execute(args: dict, ctx: Context) -> Result:
        await notion.mark_done(args["id"], ctx.now().date().isoformat())
        return Result(f"✅ Done: {args['title']}")

    async def update_task(args: dict, ctx: Context) -> Result | NeedsConfirm:
        status_text, note = (args.get("status") or "").strip(), (args.get("note") or "").strip()
        due_text, new_name, tag_text = ((args.get(k) or "").strip() for k in ("due", "new_name", "tag"))
        if not any((status_text, note, due_text, new_name, tag_text)):
            return Result("What should I change: status, due date, name, difficulty, or a progress note?")
        t = await find_task(args.get("task") or "", ctx, "Which task?", include_done=True)
        if isinstance(t, Result):
            return t
        _, schema = await notion.schema()
        change: dict = {"id": t.id, "title": t.title}
        shown: list[str] = []
        if status_text:
            option = schema.status.option_for(status_text)
            if option is None:
                choices = ", ".join(schema.status.options) or "none"
                return Result(f"I don't know the status “{status_text}”. Your list has: {choices}.")
            change["status"] = option
            shown.append(f"status → {option}")
        if due_text:
            parsed = parse_due(due_text, ctx.now())
            if isinstance(parsed, str):
                return Result(parsed)
            change["due"], change["due_text"] = parsed
            shown.append(parsed[1])
        if new_name:
            change["new_name"] = new_name
            shown.append(f"rename → “{new_name}”")
        if tag_text:
            tag = schema.tag_for(tag_text)
            if tag is None:
                choices = "; ".join(f"{c}: {', '.join(o)}" for c, o in schema.tags.items()) or "none"
                return Result(f"I don't know “{tag_text}”. You can set {choices}.")
            change["tag"] = list(tag)
            shown.append(f"{tag[0]} → {tag[1]}")
        if note:
            if not schema.note:
                return Result("Your tasks database has no text column for progress notes.")
            change["note"] = note
            shown.append(f"note → “{note}”")
        return NeedsConfirm(f"Update this task?\n{t.line(ctx.now())}\n" + "\n".join(shown), change)

    async def update_execute(args: dict, ctx: Context) -> Result:
        today = ctx.now().date().isoformat()
        await notion.update(args["id"], today=today, status=args.get("status"), note=args.get("note"),
                            due=args.get("due"), title=args.get("new_name"),
                            tag=tuple(args["tag"]) if args.get("tag") else None)
        if args.get("note"):
            try:  # the log is a nicety: the note itself is already saved
                await notion.append_log(args["id"], f"{ctx.now():%d %b %Y}: {args['note']}")
            except httpx.HTTPError as e:
                log.warning("couldn't append the progress log: %r", e)
        bits = [args[k] for k in ("status", "due_text") if args.get(k)]
        bits += [f"renamed to “{args['new_name']}”"] if args.get("new_name") else []
        bits += [args["tag"][1]] if args.get("tag") else []
        bits += [f"“{args['note']}”"] if args.get("note") else []
        return Result(f"📝 Updated {args.get('new_name') or args['title']}: {', '.join(bits)}")

    async def remove_task(args: dict, ctx: Context) -> Result | NeedsConfirm:
        t = await find_task(args.get("task") or "", ctx, "Which task should I remove?", include_done=True)
        if isinstance(t, Result):
            return t
        return NeedsConfirm(f"Move this task to Notion's trash?\n{t.line(ctx.now())}", {"id": t.id, "title": t.title})

    async def remove_execute(args: dict, ctx: Context) -> Result:
        await notion.trash(args["id"])
        return Result(f"🗑 Removed: {args['title']} (restore it from Notion's trash if needed)")

    return [
        Tool("list_tasks", "List the user's open Notion tasks / to-dos, optionally only those due in a period.",
             {"when": "optional period in the user's words: 'today', 'this week', 'overdue', 'in progress'; "
                      "empty for all open"},
             guard(list_tasks)),
        Tool("add_task", "Add a task / to-do to the user's Notion task list.",
             {"title": "the task, short", "due": "optional due date in the user's words, e.g. 'friday'",
              "project": "optional project the task belongs to"},
             guard(add_task), required=("title",), execute=guard(update_execute)),  # when it became an update
        Tool("complete_task", "Mark one of the user's Notion tasks as done / finished.",
             {"task": "words from the task's name"}, guard(complete_task), required=("task",),
             execute=guard(complete_execute)),
        Tool("update_task", "Change one of the user's Notion tasks: status (in progress, to do), due date, name, "
                            "difficulty or other tag, or a note on where it's at.",
             {"task": "words from the task's name", "status": "optional new status, e.g. 'in progress', 'to do'",
              "due": "optional new due date, e.g. 'friday'", "new_name": "optional new name",
              "tag": "optional difficulty or tag, e.g. 'boss', 'easy'", "note": "optional short progress note"},
             guard(update_task), required=("task",), execute=guard(update_execute)),
        Tool("remove_task", "Remove / delete one of the user's Notion tasks.",
             {"task": "words from the task's name"}, guard(remove_task), required=("task",),
             execute=guard(remove_execute)),
    ]
