"""What the user's databases look like: which column is the title, the due date, the status,
and what each row means. Read from Notion, so any similar database works, not only Hakim's Hub."""
import datetime as dt
import re
from dataclasses import dataclass, field

DONE_WORDS = ("done", "complete", "completed", "finished", "selesai", "cleared")
PROGRESS_WORDS = ("in progress", "doing", "active", "ongoing", "started")


def database_id(text: str) -> str:
    """Accepts the raw id or a notion.so / app.notion.com URL."""
    m = re.findall(r"[0-9a-f]{32}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", (text or "").lower())
    return m[-1].replace("-", "") if m else ""


def label(name: str) -> str:
    """An option name without its emoji: '⚔️ In progress' -> 'in progress'."""
    return re.sub(r"[^\w\s-]", "", name or "").strip().lower()


def plain(rich: list[dict] | None) -> str:
    return "".join(t.get("plain_text", "") for t in rich or [])


def match_option(options: list[str], words: str) -> str | None:
    """The option the words name: 'boss' -> '🔴 Boss', 'omni work' -> '🏢 Omni Work'."""
    w = label(words)
    if not w:
        return None
    return (next((o for o in options if label(o) == w), None)
            or next((o for o in options if label(o) and (w in label(o) or label(o) in w)), None))


def props_by_type(props: dict) -> dict[str, list[str]]:
    by_type: dict[str, list[str]] = {}
    for name, p in props.items():
        by_type.setdefault(p["type"], []).append(name)
    return by_type


def preferrer(by_type: dict[str, list[str]]):
    def prefer(kind: str, *hints: str, fallback: bool = True) -> str | None:
        names = by_type.get(kind, [])
        hit = next((n for h in hints for n in names if h in n.lower()), None)
        return hit or (names[0] if names and fallback else None)
    return prefer


def select_options(props: dict, name: str) -> list[str]:
    return [o["name"] for o in props[name][props[name]["type"]].get("options", [])]


def date_of(prop: dict | None, tz) -> tuple[dt.datetime | None, bool]:
    """A date property's start as (datetime in tz, has a time)."""
    start = ((prop or {}).get("date") or {}).get("start")
    if not start:
        return None, False
    at = dt.datetime.fromisoformat(start)
    return (at.astimezone(tz), True) if "T" in start else (at.replace(tzinfo=tz), False)


def formula_value(prop: dict | None):
    """A formula, rollup or number property's value: 25, '⚔️ ▰▰▱▱▱ 33%', or None."""
    p = prop or {}
    inner = p.get(p.get("type", "")) if p.get("type") in ("formula", "rollup") else p
    if not isinstance(inner, dict):
        return None
    return inner.get(inner.get("type", "number"))


