"""Convert MCP Assist settings into Assist Harness tool instances.

Version 1 stored one shared search provider and flat per-profile Mem0 keys.
Version 2 stores a ``tools`` list. Version 3 stores tools as subentries of the
system entry; 3.3 renames the Mem0-era ``mem0_memory`` tool to ``memory``.
Importing an ``mcp_assist`` config entry uses the same conversion and never
writes back to the original entry.
"""
from __future__ import annotations

import copy
import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import (
    CONF_BRAVE_API_KEY,
    CONF_ENABLE_CUSTOM_TOOLS,
    CONF_SEARCH_PROVIDER,
    CONF_SEARXNG_URL,
    DEFAULT_MCP_PORT,
    DOMAIN,
    SYSTEM_ENTRY_UNIQUE_ID,
)
from .tools.memory import (
    CONF_MEMORY_ENABLED,
    CONF_MEMORY_USER_ID,
    DEFAULT_MEMORY_USER_ID,
    LEGACY_KEY_MAP,
    LEGACY_TOOL_TYPE,
    TOOL_TYPE as MEMORY_TOOL_TYPE,
    has_legacy_keys,
    upgrade_legacy_config,
    upgrade_title,
    upgrade_tool_record,
)
from .tools.registry import CONF_TOOLS, cached_tool_types

_LOGGER = logging.getLogger(__name__)

OLD_DOMAIN = "mcp_assist"
OLD_SYSTEM_UNIQUE_ID = "mcp_assist_system_settings"
# Port the previous integration used. Imported entries must not keep it.
OLD_DEFAULT_MCP_PORT = 8090

SEARCH_KEYS = (
    CONF_SEARCH_PROVIDER,
    CONF_BRAVE_API_KEY,
    CONF_SEARXNG_URL,
    CONF_ENABLE_CUSTOM_TOOLS,
)
# flat per-profile keys of version 1 (Mem0 era)
LEGACY_MEMORY_KEYS = tuple(LEGACY_KEY_MAP)


def _merged(data: dict, options: dict) -> dict[str, Any]:
    return {**data, **options}


def _has_search_keys(mapping: dict) -> bool:
    return any(key in mapping for key in SEARCH_KEYS)




def search_tools_from_legacy(mapping: dict) -> list[dict[str, Any]]:
    """Turn the old search provider into a search tool plus read_url.

    ``none`` (and a missing provider with custom tools left off) adds nothing.
    ``enable_custom_tools`` without a provider is the old Brave default.
    """
    provider = mapping.get(CONF_SEARCH_PROVIDER)
    if not provider:
        provider = "brave" if mapping.get(CONF_ENABLE_CUSTOM_TOOLS) else "none"
    if provider in (None, "", "none"):
        return []
    if provider == "brave":
        search = {
            "instance_id": "brave",
            "type": "brave",
            "prefix": "",
            "config": {"api_key": mapping.get(CONF_BRAVE_API_KEY) or ""},
        }
    elif provider == "duckduckgo":
        search = {
            "instance_id": "duckduckgo",
            "type": "duckduckgo",
            "prefix": "",
            "config": {},
        }
    elif provider == "searxng":
        search = {
            "instance_id": "searxng",
            "type": "searxng",
            "prefix": "",
            "config": {"url": mapping.get(CONF_SEARXNG_URL) or ""},
        }
    else:
        return []
    read_url = {
        "instance_id": "read_url",
        "type": "read_url",
        "prefix": "",
        "config": {},
    }
    return [search, read_url]


def memory_tool_from_legacy(mapping: dict) -> dict[str, Any]:
    """Turn version-1 flat ``mem0_*`` keys into one memory tool record."""
    config = upgrade_legacy_config({key: mapping[key] for key in LEGACY_MEMORY_KEYS if key in mapping})
    config[CONF_MEMORY_ENABLED] = bool(config.get(CONF_MEMORY_ENABLED, False))
    config.setdefault("memory_api_key", "")
    config[CONF_MEMORY_USER_ID] = config.get(CONF_MEMORY_USER_ID) or DEFAULT_MEMORY_USER_ID
    return {
        "instance_id": MEMORY_TOOL_TYPE,
        "type": MEMORY_TOOL_TYPE,
        "prefix": "",
        "config": config,
    }


