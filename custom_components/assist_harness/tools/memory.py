"""[kestrel] Long-term memory for Assist Harness (fork addition).

Talks to Kira's self-hosted memory service (X-API-Key auth, POST /memories, POST /search):
- tools ``recall_memory(query)`` and ``save_memory(fact)`` for the LLM, and
- ``schedule_turn_save()``: posts each finished turn to the service for fact extraction
  as a background task, so it never delays the reply and an outage only logs a warning.

Settings live on the ``memory`` tool subentry of the system entry (``memory_enabled``,
``memory_url``, ``memory_api_key``, ``memory_user_id``). The shared MCP server uses the first
enabled config; turn auto-save uses the config visible to the profile that just spoke.

Older installs stored this tool as ``mem0_memory`` with ``mem0_*`` keys. ``upgrade_legacy_config``
and ``upgrade_tool_record`` convert those; migration.py applies them.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DOMAIN = "assist_harness"
TOOL_TYPE = "memory"
CONF_MEMORY_ENABLED = "memory_enabled"
CONF_MEMORY_URL = "memory_url"
CONF_MEMORY_API_KEY = "memory_api_key"
CONF_MEMORY_USER_ID = "memory_user_id"
DEFAULT_MEMORY_URL = "http://127.0.0.1:8890"
DEFAULT_MEMORY_USER_ID = "kira"
MEMORY_KEYS = (CONF_MEMORY_ENABLED, CONF_MEMORY_URL, CONF_MEMORY_API_KEY, CONF_MEMORY_USER_ID)

# ---- legacy (Mem0-era) names, read only by migrations ----
LEGACY_TOOL_TYPE = "mem0_memory"
LEGACY_KEY_MAP = {
    "mem0_enabled": CONF_MEMORY_ENABLED,
    "mem0_url": CONF_MEMORY_URL,
    "mem0_api_key": CONF_MEMORY_API_KEY,
    "mem0_user_id": CONF_MEMORY_USER_ID,
}
# The retired self-hosted Mem0 server; configs pointing at it move to the memory service.
LEGACY_DEFAULT_URLS = ("http://127.0.0.1:8888", "http://localhost:8888")
LEGACY_TITLES = ("Mem0 memory", "Mem0")

# Tool calls wait for these, so keep them short. Search = one embedding call + pgvector.
TOOL_TIMEOUT = 6  # seconds
# Background turn save: extraction runs inside the request and can take a few seconds,
# but it is off the reply path.
SAVE_TIMEOUT = 60
CONNECT_TIMEOUT = 2
RECALL_TOP_K = 5

# Per-turn state. Each conversation turn runs in its own asyncio task, so a ContextVar holding a
# fresh dict per turn keeps concurrent conversations apart; tool-call subtasks (asyncio.gather)
# inherit the same dict and can mark it.
_TURN: ContextVar[Optional[Dict[str, bool]]] = ContextVar("assist_harness_memory_turn", default=None)


def has_legacy_keys(mapping: Dict[str, Any]) -> bool:
    return any(key in mapping for key in LEGACY_KEY_MAP)


def upgrade_legacy_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``config`` with ``mem0_*`` keys renamed and the retired Mem0 URL replaced.

    Keys that are already new win over legacy ones; other keys (e.g. ``prefix``) are kept.
    """
    out: Dict[str, Any] = {}
    for key, value in dict(config).items():
        new = LEGACY_KEY_MAP.get(key)
        if new is None:
            out[key] = value
        elif new not in config:
            out[new] = value
    url = str(out.get(CONF_MEMORY_URL) or "").rstrip("/")
    if not url or url in LEGACY_DEFAULT_URLS:
        out[CONF_MEMORY_URL] = DEFAULT_MEMORY_URL
    return out


def upgrade_title(title: Optional[str]) -> str:
    if not title or title in LEGACY_TITLES:
        return "Memory"
    return title.replace("Mem0 memory", "Memory").replace("Mem0", "Memory")


def upgrade_tool_record(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a stored ``mem0_memory`` tool record (v2 ``tools`` list) to ``memory``."""
    if not isinstance(tool, dict) or tool.get("type") != LEGACY_TOOL_TYPE:
        return tool
    out = dict(tool)
    out["type"] = TOOL_TYPE
    if out.get("instance_id") in (None, "", LEGACY_TOOL_TYPE):
        out["instance_id"] = TOOL_TYPE
    out["config"] = upgrade_legacy_config(dict(tool.get("config") or {}))
    if out.get("title"):
        out["title"] = upgrade_title(out["title"])
    return out


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


def settings_from_config(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Normalize a memory config mapping. Disabled or missing config returns None."""
    if not config or not config.get(CONF_MEMORY_ENABLED):
        return None
    return {
        "url": (config.get(CONF_MEMORY_URL) or DEFAULT_MEMORY_URL).rstrip("/"),
        "api_key": config.get(CONF_MEMORY_API_KEY) or "",
        "user_id": config.get(CONF_MEMORY_USER_ID) or DEFAULT_MEMORY_USER_ID,
    }


def _memory_configs(hass, entry=None) -> list[Dict[str, Any]]:
    """Memory configs on the system entry visible to ``entry`` (all of them when entry is None)."""
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:
        return []
    from ..const import SYSTEM_ENTRY_UNIQUE_ID
    from .registry import disabled_tool_ids

    disabled = disabled_tool_ids(entry) if entry is not None else set()
    system = next((item for item in entries if item.unique_id == SYSTEM_ENTRY_UNIQUE_ID), None)
    owners = [system] if system is not None else [item for item in entries if item is not entry]
    configs: list[Dict[str, Any]] = []
    for owner in owners:
        for subentry in getattr(owner, "subentries", {}).values():
            if subentry.subentry_type != TOOL_TYPE:
                continue
            if subentry.subentry_id in disabled or (
                subentry.unique_id and subentry.unique_id in disabled
            ):
                continue
            configs.append({k: v for k, v in dict(subentry.data).items() if k != "prefix"})
    return configs


def get_settings(hass, entry=None) -> Optional[Dict[str, str]]:
    """Memory settings visible to ``entry``, or the first enabled config."""
    for config in _memory_configs(hass, entry):
        settings = settings_from_config(config)
        if settings:
            return settings
    return None


async def _post(hass, settings, path: str, body: Dict[str, Any], timeout: float) -> Any:
    import aiohttp

    session = async_get_clientsession(hass)
    async with session.post(
        settings["url"] + path,
        json=body,
        headers={"X-API-Key": settings["api_key"]},
        timeout=aiohttp.ClientTimeout(total=timeout, connect=CONNECT_TIMEOUT),
    ) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"memory {path} HTTP {resp.status}")
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
                "source": "voice",
            },
            SAVE_TIMEOUT,
        )
    except Exception as err:  # never let memory break a conversation
        _LOGGER.warning("Memory turn save failed: %s", err or type(err).__name__)


