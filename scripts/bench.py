"""Phase 0 benchmark: which small local model can run Nhako on this machine?

Each case in bench_cases.json goes through the same two steps the new bot will use:
  1. router - classify the message into one tool category (JSON-schema output)
  2. tools  - native tool call using only the *expected* category's tools, so a
              router miss doesn't hide whether the model can call the tool itself

Pure standard library, so it runs with any Python 3.10+ and no venv:
    python scripts/bench.py                      # every pulled model that supports tools
    python scripts/bench.py qwen3:4b phi4-mini   # specific models
"""
import argparse
import datetime
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TZ = datetime.timezone(datetime.timedelta(hours=8))  # Asia/Kuala_Lumpur

# Go/no-go gate from docs/REVIVAL_PLAN.md
GATE_ROUTER_ACC = 0.90
GATE_MEDIAN_SECONDS = 6.0

CATEGORIES = {
    "schedule": "calendar events and reminders: add, edit, move, delete, list, 'am I free', 'remind me'",
    "web": "needs current information from the internet, or reading/summarising a URL",
    "homelab": "the user's own servers: Minecraft server 'getgud' status/players, Docker containers/stacks health",
    "postiz": "the user's social media scheduler Postiz: scheduled posts, drafting a post",
    "notion": "the user's Notion workspace: tasks, notes, adding or querying pages",
    "chat": "anything else: general knowledge, advice, jokes, conversation. No tool needed",
}


def tool(name, description, props, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
                "required": list(required),
            },
        },
    }


# The tool set planned for the new bot, at most 5 per category.
# Times are passed as the user's own words; the bot parses them with dateparser.
TOOLS = {
    "schedule": [
        tool("list_events", "List calendar events in a time range.",
             {"range": "time range in the user's words, e.g. 'today', 'next week', 'friday afternoon'"}, ["range"]),
        tool("create_event", "Create a calendar event.",
             {"title": "event title", "when": "start time in the user's words, e.g. 'next tuesday 3pm'",
              "duration": "optional length, e.g. '2 hours'"}, ["title", "when"]),
        tool("update_event", "Change an existing event's time or title.",
             {"event": "which event, in the user's words", "new_when": "new time in the user's words, if changing time",
              "new_title": "new title, if renaming"}, ["event"]),
        tool("delete_event", "Delete one event, or all events on a date.",
             {"event": "which event, in the user's words", "date": "a whole day to clear, in the user's words"}),
        tool("set_reminder", "Send the user a reminder message at a time.",
             {"what": "what to remind about", "when": "time in the user's words, e.g. 'in 20 minutes', 'tomorrow 9am'"},
             ["what", "when"]),
    ],
    "web": [
        tool("web_search", "Search the web for current information.", {"query": "search query"}, ["query"]),
        tool("read_url", "Fetch a web page and return its readable text.", {"url": "full URL"}, ["url"]),
    ],
    "homelab": [
        tool("mc_status", "Minecraft server status and online players.",
             {"server": "server name, default 'getgud'"}),
        tool("docker_status", "Health and uptime of Docker containers, optionally filtered by name or stack.",
             {"name": "optional container or stack name"}),
    ],
    "postiz": [
        tool("postiz_list_posts", "List scheduled social media posts.", {"range": "optional time range"}),
        tool("postiz_draft_post", "Create a draft social media post.", {"content": "post text or topic"}, ["content"]),
    ],
    "notion": [
        tool("notion_add_task", "Add a task to the Notion tasks database.",
             {"title": "task title", "due": "optional due date in the user's words"}, ["title"]),
        tool("notion_query_tasks", "List Notion tasks, optionally by due range.", {"due": "optional range"}),
    ],
}

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}},
    "required": ["category"],
}


