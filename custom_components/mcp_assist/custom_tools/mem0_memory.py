"""Optional long-term memory backed by a self-hosted Mem0 REST server.

Profiles opt in with ``mem0_enabled`` (off by default). While enabled, the
shared MCP server exposes ``recall_memory`` and ``save_memory``. A separate
``mem0_auto_save`` option (also off by default) posts each finished turn to
``POST /memories`` in the background. Failures are logged and never fail
the reply.

The shared MCP server has one tool list, so recall and save use the first
profile that has Mem0 enabled. Automatic turn saves use the profile that
handled the conversation.
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

from ..const import (
    CONF_MEM0_API_KEY,
    CONF_MEM0_AUTO_SAVE,
    CONF_MEM0_ENABLED,
    CONF_MEM0_URL,
    CONF_MEM0_USER_ID,
    DEFAULT_MEM0_API_KEY,
    DEFAULT_MEM0_AUTO_SAVE,
    DEFAULT_MEM0_ENABLED,
    DEFAULT_MEM0_URL,
    DEFAULT_MEM0_USER_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Tool calls block the reply, so keep them short. Search is one embedding plus
# a vector lookup. The background save may wait on server-side extraction.
TOOL_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=2)
SAVE_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=2)
RECALL_TOP_K = 5

# Each conversation turn runs in its own task. A ContextVar holding a fresh
# dict keeps concurrent turns apart; tool-call subtasks inherit that dict.
_TURN: ContextVar[Optional[Dict[str, bool]]] = ContextVar(
    "mcp_assist_mem0_turn", default=None
)


def begin_turn() -> None:
    """Start per-turn memory state. Call once at the start of a turn."""
    _TURN.set({"save_memory": False})


def note_tool_call(tool_name: Optional[str]) -> None:
    """Record that save_memory was invoked during this turn."""
    turn = _TURN.get()
    if turn is not None and tool_name == "save_memory":
        turn["save_memory"] = True


def _current(options: Dict[str, Any], key: str, default: Any) -> Any:
    value = options.get(key, default)
    return default if value is None else value


def options_schema(options: Dict[str, Any]) -> Dict[Any, Any]:
    """Profile options for Mem0. Both switches default to off."""
    return {
        vol.Required(
            CONF_MEM0_ENABLED,
            default=_current(options, CONF_MEM0_ENABLED, DEFAULT_MEM0_ENABLED),
        ): bool,
        vol.Optional(
            CONF_MEM0_URL,
            default=_current(options, CONF_MEM0_URL, DEFAULT_MEM0_URL),
        ): str,
        vol.Optional(
            CONF_MEM0_API_KEY,
            default=_current(options, CONF_MEM0_API_KEY, DEFAULT_MEM0_API_KEY),
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
        vol.Optional(
            CONF_MEM0_USER_ID,
            default=_current(options, CONF_MEM0_USER_ID, DEFAULT_MEM0_USER_ID),
        ): str,
        vol.Required(
            CONF_MEM0_AUTO_SAVE,
            default=_current(options, CONF_MEM0_AUTO_SAVE, DEFAULT_MEM0_AUTO_SAVE),
        ): bool,
    }


def _settings_of(entry) -> Optional[Dict[str, Any]]:
    """Return connection settings, or None when this profile has Mem0 off."""
    opts = {**entry.data, **entry.options}
    if not opts.get(CONF_MEM0_ENABLED, DEFAULT_MEM0_ENABLED):
        return None
    url = str(opts.get(CONF_MEM0_URL) or DEFAULT_MEM0_URL).strip().rstrip("/")
    if not url:
        return None
    user_id = str(opts.get(CONF_MEM0_USER_ID) or DEFAULT_MEM0_USER_ID).strip()
    return {
        "url": url,
        "api_key": opts.get(CONF_MEM0_API_KEY) or "",
        "user_id": user_id or DEFAULT_MEM0_USER_ID,
        "auto_save": bool(opts.get(CONF_MEM0_AUTO_SAVE, DEFAULT_MEM0_AUTO_SAVE)),
    }


def get_settings(hass, entry=None) -> Optional[Dict[str, Any]]:
    """Settings for ``entry``, or for the first profile with Mem0 enabled."""
    if entry is not None:
        return _settings_of(entry)
    for candidate in hass.config_entries.async_entries(DOMAIN):
        settings = _settings_of(candidate)
        if settings:
            return settings
    return None


def any_profile_enabled(hass) -> bool:
    """Whether any profile has opted in to Mem0."""
    return get_settings(hass) is not None


async def _post(hass, settings: Dict[str, Any], path: str, body: Dict[str, Any], timeout) -> Any:
    """POST to the Mem0 server. Raises on HTTP errors and connection failures."""
    headers = {}
    if settings.get("api_key"):
        headers["X-API-Key"] = settings["api_key"]
    session = async_get_clientsession(hass)
    async with session.post(
        settings["url"] + path,
        json=body,
        headers=headers,
        timeout=timeout,
    ) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Mem0 {path} HTTP {resp.status}")
        return await resp.json()


async def _save_turn(hass, settings: Dict[str, Any], user_text: str, reply_text: str) -> None:
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
                "metadata": {"source": "mcp_assist"},
            },
            SAVE_TIMEOUT,
        )
    except Exception as err:  # a memory outage must not break the reply
        _LOGGER.warning("Mem0 turn save failed: %s", err or type(err).__name__)


def schedule_turn_save(hass, entry, user_text: str, reply_text: str) -> None:
    """Fire-and-forget extraction of a finished turn. No-op unless auto-save is on."""
    try:
        settings = get_settings(hass, entry)
        user_text = (user_text or "").strip()
        reply_text = (reply_text or "").strip()
        if not settings or not settings.get("auto_save") or not user_text or not reply_text:
            return
        turn = _TURN.get()
        if turn is not None and turn.get("save_memory"):
            # save_memory already stored this turn's fact
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
    """recall_memory / save_memory. Definitions are empty until a profile opts in."""

    TOOL_NAMES = ("recall_memory", "save_memory")

    def __init__(self, hass):
        """Initialize the tool."""
        self.hass = hass

    async def initialize(self):
        """Nothing to set up until a call is made."""

    def handles_tool(self, tool_name: str) -> bool:
        """Return True when this tool name is active for the current settings."""
        return tool_name in self.TOOL_NAMES and get_settings(self.hass) is not None

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """MCP tool definitions, or an empty list when Mem0 is off."""
        if get_settings(self.hass) is None:
            return []
        return [
            {
                "name": "recall_memory",
                "description": (
                    "Search this user's long-term memory for preferences, facts, "
                    "and past conversations. Use when earlier context could matter."
                ),
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look for, for example 'favourite tea'",
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "save_memory",
                "description": (
                    "Save one lasting fact or preference the user states, as a short "
                    "sentence (for example, 'The user's favourite tea is Earl Grey')."
                ),
                "inputSchema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "The fact to remember",
                        }
                    },
                    "required": ["fact"],
                    "additionalProperties": False,
                },
            },
        ]

    async def handle_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Run recall_memory or save_memory. Errors become a short unavailable message."""
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
                    f"- {item.get('memory')}"
                    for item in (results or [])
                    if isinstance(item, dict) and item.get("memory")
                ]
                return _text("\n".join(lines) if lines else "No relevant memories.")

            fact = str(arguments.get("fact") or "").strip()
            if not fact:
                return _text("No fact given.")
            # Extraction can take several seconds, so answer immediately.
            self.hass.async_create_background_task(
                self._save_fact(settings, fact),
                name="mcp_assist_mem0_save_fact",
            )
            return _text("Saved to memory.")
        except Exception as err:
            _LOGGER.warning("Mem0 %s failed: %s", tool_name, err or type(err).__name__)
            return _text("Memory is unavailable right now.")

    async def _save_fact(self, settings: Dict[str, Any], fact: str) -> None:
        try:
            await _post(
                self.hass,
                settings,
                "/memories",
                {
                    "messages": [{"role": "user", "content": fact}],
                    "user_id": settings["user_id"],
                    "metadata": {"source": "mcp_assist"},
                },
                SAVE_TIMEOUT,
            )
        except Exception as err:
            _LOGGER.warning("Mem0 save_memory failed: %s", err or type(err).__name__)
