"""The agent answers plain time and date questions without calling the model."""

from __future__ import annotations

import dataclasses
from datetime import datetime

from homeassistant.components.conversation import ConversationInput
from homeassistant.core import Context
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.assist_harness.agent import MCPAssistConversationEntity
from custom_components.assist_harness.const import DOMAIN


def _entity(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="assist_harness_openai_kestrel",
        title="OpenAI - Kestrel",
        data={
            "profile_name": "Kestrel",
            "server_type": "openai",
            "model_name": "Qwen3.5-9B",
            "lmstudio_url": "http://127.0.0.1:8010",
            "system_prompt": "be kind",
            "technical_prompt": "Time: {time}\nDate: {date}",
        },
        options={},
        version=3,
    )
    entry.add_to_hass(hass)
    return MCPAssistConversationEntity(hass, entry)


def _input(text: str) -> ConversationInput:
    values = {
        "text": text,
        "context": Context(),
        "conversation_id": None,
        "device_id": None,
        "language": "en",
        "agent_id": "conversation.openai_kestrel",
    }
    names = {f.name for f in dataclasses.fields(ConversationInput)}
    for field in dataclasses.fields(ConversationInput):
        if field.name not in values and field.default is dataclasses.MISSING and (
            field.default_factory is dataclasses.MISSING
        ):
            values[field.name] = None
    return ConversationInput(**{k: v for k, v in values.items() if k in names})


async def _ask(hass, monkeypatch, text: str, now: datetime):
    entity = _entity(hass)
    calls = []

    async def _fake_llm(messages):
        calls.append(messages)
        return "It is half past ten."

    monkeypatch.setattr(entity, "_call_llm", _fake_llm)
    monkeypatch.setattr(dt_util, "now", lambda *a, **k: now)
    result = await entity._async_handle_message_inner(_input(text), "conv-1")
    return result.response.speech["plain"]["speech"], calls


async def test_time_question_skips_the_model(hass, monkeypatch):
    now = datetime(2026, 11, 27, 0, 55, tzinfo=dt_util.get_time_zone("America/Vancouver"))
    speech, calls = await _ask(hass, monkeypatch, "What time is it?", now)
    assert speech == "It's five to one."
    assert calls == []


async def test_date_question_skips_the_model(hass, monkeypatch):
    now = datetime(2026, 11, 27, 0, 55, tzinfo=dt_util.get_time_zone("America/Vancouver"))
    speech, calls = await _ask(hass, monkeypatch, "What's the date?", now)
    assert speech == "It's Friday November 27th 2026."
    assert calls == []


async def test_device_request_still_goes_to_the_model(hass, monkeypatch):
    now = datetime(2026, 11, 27, 0, 55, tzinfo=dt_util.get_time_zone("America/Vancouver"))
    speech, calls = await _ask(hass, monkeypatch, "Turn off the printer light", now)
    assert len(calls) == 1
    assert "Time: five to one in the morning" in calls[0][-1]["content"]
