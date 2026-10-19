"""Step 1 of every message: pick one tool category, so the model only ever sees a few tools.

Phase 0 showed a 2B model routes well but picks tools worse when it sees all of them at
once (docs/REVIVAL_PLAN.md). Any parse problem falls back to plain chat, which is
always safe: chat can't change anything.

The prompt is deliberately static (no clock time in it): Ollama can then reuse the
already-processed prompt from the previous message instead of reading it all again.
"""
import json
import logging

from .llm import LLM

log = logging.getLogger(__name__)

CATEGORIES = {
    "schedule": "the user's calendar: add, edit, move, cancel/remove/delete an event or appointment "
                "(even vaguely named, like 'the dentist thing'), list events, 'am I free', 'remind me'",
    "web": "needs current information from the internet (news, prices, weather, releases), or a URL",
    "homelab": "LIVE STATUS checks of the user's own servers: is the Minecraft server 'getgud' up, "
               "who is online, are their Docker containers (nextcloud, postiz, vaultwarden...) healthy",
    "postiz": "the user's social media (Postiz): what posts are scheduled or published, "
              "write/draft a post, tweet, LinkedIn or Instagram caption "
              "(whether the Postiz app itself is up or running is homelab)",
    "notion": "the user's to-do / task list in Notion: add a task, what's due, what's left to do, "
              "mark a task done",
    "memory": "the user asks Nhako to remember or forget a fact about them, or asks what Nhako "
              "remembers about them",
    "chat": "anything else: general knowledge, advice, tips and how-to on ANY topic (including servers, "
            "homelab and tech), opinions, jokes, conversation",
}

# Few-shot examples for the borderline cases Phase 0 found. They are deliberately
# NOT the benchmark sentences, so tests/test_router_live.py still measures something.
EXAMPLES = [
    ("doing research on NAS drives, what should I look for?", "chat"),
    ("any tips for organising my docker compose files?", "chat"),
    ("cancel the haircut thing", "schedule"),
    ("how do I remove rust from a bike chain", "chat"),
    ("what should I put on a birthday card", "chat"),
    ("is vaultwarden running ok?", "homelab"),
    ("is postiz down?", "homelab"),
    ("how many people are on the minecraft server", "homelab"),
    ("put lunch with Aiman on thursday 1pm", "schedule"),
    ("ada apa-apa esok?", "schedule"),
    ("what's the latest Docker Desktop version", "web"),
    ("put 'renew passport' on my to-do list", "notion"),
    ("I finished the invoice task", "notion"),
    ("anything going out on social media tomorrow?", "postiz"),
    ("write a tweet about the server wipe", "postiz"),
    ("remember that I'm allergic to prawns", "memory"),
    ("don't forget to remind me at 5 to call mum", "schedule"),
    ("what do you know about me?", "memory"),
]

SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}},
    "required": ["category"],
}


def router_prompt() -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    examples = "\n".join(f'"{text}" -> {cat}' for text, cat in EXAMPLES)
    return ("You route messages for Nhako, a personal assistant.\n"
            "Classify the user's message into exactly one category. Reply with JSON only.\n"
            f"Categories:\n{cats}\n\nExamples:\n{examples}")


PROMPT = router_prompt()


def parse_category(raw: str | None) -> str:
    try:
        cat = json.loads(raw or "").get("category")
    except (json.JSONDecodeError, AttributeError):
        cat = None
    if cat not in CATEGORIES:
        log.warning("router gave %r; falling back to chat", raw)
        return "chat"
    return cat


async def classify(llm: LLM, text: str) -> str:
    msg = await llm.chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
                         format=SCHEMA)
    return parse_category(msg.content)
