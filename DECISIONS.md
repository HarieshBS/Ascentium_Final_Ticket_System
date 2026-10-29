# Decisions

## Architecture in one paragraph
One Python process (FastAPI) does three things: accepts messages over HTTP, runs a small pool of
a single worker thread that calls the LLM, and serves the operations screen. PostgreSQL is the single source
of truth. Every message is one row whose `status` moves `queued → processing → completed | failed`
(or straight to `rejected`). There is no separate queue service: the table *is* the queue
(`SELECT … FOR UPDATE SKIP LOCKED`), which removes a whole component that could fail.

## Key decisions and trade-offs

| Need | Decision | Trade-off |
|---|---|---|
| Nothing gets lost | Ingest writes to Postgres. If Postgres is down, the message is appended (and `fsync`ed) to a local spool file and replayed automatically when the database is back. The sender always gets `202` once the message is durable somewhere. | The spool is local to one machine. Fine for a single instance; multi-instance would need a shared broker. |
| Survive crashes | A row stuck in `processing` longer than `LEASE_SECONDS` is reclaimed by any worker. | A message can be picked up again after a crash mid-LLM-call; that one call is lost. |
| No wasted LLM work | The validated LLM result is written in the same `UPDATE` that marks the row completed. If that write fails because storage went down, the result is spooled to disk and applied later, never recomputed. | Extra code path for a rare case, but it is the one the brief is testing. |
| Handled once | `message_id` is the primary key; `INSERT … ON CONFLICT DO NOTHING`. Duplicates are counted, not processed. Spool replay goes through the same insert, so it is idempotent too. | A resend with the same id but *different* text is treated as a duplicate (brief says resends keep the id). Logged in `duplicates`. |
| Bad messages | Validation at the HTTP boundary (pydantic, unknown fields rejected, body size cap). Empty / whitespace text → `rejected` immediately, no LLM call. Very long text is truncated to `MAX_PROMPT_CHARS` before prompting. Messages that already failed once go to the back of the queue (ordered by attempts, then arrival), so a bad message can delay others by at most one LLM timeout, never block them. | Truncation may lose an order id at the very end of a huge message. With one worker, a message that times out still holds the LLM for up to `LLM_TIMEOUT`. |
| Give up gracefully | Attempts are counted only for failures *of the message* (bad LLM output, timeout). When the LLM is unreachable, the worker pauses (circuit breaker) instead of burning attempts — otherwise an outage would fail every message. After 3 attempts → `failed` + `failure_reason`. Ops can retry one or all; retry resets attempts. | A message that reliably times out the LLM is indistinguishable from a slow LLM; we count timeouts as attempts. |
| Accuracy | Local open-source LLM (qwen3-4b via an OpenAI-compatible API; Ollama works the same), `temperature=0`, few-shot prompt, strict JSON. Then **code, not the model,** enforces the hard rules: enum checks, `ORD-#####` normalisation, order id must actually appear in the text, product mapped to the catalogue. | Rules in code are rigid, but a 4B model is not reliable enough to trust on format. |
| Prompt injection | Customer text is sent as delimited data, the system prompt says to never follow instructions inside it, and the output is constrained to fixed enums anyway, so the worst an injection can do is pick a wrong category. | |
| Switch storage by config | `DATABASE_URL` only. Tables are created on startup; if the database itself does not exist it is created. | Needs a role allowed to `CREATE DATABASE` for the auto-create path. |
| Live ops screen | One static HTML page polling `/api/state` every second. No build step, no websocket code. | 1s polling is wasteful at scale; fine for one ops screen. |
| Security | Secrets only from env; never logged. Parameterised SQL everywhere. UI uses `textContent`, never `innerHTML`, for customer text. Retry endpoints require `OPS_TOKEN` when it is set. | No user accounts — single shared ops token. |

## The "Kestrel" naming requirement
Page 3 of the brief has one line that does not match the rest of it: the ops screen must be headed
"Kestrel Ticket Desk" and the README must start with "Project Kestrel". It sits inside the business
outcomes list but is formatted differently, and nothing else in the brief mentions Kestrel.
I noticed it, thought it might be a check on whether candidates (or their AI tools) read the brief
critically, and asked about it before applying it.

