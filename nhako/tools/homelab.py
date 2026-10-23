"""Homelab: Minecraft servers (server-list ping) and Docker containers (status, logs, control).

Docker is reached through a socket proxy (compose.yaml) that allows exactly: list containers,
one container's stats and logs, and start/stop/restart/pause/unpause. Nothing can create,
delete, exec into or reconfigure a container. Which container a command is about, and
whether it may run, is decided by code, never by the model; stopping, restarting and
pausing wait for a Confirm tap, and the bot's own containers can't be touched.
Answers are built by code, not the model: they're instant and can't be made up.
"""
import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from mcstatus import JavaServer

from . import Context, NeedsConfirm, Result, Tool

log = logging.getLogger(__name__)

STOP_TIMEOUT = 10  # seconds docker waits for a clean stop before killing
LOG_LINES = 30
LOG_CHARS = 3500   # what's shown of the log tail, to stay inside one Telegram message
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Containers the bot never stops, pauses or restarts: itself, and its only way to reach Docker.
PROTECTED = {"nhako", "nhako-docker-proxy"}
# verb -> the states it makes sense in
VALID = {"start": {"exited", "created"}, "stop": {"running", "restarting", "paused"},
         "restart": {"running", "exited", "created", "restarting"}, "pause": {"running"}, "unpause": {"paused"}}
CONFIRM = {"stop", "restart", "pause"}  # start and unpause run straight away
DONE = {"start": "started", "stop": "stopped", "restart": "restarted", "pause": "paused", "unpause": "resumed"}
# Exit codes that mean "someone stopped it", not "it crashed": clean exit, SIGTERM, SIGINT.
STOPPED_ON_PURPOSE = {0, 130, 143}
# Servers that hide player names list placeholders with this id.
ANONYMOUS = "00000000-0000-0000-0000-000000000000"


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
    names = [s.name for s in (p.sample or []) if s.name and s.id != ANONYMOUS]
    return MCStatus(name, True, names, p.online, p.max, status.version.name, status.latency)


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


def demux(raw: bytes) -> str:
    """A container without a TTY sends logs as frames: 1 byte stream, 3 zero bytes, 4 bytes length."""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\0\0\0":
        out, i = [], 0
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        raw = b"".join(out)
    return ANSI.sub("", raw.decode("utf-8", "replace"))


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

    async def action(self, container: Container, verb: str) -> None:
        """POST start/stop/restart/pause/unpause. 304 (already in that state) is fine too."""
        params = {"t": STOP_TIMEOUT} if verb in ("stop", "restart") else {}
        r = await self.client.post(f"/containers/{container.id}/{verb}", params=params, timeout=STOP_TIMEOUT + 30)
        if r.status_code != 304:
            r.raise_for_status()

    async def logs(self, container: Container, lines: int = LOG_LINES) -> str:
        r = await self.client.get(f"/containers/{container.id}/logs",
                                  params={"stdout": "1", "stderr": "1", "tail": str(lines)})
        r.raise_for_status()
        return demux(r.content)

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
    if parse_command(text):  # docker_control re-reads the verb and name from the text itself
        return "docker_control", {}
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
    ram = dict(zip([c.id for c in with_ram], await asyncio.gather(*(docker.memory(c) for c in with_ram)), strict=True))
    lines = []
    for c in hits:
        mem = ram.get(c.id, "")
        problem = f" ⚠️ {c.problem()}" if c.problem() else ""
        lines.append(f"{c.icon()} {c.name} ({c.project or 'no stack'}): {c.status}{problem}"
                     + (f", RAM {mem}" if mem else ""))
    return "\n".join(lines)


# --- control ---------------------------------------------------------------------------

# Words for each verb, longest first so "unpause" isn't read as "pause".
VERBS = [("restart", r"restart|reboot"), ("unpause", r"unpause|resume|sambung"),
         ("start", r"start|boot|turn on|bring up|hidupkan"), ("stop", r"stop|shut ?down|turn off|matikan"),
         ("pause", r"pause"), ("logs", r"logs?")]
FILLER = r"(?:(?:the|my|up|down|container|server|stack|for|of|for me)\s+)*"
COMMAND = [(verb, re.compile(rf"\b(?:{words})\s+{FILLER}([\w.-]+)", re.I)) for verb, words in VERBS]
LOGS_AFTER = re.compile(r"\b([\w.-]+)(?:'s)?\s+(?:container\s+)?logs?\b", re.I)  # "show postiz logs"
STACK_WORDS = re.compile(r"\b(stack|project|all|everything|whole|semua)\b", re.I)
STATE_FILTERS = {"running": {"running", "restarting"}, "up": {"running", "restarting"}, "paused": {"paused"},
                 "stopped": {"exited", "dead"}, "exited": {"exited", "dead"}, "created": {"created"}}


