"""Strip a trailing offer question from an assistant reply."""

from __future__ import annotations

import re

# A sentence ends at !, ?, or a period that is not part of a decimal (7.3).
# The terminator must be followed by whitespace or the end of the string.
_SENTENCE_END = re.compile(
    r"(?:[!?]+|(?<!\d)\.(?!\d))+(?=\s|$)",
)


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of each sentence in *text*."""
    stripped_end = len(text.rstrip())
    if stripped_end == 0:
        return []

    body = text[:stripped_end]
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_END.finditer(body):
        end = match.end()
        if body[start:end].strip():
            spans.append((start, end))
        start = end
    if start < stripped_end and body[start:].strip():
        spans.append((start, stripped_end))
    return spans


def strip_trailing_offer(text: str, action_succeeded: bool) -> str:
    """Drop a trailing question after a successful action.

    Never removes the only sentence. Returns *text* unchanged when the
    reply should be left alone.
    """
    if not action_succeeded or not text or not text.strip():
        return text

    spans = split_sentences(text)
    if len(spans) < 2:
        return text

    last_start, last_end = spans[-1]
    last = text[last_start:last_end].rstrip()
    if not last.endswith("?"):
        return text

    return text[:last_start].rstrip()
