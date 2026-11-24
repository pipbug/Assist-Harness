# Memory

Self-hosted long-term memory for Kira and her agents. Replaces Mem0 for Kestrel (Phase 2).

- API and mobile-first web UI, served together on `127.0.0.1:8890`
- Web UI: on the same port; put it behind a reverse proxy (tailnet-only is recommended)
- Stack and Postgres data: the compose project directory (bind mounts, no named volumes)
- Secrets: `secrets/memory.env` in the compose project directory

The UI password is `MEMORY_UI_PASSWORD` in that env file. API clients send `X-API-Key` (`MEMORY_API_KEY`).

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/memories` | `user_id`, `category`, `tag`, `q`, `limit`, `offset`; returns `results` and `total` |
| POST | `/memories` | verbatim `{text, category?, tags?, source?, agent_id?, metadata?, dedup?}` returns the memory plus `event`; or `{messages, user_id, metadata.source?, infer?}` extracts facts and returns `{results: [{id, memory, event, previous_memory?}]}` |
| GET, PUT, DELETE | `/memories/{id}` | |
| POST | `/search` | `{query, user_id?, top_k?, category?, tags?, threshold?}`, or Mem0-style `filters` and `limit`; cosine similarity in `score` |
| GET, POST | `/categories` | `{label, slug?, sort_order?}` |
| PUT, DELETE | `/categories/{slug}` | deleting a category leaves its memories uncategorised |

`source` is one of `voice`, `manual`, `winston`, `claude`, `import` (or `metadata.source`, as Mem0 clients send it).
Results also carry `memory` (same as `text`), so Mem0 clients can read them unchanged.

## Extraction and dedup (Phase 1)

- `{messages}` turns go to `gpt-5.4-nano` with the policy in
  `config/extraction_policy.txt` in the compose project directory (mounted read-only, re-read on every request,
  so edits apply without a rebuild). The model also picks a category from the current list.
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

## Build and deploy

```sh
cd /path/to/memory-stack
docker compose build memory-api && docker compose up -d --no-build --no-deps memory-api
```

## Roadmap

- Phase 2: point Assist Harness `tools/mem0_memory.py` here
- Phase 3: per-agent keys and a non-Tailscale API path for Winston and Claude
