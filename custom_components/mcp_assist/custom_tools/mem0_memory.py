"""[kestrel] Mem0 long-term memory for MCP Assist (fork addition).

Talks to a self-hosted Mem0 server over its REST API (X-API-Key auth):
- tools ``recall_memory(query)`` and ``save_memory(fact)`` for the LLM, and
- ``schedule_turn_save()``: posts each finished turn to Mem0 for fact extraction
  as a background task, so it never delays the reply and a Mem0 outage only logs a warning.

Settings are per profile (options ``mem0_enabled``, ``mem0_url``, ``mem0_api_key``,
``mem0_user_id``). The shared MCP server uses the first profile with Mem0 enabled.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

import aiohttp
import voluptuous as vol
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mcp_assist"
CONF_MEM0_ENABLED = "mem0_enabled"
CONF_MEM0_URL = "mem0_url"
CONF_MEM0_API_KEY = "mem0_api_key"
CONF_MEM0_USER_ID = "mem0_user_id"
DEFAULT_MEM0_URL = "http://127.0.0.1:8888"
DEFAULT_MEM0_USER_ID = "kira"

# Tool calls wait for these, so keep them short. Mem0 search = one embedding call + pgvector.
TOOL_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=2)
# Background turn save: 2 s to reach Mem0; extraction itself runs server-side and can take
# several seconds, but it is off the reply path.
SAVE_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=2)
RECALL_TOP_K = 5

# Per-turn state. Each conversation turn runs in its own asyncio task, so a ContextVar holding a
# fresh dict per turn keeps concurrent conversations apart; tool-call subtasks (asyncio.gather)
# inherit the same dict and can mark it.
_TURN: ContextVar[Optional[Dict[str, bool]]] = ContextVar("mcp_assist_mem0_turn", default=None)


def begin_turn() -> None:
    """Start a new turn (call at the start of each conversation turn)."""
    _TURN.set({"save_memory": False})


def note_tool_call(tool_name: Optional[str]) -> None:
    """Record that save_memory ran (or was attempted) in this turn."""
    turn = _TURN.get()
    if turn is not None and tool_name == "save_memory":
        turn["save_memory"] = True


def options_schema(options: Dict[str, Any]) -> Dict[Any, Any]:
    """Options-flow fields for a profile (added to the profile's options form)."""
    return {
        vol.Required(
            CONF_MEM0_ENABLED, default=options.get(CONF_MEM0_ENABLED, False)
        ): bool,
        vol.Optional(
            CONF_MEM0_URL, default=options.get(CONF_MEM0_URL, DEFAULT_MEM0_URL)
        ): str,
        vol.Optional(
            CONF_MEM0_API_KEY, default=options.get(CONF_MEM0_API_KEY, "")
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(
            CONF_MEM0_USER_ID,
            default=options.get(CONF_MEM0_USER_ID, DEFAULT_MEM0_USER_ID),
        ): str,
    }


def _settings_of(entry) -> Optional[Dict[str, str]]:
    opts = {**entry.data, **entry.options}
    if not opts.get(CONF_MEM0_ENABLED):
        return None
    url = (opts.get(CONF_MEM0_URL) or DEFAULT_MEM0_URL).rstrip("/")
    return {
        "url": url,
        "api_key": opts.get(CONF_MEM0_API_KEY) or "",
        "user_id": opts.get(CONF_MEM0_USER_ID) or DEFAULT_MEM0_USER_ID,
    }


def get_settings(hass, entry=None) -> Optional[Dict[str, str]]:
    """Mem0 settings of ``entry``, or of the first profile with Mem0 enabled."""
    if entry is not None:
        return _settings_of(entry)
    for candidate in hass.config_entries.async_entries(DOMAIN):
        settings = _settings_of(candidate)
        if settings:
            return settings
    return None


async def _post(hass, settings, path: str, body: Dict[str, Any], timeout) -> Any:
    session = async_get_clientsession(hass)
    async with session.post(
        settings["url"] + path,
        json=body,
        headers={"X-API-Key": settings["api_key"]},
        timeout=timeout,
    ) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Mem0 {path} HTTP {resp.status}")
        return await resp.json()


async def _save_turn(hass, settings, user_text: str, reply_text: str) -> None:
    try:
        await _post(
            hass,
            settings,
            "/memories",
            {
                "messages": [
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": reply_text},
                ],
                "user_id": settings["user_id"],
                "metadata": {"source": "voice"},
            },
            SAVE_TIMEOUT,
        )
    except Exception as err:  # never let memory break a conversation
        _LOGGER.warning("Mem0 turn save failed: %s", err or type(err).__name__)


def schedule_turn_save(hass, entry, user_text: str, reply_text: str) -> None:
    """Fire-and-forget: send a finished turn to Mem0 for extraction."""
    try:
        settings = get_settings(hass, entry)
        if not settings or not user_text or not reply_text:
            return
        turn = _TURN.get()
        if turn is not None and turn.get("save_memory"):
            # save_memory already stored this turn's fact; a turn save would duplicate it
            _LOGGER.debug("Mem0 turn save skipped: save_memory ran this turn")
            return
        hass.async_create_background_task(
            _save_turn(hass, settings, user_text, reply_text),
            name="mcp_assist_mem0_turn_save",
        )
    except Exception as err:
        _LOGGER.warning("Mem0 turn save not scheduled: %s", err)


def _text(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class Mem0MemoryTool:
    """recall_memory / save_memory backed by Mem0. Inactive unless a profile enables Mem0."""

    TOOL_NAMES = ("recall_memory", "save_memory")

    def __init__(self, hass):
        self.hass = hass

    async def initialize(self):
        pass

    def handles_tool(self, tool_name: str) -> bool:
        return tool_name in self.TOOL_NAMES and get_settings(self.hass) is not None

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        if get_settings(self.hass) is None:
            return []
        return [
            {
                "name": "recall_memory",
                "description": (
                    "Search long-term memory about Kira (preferences, facts, past "
                    "conversations). Use when past context or her preferences could matter."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look for, e.g. 'favourite tea'",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "save_memory",
                "description": (
                    "Save a lasting fact or preference Kira states, as one short sentence "
                    "(e.g. 'Kira's favourite tea is Earl Grey')."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string", "description": "The fact to remember"}
                    },
                    "required": ["fact"],
                    "additionalProperties": False,
                },
            },
        ]

    async def handle_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        settings = get_settings(self.hass)
        if not settings:
            return _text("Memory is not enabled.")
        try:
            if tool_name == "recall_memory":
                query = str(arguments.get("query") or "").strip()
                if not query:
                    return _text("No query given.")
                data = await _post(
                    self.hass,
                    settings,
                    "/search",
                    {
                        "query": query,
                        "filters": {"user_id": settings["user_id"]},
                        "top_k": RECALL_TOP_K,
                    },
                    TOOL_TIMEOUT,
                )
                results = data.get("results", data) if isinstance(data, dict) else data
                lines = [
                    f"- {r.get('memory')}"
                    for r in (results or [])
                    if isinstance(r, dict) and r.get("memory")
                ]
                return _text("\n".join(lines) if lines else "No relevant memories.")

            fact = str(arguments.get("fact") or "").strip()
            if not fact:
                return _text("No fact given.")
            # Extraction can take seconds: save in the background and answer at once.
            self.hass.async_create_background_task(
                self._save_fact(settings, fact), name="mcp_assist_mem0_save_fact"
            )
            return _text("Saved to memory.")
        except Exception as err:
            _LOGGER.warning("Mem0 %s failed: %s", tool_name, err or type(err).__name__)
            return _text("Memory is unavailable right now.")

    async def _save_fact(self, settings, fact: str) -> None:
        try:
            await _post(
                self.hass,
                settings,
                "/memories",
                {
                    "messages": [{"role": "user", "content": fact}],
                    "user_id": settings["user_id"],
                    "metadata": {"source": "voice"},
                },
                SAVE_TIMEOUT,
            )
        except Exception as err:
            _LOGGER.warning("Mem0 save_memory failed: %s", err or type(err).__name__)
