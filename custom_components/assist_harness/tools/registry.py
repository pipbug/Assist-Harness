"""Discover tool modules and run the configured instances."""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Iterable

from homeassistant.core import HomeAssistant

from ..const import DOMAIN, SYSTEM_ENTRY_UNIQUE_ID
from .base import Tool, ToolInstance, normalize_prefix

_LOGGER = logging.getLogger(__name__)

CONF_TOOLS = "tools"
CONF_PROFILE_TOOLS = "profile_tools"  # v3.1 allowlist, removed by the minor migration
CONF_DISABLED_TOOLS = "disabled_tools"
_PACKAGE = "custom_components.assist_harness.tools"
# Modules in this package that are not tool types.
_SKIP = {"base", "registry"}
_DISCOVERED: dict[str, Tool] | None = None


def discover_tool_types(package: str = _PACKAGE) -> dict[str, Tool]:
    """Import every tool module in ``package`` and return ``id -> TOOL``.

    A module is a tool when it defines ``TOOL``. Adding a tool is dropping
    such a file into the package. Modules whose names start with ``_`` are
    ignored, as are the registry helpers.
    """
    module = importlib.import_module(package)
    found: dict[str, Tool] = {}
    for info in pkgutil.iter_modules(module.__path__):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        imported = importlib.import_module(f"{package}.{info.name}")
        tool = getattr(imported, "TOOL", None)
        if tool is None:
            continue
        if not getattr(tool, "id", None):
            _LOGGER.warning("Tool module %s has TOOL without an id", info.name)
            continue
        found[tool.id] = tool
    if package == _PACKAGE:
        global _DISCOVERED
        _DISCOVERED = found
    return found


def cached_tool_types() -> dict[str, Tool]:
    """Return tool types discovered earlier, discovering now if nothing is cached.

    Callers on the event loop should use ``async_ensure_tool_types`` so the
    import work runs in an executor.
    """
    if _DISCOVERED is not None:
        return _DISCOVERED
    return discover_tool_types()


