"""Unit tests for the spoken time and date given to the model each turn.

The formatter is loaded from the file so these tests do not need Home Assistant.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import date, datetime
from pathlib import Path

import pytest

_MODULE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "assist_harness"
    / "spoken_time.py"
)
_SPEC = importlib.util.spec_from_file_location("spoken_time", _MODULE)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)
format_spoken_time = _MOD.format_spoken_time
format_spoken_date = _MOD.format_spoken_date


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, "midnight"),
        (0, 1, "twelve oh one in the morning"),
        (0, 5, "five past midnight"),
        (0, 15, "quarter past midnight"),
        (0, 30, "half past midnight"),
        (0, 47, "twelve forty-seven in the morning"),
        (0, 55, "five to one in the morning"),
        (1, 5, "five past one in the morning"),
        (1, 30, "half past one in the morning"),
        (7, 0, "seven o'clock in the morning"),
        (7, 9, "seven oh nine in the morning"),
        (9, 13, "nine thirteen in the morning"),
        (10, 47, "ten forty-seven in the morning"),
        (11, 45, "quarter to noon"),
        (11, 55, "five to noon"),
        (11, 59, "eleven fifty-nine in the morning"),
        (12, 0, "noon"),
        (12, 1, "twelve oh one in the afternoon"),
        (12, 15, "quarter past twelve in the afternoon"),
        (12, 30, "half past twelve in the afternoon"),
        (12, 45, "quarter to one in the afternoon"),
        (13, 30, "half past one in the afternoon"),
        (13, 40, "twenty to two in the afternoon"),
        (17, 35, "twenty-five to six in the evening"),
        (17, 50, "ten to six in the evening"),
        (18, 0, "six o'clock in the evening"),
        (19, 1, "seven oh one in the evening"),
        (19, 20, "twenty past seven in the evening"),
        (19, 25, "twenty-five past seven in the evening"),
        (22, 31, "ten thirty-one in the evening"),
        (23, 45, "quarter to midnight"),
        (23, 55, "five to midnight"),
        (23, 59, "eleven fifty-nine in the evening"),
    ],
)
def test_spoken_time(hour, minute, expected):
    assert format_spoken_time(hour, minute) == expected


def test_every_minute_is_words_only_and_distinct():
    seen = set()
    for hour in range(24):
        for minute in range(60):
            text = format_spoken_time(hour, minute)
            assert re.fullmatch(r"[a-z' -]+", text), text
            assert "  " not in text and text == text.strip()
            seen.add(text)
    # No two minutes of the day read the same, so nothing is rounded
    assert len(seen) == 24 * 60


@pytest.mark.parametrize(("hour", "minute"), [(24, 0), (-1, 0), (0, 60), (0, -1)])
def test_rejects_invalid_time(hour, minute):
    with pytest.raises(ValueError):
        format_spoken_time(hour, minute)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 27), "Friday November 27th 2026"),
        (date(2026, 11, 1), "Sunday November 1st 2026"),
        (date(2026, 11, 2), "Monday November 2nd 2026"),
        (date(2026, 11, 3), "Tuesday November 3rd 2026"),
        (date(2026, 11, 11), "Wednesday November 11th 2026"),
        (date(2026, 11, 12), "Thursday November 12th 2026"),
        (date(2026, 11, 13), "Friday November 13th 2026"),
        (date(2026, 11, 21), "Saturday November 21st 2026"),
        (date(2026, 11, 22), "Sunday November 22nd 2026"),
        (date(2026, 11, 23), "Monday November 23rd 2026"),
        (date(2026, 12, 31), "Thursday December 31st 2026"),
    ],
)
def test_spoken_date(day, expected):
    assert format_spoken_date(day) == expected


# --- Plain time and date questions answered without the model ---

classify_clock_question = _MOD.classify_clock_question
clock_answer = _MOD.clock_answer


@pytest.mark.parametrize(
    "text",
    [
        "What time is it?",
        "what time is it",
        "What's the time?",
        "What’s the time?",
        "whats the time",
        "What is the time?",
        "What time's it?",
        "Kestrel, what time is it?",
        "Hey Kestrel, what's the time please?",
        "Can you tell me the time?",
        "Could you tell me what time it is?",
        "Do you know what time it is?",
        "What time is it now?",
        "What time is it right now, miss?",
        "Have you got the time?",
        "Do you have the time?",
        "The time?",
    ],
)
def test_time_questions_are_caught(text):
    assert classify_clock_question(text) == "time"


@pytest.mark.parametrize(
    "text",
    [
        "What's the date?",
        "What is the date today?",
        "What's today's date?",
        "What date is it?",
        "What day is it?",
        "What day is it today?",
        "What day of the week is it?",
        "What's today?",
        "Kestrel, what's the date?",
        "Can you tell me the date please?",
        "Today's date?",
    ],
)
def test_date_questions_are_caught(text):
    assert classify_clock_question(text) == "date"


@pytest.mark.parametrize(
    "text",
    [
        "Turn off the printer light.",
        "Set a timer for ten minutes.",
        "Turn on the lights at the time I wake up.",
        "What time is sunset?",
        "What time is it in Tokyo?",
        "What time does the printer finish?",
        "What time did the front door open?",
        "When did the kitchen light turn on?",
        "Is anything on?",
        "What's on today?",
        "What's the temperature?",
        "What's the weather today?",
        "Remind me what I told you about my coffee.",
        "What's the time and date?",
        "Time to turn off the lights.",
        "Set the clock to seven.",
        "What day is bin collection?",
        "What date is my dentist appointment?",
        "How are you?",
        "Good night.",
        "",
        "   ",
    ],
)
def test_other_requests_go_to_the_model(text):
    assert classify_clock_question(text) is None


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (0, 0, "It's midnight."),
        (0, 55, "It's five to one."),
        (1, 7, "It's one oh seven."),
        (10, 47, "It's ten forty-seven."),
        (11, 45, "It's quarter to noon."),
        (12, 0, "It's noon."),
        (12, 15, "It's quarter past twelve."),
        (12, 45, "It's quarter to one."),
        (19, 0, "It's seven o'clock."),
        (23, 59, "It's eleven fifty-nine."),
    ],
)
def test_time_answer(hour, minute, expected):
    now = datetime(2026, 11, 27, hour, minute, 30)
    assert clock_answer("time", now) == expected


def test_date_answer():
    assert (
        clock_answer("date", datetime(2026, 11, 27, 0, 55))
        == "It's Friday November 27th 2026."
    )


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        clock_answer("weather", datetime(2026, 11, 27, 0, 55))
