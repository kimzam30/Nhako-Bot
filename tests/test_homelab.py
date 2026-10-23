"""Homelab: container states, stack summaries, Minecraft status and the alert monitor."""
import httpx
import pytest

from nhako.monitor import Monitor
from nhako.tools import NeedsConfirm, homelab
from nhako.tools.homelab import Container, Docker, MCStatus

# A snapshot shaped like the real `docker ps -a` on kimmain (2026-10-19).
PS = [
    ("mc-server", "minecraft", "running", "Up 8 hours (healthy)"),
    ("mc-backup", "minecraft", "running", "Up 4 days (healthy)"),
    ("nextcloud", "nextcloud", "running", "Up 4 days (healthy)"),
    ("nextcloud-db", "nextcloud", "running", "Up 4 days (healthy)"),
    ("kimzam-server", "kimzamwrld", "exited", "Exited (0) 36 hours ago"),
    ("kimzam-backup", "kimzamwrld", "exited", "Exited (255) 36 hours ago"),
    ("portainer", "portainer", "exited", "Exited (2) 7 hours ago"),
    ("homepage", "homepage", "exited", "Exited (143) 7 hours ago"),
    ("postiz", "postiz", "running", "Up 4 days (unhealthy)"),
    ("getgud-staging", "", "exited", "Exited (0) 3 days ago"),
]


def api_rows(ps):
    return [{"Id": f"id-{n}", "Names": [f"/{n}"], "State": st, "Status": status,
             "Labels": {"com.docker.compose.project": p} if p else {}} for n, p, st, status in ps]


def fake_docker(ps=PS, mem=None, fail=False):
    def handler(req: httpx.Request):
        if fail:
            return httpx.Response(503)
        if req.url.path == "/containers/json":
            assert req.method == "GET" and req.url.params["all"] == "true"
            return httpx.Response(200, json=api_rows(ps))
        if req.url.path.endswith("/stats"):
            return httpx.Response(200, json={"memory_stats": mem or {}})
        return httpx.Response(404)
    return Docker("http://proxy:2375", transport=httpx.MockTransport(handler))


def c(status, state="running"):
    return Container("x", "p", state, status)


@pytest.mark.parametrize("container, problem", [
    (c("Up 4 days (healthy)"), None),
    (c("Up 2 minutes"), None),
    (c("Up 1 minute (health: starting)"), None),
    (c("Up 4 days (unhealthy)"), "unhealthy"),
    (c("Restarting (1) 3 seconds ago", "restarting"), "restarting (crash loop?)"),
    (c("Exited (0) 3 days ago", "exited"), None),          # docker stop / clean exit
    (c("Exited (143) 7 hours ago", "exited"), None),       # SIGTERM from docker stop
    (c("Exited (2) 7 hours ago", "exited"), "exited with code 2"),
    (c("Exited (137) 1 hour ago", "exited"), "exited with code 137"),  # killed / OOM
])
def test_container_problem(container, problem):
    assert container.problem() == problem


def test_stack_summary():
    containers = [Container(n, p, st, s) for n, p, st, s in PS]
    text = homelab.stack_summary(containers)
    lines = text.splitlines()
    assert lines[0] == "🐳 10 containers: 5 running, 5 stopped, 3 need attention"
    assert "🟢 minecraft: 2/2 up" in lines
    assert "🔴 kimzamwrld: stopped (kimzam-backup exited with code 255)" in lines
    assert "⚪ homepage: stopped" in lines
    assert "🔴 postiz: 1/1 up (postiz unhealthy)" in lines
    assert "⚪ (no stack): stopped" in lines


async def test_docker_report_overview():
    text = await homelab.docker_report(fake_docker())
    assert text.startswith("🐳 10 containers")


async def test_docker_report_one_stack_with_memory():
    mem = {"usage": 600 * 2**20, "limit": 13 * 2**30, "stats": {"inactive_file": 100 * 2**20}}
    text = await homelab.docker_report(fake_docker(mem=mem), "is nextcloud healthy?")
    assert "🟢 nextcloud (nextcloud): Up 4 days (healthy), RAM 500 MiB / 13.0 GiB" in text
    assert "nextcloud-db" in text and "mc-server" not in text


async def test_docker_report_unknown_name_falls_back_to_overview():
    text = await homelab.docker_report(fake_docker(), "jellyfin")
    assert text.startswith("No container matches “jellyfin”.\n🐳")


async def test_docker_tool_without_proxy(ctx):
    tool = {t.name: t for t in homelab.tools(ctx)}["docker_status"]
    assert "isn't set up" in (await tool.run({}, ctx)).text


