"""Strip a trailing offer question from an assistant reply."""

from __future__ import annotations

import re

# State-changing tools. Prefixed copies such as home_save_memory count too.
# set_conversation_state is excluded: it only flags whether a reply is expected.
ACTION_TOOLS = frozenset(
    {
        "perform_action",
        "call_service",
        "save_memory",
        "run_script",
        "run_automation",
    }
)

# Trailing questions that start with one of these (case-insensitive, after
# leading conjunctions are stripped) are dropped even when no tool ran.
OFFER_PHRASES = (
    "shall i",
    "should i",
    "would you like",
    "do you want",
    "do you need",
    "can i",
    "may i",
    "is there anything",
    "anything else",
    "want me to",
    "how else",
    "what else can i",
)

_LEADING_CONJUNCTIONS = ("and", "but", "or", "so", "well", "then", "also")

# A sentence ends at !, ?, or a period that is not part of a decimal (7.3).
# The terminator must be followed by whitespace or the end of the string.
_SENTENCE_END = re.compile(
    r"(?:[!?]+|(?<!\d)\.(?!\d))+(?=\s|$)",
)


def is_action_tool(name: str | None) -> bool:
    """True for state-changing tools and prefixed copies."""
    if not name:
        return False
    if name in ACTION_TOOLS:
        return True
    return any(name.endswith("_" + known) for known in ACTION_TOOLS)


def is_offer_question(sentence: str) -> bool:
    """True if *sentence* is a trailing offer, not a clarifying question."""
    text = sentence.strip()
    if not text.endswith("?"):
        return False

    body = text[:-1].strip().lower()
    while body:
        stripped = False
        for conjunction in _LEADING_CONJUNCTIONS:
            prefix = conjunction + " "
            if body.startswith(prefix):
                body = body[len(prefix):].lstrip(" ,;")
                stripped = True
                break
        if not stripped:
            break
    return any(body.startswith(phrase) for phrase in OFFER_PHRASES)


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
    """Drop a trailing offer, or any trailing question after a successful action.

    Never removes the only sentence. Returns *text* unchanged when the
    reply should be left alone.
    """
    if not text or not text.strip():
        return text

    spans = split_sentences(text)
    if len(spans) < 2:
        return text

    last_start, last_end = spans[-1]
    last = text[last_start:last_end].rstrip()
    if last.endswith("?") and (action_succeeded or is_offer_question(last)):
        return text[:last_start].rstrip()
    return text