@dataclass
class StatusColumn:
    """A status, select or checkbox column, and which of its options mean what."""
    name: str | None = None
    kind: str | None = None                          # "status", "select" or "checkbox"
    options: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)    # option names that mean finished
    start: str | None = None                          # option set on new tasks
    progress: str | None = None                       # option meaning "working on it"

    @classmethod
    def find(cls, props: dict, prefer) -> "StatusColumn":
        if name := prefer("status", "status"):
            st = props[name]["status"]
            options = {o["id"]: o["name"] for o in st.get("options", [])}
            groups = {g["name"].lower(): [options[i] for i in g.get("option_ids", []) if i in options]
                      for g in st.get("groups", [])}
            done = groups.get("complete") or [n for n in options.values() if label(n) in DONE_WORDS]
            return cls(name, "status", list(options.values()), done, (groups.get("to-do") or [None])[0],
                       (groups.get("in progress") or [None])[0])
        if name := prefer("checkbox", "done", "complete", "selesai"):
            return cls(name, "checkbox")
        if name := prefer("select", "status"):
            options = select_options(props, name)
            done = [n for n in options if label(n) in DONE_WORDS]
            progress = next((n for n in options if label(n) in PROGRESS_WORDS or "progress" in label(n)), None)
            start = next((n for n in options if n not in done and n != progress), None)
            return cls(name, "select", options, done, start, progress)
        return cls()

    def value(self, option: str | bool) -> dict:
        if self.kind == "checkbox":
            return {self.name: {"checkbox": bool(option)}}
        return {self.name: {self.kind: {"name": option}}}

    def open_filter(self) -> dict | None:
        if self.kind == "checkbox":
            return {"property": self.name, "checkbox": {"equals": False}}
        if self.kind in ("status", "select") and self.done:
            parts = [{"property": self.name, self.kind: {"does_not_equal": d}} for d in self.done]
            return parts[0] if len(parts) == 1 else {"and": parts}
        return None

    def done_filter(self) -> dict | None:
        if self.kind == "checkbox":
            return {"property": self.name, "checkbox": {"equals": True}}
        if self.kind in ("status", "select") and self.done:
            parts = [{"property": self.name, self.kind: {"equals": d}} for d in self.done]
            return parts[0] if len(parts) == 1 else {"or": parts}
        return None

    def read(self, props: dict) -> str:
        if self.kind in ("status", "select"):
            return ((props.get(self.name) or {}).get(self.kind) or {}).get("name", "")
        if self.kind == "checkbox":
            return "done" if (props.get(self.name) or {}).get("checkbox") else ""
        return ""

    def is_done(self, status: str) -> bool:
        return status in self.done or (self.kind == "checkbox" and status == "done")

    def option_for(self, words: str) -> str | None:
        """The option the user means: 'in progress', 'back to to-do', 'pause', 'done', or an option's name."""
        w = label(words)
        if not w or self.kind not in ("status", "select"):
            return None
        if "progress" in w:
            return self.progress
        if any(k in w for k in ("to do", "todo", "to-do", "not started", "backlog", "reopen")):
            return self.start
        if any(k in w for k in DONE_WORDS) or "finish" in w:
            return self.done[0] if self.done else None
        if own := match_option(self.options, w):  # "pause" -> "💤 Paused", "idea" -> "🌱 Idea"
            return own
        if any(k in w for k in ("doing", "start", "working", "ongoing", "active", "resume")):
            return self.progress
        return None


@dataclass
class TaskSchema:
    title: str
    due: str | None = None
    status: StatusColumn = field(default_factory=StatusColumn)
    assignee: str | None = None        # people property, filled with the workspace owner
    note: str | None = None            # text property for "where I'm at"
    project: str | None = None         # relation property to the projects database
    project_source: str | None = None  # that database's data source id
    cleared_on: str | None = None      # date property set when a task is marked done
    xp: str | None = None              # formula or number worth of a finished task
    tags: dict[str, list[str]] = field(default_factory=dict)  # other select columns -> options

    @classmethod
    def from_properties(cls, props: dict) -> "TaskSchema":
        by_type = props_by_type(props)
        prefer = preferrer(by_type)
        project = prefer("relation", "campaign", "project", fallback=False)
        cleared = prefer("date", "cleared", "completed", "finished", fallback=False)
        due = next((n for h in ("due", "deadline", "date", "tarikh") for n in by_type.get("date", [])
                    if h in n.lower() and n != cleared), None)
        due = due or next((n for n in by_type.get("date", []) if n != cleared), None)
        status = StatusColumn.find(props, prefer)
        tags = {n: select_options(props, n) for n in by_type.get("select", []) if n != status.name}
        return cls(title=by_type["title"][0], due=due, status=status, assignee=prefer("people", "assign", "owner"),
                   note=prefer("rich_text", "where", "note", "progress", fallback=False), project=project,
                   project_source=props[project]["relation"].get("data_source_id") if project else None,
                   cleared_on=cleared, xp=prefer("formula", "xp", "points", fallback=False)
                   or prefer("number", "xp", "points", fallback=False), tags=tags)

    def open_filter(self) -> dict | None:
        return self.status.open_filter()

    def done_value(self) -> dict | None:
        if self.status.kind == "checkbox":
            return self.status.value(True)
        if self.status.kind in ("status", "select") and self.status.done:
            return self.status.value(self.status.done[0])
        return None

    def tag_for(self, words: str) -> tuple[str, str] | None:
        """(column, option) for 'boss' or 'omni work', across the extra select columns."""
        for column, options in self.tags.items():
            if option := match_option(options, words):
                return column, option
        return None


