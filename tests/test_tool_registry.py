"""Registry, migration, import, and external MCP coverage."""

from __future__ import annotations

import sys
from pathlib import Path

from aiohttp import web
from homeassistant.helpers.selector import TextSelector, TextSelectorType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.assist_harness.const import DOMAIN, SYSTEM_ENTRY_UNIQUE_ID
from custom_components.assist_harness.mcp_server import MCPServer
from custom_components.assist_harness.migration import (
    async_import_mcp_assist,
    async_migrate_entry,
    migrate_mapping,
    plan_mcp_assist_import,
)
from custom_components.assist_harness.tool_flow import tool_config_schema
from custom_components.assist_harness.tools.registry import (
    discover_tool_types,
    filter_tools_for_entry,
    get_registry,
)

BUILTIN = "discover_entities"


def _entry(hass, *, unique_id, data=None, options=None, domain=DOMAIN, version=2, title="Test"):
    entry = MockConfigEntry(
        domain=domain,
        unique_id=unique_id,
        title=title,
        data=data or {},
        options=options or {},
        version=version,
    )
    entry.add_to_hass(hass)
    return entry


async def _names(server):
    result = await server.handle_tools_list()
    return [tool["name"] for tool in result["tools"]]


def test_registry_discovers_builtin_modules():
    """Every built-in tool module is found without a hardcoded list."""
    found = discover_tool_types()
    assert set(found) == {
        "brave",
        "duckduckgo",
        "searxng",
        "read_url",
        "mem0_memory",
        "external_mcp",
    }
    assert found["mem0_memory"].shared is False
    assert found["searxng"].allow_multiple is True
    assert found["external_mcp"].allow_multiple is True
    assert "api_key" in found["brave"].secrets
    assert "bearer_token" in found["external_mcp"].secrets
    assert "mem0_api_key" in found["mem0_memory"].secrets


def test_drop_in_module_is_discovered():
    """Adding a tool is a new file. The registry does not keep a name list."""
    path = Path(__file__).resolve().parents[1] / "custom_components/assist_harness/tools/zz_sample.py"
    path.write_text(
        "from .base import Tool\n"
        "import voluptuous as vol\n"
        "class _Sample(Tool):\n"
        "    id = 'zz_sample'\n"
        "    name = 'Sample'\n"
        "    description = 'Drop-in example.'\n"
        "    shared = True\n"
        "    allow_multiple = False\n"
        "    secrets = frozenset({'token'})\n"
        "    config_schema = vol.Schema({vol.Optional('token', default=''): str})\n"
        "TOOL = _Sample()\n"
    )
    module_name = "custom_components.assist_harness.tools.zz_sample"
    sys.modules.pop(module_name, None)
    try:
        found = discover_tool_types()
        assert found["zz_sample"].name == "Sample"
        assert found["zz_sample"].secrets == frozenset({"token"})
    finally:
        path.unlink(missing_ok=True)
        sys.modules.pop(module_name, None)


def test_secret_fields_render_as_passwords():
    tool = discover_tool_types()["brave"]
    schema = tool_config_schema(tool, {})
    for marker, validator in schema.schema.items():
        if getattr(marker, "schema", None) == "api_key":
            assert isinstance(validator, TextSelector)
            assert validator.config["type"] == TextSelectorType.PASSWORD
            break
    else:
        raise AssertionError("api_key field missing")


async def test_add_and_remove_tool_updates_tools_list_without_restart(hass):
    """Adding or removing a tool changes tools/list on the running server object."""
    system = _entry(
        hass,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        data={"mcp_port": 8091},
        options={"tools": []},
        title="Shared",
    )
    registry = get_registry(hass)
    await registry.async_reload()
    server = MCPServer(hass, 8091, system)
    server.registry = registry
    assert server.site is None

    listed = await _names(server)
    assert BUILTIN in listed
    assert "search" not in listed
    assert "read_url" not in listed

    hass.config_entries.async_update_entry(
        system,
        options={
            "tools": [
                {
                    "instance_id": "brave",
                    "type": "brave",
                    "prefix": "",
                    "config": {"api_key": "test-key"},
                },
                {
                    "instance_id": "read_url",
                    "type": "read_url",
                    "prefix": "",
                    "config": {},
                },
            ]
        },
    )
    await registry.async_reload()
    listed = await _names(server)
    assert "search" in listed
    assert "read_url" in listed
    assert BUILTIN in listed
    assert server.site is None

    hass.config_entries.async_update_entry(
        system,
        options={
            "tools": [
                {
                    "instance_id": "read_url",
                    "type": "read_url",
                    "prefix": "",
                    "config": {},
                }
            ]
        },
    )
    await registry.async_reload()
    listed = await _names(server)
    assert "search" not in listed
    assert "read_url" in listed
    assert server.site is None


