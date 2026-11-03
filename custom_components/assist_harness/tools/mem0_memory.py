"""[kestrel] Mem0 long-term memory for Assist Harness (fork addition).

Talks to Mem0 over its REST API: a self-hosted server (X-API-Key auth, /memories, /search), or
the hosted Mem0 Platform when the URL is https://api.mem0.ai ("Authorization: Token" auth,
/v3/memories/add/, /v3/memories/search/; adds are queued server-side and return at once):
- tools ``recall_memory(query)`` and ``save_memory(fact)`` for the LLM, and
- ``schedule_turn_save()``: posts each finished turn to Mem0 for fact extraction
  as a background task, so it never delays the reply and a Mem0 outage only logs a warning.

Settings live on a per-profile ``mem0_memory`` tool (``mem0_enabled``, ``mem0_url``,
``mem0_api_key``, ``mem0_user_id``). Flat options of that shape still work until
migration. The shared MCP server uses the first profile with Mem0 enabled.
Turn auto-save uses the profile that just spoke.
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

DOMAIN = "assist_harness"
CONF_MEM0_ENABLED = "mem0_enabled"
CONF_MEM0_URL = "mem0_url"
CONF_MEM0_API_KEY = "mem0_api_key"
CONF_MEM0_USER_ID = "mem0_user_id"
DEFAULT_MEM0_URL = "http://127.0.0.1:8888"
DEFAULT_MEM0_USER_ID = "kira"
PLATFORM_HOST = "api.mem0.ai"
# Logical path -> Mem0 Platform path
_PLATFORM_PATHS = {"/memories": "/v3/memories/add/", "/search": "/v3/memories/search/"}

# Tool calls wait for these, so keep them short. Mem0 search = one embedding call + pgvector.
TOOL_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=2)
# Background turn save: 2 s to reach Mem0; self-hosted extraction runs inside the request and can
# take several seconds (the Platform queues it and answers at once), but it is off the reply path.
SAVE_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=2)
RECALL_TOP_K = 5

# Per-turn state. Each conversation turn runs in its own asyncio task, so a ContextVar holding a
# fresh dict per turn keeps concurrent conversations apart; tool-call subtasks (asyncio.gather)
# inherit the same dict and can mark it.
_TURN: ContextVar[Optional[Dict[str, bool]]] = ContextVar("assist_harness_mem0_turn", default=None)


def begin_turn() -> None:
    """Start a new turn (call at the start of each conversation turn)."""
    _TURN.set({"save_memory": False})


def note_tool_call(tool_name: Optional[str]) -> None:
    """Record that save_memory ran (or was attempted) in this turn.

    A tool-name prefix (``home_save_memory``) still counts, so the turn
    auto-save stays skipped after an explicit save.
    """
    turn = _TURN.get()
    if turn is None or not tool_name:
        return
    if tool_name == "save_memory" or tool_name.endswith("_save_memory"):
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


def is_platform(url: str) -> bool:
    """True when ``url`` points at the hosted Mem0 Platform rather than a self-hosted server."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host == PLATFORM_HOST or host.endswith("." + PLATFORM_HOST)


def _config_of(entry) -> Optional[Dict[str, Any]]:
    """Return this entry's mem0_memory config, or legacy flat options."""
    for source in (entry.options, entry.data):
        if "tools" not in source:
            continue
        for tool in source.get("tools") or []:
            if isinstance(tool, dict) and tool.get("type") == "mem0_memory":
                return dict(tool.get("config") or {})
    opts = {**entry.data, **entry.options}
    if any(
        key in opts
        for key in (CONF_MEM0_ENABLED, CONF_MEM0_URL, CONF_MEM0_API_KEY, CONF_MEM0_USER_ID)
    ):
        return opts
    return None


def settings_from_config(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Normalize a mem0 config mapping. Disabled or missing config returns None."""
    if not config or not config.get(CONF_MEM0_ENABLED):
        return None
    url = (config.get(CONF_MEM0_URL) or DEFAULT_MEM0_URL).rstrip("/")
    return {
        "url": url,
        "api_key": config.get(CONF_MEM0_API_KEY) or "",
        "user_id": config.get(CONF_MEM0_USER_ID) or DEFAULT_MEM0_USER_ID,
    }


def _settings_of(entry) -> Optional[Dict[str, str]]:
    return settings_from_config(_config_of(entry))


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
    if is_platform(settings["url"]):
        path = _PLATFORM_PATHS.get(path, path)
        headers = {"Authorization": "Token " + settings["api_key"], "Accept": "application/json"}
    else:
        headers = {"X-API-Key": settings["api_key"]}
    async with session.post(
        settings["url"] + path,
        json=body,
        headers=headers,
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
            name="assist_harness_mem0_turn_save",
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
        return self._DEFINITION_BODIES()

    @staticmethod
    def _DEFINITION_BODIES() -> List[Dict[str, Any]]:
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

    async def handle_call(self, tool_name: str, arguments: Dict[str, Any], settings=None) -> Dict[str, Any]:
        if settings is None:
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
                self._save_fact(settings, fact), name="assist_harness_mem0_save_fact"
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


from .base import Tool, ToolInstance, apply_prefix, strip_prefix


class _Registered(Tool):
    """Per-profile Mem0. One instance per profile keeps the previous behaviour."""

    id = "mem0_memory"
    name = "Mem0 memory"
    description = (
        "Long-term memory via Mem0. Enable it, set the server URL "
        "(https://api.mem0.ai for the hosted platform), API key, and user id. "
        "recall_memory and save_memory are listed while it is enabled, and each "
        "finished turn is saved in the background."
    )
    shared = False
    allow_multiple = False
    secrets = frozenset({CONF_MEM0_API_KEY})
    config_schema = vol.Schema(
        {
            vol.Required(CONF_MEM0_ENABLED, default=False): bool,
            vol.Optional(CONF_MEM0_URL, default=DEFAULT_MEM0_URL): str,
            vol.Optional(CONF_MEM0_API_KEY, default=""): str,
            vol.Optional(CONF_MEM0_USER_ID, default=DEFAULT_MEM0_USER_ID): str,
        }
    )

    async def async_setup(self, hass, instance: ToolInstance) -> None:
        runtime = Mem0MemoryTool(hass)
        await runtime.initialize()
        instance.runtime = runtime

    async def async_teardown(self, hass, instance: ToolInstance) -> None:
        instance.runtime = None

    def definitions(self, hass, instance: ToolInstance):
        if settings_from_config(instance.config) is None:
            return []
        runtime = instance.runtime or Mem0MemoryTool(hass)
        # The class hides definitions unless some profile is enabled. Build from
        # its schema by asking with a temporary view of this instance's config.
        raw = _definitions_for(instance.config)
        return apply_prefix(instance, raw)

    async def async_call(self, hass, instance, tool_name, arguments):
        runtime = instance.runtime or Mem0MemoryTool(hass)
        return await runtime.handle_call(
            strip_prefix(instance, tool_name),
            arguments,
            settings_from_config(instance.config),
        )


def _definitions_for(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    if settings_from_config(config) is None:
        return []
    return Mem0MemoryTool._DEFINITION_BODIES()


TOOL = _Registered()
