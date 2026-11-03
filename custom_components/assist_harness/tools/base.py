"""Interface for an optional Assist Harness tool module.

Drop a module in this package that defines ``TOOL``. The registry loads it
automatically. Nothing in the loader, ``const.py``, or the config flow needs
to mention the new tool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

# Keys the options flow renders as password inputs.
Secrets = frozenset


def normalize_prefix(prefix: str | None) -> str:
    """Return a tool-name prefix, adding a separator when one is missing."""
    text = (prefix or "").strip()
    if not text:
        return ""
    if text[-1] not in "_-.:/":
        text += "_"
    return text


def apply_prefix(instance: "ToolInstance", definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy tool definitions with this instance's prefix applied to each name."""
    prefix = instance.prefix or ""
    prefixed: list[dict[str, Any]] = []
    for definition in definitions:
        item = dict(definition)
        item["name"] = f"{prefix}{item.get('name', '')}"
        prefixed.append(item)
    return prefixed


def strip_prefix(instance: "ToolInstance", tool_name: str) -> str:
    """Remove this instance's prefix from an incoming tool name."""
    prefix = instance.prefix or ""
    if prefix and tool_name.startswith(prefix):
        return tool_name[len(prefix) :]
    return tool_name


@dataclass
class ToolInstance:
    """One configured copy of a tool type."""

    instance_id: str
    type_id: str
    prefix: str
    config: dict[str, Any]
    entry_id: str
    shared: bool
    runtime: Any = None
    cached_definitions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable id used by per-profile selection.

        Shared tools are unique on the system entry. Per-profile tools can
        reuse the same instance id on another profile, so the entry id is
        part of the key.
        """
        if self.shared:
            return self.instance_id
        return f"{self.entry_id}:{self.instance_id}"


class Tool:
    """Self-describing optional tool.

    Subclasses (one per module, exposed as ``TOOL``) declare:

    - ``id``, ``name``, ``description``
    - ``config_schema``: a voluptuous schema for the instance config
    - ``secrets``: schema keys rendered as password fields
    - ``shared``: True stores config on the system entry, False per profile
    - ``allow_multiple``: more than one instance of this type may exist
    - ``async_setup`` / ``async_teardown``
    - ``definitions`` and ``async_call``

    A tool that talks to a remote server can also implement
    ``async_definitions``. The registry calls it before ``tools/list`` so a
    server that is down contributes nothing and can reconnect later.
    """

    id: str = ""
    name: str = ""
    description: str = ""
    shared: bool = True
    allow_multiple: bool = True
    secrets: frozenset[str] = frozenset()
    config_schema: vol.Schema = vol.Schema({})

    async def async_setup(self, hass, instance: ToolInstance) -> None:
        """Prepare ``instance.runtime``. Failures are contained by the registry."""

    async def async_teardown(self, hass, instance: ToolInstance) -> None:
        """Release anything ``async_setup`` acquired."""

    def definitions(self, hass, instance: ToolInstance) -> list[dict[str, Any]]:
        """MCP tool definitions this instance currently provides."""
        return []

    async def async_call(
        self,
        hass,
        instance: ToolInstance,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Run a tool call. ``tool_name`` is the unprefixed name."""
        raise ValueError(tool_name)
