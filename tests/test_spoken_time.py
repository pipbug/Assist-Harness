"""Unit tests for the spoken time and date given to the model each turn.

The formatter is loaded from the file so these tests do not need Home Assistant.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import date
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
