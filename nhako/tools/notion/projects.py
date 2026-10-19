"""Project / campaign tools, XP stats, and reading any Notion page shared with the integration."""
import logging
import re

import httpx

from .. import Context, NeedsConfirm, Result, Tool
from ..calendar import match_score
from .client import Notion, page_title
from .reports import NO_PROJECTS, format_projects, stats_text
from .schema import Project
from .tasks import best_matches, guarded, task_words

log = logging.getLogger(__name__)


def parse_percent(text: str) -> float | None | str:
    """'50%' -> 50.0; 'auto' / 'reset' -> None (back to the formula); nonsense -> the error to show."""
    t = (text or "").lower()
    if any(w in t for w in ("auto", "reset", "clear", "remove", "none")):
        return None
    m = re.search(r"\d+(?:\.\d+)?", t)
    if not m:
        return f"I couldn't read a percentage in “{text}”. Try “50%”."
    value = float(m.group())
    if "." in m.group() and value <= 1 and "%" not in t:
        value *= 100  # "0.5"
    if not 0 <= value <= 100:
        return "Progress has to be between 0 and 100%."
    return value


async def find_project(notion: Notion, phrase: str) -> Project | Result:
    """One project matching the user's words, or the Result to send instead."""
    if await notion.project_schema() is None:
        return Result(NO_PROJECTS)
    words = task_words(phrase)
    if not words:
        return Result("Which project?")
    matches = best_matches(await notion.projects(), words)
    if not matches:
        return Result(f"I can't find a project matching “{' '.join(words)}”.")
    if len(matches) > 1:
        return Result("Which one?\n" + "\n".join(p.line() for p in matches[:8]))
    return matches[0]


def named_in(projects: list[Project], text: str) -> Project | None:
    """The one project the whole message names, for 'what's left in Octomations?' when the model
    leaves the project argument empty. Every word of the title must appear."""
    t = text.lower()
    hits = [p for p in projects if (words := re.findall(r"\w+", p.title.lower()))
            and all(re.search(rf"\b{re.escape(w)}\b", t) for w in words)]
    return max(hits, key=lambda p: len(p.title)) if hits else None


# Words that only mean the user's Notion here: the router sent "how much XP" to chat and
# "push the bug fixes quest to friday" to the calendar.
NOTION_WORDS = re.compile(r"\b(quests?|xp|campaigns?|notion)\b", re.I)
# With a project's name: "pause NeraOS" went to homelab, "what's in my Nhako Depot page?" to the calendar.
PROJECT_ACTIONS = re.compile(r"\b(pause|paused|resume|progress|cleared|page|far along|status|left in)\b|%", re.I)


def claim(notion: Notion | None):
    async def check(text: str, ctx: Context) -> str | None:
        if notion is None:
            return None
        if NOTION_WORDS.search(text):
            return "notion"
        if PROJECT_ACTIONS.search(text):
            try:
                if await notion.project_schema() is not None and named_in(await notion.projects(), text):
                    return "notion"
            except httpx.HTTPError as e:
                log.warning("notion unreachable while checking %r: %r", text[:60], e)
        return None
    return check


