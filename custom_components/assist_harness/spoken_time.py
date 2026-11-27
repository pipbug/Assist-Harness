"""Spoken forms of the clock time and date for the per-turn context.

Small models convert digits to words badly, for example reading 00:55 as
"quarter to one" or "half past one". The harness therefore hands the model
the time already written the way it should be said, so the model only has
to repeat it.

This module has no Home Assistant imports so it can be unit tested alone.
"""

from __future__ import annotations

import re
from datetime import date, datetime

_HOURS = (
    "twelve",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
)
_UNITS = ("", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_TEENS = (
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}

# Minutes said relative to the hour ("ten past", "quarter to")
_PAST = {5: "five", 10: "ten", 15: "quarter", 20: "twenty", 25: "twenty-five", 30: "half"}


def _number_words(n: int) -> str:
    """Spell 1 to 59 in words."""
    if n < 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS[n - 10]
    tens, units = divmod(n, 10)
    return _TENS[tens] if units == 0 else f"{_TENS[tens]}-{_UNITS[units]}"


def _day_part(hour: int) -> str:
    """Day part for a 24-hour hour (0 to 23)."""
    if hour < 12:
        return "in the morning"
    if hour < 18:
        return "in the afternoon"
    return "in the evening"


def _hour_phrase(hour: int) -> str:
    """Name an hour for "past" and "to" forms, with its day part."""
    hour %= 24
    if hour == 0:
        return "midnight"
    if hour == 12:
        return "noon"
    return f"{_HOURS[hour % 12]} {_day_part(hour)}"


def format_spoken_time(hour: int, minute: int) -> str:
    """Format a 24-hour time as British spoken words, to the exact minute.

    Whole hours and multiples of five use "past" and "to" ("five to one in
    the morning", "quarter past three in the afternoon"); other minutes use
    the shorter clock reading ("ten forty-seven in the morning").
    00:00 is "midnight" and 12:00 is "noon". No digits, no rounding.
    """
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time {hour}:{minute}")

    if minute == 0:
        if hour in (0, 12):
            return _hour_phrase(hour)
        return f"{_HOURS[hour % 12]} o'clock {_day_part(hour)}"
    if minute in _PAST:
        if hour == 12:
            # "quarter past noon" sounds odd; say "twelve" with the day part
            return f"{_PAST[minute]} past twelve {_day_part(hour)}"
        return f"{_PAST[minute]} past {_hour_phrase(hour)}"
    if 60 - minute in _PAST:
        return f"{_PAST[60 - minute]} to {_hour_phrase(hour + 1)}"

    minute_words = _number_words(minute)
    if minute < 10:
        minute_words = f"oh {minute_words}"
    return f"{_HOURS[hour % 12]} {minute_words} {_day_part(hour)}"


def format_spoken_date(day: date) -> str:
    """Format a date as "Friday November 27th 2026"."""
    n = day.day
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{day.strftime('%A %B')} {n}{suffix} {day.year}"


_DAY_PARTS = (" in the morning", " in the afternoon", " in the evening")

# Leading and trailing words that do not change a question's meaning
_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|oh)\s+)?(?:kestrel\s+)?"
    r"(?:(?:please|excuse me|sorry)\s+)?"
    r"(?:(?:can|could|would|will) you (?:please )?tell me\s+|tell me\s+"
    r"|do you know\s+|(?:do )?you know\s+)?"
)
_TRAIL = re.compile(
    r"(?:\s+(?:please|kestrel|miss|then|now|right now|at the moment|today|again))*$"
)

_TIME_QUESTIONS = re.compile(
    r"(?:what(?: is|s|'s)? (?:the )?time(?: is it| it is)?"
    r"|what time(?: is|s|'s) it"
    r"|what's the time|whats the time"
    r"|the time"
    r"|(?:have|got) you got the time|have you the time|do you have the time"
    r"|got the time|time check)"
)
_DATE_QUESTIONS = re.compile(
    r"(?:what(?: is|s|'s)? (?:the |today's |todays )?date(?: is it| it is)?"
    r"|what date(?: is|s|'s) it"
    r"|what day(?: of the week)?(?: is|s|'s) (?:it|today)"
    r"|what(?: is|s|'s) today"
    r"|(?:the |today's |todays )date"
    r"|which day is it)"
)


def _candidates(text: str) -> tuple[str, ...]:
    """The question with fillers removed, with and without trailing fillers."""
    text = text.lower().replace("\u2019", "'")
    text = re.sub(r"[^a-z' ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _LEAD.sub("", text, count=1).strip()
    return (text, _TRAIL.sub("", text).strip())


def classify_clock_question(text: str) -> str | None:
    """Return "time" or "date" for a plain time or date question, else None.

    Only whole-utterance matches count, so anything with more in it, such
    as a device, a place ("what time is it in Tokyo") or an event ("what
    time is sunset"), is left to the model.
    """
    for candidate in _candidates(text):
        if not candidate:
            continue
        if _TIME_QUESTIONS.fullmatch(candidate):
            return "time"
        if _DATE_QUESTIONS.fullmatch(candidate):
            return "date"
    return None


def clock_answer(kind: str, now: datetime) -> str:
    """Kestrel's direct answer, such as "It's five to one." """
    if kind == "time":
        spoken = format_spoken_time(now.hour, now.minute)
        for part in _DAY_PARTS:
            if spoken.endswith(part):
                spoken = spoken[: -len(part)]
                break
        return f"It's {spoken}."
    if kind == "date":
        return f"It's {format_spoken_date(now)}."
    raise ValueError(f"unknown clock question kind {kind!r}")
