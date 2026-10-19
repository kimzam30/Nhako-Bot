import datetime as dt

import pytest

from nhako.timeparse import parse_duration, parse_range, parse_when, pick_when

from .conftest import KL, NOW  # Monday 19 Oct 2026, 07:30


def at(day, hour=0, minute=0):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=KL)


@pytest.mark.parametrize("text, expected", [
    ("tomorrow 9am", at(20, 9)),
    ("next tuesday 3pm", at(27, 15)),      # the Tuesday after the coming one
    ("this saturday 8pm", at(24, 20)),
    ("saturday 8pm", at(24, 20)),
    ("in 20 minutes", at(19, 7, 50)),
    ("5pm", at(19, 17)),
    ("friday afternoon", at(23, 15)),
    ("tonight", at(19, 20)),
    # Malay, including the exact phrase qwen3.5:2b passed through in Phase 0
    ("esok 3 petang", at(20, 15)),
    ("jam 3 petang esok", at(20, 15)),
    ("dalam 20 minit", at(19, 7, 50)),
])
def test_parse_when_with_time(text, expected):
    when = parse_when(text, NOW)
    assert when is not None and when.has_time
    assert when.at == expected


@pytest.mark.parametrize("text, day", [("friday", 23), ("sunday", 25), ("sabtu ni", 24)])
def test_parse_when_date_only_is_all_day(text, day):
    when = parse_when(text, NOW)
    assert when.at == at(day) and not when.has_time


def test_time_already_passed_today_means_tomorrow():
    evening = NOW.replace(hour=18)
    assert parse_when("5pm", evening).at == at(20, 17)


def test_parse_when_gives_up_on_nonsense():
    assert parse_when("purple elephants", NOW) is None


@pytest.mark.parametrize("text, start, end", [
    ("today", NOW, at(20)),
    ("hari ini", NOW, at(20)),
    ("tomorrow", at(20), at(21)),
    ("next week", at(26), at(26) + dt.timedelta(days=7)),
    ("this week", NOW, at(26)),
    ("this weekend", at(24), at(26)),
    ("friday afternoon", at(23), at(24)),
    ("sabtu", at(24), at(25)),
    ("", NOW, NOW + dt.timedelta(days=7)),
    (None, NOW, NOW + dt.timedelta(days=7)),
    ("blah", NOW, NOW + dt.timedelta(days=7)),
])
def test_parse_range(text, start, end):
    s, e, _ = parse_range(text, NOW)
    assert (s, e) == (start, end)


def test_parse_range_next_month():
    s, e, label = parse_range("next month", NOW)
    assert (s.date(), e.date(), label) == (dt.date(2026, 11, 1), dt.date(2026, 12, 1), "next month")


@pytest.mark.parametrize("text, expected", [
    ("2 hours", dt.timedelta(hours=2)), ("90 min", dt.timedelta(minutes=90)),
    ("1.5h", dt.timedelta(hours=1.5)), ("2 jam", dt.timedelta(hours=2)), (None, dt.timedelta(hours=1)),
])
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


# A bare clock time used to be decided against the real clock (dateparser), not `now`,
# so these depended on the time of day the tests ran.
@pytest.mark.parametrize("hour, text, expected", [
    (7, "9am", "19 09:00"), (7, "6pm", "19 18:00"), (7, "3 petang", "19 15:00"),
    (15, "6pm", "19 18:00"), (15, "18:00", "19 18:00"), (15, "9am", "20 09:00"), (15, "3pm", "20 15:00"),
    (19, "6pm", "20 18:00"), (19, "9.30pm", "19 21:30"),
    (23, "noon", "20 12:00"),
])
def test_bare_time_is_today_unless_passed(hour, text, expected):
    now = NOW.replace(hour=hour, minute=10)
    assert parse_when(text, now).at.strftime("%d %H:%M") == expected


@pytest.mark.parametrize("model, message, drop, expected", [
    ("in 20 minutes", "remind me to call mum at 6pm", ["call mum"], "at 6pm"),    # copied the example
    ("6pm", "remind me at 6pm to buy milk", ["buy milk"], "6pm"),
    ("tomorrow 3pm", "esok 3 petang ada meeting", ["meeting"], "tomorrow 3pm"),    # Malay, still grounded
    ("in 20 minutes", "remind me in 20 minutes to stretch", ["stretch"], "in 20 minutes"),
    ("next tuesday 3pm", "add dentist next tuesday 3pm", ["dentist"], "next tuesday 3pm"),
    ("friday", "", [], "friday"),                                                  # no message: trust it
])
def test_pick_when(model, message, drop, expected):
    assert pick_when(model, message, *drop) == expected