def migrate_mapping(
    data: dict | None,
    options: dict | None,
    *,
    convert_search: bool,
) -> tuple[dict, dict]:
    """Return new ``data`` and ``options`` without mutating the inputs."""
    data = copy.deepcopy(dict(data or {}))
    options = copy.deepcopy(dict(options or {}))
    merged = _merged(data, options)
    if CONF_TOOLS in options:
        tools = [upgrade_tool_record(dict(item)) for item in (options.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    elif CONF_TOOLS in data:
        tools = [upgrade_tool_record(dict(item)) for item in (data.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    else:
        tools = []
    existing = {item.get("instance_id") for item in tools}

    if convert_search and _has_search_keys(merged):
        for tool in search_tools_from_legacy(merged):
            if tool["instance_id"] not in existing:
                tools.append(tool)
                existing.add(tool["instance_id"])

    if has_legacy_keys(merged) and MEMORY_TOOL_TYPE not in existing:
        tools.append(memory_tool_from_legacy(merged))

    for key in SEARCH_KEYS + LEGACY_MEMORY_KEYS:
        data.pop(key, None)
        options.pop(key, None)
    data.pop(CONF_TOOLS, None)
    if tools or CONF_TOOLS in options:
        options[CONF_TOOLS] = tools
    return data, options


def shared_type_ids() -> set[str]:
    return {tool_id for tool_id, tool in cached_tool_types().items() if tool.shared}


def _entry_tools(data: dict, options: dict) -> list[dict]:
    if CONF_TOOLS in options:
        return [item for item in (options.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    if CONF_TOOLS in data:
        return [item for item in (data.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    return []


class _Snap:
    def __init__(self, entry) -> None:
        self.unique_id = entry.unique_id
        self.title = entry.title
        self.version = entry.version
        self.data = copy.deepcopy(dict(entry.data))
        self.options = copy.deepcopy(dict(entry.options))


def plan_migrations(entries) -> list[dict[str, Any]]:
    """Plan version-2 payloads for a set of entries. Inputs are not modified."""
    snaps = [_Snap(entry) for entry in entries]
    shared_ids = shared_type_ids()
    system = next((snap for snap in snaps if snap.unique_id == SYSTEM_ENTRY_UNIQUE_ID), None)
    if system is None:
        system = next((snap for snap in snaps if snap.unique_id == OLD_SYSTEM_UNIQUE_ID), None)

    system_merged = _merged(system.data, system.options) if system else {}
    # The system entry owns search when it still has the legacy keys, or when
    # it already has shared tools from an earlier migration pass.
    system_owns_search = bool(system) and (
        _has_search_keys(system_merged)
        or any(
            item.get("type") in shared_ids
            for item in _entry_tools(system.data, system.options)
        )
    )

    search_owner = None
    if system_owns_search and system is not None:
        search_owner = system.unique_id
    else:
        for snap in snaps:
            if snap is system:
                continue
            if _has_search_keys(_merged(snap.data, snap.options)):
                search_owner = snap.unique_id
                break

    plans = []
    for snap in snaps:
        merged = _merged(snap.data, snap.options)
        convert_search = snap.unique_id == search_owner and _has_search_keys(merged)
        data, options = migrate_mapping(
            snap.data, snap.options, convert_search=convert_search
        )
        is_system = snap.unique_id in (SYSTEM_ENTRY_UNIQUE_ID, OLD_SYSTEM_UNIQUE_ID)
        plans.append(
            {
                "unique_id": snap.unique_id,
                "title": snap.title,
                "data": data,
                "options": options,
                "is_system": is_system,
            }
        )
    return plans


SELECTION_MINOR_VERSION = 2
# 3.3: the mem0_memory tool subentry is now ``memory`` with memory_* keys
RENAME_MINOR_VERSION = 3


def _system_subentries(hass: HomeAssistant):
    for candidate in hass.config_entries.async_entries(DOMAIN):
        if candidate.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
            return list(candidate.subentries.values())
    return []


def migrate_disabled_tools(options: dict, subentries) -> tuple[dict, bool]:
    """Turn a ``profile_tools`` allowlist into ``disabled_tools``.

    Returns the new options and whether the conversion is finished. It is not
    finished when an allowlist is present but the system entry has no tool
    subentries yet, so the caller leaves the minor version alone and retries.
    """
    options = dict(options)
    if "profile_tools" not in options:
        return options, True
    allow = options.get("profile_tools")
    if allow is None:
        options.pop("profile_tools", None)
        options.pop("disabled_tools", None)
        return options, True
    if not subentries:
        return options, False
    allow_set = {str(item) for item in allow}
    disabled: list[str] = []
    for subentry in subentries:
        identifiers = {subentry.subentry_id}
        if subentry.unique_id:
            identifiers.add(subentry.unique_id)
        if not identifiers & allow_set:
            disabled.append(subentry.subentry_id)
    options.pop("profile_tools", None)
    if disabled:
        options["disabled_tools"] = disabled
    else:
        options.pop("disabled_tools", None)
    return options, True


def apply_disabled_tool_migration(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bump to minor version 2 once ``profile_tools`` can be converted."""
    if entry.version != 3 or entry.minor_version >= SELECTION_MINOR_VERSION:
        return True
    options, done = migrate_disabled_tools(dict(entry.options), _system_subentries(hass))
    if not done:
        return True
    hass.config_entries.async_update_entry(
        entry, options=options, minor_version=SELECTION_MINOR_VERSION
    )
    return True


def rename_legacy_memory_subentries(hass: HomeAssistant) -> None:
    """Replace ``mem0_memory`` subentries on the system entry with ``memory`` ones.

    Subentry types can't change in place, so each one is re-added under the new type with
    renamed keys and removed. Profiles that disabled the old subentry keep it disabled.
    Idempotent: does nothing once no legacy subentry is left.
    """
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigSubentry

    system = next(
        (item for item in hass.config_entries.async_entries(DOMAIN) if item.unique_id == SYSTEM_ENTRY_UNIQUE_ID),
        None,
    )
    if system is None:
        return
    renamed: dict[str, str] = {}
    for old in list(system.subentries.values()):
        if old.subentry_type != LEGACY_TOOL_TYPE:
            continue
        taken = {sub.unique_id for sub in system.subentries.values() if sub.unique_id}
        unique_id = MEMORY_TOOL_TYPE if old.unique_id in (None, LEGACY_TOOL_TYPE) else old.unique_id
        if unique_id in taken and unique_id != old.unique_id:
            unique_id = f"{MEMORY_TOOL_TYPE}_{old.subentry_id[-6:].lower()}"
        new = ConfigSubentry(
            data=MappingProxyType(upgrade_legacy_config(dict(old.data))),
            subentry_type=MEMORY_TOOL_TYPE,
            title=upgrade_title(old.title),
            unique_id=unique_id,
        )
        hass.config_entries.async_remove_subentry(system, old.subentry_id)
        hass.config_entries.async_add_subentry(system, new)
        renamed[old.subentry_id] = new.subentry_id
        if old.unique_id:
            renamed[old.unique_id] = new.subentry_id
        _LOGGER.info("Renamed tool subentry %s (%s) to %s", old.title, LEGACY_TOOL_TYPE, MEMORY_TOOL_TYPE)
    if not renamed:
        return
    pending = hass.data.setdefault(DOMAIN, {}).setdefault("tools_only_entries", set())
    for profile in hass.config_entries.async_entries(DOMAIN):
        if profile.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
            continue
        raw = [str(item) for item in (profile.options.get("disabled_tools") or [])]
        if not any(item in renamed for item in raw):
            continue
        updated: list[str] = []
        for item in raw:
            item = renamed.get(item, item)
            if item not in updated:
                updated.append(item)
        pending.add(profile.entry_id)
        hass.config_entries.async_update_entry(profile, options={**profile.options, "disabled_tools": updated})


def prune_disabled_tool_ids(hass: HomeAssistant) -> None:
    """Drop deleted subentry ids from every profile's disabled set.

    The profile update is marked tools-only by entry id. A shared flag would
    be consumed by the system entry's listener before the profile listener
    runs, and the profile would reload.
    """
    live = {subentry.subentry_id for subentry in _system_subentries(hass)}
    pending = hass.data.setdefault(DOMAIN, {}).setdefault("tools_only_entries", set())
    for profile in hass.config_entries.async_entries(DOMAIN):
        if profile.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
            continue
        raw = profile.options.get("disabled_tools")
        if not raw:
            continue
        kept = [str(item) for item in raw if str(item) in live]
        if kept == [str(item) for item in raw]:
            continue
        options = dict(profile.options)
        if kept:
            options["disabled_tools"] = kept
        else:
            options.pop("disabled_tools", None)
        pending.add(profile.entry_id)
        hass.config_entries.async_update_entry(profile, options=options)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Upgrade one config entry to version 3 (tools as subentries)."""
    from .tool_flow import attach_subentries, subentries_for_tools
    from .tools.registry import async_ensure_tool_types

    await async_ensure_tool_types(hass)
    if entry.version > 3 or (
        entry.version == 3 and entry.minor_version >= RENAME_MINOR_VERSION
    ):
        return True

    if entry.version < 3:
        data = dict(entry.data)
        options = dict(entry.options)
        if entry.version < 2:
            entries = list(hass.config_entries.async_entries(DOMAIN))
            if all(candidate.entry_id != entry.entry_id for candidate in entries):
                entries.append(entry)
            plans = {plan["unique_id"]: plan for plan in plan_migrations(entries)}
            plan = plans.get(entry.unique_id)
            if plan is None:
                data, options = migrate_mapping(data, options, convert_search=False)
            else:
                data, options = plan["data"], plan["options"]

        tools = [item for item in (options.get(CONF_TOOLS) or data.get(CONF_TOOLS) or []) if isinstance(item, dict)]
        options.pop(CONF_TOOLS, None)
        data.pop(CONF_TOOLS, None)
        is_system = entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID
        if is_system and data.get("mcp_port") == OLD_DEFAULT_MCP_PORT:
            # The previous integration's default. Keeping it would bind the same port.
            data["mcp_port"] = DEFAULT_MCP_PORT
        if is_system:
            for other in hass.config_entries.async_entries(DOMAIN):
                if other.entry_id == entry.entry_id or other.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
                    continue
                tools.extend(
                    upgrade_tool_record(item)
                    for item in (other.options.get(CONF_TOOLS) or other.data.get(CONF_TOOLS) or [])
                    if isinstance(item, dict)
                )
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=3, minor_version=1
        )
        if is_system:
            attach_subentries(hass, entry, subentries_for_tools(tools))
        else:
            system = next(
                (
                    candidate
                    for candidate in hass.config_entries.async_entries(DOMAIN)
                    if candidate.unique_id == SYSTEM_ENTRY_UNIQUE_ID
                ),
                None,
            )
            if system is not None and tools:
                attach_subentries(hass, system, subentries_for_tools(tools))
            _LOGGER.info("Migrated %s to tool subentries (version 3)", entry.title)

    apply_disabled_tool_migration(hass, entry)
    if entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
        for profile in hass.config_entries.async_entries(DOMAIN):
            if profile.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
                continue
            if profile.version == 3 and profile.minor_version < SELECTION_MINOR_VERSION:
                apply_disabled_tool_migration(hass, profile)

    # 3.2 -> 3.3: rename the memory tool. Runs from whichever entry migrates first.
    if entry.version == 3 and entry.minor_version == SELECTION_MINOR_VERSION:
        rename_legacy_memory_subentries(hass)
        hass.config_entries.async_update_entry(entry, minor_version=RENAME_MINOR_VERSION)
        if entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
            # profiles finished above have nothing of their own to rename
            for profile in hass.config_entries.async_entries(DOMAIN):
                if profile.version == 3 and profile.minor_version == SELECTION_MINOR_VERSION:
                    hass.config_entries.async_update_entry(profile, minor_version=RENAME_MINOR_VERSION)
    return True


def _map_unique_id(old: str | None, is_system: bool) -> str:
    if is_system:
        return SYSTEM_ENTRY_UNIQUE_ID
    if old and old.startswith("mcp_assist"):
        return "assist_harness" + old[len("mcp_assist") :]
    suffix = old or "profile"
    return f"assist_harness_imported_{suffix}"


def plan_mcp_assist_import(entries) -> list[dict[str, Any]]:
    """Build Assist Harness entry payloads from mcp_assist entries.

    The returned mappings are deep copies. ``entries`` are not modified.
    """
    snapshots = []
    for entry in entries:
        snapshots.append(
            _ImportView(
                unique_id=entry.unique_id,
                title=entry.title,
                data=copy.deepcopy(dict(entry.data)),
                options=copy.deepcopy(dict(entry.options)),
            )
        )
    plans = plan_migrations(snapshots)
    from .tool_flow import subentries_for_tools

    gathered: list[dict[str, Any]] = []
    imported = []
    has_system = False
    for plan in plans:
        is_system = plan["unique_id"] == OLD_SYSTEM_UNIQUE_ID or plan["is_system"]
        gathered.extend(plan["options"].pop(CONF_TOOLS, []) or [])
        if is_system:
            has_system = True
            port = plan["data"].get("mcp_port")
            if port in (None, OLD_DEFAULT_MCP_PORT):
                plan["data"]["mcp_port"] = DEFAULT_MCP_PORT
        imported.append(
            {
                "unique_id": _map_unique_id(plan["unique_id"], is_system),
                "title": plan["title"] or ("Shared MCP Server Settings" if is_system else "Assist Harness"),
                "data": plan["data"],
                "options": plan["options"],
                "is_system": is_system,
                "subentries": [],
            }
        )
    if not has_system:
        imported.insert(
            0,
            {
                "unique_id": SYSTEM_ENTRY_UNIQUE_ID,
                "title": "Shared MCP Server Settings",
                "data": {"mcp_port": DEFAULT_MCP_PORT},
                "options": {},
                "is_system": True,
                "subentries": [],
            },
        )
    payloads = subentries_for_tools(gathered)
    for plan in imported:
        if plan["is_system"]:
            plan["subentries"] = payloads
    return imported


class _ImportView:
    """Enough of a config entry for ``plan_migrations``."""

    def __init__(self, unique_id, title, data, options) -> None:
        self.unique_id = unique_id
        self.title = title
        self.version = 1
        self.data = data
        self.options = options


def _make_entry(plan: dict[str, Any]) -> ConfigEntry:
    return ConfigEntry(
        version=3,
        minor_version=RENAME_MINOR_VERSION,
        domain=DOMAIN,
        title=plan["title"],
        data=plan["data"],
        options=plan["options"],
        source="import",
        unique_id=plan["unique_id"],
        discovery_keys=MappingProxyType({}),
        state=ConfigEntryState.NOT_LOADED,
        subentries_data=plan.get("subentries") or (),
    )


async def async_import_mcp_assist(hass: HomeAssistant) -> list[ConfigEntry]:
    """Copy every mcp_assist entry into Assist Harness. Originals stay untouched."""
    from .tools.registry import async_ensure_tool_types

    await async_ensure_tool_types(hass)
    old_entries = hass.config_entries.async_entries(OLD_DOMAIN)
    plans = plan_mcp_assist_import(old_entries)
    plans.sort(key=lambda plan: 0 if plan["is_system"] else 1)
    created: list[ConfigEntry] = []
    for plan in plans:
        if hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, plan["unique_id"]):
            _LOGGER.info("Skipping import of %s; it already exists", plan["unique_id"])
            continue
        entry = _make_entry(plan)
        await hass.config_entries.async_add(entry)
        created.append(entry)
    _LOGGER.info("Imported %d entr%s from MCP Assist", len(created), "y" if len(created) == 1 else "ies")
    return created
