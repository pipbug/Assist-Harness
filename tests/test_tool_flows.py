"""Options, subentry, migration, and translation coverage."""

from __future__ import annotations

import json
from pathlib import Path

from homeassistant.helpers.selector import TextSelector, TextSelectorType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.assist_harness.const import DOMAIN, SYSTEM_ENTRY_UNIQUE_ID
from custom_components.assist_harness.migration import async_migrate_entry
from custom_components.assist_harness.tools.registry import async_ensure_tool_types, async_get_registry

ROOT = Path(__file__).resolve().parents[1]
STRINGS = json.loads((ROOT / "custom_components/assist_harness/strings.json").read_text())


def _lookup(tree, dotted: str):
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _system(hass, **kwargs):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        title="Shared MCP Server Settings",
        data={"mcp_port": 8091, "allowed_ips": "", "enable_gap_filling": True},
        version=3,
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


def _profile(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="assist_harness_ollama_kira",
        title="Ollama - Kira",
        data={
            "profile_name": "Kira",
            "server_type": "ollama",
            "model_name": "llama3",
            "system_prompt": "be kind",
            "technical_prompt": "use tools",
        },
        options={},
        version=3,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def _flow_deps(monkeypatch):
    async def _skip(*_args, **_kwargs):
        return None

    monkeypatch.setattr("homeassistant.config_entries.async_process_deps_reqs", _skip)


def test_translation_files_cover_strings_json():
    """Every strings.json key exists in every translation, so nothing renders raw."""
    for path in (ROOT / "custom_components/assist_harness/translations").glob("*.json"):
        data = json.loads(path.read_text())

        def walk(source, translated, prefix=""):
            assert isinstance(translated, dict), prefix or path.name
            for key, value in source.items():
                assert key in translated, f"{path.name} missing {prefix}{key}"
                if isinstance(value, dict):
                    walk(value, translated[key], f"{prefix}{key}.")

        walk(STRINGS, data)


@pytest.mark.usefixtures("_flow_deps")
async def test_subentry_flows_add_reconfigure_and_delete(hass):
    await async_ensure_tool_types(hass)
    entry = _system(hass)
    registry = await async_get_registry(hass)

    payloads = {
        "brave": {"name": "Brave Search", "prefix": "", "api_key": "abc"},
        "duckduckgo": {"name": "DuckDuckGo", "prefix": "ddg"},
        "searxng": {"name": "Home search", "prefix": "home", "url": "http://192.168.1.10:8888"},
        "read_url": {"name": "Read URL", "prefix": ""},
        "mem0_memory": {
            "name": "Mem0 memory",
            "prefix": "",
            "mem0_enabled": True,
            "mem0_url": "https://api.mem0.ai",
            "mem0_api_key": "tok",
            "mem0_user_id": "kira",
        },
        "external_mcp": {
            "name": "Demo server",
            "prefix": "demo",
            "url": "http://127.0.0.1:9/mcp",
            "transport": "streamable_http",
            "bearer_token": "secret",
            "headers": "",
        },
    }

    for tool_id, payload in payloads.items():
        result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, tool_id),
            context={"source": "user"},
        )
        assert result["type"] == "form", tool_id
        assert result["step_id"] == "user"
        block = STRINGS["config_subentries"][tool_id]["step"]["user"]
        for marker in result["data_schema"].schema:
            key = marker.schema
            assert key in block["data"], f"{tool_id}.{key} has no label"
            assert key in block["data_description"], f"{tool_id}.{key} has no description"
            if key in ("api_key", "mem0_api_key", "bearer_token"):
                assert isinstance(result["data_schema"].schema[marker], TextSelector)
                assert result["data_schema"].schema[marker].config["type"] == TextSelectorType.PASSWORD
        created = await hass.config_entries.subentries.async_configure(result["flow_id"], payload)
        assert created["type"] == "create_entry", tool_id
        assert created["title"] == payload["name"]

    by_type = {sub.subentry_type: sub for sub in entry.subentries.values()}
    assert set(by_type) == set(payloads)
    assert by_type["mem0_memory"].data["mem0_url"] == "https://api.mem0.ai"
    assert by_type["mem0_memory"].data["mem0_user_id"] == "kira"
    assert by_type["brave"].data["api_key"] == "abc"
    assert by_type["searxng"].data["prefix"] == "home_"
    # Drop the external server before the registry connects to it.
    assert hass.config_entries.async_remove_subentry(entry, by_type["external_mcp"].subentry_id)

    await registry.async_reload()
    names = {item["name"] for item in registry.definitions()}
    assert "search" in names
    assert "ddg_search" in names
    assert "home_search" in names
    assert "read_url" in names
    assert "recall_memory" in names
    assert "save_memory" in names

    brave = by_type["brave"]
    reconfigure = await entry.start_subentry_reconfigure_flow(hass, brave.subentry_id)
    assert reconfigure["step_id"] == "reconfigure"
    updated = await hass.config_entries.subentries.async_configure(
        reconfigure["flow_id"],
        {"name": "Brave Search", "prefix": "", "api_key": "rotated"},
    )
    assert updated["type"] == "abort"
    assert updated["reason"] == "reconfigure_successful"
    assert entry.subentries[brave.subentry_id].data["api_key"] == "rotated"

    assert hass.config_entries.async_remove_subentry(entry, brave.subentry_id)
    await registry.async_reload()
    names = {item["name"] for item in registry.definitions()}
    assert "home_search" in names
    assert "ddg_search" in names
    assert "read_url" in names
    # The unprefixed Brave search tool is gone.
    assert "search" not in names