async def async_ensure_tool_types(hass: HomeAssistant) -> dict[str, Tool]:
    """Discover built-in tool modules once, off the event loop."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    cached = domain_data.get("tool_types")
    if cached:
        return cached
    if _DISCOVERED is not None:
        domain_data["tool_types"] = _DISCOVERED
        return _DISCOVERED
    found = await hass.async_add_executor_job(discover_tool_types)
    domain_data["tool_types"] = found
    return found


def subentry_records(entry) -> list[dict[str, Any]]:
    """Tool records stored as config subentries."""
    if entry is None or not getattr(entry, "subentries", None):
        return []
    records: list[dict[str, Any]] = []
    for subentry in entry.subentries.values():
        data = dict(subentry.data)
        prefix = data.pop("prefix", "") or ""
        records.append(
            {
                "instance_id": subentry.unique_id or subentry.subentry_id,
                "type": subentry.subentry_type,
                "prefix": prefix,
                "config": data,
                "title": subentry.title,
                "subentry_id": subentry.subentry_id,
            }
        )
    return records


def tools_of(entry) -> list[dict[str, Any]]:
    """Return the tool records stored on a config entry.

    Options win when the key is present, including an explicit empty list.
    """
    if entry is None:
        return []
    if CONF_TOOLS in entry.options:
        return [dict(item) for item in (entry.options.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    if CONF_TOOLS in entry.data:
        return [dict(item) for item in (entry.data.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    return []


def disabled_tool_ids(entry) -> set[str]:
    """Subentry ids this profile has switched off. Missing means none."""
    if entry is None:
        return set()
    if CONF_DISABLED_TOOLS in entry.options:
        value = entry.options.get(CONF_DISABLED_TOOLS)
    elif CONF_DISABLED_TOOLS in entry.data:
        value = entry.data.get(CONF_DISABLED_TOOLS)
    else:
        return set()
    if not value:
        return set()
    return {str(item) for item in value}


def _system_entry(hass: HomeAssistant):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
            return entry
    return None


def _profiles(hass: HomeAssistant) -> list:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.unique_id != SYSTEM_ENTRY_UNIQUE_ID
    ]


class ToolRegistry:
    """Live set of tool instances behind the shared MCP server."""

    def __init__(self, hass: HomeAssistant, types: dict[str, Tool] | None = None) -> None:
        self.hass = hass
        self.types: dict[str, Tool] = types if types is not None else cached_tool_types()
        self.instances: dict[str, ToolInstance] = {}
        self.generation = 0

    def shared_type_ids(self) -> set[str]:
        return {tool_id for tool_id, tool in self.types.items() if tool.shared}

    def iter_records(self) -> Iterable[tuple[str, bool, dict[str, Any]]]:
        """Yield ``(entry_id, shared, record)`` in tools/list order.

        Tool instances are subentries of the system entry. Options lists are
        only a fallback for an entry that has not been migrated yet.
        """
        system = _system_entry(self.hass)
        records = subentry_records(system)
        owner = system.entry_id if system is not None else ""
        if not records and system is not None:
            records = tools_of(system)
        if not records:
            for entry in _profiles(self.hass):
                borrowed = subentry_records(entry) or tools_of(entry)
                if borrowed:
                    records = borrowed
                    owner = entry.entry_id
                    break
        for record in records:
            tool = self.types.get(str(record.get("type")))
            shared = True if tool is None else bool(tool.shared)
            yield owner, shared, record

    async def async_reload(self) -> None:
        """Match running instances to config. Removed tools are torn down."""
        before = self.definitions()
        desired: dict[str, ToolInstance] = {}
        order: list[str] = []
        for entry_id, shared, record in self.iter_records():
            parsed = _parse_record(record)
            if parsed is None:
                continue
            instance_id, type_id, prefix, config = parsed
            tool = self.types.get(type_id)
            if tool is None:
                _LOGGER.warning("Unknown tool type %s (instance %s)", type_id, instance_id)
                continue
            instance = ToolInstance(
                instance_id=instance_id,
                type_id=type_id,
                prefix=prefix,
                config=config,
                entry_id=entry_id or "",
                shared=shared and tool.shared,
                subentry_id=record.get("subentry_id"),
            )
            if instance.key in desired:
                _LOGGER.warning("Duplicate tool instance %s ignored", instance.key)
                continue
            desired[instance.key] = instance
            order.append(instance.key)

        structural = False
        for key in list(self.instances):
            if key not in desired:
                await self._teardown(self.instances.pop(key))
                structural = True

        rebuilt: dict[str, ToolInstance] = {}
        for key in order:
            desired_instance = desired[key]
            current = self.instances.get(key)
            if current is not None and _same_config(current, desired_instance):
                current.entry_id = desired_instance.entry_id
                current.shared = desired_instance.shared
                current.subentry_id = desired_instance.subentry_id
                rebuilt[key] = current
                continue
            if current is not None:
                await self._teardown(current)
            await self._setup(desired_instance)
            rebuilt[key] = desired_instance
            structural = True

        self.instances = rebuilt
        await self._refresh_cached()
        if structural or self.definitions() != before:
            self.generation += 1
            await self._notify_list_changed()

    async def async_refresh(self) -> None:
        """Refresh dynamic definitions (external servers) without tearing down."""
        before = self.definitions()
        await self._refresh_cached()
        if self.definitions() != before:
            self.generation += 1
            await self._notify_list_changed()

    async def async_teardown_all(self) -> None:
        """Tear every instance down. Used when the MCP server stops."""
        for instance in list(self.instances.values()):
            await self._teardown(instance)
        self.instances.clear()

    def definitions(self) -> list[dict[str, Any]]:
        """Current MCP tool definitions. The first owner of a name wins."""
        listed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for instance in self.instances.values():
            for definition in instance.cached_definitions:
                name = definition.get("name")
                if not name or name in seen:
                    if name in seen:
                        _LOGGER.debug("Skipping duplicate tool name %s", name)
                    continue
                seen.add(name)
                listed.append(definition)
        return listed

    def optional_names(self) -> set[str]:
        return {definition["name"] for definition in self.definitions() if definition.get("name")}

    def allowed_names(self, entry) -> set[str] | None:
        """Tool names a profile's agent may see, or None when none are switched off.

        ``disabled_tools`` lists subentry ids. An id that does not match a
        current tool is ignored, so a deleted tool cannot hide a new one.
        """
        disabled = disabled_tool_ids(entry)
        if not disabled:
            return None
        names: set[str] = set()
        for instance in self.instances.values():
            if instance.subentry_id and instance.subentry_id in disabled:
                continue
            if instance.instance_id in disabled:
                continue
            for definition in instance.cached_definitions:
                if definition.get("name"):
                    names.add(definition["name"])
        return names

    def handles(self, tool_name: str) -> bool:
        return self.instance_for(tool_name) is not None

    def instance_for(self, tool_name: str) -> ToolInstance | None:
        for instance in self.instances.values():
            for definition in instance.cached_definitions:
                if definition.get("name") == tool_name:
                    return instance
        return None

    async def async_call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        instance = self.instance_for(tool_name)
        if instance is None:
            raise ValueError(f"Unknown tool: {tool_name}")
        tool = self.types.get(instance.type_id)
        if tool is None:
            raise ValueError(f"Unknown tool type: {instance.type_id}")
        from .base import strip_prefix

        try:
            return await tool.async_call(
                self.hass, instance, strip_prefix(instance, tool_name), arguments or {}
            )
        except Exception as err:
            _LOGGER.error("Tool %s failed: %s", tool_name, err)
            return {
                "content": [{"type": "text", "text": f"❌ {instance.type_id} error: {err}"}],
                "isError": True,
            }

    async def _setup(self, instance: ToolInstance) -> None:
        tool = self.types[instance.type_id]
        try:
            await tool.async_setup(self.hass, instance)
        except Exception as err:
            _LOGGER.error(
                "Failed to set up tool %s (%s): %s",
                instance.instance_id,
                instance.type_id,
                err,
            )
            instance.runtime = None

    async def _teardown(self, instance: ToolInstance) -> None:
        tool = self.types.get(instance.type_id)
        if tool is None:
            return
        try:
            await tool.async_teardown(self.hass, instance)
        except Exception as err:
            _LOGGER.warning(
                "Failed to tear down tool %s: %s", instance.instance_id, err
            )
        instance.runtime = None
        instance.cached_definitions = []

    async def _refresh_cached(self) -> None:
        for instance in self.instances.values():
            tool = self.types.get(instance.type_id)
            if tool is None:
                instance.cached_definitions = []
                continue
            try:
                refresher = getattr(tool, "async_definitions", None)
                if refresher is not None:
                    definitions = await refresher(self.hass, instance)
                else:
                    definitions = tool.definitions(self.hass, instance)
                instance.cached_definitions = list(definitions or [])
            except Exception as err:
                _LOGGER.error(
                    "Tool %s did not return definitions: %s", instance.instance_id, err
                )
                instance.cached_definitions = []

    async def _notify_list_changed(self) -> None:
        server = self.hass.data.get(DOMAIN, {}).get("shared_mcp_server")
        if server is None:
            return
        try:
            await server.broadcast_notification("notifications/tools/list_changed")
        except Exception as err:
            _LOGGER.debug("Could not notify tools/list_changed: %s", err)


def get_registry(hass: HomeAssistant) -> ToolRegistry:
    """Return the process-wide registry, creating it from the cached types."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    registry = domain_data.get("tool_registry")
    if registry is None:
        registry = ToolRegistry(hass, domain_data.get("tool_types"))
        domain_data["tool_registry"] = registry
    return registry


