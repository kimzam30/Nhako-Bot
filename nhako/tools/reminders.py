"""Reminders the bot itself sends on Telegram. Stored in SQLite; nhako/scheduler.py delivers them."""
from ..timeparse import parse_when, pick_when
from . import Context, Result, Tool

DEFAULT_HOUR = 9  # "remind me on friday to ..." with no time


async def set_reminder(args: dict, ctx: Context) -> Result:
    what = (args.get("what") or "").strip()
    when = parse_when(pick_when(args.get("when") or "", ctx.text, what), ctx.now())
    if not what:
        return Result("What should I remind you about?")
    if when is None:
        return Result(f"I couldn't work out when “{args.get('when')}” is. "
                      "Try “in 20 minutes” or “tomorrow 9am”.")
    at = when.at if when.has_time else when.at.replace(hour=DEFAULT_HOUR)
    if at <= ctx.now():
        return Result(f"{at:%a %d %b %H:%M} has already passed. When should I remind you?")
    await ctx.store.add_reminder(ctx.chat_id, what, at)
    return Result(f"⏰ OK, I'll remind you {at:%a %d %b at %H:%M}: {what}\n(/reminders to see or cancel)")


def tools(ctx: Context) -> list[Tool]:
    return [Tool("set_reminder", "Send the user a reminder message at a time ('remind me', 'ping me', 'ingatkan').",
                 {"what": "what to remind about",
                  "when": "time in the user's words, e.g. 'in 20 minutes', 'tomorrow 9am'"},
                 set_reminder, required=("what", "when"))]