@pytest.mark.usefixtures("_flow_deps")
async def test_system_options_and_profile_tool_selection(hass, monkeypatch):
    async def _no_models(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "custom_components.assist_harness.config_flow.fetch_models_from_lmstudio",
        _no_models,
    )
    await async_ensure_tool_types(hass)
    system = _system(hass)
    created = await hass.config_entries.subentries.async_init(
        (system.entry_id, "read_url"),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(
        created["flow_id"],
        {"name": "Read URL", "prefix": ""},
    )
    profile = _profile(hass)

    server = await hass.config_entries.options.async_init(system.entry_id)
    assert server["type"] == "form"
    assert server["step_id"] == "mcp_server"
    assert "menu_options" not in server or server.get("step_id") != "system_menu"
    saved = await hass.config_entries.options.async_configure(
        server["flow_id"],
        {
            "mcp_port": 8091,
            "allowed_ips": "",
            "enable_gap_filling": True,
            "max_entities_per_discovery": 50,
        },
    )
    assert saved["type"] == "create_entry"
    assert system.data["mcp_port"] == 8091

    menu = await hass.config_entries.options.async_init(profile.entry_id)
    assert menu["type"] == "menu"
    assert menu["step_id"] == "init"
    assert menu["menu_options"] == ["profile", "profile_tools"]
    _lookup(STRINGS, "options.step.init.menu_options.profile")
    _lookup(STRINGS, "options.step.init.menu_options.profile_tools")

    tools_step = await hass.config_entries.options.async_configure(
        menu["flow_id"],
        {"next_step_id": "profile_tools"},
    )
    assert tools_step["type"] == "form"
    assert tools_step["step_id"] == "profile_tools"
    chosen = await hass.config_entries.options.async_configure(
        tools_step["flow_id"],
        {"use_all": False, "profile_tools": ["read_url"]},
    )
    assert chosen["type"] == "create_entry"
    assert profile.options["profile_tools"] == ["read_url"]

    menu_again = await hass.config_entries.options.async_init(profile.entry_id)
    profile_form = await hass.config_entries.options.async_configure(
        menu_again["flow_id"],
        {"next_step_id": "profile"},
    )
    assert profile_form["type"] == "form"
    assert profile_form["step_id"] == "profile"
    labels = STRINGS["options"]["step"]["profile"]["data"]
    for marker in profile_form["data_schema"].schema:
        key = getattr(marker, "schema", None)
        if isinstance(key, str):
            assert key in labels, key


async def test_agent_module_loads_on_this_home_assistant(hass):
    """agent.py imports the 2026.10 ChatLog types and can be constructed."""
    from custom_components.assist_harness.agent import MCPAssistConversationEntity

    entry = _profile(hass)
    entity = MCPAssistConversationEntity(hass, entry)
    assert entity.name
    assert entity._tool_cache_token()[0] == 0


async def test_v2_options_migrate_to_subentries(hass):
    """Kira's installed v2 layout: Brave and read_url on the system entry, Mem0 on the profile."""
    system = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        title="Shared MCP Server Settings",
        version=2,
        data={"mcp_port": 8090, "allowed_ips": ""},
        options={
            "tools": [
                {"instance_id": "brave", "type": "brave", "prefix": "", "config": {"api_key": "abc"}},
                {"instance_id": "read_url", "type": "read_url", "prefix": "", "config": {}},
            ]
        },
    )
    profile = MockConfigEntry(
        domain=DOMAIN,
        unique_id="assist_harness_ollama_kira",
        title="Ollama - Kira",
        version=2,
        data={"profile_name": "Kira", "model_name": "llama3", "system_prompt": "be kind"},
        options={
            "tools": [
                {
                    "instance_id": "mem0_memory",
                    "type": "mem0_memory",
                    "prefix": "",
                    "config": {
                        "mem0_enabled": True,
                        "mem0_url": "https://api.mem0.ai",
                        "mem0_api_key": "tok",
                        "mem0_user_id": "kira",
                    },
                }
            ]
        },
    )
    system.add_to_hass(hass)
    profile.add_to_hass(hass)
    assert await async_migrate_entry(hass, profile)
    assert await async_migrate_entry(hass, system)
    assert system.data["mcp_port"] == 8091
    assert system.version == 3
    assert profile.version == 3
    assert "tools" not in system.options
    assert "tools" not in profile.options
    assert profile.data["system_prompt"] == "be kind"
    by_type = {sub.subentry_type: sub for sub in system.subentries.values()}
    assert set(by_type) == {"brave", "read_url", "mem0_memory"}
    assert by_type["brave"].data["api_key"] == "abc"
    assert by_type["mem0_memory"].data["mem0_url"] == "https://api.mem0.ai"
    assert by_type["mem0_memory"].data["mem0_api_key"] == "tok"
    assert by_type["mem0_memory"].data["mem0_user_id"] == "kira"
    assert by_type["mem0_memory"].data["mem0_enabled"] is True
