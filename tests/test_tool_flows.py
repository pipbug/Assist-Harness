"""Options, subentry, migration, and translation coverage."""

from __future__ import annotations

import json
import re
from pathlib import Path

from homeassistant.helpers.selector import TextSelector, TextSelectorType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.assist_harness.const import DOMAIN, SYSTEM_ENTRY_UNIQUE_ID
from custom_components.assist_harness.mcp_server import MCPServer
from custom_components.assist_harness.migration import async_migrate_entry
from custom_components.assist_harness.tools.registry import (
    async_ensure_tool_types,
    async_get_registry,
    filter_tools_for_entry,
)

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


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{([a-zA-Z0-9_]+)\}", text))


def test_translation_placeholders_match_strings_json():
    """A translated string must ask for the same {placeholders} as strings.json.

    The frontend renders translations/*.json, so a leftover {server_info} there
    crashes even when strings.json no longer has that variable.
    """

    def walk(source, translated, prefix=""):
        if isinstance(source, dict):
            assert isinstance(translated, dict), prefix
            for key, value in source.items():
                walk(value, translated[key], f"{prefix}.{key}" if prefix else key)
            return
        if isinstance(source, str):
            assert _placeholders(source) == _placeholders(translated), (
                f"{prefix} placeholders strings={_placeholders(source)} "
                f"translation={_placeholders(translated)}"
            )

    for path in (ROOT / "custom_components/assist_harness/translations").glob("*.json"):
        walk(STRINGS, json.loads(path.read_text()))


def test_config_flow_steps_are_in_strings_json():
    """Every form step and menu option in the config flow has a strings.json entry."""
    source = (ROOT / "custom_components/assist_harness/config_flow.py").read_text()
    options_at = source.index("class AssistHarnessOptionsFlow")
    config_source, options_source = source[:options_at], source[options_at:]

    def step_ids(text: str) -> set[str]:
        return set(re.findall(r'step_id="([^"]+)"', text))

    def menu_blocks(text: str) -> list[tuple[str, list[str]]]:
        blocks = []
        for match in re.finditer(
            r'async_show_menu\(\s*step_id="([^"]+)",\s*menu_options=\[([^\]]+)\]',
            text,
            re.S,
        ):
            options = re.findall(r'"([^"]+)"', match.group(2))
            blocks.append((match.group(1), options))
        return blocks

    for step_id in step_ids(config_source):
        assert step_id in STRINGS["config"]["step"], step_id
    for step_id in step_ids(options_source):
        assert step_id in STRINGS["options"]["step"], step_id
    for step_id, options in menu_blocks(options_source):
        labels = STRINGS["options"]["step"][step_id]["menu_options"]
        for option in options:
            assert option in labels, option
    for step_id, options in menu_blocks(config_source):
        labels = STRINGS["config"]["step"][step_id]["menu_options"]
        for option in options:
            assert option in labels, option


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

    read_url = next(sub for sub in system.subentries.values() if sub.subentry_type == "read_url")
    tools_menu = await hass.config_entries.options.async_configure(
        menu["flow_id"],
        {"next_step_id": "profile_tools"},
    )
    assert tools_menu["type"] == "menu"
    assert tools_menu["step_id"] == "profile_tools"
    assert list(tools_menu["menu_options"])[0] == "choose_tools"
    assert tools_menu["menu_options"]["choose_tools"] == "Choose tools"
    assert tools_menu["menu_options"][f"tool_{read_url.subentry_id}"] == "Read URL"
    assert "shared by every profile" in STRINGS["options"]["step"]["profile_tools"]["description"]
    assert "use_all" not in STRINGS["options"]["step"].get("profile_tools", {})

    tools_step = await hass.config_entries.options.async_configure(
        tools_menu["flow_id"],
        {"next_step_id": "choose_tools"},
    )
    assert tools_step["type"] == "form"
    assert tools_step["step_id"] == "choose_tools"
    defaults = []
    for marker in tools_step["data_schema"].schema:
        if getattr(marker, "schema", None) == "enabled_tools":
            defaults = list(marker.default() if callable(marker.default) else marker.default)
    assert defaults == [read_url.subentry_id]
    chosen = await hass.config_entries.options.async_configure(
        tools_step["flow_id"],
        {"enabled_tools": []},
    )
    assert chosen["type"] == "create_entry"
    assert profile.options["disabled_tools"] == [read_url.subentry_id]
    assert "profile_tools" not in profile.options

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
    assert system.minor_version == 2
    assert profile.minor_version == 2
    assert "profile_tools" not in profile.options
    assert "disabled_tools" not in profile.options


