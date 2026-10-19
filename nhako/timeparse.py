"""Turn the user's own words ("next tuesday 3pm", "esok 3 petang") into datetimes.

The model is only asked to copy the time phrase out of the message; all date math
happens here, where it can be tested. dateparser does the heavy lifting; this module
fills its gaps ("this/next <weekday>", Malay) and adds ranges ("next week").
"""
import datetime as dt
import re
from dataclasses import dataclass

import dateparser

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Malay -> English, applied word by word before dateparser sees the text.
MALAY_WORDS = {
    "hari ini": "today", "harini": "today", "malam ini": "tonight", "malam ni": "tonight",
    "esok": "tomorrow", "besok": "tomorrow", "lusa": "day after tomorrow",
    "minggu depan": "next week", "minggu ni": "this week", "minggu ini": "this week",
    "bulan depan": "next month", "hujung minggu": "weekend",
    "isnin": "monday", "selasa": "tuesday", "rabu": "wednesday", "khamis": "thursday",
    "jumaat": "friday", "sabtu": "saturday", "ahad": "sunday",
    "minit": "minutes", "dalam": "in",
}
MALAY_TIME_OF_DAY = {"pagi": "am", "tengahari": "pm", "petang": "pm", "malam": "pm"}
# "friday afternoon" with no clock time -> a default hour dateparser understands
PARTS_OF_DAY = {"morning": "9am", "afternoon": "3pm", "evening": "7pm", "night": "9pm"}


MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
# Words that name a day (English and Malay). Used to tell "move it to 5pm" (same day)
# from "move it to friday", and to pull the day out of "tomorrow's meeting".
DATE_WORDS = "|".join([
    "today", "tonight", "tomorrow", "day after tomorrow", "weekend", "week", "month",
    *WEEKDAYS, "harini", "hari ini", "esok", "besok", "lusa",
    "isnin", "selasa", "rabu", "khamis", "jumaat", "sabtu", "ahad", "minggu depan",
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})[a-z]*", rf"(?:{MONTHS})[a-z]*\s+\d{{1,2}}(?:st|nd|rd|th)?",
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?", r"\d{4}-\d{2}-\d{2}",
])


def has_date(text: str) -> bool:
    """True if the text names a day, not just a clock time."""
    t = (text or "").lower()
    return bool(re.search(rf"\b(?:{DATE_WORDS})\b", t) or re.search(r"\bin \d+\s*(day|week|month)", t))


@dataclass
class When:
    at: dt.datetime
    has_time: bool  # False for "friday" -> an all-day event


