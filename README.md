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
| POST | `/memories` | `{text, category?, tags?, source?, agent_id?, metadata?}`; an exact duplicate returns 200 with `event: NOOP` |
| GET, PUT, DELETE | `/memories/{id}` | |
| POST | `/search` | `{query, user_id?, top_k?, category?, tags?}`; cosine similarity in `score` |
| GET, POST | `/categories` | `{label, slug?, sort_order?}` |
| PUT, DELETE | `/categories/{slug}` | deleting a category leaves its memories uncategorised |

`source` is one of `voice`, `manual`, `winston`, `claude`, `import`. Embeddings use OpenAI
`text-embedding-3-small`; if embedding fails, the memory is still saved with a NULL vector.

## UI

`/` lists memories (search, category chips, tag links), `/new` adds, `/m/{id}` edits or deletes, `/c` manages categories.

## Build and deploy

```sh
cd /path/to/memory-stack
docker compose build memory-api && docker compose up -d --no-build --no-deps memory-api
```

## Roadmap

- Phase 1: extraction from `{messages}` turns, near-duplicate merge, embedding backfill
- Phase 2: point Assist Harness `tools/mem0_memory.py` here
- Phase 3: per-agent keys and a non-Tailscale API path for Winston and Claude
