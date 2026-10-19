"""Notion tasks: list what's open or due, add a task, mark one done.

Auth: an internal integration token (notion.so/profile/integrations) with the tasks
database shared to it. The database's columns are read from Notion, so any
"title + date + status/checkbox" database works, not only the "My Tasks" template.
"""
import datetime as dt
import logging
import re
from dataclasses import dataclass, field

import httpx

from ..timeparse import parse_range, parse_when
from . import Context, NeedsConfirm, Result, Tool
from .calendar import FILLER, MATCH_THRESHOLD, match_score

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"  # databases hold "data sources"; pages are created under the data source
NOT_SET_UP = ("Notion isn't set up. Put NOTION_TOKEN and NOTION_TASKS_DB in .env "
              "(see .env.example) and share the database with the integration.")
DONE_WORDS = ("done", "complete", "completed", "finished", "selesai")
MAX_PAGES = 10  # 1,000 open tasks is plenty for a personal list
TASK_WORDS = FILLER | {"task", "tasks", "todo", "to-do", "mark", "as", "done", "complete", "completed", "finished",
                       "finish", "tugas", "i", "i've", "ive", "just", "already", "with", "have", "saya", "aku",
                       "dah", "sudah", "siap", "selesai"}
# "I finished the invoice", "dah siap invoice tu": a task marked done, said casually.
DONE = re.compile(r"\b(finish|finished|complete|completed|done|selesai|siap)\b", re.I)


def database_id(text: str) -> str:
    """Accepts the raw id or a notion.so / app.notion.com URL."""
    m = re.findall(r"[0-9a-f]{32}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", (text or "").lower())
    return m[-1].replace("-", "") if m else ""


@dataclass
class TaskSchema:
    title: str
    due: str | None = None
    status: str | None = None        # property name
    status_type: str | None = None   # "status", "select" or "checkbox"
    done: list[str] = field(default_factory=list)   # option names that mean finished
    start: str | None = None          # option set on new tasks
    assignee: str | None = None       # people property, filled with the workspace owner

    @classmethod
    def from_properties(cls, props: dict) -> "TaskSchema":
        by_type: dict[str, list[str]] = {}
        for name, p in props.items():
            by_type.setdefault(p["type"], []).append(name)

        def prefer(kind: str, *hints: str) -> str | None:
            names = by_type.get(kind, [])
            return next((n for h in hints for n in names if h in n.lower()), names[0] if names else None)

        s = cls(title=by_type["title"][0], due=prefer("date", "due", "deadline", "date", "tarikh"),
                assignee=prefer("people", "assign", "owner"))
        if name := prefer("status", "status"):
            st = props[name]["status"]
            options = {o["id"]: o["name"] for o in st.get("options", [])}
            groups = {g["name"].lower(): [options[i] for i in g.get("option_ids", []) if i in options]
                      for g in st.get("groups", [])}
            s.status, s.status_type = name, "status"
            s.done = groups.get("complete") or [n for n in options.values() if n.lower() in DONE_WORDS]
            s.start = (groups.get("to-do") or [None])[0]
        elif name := prefer("checkbox", "done", "complete", "selesai"):
            s.status, s.status_type = name, "checkbox"
        elif name := prefer("select", "status"):
            options = [o["name"] for o in props[name]["select"].get("options", [])]
            s.status, s.status_type = name, "select"
            s.done = [n for n in options if n.lower() in DONE_WORDS]
        return s

    def open_filter(self) -> dict | None:
        if self.status_type == "checkbox":
            return {"property": self.status, "checkbox": {"equals": False}}
        if self.status_type in ("status", "select") and self.done:
            key = self.status_type
            parts = [{"property": self.status, key: {"does_not_equal": d}} for d in self.done]
            return parts[0] if len(parts) == 1 else {"and": parts}
        return None

    def done_value(self) -> dict | None:
        if self.status_type == "checkbox":
            return {self.status: {"checkbox": True}}
        if self.status_type in ("status", "select") and self.done:
            return {self.status: {self.status_type: {"name": self.done[0]}}}
        return None


@dataclass
class Task:
    id: str
    title: str
    due: dt.datetime | None
    has_time: bool
    url: str
    status: str = ""

    def overdue(self, now: dt.datetime) -> bool:
        if self.due is None:
            return False
        return self.due < now if self.has_time else self.due.date() < now.date()

    def line(self, now: dt.datetime) -> str:
        if self.due is None:
            return f"• {self.title}"
        fmt = "%a %d %b %H:%M" if self.has_time else "%a %d %b"
        return f"• {self.title}  ({'⚠️ overdue, ' if self.overdue(now) else ''}due {self.due:{fmt}})"


