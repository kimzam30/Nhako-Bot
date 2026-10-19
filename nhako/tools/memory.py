"""Long-term memory: short facts the user asks Nhako to remember ("I prefer meetings after 10am").

Facts are saved only when the user asks, never picked up from chat on their own,
and every stored fact goes into the system prompt for conversation (see facts_prompt), but
not when the model picks a tool: there, even one related fact made a 2B model choose wrong.
No embeddings: a few dozen facts fit in the prompt, so a vector search would only add a
model swap and a way to miss things.
"""
import re

from . import Context, NeedsConfirm, Result, Tool
from .calendar import FILLER, MATCH_THRESHOLD, match_score

MAX_FACTS = 50
PROMPT_BUDGET = 1500  # characters of facts in the system prompt (num_ctx is 4096 tokens)
# Never stored: they'd sit in plain text in the database and in every prompt.
# "pin" only counts with a number in the fact, so "I collect pin badges" is fine.
SECRET = re.compile(r"\b(password|passwd|kata laluan|api[ -]?key|token|secret|otp|cvv|card number|"
                    r"no\.? kad|ic number|no\.? ic|passport number)\b|\bpin\b(?=.*\d)|\d{12,}|"
                    r"\b[A-Za-z0-9_\-]{32,}\b", re.I)
FORGET_WORDS = FILLER | {"forget", "remember", "about", "that", "i", "me", "my", "lupakan", "lupa", "fact"}


def facts_prompt(facts: list[tuple[int, str]]) -> str:
    """The system-prompt lines, newest facts kept when over budget."""
    lines, used = [], 0
    for _, text in reversed(facts):
        if used + len(text) > PROMPT_BUDGET:
            break
        lines.append(f"- {text}")
        used += len(text) + 3
    return "What you know about the user (they asked you to remember this):\n" + "\n".join(reversed(lines)) \
        if lines else ""


def tidy(fact: str) -> str:
    """'remember that I prefer tea.' -> 'I prefer tea'; 'tolong ingat yang saya alah udang' -> 'saya alah udang'"""
    fact = re.sub(r"^\s*(please\s+|tolong\s+)?(remember|ingat|note|don't forget|jangan lupa)\s+"
                  r"(that\s+|yang\s+|bahawa\s+)?", "", fact or "", flags=re.I)
    fact = re.sub(r"[,.]?\s*(please\s+)?(remember|ingat)\s+(it|that|this|tu|ni|ya)\s*[.!]*$", "", fact, flags=re.I)
    return re.sub(r"^\s*that\s+", "", fact, flags=re.I).strip().rstrip(".").strip()


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w'-]+", text.lower()) if len(w) >= 3 and w not in FORGET_WORDS}


def mostly_said(fact: str, message: str) -> bool:
    """At least half of the fact's words come from the message ('allergy' counts for 'allergic')."""
    words, said = content_words(fact), content_words(message)
    if not words:
        return False
    stems = {w[:5] for w in said}
    return sum(w in said or w[:5] in stems for w in words) / len(words) >= 0.5


def tools(ctx: Context) -> list[Tool]:

    async def remember(args: dict, ctx: Context) -> Result:
        fact = tidy(args.get("fact") or "")
        said = tidy(ctx.text)
        if said and not mostly_said(fact, said):
            # The model wrote something the user didn't say (in tests: it repeated the previous fact
            # from the history, or added the example from this tool's description). Keep the user's words.
            fact = said
        if not fact:
            return Result("What should I remember?")
        if SECRET.search(fact):
            return Result("🔒 I don't store passwords, PINs, keys or ID/card numbers. "
                          "A password manager is the right place for those.")
        if len(fact) > 300:
            return Result("That's a bit long for me to keep. Can you put it in one short sentence?")
        if len(await ctx.store.facts(ctx.chat_id)) >= MAX_FACTS:
            return Result(f"I'm holding {MAX_FACTS} facts already. Remove some with /memory first.")
        if not await ctx.store.add_fact(ctx.chat_id, fact):
            return Result(f"I already know that: {fact}")
        return Result(f"🧠 Got it, I'll remember: {fact}\n(/memory to see or remove what I know)")

    async def forget(args: dict, ctx: Context) -> Result | NeedsConfirm:
        facts = await ctx.store.facts(ctx.chat_id)
        words = [w for w in re.findall(r"[\w'-]+", (args.get("fact") or "").lower()) if w not in FORGET_WORDS]
        said = content_words(ctx.text)
        if said and not set(words) & said:
            words = sorted(said)  # the model named something the user didn't say
        if not words:
            return Result("Which fact should I forget? /memory lists them, with a button to remove each.")
        scored = [(match_score(text, words), fid, text) for fid, text in facts]
        best = max((s for s, _, _ in scored), default=0)
        matches = [(fid, text) for s, fid, text in scored if s >= MATCH_THRESHOLD and s == best]
        if not matches:
            return Result(f"I don't remember anything about “{' '.join(words)}”.")
        listing = "\n".join(f"• {text}" for _, text in matches[:10])
        return NeedsConfirm(f"Forget {'this' if len(matches) == 1 else f'these {len(matches)}'}?\n{listing}",
                            {"ids": [fid for fid, _ in matches]})

    async def forget_execute(args: dict, ctx: Context) -> Result:
        gone = [t for fid in args["ids"] if (t := await ctx.store.delete_fact(fid, ctx.chat_id))]
        return Result("🧹 Forgotten:\n" + "\n".join(f"• {t}" for t in gone) if gone else "Already forgotten.")

    async def list_memories(args: dict, ctx: Context) -> Result:
        facts = await ctx.store.facts(ctx.chat_id)
        if not facts:
            return Result("I don't have anything saved about you yet. Say “remember that …”.")
        return Result(f"🧠 What I remember ({len(facts)}):\n" + "\n".join(f"• {t}" for _, t in facts))

    return [
        Tool("remember", "Save a lasting fact or preference about the user that they asked to be remembered.",
             {"fact": "only what the user just asked to remember, as one short sentence"},
             remember, required=("fact",)),
        Tool("forget", "Delete something the user previously asked to be remembered.",
             {"fact": "words from the fact to forget"}, forget, required=("fact",), execute=forget_execute),
        Tool("list_memories", "Show what the user has asked Nhako to remember about them.", {}, list_memories),
    ]
