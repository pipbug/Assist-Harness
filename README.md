# Assist Harness for Home Assistant

Assist Harness is a Home Assistant conversation agent. It runs an MCP server on Home Assistant so the model discovers and controls entities with tools instead of receiving the whole entity list on every turn. It works with LM Studio, llama.cpp, Ollama, vLLM, OpenAI (and OpenAI-compatible services), Google Gemini, Anthropic Claude, and OpenRouter, and it can hand a turn to an OpenClaw or Hermes Agent gateway.

Assist Harness began as a fork of [mike-nott/mcp-assist](https://github.com/mike-nott/mcp-assist) (MIT).

Optional tools are modules. Add one by dropping a file into `custom_components/assist_harness/tools/`. The options flow picks it up with no edit to the loader, `const.py`, or `config_flow.py`.

## Features

- MCP tools for entity discovery, state, history, scripts, and automations, so a large home does not dump thousands of entities into the prompt
- Smart Entity Index: a pre-generated system structure index (~400-800 tokens) for context-aware queries
- Several profiles, each with its own model, prompts, and choice of tools
- Optional tools you can add and remove while Home Assistant is running: Brave, DuckDuckGo, SearXNG, Read URL, Mem0, and external MCP servers
- More than one instance of the same tool when that is useful (two SearXNG servers, several external MCP servers), with a name prefix so their tools do not collide
- Per profile, choose which enabled tools that profile's agent sees. Built-in Home Assistant tools stay on
- Thinking control, multilingual prompts, and voice-oriented response cleanup

## Smart Entity Index

The Smart Entity Index is a lightweight (~400-800 tokens) snapshot of your Home Assistant system structure. The model reads it with the `get_index` tool, so it can choose sensible queries without receiving a full entity dump. The index includes areas, domains, device classes, people, calendars, zones, automations, and scripts. For entities without a standardized `device_class` (for example from custom integrations), LLM gap-filling infers semantic categories from naming patterns. That keeps queries faster and more accurate, and uses about 95% fewer tokens than sending every entity.

**Enable Smart Entity Index** is a shared setting, on by default. It turns gap-filling on for every profile, because they all use the same MCP server.

## Requirements

- Home Assistant 2024.1 or newer
- One of:
  - Local: LM Studio, llama.cpp, Ollama, or vLLM
  - Cloud: OpenAI (or a compatible endpoint), Gemini, Claude, or OpenRouter
  - Agent gateway: OpenClaw or Hermes Agent
- Python 3.11+

## Installation

### HACS

Add `https://github.com/pipbug/assist-harness` as a custom repository (category: Integration), install **Assist Harness**, and restart Home Assistant.

### Manual

Copy `custom_components/assist_harness` into your Home Assistant `custom_components` directory and restart.

The default MCP port is **8091**, so Assist Harness can run next to an existing MCP Assist install on 8090.

## Configuration

1. **Settings → Devices & services → Add integration → Assist Harness**
2. If config entries for the old `mcp_assist` domain exist and Assist Harness has none yet, the first step offers to import them. Import copies the system entry and every profile (prompts, model, keys, Mem0). It does not modify or delete the MCP Assist entries.
3. Create a profile: name, server type, URL or API key, model, prompts, and advanced settings.
4. Shared server settings (port, allowed IPs, entity index) apply to every profile. Changing the port needs a restart. Tools do not.

### Tools

On the Assist Harness integration page, the system entry lists each tool as its own item. Each tool type has an **Add** button (Add Brave Search, Add SearXNG, Add external MCP server, and so on). Open an item to reconfigure it, or delete it from the same place. Adding, changing, or deleting a tool updates the MCP `tools/list` immediately. Home Assistant does not restart.

A tool that can be added more than once (two SearXNG servers, several external MCP servers) asks for a name and an optional tool-name prefix so the tools do not collide (`home` becomes `home_`).

Open a profile and choose **Tools**. The form has one section per tool, titled with that tool's display name (and its prefix when you have two of the same kind, such as two SearXNG servers). The first switch, **Enabled for this profile**, hides the tool from that profile only. A tool you add later stays on until you switch it off. The fields under the switch are that tool's settings. They are stored on the system entry and shared by every profile; a secret left blank keeps the value already stored. One submit saves the switches and any settings that changed. Built-in Home Assistant tools such as `discover_entities` and `perform_action` are not in that list; they stay on.

### Built-in tool types

| Id | What it adds | Config |
| --- | --- | --- |
| `brave` | `search` via the Brave Search API | API key |
| `duckduckgo` | `search` via DuckDuckGo | none |
| `searxng` | `search` via your SearXNG server | URL. JSON output must be enabled (`search.formats` includes `json`) |
| `read_url` | `read_url` fetches a page and returns text. It is its own tool, not tied to search | none |
| `mem0_memory` | `recall_memory` and `save_memory`, plus a background save of each finished turn | enable, URL, API key, user id |
| `external_mcp` | Proxies another MCP server's tools | URL, transport (`streamable_http` or `sse`), optional bearer token, optional extra headers, and the instance prefix |

An external server that is down is skipped: its tools are left off `tools/list` and the rest of the server keeps working. The next `tools/list` tries to reconnect.

Mem0 talks to a self-hosted server (`X-API-Key`, `/memories`, `/search`) or to the hosted platform when the URL is `https://api.mem0.ai` (`Authorization: Token`, `/v3/memories/...`). The turn save runs in the background and is skipped when `save_memory` already ran in that turn. A Mem0 outage logs a warning and does not break the reply.

### Upgrading an Assist Harness entry

Assist Harness **0.21.2** stores tools as config subentries (config entry version 3). On startup, version 1 and version 2 entries are migrated in place:

- `search_provider: brave`, `duckduckgo`, or `searxng` becomes that search tool plus `read_url`. An old `enable_custom_tools: true` with no provider becomes Brave plus `read_url`. `none` adds neither.
- The Brave API key and SearXNG URL move onto those tools.
- Per-profile `mem0_enabled`, `mem0_url`, `mem0_api_key`, and `mem0_user_id` become one Mem0 subentry on the system entry with the same values, including a disabled switch.
- Version 2 tool lists (the `tools` option) are moved into those same subentries.

After migration, search names stay `search` and `read_url`, and Mem0 names stay `recall_memory` and `save_memory`.

## Writing a tool

Create `custom_components/assist_harness/tools/<id>.py`. The registry imports every module in that folder that defines `TOOL` (names starting with `_` are ignored).

```python
from __future__ import annotations

import voluptuous as vol

from .base import Tool, apply_prefix, strip_prefix

class HelloTool(Tool):
    id = "hello"
    name = "Hello"
    description = "A tiny example tool."
    shared = True              # False stores config on each profile
    allow_multiple = True      # False allows one instance per owner
    secrets = frozenset({"token"})  # rendered as password fields
    config_schema = vol.Schema({
        vol.Optional("token", default=""): str,
        vol.Optional("greeting", default="Hello"): str,
    })

    async def async_setup(self, hass, instance) -> None:
        instance.runtime = {
            "token": instance.config.get("token") or "",
            "greeting": instance.config.get("greeting") or "Hello",
        }

    async def async_teardown(self, hass, instance) -> None:
        instance.runtime = None

    def definitions(self, hass, instance):
        return apply_prefix(instance, [{
            "name": "hello",
            "description": "Say hello.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }])

    async def async_call(self, hass, instance, tool_name, arguments):
        name = strip_prefix(instance, tool_name)
        if name != "hello":
            raise ValueError(name)
        who = arguments.get("name") or "there"
        greeting = instance.runtime["greeting"]
        return {"content": [{"type": "text", "text": f"{greeting}, {who}."}]}

TOOL = HelloTool()
```

`async_setup` runs when the instance is added or its config changes. `async_teardown` runs on remove and on shutdown. If a tool's definitions come from the network, implement `async_definitions` instead of `definitions`. Return an empty list when the remote side is down; the registry calls it again on the next `tools/list`.

Restart Home Assistant once after you add the file so the module is imported. After that, add and remove instances from the Tools menu without another restart.

## Exposing entities

The control tools only act on entities exposed to Assist.

1. **Settings → Voice assistants → Expose**
2. Expose the entities this profile should control

## Development

```bash
python -m compileall custom_components/assist_harness tests
python -m pytest
```

Tests use `pytest-homeassistant-custom-component`.

## License

MIT. Copyright (c) 2025 Mike Nott. Copyright (c) 2026 Kira. See [LICENSE](LICENSE).
