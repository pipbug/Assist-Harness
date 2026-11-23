from __future__ import annotations

import hashlib

import httpx

from .settings import settings

EMBED_URL = "https://api.openai.com/v1/embeddings"


def content_hash(user_id: str, text: str) -> str:
    normalised = " ".join((text or "").split()).strip().lower()
    payload = f"{user_id}\n{normalised}".encode()
    return hashlib.sha256(payload).hexdigest()


def embed_text(text: str) -> list[float]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    resp = httpx.post(
        EMBED_URL,
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={"model": settings.openai_embed_model, "input": text},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"][0]["embedding"]
