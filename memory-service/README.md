# Memory service

Self-hosted long-term memory behind Assist Harness's `memory` tool (`recall_memory`, `save_memory`, and the
background save of each finished turn). One small FastAPI app with Postgres + pgvector, OpenAI embeddings and
fact extraction, and a portrait-first web UI for reading and editing memories, categories and tags.

- API and web UI are served together on one port (`127.0.0.1:8890` in the example compose file).
  Put the UI behind a reverse proxy; tailnet-only is recommended.
- All data lives in bind mounts in the compose project directory (no named volumes).
- Secrets go in `secrets/memory.env` there (see `memory.env.example`). The UI password is
  `MEMORY_UI_PASSWORD`; API clients send `X-API-Key: <MEMORY_API_KEY>`.

In Home Assistant, add the **Memory** tool on the Assist Harness system entry with this service's URL and API key.

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/memories` | `user_id`, `category`, `tag`, `q`, `limit`, `offset`; returns `results` and `total` |
| POST | `/memories` | verbatim `{text, category?, tags?, source?, agent_id?, metadata?, dedup?}` returns the memory plus `event`; or `{messages, user_id, metadata.source?, infer?}` extracts facts and returns `{results: [{id, memory, event, previous_memory?}]}` |
| GET, PUT, DELETE | `/memories/{id}` | |
| POST | `/search` | `{query, user_id?, top_k?, category?, tags?, threshold?}`, or Mem0-style `filters` and `limit`; cosine similarity in `score` |
| GET, POST | `/categories` | `{label, slug?, sort_order?}` |
| PUT, DELETE | `/categories/{slug}` | deleting a category leaves its memories uncategorised |

`source` is one of `voice`, `manual`, `winston`, `claude`, `import`.
For older Mem0-style clients the API also accepts `metadata.source`, `filters` and `limit`, and results carry
`memory` (same as `text`).

## Extraction and dedup

- `{messages}` turns go to `gpt-5.4-nano` with the policy in `config/extraction_policy.txt` in the compose
  project directory (mounted read-only and re-read on every request, so edits apply without a rebuild; a short
  built-in policy is used if the file is missing). The model also picks a category from the current list.
- Every API write (extracted or verbatim) is checked against the user's existing memories: an exact duplicate
  (`content_hash`) or cosine similarity >= 0.99 is a NOOP. Neighbours >= 0.70 go to the LLM, which picks ADD,
  UPDATE (rewrite the existing row with merged or corrected text) or NOOP. The old wording is kept in
  `metadata.history` and shown in the UI under "Earlier wording".
- Don't lower `DEDUP_NOOP` much: "lights at 40 %" vs "30 %" scores ~0.97, and only the LLM catches that it is a correction.
- Writes are serialised per user with a Postgres advisory lock. UI adds skip near-duplicate merging.
- A background thread embeds any memory that was saved without a vector (every `BACKFILL_INTERVAL_S`, default 600).

Tunables (env): `OPENAI_LLM_MODEL`, `OPENAI_REASONING_EFFORT`, `DEDUP_NOOP`, `DEDUP_CANDIDATE`, `BACKFILL_INTERVAL_S`.

## UI

`/` lists memories (search, category chips, tag links), `/new` adds, `/m/{id}` edits or deletes, `/c` manages categories.

## Deploy

Copy `compose.example.yaml` to a stack directory as `compose.yaml`, set `build:` to this folder, create
`secrets/memory.env` from `memory.env.example` (mode 600) and, optionally, `config/extraction_policy.txt`. Then:

```sh
docker compose build memory-api && docker compose up -d
```

Rebuild and recreate only the API after a code change:

```sh
docker compose build memory-api && docker compose up -d --no-build --no-deps memory-api
```

## Possible next step

Per-agent API keys (the `agent_id` column and the `winston`/`claude` sources are already there) and an
authenticated path for agents that can't reach the tailnet.
