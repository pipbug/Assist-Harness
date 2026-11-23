"""Entity aliases in discovery results and name_contains matching."""

from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_FORMAT = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "assist_harness"
    / "discovery_format.py"
)
_SPEC = importlib.util.spec_from_file_location("discovery_format", _FORMAT)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)
format_aliases_suffix = _MOD.format_aliases_suffix

needs_ha = pytest.mark.skipif(
    importlib.util.find_spec("homeassistant") is None,
    reason="homeassistant is not installed",
)


def test_suffix_includes_entity_alias():
    suffix = format_aliases_suffix(
        {
            "entity_id": "light.centauri_carbon_chamber_light",
            "name": "Chamber Light",
            "aliases": ["Chamber Light", "Printer Light"],
        }
    )
    assert suffix == " (Aliases: Printer Light)"


def test_suffix_includes_device_alias_not_already_listed():
    suffix = format_aliases_suffix(
        {
            "entity_id": "light.centauri_carbon_chamber_light",
            "name": "Chamber Light",
            "aliases": ["Printer Light"],
            "device_aliases": ["Centauri", "Printer Light"],
        }
    )
    assert suffix == " (Aliases: Printer Light, Centauri)"


def test_suffix_empty_when_no_useful_aliases():
    assert format_aliases_suffix({"entity_id": "light.x", "name": "Lamp"}) == ""
    assert (
        format_aliases_suffix(
            {"entity_id": "light.x", "name": "Lamp", "aliases": ["Lamp", "light.x"]}
        )
        == ""
    )


def test_suffix_skips_non_strings():
    suffix = format_aliases_suffix(
        {"entity_id": "light.x", "name": "Lamp", "aliases": [None, 1, "Desk lamp"]}
    )
    assert suffix == " (Aliases: Desk lamp)"


@pytest.fixture
def discovery():
    """SmartDiscovery used for matching and details tests."""
    from custom_components.assist_harness.discovery import SmartDiscovery

    return SmartDiscovery(hass=MagicMock())


@needs_ha
def test_name_contains_matches_resolved_aliases(discovery):
    state = SimpleNamespace(
        entity_id="light.centauri_carbon_chamber_light",
        name="Chamber Light",
        attributes={"friendly_name": "Centauri Carbon Chamber Light"},
    )
    entry = SimpleNamespace(aliases=["Printer Light"])
    context = {
        "area": "Bedroom",
        "floor": None,
        "device": "Centauri Carbon",
        "device_name": "Centauri Carbon",
        "device_name_by_user": None,
        "area_aliases": [],
        "floor_aliases": [],
        "device_aliases": [],
        "labels": [],
    }

    with patch(
        "custom_components.assist_harness.discovery.er.async_get_entity_aliases",
        return_value=[
            "Centauri Carbon Chamber Light",
            "Chamber Light",
            "Printer Light",
        ],
    ):
        assert discovery._entity_matches_search_term(
            "printer light", state, entry, context
        )
        assert discovery._entity_matches_search_term("printer", state, entry, context)
        assert not discovery._entity_matches_search_term(
            "kitchen", state, entry, context
        )


@needs_ha
def test_name_contains_matches_raw_registry_aliases(discovery):
    """Fall back to RegistryEntry.aliases when the HA helper is absent."""
    import custom_components.assist_harness.discovery as discovery_mod

    state = SimpleNamespace(
        entity_id="light.centauri_carbon_chamber_light",
        name="Chamber Light",
        attributes={},
    )
    entry = SimpleNamespace(aliases=["Printer light"])
    context = {
        "area": "Bedroom",
        "floor": None,
        "device": None,
        "device_name": None,
        "device_name_by_user": None,
        "area_aliases": [],
        "floor_aliases": [],
        "device_aliases": [],
        "labels": [],
    }

    with patch.object(
        discovery_mod.er, "async_get_entity_aliases", new=None, create=True
    ):
        assert discovery._entity_matches_search_term(
            "printer light", state, entry, context
        )