async def test_docker_tool_proxy_down(ctx):
    tool = {t.name: t for t in homelab.tools(ctx, fake_docker(fail=True))}["docker_status"]
    assert "couldn't reach Docker" in (await tool.run({}, ctx)).text


# --- Minecraft ---

def test_parse_and_pick_servers():
    servers = homelab.parse_servers("getgud=host:25565, kimzamwrld=host:25566")
    assert servers == {"getgud": "host:25565", "kimzamwrld": "host:25566"}
    assert list(homelab.pick_servers(servers, "who's on getgud")) == ["getgud"]
    assert list(homelab.pick_servers(servers, "")) == ["getgud", "kimzamwrld"]
    assert list(homelab.pick_servers(servers, "the minecraft server")) == ["getgud", "kimzamwrld"]


def test_mc_describe():
    s = MCStatus("getgud", True, ["majorvazor", "desh0_0"], 2, 15, "26.2", 1.2)
    assert s.describe() == "🟢 getgud: 2 online: desh0_0, majorvazor (2/15, 26.2, 1 ms)"
    assert "nobody online" in MCStatus("getgud", True, [], 0, 15, "26.2", 1).describe()
    assert "and 3 more" in MCStatus("getgud", True, ["a"] * 12, 15, 15, "26.2", 1).describe()
    assert MCStatus("kimzamwrld", False).describe() == "🔴 kimzamwrld isn't answering."


async def test_mc_ping_of_closed_port_is_offline():
    status = await homelab.ping("nothing", "127.0.0.1:1", timeout=1)
    assert not status.online


# --- monitor ---

class FlakyMC:
    def __init__(self, *answers):
        self.answers = list(answers)

    async def __call__(self, name, address, timeout=3.0):
        online = self.answers.pop(0)
        return MCStatus(name, online, [], 1 if online else 0, 15, "26.2", 1)


async def test_monitor_reports_changes_only(store, monkeypatch):
    monkeypatch.setattr("nhako.monitor.ping", FlakyMC(True, True, True))
    m = Monitor(store, fake_docker(), {"getgud": "h:1"}, {"getgud"}, ignore=set())
    first = await m.check()
    assert sorted(first) == ["🔴 kimzam-backup: exited with code 255", "🔴 portainer: exited with code 2",
                             "🔴 postiz: unhealthy"]
    assert await m.check() == []  # nothing changed

    healed = [row for row in PS if row[0] != "portainer"]
    m.docker = fake_docker(healed)
    assert await m.check() == ["✅ portainer: no longer exited with code 2"]


async def test_monitor_survives_restart(store, monkeypatch):
    monkeypatch.setattr("nhako.monitor.ping", FlakyMC(True, True))
    await Monitor(store, fake_docker(), {}, set(), set()).check()
    assert await Monitor(store, fake_docker(), {}, set(), set()).check() == []


async def test_monitor_ignore_list(store):
    alerts = await Monitor(store, fake_docker(), {}, set(), ignore={"portainer", "kimzam-backup"}).check()
    assert alerts == ["🔴 postiz: unhealthy"]


async def test_monitor_proxy_down_keeps_quiet_and_state(store):
    await Monitor(store, fake_docker(), {}, set(), set()).check()
    assert await Monitor(store, fake_docker(fail=True), {}, set(), set()).check() == []
    assert await Monitor(store, fake_docker(), {}, set(), set()).check() == []  # no re-alerts after


async def test_monitor_minecraft_needs_two_misses(store, monkeypatch):
    monkeypatch.setattr("nhako.monitor.ping", FlakyMC(False, False, False, True))
    m = Monitor(store, None, {"getgud": "h:1", "kimzamwrld": "h:2"}, {"getgud"}, set())
    assert await m.check() == []                                   # one miss: could be a lag spike
    assert await m.check() == ["🔴 Minecraft getgud isn't answering"]
    assert await m.check() == []                                   # still down: no repeat
    assert await m.check() == ["✅ Minecraft getgud is back (1/15 online)"]


@pytest.mark.parametrize("text, tool", [
    ("is nextcloud healthy?", "docker_status"), ("how are my stacks doing", "docker_status"),
    ("siapa main getgud", "mc_status"), ("any players online?", "mc_status"),
])
def test_guess_when_model_makes_no_tool_call(text, tool):
    assert homelab.guess(text)[0] == tool


def test_match_ignores_question_words():
    containers = [Container(n, p, st, s) for n, p, st, s in PS]
    assert [c.name for c in homelab.match_containers(containers, "is nextcloud healthy?")] == \
        ["nextcloud", "nextcloud-db"]
    assert homelab.match_containers(containers, "how are my stacks doing") == []