def schedule_turn_save(hass, entry, user_text: str, reply_text: str) -> None:
    """Fire-and-forget: send a finished turn to the memory service for extraction."""
    try:
        settings = get_settings(hass, entry)
        if not settings or not user_text or not reply_text:
            return
        turn = _TURN.get()
        if turn is not None and turn.get("save_memory"):
            # save_memory already stored this turn's fact; a turn save would duplicate it
            _LOGGER.debug("Memory turn save skipped: save_memory ran this turn")
            return
        hass.async_create_background_task(
            _save_turn(hass, settings, user_text, reply_text),
            name="assist_harness_memory_turn_save",
        )
    except Exception as err:
        _LOGGER.warning("Memory turn save not scheduled: %s", err)


def _text(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


class MemoryTool:
    """recall_memory / save_memory backed by the memory service. Inactive unless enabled."""

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
        return self.definition_bodies()

    @staticmethod
    def definition_bodies() -> List[Dict[str, Any]]:
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
                    {"query": query, "user_id": settings["user_id"], "top_k": RECALL_TOP_K},
                    TOOL_TIMEOUT,
                )
                results = data.get("results", []) if isinstance(data, dict) else []
                lines = [
                    f"- {r.get('text')}"
                    for r in results
                    if isinstance(r, dict) and r.get("text")
                ]
                return _text("\n".join(lines) if lines else "No relevant memories.")

            fact = str(arguments.get("fact") or "").strip()
            if not fact:
                return _text("No fact given.")
            # Save in the background and answer at once.
            self.hass.async_create_background_task(
                self._save_fact(settings, fact), name="assist_harness_memory_save_fact"
            )
            return _text("Saved to memory.")
        except Exception as err:
            _LOGGER.warning("Memory %s failed: %s", tool_name, err or type(err).__name__)
            return _text("Memory is unavailable right now.")

    async def _save_fact(self, settings, fact: str) -> None:
        # Stored verbatim (the model already wrote the fact); the service merges near-duplicates.
        try:
            await _post(
                self.hass,
                settings,
                "/memories",
                {"text": fact, "user_id": settings["user_id"], "source": "voice"},
                SAVE_TIMEOUT,
            )
        except Exception as err:
            _LOGGER.warning("Memory save_memory failed: %s", err or type(err).__name__)


from .base import Tool, ToolInstance, apply_prefix, strip_prefix


class _Registered(Tool):
    """One memory tool on the system entry."""

    id = TOOL_TYPE
    name = "Memory"
    description = (
        "Long-term memory via Kira's memory service. Enable it, set the service URL "
        f"({DEFAULT_MEMORY_URL}), API key, and user id. recall_memory and save_memory are "
        "listed while it is enabled, and each finished turn is saved in the background."
    )
    shared = False
    allow_multiple = False
    secrets = frozenset({CONF_MEMORY_API_KEY})
    config_schema = vol.Schema(
        {
            vol.Required(CONF_MEMORY_ENABLED, default=False): bool,
            vol.Optional(CONF_MEMORY_URL, default=DEFAULT_MEMORY_URL): str,
            vol.Optional(CONF_MEMORY_API_KEY, default=""): str,
            vol.Optional(CONF_MEMORY_USER_ID, default=DEFAULT_MEMORY_USER_ID): str,
        }
    )

    async def async_setup(self, hass, instance: ToolInstance) -> None:
        runtime = MemoryTool(hass)
        await runtime.initialize()
        instance.runtime = runtime

    async def async_teardown(self, hass, instance: ToolInstance) -> None:
        instance.runtime = None

    def definitions(self, hass, instance: ToolInstance):
        if settings_from_config(instance.config) is None:
            return []
        return apply_prefix(instance, MemoryTool.definition_bodies())

    async def async_call(self, hass, instance, tool_name, arguments):
        runtime = instance.runtime or MemoryTool(hass)
        return await runtime.handle_call(
            strip_prefix(instance, tool_name),
            arguments,
            settings_from_config(instance.config),
        )


TOOL = _Registered()