@needs_ha
def test_create_entity_info_includes_aliases(discovery):
    state = SimpleNamespace(
        entity_id="light.centauri_carbon_chamber_light",
        name="Chamber Light",
        domain="light",
        state="off",
        attributes={"friendly_name": "Centauri Carbon Chamber Light"},
    )
    entry = SimpleNamespace(aliases=["Printer Light"])
    context = {
        "area": "Bedroom",
        "area_id": "bedroom",
        "area_aliases": [],
        "floor": None,
        "floor_id": None,
        "floor_aliases": [],
        "device": "Centauri Carbon",
        "device_name": "Centauri Carbon",
        "device_name_by_user": None,
        "device_aliases": [],
        "labels": [],
        "label_ids": [],
    }

    with (
        patch("custom_components.assist_harness.discovery.er.async_get"),
        patch("custom_components.assist_harness.discovery.dr.async_get"),
        patch("custom_components.assist_harness.discovery.ar.async_get"),
        patch("custom_components.assist_harness.discovery.fr", None),
        patch("custom_components.assist_harness.discovery.lr", None),
        patch.object(
            discovery,
            "_get_entity_aliases",
            return_value=[
                "Centauri Carbon Chamber Light",
                "Chamber Light",
                "Printer Light",
            ],
        ),
    ):
        info = discovery._create_entity_info(
            state, entity_entry=entry, entity_context=context
        )

    assert info["aliases"] == [
        "Centauri Carbon Chamber Light",
        "Chamber Light",
        "Printer Light",
    ]
    assert info["area"] == "Bedroom"


@needs_ha
def test_format_discovery_results_includes_aliases():
    from custom_components.assist_harness.mcp_server import MCPServer

    entities = [
        {
            "entity_id": "light.centauri_carbon_chamber_light",
            "name": "Chamber Light",
            "state": "off",
            "area": "Bedroom",
            "aliases": ["Printer Light"],
        }
    ]
    result = MCPServer._format_discovery_results(object(), entities, {})
    text = result["content"][0]["text"]
    assert "Aliases: Printer Light" in text
    assert "Chamber Light" in text


@needs_ha
def test_format_area_discovery_includes_name_and_aliases():
    from custom_components.assist_harness.mcp_server import MCPServer

    entities = [
        {
            "entity_id": "_summary",
            "query_type": "area",
            "query": "Bedroom area",
            "total_found": 1,
        },
        {
            "entity_id": "light.centauri_carbon_chamber_light",
            "name": "Chamber Light",
            "state": "off",
            "area": "Bedroom",
            "aliases": ["Printer Light"],
            "relationship": "Light",
        },
    ]
    result = MCPServer._format_discovery_results(object(), entities, {})
    text = result["content"][0]["text"]
    assert "Chamber Light" in text
    assert "Aliases: Printer Light" in text


@needs_ha
@pytest.mark.asyncio
async def test_get_entity_details_includes_aliases(discovery):
    import custom_components.assist_harness.discovery as discovery_mod

    state = SimpleNamespace(
        entity_id="light.centauri_carbon_chamber_light",
        name="Chamber Light",
        domain="light",
        state="off",
        attributes={"friendly_name": "Centauri Carbon Chamber Light"},
        last_changed=datetime(2026, 11, 23),
        last_updated=datetime(2026, 11, 23),
    )
    entry = SimpleNamespace(
        unique_id="printer-light",
        entity_category=None,
        disabled_by=None,
        aliases=["Printer Light"],
    )
    context = {
        "area": "Bedroom",
        "area_id": "bedroom",
        "area_aliases": [],
        "floor": None,
        "floor_id": None,
        "floor_aliases": [],
        "device": "Centauri Carbon",
        "device_name": "Centauri Carbon",
        "device_name_by_user": None,
        "device_aliases": [],
        "labels": [],
        "label_ids": [],
        "label_sources": {},
    }

    entity_registry = MagicMock()
    entity_registry.async_get.return_value = entry
    discovery.hass.states.get.return_value = state

    with (
        patch.object(discovery_mod, "async_should_expose", return_value=True),
        patch(
            "custom_components.assist_harness.discovery.er.async_get",
            return_value=entity_registry,
        ),
        patch(
            "custom_components.assist_harness.discovery.ar.async_get",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.assist_harness.discovery.dr.async_get",
            return_value=MagicMock(),
        ),
        patch.object(discovery_mod, "fr", None),
        patch.object(discovery_mod, "lr", None),
        patch.object(discovery, "_get_entity_context", return_value=context),
        patch.object(discovery, "_get_entity_aliases", return_value=["Printer Light"]),
    ):
        details = await discovery.get_entity_details(
            ["light.centauri_carbon_chamber_light"]
        )

    info = details["light.centauri_carbon_chamber_light"]
    assert info["aliases"] == ["Printer Light"]
    assert info["name"] == "Chamber Light"
    assert "error" not in info