def parse_command(text: str) -> list[tuple[str, str]]:
    """'please restart the nextcloud stack' -> [('restart', 'nextcloud')]. Every reading, best first;
    which one names a real container is for `resolve` to decide."""
    found = [(verb, m[1].lower()) for verb, rx in COMMAND for m in rx.finditer(text or "")]
    found += [("logs", m[1].lower()) for m in LOGS_AFTER.finditer(text or "")]
    return found


def resolve(containers: list[Container], target: str, whole_stack: bool = False) -> list[Container]:
    """Which containers a name means: the container with that exact name, or (asked for a stack, or
    no container has the name) every container of the compose project; else name prefixes."""
    t = target.lower()
    exact = [c for c in containers if c.name.lower() == t]
    stack = [c for c in containers if c.project.lower() == t]
    prefix = [c for c in containers if len(t) >= 4 and c.name.lower().startswith(t)]
    if whole_stack and (stack or prefix):
        project = (stack or prefix)[0].project
        return [c for c in containers if project and c.project == project] or stack or prefix
    return exact or stack or prefix


async def find_command(docker: Docker, text: str, verb: str = "", name: str = ""):
    """-> (verb, containers, containers list) for the first reading that names a real container."""
    containers = await docker.containers()
    readings = parse_command(text) or ([(verb, name.lower())] if verb and name else [])
    for v, target in readings:
        if hits := resolve(containers, target, bool(STACK_WORDS.search(text))):
            return v, hits, containers
    return None, [], containers


def check_targets(verb: str, targets: list[Container], protected: set[str]) -> tuple[list[Container], list[str]]:
    """Split into (the ones to act on, why each of the others is skipped)."""
    ok, skipped = [], []
    for c in targets:
        if c.name in protected:
            skipped.append(f"🔒 {c.name} is protected; I won't {verb} it from here.")
        elif c.state not in VALID[verb]:
            skipped.append(f"⚪ {c.name} is {c.state}; nothing to {verb}.")
        else:
            ok.append(c)
    return ok, skipped


async def run_action(docker: Docker, verb: str, targets: list[Container]) -> str:
    lines = []
    for c in targets:
        try:
            await docker.action(c, verb)
        except httpx.HTTPError as e:
            log.warning("docker %s %s failed: %r", verb, c.name, e)
            lines.append(f"❌ {c.name}: couldn't {verb} it ({getattr(getattr(e, 'response', None), 'status_code', e)})")
    fresh = {c.id: c for c in await docker.containers()}
    for c in targets:
        if not any(line.startswith(f"❌ {c.name}:") for line in lines):
            now = fresh.get(c.id, c)
            lines.append(f"{now.icon()} {c.name} {DONE[verb]}: {now.status}")
    return "\n".join(lines)


def logs_text(name: str, text: str) -> str:
    text = text.strip() or "(nothing logged)"
    if len(text) > LOG_CHARS:
        text = "…" + text[-LOG_CHARS:].split("\n", 1)[-1]
    return f"📜 {name}, last lines:\n{text}"


def filter_containers(containers: list[Container], state: str) -> list[Container]:
    states = STATE_FILTERS.get(state.lower().strip())
    return [c for c in containers if states is None or c.state in states]


def protected_names(settings) -> set[str]:
    return PROTECTED | {n.strip() for n in settings.docker_protected.split(",") if n.strip()}


def claim(docker: Docker | None):
    """'restart nextcloud', 'pause postiz', 'show vaultwarden logs' -> homelab, decided in code, when the
    name is a real container or stack. 'pause NeraOS' (a Notion project) falls through, and so does
    'pause Nhako Beam': the bot's own (protected) containers are never a reason to claim."""
    async def check(text: str, ctx: Context) -> str | None:
        if docker is None or not parse_command(text):
            return None
        try:
            _, hits, _ = await find_command(docker, text)
        except httpx.HTTPError as e:
            log.warning("docker unreachable while checking %r: %r", text[:60], e)
            return None
        protected = protected_names(ctx.settings)
        return "homelab" if any(c.name not in protected for c in hits) else None
    return check


# --- tools ----------------------------------------------------------------------------

