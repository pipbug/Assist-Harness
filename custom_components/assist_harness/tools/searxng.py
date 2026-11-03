"""SearXNG Search custom tool for ha-lmstudio-mcp."""
import aiohttp
import asyncio
import logging
from typing import Dict, Any, List
from urllib.parse import urljoin
_LOGGER = logging.getLogger(__name__)

class SearXNGSearchTool:
    """SearXNG Search API tool."""

    def __init__(self, hass, local_url=None):
        """Initialize SearXNG Search tool."""
        self.hass = hass
        if not local_url:
            self.base_url = None
        else:
            base_url = local_url.rstrip("/")
            self.base_url = base_url if base_url.endswith("/search") else f"{base_url}/search"

    async def initialize(self):
        """Initialize the tool."""
        if not self.base_url:
            raise ValueError("SearXNG URL is not configured")

    def handles_tool(self, tool_name: str) -> bool:
        """Check if this class handles the given tool."""
        return tool_name == "search"

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """Get MCP tool definition for SearXNG Search."""
        return [{
            "name": "search",
            "description": "Search the web for current information using SearXNG",
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
        """Execute SearXNG Search."""
        query = arguments.get("query")
        count = min(arguments.get("count", 5), 20)  # Enforce max limit

        _LOGGER.debug(f"SearXNG Search: '{query}' (count: {count})")

        params = {
            "q": query,
            "format": "json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.base_url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        error = await response.text()
                        _LOGGER.error(f"SearXNG Search error {response.status}: {error}")
                        return {
                            "content": [{
                                "type": "text",
                                "text": f"❌ Search failed (HTTP {response.status}): {error[:200]}"
                            }]
                        }

                    data = await response.json()

                    results_data = data.get("results")
                    if results_data is None:
                        results_data = []

                    results = []
                    for item in results_data[:count]:
                        if not isinstance(item, dict):
                            continue
                        title = item.get("title","")
                        url = item.get("url", "")
                        description = item.get("content","")
                        results.append({
                            "title": title,
                            "url": url,
                            "description": description,
                        })

                    text_results = f"🔍 Search results for '{query}':\n\n"
                    if results:
                        for i, result in enumerate(results, 1):
                            text_results += f"{i}. **{result['title']}**\n"
                            text_results += f"   {result['url']}\n"
                            text_results += f"   {result['description']}\n\n"
                    else:
                        text_results += "No search results were returned.\n\n"

                    return {
                        "content": [{
                            "type": "text",
                            "text": text_results
                        }]
                    }

        except asyncio.TimeoutError:  # Fix: Correct exception
            _LOGGER.error("SearXNG Search timeout")
            return {
                "content": [{
                    "type": "text",
                    "text": "❌ Search timeout - please try again"
                }]
            }
        except Exception as e:
            _LOGGER.error(f"SearXNG Search exception: {e}")
            return {
                "content": [{
                    "type": "text",
                    "text": f"❌ Search error: {str(e)}"
                }]
            }


from .base import Tool, ToolInstance, apply_prefix, strip_prefix
import voluptuous as vol


class _Registered(Tool):

    id = "searxng"
    name = "SearXNG Search"
    description = "Search the web with your own SearXNG server. Add another instance for a second URL."
    shared = True
    allow_multiple = True
    secrets = frozenset()
    config_schema = vol.Schema({vol.Required("url", default=""): str})

    async def async_setup(self, hass, instance: ToolInstance) -> None:
        runtime = SearXNGSearchTool(hass, (instance.config or {}).get("url") or "")
        # An empty URL used to raise during initialize and still leave the tool
        # registered. Keep the tool listed; calls fail until a URL is set.
        try:
            await runtime.initialize()
        except ValueError:
            pass
        instance.runtime = runtime

    async def async_teardown(self, hass, instance: ToolInstance) -> None:
        instance.runtime = None

    def definitions(self, hass, instance: ToolInstance):
        runtime = instance.runtime or SearXNGSearchTool(hass, (instance.config or {}).get("url") or "")
        return apply_prefix(instance, runtime.get_tool_definitions())

    async def async_call(self, hass, instance, tool_name, arguments):
        return await instance.runtime.handle_call(strip_prefix(instance, tool_name), arguments)


TOOL = _Registered()
