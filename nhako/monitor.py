"""Homelab watchdog: tell the user when something breaks, and when it recovers.

Only *changes* are reported, and the last known state lives in SQLite, so a bot restart
doesn't repeat old alerts. A clean `docker stop` is not a problem (see homelab.Container).
"""
import json
import logging

from .store import Store
from .tools.homelab import Docker, ping

log = logging.getLogger(__name__)

STATE_KEY = "monitor_state"
MC_FAILS_BEFORE_ALERT = 2      # one missed ping is often just a lag spike or a restart
OLLAMA_FAILS_BEFORE_ALERT = 2  # same for Ollama: a model load can stall one check


class Monitor:
    def __init__(self, store: Store, docker: Docker | None, mc_servers: dict[str, str],
                 mc_alert: set[str], ignore: set[str], llm=None):
        self.store, self.docker, self.llm = store, docker, llm
        self.mc = {n: a for n, a in mc_servers.items() if n in mc_alert}
        self.ignore = ignore

    async def check(self) -> list[str]:
        state = json.loads(await self.store.get(STATE_KEY) or "{}")
        alerts: list[str] = []
        if self.docker is not None:
            alerts += await self._containers(state)
        alerts += await self._minecraft(state)
        if self.llm is not None:
            alerts += await self._ollama(state)
        await self.store.set(STATE_KEY, json.dumps(state))
        return alerts

    async def _containers(self, state: dict) -> list[str]:
        try:
            containers = await self.docker.containers()
        except Exception as e:  # proxy down: say nothing rather than guess
            log.warning("monitor: docker unreachable: %r", e)
            return []
        before: dict[str, str] = state.get("containers", {})
        now = {c.name: p for c in containers if c.name not in self.ignore and (p := c.problem())}
        alerts = [f"🔴 {name}: {problem}" for name, problem in now.items() if before.get(name) != problem]
        alerts += [f"✅ {name}: no longer {problem}" for name, problem in before.items() if name not in now]
        state["containers"] = now
        return alerts

    async def _minecraft(self, state: dict) -> list[str]:
        fails: dict[str, int] = state.get("mc_fails", {})
        down: list[str] = state.get("mc_down", [])
        alerts = []
        for name, address in self.mc.items():
            status = await ping(name, address)
            if status.online:
                fails[name] = 0
                if name in down:
                    down.remove(name)
                    alerts.append(f"✅ Minecraft {name} is back ({status.count}/{status.max} online)")
            else:
                fails[name] = fails.get(name, 0) + 1
                if fails[name] >= MC_FAILS_BEFORE_ALERT and name not in down:
                    down.append(name)
                    alerts.append(f"🔴 Minecraft {name} isn't answering")
        state["mc_fails"], state["mc_down"] = fails, down
        return alerts

    async def _ollama(self, state: dict) -> list[str]:
        """The bot's own brain: without this, a dead Ollama only shows up when the user asks something."""
        problem = await self.llm.health()
        if problem is None:
            state["ollama_fails"] = 0
            if state.pop("ollama_down", False):
                return ["✅ Ollama is back, I can think again"]
            return []
        state["ollama_fails"] = state.get("ollama_fails", 0) + 1
        if state["ollama_fails"] >= OLLAMA_FAILS_BEFORE_ALERT and not state.get("ollama_down"):
            state["ollama_down"] = True
            return [f"🔴 Ollama: {problem}. Commands like /today and /mc still work."]
        return []