def _enabled_default(step) -> list:
    for marker in step["data_schema"].schema:
        if getattr(marker, "schema", None) == "enabled_tools":
            default = marker.default
            return list(default() if callable(default) else default)
    return []


async def _add_tool(hass, system, tool_id, payload):
    created = await hass.config_entries.subentries.async_init(
        (system.entry_id, tool_id),
        context={"source": "user"},
    )
    await hass.config_entries.subentries.async_configure(created["flow_id"], payload)
    return next(sub for sub in system.subentries.values() if sub.subentry_type == tool_id and sub.title == payload["name"])


@pytest.mark.usefixtures("_flow_deps")
async def test_profile_tools_menu_edits_shared_settings_and_keeps_new_tools(hass):
    """Choosing tools and editing one from the profile updates the system subentry."""
    from custom_components.assist_harness import async_setup_entry, async_update_options

    await async_ensure_tool_types(hass)
    system = _system(hass)
    assert await async_setup_entry(hass, system)
    profile = _profile(hass)
    profile.add_update_listener(async_update_options)
    brave = await _add_tool(
        hass, system, "brave", {"name": "Brave Search", "prefix": "", "api_key": "abc"}
    )
    read_url = await _add_tool(
        hass, system, "read_url", {"name": "Read URL", "prefix": ""}
    )
    reloads = []

    async def _reload(entry_id):
        reloads.append(entry_id)
        return True

    hass.config_entries.async_reload = _reload
    registry = await async_get_registry(hass)
    await registry.async_reload()
    server = MCPServer(hass, 8091, system)
    server.registry = registry

    menu = await hass.config_entries.options.async_init(profile.entry_id)
    tools_menu = await hass.config_entries.options.async_configure(
        menu["flow_id"],
        {"next_step_id": "profile_tools"},
    )
    assert list(tools_menu["menu_options"]) == [
        "choose_tools",
        f"tool_{brave.subentry_id}",
        f"tool_{read_url.subentry_id}",
    ]
    assert tools_menu["menu_options"][f"tool_{brave.subentry_id}"] == "Brave Search"

    choose = await hass.config_entries.options.async_configure(
        tools_menu["flow_id"],
        {"next_step_id": "choose_tools"},
    )
    assert _enabled_default(choose) == [brave.subentry_id, read_url.subentry_id]
    saved = await hass.config_entries.options.async_configure(
        choose["flow_id"],
        {"enabled_tools": [brave.subentry_id]},
    )
    assert saved["type"] == "create_entry"
    assert profile.options["disabled_tools"] == [read_url.subentry_id]

    duck = await _add_tool(
        hass, system, "duckduckgo", {"name": "DuckDuckGo", "prefix": "ddg"}
    )
    await hass.async_block_till_done()
    await registry.async_reload()
    menu = await hass.config_entries.options.async_init(profile.entry_id)
    tools_menu = await hass.config_entries.options.async_configure(
        menu["flow_id"],
        {"next_step_id": "profile_tools"},
    )
    choose = await hass.config_entries.options.async_configure(
        tools_menu["flow_id"],
        {"next_step_id": "choose_tools"},
    )
    assert _enabled_default(choose) == [brave.subentry_id, duck.subentry_id]
    listed = [
        {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}
        for name in ("discover_entities", "search", "read_url", "ddg_search")
    ]
    kept = [tool["function"]["name"] for tool in filter_tools_for_entry(hass, profile, listed)]
    assert kept == ["discover_entities", "search", "ddg_search"]

    # Leave the chooser without saving and edit Brave from the menu.
    hass.config_entries.options.async_abort(choose["flow_id"])
    menu = await hass.config_entries.options.async_init(profile.entry_id)
    tools_menu = await hass.config_entries.options.async_configure(
        menu["flow_id"],
        {"next_step_id": "profile_tools"},
    )
    form = await hass.config_entries.options.async_configure(
        tools_menu["flow_id"],
        {"next_step_id": f"tool_{brave.subentry_id}"},
    )
    assert form["type"] == "form"
    assert form["step_id"] == "tool_settings"
    assert "shared by every profile" in STRINGS["options"]["step"]["tool_settings"]["description"]
    secret_default = None
    for marker, selector in form["data_schema"].schema.items():
        if getattr(marker, "schema", None) == "api_key":
            assert isinstance(selector, TextSelector)
            assert selector.config["type"] == TextSelectorType.PASSWORD
            secret_default = marker.default() if callable(marker.default) else marker.default
    assert secret_default == ""
    back = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {"name": "Web search", "prefix": "home", "api_key": ""},
    )
    assert back["type"] == "menu"
    assert back["step_id"] == "profile_tools"
    updated = system.subentries[brave.subentry_id]
    assert updated.title == "Web search"
    assert updated.data["prefix"] == "home_"
    assert updated.data["api_key"] == "abc"
    await hass.async_block_till_done()
    names = [tool["name"] for tool in (await server.handle_tools_list())["tools"]]
    assert "home_search" in names
    assert "search" not in names
    assert "read_url" in names
    assert reloads == []

    hass.config_entries.async_remove_subentry(system, read_url.subentry_id)
    await hass.async_block_till_done()
    assert "disabled_tools" not in profile.options
    assert reloads == []


