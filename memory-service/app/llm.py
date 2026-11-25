from __future__ import annotations

import json

import httpx

from .settings import settings

CHAT_URL = "https://api.openai.com/v1/chat/completions"


def chat_json(system: str, user: str, schema_name: str, schema: dict, timeout: float = 40.0) -> dict:
    """One chat completion constrained to a strict JSON schema."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    body = {
        "model": settings.openai_llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    if settings.openai_reasoning_effort:
        body["reasoning_effort"] = settings.openai_reasoning_effort
    resp = httpx.post(
        CHAT_URL,
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        json=body,
        timeout=timeout,
    )
    resp.raise_for_status()
    return json.loads(resp.json()["choices"][0]["message"]["content"])
