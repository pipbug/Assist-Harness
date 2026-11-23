# Memory

Self-hosted long-term memory for Kira and her agents (Phase 0).

- API and mobile-first web UI, served together on `127.0.0.1:8890`
- Web UI: on the same port; put it behind a reverse proxy (tailnet-only is recommended)
- Stack and Postgres data: the compose project directory (bind mounts, no named volumes)
- Secrets: `secrets/memory.env` in the compose project directory

The UI password is `MEMORY_UI_PASSWORD` in that env file. API clients send `X-API-Key` (`MEMORY_API_KEY`).
