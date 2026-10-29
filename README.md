Project Kestrel

# Kestrel Ticket Desk: Ticket Intelligence System

Classifies incoming customer support messages and extracts order ID, product, urgency and requested
action using a local open-source LLM (any OpenAI-compatible endpoint: the provided qwen3-4b, Ollama, llama.cpp, vLLM).
Built to survive LLM and storage outages without losing, duplicating or repeating work.

## Architecture

```mermaid
flowchart LR
    S[Message sender] -- "POST /messages" --> API[FastAPI ingest<br/>validate + dedupe]
    API -- "INSERT ON CONFLICT DO NOTHING" --> DB[(PostgreSQL<br/>messages table = queue)]
    API -. "storage down" .-> SP[[Local spool file<br/>fsync'd JSONL]]
    SP -. "auto replay when storage is back" .-> DB
    DB -- "claim: FOR UPDATE SKIP LOCKED" --> W[Single worker<br/>one LLM call at a time]
    W -- "call 1: classify (category)" --> LLM[Local LLM<br/>OpenAI-compatible API]
    W -- "call 2: extract (order, product,<br/>urgency, action)" --> LLM
    W -- "save category after call 1,<br/>details after call 2" --> DB
    W -. "storage down after LLM success" .-> SP
    M[Health monitor] -. "ping every 3s" .-> LLM
    M -. "SELECT 1" .-> DB
    UI[Ops screen<br/>polls /api/state every 1s] --> API
```

Each message gets two independent LLM calls: classification, then extraction. The category is saved
as soon as call 1 succeeds, so a failed extraction never re-runs classification.

Message lifecycle: `queued → processing → completed`, or `→ queued` again on a bad LLM answer
(back-off 2s, 4s), `→ failed` after 3 attempts. Empty or invalid text goes straight to `rejected`.
If the LLM is unreachable, the worker pauses and no attempts are used. Design choices and trade-offs are in
[DECISIONS.md](DECISIONS.md).

## Run it (Docker, recommended)

Prerequisites: Docker with Compose.

```bash
cp .env.example .env        # then set LLM_BASE_URL and LLM_API_KEY
docker compose up -d --build
```

Windows PowerShell (one command per line):

```powershell
Copy-Item .env.example .env          # first time only, then edit LLM_BASE_URL
docker compose up -d --build         # builds and starts db, ingest, worker, ops
docker compose ps                    # all four should say (healthy) after ~20 s
docker compose logs -f worker        # watch LLM processing; Ctrl+C to stop watching
docker compose down                  # stop everything (data stays in the volumes)
```

Demo that ingest keeps accepting while other parts are down:

```powershell
docker compose stop worker           # messages still get 202 and wait in the queue
docker compose start worker          # the backlog drains
docker compose stop db               # messages still get 202, spooled to disk ("Waiting on disk")
docker compose start db              # the spool replays into Postgres within a few seconds
```

- Ops screen: http://localhost:8001 (separate `ops` service; also serves `/api/state`, `/api/results`, `/api/llm-audit`)
- Point the message sender at: `http://<this-machine>:8000/messages` (the `ingest` service; JSON `POST`, see `input_schema.json`)
- Services: `ingest` (accepts messages only), `worker` (the single LLM caller), `ops` (screen + API), `db`. Same image, `ROLE` env var. Without Docker, one process runs all three (`ROLE=all`, the default).

## Run it without Docker

Prerequisites: Python 3.11+, a reachable PostgreSQL 14+.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
cp .env.example .env                           # edit values
set -a; . ./.env; set +a                       # Windows PowerShell: set the variables by hand
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

## Switch to a new, empty storage location

Change only `DATABASE_URL` (or `POSTGRES_DB` with Docker) and restart. Tables, and the database
itself if it does not exist, are created automatically.

## Get results.json

```bash
curl http://localhost:8001/api/results -o results.json   # without Docker: port 8000
```

One entry per message: `message_id, status, category, order_id, product, urgency, requested_action`.

## Operations

| What | How |
|---|---|
| Live status, totals, health alerts | http://localhost:8001 (refreshes itself every second) |
| Retry one failed message | "Retry" button, or `POST /api/retry/{message_id}` |
| Retry all failed | "Retry all failed" button, or `POST /api/retry-failed` |

If `OPS_TOKEN` is set, retry calls need header `X-Ops-Token` (the screen asks for it once).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `http://localhost:11434` | OpenAI-compatible server (no `/v1`) |
| `LLM_MODEL` | `qwen3-4b` | Model name |
| `LLM_API_KEY` | empty | Sent as `Bearer` token; never logged |
| `LLM_TIMEOUT` | `120` | Seconds per LLM call |
| `DATABASE_URL` | `postgresql://tickets:tickets@localhost:5432/tickets` | Storage |
| `SPOOL_DIR` | `spool` | Where messages wait while storage is down |
| `OPS_TOKEN` | empty | Protects retry endpoints |

## Tests

```bash
pip install pytest && python -m pytest -q
```

Offline end-to-end: `uvicorn tests.fake_llm:app --port 9000` and set `LLM_BASE_URL=http://localhost:9000`.
