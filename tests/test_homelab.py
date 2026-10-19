"""Homelab: container states, stack summaries, Minecraft status and the alert monitor."""
import httpx
import pytest

from nhako.monitor import Monitor
from nhako.tools import homelab
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
