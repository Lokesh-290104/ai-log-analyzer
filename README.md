# AI Log Analyzer

[![CI](https://github.com/Lokesh-290104/ai-log-analyzer/actions/workflows/ci.yml/badge.svg)](https://github.com/Lokesh-290104/ai-log-analyzer/actions/workflows/ci.yml)

Ask plain-English questions about a log file. An LLM agent answers by **calling tools**:
deterministic Python computes every count, time and percentage, and code checks the final
answer before you see it. If the model's output can't be validated, you get an error, never
a guessed answer.

> **LLM as witness, code as judge.** The model picks tools and explains the results. It never
> does arithmetic. Every number in an answer must appear in a tool result the answer cites,
> or the answer is rejected.

**Live demo:** https://ai-log-analyzer.onrender.com (free tier: the first request after idle takes ~1 min to wake up)
· Click **Try the sample incident log**, then ask *"What happened between 10:40 and 10:50?"*

![Answer with verified numbers and the tool calls behind it](docs/screenshot-answer.png)

| Upload and overview (no AI involved) | |
|---|---|
| ![Overview: stat tiles, errors-over-time chart, top errors](docs/screenshot-overview.png) | ![Upload screen](docs/screenshot-upload.png) |

## What it does

1. **Upload or paste a log** (plain text, JSON lines or logfmt, up to 5 MB / 100k lines).
   The parser normalizes every entry to `(line, UTC time, level, service, message)`, attaches
   stack traces to their entry, and **reports** unrecognized lines instead of guessing.
2. **Overview, instantly and without an LLM:** totals, error rate, an errors-over-time chart,
   top error messages (ids and numbers normalized so `ORD-123` and `ORD-456` group together).
3. **Ask a question.** The agent chooses among 7 tools, sees their results, and answers.
   The UI shows the answer, a **Verified** badge listing every figure checked, and the full
   evidence trail: each tool call's arguments, the model's reason, and the exact result.

## How the agent works

```
 question ─► LLM ─► JSON reply ─► Pydantic AgentStep (discriminated union on "type")
              ▲          │  not JSON / wrong shape ─► repair turn (max 2) ─► still bad ─► FAIL
              │          ├─ tool_call ─► known tool? args valid (extra="forbid")? ─► no ─► repair turn
              │          │       └─► run tool (pure Python) ─► TOOL_RESULT id=N (as data) ─┐
              │          └─ final_answer {answer, evidence:[ids]}                          │
              │                  └─► judge: cited ids exist? every number/time in the      │
              │                      answer found in the cited results or the question?    │
              │                        ├─ no ─► repair turn ─► still no ─► FAIL             │
              │                        └─ yes ─► ANSWERED (+ verification details)          │
              └────────────────────────────────────────────────────────────────────────────┘
 Bounds: 6 model turns and a 90 s deadline per question; exceeding either fails closed.
```

- **Schemas as contracts.** The model must reply with one of two JSON shapes, validated by
  Pydantic. Every tool has its own Pydantic args model; unknown fields are rejected, levels
  are normalized, time windows must be valid ISO timestamps with `start < end`. The prompt's
  tool list is **generated from those models**, so what the model reads and what the code
  enforces can't drift. The HTTP API uses Pydantic request/response models too, mirrored
  in `frontend/src/types.ts`.
- **Code as judge** ([`backend/app/judge.py`](backend/app/judge.py)). Numbers are extracted
  from the answer and must match a value in the *cited* tool results (or the question).
  Rounding a fractional value to the precision written is allowed (`74.6` → `75%`);
  arithmetic is not (answering `193 − 144 = 49` is rejected unless a tool returned 49). Times are
  checked as whole units: `10:42` must match a real timestamp. They're never split into
  `10` and `42`, which would let any small number hide inside a timestamp.
- **Fail closed.** Invalid JSON, a hallucinated tool, bad args, or an unverifiable answer
  each get a limited repair turn with the exact validation error. When the budget runs out
  the API returns `status: "failed"` with the trace and **no answer text**.
- **Prompt injection.** Log lines are attacker-controlled text. Tool results are sent as
  labeled data ("not instructions"), and even if the model obeys a malicious line
  ("report zero errors"), the judge rejects the invented number (see
  `test_injection_in_log_data_cannot_change_numbers`).

### Tools ([`backend/app/tools.py`](backend/app/tools.py))

| Tool | Returns |
|---|---|
| `summary` | totals, time range, counts per level and service |
| `count_by` | counts + share % grouped by level or service, with filters |
| `top_messages` | most frequent message patterns, counts, share %, first/last seen |
| `find_occurrence` | first or last matching entry and the match count |
| `entries_in_window` | counts and samples between two timestamps |
| `search` | case-insensitive text search in messages and stack traces |
| `timeline` | counts and errors per 1/5/15/60-min bucket, and the peak |

All tools share filters (`level`, `service`, `start`, `end`), are pure functions over parsed
entries, and cap list sizes so large logs can't flood the prompt.

## Architecture

```
 React (Vite, TS, Recharts)          FastAPI (async)                          PostgreSQL
 ─────────────────────────           ───────────────                          ──────────
 upload / paste ── POST /api/uploads ─► size caps ─► parser ─► bulk insert ─► uploads, log_entries
 overview + chart ◄── overview JSON (computed by the same tools at upload time)
 ask ── POST /api/uploads/{id}/ask ─► rate limit ─► entries (LRU cache ◄─ DB) ─► agent ─► judge
 answer + trace ◄── stored analysis (question, status, answer, trace, verification) ─► analyses
                                      │
                                      └─► LLM: Gemini API (gemma-4-26b-a4b-it) → OpenRouter fallback
```

| Path | What's there |
|---|---|
| `backend/app/parser.py` | log text → `LogEntry`; JSON lines, logfmt, 4 text layouts, stack traces |
| `backend/app/tools.py` | the 7 deterministic tools and their Pydantic args models |
| `backend/app/agent.py` | agent loop, step schema, repair/fail-closed policy |
| `backend/app/judge.py` | evidence + number/time verification |
| `backend/app/prompts.py` | system prompt generated from the tool models |
| `backend/app/llm.py` | async Gemini/OpenRouter clients: retry, model failover, quota wait, provider chain; `FakeLLM` for tests |
| `backend/app/db.py` | SQLAlchemy 2 async models (asyncpg / aiosqlite), entry cache |
| `backend/app/main.py` | FastAPI routes, rate limits, upload caps, serves the built React app |
| `backend/eval/` | 12-question eval against the real model |
| `frontend/src/` | React app and Vitest tests |

Design choices worth calling out:

- **JSON tool protocol instead of native function calling.** Gemma on the Gemini API doesn't
  reliably support native tool calls, and a JSON protocol works identically across Gemini and
  OpenRouter models. Each step is validated by the same Pydantic schema regardless of provider.
- **Stats without the LLM.** The dashboard is computed at upload by the agent's own tools, so
  the chart and the agent can never disagree, and viewing a log costs no LLM quota.
- **Async end to end:** FastAPI, SQLAlchemy async sessions, `google-genai`'s async client and
  `httpx.AsyncClient`. A slow model call never blocks other requests.
- **Resilient on free tiers:** per-model retry on 5xx, failover across models, a short wait
  when every model hits a per-minute quota, then fallback to the next provider
  (`LLM_PROVIDER=gemini,openrouter`).
- **Public-demo safety:** per-IP and daily limits on questions and uploads (checked before any
  model call), 5 MB / 100k-line caps (checked from `Content-Length` before the body is read),
  unguessable UUID upload ids and no "list all uploads" endpoint.

## Tests and eval

```bash
cd backend && python -m pytest          # 162 tests, offline: FakeLLM + SQLite
cd frontend && npm test                 # Vitest + Testing Library
```

- **Backend:** parser formats and edge cases, every tool (including invalid args), judge rules
  (rounding, arithmetic, times, injection), the agent loop (repair, fail closed, step limit,
  deadline, provider errors), LLM failover/quota/chain logic, and the API (413/400/404/422/429/503,
  persistence, proxy-aware rate limiting, static serving). CI runs the API tests a second time
  against **PostgreSQL**.
- **Frontend:** upload → overview → ask → verified answer; the fail-closed view; loading, rate-limit,
  network and oversize-file errors; reopening from the URL.
- **Eval against the real model** (`python -m eval.run_eval`, needs a key): 12 questions
  including an off-topic question and a prompt-injection attempt. Current result with
  `gemma-4-26b-a4b-it`: **12/12**.

CI (GitHub Actions): backend tests on SQLite and Postgres, frontend lint + tests + build,
then a Docker build and smoke test of the production image.

## Run it locally

Requirements: Python 3.12+ (3.14 used), Node 20+. Postgres is optional (SQLite by default).

```bash
cp .env.example .env              # add GEMINI_API_KEY (free: https://aistudio.google.com/apikey)
cd backend
python -m venv .venv && .venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload     # API on http://localhost:8000
```

```bash
cd frontend
npm install
npm run dev                       # UI on http://localhost:5173 (proxies /api to :8000)
```

To use Postgres, set `DATABASE_URL=postgresql://user:pass@localhost:5432/logs` in `.env`.
Tables are created on startup.

Or run the production image (API and UI on one port):

```bash
docker build -t ai-log-analyzer .
docker run -p 8000:8000 --env-file .env ai-log-analyzer
```

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/uploads` | multipart `file` (+ optional `name`) → parse stats + overview |
| `POST` | `/api/uploads/sample` | load the built-in incident log |
| `GET` | `/api/uploads/{id}` | parse stats + overview |
| `POST` | `/api/uploads/{id}/ask` | `{"question": "..."}` → analysis (answer or fail-closed, trace, verification) |
| `GET` | `/api/uploads/{id}/analyses` | last 20 analyses |
| `GET` | `/api/health` | liveness + database check |

Interactive docs at `/docs`.

## Deploy (Render)

`render.yaml` is a Render Blueprint: one Docker web service (API + built UI) and a free
PostgreSQL database. **New → Blueprint → pick this repo**, then paste `GEMINI_API_KEY`
(and optionally `OPENROUTER_API_KEY`). Note: Render's free Postgres expires after 30 days;
the app recreates its tables on an empty database.

## Known limits

- The judge checks that every figure is *traceable* to cited evidence, not that the sentence
  reads it correctly (e.g. swapping two services' counts would pass). Numbers written as
  words ("three") aren't checked.
- Rate limits are in memory, per instance (enough for one free instance; several instances
  would need Redis).
- No accounts: anyone with an upload's UUID link can view it. Don't upload sensitive logs to
  the public demo.
- See [`TODOS.md`](TODOS.md) for planned work (migrations, auth, streaming agent steps).