def parse_page(page: dict, schema: TaskSchema, tz) -> Task:
    props = page.get("properties", {})
    title = "".join(t.get("plain_text", "") for t in props.get(schema.title, {}).get("title", [])) or "(untitled)"
    due, has_time = None, False
    start = ((props.get(schema.due) or {}).get("date") or {}).get("start") if schema.due else None
    if start:
        has_time = "T" in start
        due = dt.datetime.fromisoformat(start)
        due = due.astimezone(tz) if has_time else due.replace(tzinfo=tz)
    status = ""
    if schema.status_type in ("status", "select"):
        status = ((props.get(schema.status) or {}).get(schema.status_type) or {}).get("name", "")
    return Task(page["id"], title, due, has_time, page.get("url", ""), status)


class Notion:
    def __init__(self, token: str, db: str, transport: httpx.AsyncBaseTransport | None = None):
        self.db = database_id(db)
        self.client = httpx.AsyncClient(base_url=API, transport=transport, timeout=15,
                                        headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION})
        self._source: str | None = None
        self._schema: TaskSchema | None = None
        self._owner: str | None | bool = False  # False: not looked up yet

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, path: str, json: dict | None = None) -> dict:
        r = await self.client.request(method, path, json=json)
        if r.status_code >= 400:
            log.warning("notion %s %s -> %s %s", method, path, r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json()

    async def schema(self) -> tuple[str, TaskSchema]:
        if self._schema is None:
            db = await self._req("GET", f"/databases/{self.db}")
            self._source = db["data_sources"][0]["id"]
            source = await self._req("GET", f"/data_sources/{self._source}")
            self._schema = TaskSchema.from_properties(source["properties"])
            log.info("notion tasks schema: %s", self._schema)
        return self._source, self._schema

    async def owner(self) -> str | None:
        """The only human in the workspace, so new tasks show up in a 'Assigned to me' view."""
        if self._owner is False:
            people = [u["id"] for u in (await self._req("GET", "/users")).get("results", [])
                      if u.get("type") == "person"]
            self._owner = people[0] if len(people) == 1 else None
        return self._owner

    async def open_tasks(self, tz) -> list[Task]:
        source, schema = await self.schema()
        body: dict = {"page_size": 100}
        if f := schema.open_filter():
            body["filter"] = f
        if schema.due:
            body["sorts"] = [{"property": schema.due, "direction": "ascending"}]
        pages: list[dict] = []
        for _ in range(MAX_PAGES):  # Notion returns at most 100 per request
            resp = await self._req("POST", f"/data_sources/{source}/query", body)
            pages += resp.get("results", [])
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            body["start_cursor"] = resp["next_cursor"]
        return [parse_page(p, schema, tz) for p in pages]

    async def add(self, title: str, due: str | None) -> dict:
        source, schema = await self.schema()
        props: dict = {schema.title: {"title": [{"text": {"content": title}}]}}
        if due and schema.due:
            props[schema.due] = {"date": {"start": due}}
        if schema.status_type == "status" and schema.start:
            props[schema.status] = {"status": {"name": schema.start}}
        if schema.assignee and (owner := await self.owner()):
            props[schema.assignee] = {"people": [{"id": owner}]}
        return await self._req("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": source},
                                                  "properties": props})

    async def mark_done(self, page_id: str) -> None:
        _, schema = await self.schema()
        value = schema.done_value()
        if value is None:
            raise ValueError("this database has no status or done column")
        await self._req("PATCH", f"/pages/{page_id}", {"properties": value})


# --- tools ----------------------------------------------------------------------------

def due_in(tasks: list[Task], text: str, now: dt.datetime) -> tuple[list[Task], str]:
    """Tasks due within the range the user named, overdue ones included. No range: everything open."""
    t = (text or "").lower().strip()
    if not t or t in ("all", "open", "everything", "semua"):
        return tasks, "open"
    if "overdue" in t or "lewat" in t:
        return [x for x in tasks if x.overdue(now)], "overdue"
    _, end, label = parse_range(t, now)
    return [x for x in tasks if x.due and x.due < end], f"due {label}"


def format_tasks(tasks: list[Task], label: str, now: dt.datetime) -> str:
    if not tasks:
        return f"✅ No tasks {label}." if label != "open" else "✅ No open tasks. Nice."
    head = f"📝 {len(tasks)} task{'s' * (len(tasks) != 1)} {label}:"
    more = f"\n…and {len(tasks) - 30} more" if len(tasks) > 30 else ""
    return "\n".join([head, *(t.line(now) for t in tasks[:30])]) + more


