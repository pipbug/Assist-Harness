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
CONF_PROFILE_TOOLS = "profile_tools"
_PACKAGE = "custom_components.assist_harness.tools"
# Modules in this package that are not tool types.
_SKIP = {"base", "registry"}


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
    return found


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


def profile_selection(entry) -> list[str] | None:
    """Instance keys this profile may use, or None for every enabled tool."""
    if entry is None:
        return None
    if CONF_PROFILE_TOOLS in entry.options:
        value = entry.options.get(CONF_PROFILE_TOOLS)
    elif CONF_PROFILE_TOOLS in entry.data:
        value = entry.data.get(CONF_PROFILE_TOOLS)
    else:
        return None
    if value is None:
        return None
    return [str(item) for item in value]


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

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.types: dict[str, Tool] = discover_tool_types()
        self.instances: dict[str, ToolInstance] = {}
        self.generation = 0

    def shared_type_ids(self) -> set[str]:
        return {tool_id for tool_id, tool in self.types.items() if tool.shared}

    def iter_records(self) -> Iterable[tuple[str, bool, dict[str, Any]]]:
        """Yield ``(entry_id, shared, record)`` in tools/list order.

        Shared tools come from the system entry. When the system entry has
        none (a profile was migrated before a system entry existed), the
        first profile that stores shared tools is used instead. Per-profile
        tools follow, in profile order.
        """
        shared_ids = self.shared_type_ids()
        system = _system_entry(self.hass)
        system_shared = [
            record
            for record in tools_of(system)
            if record.get("type") in shared_ids
        ]
        system_declares_tools = system is not None and (
            CONF_TOOLS in system.options or CONF_TOOLS in system.data
        )
        if system_shared and system is not None:
            for record in system_shared:
                yield system.entry_id, True, record
        elif not system_declares_tools:
            # No system tool list yet: a profile may still hold the migrated
            # search tools from before the system entry existed.
            for entry in _profiles(self.hass):
                borrowed = [
                    record
                    for record in tools_of(entry)
                    if record.get("type") in shared_ids
                ]
                if borrowed:
                    for record in borrowed:
                        yield entry.entry_id, True, record
                    break

        for entry in _profiles(self.hass):
            for record in tools_of(entry):
                if record.get("type") in shared_ids:
                    continue
                yield entry.entry_id, False, record

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
        """Tool names a profile's agent may see, or None for all of them."""
        selected = profile_selection(entry)
        if selected is None:
            return None
        chosen = set(selected)
        names: set[str] = set()
        for instance in self.instances.values():
            if instance.key in chosen or instance.instance_id in chosen:
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
    """Return the process-wide registry, creating it on first use."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    registry = domain_data.get("tool_registry")
    if registry is None:
        registry = ToolRegistry(hass)
        domain_data["tool_registry"] = registry
    return registry


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