def tools(ctx: Context, notion: Notion | None = None) -> list[Tool]:
    s = ctx.settings
    notion = notion or (Notion(s.notion_token, s.notion_tasks_db) if s.notion_token and s.notion_tasks_db else None)
    guard = guarded(notion)

    async def one_project(p: Project, ctx: Context) -> Result:
        tasks = [t for t in await notion.open_tasks(ctx.now().tzinfo) if p.id in t.projects]
        left = "\n".join(t.line(ctx.now()) for t in tasks) if tasks else "No open tasks in it."
        return Result(f"🗺️ {p.line()[2:]}\n\n{left}", footer=f"🔗 {p.url}" if p.url else "")

    async def list_projects(args: dict, ctx: Context) -> Result:
        if await notion.project_schema() is None:
            return Result(NO_PROJECTS)
        name = (args.get("project") or "").strip()
        everything = name.lower() in ("all", "semua", "everything")
        if name and not everything:
            p = await find_project(notion, name)
            return p if isinstance(p, Result) else await one_project(p, ctx)
        projects = await notion.projects()
        if not everything and (p := named_in(projects, ctx.text)):
            return await one_project(p, ctx)
        return Result(format_projects(projects, everything=everything))

    async def update_project(args: dict, ctx: Context) -> Result | NeedsConfirm:
        status_text, percent_text = (args.get("status") or "").strip(), (args.get("percent") or "").strip()
        if not status_text and not percent_text:
            return Result("What should I change: the project's status or its progress %?")
        p = await find_project(notion, args.get("project") or "")
        if isinstance(p, Result):
            return p
        _, schema = await notion.project_schema()
        change: dict = {"id": p.id, "title": p.title}
        shown = []
        if status_text:
            option = schema.status.option_for(status_text)
            if option is None:
                return Result(f"I don't know the status “{status_text}”. Projects can be: "
                              f"{', '.join(schema.status.options) or 'none'}.")
            change["status"] = option
            shown.append(f"status → {option}")
        if percent_text:
            percent = parse_percent(percent_text)
            if isinstance(percent, str):
                return Result(percent)
            if not schema.manual:
                return Result("Your projects database has no number column I can write progress into.")
            change["set_percent"], change["percent"] = True, percent
            shown.append("progress → automatic (from finished tasks)" if percent is None
                         else f"progress → {percent:g}%")
        return NeedsConfirm(f"Update {p.title}?\nNow: {p.progress or 'no progress yet'}"
                            + (f" · {p.status}" if p.status else "") + "\n" + "\n".join(shown), change)

    async def update_project_execute(args: dict, ctx: Context) -> Result:
        await notion.update_project(args["id"], status=args.get("status"), percent=args.get("percent"),
                                    set_percent=bool(args.get("set_percent")))
        bits = [args["status"]] if args.get("status") else []
        if args.get("set_percent"):
            bits.append("progress automatic again" if args.get("percent") is None
                        else f"progress {args['percent']:g}%")
        return Result(f"🗺️ {args['title']}: {', '.join(bits)}")

    async def add_project(args: dict, ctx: Context) -> Result:
        name = (args.get("name") or "").strip()
        if not name:
            return Result("What's the project called?")
        found = await notion.project_schema()
        if found is None:
            return Result(NO_PROJECTS)
        schema = found[1]
        status = None
        if (status_text := (args.get("status") or "").strip()):
            status = schema.status.option_for(status_text)
        status = status or schema.status.start  # a new project starts as an idea unless told otherwise
        page = await notion.add_project(name, status, (args.get("about") or "").strip())
        url = page.get("url", "")
        return Result(f"🗺️ New project: {name}" + (f" ({status})" if status else ""),
                      footer=f"🔗 {url}" if url else "")

    async def quest_stats(args: dict, ctx: Context) -> Result:
        return Result(await stats_text(notion, ctx.now(), args.get("period") or ""))

    async def read_notion(args: dict, ctx: Context) -> Result:
        query = (args.get("page") or "").strip()
        words = task_words(query)
        if not words:
            return Result("Which Notion page?")
        pages = await notion.search(" ".join(words)) or await notion.search(query)
        if not pages:
            return Result(f"I can't find a Notion page matching “{query}”. Is it shared with the integration?")
        page = max(pages, key=lambda pg: match_score(page_title(pg), words))  # Notion's order breaks ties
        text = await notion.page_text(page)
        return Result(f"Notion page content:\n{text}", summarize=True,
                      footer=f"🔗 {page.get('url')}" if page.get("url") else "")

    return [
        Tool("list_projects", "List the user's projects and how far along each is, or one project's open tasks.",
             {"project": "optional project name; empty for all projects"}, guard(list_projects)),
        Tool("update_project", "Change one of the user's projects: its status (active, paused, cleared) or its "
                               "progress as a percentage.",
             {"project": "the project's name", "status": "optional new status, e.g. 'paused', 'active', 'cleared'",
              "percent": "optional progress, e.g. '50%', or 'auto' to go back to automatic"},
             guard(update_project), required=("project",), execute=guard(update_project_execute)),
        Tool("add_project", "Start / create a new project for the user.",
             {"name": "the project's name", "about": "optional one-line description",
              "status": "optional starting status, e.g. 'active'; default idea"},
             guard(add_project), required=("name",)),
        Tool("quest_stats", "The user's XP, level and how many tasks they cleared in a period.",
             {"period": "optional: 'this week', 'last week', 'this month', 'all time'; default this week"},
             guard(quest_stats)),
        Tool("read_notion", "Read one of the user's Notion pages to answer a question about what's in it.",
             {"page": "the page's name or topic"}, guard(read_notion), required=("page",)),
    ]