async def test_agent_uses_fallback_instead_of_chat(ctx, store):
    from nhako.agent import Agent
    from nhako.tools import default_registry

    from .conftest import FakeLLM
    reg = default_registry(ctx, docker=fake_docker())
    llm = FakeLLM("homelab", tool=None, text="Nextcloud is perfectly healthy!")  # would be made up
    reply = await Agent(llm, store, reg, ctx).handle(1, "is nextcloud healthy?")
    assert reply.text.startswith("🟢 nextcloud (nextcloud): Up 4 days (healthy)")


class FakeLLMHealth:
    def __init__(self, *answers):
        self.answers = list(answers)

    async def health(self):
        return self.answers.pop(0)


async def test_monitor_alerts_when_ollama_goes_down_and_comes_back(store):
    m = Monitor(store, None, {}, set(), set(), llm=FakeLLMHealth("can't reach Ollama", "can't reach Ollama", None))
    assert await m.check() == []  # one failed check is just a blip
    assert await m.check() == ["🔴 Ollama: can't reach Ollama. Commands like /today and /mc still work."]
    assert await m.check() == ["✅ Ollama is back, I can think again"]


# --- control: start/stop/restart/pause/unpause, logs, routing ---

CONTROL_PS = PS + [("nhako", "nhako", "running", "Up 8 hours (healthy)"),
                   ("postiz-redis", "postiz", "running", "Up 4 days (healthy)"),
                   ("beszel", "beszel", "paused", "Up 2 hours (Paused)"),
                   ("rustdesk-hbbs", "rustdesk", "created", "Created")]


class FakeDockerAPI:
    """List, logs and actions; an action really changes the state, like Docker would."""
    def __init__(self, ps=CONTROL_PS, log=b""):
        self.rows = {n: [n, p, st, s] for n, p, st, s in ps}
        self.log, self.actions = log, []

    def __call__(self, req: httpx.Request):
        path = req.url.path
        if path == "/containers/json":
            return httpx.Response(200, json=api_rows([tuple(r) for r in self.rows.values()]))
        name = path.split("/")[2].removeprefix("id-")
        if path.endswith("/logs"):
            assert req.url.params["tail"] and req.url.params["stderr"] == "1"
            return httpx.Response(200, content=self.log)
        verb = path.rsplit("/", 1)[1]
        assert req.method == "POST" and name in self.rows
        self.actions.append((verb, name, dict(req.url.params)))
        state = {"start": "running", "restart": "running", "unpause": "running", "stop": "exited", "pause": "paused"}[verb]
        self.rows[name][2:] = [state, {"exited": "Exited (0) Less than a second ago",
                                       "paused": "Up 4 days (Paused)"}.get(state, "Up Less than a second")]
        return httpx.Response(204)


@pytest.fixture
def api():
    return FakeDockerAPI()


@pytest.fixture
def control(ctx, api):
    client = Docker("http://proxy:2375", transport=httpx.MockTransport(api))
    return client, {t.name: t for t in homelab.tools(ctx, client)}


def said(ctx, text):
    import dataclasses
    return dataclasses.replace(ctx, text=text)


@pytest.mark.parametrize("text, expected", [
    ("restart nextcloud", ("restart", "nextcloud")),
    ("please stop the postiz stack", ("stop", "postiz")),
    ("can you resume beszel", ("unpause", "beszel")),
    ("unpause beszel", ("unpause", "beszel")),
    ("turn on the homepage container", ("start", "homepage")),
    ("matikan nextcloud", ("stop", "nextcloud")),
    ("show postiz logs", ("logs", "postiz")),
    ("logs for vaultwarden", ("logs", "vaultwarden")),
])
def test_parse_command(text, expected):
    assert homelab.parse_command(text)[0] == expected


@pytest.mark.parametrize("text", ["is nextcloud stopped?", "which containers are paused", "how are my stacks",
                                  "write a blog post"])
def test_status_questions_are_not_commands(text):
    assert homelab.parse_command(text) == []


def test_resolve_exact_name_stack_and_prefix():
    containers = [Container(n, p, st, s, id=f"id-{n}") for n, p, st, s in CONTROL_PS]
    names = lambda cs: [c.name for c in cs]
    assert names(homelab.resolve(containers, "postiz")) == ["postiz"]
    assert names(homelab.resolve(containers, "postiz", whole_stack=True)) == ["postiz", "postiz-redis"]
    assert names(homelab.resolve(containers, "kimzamwrld")) == ["kimzam-server", "kimzam-backup"]
    assert names(homelab.resolve(containers, "kimzam")) == ["kimzam-server", "kimzam-backup"]
    assert homelab.resolve(containers, "neraos") == []