def tools(ctx: Context, docker: Docker | None = None) -> list[Tool]:
    servers = parse_servers(ctx.settings.mc_servers)
    url = ctx.settings.docker_proxy_url
    docker = docker or (Docker(url) if url else None)
    protected = protected_names(ctx.settings)

    async def mc_status(args: dict, ctx: Context) -> Result:
        if not servers:
            return Result("No Minecraft servers are configured (MC_SERVERS).")
        return Result(await mc_report(servers, args.get("server") or ""))

    def guarded(fn):
        async def run(args: dict, ctx: Context):
            if docker is None:
                return Result("Docker isn't set up (DOCKER_PROXY_URL).")
            try:
                return await fn(args, ctx)
            except httpx.HTTPError as e:
                log.warning("docker proxy failed: %r", e)
                return Result("I couldn't reach Docker (is the nhako-docker-proxy container running?).")
        return run

    @guarded
    async def docker_status(args: dict, ctx: Context) -> Result:
        if parse_command(ctx.text):  # the model picked status for "restart nextcloud": do what was asked
            return await docker_control(args, ctx)
        state = (args.get("state") or "").lower()
        if state in STATE_FILTERS:
            hits = filter_containers(await docker.containers(), state)
            if not hits:
                return Result(f"🐳 No {state} containers.")
            return Result(f"🐳 {len(hits)} {state}:\n" + "\n".join(f"{c.icon()} {c.name}: {c.status}" for c in hits))
        return Result(await docker_report(docker, args.get("name") or ""))

    @guarded
    async def docker_control(args: dict, ctx: Context) -> Result | NeedsConfirm:
        verb = (args.get("action") or "").lower()
        verb = {"resume": "unpause", "reboot": "restart"}.get(verb, verb)
        # The verb must come from the user's own words: a status question never stops anything.
        if ctx.text and not parse_command(ctx.text):
            return Result(await docker_report(docker, args.get("name") or ""))
        verb, targets, containers = await find_command(docker, ctx.text, verb, args.get("name") or "")
        if not targets:
            names = ", ".join(c.name for c in containers[:40])
            return Result(f"I couldn't tell which container you mean. I have: {names}")
        if verb == "logs":
            if len(targets) > 1:
                return Result("Logs of which one? " + ", ".join(c.name for c in targets))
            return Result(logs_text(targets[0].name, await docker.logs(targets[0])))
        if verb not in VALID:
            return Result(await docker_report(docker, targets[0].name))
        ok, skipped = check_targets(verb, targets, protected)
        if not ok:
            return Result("\n".join(skipped))
        note = ("\n" + "\n".join(skipped)) if skipped else ""
        if verb in CONFIRM:
            what = ok[0].name if len(ok) == 1 else f"{len(ok)} containers ({', '.join(c.name for c in ok)})"
            return NeedsConfirm(f"🐳 {verb.capitalize()} {what}?{note}", {"verb": verb, "ids": [c.id for c in ok]})
        return Result(await run_action(docker, verb, ok) + note)

    async def control_execute(args: dict, ctx: Context) -> Result:
        targets = [c for c in await docker.containers() if c.id in args["ids"]]
        ok, skipped = check_targets(args["verb"], targets, protected)  # it may have changed meanwhile
        out = await run_action(docker, args["verb"], ok) if ok else ""
        return Result("\n".join(filter(None, [out, *skipped])) or "Those containers are gone.")

    @guarded
    async def docker_logs(args: dict, ctx: Context) -> Result:
        lines = int(args["lines"]) if (args.get("lines") or "").isdigit() else LOG_LINES
        containers = await docker.containers()
        targets = resolve(containers, args.get("name") or "")
        if not targets:
            _, targets, _ = await find_command(docker, ctx.text)
        if len(targets) != 1:
            return Result("Logs of which container? " + ", ".join(c.name for c in (targets or containers)[:40]))
        return Result(logs_text(targets[0].name, await docker.logs(targets[0], min(lines, 200))))

    names = ", ".join(servers) or "none configured"
    return [
        Tool("mc_status", f"Minecraft server status and who is online. Servers: {names}.",
             {"server": f"server name ({names}); empty for all"}, mc_status),
        Tool("docker_status", "Health and uptime of the user's Docker containers and compose stacks, "
             "or a list of the running / paused / stopped / created ones.",
             {"name": "optional container or stack name, e.g. 'nextcloud'; empty for an overview",
              "state": "optional: running, paused, stopped or created"}, docker_status),
        Tool("docker_control", "Start, stop, restart, pause or resume (unpause) a Docker container or a whole "
             "compose stack.",
             {"name": "container or stack name, e.g. 'nextcloud'",
              "action": "start, stop, restart, pause or unpause"}, docker_control, required=("name", "action"),
             execute=control_execute),
        Tool("docker_logs", "The last lines of a Docker container's log.",
             {"name": "container name", "lines": "optional number of lines, default 30"}, docker_logs,
             required=("name",)),
    ]