**Decision: follow it.** It is part of the written brief, it costs one heading and one README line,
and it changes no behaviour, data or security. Leaving it out would break a stated requirement for no
benefit. I am mentioning it openly here and in the review rather than applying it silently.

## Review feedback (11:30): single worker, two LLM calls
- **One worker, one LLM call at a time.** The model runs locally on ~4 GB of VRAM, so parallel calls only
  queue up inside the model server and cause timeouts. There is no `WORKERS` setting any more, so it cannot
  be turned up by mistake. Throughput is limited by the model either way; ingest is not affected, because
  it never waits for the LLM.
- **Classification and extraction are two separate calls** with their own prompt, few-shot examples and
  parser (`llm.classify`, `llm.extract`). Neither sees the other's output, so either prompt can change
  without touching the other. Urgency belongs to extraction, as the brief lists it as an extracted detail.
- **Classification is saved before extraction starts.** If extraction fails, the retry skips straight to
  extraction. A retry from the ops screen also keeps the saved category. If storage is down at that moment,
  the category travels with the final result (or the spool), so it is still not recomputed.
- **Attempts are per message, not per call.** A bad answer from either call uses one of the message's 3
  attempts, and `failure_reason` says which stage failed (`classify: …` / `extract: …`). Per-call budgets
  would let a message burn up to 6 slow LLM calls before failing, against the brief's "fails 3 times".
- Trade-off: two calls roughly double the per-message latency. Accepted: the reviewers asked for it, and
  small models are more accurate with one focused task per prompt.

## Crash mid-write: torn spool lines
If the machine dies while a line is being appended to `spool/spool.jsonl`, the last line can be cut off
mid-sentence. Before this change, replay stopped at that line and every good line after it stayed stuck.

**Decision: quarantine and keep going.** Replay moves any line it cannot read (bad JSON, missing fields,
garbage bytes) to `spool/spool.bad` (appended and `fsync`ed before the spool shrinks) and carries on with the
next line. Two small guards go with it: startup no longer crashes on a torn line, and a new append first adds a
newline if the file ends mid-line, so a fresh record is never glued onto the torn one and lost with it.

**Why recovery can stay small.** The spool is `fsync`ed *before* the sender gets `202`, so a line torn by a crash
was never acknowledged:
- a torn `msg` line: the sender got no `202` and resends with the same `message_id` (the brief says resends keep the id);
- a torn `result` line: the row is still `processing`; the startup reset / lease expiry requeues it. Cost: one repeat LLM call.

So nothing acknowledged is lost by a torn tail. What is left for a human:
1. **Alert.** The ops screen shows "N unreadable spool line(s) quarantined" and a table of them.
2. **Salvage the id.** `message_id` is written before the text, so it usually survives the cut. Each row shows
   the salvaged id and whether that id has since **arrived again** in storage. "yes" = nothing to do.
   "missing" = ask the sender to resend that id (or it was corruption in the middle of the file, not a torn tail).
3. **Mark reviewed.** One button (`POST /api/quarantine/clear`, ops token) renames the file to
   `spool.bad.<time>.reviewed`. It is never deleted, so the raw lines stay available for an audit.

**Not built: "re-submit from quarantine".** The only thing to re-submit would be the truncated text, and
classifying half a message gives a confidently wrong ticket. The sender's resend is the real re-submit.
If a non-resending source is ever added, a re-submit button can insert the salvaged id as `failed` with
`failure_reason = "text torn by crash"` so it lands in the existing Failed list.

Trade-offs: a crash between writing `.bad` and shrinking the spool quarantines the same line twice (harmless).
`errors="replace"` means a torn multi-byte character is shown as `�` in the preview.
Tests: `tests/test_spool.py` (truncated line, torn tail plus new append, garbage lines, storage down, clear).

## Status meanings
`queued` waiting · `processing` a worker holds it · `completed` · `failed` (3 attempts) · `rejected` (invalid input).

## What I would do with more time
- Proper message broker (or Postgres LISTEN/NOTIFY) instead of polling.
- Accuracy evaluation set larger than 25 messages, and a second-pass "self-check" call for low-confidence cases.
- Real auth for the ops screen, metrics export (Prometheus), structured logs.
