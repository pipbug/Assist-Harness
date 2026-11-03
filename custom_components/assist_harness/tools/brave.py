"""Brave Search custom tool for ha-lmstudio-mcp."""
import aiohttp
import asyncio
import logging
from typing import Dict, Any, List

_LOGGER = logging.getLogger(__name__)

class BraveSearchTool:
    """Brave Search API tool."""

    def __init__(self, hass, api_key=None):
        """Initialize Brave Search tool."""
        self.hass = hass
        # Use provided API key - no fallback for security
        self.api_key = api_key or ""
        self.base_url = "https://api.search.brave.com/res/v1/web/search"

    async def initialize(self):
        """Initialize the tool."""
        pass  # No logging needed

    def handles_tool(self, tool_name: str) -> bool:
        """Check if this class handles the given tool."""
        return tool_name == "search"

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Get MCP tool definition for Brave Search."""
        return [{
            "name": "search",
            "description": "Search the web for current information using Brave Search",
            "inputSchema": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query"
                    },
                    "count": {
                        "type": "number",
                        "description": "Number of results to return (default 5, max 20)",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5
                    }
                },
                "required": ["query"],
                "additionalProperties": False
            }
        }]

    async def handle_call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Brave Search."""
        query = arguments.get("query")
        count = min(arguments.get("count", 5), 20)  # Enforce max limit

        _LOGGER.debug(f"Brave Search: '{query}' (count: {count})")

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key
        }

        # Fix: Convert all values to strings for URL parameters
        params = {
            "q": query,
            "count": str(count),  # Convert to string
            "text_decorations": "false",  # String not boolean
            "search_lang": "en",
            "country": "us"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.base_url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        error = await response.text()
                        _LOGGER.error(f"Brave Search error {response.status}: {error}")
                        return {
                            "content": [{
                                "type": "text",
                                "text": f"❌ Search failed (HTTP {response.status}): {error[:200]}"
                            }]
                        }

                    data = await response.json()

                    # Format results for LLM
                    results = []
                    for item in data.get("web", {}).get("results", [])[:count]:
                        results.append({
                            "title": item.get("title", ""),
                            "url": item.get("url", ""),
                            "description": item.get("description", "")
                        })

                    # Format as text for the LLM
                    text_results = f"🔍 Search results for '{query}':\n\n"
                    for i, result in enumerate(results, 1):
                        text_results += f"{i}. **{result['title']}**\n"
                        text_results += f"   {result['url']}\n"
                        text_results += f"   {result['description']}\n\n"

                    return {
                        "content": [{
                            "type": "text",
                            "text": text_results
                        }]
                    }

        except asyncio.TimeoutError:  # Fix: Correct exception
            _LOGGER.error("Brave Search timeout")
            return {
                "content": [{
                    "type": "text",
                    "text": "❌ Search timeout - please try again"
                }]
            }
        except Exception as e:
            _LOGGER.error(f"Brave Search exception: {e}")
            return {
                "content": [{
                    "type": "text",
                    "text": f"❌ Search error: {str(e)}"
                }]
            }


from .base import Tool, ToolInstance, apply_prefix, strip_prefix
import voluptuous as vol


class _Registered(Tool):

    id = "brave"
    name = "Brave Search"
    description = "Search the web with the Brave Search API. Needs an API key."
    shared = True
    allow_multiple = True
    secrets = frozenset({"api_key"})
    config_schema = vol.Schema({vol.Optional("api_key", default=""): str})

    async def async_setup(self, hass, instance: ToolInstance) -> None:
        runtime = BraveSearchTool(hass, (instance.config or {}).get("api_key") or "")
        await runtime.initialize()
        instance.runtime = runtime

    async def async_teardown(self, hass, instance: ToolInstance) -> None:
        instance.runtime = None

    def definitions(self, hass, instance: ToolInstance):
        runtime = instance.runtime or BraveSearchTool(hass, "")
        return apply_prefix(instance, runtime.get_tool_definitions())

    async def async_call(self, hass, instance, tool_name, arguments):
        return await instance.runtime.handle_call(strip_prefix(instance, tool_name), arguments)


TOOL = _Registered()