async def test_multiple_instances_use_prefixes(hass):
    system = _entry(
        hass,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        options={
            "tools": [
                {
                    "instance_id": "searxng",
                    "type": "searxng",
                    "prefix": "home",
                    "config": {"url": "http://192.168.1.10:8888"},
                },
                {
                    "instance_id": "searxng_2",
                    "type": "searxng",
                    "prefix": "lab_",
                    "config": {"url": "http://192.168.1.20:8888"},
                },
            ]
        },
    )
    registry = get_registry(hass)
    await registry.async_reload()
    server = MCPServer(hass, 8091, system)
    server.registry = registry
    listed = set(await _names(server))
    assert "home_search" in listed
    assert "lab_search" in listed


async def test_profile_tool_filter_defaults_to_all(hass):
    system = _entry(
        hass,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        options={
            "tools": [
                {"instance_id": "brave", "type": "brave", "prefix": "", "config": {"api_key": "k"}},
                {"instance_id": "read_url", "type": "read_url", "prefix": "", "config": {}},
            ]
        },
    )
    profile = _entry(
        hass,
        unique_id="assist_harness_ollama_kira",
        data={"profile_name": "Kira"},
        options={},
    )
    registry = get_registry(hass)
    await registry.async_reload()
    tools = [
        {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}
        for name in (BUILTIN, "search", "read_url")
    ]
    kept = [tool["function"]["name"] for tool in filter_tools_for_entry(hass, profile, tools)]
    assert kept == [BUILTIN, "search", "read_url"]

    hass.config_entries.async_update_entry(profile, options={"profile_tools": ["read_url"]})
    kept = [tool["function"]["name"] for tool in filter_tools_for_entry(hass, profile, tools)]
    assert kept == [BUILTIN, "read_url"]

    hass.config_entries.async_update_entry(profile, options={"profile_tools": []})
    kept = [tool["function"]["name"] for tool in filter_tools_for_entry(hass, profile, tools)]
    assert kept == [BUILTIN]
    assert system.entry_id


def test_legacy_search_and_mem0_migration():
    data, options = migrate_mapping(
        {
            "mcp_port": 8090,
            "search_provider": "brave",
            "brave_api_key": "abc",
            "allowed_ips": "",
        },
        {},
        convert_search=True,
    )
    assert data["mcp_port"] == 8090
    assert "search_provider" not in data
    assert "brave_api_key" not in data
    by_type = {tool["type"]: tool for tool in options["tools"]}
    assert by_type["brave"]["config"] == {"api_key": "abc"}
    assert by_type["brave"]["prefix"] == ""
    assert "read_url" in by_type

    _, duck = migrate_mapping({"search_provider": "duckduckgo"}, {}, convert_search=True)
    assert {tool["type"] for tool in duck["tools"]} == {"duckduckgo", "read_url"}

    _, searx = migrate_mapping(
        {"search_provider": "searxng", "searxng_url": "http://192.168.1.10:8888"},
        {},
        convert_search=True,
    )
    searx_tool = next(tool for tool in searx["tools"] if tool["type"] == "searxng")
    assert searx_tool["config"]["url"] == "http://192.168.1.10:8888"
    assert any(tool["type"] == "read_url" for tool in searx["tools"])

    _, none_tools = migrate_mapping({"search_provider": "none"}, {}, convert_search=True)
    assert "tools" not in none_tools

    _, legacy_flag = migrate_mapping({"enable_custom_tools": True}, {}, convert_search=True)
    assert {tool["type"] for tool in legacy_flag["tools"]} == {"brave", "read_url"}

    _, mem0_options = migrate_mapping(
        {},
        {
            "mem0_enabled": True,
            "mem0_url": "https://api.mem0.ai",
            "mem0_api_key": "tok",
            "mem0_user_id": "kira",
            "system_prompt": "be kind",
        },
        convert_search=False,
    )
    assert "mem0_enabled" not in mem0_options
    assert mem0_options["system_prompt"] == "be kind"
    mem0 = mem0_options["tools"][0]
    assert mem0["type"] == "mem0_memory"
    assert mem0["prefix"] == ""
    assert mem0["config"] == {
        "mem0_enabled": True,
        "mem0_url": "https://api.mem0.ai",
        "mem0_api_key": "tok",
        "mem0_user_id": "kira",
    }