def normalize(text: str) -> str:
    t = " " + text.lower().strip() + " "
    t = re.sub(r"\b(pukul|pkl|at|on)\b|\bjam\b(?=\s*\d)", " ", t)  # "jam 3" = "at 3", "2 jam" = 2 hours
    # "3 petang" / "3.30 pagi" -> "3pm" / "3:30am" (before "jam" etc. get rewritten)
    t = re.sub(r"\b(\d{1,2})(?:[.:](\d{2}))?\s*(pagi|tengahari|petang|malam)\b",
               lambda m: f"{m[1]}{':' + m[2] if m[2] else ''}{MALAY_TIME_OF_DAY[m[3]]}", t)
    for ms, en in sorted(MALAY_WORDS.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(rf"\b{ms}\b", en, t)
    t = re.sub(r"\b(ni|ini)\b", " ", t)  # "sabtu ni" = "this saturday"
    return re.sub(r"\s+", " ", t).strip()


def _resolve_weekdays(text: str, now: dt.datetime) -> str:
    """'this friday' -> the coming Friday, 'next friday' -> the one after that.
    Rewritten as an ISO date, which dateparser always understands."""
    def repl(m):
        target = WEEKDAYS.index(m[2])
        days = (target - now.weekday()) % 7
        if m[1] == "next":  # the one after the coming one: on a Monday, "next tuesday" is 8 days away
            days += 7
        return (now + dt.timedelta(days=days)).strftime("%Y-%m-%d")
    return re.sub(rf"\b(this|next|coming)\s+({'|'.join(WEEKDAYS)})\b", repl, text)


def _has_time(text: str) -> bool:
    return bool(re.search(r"\d\s*(am|pm)\b|\d[:.]\d{2}|\bnoon\b|\bmidnight\b|\bin \d+|\btonight\b", text))


# Just a clock time: "6pm", "18:00", "9.30am" (after normalize, so "3 petang" is "3pm").
TIME_ONLY = re.compile(r"^\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?$|^(?:noon|midnight)$")


def parse_when(text: str, now: dt.datetime) -> When | None:
    """A single point in time. `now` must be timezone-aware."""
    t = _resolve_weekdays(normalize(text), now)
    t = t.replace("tonight", "today 8pm") if not re.search(r"\d", t) else t.replace("tonight", "today")
    for part, hour in PARTS_OF_DAY.items():
        t = re.sub(rf"\b(this |in the )?{part}\b", "" if re.search(r"\d", t) else hour, t).strip()
    parsed = dateparser.parse(t, languages=["en"], settings={
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now.replace(tzinfo=None),
        "TIMEZONE": str(now.tzinfo),
        "RETURN_AS_TIMEZONE_AWARE": True,
    })
    if parsed is None:
        return None
    has_time = _has_time(t)
    if TIME_ONLY.match(t.strip()):
        # dateparser decides "future" for a bare time against the real clock, not RELATIVE_BASE,
        # so "9am" asked at 07:10 could land tomorrow. Today first; the rule below moves it on.
        parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
    if not has_time:
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    # "5pm" said at 6pm means tomorrow, unless a day was named explicitly.
    elif parsed < now and parsed.date() == now.date() and "today" not in t:
        parsed += dt.timedelta(days=1)
    return When(parsed.replace(second=0, microsecond=0), has_time)


# Words that carry no time: what's left of "remind me to call mum at 6pm" minus "call mum" is the time.
COMMAND_WORDS = re.compile(r"\b(remind|ingatkan|ingat|ping|add|put|create|schedule|set|book|make|move|push|"
                           r"shift|change|reschedule|me|saya|aku|to|untuk|supaya|a|an|the|please|tolong|"
                           r"event|appointment|reminder|it|in my calendar|calendar)\b", re.I)
VAGUE_WORDS = {"at", "on", "in", "the", "this", "next", "pukul", "jam", "by", "from"}


def grounded(value: str, message: str) -> bool:
    """Is a time the model passed actually in the user's message? Small models sometimes copy the
    example from a tool description ("in 20 minutes") instead of the user's "at 6pm"."""
    said = normalize(message)
    v = normalize(value)
    numbers = re.findall(r"\d+", v)
    words = [w for w in re.findall(r"[a-z]+", v) if w not in VAGUE_WORDS and w not in ("am", "pm")]
    return all(n in re.findall(r"\d+", said) for n in numbers) and all(w in said for w in words)


def time_from_message(message: str, *drop: str) -> str:
    """The time part of a message: the message minus what it's about and the command words."""
    t = message.lower()
    for d in sorted(filter(None, drop), key=len, reverse=True):
        t = t.replace(d.lower().strip(), " ")
    t = COMMAND_WORDS.sub(" ", t)
    return re.sub(r"[\s,.!?]+", " ", t).strip()


def pick_when(model_value: str, message: str, *drop: str) -> str:
    """The model's time if the user said it, else the time words from the message itself."""
    if not message or not (model_value or "").strip() or grounded(model_value, message):
        return model_value or ""
    return time_from_message(message, *drop) or model_value


def _day(d: dt.datetime) -> dt.datetime:
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_range(text: str | None, now: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """A [start, end) window plus a human label. Unknown/empty text means the next 7 days."""
    t = normalize(text or "")
    today = _day(now)
    next_monday = today + dt.timedelta(days=7 - now.weekday())

    if not t or t in ("upcoming", "coming up", "soon", "next few days"):
        return now, now + dt.timedelta(days=7), "the next 7 days"
    if re.search(r"\b(today|tonight)\b", t):
        return now, today + dt.timedelta(days=1), "today"
    if "day after tomorrow" in t:
        start = today + dt.timedelta(days=2)
        return start, start + dt.timedelta(days=1), "the day after tomorrow"
    if "tomorrow" in t:
        start = today + dt.timedelta(days=1)
        return start, start + dt.timedelta(days=1), "tomorrow"
    if "next week" in t:
        return next_monday, next_monday + dt.timedelta(days=7), "next week"
    if "week" in t and "weekend" not in t:  # "this week"
        return now, next_monday, "this week"
    if "weekend" in t:
        saturday = today + dt.timedelta(days=(5 - now.weekday()) % 7)
        if now.weekday() == 6:  # on a Sunday "this weekend" is today
            saturday = today - dt.timedelta(days=1)
        if "next" in t:
            saturday += dt.timedelta(days=7)
        return max(saturday, now), saturday + dt.timedelta(days=2), "the weekend"
    if "month" in t:
        first_next = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        if "next" in t:
            return first_next, (first_next + dt.timedelta(days=32)).replace(day=1), "next month"
        return now, first_next, "this month"

    when = parse_when(t, now)
    if when is None:
        return now, now + dt.timedelta(days=7), "the next 7 days"
    start = _day(when.at)
    return max(start, now) if start.date() == now.date() else start, start + dt.timedelta(days=1), \
        when.at.strftime("%A %d %b")


def parse_duration(text: str | None) -> dt.timedelta:
    """'2 hours', '90 min', '1.5h' -> timedelta. Defaults to one hour."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|jam|m|min|mins|minute|minutes|minit)\b",
                  (text or "").lower())
    if not m:
        return dt.timedelta(hours=1)
    n = float(m[1])
    return dt.timedelta(hours=n) if m[2].startswith(("h", "j")) else dt.timedelta(minutes=n)