@dataclass
class ProjectSchema:
    title: str
    status: StatusColumn = field(default_factory=StatusColumn)
    progress: str | None = None   # formula or number column shown as the progress
    manual: str | None = None     # number column the bot writes a percentage into
    about: str | None = None      # text column describing the project

    @classmethod
    def from_properties(cls, props: dict) -> "ProjectSchema":
        by_type = props_by_type(props)
        prefer = preferrer(by_type)
        manual = prefer("number", "manual", "%", "progress", "percent", fallback=False)
        return cls(title=by_type["title"][0], status=StatusColumn.find(props, prefer),
                   progress=prefer("formula", "progress", fallback=False) or manual, manual=manual,
                   about=prefer("rich_text", "about", "description", "summary", fallback=False))


@dataclass
class Task:
    id: str
    title: str
    due: dt.datetime | None
    has_time: bool
    url: str
    status: str = ""
    in_progress: bool = False
    note: str = ""
    projects: list[str] = field(default_factory=list)
    done: bool = False
    xp: float = 0
    cleared_on: dt.datetime | None = None  # the "Cleared on" date, else when the page was last edited
    project_names: list[str] = field(default_factory=list)  # filled in by the client

    def overdue(self, now: dt.datetime) -> bool:
        if self.due is None or self.done:
            return False
        return self.due < now if self.has_time else self.due.date() < now.date()

    def line(self, now: dt.datetime, show_project: bool = True) -> str:
        text = f"• {self.title}"
        if self.due is not None:
            fmt = "%a %d %b %H:%M" if self.has_time else "%a %d %b"
            text += f"  ({'⚠️ overdue, ' if self.overdue(now) else ''}due {self.due:{fmt}})"
        if self.in_progress or self.done:
            text += f"  · {self.status}"
        if show_project and self.project_names:
            text += f"  · 🗺️ {', '.join(self.project_names)}"
        if self.note:
            text += f"\n   ↳ {self.note}"
        return text


@dataclass
class Project:
    id: str
    title: str
    status: str
    progress: str
    url: str
    done: bool = False
    active: bool = True  # the usual "working on it" status: not worth showing on every line

    def line(self) -> str:
        text = f"• {self.title}  {self.progress}".rstrip()
        return text + (f"  · {self.status}" if self.status and not self.active else "")


def parse_page(page: dict, schema: TaskSchema, tz) -> Task:
    props = page.get("properties", {})
    title = plain(props.get(schema.title, {}).get("title")) or "(untitled)"
    due, has_time = date_of(props.get(schema.due), tz) if schema.due else (None, False)
    status = schema.status.read(props)
    note = plain((props.get(schema.note) or {}).get("rich_text")) if schema.note else ""
    projects = [r["id"] for r in (props.get(schema.project) or {}).get("relation", [])] if schema.project else []
    xp = formula_value(props.get(schema.xp)) if schema.xp else None
    cleared = date_of(props.get(schema.cleared_on), tz)[0] if schema.cleared_on else None
    if cleared is None and page.get("last_edited_time"):
        cleared = dt.datetime.fromisoformat(page["last_edited_time"].replace("Z", "+00:00")).astimezone(tz)
    return Task(page["id"], title, due, has_time, page.get("url", ""), status,
                bool(status) and status == schema.status.progress, note.strip(), projects,
                schema.status.is_done(status), xp if isinstance(xp, (int, float)) else 0, cleared)


def parse_project(page: dict, schema: ProjectSchema) -> Project:
    props = page.get("properties", {})
    title = plain(props.get(schema.title, {}).get("title")) or "(untitled)"
    status = schema.status.read(props)
    progress = ""
    if schema.progress:
        raw = formula_value(props.get(schema.progress))
        progress = f"{raw:g}%" if isinstance(raw, (int, float)) else str(raw or "")
    return Project(page["id"], title, status, progress, page.get("url", ""), schema.status.is_done(status),
                   not status or status == schema.status.progress)