async def test_config_entry_version_migration(hass):
    system = _entry(
        hass,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        version=1,
        data={"mcp_port": 8091, "search_provider": "duckduckgo"},
        title="Shared",
    )
    profile = _entry(
        hass,
        unique_id="assist_harness_ollama_kira",
        version=1,
        data={"profile_name": "Kira", "model_name": "llama3"},
        options={"mem0_enabled": False, "mem0_url": "http://127.0.0.1:8888", "mem0_user_id": "kira"},
        title="Ollama - Kira",
    )
    assert await async_migrate_entry(hass, system)
    assert await async_migrate_entry(hass, profile)
    assert system.version == 3
    assert profile.version == 3
    assert "tools" not in system.options
    assert "tools" not in profile.options
    by_type = {sub.subentry_type: sub for sub in system.subentries.values()}
    assert set(by_type) == {"duckduckgo", "read_url", "mem0_memory"}
    mem0 = by_type["mem0_memory"].data
    assert mem0["mem0_enabled"] is False
    assert mem0["mem0_url"] == "http://127.0.0.1:8888"
    assert mem0["mem0_user_id"] == "kira"

    registry = get_registry(hass)
    await registry.async_reload()
    names = {item["name"] for item in registry.definitions()}
    assert "search" in names
    assert "read_url" in names
    assert "recall_memory" not in names


async def test_mcp_assist_import_copies_entries_and_leaves_originals(hass, monkeypatch):
    old_system = MockConfigEntry(
        domain="mcp_assist",
        unique_id="mcp_assist_system_settings",
        title="Shared MCP Server Settings",
        version=1,
        data={
            "mcp_port": 8090,
            "search_provider": "brave",
            "brave_api_key": "abc",
            "allowed_ips": "192.168.1.0/24",
            "enable_gap_filling": True,
        },
    )
    old_profile = MockConfigEntry(
        domain="mcp_assist",
        unique_id="mcp_assist_ollama_kira",
        title="Ollama - Kira",
        version=1,
        data={
            "profile_name": "Kira",
            "server_type": "ollama",
            "model_name": "llama3",
            "system_prompt": "be kind",
            "technical_prompt": "use the tools",
            "api_key": "secret",
        },
        options={
            "mem0_enabled": True,
            "mem0_url": "https://api.mem0.ai",
            "mem0_api_key": "tok",
            "mem0_user_id": "kira",
        },
    )
    before = {
        "system_data": dict(old_system.data),
        "system_options": dict(old_system.options),
        "profile_data": dict(old_profile.data),
        "profile_options": dict(old_profile.options),
        "system_version": old_system.version,
        "profile_version": old_profile.version,
    }
    old_system.add_to_hass(hass)
    old_profile.add_to_hass(hass)

    async def _no_setup(_entry_id):
        return True

    monkeypatch.setattr(hass.config_entries, "async_setup", _no_setup)
    created = await async_import_mcp_assist(hass)
    assert len(created) == 2

    assert dict(old_system.data) == before["system_data"]
    assert dict(old_system.options) == before["system_options"]
    assert dict(old_profile.data) == before["profile_data"]
    assert dict(old_profile.options) == before["profile_options"]
    assert old_system.version == before["system_version"]
    assert old_profile.version == before["profile_version"]
    assert old_system.domain == "mcp_assist"
    assert hass.config_entries.async_entries("mcp_assist")

    new_system = hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, SYSTEM_ENTRY_UNIQUE_ID
    )
    assert new_system is not None
    assert new_system.data["mcp_port"] == 8091
    assert new_system.data["allowed_ips"] == "192.168.1.0/24"
    by_type = {sub.subentry_type: sub for sub in new_system.subentries.values()}
    assert set(by_type) == {"brave", "read_url", "mem0_memory"}
    assert by_type["brave"].data["api_key"] == "abc"
    assert by_type["mem0_memory"].data["mem0_url"] == "https://api.mem0.ai"
    assert by_type["mem0_memory"].data["mem0_api_key"] == "tok"
    assert by_type["mem0_memory"].data["mem0_enabled"] is True
    assert by_type["mem0_memory"].data["mem0_user_id"] == "kira"

    new_profile = hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, "assist_harness_ollama_kira"
    )
    assert new_profile is not None
    assert new_profile.data["system_prompt"] == "be kind"
    assert new_profile.data["technical_prompt"] == "use the tools"
    assert new_profile.data["model_name"] == "llama3"
    assert new_profile.data["api_key"] == "secret"
    assert not new_profile.subentries

    # Planning itself does not touch the source objects.
    snapshot = dict(old_profile.options)
    plan_mcp_assist_import([old_system, old_profile])
    assert dict(old_profile.options) == snapshot


