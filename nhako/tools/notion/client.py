"""The Notion API, as far as Nhako needs it. One client per bot; schemas are cached for a while."""
import asyncio
import logging
import time

import httpx

from .schema import Project, ProjectSchema, Task, TaskSchema, database_id, parse_page, parse_project, plain

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"  # databases hold "data sources"; pages are created under the data source
MAX_PAGES = 10          # 1,000 rows is plenty for a personal list
SCHEMA_TTL = 600        # seconds: columns or options added in Notion show up without a restart
TEXT_BLOCKS = ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item",
               "to_do", "quote", "callout", "toggle", "code")


def text_block(content: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]}}


def page_title(page: dict) -> str:
    for p in (page.get("properties") or {}).values():
        if p.get("type") == "title":
            return plain(p.get("title")) or "(untitled)"
    return plain(page.get("title")) or "(untitled)"


class Notion:
    def __init__(self, token: str, db: str, transport: httpx.AsyncBaseTransport | None = None,
                 clock=time.monotonic):
        self.db = database_id(db)
        self.client = httpx.AsyncClient(base_url=API, transport=transport, timeout=15,
                                        headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION})
        self.clock = clock
        self._source: str | None = None
        self._schema: TaskSchema | None = None
        self._projects: ProjectSchema | None = None
        self._loaded_at = 0.0
        self._titles: dict[str, str] | None = None  # project id -> name, for the project on each task line
        self._titles_at = 0.0
        self._owner: str | None | bool = False  # False: not looked up yet

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _req(self, method: str, path: str, json: dict | None = None) -> dict:
        for attempt in range(2):
            r = await self.client.request(method, path, json=json)
            if r.status_code == 429 and attempt == 0:  # rate limited: wait as told (capped), once
                wait = min(float(r.headers.get("Retry-After") or 1), 5.0)
                log.info("notion rate limit on %s %s; retrying in %.1fs", method, path, wait)
                await asyncio.sleep(wait)
                continue
            break
        if r.status_code >= 400:
            log.warning("notion %s %s -> %s %s", method, path, r.status_code, r.text[:300])
        r.raise_for_status()
        return r.json()

    async def _query_all(self, source: str, body: dict) -> list[dict]:
        pages: list[dict] = []
        for _ in range(MAX_PAGES):  # Notion returns at most 100 per request
            resp = await self._req("POST", f"/data_sources/{source}/query", body)
            pages += resp.get("results", [])
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            body = {**body, "start_cursor": resp["next_cursor"]}
        return pages

    # --- schemas -----------------------------------------------------------------------

    async def schema(self) -> tuple[str, TaskSchema]:
        if self._schema is None or self.clock() - self._loaded_at > SCHEMA_TTL:
            db = await self._req("GET", f"/databases/{self.db}")
            self._source = db["data_sources"][0]["id"]
            source = await self._req("GET", f"/data_sources/{self._source}")
            self._schema = TaskSchema.from_properties(source["properties"])
            self._projects = None  # reread with the tasks schema
            self._loaded_at = self.clock()
            log.info("notion tasks schema: %s", self._schema)
        return self._source, self._schema

    async def project_schema(self) -> tuple[str, ProjectSchema] | None:
        _, schema = await self.schema()
        if not schema.project_source:
            return None
        if self._projects is None:
            source = await self._req("GET", f"/data_sources/{schema.project_source}")
            self._projects = ProjectSchema.from_properties(source["properties"])
            log.info("notion projects schema: %s", self._projects)
        return schema.project_source, self._projects

    async def owner(self) -> str | None:
        """The only human in the workspace, so new tasks show up in a 'Assigned to me' view."""
        if self._owner is False:
            people = [u["id"] for u in (await self._req("GET", "/users")).get("results", [])
                      if u.get("type") == "person"]
            self._owner = people[0] if len(people) == 1 else None
        return self._owner

    # --- tasks -------------------------------------------------------------------------

    async def _tasks(self, tz, filter_: dict | None) -> list[Task]:
        source, schema = await self.schema()
        body: dict = {"page_size": 100}
        if filter_:
            body["filter"] = filter_
        if schema.due:
            body["sorts"] = [{"property": schema.due, "direction": "ascending"}]
        tasks = [parse_page(p, schema, tz) for p in await self._query_all(source, body)]
        if any(t.projects for t in tasks):
            try:
                titles = await self.project_titles()
            except httpx.HTTPError as e:  # the names are a nicety: still list the tasks
                log.warning("couldn't read project names: %r", e)
                titles = {}
            for t in tasks:
                t.project_names = [titles[i] for i in t.projects if i in titles]
        return tasks

    async def open_tasks(self, tz) -> list[Task]:
        _, schema = await self.schema()
        return await self._tasks(tz, schema.open_filter())

    async def done_tasks(self, tz) -> list[Task]:
        _, schema = await self.schema()
        f = schema.status.done_filter()
        return await self._tasks(tz, f) if f else []

    async def add(self, title: str, due: str | None, project_id: str | None = None) -> dict:
        source, schema = await self.schema()
        props: dict = {schema.title: {"title": [{"text": {"content": title}}]}}
        if due and schema.due:
            props[schema.due] = {"date": {"start": due}}
        if schema.status.kind in ("status", "select") and schema.status.start:
            props.update(schema.status.value(schema.status.start))
        if project_id and schema.project:
            props[schema.project] = {"relation": [{"id": project_id}]}
        if schema.assignee and (owner := await self.owner()):
            props[schema.assignee] = {"people": [{"id": owner}]}
        return await self._req("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": source},
                                                  "properties": props})

    async def update(self, page_id: str, *, today: str, status: str | None = None, note: str | None = None,
                     due: str | None = None, title: str | None = None, tag: tuple[str, str] | None = None) -> None:
        """due: an ISO date, or "" to clear it. today: the date written to "Cleared on" when it's done."""
        _, schema = await self.schema()
        props: dict = {}
        if status:
            props.update(schema.status.value(status))
            if schema.cleared_on:
                done = schema.status.is_done(status)
                props[schema.cleared_on] = {"date": {"start": today} if done else None}
        if note is not None and schema.note:
            props[schema.note] = {"rich_text": [{"text": {"content": note}}] if note else []}
        if due is not None and schema.due:
            props[schema.due] = {"date": {"start": due} if due else None}
        if title:
            props[schema.title] = {"title": [{"text": {"content": title}}]}
        if tag:
            column, option = tag
            props[column] = {"select": {"name": option}}
        if props:
            await self._req("PATCH", f"/pages/{page_id}", {"properties": props})

    async def mark_done(self, page_id: str, today: str) -> None:
        _, schema = await self.schema()
        value = schema.done_value()
        if value is None:
            raise ValueError("this database has no status or done column")
        if schema.cleared_on:
            value = {**value, schema.cleared_on: {"date": {"start": today}}}
        await self._req("PATCH", f"/pages/{page_id}", {"properties": value})

    async def trash(self, page_id: str) -> None:
        """Notion's trash, not a hard delete: the page can be restored from there for 30 days."""
        await self._req("PATCH", f"/pages/{page_id}", {"in_trash": True})

    async def append_log(self, page_id: str, line: str) -> None:
        """A dated bullet at the end of the page body: the history behind "Where I'm at"."""
        await self._req("PATCH", f"/blocks/{page_id}/children", {"children": [text_block(line)]})

    # --- projects ----------------------------------------------------------------------

    async def projects(self) -> list[Project]:
        found = await self.project_schema()
        if found is None:
            return []
        source, schema = found
        projects = [parse_project(p, schema) for p in await self._query_all(source, {"page_size": 100})]
        self._titles, self._titles_at = {p.id: p.title for p in projects}, self.clock()
        return projects

    async def project_titles(self) -> dict[str, str]:
        """Project id -> name, cached like the schema."""
        if self._titles is None or self.clock() - self._titles_at > SCHEMA_TTL:
            await self.projects()
        return self._titles or {}

    async def add_project(self, title: str, status: str | None, about: str = "") -> dict:
        found = await self.project_schema()
        if found is None:
            raise ValueError("no projects database")
        source, schema = found
        props: dict = {schema.title: {"title": [{"text": {"content": title}}]}}
        if status and schema.status.kind in ("status", "select"):
            props.update(schema.status.value(status))
        if about and schema.about:
            props[schema.about] = {"rich_text": [{"text": {"content": about}}]}
        self._titles = None  # a new name to show next time
        return await self._req("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": source},
                                                  "properties": props})

    async def update_project(self, page_id: str, *, status: str | None = None, percent: float | None = None,
                             set_percent: bool = False) -> None:
        """set_percent with percent None clears the manual value, so the formula takes over again."""
        found = await self.project_schema()
        if found is None:
            raise ValueError("no projects database")
        schema = found[1]
        props: dict = {}
        if status:
            props.update(schema.status.value(status))
        if set_percent:
            if not schema.manual:
                raise ValueError("the projects database has no number column for progress")
            props[schema.manual] = {"number": percent}
        if props:
            await self._req("PATCH", f"/pages/{page_id}", {"properties": props})

    # --- any page ----------------------------------------------------------------------

    async def search(self, query: str) -> list[dict]:
        """Pages shared with the integration whose titles match, best first (Notion's ranking)."""
        resp = await self._req("POST", "/search", {"query": query, "page_size": 10,
                                                   "filter": {"property": "object", "value": "page"}})
        return resp.get("results", [])

    async def page_text(self, page: dict, limit: int = 3000) -> str:
        """A page's properties and top-level text blocks as plain text, cut at `limit` characters."""
        lines = [f"# {page_title(page)}"]
        for name, p in (page.get("properties") or {}).items():
            value = prop_text(p)
            if value and p.get("type") != "title":
                lines.append(f"{name}: {value}")
        resp = await self._req("GET", f"/blocks/{page['id']}/children?page_size=100")
        for b in resp.get("results", []):
            kind = b.get("type", "")
            if kind in TEXT_BLOCKS:
                text = plain(b[kind].get("rich_text"))
                if kind == "to_do":
                    text = f"[{'x' if b[kind].get('checked') else ' '}] {text}"
                elif kind.startswith("heading"):
                    text = f"## {text}"
                elif kind.endswith("list_item"):
                    text = f"- {text}"
                if text.strip():
                    lines.append(text)
            elif kind in ("child_page", "child_database"):
                lines.append(f"[{kind.replace('child_', '')}: {b[kind].get('title', '')}]")
        text = "\n".join(lines)
        return text if len(text) <= limit else text[:limit] + "\n…(cut)"


def prop_text(p: dict) -> str:
    kind = p.get("type")
    v = p.get(kind)
    if kind in ("rich_text", "title"):
        return plain(v)
    if kind in ("select", "status"):
        return (v or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(o.get("name", "") for o in v or [])
    if kind == "date":
        return (v or {}).get("start", "")
    if kind in ("number", "url", "checkbox"):
        return "" if v is None else str(v)
    if kind in ("formula", "rollup"):
        inner = v or {}
        value = inner.get(inner.get("type", ""))
        return "" if value is None or isinstance(value, (list, dict)) else str(value)
    return ""