async def async_get_registry(hass: HomeAssistant) -> ToolRegistry:
    """Discover tool modules in an executor, then return the registry."""
    await async_ensure_tool_types(hass)
    return get_registry(hass)


def filter_tools_for_entry(hass: HomeAssistant, entry, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop optional tools this profile did not select. Built-in tools stay."""
    domain_data = hass.data.get(DOMAIN, {})
    registry: ToolRegistry | None = domain_data.get("tool_registry")
    if registry is None or not tools:
        return tools
    allowed = registry.allowed_names(entry)
    if allowed is None:
        return tools
    optional = registry.optional_names()
    kept: list[dict[str, Any]] = []
    for tool in tools:
        name = _tool_name(tool)
        if name in optional and name not in allowed:
            continue
        kept.append(tool)
    return kept


def registry_generation(hass: HomeAssistant) -> int:
    registry = hass.data.get(DOMAIN, {}).get("tool_registry")
    if registry is None:
        return 0
    return registry.generation


def _tool_name(tool: dict[str, Any]) -> str:
    if "function" in tool and isinstance(tool["function"], dict):
        return str(tool["function"].get("name") or "")
    return str(tool.get("name") or "")


def _parse_record(record: dict[str, Any]):
    type_id = record.get("type")
    instance_id = record.get("instance_id") or type_id
    if not type_id or not instance_id:
        return None
    config = record.get("config") or {}
    if not isinstance(config, dict):
        config = {}
    return str(instance_id), str(type_id), normalize_prefix(record.get("prefix")), dict(config)


def _same_config(current: ToolInstance, desired: ToolInstance) -> bool:
    return (
        current.type_id == desired.type_id
        and current.prefix == desired.prefix
        and current.config == desired.config
    )
