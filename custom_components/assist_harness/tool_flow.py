"""Config subentry flow for optional tools.

Each tool type is a subentry type of the system entry. Home Assistant draws
the Add, Reconfigure, and Delete actions. This module only builds the form
from the tool's voluptuous schema.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN, SYSTEM_ENTRY_UNIQUE_ID
from .tools.base import Tool, normalize_prefix
from .tools.registry import cached_tool_types

_LOGGER = logging.getLogger(__name__)

_INSTANCE_ID = re.compile(r"^[a-z][a-z0-9_]{0,48}$")


def _marker_default(marker: Any) -> Any:
    raw = getattr(marker, "default", vol.UNDEFINED)
    if raw is vol.UNDEFINED:
        return vol.UNDEFINED
    if callable(raw):
        try:
            return raw()
        except Exception:
            return vol.UNDEFINED
    return raw


def _marker_key(marker: Any) -> str:
    return marker.schema if isinstance(marker, vol.Marker) else marker


def _is_secret(tool: Tool, key: str) -> bool:
    return key in tool.secrets


def _selector_for(tool: Tool, key: str, validator: Any) -> Any:
    """Pick a Home Assistant selector that matches the profile forms."""
    if _is_secret(tool, key):
        return TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    if key == "url" or str(key).endswith("_url"):
        return TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
    if validator is bool:
        return BooleanSelector()
    if isinstance(validator, vol.In):
        container = getattr(validator, "container", ())
        options = [str(item) for item in container]
        return SelectSelector(
            SelectSelectorConfig(
                options=options,
                translation_key=f"tool_{key}",
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    if validator in (int, float):
        return NumberSelector(
            NumberSelectorConfig(mode=NumberSelectorMode.BOX)
        )
    return TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))


def tool_config_schema(tool: Tool, current: dict[str, Any] | None = None) -> vol.Schema:
    """Form schema for one tool: name, prefix, then the tool's own fields."""
    current = current or {}
    fields: dict[Any, Any] = {
        vol.Required("name", default=current.get("name") or tool.name): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Optional("prefix", default=current.get("prefix") or ""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
    }
    mapping = getattr(tool.config_schema, "schema", None)
    if isinstance(mapping, dict):
        for marker, validator in mapping.items():
            key = _marker_key(marker)
            required = isinstance(marker, vol.Required)
            default = current.get(key, _marker_default(marker))
            selector = _selector_for(tool, str(key), validator)
            if default is vol.UNDEFINED:
                new_marker = vol.Required(key) if required else vol.Optional(key)
            elif required:
                new_marker = vol.Required(key, default=default)
            else:
                new_marker = vol.Optional(key, default=default)
            fields[new_marker] = selector
    return vol.Schema(fields)


def _split_input(user_input: dict[str, Any], tool: Tool) -> tuple[str, str, dict[str, Any]]:
    name = str(user_input.get("name") or tool.name).strip() or tool.name
    prefix = normalize_prefix(user_input.get("prefix"))
    config = {
        key: value
        for key, value in user_input.items()
        if key not in ("name", "prefix")
    }
    try:
        config = dict(tool.config_schema(config))
    except vol.Invalid:
        config = dict(config)
    return name, prefix, config


def _next_instance_id(existing: set[str], type_id: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    base = slug if slug and _INSTANCE_ID.match(slug) else type_id
    if base not in existing:
        return base
    number = 2
    while f"{base}_{number}" in existing:
        number += 1
    return f"{base}_{number}"


def _taken_names(hass, *, skip_unique_id: str | None) -> set[str]:
    """Tool names already published by other instances."""
    from .tools.base import ToolInstance

    system = _system_entry(hass)
    taken: set[str] = set()
    if system is None:
        return taken
    types = cached_tool_types()
    for subentry in system.subentries.values():
        if skip_unique_id and subentry.unique_id == skip_unique_id:
            continue
        tool = types.get(subentry.subentry_type)
        if tool is None:
            continue
        data = dict(subentry.data)
        prefix = normalize_prefix(data.pop("prefix", ""))
        instance = ToolInstance(
            instance_id=subentry.unique_id or subentry.subentry_id,
            type_id=tool.id,
            prefix=prefix,
            config=data,
            entry_id=system.entry_id,
            shared=tool.shared,
        )
        try:
            definitions = tool.definitions(hass, instance) or []
        except Exception:
            definitions = []
        for definition in definitions:
            if definition.get("name"):
                taken.add(definition["name"])
    return taken


def _preview_names(hass, tool: Tool, prefix: str, config: dict[str, Any]) -> list[str]:
    from .tools.base import ToolInstance

    instance = ToolInstance(
        instance_id="preview",
        type_id=tool.id,
        prefix=prefix,
        config=config,
        entry_id="",
        shared=tool.shared,
    )
    try:
        definitions = tool.definitions(hass, instance) or []
    except Exception:
        return []
    return [item["name"] for item in definitions if item.get("name")]


def _system_entry(hass) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
            return entry
    return None


class ToolSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one tool instance."""

    def _tool(self) -> Tool:
        tool = cached_tool_types().get(self._subentry_type)
        if tool is None:
            raise KeyError(self._subentry_type)
        return tool

    def _suggested(self, tool: Tool, source: dict[str, Any]) -> dict[str, Any]:
        suggested = {"name": source.get("name") or tool.name, "prefix": source.get("prefix") or ""}
        mapping = getattr(tool.config_schema, "schema", None)
        if isinstance(mapping, dict):
            for marker, _validator in mapping.items():
                key = _marker_key(marker)
                if key in source:
                    suggested[str(key)] = source[key]
                else:
                    default = _marker_default(marker)
                    if default is not vol.UNDEFINED:
                        suggested[str(key)] = default
        return suggested

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a tool instance."""
        tool = self._tool()
        entry = self._get_entry()
        if not tool.allow_multiple and entry.get_subentries_of_type(tool.id):
            return self.async_abort(reason="already_configured")
        errors: dict[str, str] = {}
        if user_input is not None:
            name, prefix, config = _split_input(user_input, tool)
            existing = {
                sub.unique_id
                for sub in entry.subentries.values()
                if sub.unique_id
            }
            instance_id = _next_instance_id(existing, tool.id, name)
            names = _preview_names(self.hass, tool, prefix, config)
            if any(item in _taken_names(self.hass, skip_unique_id=None) for item in names):
                errors["prefix"] = "duplicate_tool_name"
            if not errors:
                return self.async_create_entry(
                    title=name,
                    data={"prefix": prefix, **config},
                    unique_id=instance_id,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                tool_config_schema(tool),
                self._suggested(tool, {}),
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a tool instance without reloading Home Assistant."""
        tool = self._tool()
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}
        if user_input is not None:
            name, prefix, config = _split_input(user_input, tool)
            names = _preview_names(self.hass, tool, prefix, config)
            taken = _taken_names(self.hass, skip_unique_id=subentry.unique_id)
            if any(item in taken for item in names):
                errors["prefix"] = "duplicate_tool_name"
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    title=name,
                    data={"prefix": prefix, **config},
                )
        current = {"name": subentry.title, **dict(subentry.data)}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                tool_config_schema(tool, current),
                self._suggested(tool, current),
            ),
            errors=errors,
        )


@callback
def async_get_supported_subentry_types(
    config_entry: ConfigEntry,
) -> dict[str, type[ConfigSubentryFlow]]:
    """One subentry flow per tool type, only on the system entry."""
    if config_entry.unique_id != SYSTEM_ENTRY_UNIQUE_ID:
        return {}
    return {tool_id: ToolSubentryFlow for tool_id in cached_tool_types()}


def subentries_for_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert stored tool records into config-entry subentry payloads."""
    types = cached_tool_types()
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("type"):
            continue
        instance_id = str(tool.get("instance_id") or tool["type"])
        if instance_id in seen:
            continue
        seen.add(instance_id)
        type_id = str(tool["type"])
        info = types.get(type_id)
        display = info.name if info is not None else type_id
        prefix = tool.get("prefix") or ""
        title = tool.get("title") or (f"{display} ({prefix})" if prefix else display)
        data = {"prefix": prefix, **dict(tool.get("config") or {})}
        payloads.append(
            {
                "data": data,
                "subentry_type": type_id,
                "title": title,
                "unique_id": instance_id,
            }
        )
    return payloads


def attach_subentries(hass, entry: ConfigEntry, payloads: list[dict[str, Any]]) -> None:
    """Add subentries that are not already present. Does not reload the entry."""
    from types import MappingProxyType

    existing = {sub.unique_id for sub in entry.subentries.values() if sub.unique_id}
    for payload in payloads:
        if payload.get("unique_id") in existing:
            continue
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(dict(payload["data"])),
                subentry_type=payload["subentry_type"],
                title=payload["title"],
                unique_id=payload.get("unique_id"),
            ),
        )
        existing.add(payload.get("unique_id"))
