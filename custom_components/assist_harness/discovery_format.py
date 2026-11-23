"""Compact formatting helpers for discovery tool results.

Kept free of Home Assistant imports so the helpers can be unit-tested alone.
"""

from __future__ import annotations

from typing import Any, Mapping


def format_aliases_suffix(entity: Mapping[str, Any]) -> str:
    """Return a compact aliases suffix, or an empty string if none are useful.

    Entity aliases are included first, then device aliases. Names that merely
    repeat the entity name or entity ID are omitted.
    """
    raw: list[Any] = []
    aliases = entity.get("aliases")
    if aliases:
        raw.extend(aliases)
    device_aliases = entity.get("device_aliases")
    if device_aliases:
        raw.extend(device_aliases)

    seen: set[str] = set()
    for key in ("name", "entity_id"):
        value = str(entity.get(key) or "").strip()
        if value:
            seen.add(value.casefold())

    unique: list[str] = []
    for alias in raw:
        if not isinstance(alias, str):
            continue
        text = alias.strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        unique.append(text)

    if not unique:
        return ""
    return f" (Aliases: {', '.join(unique)})"