async def test_start_runs_straight_away(control, api, ctx):
    _, tools = control
    out = await tools["docker_control"].run({}, said(ctx, "start homepage"))
    assert out.text == "🟢 homepage started: Up Less than a second"
    assert api.actions == [("start", "homepage", {})]


async def test_stop_waits_for_confirm(control, api, ctx):
    _, tools = control
    ask = await tools["docker_control"].run({"name": "nextcloud", "action": "stop"}, said(ctx, "stop nextcloud"))
    assert isinstance(ask, NeedsConfirm) and ask.prompt == "🐳 Stop nextcloud?" and api.actions == []
    done = await tools["docker_control"].execute(ask.args, ctx)
    assert done.text == "⚪ nextcloud stopped: Exited (0) Less than a second ago"
    assert api.actions == [("stop", "nextcloud", {"t": "10"})]


async def test_whole_stack_one_confirm(control, api, ctx):
    _, tools = control
    ask = await tools["docker_control"].run({}, said(ctx, "restart the postiz stack"))
    assert ask.prompt == "🐳 Restart 2 containers (postiz, postiz-redis)?"
    await tools["docker_control"].execute(ask.args, ctx)
    assert [a[:2] for a in api.actions] == [("restart", "postiz"), ("restart", "postiz-redis")]


async def test_protected_and_wrong_state(control, api, ctx):
    _, tools = control
    assert "🔒 nhako is protected" in (await tools["docker_control"].run({}, said(ctx, "restart nhako"))).text
    assert "nextcloud is running; nothing to start" in (await tools["docker_control"].run({}, said(ctx, "start nextcloud"))).text
    assert "🟢 beszel resumed" in (await tools["docker_control"].run({}, said(ctx, "resume beszel"))).text
    assert api.actions == [("unpause", "beszel", {})]


async def test_model_args_never_act_without_a_verb_from_the_user(control, api, ctx):
    _, tools = control
    out = await tools["docker_control"].run({"name": "nextcloud", "action": "stop"}, said(ctx, "is nextcloud ok?"))
    assert out.text.startswith("🟢 nextcloud") and api.actions == []


async def test_status_tool_does_what_was_asked(control, api, ctx):
    _, tools = control
    ask = await tools["docker_status"].run({"name": "nextcloud"}, said(ctx, "pause nextcloud"))
    assert isinstance(ask, NeedsConfirm) and ask.args["verb"] == "pause"


async def test_state_filter(control, ctx):
    _, tools = control
    assert (await tools["docker_status"].run({"state": "paused"}, ctx)).text == "🐳 1 paused:\n⚪ beszel: Up 2 hours (Paused)"
    assert "rustdesk-hbbs" in (await tools["docker_status"].run({"state": "created"}, ctx)).text


def test_demux_and_ansi():
    frame = lambda stream, text: bytes([stream, 0, 0, 0]) + len(text).to_bytes(4, "big") + text
    raw = frame(1, b"\x1b[32mready\x1b[0m\n") + frame(2, b"warn: slow\n")
    assert homelab.demux(raw) == "ready\nwarn: slow\n"
    assert homelab.demux(b"plain tty output\n") == "plain tty output\n"


async def test_logs(ctx):
    api = FakeDockerAPI(log=bytes([1, 0, 0, 0, 0, 0, 0, 6]) + b"hello\n")
    tools = {t.name: t for t in homelab.tools(ctx, Docker("http://proxy:2375", transport=httpx.MockTransport(api)))}
    assert (await tools["docker_logs"].run({"name": "nextcloud"}, ctx)).text == "📜 nextcloud, last lines:\nhello"
    assert (await tools["docker_control"].run({}, said(ctx, "show vaultwarden logs"))).text.startswith("I couldn't tell")


async def test_claim_routes_real_containers_only(control, ctx):
    client, _ = control
    check = homelab.claim(client)
    assert await check("restart nextcloud", ctx) == "homelab"
    assert await check("pause NeraOS", ctx) is None            # a Notion project, not a container
    assert await check("start a new project called Nhako Radio", ctx) is None
    assert await check("pause Nhako Beam", ctx) is None        # also a project; nhako itself is protected
    assert await check("is nextcloud healthy?", ctx) is None   # status: the router handles it


def test_guess_action_goes_to_control():
    assert homelab.guess("restart nextcloud") == ("docker_control", {})