async def tasks_report(notion: Notion | None, now: dt.datetime, when: str = "") -> str:
    """Plain task list, no model involved: used by /tasks and the morning agenda."""
    if notion is None:
        return NOT_SET_UP
    try:
        return format_tasks(*due_in(await notion.open_tasks(now.tzinfo), when, now), now)
    except httpx.HTTPError as e:
        log.warning("notion failed: %r", e)
        return "⚠️ I couldn't read your Notion tasks."


def task_words(phrase: str) -> list[str]:
    return [w for w in re.findall(r"[\w-]+", (phrase or "").lower()) if w not in TASK_WORDS]


def best_tasks(tasks: list[Task], words: list[str]) -> list[Task]:
    """The open tasks whose titles match the words best, if any match well enough."""
    scored = [(match_score(t.title, words), t) for t in tasks]
    best = max((sc for sc, _ in scored), default=0)
    return [t for sc, t in scored if sc >= MATCH_THRESHOLD and sc == best]


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
        return "notion" if best_tasks(tasks, words) else None
    return check


def tools(ctx: Context, notion: Notion | None = None) -> list[Tool]:
    s = ctx.settings
    notion = notion or (Notion(s.notion_token, s.notion_tasks_db) if s.notion_token and s.notion_tasks_db else None)

    def guard(fn):
        async def run(args: dict, ctx: Context):
            if notion is None:
                return Result(NOT_SET_UP)
            try:
                return await fn(args, ctx)
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403, 404):
                    return Result("Notion refused that. Check NOTION_TOKEN, and that the tasks database is "
                                  "shared with the integration (••• → Connections).")
                raise
            except httpx.HTTPError as e:
                log.warning("notion unreachable: %r", e)
                return Result("I couldn't reach Notion just now. Try again in a bit.")
        return run

    async def list_tasks(args: dict, ctx: Context) -> Result:
        now = ctx.now()
        return Result(format_tasks(*due_in(await notion.open_tasks(now.tzinfo), args.get("when") or "", now), now))

    async def add_task(args: dict, ctx: Context) -> Result:
        title = (args.get("title") or "").strip()
        if not title:
            return Result("What's the task?")
        due, due_text = None, ""
        if (when_text := (args.get("due") or "").strip()):
            when = parse_when(when_text, ctx.now())
            if when is None:
                return Result(f"I couldn't work out when “{when_text}” is. Try “friday” or “tomorrow 5pm”.")
            due = when.at.isoformat() if when.has_time else when.at.date().isoformat()
            due_text = f", due {when.at:%a %d %b %H:%M}" if when.has_time else f", due {when.at:%a %d %b}"
        page = await notion.add(title, due)
        url = page.get("url", "")
        return Result(f"📝 Added to Notion: {title}{due_text}", footer=f"🔗 {url}" if url else "")

    async def complete_task(args: dict, ctx: Context) -> Result | NeedsConfirm:
        words = task_words(args.get("task") or "")
        if not words:
            return Result("Which task did you finish?")
        matches = best_tasks(await notion.open_tasks(ctx.now().tzinfo), words)
        if not matches:
            return Result(f"I can't find an open task matching “{' '.join(words)}”.")
        if len(matches) > 1:
            listing = "\n".join(t.line(ctx.now()) for t in matches[:8])
            return Result(f"Which one?\n{listing}")
        t = matches[0]
        return NeedsConfirm(f"Mark this task as done?\n{t.line(ctx.now())}", {"id": t.id, "title": t.title})

    async def complete_execute(args: dict, ctx: Context) -> Result:
        await notion.mark_done(args["id"])
        return Result(f"✅ Done: {args['title']}")

    return [
        Tool("list_tasks", "List the user's open Notion tasks / to-dos, optionally only those due in a period.",
             {"when": "optional period in the user's words: 'today', 'this week', 'overdue'; empty for all open"},
             guard(list_tasks)),
        Tool("add_task", "Add a task / to-do to the user's Notion task list.",
             {"title": "the task, short", "due": "optional due date in the user's words, e.g. 'friday'"},
             guard(add_task), required=("title",)),
        Tool("complete_task", "Mark one of the user's Notion tasks as done / finished.",
             {"task": "words from the task's name"}, guard(complete_task), required=("task",),
             execute=guard(complete_execute)),
    ]
