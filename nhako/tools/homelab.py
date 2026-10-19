"""Homelab status: Minecraft servers (server-list ping) and Docker containers (read-only).

Docker is reached through a socket proxy that only allows GET on /containers
(compose.yaml), so nothing here can start, stop or change a container.
Answers are built by code, not the model: they're instant and can't be made up.
"""
import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from mcstatus import JavaServer

from . import Context, Result, Tool

log = logging.getLogger(__name__)

# Exit codes that mean "someone stopped it", not "it crashed": clean exit, SIGTERM, SIGINT.
STOPPED_ON_PURPOSE = {0, 130, 143}


# --- Minecraft ------------------------------------------------------------------------

def parse_servers(spec: str) -> dict[str, str]:
    """'getgud=host:25565,kimzamwrld=host:25566' -> {'getgud': 'host:25565', ...} (order kept)."""
    out = {}
    for item in filter(None, (s.strip() for s in spec.split(","))):
        name, _, addr = item.partition("=")
        out[name.strip().lower()] = addr.strip()
    return out


@dataclass
class MCStatus:
    name: str
    online: bool
    players: list[str] = None
    count: int = 0
    max: int = 0
    version: str = ""
    latency_ms: float = 0.0

    def describe(self) -> str:
        if not self.online:
            return f"🔴 {self.name} isn't answering."
        if not self.count:
            who = "nobody online"
        else:
            names = ", ".join(sorted(self.players, key=str.lower))
            more = self.count - len(self.players)  # the ping lists at most ~12 names
            who = f"{self.count} online: {names}" + (f" and {more} more" if more > 0 else "")
        return f"🟢 {self.name}: {who} ({self.count}/{self.max}, {self.version}, {self.latency_ms:.0f} ms)"


async def ping(name: str, address: str, timeout: float = 3.0) -> MCStatus:
    try:
        server = await JavaServer.async_lookup(address, timeout=timeout)  # async: SRV/DNS can't block the loop
        status = await asyncio.wait_for(server.async_status(), timeout + 1)
    except Exception as e:  # refused, timeout, bad handshake: all mean "not answering"
        log.info("minecraft %s (%s) not answering: %r", name, address, e)
        return MCStatus(name, online=False)
    p = status.players
    return MCStatus(name, True, [s.name for s in (p.sample or []) if s.name and s.id != "00000000-0000-0000-0000-000000000000"],
                    p.online, p.max, status.version.name, status.latency)


def pick_servers(servers: dict[str, str], wanted: str) -> dict[str, str]:
    """Which servers a request is about. Unknown or empty -> all of them."""
    w = (wanted or "").lower()
    hits = {n: a for n, a in servers.items() if n in w or (w and w in n)}
    return hits or servers


async def mc_report(servers: dict[str, str], wanted: str = "") -> str:
    chosen = pick_servers(servers, wanted)
    results = await asyncio.gather(*(ping(n, a) for n, a in chosen.items()))
    return "\n".join(r.describe() for r in results)


# --- Docker ---------------------------------------------------------------------------

@dataclass
class Container:
    name: str
    project: str
    state: str    # running, exited, restarting, paused, created, dead
    status: str   # "Up 4 days (healthy)", "Exited (2) 7 hours ago"
    id: str = ""

    @property
    def health(self) -> str | None:
        m = re.search(r"\((healthy|unhealthy|health: starting)\)", self.status)
        return m[1] if m else None

    @property
    def exit_code(self) -> int | None:
        m = re.match(r"Exited \((-?\d+)\)", self.status)
        return int(m[1]) if m else None

    def problem(self) -> str | None:
        """Why this container needs attention, or None. A clean stop is not a problem."""
        if self.health == "unhealthy":
            return "unhealthy"
        if self.state == "restarting":
            return "restarting (crash loop?)"
        if self.state == "dead":
            return "dead"
        if self.state == "exited" and self.exit_code not in STOPPED_ON_PURPOSE:
            return f"exited with code {self.exit_code}"
        return None

    def icon(self) -> str:
        if self.problem():
            return "🔴"
        if self.state != "running":
            return "⚪"
        return "🟡" if self.health == "health: starting" else "🟢"


class Docker:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None):
        # One client for the bot's lifetime: the connection to the proxy is reused.
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=10)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, **params):
        r = await self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def containers(self) -> list[Container]:
        rows = await self._get("/containers/json", all="true")
        return sorted((Container(name=r["Names"][0].lstrip("/"),
                                 project=r.get("Labels", {}).get("com.docker.compose.project", ""),
                                 state=r["State"], status=r["Status"], id=r["Id"]) for r in rows),
                      key=lambda c: (c.project or "~", c.name))

    async def memory(self, container: Container) -> str:
        """'412 MiB / 13 GiB' for a running container, '' if unknown."""
        try:
            s = await self._get(f"/containers/{container.id}/stats", stream="false", **{"one-shot": "true"})
            mem = s.get("memory_stats", {})
            used = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
            return f"{used / 2**20:,.0f} MiB / {mem.get('limit', 0) / 2**30:.1f} GiB" if used > 0 else ""
        except Exception:
            return ""


