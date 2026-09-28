# TODOS

- **Alembic migrations** — What: versioned schema migrations. Why: `create_all` can't evolve a live schema. Depends on: first schema change after launch.
- **Auth / multi-tenant uploads** — What: user accounts owning uploads. Why: demo relies on unguessable UUIDs only. Depends on: nothing.
- **Stream agent steps (SSE)** — What: push each tool call to the UI as it runs. Why: multi-step answers take 5-20 s. Depends on: agent loop emitting events.