async def test_config_flow_offers_mcp_assist_import(hass, monkeypatch):
    async def _skip_deps(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "homeassistant.config_entries.async_process_deps_reqs", _skip_deps
    )
    MockConfigEntry(
        domain="mcp_assist",
        unique_id="mcp_assist_system_settings",
        title="Shared",
        data={"mcp_port": 8090, "search_provider": "none"},
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "form"
    assert result["step_id"] == "import_offer"
    declined = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"import_existing": False}
    )
    assert declined["type"] == "form"
    assert declined["step_id"] == "user"
    # Declining did not create Assist Harness entries or remove the old one.
    assert not hass.config_entries.async_entries(DOMAIN)
    assert hass.config_entries.async_entries("mcp_assist")


async def _serve_mcp(handler):
    app = web.Application()
    app.router.add_post("/mcp", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/mcp"


@pytest.mark.enable_socket
async def test_external_mcp_proxies_and_omits_when_down(hass, monkeypatch):
    import socket

    import aiohttp
    import pytest_socket

    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts(["127.0.0.1", "::1"])

    class _IpResolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [
                {
                    "hostname": host,
                    "host": host,
                    "port": port,
                    "family": family,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            ]

        async def close(self):
            return None

    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=_IpResolver()))
    monkeypatch.setattr(
        "custom_components.assist_harness.tools.external_mcp.async_get_clientsession",
        lambda _hass, *args, **kwargs: session,
    )
    seen = {"authorization": None}

    async def handler(request):
        body = await request.json()
        seen["authorization"] = request.headers.get("Authorization")
        method = body.get("method")
        message_id = body.get("id")
        if method == "initialize":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "demo", "version": "1"},
                    },
                }
            )
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/list":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "tools": [
                            {
                                "name": "ping",
                                "description": "Ping the demo server",
                                "inputSchema": {"type": "object", "properties": {}},
                            }
                        ]
                    },
                }
            )
        if method == "tools/call":
            assert body["params"]["name"] == "ping"
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"content": [{"type": "text", "text": "pong"}]},
                }
            )
        return web.json_response(
            {"jsonrpc": "2.0", "id": message_id, "error": {"code": -32601, "message": "no"}}
        )

    runner, url = await _serve_mcp(handler)
    system = _entry(
        hass,
        unique_id=SYSTEM_ENTRY_UNIQUE_ID,
        options={
            "tools": [
                {"instance_id": "brave", "type": "brave", "prefix": "", "config": {"api_key": "k"}},
                {
                    "instance_id": "demo",
                    "type": "external_mcp",
                    "prefix": "demo_",
                    "config": {
                        "url": "http://127.0.0.1:1/mcp",
                        "transport": "streamable_http",
                        "bearer_token": "secret",
                        "headers": "",
                    },
                },
            ]
        },
    )
    registry = get_registry(hass)
    await registry.async_reload()
    server = MCPServer(hass, 8091, system)
    server.registry = registry

    listed = await _names(server)
    assert "search" in listed
    assert "demo_ping" not in listed

    tools = system.options["tools"]
    tools[1]["config"]["url"] = url
    hass.config_entries.async_update_entry(system, options={"tools": tools})
    await registry.async_reload()
    listed = await _names(server)
    assert "demo_ping" in listed
    assert "search" in listed
    assert seen["authorization"] == "Bearer secret"

    result = await server.handle_tool_call({"name": "demo_ping", "arguments": {}})
    assert result["content"][0]["text"] == "pong"

    await runner.cleanup()
    await registry.async_refresh()
    listed = await _names(server)
    assert "demo_ping" not in listed
    assert "search" in listed
    assert BUILTIN in listed
    await registry.async_teardown_all()
    await session.close()
