"""Options-flow steps for adding, editing, and removing tool instances.

The steps are generic: they read the tool registry, so a new tool module
shows up here without a change to this file.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN, SYSTEM_ENTRY_UNIQUE_ID
from .tools.base import Tool, ToolInstance, normalize_prefix
from .tools.registry import CONF_PROFILE_TOOLS, CONF_TOOLS, discover_tool_types, get_registry

_LOGGER = logging.getLogger(__name__)

_INSTANCE_ID = re.compile(r"^[a-z][a-z0-9_]{0,48}$")


def _marker_default(marker) -> Any:
    raw = getattr(marker, "default", vol.UNDEFINED)
    if raw is vol.UNDEFINED:
        return vol.UNDEFINED
    if callable(raw):
        try:
            return raw()
        except Exception:
            return vol.UNDEFINED
    return raw


def flow_schema(tool: Tool, current: dict[str, Any], *, include_identity: bool, suggested_id: str) -> vol.Schema:
    """Build an options form from a tool's voluptuous schema.

    Keys listed in ``tool.secrets`` are password fields.
    """
    fields: dict[Any, Any] = {}
    if include_identity:
        fields[vol.Required("instance_id", default=suggested_id)] = str
    fields[vol.Optional("prefix", default=current.get("prefix", ""))] = str
    schema = getattr(tool, "config_schema", None)
    mapping = getattr(schema, "schema", None)
    if isinstance(mapping, dict):
        for marker, validator in mapping.items():
            key = marker.schema if isinstance(marker, vol.Marker) else marker
            required = isinstance(marker, vol.Required)
            default = current.get(key, _marker_default(marker))
            if key in tool.secrets:
                validator = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
            if default is vol.UNDEFINED:
                new_marker = vol.Required(key) if required else vol.Optional(key)
            elif required:
                new_marker = vol.Required(key, default=default)
            else:
                new_marker = vol.Optional(key, default=default)
            fields[new_marker] = validator
    return vol.Schema(fields)


def _system_entry(hass):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
            return entry
    return None


def _records(entry) -> list[dict]:
    if entry is None:
        return []
    if CONF_TOOLS in entry.options:
        return [dict(item) for item in (entry.options.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    if CONF_TOOLS in entry.data:
        return [dict(item) for item in (entry.data.get(CONF_TOOLS) or []) if isinstance(item, dict)]
    return []


def _owner_entry(hass, flow_entry, tool: Tool):
    if tool.shared:
        return _system_entry(hass)
    if flow_entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID:
        return None
    return flow_entry


def _next_instance_id(existing: set[str], type_id: str) -> str:
    if type_id not in existing:
        return type_id
    number = 2
    while f"{type_id}_{number}" in existing:
        number += 1
    return f"{type_id}_{number}"


def _preview_names(hass, tool: Tool, prefix: str, config: dict) -> list[str]:
    instance = ToolInstance(
        instance_id="preview",
        type_id=tool.id,
        prefix=normalize_prefix(prefix),
        config=config,
        entry_id="",
        shared=tool.shared,
    )
    try:
        definitions = tool.definitions(hass, instance) or []
    except Exception:
        return []
    return [item.get("name", "") for item in definitions if item.get("name")]


class ToolFlowMixin:
    """Tool menu steps mixed into the options flow."""

    def _tool_types(self) -> dict[str, Tool]:
        return discover_tool_types()

    def _flow_is_system(self) -> bool:
        return self.config_entry.unique_id == SYSTEM_ENTRY_UNIQUE_ID

    def _visible_instances(self) -> list[dict[str, Any]]:
        """Instances the current options flow can show."""
        types = self._tool_types()
        rows: list[dict[str, Any]] = []
        system = _system_entry(self.hass)
        if system is not None:
            for record in _records(system):
                tool = types.get(record.get("type"))
                if tool is None or not tool.shared:
                    continue
                rows.append(self._describe(system, record, tool, shared=True))
        if not self._flow_is_system():
            for record in _records(self.config_entry):
                tool = types.get(record.get("type"))
                if tool is None or tool.shared:
                    continue
                rows.append(self._describe(self.config_entry, record, tool, shared=False))
        return rows

    def _describe(self, entry, record: dict, tool: Tool, *, shared: bool) -> dict[str, Any]:
        instance_id = str(record.get("instance_id") or tool.id)
        prefix = normalize_prefix(record.get("prefix"))
        key = instance_id if shared else f"{entry.entry_id}:{instance_id}"
        label = tool.name
        if prefix:
            label = f"{label} [{prefix}]"
        if shared and not self._flow_is_system():
            label = f"{label} (shared)"
        return {
            "key": key,
            "entry_id": entry.entry_id,
            "instance_id": instance_id,
            "type": tool.id,
            "prefix": prefix,
            "config": dict(record.get("config") or {}),
            "label": label,
            "shared": shared,
        }

    async def _persist_tools(self, entry, records: list[dict]) -> None:
        options = dict(entry.options)
        options[CONF_TOOLS] = records
        kwargs: dict[str, Any] = {"options": options}
        if CONF_TOOLS in entry.data:
            data = dict(entry.data)
            data.pop(CONF_TOOLS, None)
            kwargs["data"] = data
        self.hass.data.setdefault(DOMAIN, {})["tools_only_update"] = True
        self.hass.config_entries.async_update_entry(entry, **kwargs)
        registry = self.hass.data.get(DOMAIN, {}).get("tool_registry")
        if registry is None and self.hass.data.get(DOMAIN, {}).get("shared_mcp_server") is not None:
            registry = get_registry(self.hass)
        if registry is not None:
            await registry.async_reload()

    async def _persist_selection(self, use_all: bool, selected: list[str]) -> None:
        if self._flow_is_system():
            return
        options = dict(self.config_entry.options)
        if use_all:
            options.pop(CONF_PROFILE_TOOLS, None)
        else:
            options[CONF_PROFILE_TOOLS] = list(selected)
        self.hass.data.setdefault(DOMAIN, {})["tools_only_update"] = True
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)

    async def async_step_tools(self, user_input=None):
        """List enabled tools and offer add, edit, or remove."""
        errors: dict[str, str] = {}
        instances = self._visible_instances()
        use_all = CONF_PROFILE_TOOLS not in self.config_entry.options
        selected_default = [row["key"] for row in instances]

        if user_input is not None:
            if not self._flow_is_system():
                await self._persist_selection(
                    bool(user_input.get("use_all", True)),
                    list(user_input.get("profile_tools") or []),
                )
            action = user_input.get("action") or "done"
            if action == "add":
                return await self.async_step_tool_add()
            if action.startswith("edit:"):
                self._tool_edit_key = action.split(":", 1)[1]
                return await self.async_step_tool_config()
            if action.startswith("remove:"):
                self._tool_remove_key = action.split(":", 1)[1]
                return await self.async_step_tool_remove()
            if self._flow_is_system():
                return self.async_abort(reason="tools_updated")
            return await self.async_step_profile_next()

        options = [{"value": "done", "label": "Done"}, {"value": "add", "label": "Add a tool"}]
        for row in instances:
            options.append({"value": f"edit:{row['key']}", "label": f"Edit {row['label']}"})
            options.append({"value": f"remove:{row['key']}", "label": f"Remove {row['label']}"})

        schema: dict[Any, Any] = {
            vol.Required("action", default="done"): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )
        }
        if not self._flow_is_system():
            current = self.config_entry.options.get(CONF_PROFILE_TOOLS)
            schema[vol.Required("use_all", default=use_all)] = bool
            schema[
                vol.Optional(
                    "profile_tools",
                    default=list(current) if current is not None else selected_default,
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": row["key"], "label": row["label"]} for row in instances],
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        description = (
            "Shared tools are stored for every profile. "
            "Removing a tool tears it down immediately."
        )
        if not self._flow_is_system():
            description = (
                "Add profile tools such as Mem0, or choose which enabled tools "
                "this profile's agent can see. Built-in Home Assistant tools stay on. "
                "The default is all enabled tools. Changes apply without a restart."
            )
        return self.async_show_form(
            step_id="tools",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"info": description},
        )

    async def async_step_tool_add(self, user_input=None):
        """Pick a tool type, then fill its schema."""
        types = self._tool_types()
        available = []
        for tool in types.values():
            if tool.shared and _system_entry(self.hass) is None:
                continue
            if not tool.shared and self._flow_is_system():
                continue
            if not tool.allow_multiple:
                owner = _owner_entry(self.hass, self.config_entry, tool)
                if any(record.get("type") == tool.id for record in _records(owner)):
                    continue
            available.append(tool)
        available.sort(key=lambda tool: tool.name.lower())
        if user_input is not None:
            self._tool_add_type = user_input["tool_type"]
            self._tool_edit_key = None
            return await self.async_step_tool_config()
        if not available:
            return self.async_show_form(
                step_id="tool_add",
                data_schema=vol.Schema({}),
                errors={"base": "no_tools"},
                description_placeholders={"info": "Every available tool is already enabled."},
            )
        return self.async_show_form(
            step_id="tool_add",
            data_schema=vol.Schema(
                {
                    vol.Required("tool_type"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {
                                    "value": tool.id,
                                    "label": f"{tool.name} — {tool.description}",
                                }
                                for tool in available
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            description_placeholders={"info": "Choose a tool to add. You can add another instance later when the tool allows it."},
        )

    def _find_row(self, key: str) -> dict[str, Any] | None:
        for row in self._visible_instances():
            if row["key"] == key:
                return row
        return None

    async def async_step_tool_config(self, user_input=None):
        """Create or update one tool instance from its schema."""
        types = self._tool_types()
        editing = self._find_row(getattr(self, "_tool_edit_key", None) or "")
        if editing is not None:
            tool = types[editing["type"]]
            suggested = editing["instance_id"]
            current = {**editing["config"], "prefix": editing["prefix"]}
            include_identity = False
        else:
            tool = types[getattr(self, "_tool_add_type", "")]
            owner = _owner_entry(self.hass, self.config_entry, tool)
            existing_ids = {str(record.get("instance_id")) for record in _records(owner)}
            suggested = _next_instance_id(existing_ids, tool.id)
            current = {}
            include_identity = True

        errors: dict[str, str] = {}
        if user_input is not None:
            instance_id = editing["instance_id"] if editing else str(user_input.get("instance_id") or "").strip().lower()
            if include_identity and not _INSTANCE_ID.match(instance_id):
                errors["instance_id"] = "invalid_instance_id"
            prefix = normalize_prefix(user_input.get("prefix", current.get("prefix", "")))
            raw_config = {
                key: value
                for key, value in user_input.items()
                if key not in ("instance_id", "prefix")
            }
            try:
                config = tool.config_schema(raw_config)
            except vol.Invalid:
                errors["base"] = "invalid_tool_config"
                config = raw_config
            if not errors:
                names = _preview_names(self.hass, tool, prefix, config)
                taken = set()
                for row in self._visible_instances():
                    if editing and row["key"] == editing["key"]:
                        continue
                    taken.update(
                        _preview_names(
                            self.hass,
                            types[row["type"]],
                            row["prefix"],
                            row["config"],
                        )
                    )
                if any(name in taken for name in names):
                    errors["prefix"] = "duplicate_tool_name"
            if not errors:
                owner = _owner_entry(self.hass, self.config_entry, tool)
                records = _records(owner)
                new_record = {
                    "instance_id": instance_id,
                    "type": tool.id,
                    "prefix": prefix,
                    "config": config,
                }
                replaced = False
                updated = []
                for record in records:
                    if str(record.get("instance_id")) == instance_id:
                        updated.append(new_record)
                        replaced = True
                    else:
                        updated.append(record)
                if not replaced:
                    if any(str(record.get("instance_id")) == instance_id for record in records):
                        errors["instance_id"] = "duplicate_instance"
                    else:
                        updated.append(new_record)
                if not errors:
                    await self._persist_tools(owner, updated)
                    return await self.async_step_tools()

        return self.async_show_form(
            step_id="tool_config",
            data_schema=flow_schema(
                tool,
                current,
                include_identity=include_identity,
                suggested_id=suggested,
            ),
            errors=errors,
            description_placeholders={
                "name": tool.name,
                "info": tool.description,
            },
        )

    async def async_step_tool_remove(self, user_input=None):
        """Tear a tool down and drop it from tools/list."""
        row = self._find_row(getattr(self, "_tool_remove_key", "") or "")
        if row is None:
            return await self.async_step_tools()
        if user_input is not None:
            if user_input.get("confirm", True):
                entry = next(
                    (
                        candidate
                        for candidate in self.hass.config_entries.async_entries(DOMAIN)
                        if candidate.entry_id == row["entry_id"]
                    ),
                    None,
                )
                if entry is not None:
                    records = [
                        record
                        for record in _records(entry)
                        if str(record.get("instance_id")) != row["instance_id"]
                    ]
                    await self._persist_tools(entry, records)
                    if not self._flow_is_system() and CONF_PROFILE_TOOLS in self.config_entry.options:
                        selected = [
                            key
                            for key in self.config_entry.options.get(CONF_PROFILE_TOOLS) or []
                            if key != row["key"]
                        ]
                        await self._persist_selection(False, selected)
            return await self.async_step_tools()
        return self.async_show_form(
            step_id="tool_remove",
            data_schema=vol.Schema({vol.Required("confirm", default=True): bool}),
            description_placeholders={"name": row["label"], "info": f"Remove {row['label']}? Its tools disappear immediately."},
        )