async def test_profile_tools_allowlist_migrates_to_disabled_set(hass):
    """An old allowlist becomes the tools that were left unticked."""
    system = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        title="Shared MCP Server Settings",
        version=3,
        minor_version=2,
        data={"mcp_port": 8091},
        subentries_data=[
            {
                "subentry_id": "sub-brave",
                "subentry_type": "brave",
                "title": "Brave Search",
                "unique_id": "brave",
                "data": {"prefix": "", "api_key": "abc"},
            },
            {
                "subentry_id": "sub-read",
                "subentry_type": "read_url",
                "title": "Read URL",
                "unique_id": "read_url",
                "data": {"prefix": ""},
            },
        ],
    )
    profile = MockConfigEntry(
        domain=DOMAIN,
        unique_id="assist_harness_ollama_kira",
        title="Ollama - Kira",
        version=3,
        minor_version=1,
        data={"profile_name": "Kira"},
        options={"profile_tools": ["read_url"], "system_prompt": "be kind"},
    )
    everyone = MockConfigEntry(
        domain=DOMAIN,
        unique_id="assist_harness_ollama_all",
        title="Ollama - All",
        version=3,
        minor_version=1,
        data={"profile_name": "All"},
        options={"system_prompt": "hi"},
    )
    waiting = MockConfigEntry(
        domain=DOMAIN,
        unique_id="assist_harness_ollama_waiting",
        title="Ollama - Waiting",
        version=3,
        minor_version=1,
        data={"profile_name": "Waiting"},
        options={"profile_tools": ["read_url"]},
    )
    system.add_to_hass(hass)
    profile.add_to_hass(hass)
    everyone.add_to_hass(hass)
    assert await async_migrate_entry(hass, profile)
    assert await async_migrate_entry(hass, everyone)
    assert profile.minor_version == 2
    assert profile.options["disabled_tools"] == ["sub-brave"]
    assert profile.options["system_prompt"] == "be kind"
    assert "profile_tools" not in profile.options
    assert everyone.minor_version == 2
    assert "disabled_tools" not in everyone.options
    assert "profile_tools" not in everyone.options

    # No tool subentries yet, so the allowlist stays until a later startup.
    await hass.config_entries.async_remove(system.entry_id)
    waiting.add_to_hass(hass)
    assert await async_migrate_entry(hass, waiting)
    assert waiting.minor_version == 1
    assert waiting.options["profile_tools"] == ["read_url"]


async def test_system_migration_finishes_a_deferred_allowlist(hass):
    """A profile migrated before the tools exist is converted with the system entry."""
    system = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        title="Shared MCP Server Settings",
        version=2,
        data={"mcp_port": 8091},
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
        version=3,
        minor_version=1,
        data={"profile_name": "Kira"},
        options={"profile_tools": ["read_url"]},
    )
    system.add_to_hass(hass)
    profile.add_to_hass(hass)
    assert await async_migrate_entry(hass, profile)
    assert profile.minor_version == 1
    assert profile.options["profile_tools"] == ["read_url"]
    assert await async_migrate_entry(hass, system)
    assert system.minor_version == 2
    assert profile.minor_version == 2
    assert "profile_tools" not in profile.options
    brave = next(sub for sub in system.subentries.values() if sub.subentry_type == "brave")
    assert profile.options["disabled_tools"] == [brave.subentry_id]
