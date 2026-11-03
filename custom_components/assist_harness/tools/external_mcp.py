"""Proxy tools from another MCP server (streamable HTTP or SSE)."""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urljoin

import aiohttp
import voluptuous as vol
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base import Tool, ToolInstance, apply_prefix, strip_prefix

_LOGGER = logging.getLogger(__name__)

_CONNECT_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=2)
_CALL_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=2)


def _parse_headers(text: str, bearer: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.strip()
        if name:
            headers[name] = value.strip()
    token = (bearer or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class _Client:
    """Minimal MCP client. A dead server leaves ``tools`` empty."""

    def __init__(self, hass, url: str, transport: str, headers: dict[str, str]) -> None:
        self.hass = hass
        self.url = url
        self.transport = transport if transport in ("streamable_http", "sse") else "streamable_http"
        self.headers = headers
        self.tools: list[dict[str, Any]] = []
        self.connected = False
        self.session_id: str | None = None
        self._warned = False
        self._sse_task = None
        self._sse_response = None
        self._endpoint: str | None = None
        self._pending: dict[Any, Any] = {}
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def close(self) -> None:
        self.connected = False
        task = self._sse_task
        self._sse_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except Exception:
                pass
        response = self._sse_response
        self._sse_response = None
        if response is not None and not response.closed:
            response.close()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def connect(self) -> None:
        await self.close()
        self.connected = False
        self.session_id = None
        if self.transport == "sse":
            await self._connect_sse()
        else:
            await self._rpc(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "assist-harness", "version": "0.20.0"},
                    },
                }
            )
            await self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.connected = True

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc(
            {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": {}}
        )
        tools = []
        if isinstance(result, dict):
            tools = [item for item in result.get("tools") or [] if isinstance(item, dict)]
        self.tools = tools
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._rpc(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if isinstance(result, dict) and "content" in result:
            return result
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    async def _connect_sse(self) -> None:
        import asyncio

        session = async_get_clientsession(self.hass)
        headers = {
            "Accept": "text/event-stream",
            **self.headers,
        }
        response = await session.get(self.url, headers=headers, timeout=_CONNECT_TIMEOUT)
        if response.status >= 400:
            response.close()
            raise RuntimeError(f"SSE HTTP {response.status}")
        self._sse_response = response
        self._endpoint_ready = asyncio.Event()
        self._sse_task = asyncio.create_task(self._read_sse(response))
        await asyncio.wait_for(self._endpoint_ready.wait(), timeout=8)
        await self._rpc(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "assist-harness", "version": "0.20.0"},
                },
            }
        )
        await self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _read_sse(self, response: aiohttp.ClientResponse) -> None:
        event = "message"
        data_lines: list[str] = []
        try:
            async for raw in response.content:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    await self._dispatch_sse(event, "\n".join(data_lines))
                    event = "message"
                    data_lines = []
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
        except Exception as err:
            self.connected = False
            _LOGGER.debug("External MCP SSE closed: %s", err)

    async def _dispatch_sse(self, event: str, data: str) -> None:
        if event == "endpoint" and data:
            self._endpoint = urljoin(self.url, data.strip())
            ready = getattr(self, "_endpoint_ready", None)
            if ready is not None:
                ready.set()
            return
        if not data:
            return
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            return
        future = self._pending.get(message.get("id"))
        if future is not None and not future.done():
            future.set_result(message)

    async def _rpc(self, payload: dict[str, Any]) -> Any:
        import asyncio

        if self.transport == "sse":
            return await self._rpc_sse(payload)
        session = async_get_clientsession(self.hass)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        timeout = _CALL_TIMEOUT if payload.get("method") == "tools/call" else _CONNECT_TIMEOUT
        async with session.post(self.url, json=payload, headers=headers, timeout=timeout) as response:
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self.session_id = session_id
            if payload.get("id") is None or response.status == 202:
                if response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")
                return None
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"HTTP {response.status}: {body[:200]}")
            content_type = response.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                message = await _json_from_sse(response, payload.get("id"))
            else:
                message = await response.json(content_type=None)
        if isinstance(message, dict) and message.get("error"):
            raise RuntimeError(message["error"])
        if isinstance(message, dict):
            return message.get("result")
        return message

    async def _rpc_sse(self, payload: dict[str, Any]) -> Any:
        import asyncio

        if not self._endpoint:
            raise RuntimeError("SSE endpoint is not ready")
        loop = asyncio.get_running_loop()
        future = None
        if payload.get("id") is not None:
            future = loop.create_future()
            self._pending[payload["id"]] = future
        session = async_get_clientsession(self.hass)
        headers = {"Content-Type": "application/json", **self.headers}
        timeout = _CALL_TIMEOUT if payload.get("method") == "tools/call" else _CONNECT_TIMEOUT
        async with session.post(
            self._endpoint, json=payload, headers=headers, timeout=timeout
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise RuntimeError(f"HTTP {response.status}: {body[:200]}")
            if response.content_type == "application/json":
                message = await response.json(content_type=None)
                if isinstance(message, dict) and message.get("error"):
                    raise RuntimeError(message["error"])
                if isinstance(message, dict) and "result" in message:
                    return message.get("result")
        if future is None:
            return None
        message = await asyncio.wait_for(future, timeout=8)
        self._pending.pop(payload.get("id"), None)
        if isinstance(message, dict) and message.get("error"):
            raise RuntimeError(message["error"])
        if isinstance(message, dict):
            return message.get("result")
        return message


async def _json_from_sse(response: aiohttp.ClientResponse, message_id: Any) -> Any:
    """Read a single JSON-RPC response from a streamable-HTTP SSE body."""
    event = "message"
    data_lines: list[str] = []
    async for raw in response.content:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                try:
                    message = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    message = None
                if isinstance(message, dict) and message.get("id") == message_id:
                    return message
            event = "message"
            data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith("event:"):
            event = line[6:].strip()
        _ = event
    raise RuntimeError("SSE response ended before a matching message")


class ExternalMcpTool(Tool):
    """Connect to an external MCP server and proxy its tools."""

    id = "external_mcp"
    name = "External MCP server"
    description = (
        "Connect to another MCP server over streamable HTTP or SSE and proxy "
        "its tools. Several servers can be added. A tool-name prefix keeps "
        "their tools from colliding. If a server is down its tools are omitted "
        "until it reconnects; everything else keeps working."
    )
    shared = True
    allow_multiple = True
    secrets = frozenset({"bearer_token"})
    config_schema = vol.Schema(
        {
            vol.Required("url"): str,
            vol.Optional("transport", default="streamable_http"): vol.In(
                ["streamable_http", "sse"]
            ),
            vol.Optional("bearer_token", default=""): str,
            vol.Optional("headers", default=""): str,
        }
    )

    async def async_setup(self, hass, instance: ToolInstance) -> None:
        config = instance.config or {}
        client = _Client(
            hass,
            str(config.get("url") or "").strip(),
            str(config.get("transport") or "streamable_http"),
            _parse_headers(str(config.get("headers") or ""), str(config.get("bearer_token") or "")),
        )
        instance.runtime = client
        if not client.url:
            return
        try:
            await client.connect()
            await client.list_tools()
            client._warned = False
        except Exception as err:
            client.connected = False
            client.tools = []
            client._warned = True
            _LOGGER.warning(
                "External MCP %s is unavailable (%s). Its tools are omitted.",
                instance.instance_id,
                err,
            )

    async def async_teardown(self, hass, instance: ToolInstance) -> None:
        client = instance.runtime
        if client is not None:
            await client.close()
        instance.runtime = None

    async def async_definitions(self, hass, instance: ToolInstance) -> list[dict[str, Any]]:
        client: _Client | None = instance.runtime
        if client is None or not client.url:
            return []
        try:
            if not client.connected:
                await client.connect()
            tools = await client.list_tools()
            if client._warned:
                _LOGGER.info("External MCP %s reconnected", instance.instance_id)
            client._warned = False
        except Exception as err:
            client.connected = False
            client.tools = []
            if not client._warned:
                _LOGGER.warning(
                    "External MCP %s is unavailable (%s). Its tools are omitted.",
                    instance.instance_id,
                    err,
                )
                client._warned = True
            try:
                await client.connect()
                tools = await client.list_tools()
            except Exception:
                return []
            client._warned = False
            client.connected = True
        return apply_prefix(instance, tools)

    async def async_call(self, hass, instance, tool_name, arguments):
        client: _Client | None = instance.runtime
        if client is None:
            return {"content": [{"type": "text", "text": "❌ External MCP server is not configured."}]}
        name = strip_prefix(instance, tool_name)
        try:
            if not client.connected:
                await client.connect()
                await client.list_tools()
            return await client.call(name, arguments)
        except Exception as err:
            client.connected = False
            _LOGGER.warning("External MCP call %s failed: %s", tool_name, err)
            try:
                await client.connect()
                await client.list_tools()
                return await client.call(name, arguments)
            except Exception as retry_err:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"❌ External MCP server is unavailable: {retry_err}",
                        }
                    ],
                    "isError": True,
                }


TOOL = ExternalMcpTool()
