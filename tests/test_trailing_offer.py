"""Unit tests for trailing-offer stripping.

The filter is loaded from the file so these tests do not need Home Assistant.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "assist_harness"
    / "trailing_offer.py"
)
_SPEC = importlib.util.spec_from_file_location("trailing_offer", _MODULE)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)
strip_trailing_offer = _MOD.strip_trailing_offer
is_action_tool = _MOD.is_action_tool


def test_drops_question_after_action():
    text = "The printer light is off. Would you like me to adjust anything else?"
    assert (
        strip_trailing_offer(text, action_succeeded=True)
        == "The printer light is off."
    )


def test_keeps_question_when_no_action():
    text = "The printer light is off. Would you like me to adjust anything else?"
    assert strip_trailing_offer(text, action_succeeded=False) == text


def test_keeps_single_sentence():
    text = "Shall I turn it off?"
    assert strip_trailing_offer(text, action_succeeded=True) == text


def test_trailing_whitespace():
    text = "The printer light is off. Would you like me to adjust anything else?  \n"
    assert (
        strip_trailing_offer(text, action_succeeded=True)
        == "The printer light is off."
    )


def test_multiple_sentences():
    text = (
        "The kitchen light is on. The printer light is off. Need anything else?"
    )
    assert (
        strip_trailing_offer(text, action_succeeded=True)
        == "The kitchen light is on. The printer light is off."
    )


def test_keeps_non_question_ending():
    text = "The printer light is off. It is in the bedroom."
    assert strip_trailing_offer(text, action_succeeded=True) == text


def test_empty_and_whitespace():
    assert strip_trailing_offer("", action_succeeded=True) == ""
    assert strip_trailing_offer("   ", action_succeeded=True) == "   "


def test_exclamation_then_question():
    text = "Done! Want anything else?"
    assert strip_trailing_offer(text, action_succeeded=True) == "Done!"


def test_decimal_is_not_a_sentence_boundary():
    text = "Set to 7.3 degrees. Need anything else?"
    assert (
        strip_trailing_offer(text, action_succeeded=True) == "Set to 7.3 degrees."
    )


def test_save_memory_triggers_strip():
    text = "I've saved that, miss. Would you like me to adjust anything else?"
    assert is_action_tool("save_memory")
    assert is_action_tool("home_save_memory")
    assert (
        strip_trailing_offer(text, action_succeeded=is_action_tool("save_memory"))
        == "I've saved that, miss."
    )


def test_recall_memory_does_not_trigger_strip():
    text = "You prefer dim lights. Would you like me to adjust anything else?"
    assert not is_action_tool("recall_memory")
    assert not is_action_tool("home_recall_memory")
    assert (
        strip_trailing_offer(
            text, action_succeeded=is_action_tool("recall_memory")
        )
        == text
    )


def test_action_tool_classification():
    assert is_action_tool("perform_action")
    assert is_action_tool("call_service")
    assert is_action_tool("run_script")
    assert is_action_tool("run_automation")
    assert is_action_tool("home_perform_action")
    assert not is_action_tool("set_conversation_state")
    assert not is_action_tool("discover_entities")
    assert not is_action_tool("get_entity_details")
    assert not is_action_tool("get_entity_history")
    assert not is_action_tool("list_areas")
    assert not is_action_tool("list_domains")
    assert not is_action_tool("search")
    assert not is_action_tool("read_url")
    assert not is_action_tool("")
    assert not is_action_tool(None)