def stack_summary(containers: list[Container]) -> str:
    """One line per compose project, problems spelled out."""
    projects: dict[str, list[Container]] = {}
    for c in containers:
        projects.setdefault(c.project or "(no stack)", []).append(c)
    running = sum(c.state == "running" for c in containers)
    problems = [c for c in containers if c.problem()]
    lines = [f"🐳 {len(containers)} containers: {running} running, {len(containers) - running} stopped"
             + (f", {len(problems)} need attention" if problems else ", all fine")]
    for name, cs in projects.items():
        up = sum(c.state == "running" for c in cs)
        bad = [c for c in cs if c.problem()]
        icon = "🔴" if bad else ("🟢" if up == len(cs) else ("⚪" if up == 0 else "🟡"))
        detail = "; ".join(f"{c.name} {c.problem()}" for c in bad)
        state = "stopped" if up == 0 else f"{up}/{len(cs)} up"
        lines.append(f"{icon} {name}: {state}" + (f" ({detail})" if detail else ""))
    return "\n".join(lines)


# Words in "is nextcloud healthy?" that never name a container.
NOT_NAMES = {"how", "are", "the", "doing", "healthy", "health", "status", "running", "stack", "stacks",
             "container", "containers", "docker", "server", "servers", "down", "okay", "what", "check",
             "all", "everything", "semua", "my", "is", "ok", "up", "any", "fine", "still", "alive"}


def match_containers(containers: list[Container], wanted: str) -> list[Container]:
    words = [w for w in re.findall(r"[\w-]+", (wanted or "").lower()) if len(w) >= 3 and w not in NOT_NAMES]
    if not words:
        return []
    return [c for c in containers if any(x in c.name.lower() or x == c.project.lower() for x in words)]


MC_WORDS = re.compile(r"\b(minecraft|mc|getgud|kimzamwrld|online|players?|main|siapa)\b", re.I)


def guess(text: str) -> tuple[str, dict]:
    """No tool call from the model: decide in code, never let it guess a status."""
    if MC_WORDS.search(text):
        return "mc_status", {"server": text}
    return "docker_status", {"name": text}


async def docker_report(docker: Docker, wanted: str = "") -> str:
    containers = await docker.containers()
    hits = match_containers(containers, wanted)
    if not wanted.strip() or not hits:
        prefix = f"No container matches “{wanted}”.\n" if wanted.strip() and not hits else ""
        return prefix + stack_summary(containers)
    hits = hits[:15]
    # RAM only for a handful of containers; stats take a second or two each, so fetch them at once.
    with_ram = [c for c in hits if c.state == "running"] if len(hits) <= 5 else []
    ram = dict(zip((c.id for c in with_ram), await asyncio.gather(*(docker.memory(c) for c in with_ram))))
    lines = []
    for c in hits:
        mem = ram.get(c.id, "")
        problem = f" ⚠️ {c.problem()}" if c.problem() else ""
        lines.append(f"{c.icon()} {c.name} ({c.project or 'no stack'}): {c.status}{problem}"
                     + (f", RAM {mem}" if mem else ""))
    return "\n".join(lines)


# --- tools ----------------------------------------------------------------------------

def tools(ctx: Context, docker: Docker | None = None) -> list[Tool]:
    servers = parse_servers(ctx.settings.mc_servers)
    url = ctx.settings.docker_proxy_url
    docker = docker or (Docker(url) if url else None)

    async def mc_status(args: dict, ctx: Context) -> Result:
        if not servers:
            return Result("No Minecraft servers are configured (MC_SERVERS).")
        return Result(await mc_report(servers, args.get("server") or ""))

    async def docker_status(args: dict, ctx: Context) -> Result:
        if docker is None:
            return Result("Docker status isn't set up (DOCKER_PROXY_URL).")
        try:
            return Result(await docker_report(docker, args.get("name") or ""))
        except httpx.HTTPError as e:
            log.warning("docker proxy failed: %r", e)
            return Result("I couldn't reach Docker (is the nhako-docker-proxy container running?).")

    names = ", ".join(servers) or "none configured"
    return [
        Tool("mc_status", f"Minecraft server status and who is online. Servers: {names}.",
             {"server": f"server name ({names}); empty for all"}, mc_status),
        Tool("docker_status", "Health and uptime of the user's Docker containers and compose stacks.",
             {"name": "optional container or stack name, e.g. 'nextcloud'; empty for an overview"}, docker_status),
    ]