class Ollama:
    def __init__(self, host, num_ctx):
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx
        self.no_think = {}  # model -> whether "think": false is accepted

    def _post(self, path, body, timeout=300):
        req = urllib.request.Request(self.host + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _get(self, path):
        with urllib.request.urlopen(self.host + path, timeout=10) as r:
            return json.loads(r.read())

    def chat(self, model, messages, **extra):
        body = {"model": model, "messages": messages, "stream": False,
                "options": {"temperature": 0, "num_ctx": self.num_ctx}, **extra}
        if self.no_think.get(model, True):
            body["think"] = False
        try:
            return self._post("/api/chat", body)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if "think" in msg and "think" in body:  # model has no thinking mode
                self.no_think[model] = False
                return self.chat(model, messages, **extra)
            raise RuntimeError(f"HTTP {e.code}: {msg}") from None

    def tool_models(self):
        names = [m["name"] for m in self._get("/api/tags")["models"]]
        return [n for n in names if "tools" in self._post("/api/show", {"model": n}).get("capabilities", [])]

    def gpu_share(self, model):
        for m in self._get("/api/ps").get("models", []):
            if model in (m["name"], m["model"]) or m["name"] == model + ":latest":
                return m["size_vram"] / m["size"] if m["size"] else 0.0, m["size"]
        return None, None


def system_prompt():
    now = datetime.datetime.now(TZ).strftime("%A, %d %B %Y, %I:%M %p")
    return f"You are Nhako, a personal assistant. The user is in Malaysia (GMT+8). It is now {now}."


def router_messages(text):
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    return [
        {"role": "system", "content": system_prompt() +
         "\nClassify the user's message into exactly one category. Reply with JSON only.\n" + cats},
        {"role": "user", "content": text},
    ]


def args_ok(expected, actual):
    """Each expected key (or 'a|b' for either key) must contain one of the substrings.
    An empty substring list only checks the call happened."""
    actual = {k: str(v).lower() for k, v in (actual or {}).items()}
    for keys, needles in expected.items():
        values = [actual.get(k, "") for k in keys.split("|")]
        if not any(any(n.lower() in v for n in needles) for v in values):
            return False
    return True


def seconds(resp):
    return resp.get("total_duration", 0) / 1e9


def tok_per_s(resp):
    d = resp.get("eval_duration", 0)
    return resp.get("eval_count", 0) / (d / 1e9) if d else 0.0


def prompt_tok_per_s(resp):
    d = resp.get("prompt_eval_duration", 0)
    return resp.get("prompt_eval_count", 0) / (d / 1e9) if d else 0.0


def run_case(client, model, case):
    out = {"id": case["id"], "lang": case.get("lang", "en"), "expected_category": case["category"]}

    r = client.chat(model, router_messages(case["text"]), format=ROUTER_SCHEMA)
    try:
        out["category"] = json.loads(r["message"]["content"]).get("category")
    except (json.JSONDecodeError, AttributeError):
        out["category"] = None
    out["router_ok"] = out["category"] == case["category"]
    out["router_s"] = seconds(r)
    timings = [r]

    if case["category"] != "chat":
        r = client.chat(model, [{"role": "system", "content": system_prompt()},
                                {"role": "user", "content": case["text"]}],
                        tools=TOOLS[case["category"]])
        calls = r["message"].get("tool_calls") or []
        fn = calls[0]["function"] if calls else {}
        out["tool"] = fn.get("name")
        out["arguments"] = fn.get("arguments")
        out["tool_ok"] = out["tool"] in [case["tool"], *case.get("alt_tools", [])]
        out["args_ok"] = out["tool_ok"] and args_ok(case.get("args", {}), out["arguments"])
        out["tool_s"] = seconds(r)
        timings.append(r)

    # What the user would actually wait for: router + tool call (the tool's own work comes on top).
    out["total_s"] = sum(seconds(t) for t in timings)
    out["gen_tok_s"] = statistics.mean(tok_per_s(t) for t in timings)
    out["prompt_tok_s"] = statistics.mean(prompt_tok_per_s(t) for t in timings)
    return out


def pct(rows, key):
    rows = [r for r in rows if key in r]
    return sum(r[key] for r in rows) / len(rows) if rows else 0.0


def bench_model(client, model, cases):
    print(f"\n=== {model}")
    t = time.time()
    client.chat(model, [{"role": "user", "content": "hi"}])  # load into VRAM, not counted
    print(f"  load + warmup: {time.time() - t:.1f}s")
    share, size = client.gpu_share(model)
    if share is not None:
        print(f"  loaded size: {size / 2**30:.2f} GiB, on GPU: {share:.0%}")

    rows = []
    for case in cases:
        try:
            row = run_case(client, model, case)
        except Exception as e:  # keep going; one bad case shouldn't kill the run
            row = {"id": case["id"], "lang": case.get("lang", "en"), "error": str(e),
                   "router_ok": False, "tool_ok": False, "args_ok": False, "total_s": 0}
        rows.append(row)
        mark = lambda k, row=row: {True: "ok", False: "XX"}.get(row.get(k), "--")
        print(f"  {row['id']:<11} router {mark('router_ok')}  tool {mark('tool_ok')}  args {mark('args_ok')}"
              f"  {row.get('total_s', 0):5.1f}s  -> {row.get('category')} / {row.get('tool')} "
              f"{row.get('arguments') or ''}"
              + (f"  ERROR {row['error']}" if "error" in row else ""))

    en = [r for r in rows if r["lang"] == "en"]
    ms = [r for r in rows if r["lang"] == "ms"]
    times = [r["total_s"] for r in en if r.get("total_s")]
    summary = {
        "model": model,
        "gpu_share": share,
        "size_gib": size / 2**30 if size else None,
        "router_acc": pct(en, "router_ok"),
        "tool_acc": pct(en, "tool_ok"),
        "args_acc": pct(en, "args_ok"),
        "ms_router_acc": pct(ms, "router_ok"),
        "ms_args_acc": pct(ms, "args_ok"),
        "median_s": statistics.median(times) if times else None,
        "p90_s": sorted(times)[int(len(times) * 0.9) - 1] if times else None,
        "gen_tok_s": statistics.mean(r["gen_tok_s"] for r in rows if r.get("gen_tok_s")) if rows else 0,
        "prompt_tok_s": statistics.mean(r["prompt_tok_s"] for r in rows if r.get("prompt_tok_s")) if rows else 0,
    }
    summary["pass"] = (summary["router_acc"] >= GATE_ROUTER_ACC
                       and summary["median_s"] is not None and summary["median_s"] < GATE_MEDIAN_SECONDS)
    return summary, rows


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("models", nargs="*", help="models to test (default: every pulled model with tool support)")
    p.add_argument("--host", default=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434"))
    p.add_argument("--ctx", type=int, default=4096, help="num_ctx (default 4096)")
    p.add_argument("--cases", default=str(HERE / "bench_cases.json"))
    a = p.parse_args()
    if not a.host.startswith("http"):
        a.host = "http://" + a.host

    client = Ollama(a.host, a.ctx)
    cases = json.loads(Path(a.cases).read_text(encoding="utf-8"))
    try:
        models = a.models or client.tool_models()
    except urllib.error.URLError as e:
        sys.exit(f"Can't reach Ollama at {a.host} ({e.reason}). Is it running?")
    if not models:
        sys.exit("No models with tool support are pulled. Try: ollama pull qwen3:4b")

    results = [bench_model(client, m, cases) for m in models]

    print(f"\n{'model':<24}{'GPU':>5}{'GiB':>6}{'router':>8}{'tool':>6}{'args':>6}{'ms':>6}"
          f"{'median':>8}{'p90':>6}{'gen t/s':>9}{'pp t/s':>8}  gate")
    for s, _ in results:
        gpu = f"{s['gpu_share']:.0%}" if s["gpu_share"] is not None else "?"
        print(f"{s['model']:<24}{gpu:>5}{s['size_gib'] or 0:>6.2f}{s['router_acc']:>8.0%}{s['tool_acc']:>6.0%}"
              f"{s['args_acc']:>6.0%}{s['ms_router_acc']:>6.0%}{s['median_s'] or 0:>7.1f}s{s['p90_s'] or 0:>5.1f}s"
              f"{s['gen_tok_s']:>9.1f}{s['prompt_tok_s']:>8.0f}  {'PASS' if s['pass'] else 'fail'}")
    print(f"\nGate: router >= {GATE_ROUTER_ACC:.0%} on English cases and median < {GATE_MEDIAN_SECONDS:.0f}s. "
          "'ms' = Malay/mixed router accuracy, reported but not gated.")

    out_dir = HERE / "bench_results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"bench-{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"ctx": a.ctx, "results": [{"summary": s, "cases": r} for s, r in results]},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Full results: {out}")


if __name__ == "__main__":
    main()
