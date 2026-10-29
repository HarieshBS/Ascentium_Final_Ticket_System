"""Ticket service: HTTP ingest, worker threads, ops API. Postgres is the queue and the source of truth."""
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

import llm

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://tickets:tickets@localhost:5432/tickets")
# all = everything in one process; ingest / worker / ops = one compose service each, same image.
ROLE = os.environ.get("ROLE", "all")
# Each service owns its spool file in the shared dir, so no two processes rewrite the same file.
SPOOL = Path(os.environ.get("SPOOL_DIR", "spool")) / os.environ.get("SPOOL_FILE", "spool.jsonl")
BAD_SPOOL = SPOOL.with_suffix(".bad")  # unreadable spool lines (torn by a crash mid-write), kept for ops
MAX_ATTEMPTS = 3
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "300"))
OPS_TOKEN = os.environ.get("OPS_TOKEN", "")
MAX_BODY = 1_000_000
MAX_ID_LEN = 128

log = logging.getLogger("tickets")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

health = {"llm": False, "storage": False}
spool_lock = threading.Lock()
spooled_results: set[str] = set()  # ids whose LLM result is on disk waiting for storage: never re-call the LLM for them
last_state: dict = {}
stop = threading.Event()

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id       TEXT PRIMARY KEY,
    text             TEXT,
    status           TEXT NOT NULL,
    category         TEXT,
    order_id         TEXT,
    product          TEXT,
    urgency          TEXT,
    requested_action TEXT,
    attempts         INT NOT NULL DEFAULT 0,
    failure_reason   TEXT,
    duplicates       INT NOT NULL DEFAULT 0,
    llm_calls        INT NOT NULL DEFAULT 0,
    llm_repeats      INT NOT NULL DEFAULT 0,
    llm_raw          TEXT,
    not_before       TIMESTAMPTZ,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_status ON messages (status, received_at);
CREATE TABLE IF NOT EXISTS llm_audit (
    id          TEXT PRIMARY KEY,
    message_id  TEXT NOT NULL,
    stage       TEXT NOT NULL,
    model       TEXT,
    prompt_hash TEXT,
    raw_output  TEXT,
    latency_ms  INT,
    outcome     TEXT NOT NULL,   -- passed | corrected | rejected | unavailable
    reason      TEXT,
    fields      TEXT[] NOT NULL DEFAULT '{}',   -- fields code had to correct
    created_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS llm_audit_created ON llm_audit (created_at);
ALTER TABLE llm_audit ADD COLUMN IF NOT EXISTS checks JSONB NOT NULL DEFAULT '{}';  -- layer -> pass | fail
"""
# Hand-checked answers for the practice set, scored live against what the pipeline stored.
GOLDEN_FILE = Path(__file__).with_name("golden.json")
GOLDEN = {g["message_id"]: g for g in json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))} if GOLDEN_FILE.exists() else {}
GOLDEN_FIELDS = ("category", "order_id", "product", "urgency")


def db():
    return psycopg.connect(DATABASE_URL, connect_timeout=3, autocommit=True, row_factory=dict_row)


def ensure_schema():
    try:
        conn = db()
    except psycopg.OperationalError as e:
        if "does not exist" not in str(e):
            raise
        # New empty storage location: create the database itself so switching is config-only.
        params = conninfo_to_dict(DATABASE_URL)
        with psycopg.connect(**{**params, "dbname": "postgres"}, connect_timeout=3, autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(params["dbname"])))
        conn = db()
    with conn:
        conn.execute(SCHEMA)


WORKER_LOCK = 4242  # Postgres advisory lock key: at most one worker processes at a time (4 GB VRAM)


def take_worker_lock():
    """Session lock on its own connection, held for the worker's lifetime. None if another worker holds it."""
    conn = db()
    if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (WORKER_LOCK,)).fetchone()["ok"]:
        conn.close()
        return None
    # We are the only worker, so anything still 'processing' was orphaned by a dead one.
    conn.execute("UPDATE messages SET status = 'queued', not_before = NULL WHERE status = 'processing'")
    return conn


# ---------- spool: local, fsynced fallback when storage is down ----------

HOLDS_BACK = ("class", "result")  # spooled LLM results: the message must not be re-processed until replayed


def spool(record: dict):
    with spool_lock:
        SPOOL.parent.mkdir(parents=True, exist_ok=True)
        with SPOOL.open("a+b") as f:
            if f.seek(0, 2):  # never glue a new record onto a torn last line left by a crash
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write((json.dumps(record) + "\n").encode())
            f.flush()
            os.fsync(f.fileno())
        if record["kind"] in HOLDS_BACK:
            spooled_results.add(record["message_id"])


def replay_spool():
    """Push spooled messages/results into storage. Safe to run twice: every write is idempotent."""
    with spool_lock:
        if not SPOOL.exists():
            return
        lines = read_lines(SPOOL)
        done, bad = 0, []
        try:
            for line in lines:
                try:
                    rec = json.loads(line)
                    mid, kind = rec["message_id"], rec["kind"]
                    args = {"msg": lambda: (mid, rec["text"]), "class": lambda: (mid, rec["category"], rec["calls"]),
                            "result": lambda: (mid, rec["fields"], rec["calls"]), "audit": lambda: (rec["audit"],)}[kind]()
                except (ValueError, TypeError, KeyError):
                    bad.append(line)  # quarantined below; must not block the lines after it
                    done += 1
                    continue
                {"msg": store_message, "class": save_classification, "result": save_result,
                 "audit": insert_audit}[kind](*args)
                done += 1
        finally:
            if bad:
                # Written before the spool shrinks. ponytail: a crash in between quarantines the line twice; harmless.
                with BAD_SPOOL.open("a", encoding="utf-8") as f:
                    f.write("".join(l + "\n" for l in bad))
                    f.flush()
                    os.fsync(f.fileno())
                log.error("quarantined %d unreadable spool line(s) to %s", len(bad), BAD_SPOOL)
            rest = lines[done:]
            tmp = SPOOL.with_suffix(".tmp")
            tmp.write_text("".join(l + "\n" for l in rest), encoding="utf-8")
            os.replace(tmp, SPOOL)
            spooled_results.clear()
            load_spooled_results()  # ids still waiting on disk stay held back from the queue
    if done:
        log.info("replayed %d spooled records", done)


def read_lines(path: Path) -> list[str]:
    # errors="replace": a torn write can end mid-character
    return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []


def spool_size() -> int:
    """Records waiting on disk across every service's spool file."""
    with spool_lock:
        return sum(len(read_lines(p)) for p in SPOOL.parent.glob("*.jsonl"))


def load_spooled_results():
    for line in read_lines(SPOOL):
        try:
            rec = json.loads(line)
            if rec["kind"] in HOLDS_BACK:
                spooled_results.add(rec["message_id"])
        except (ValueError, TypeError, KeyError):
            pass  # torn line: replay_spool quarantines it


ID_IN_LINE = re.compile(r'"message_id": "((?:[^"\\]|\\.)*)"')


def quarantined() -> list[dict]:
    """Quarantined lines with the message_id salvaged where it survived (it is written before the text)."""
    with spool_lock:
        lines = [l for p in sorted(SPOOL.parent.glob("*.bad")) for l in read_lines(p)]
    out = []
    for line in lines:
        m = ID_IN_LINE.search(line)
        try:
            mid = json.loads(f'"{m[1]}"') if m else None
        except ValueError:
            mid = None
        out.append({"message_id": mid, "preview": line[:120]})
    return out


# ---------- storage operations ----------

def store_message(message_id: str, text) -> bool:
    """Insert once; a repeat only bumps the duplicate counter. Returns True if new."""
    if not isinstance(text, str):
        status, reason = "rejected", "text is not a string"
    elif not text.strip():
        status, reason = "rejected", "empty message"
    else:
        status, reason = "queued", None
    with db() as conn:
        row = conn.execute(
            """INSERT INTO messages (message_id, text, status, failure_reason) VALUES (%s, %s, %s, %s)
               ON CONFLICT (message_id) DO UPDATE SET duplicates = messages.duplicates + 1
               RETURNING (xmax = 0) AS inserted""",
            (message_id, text if isinstance(text, str) else None, status, reason)).fetchone()
    return row["inserted"]


def claim():
    with db() as conn:
        return conn.execute(
            """UPDATE messages SET status = 'processing', not_before = now() + %s * interval '1 second', updated_at = now()
               WHERE message_id = (
                   SELECT message_id FROM messages
                   WHERE ((status = 'queued' AND (not_before IS NULL OR not_before <= now()))
                       OR (status = 'processing' AND not_before < now()))   -- lease expired: worker died
                     AND NOT (message_id = ANY(%s))
                   ORDER BY attempts, received_at   -- retries go behind fresh messages
                   LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING message_id, text, attempts, category""",
            (LEASE_SECONDS, list(spooled_results))).fetchone()


def save_classification(message_id, category, calls):
    with db() as conn:
        conn.execute(
            """UPDATE messages SET llm_repeats = llm_repeats + (category IS NOT NULL)::int, category = %s,
                   llm_calls = llm_calls + %s, updated_at = now() WHERE message_id = %s""",
            (category, calls, message_id))


def save_result(message_id, fields, calls):
    with db() as conn:
        conn.execute(
            """UPDATE messages SET status = 'completed', category = %(category)s, order_id = %(order_id)s,
                   product = %(product)s, urgency = %(urgency)s, requested_action = %(requested_action)s,
                   llm_raw = %(raw)s, attempts = attempts + 1, failure_reason = NULL,
                   llm_calls = llm_calls + %(calls)s, updated_at = now()
               WHERE message_id = %(id)s AND status <> 'completed'""",
            {**fields, "id": message_id, "calls": calls})


def record_failure(message_id, reason, attempts, calls):
    final = attempts >= MAX_ATTEMPTS
    with db() as conn:
        conn.execute(
            """UPDATE messages SET status = %s, attempts = %s, failure_reason = %s, llm_calls = llm_calls + %s,
                   not_before = now() + %s * interval '1 second', updated_at = now()
               WHERE message_id = %s""",
            ("failed" if final else "queued", attempts, reason, calls, 2 ** attempts, message_id))


def insert_audit(a: dict):
    with db() as conn:
        conn.execute(
            """INSERT INTO llm_audit (id, message_id, stage, model, prompt_hash, raw_output, latency_ms, outcome,
                                      reason, fields, checks, created_at)
               VALUES (%(id)s, %(message_id)s, %(stage)s, %(model)s, %(prompt_hash)s, %(raw)s, %(latency_ms)s,
                       %(outcome)s, %(reason)s, %(fields)s, %(checks)s, to_timestamp(%(at)s))
               ON CONFLICT (id) DO NOTHING""",  # replay may run twice
            {**a, "checks": Jsonb(a.get("checks") or {})})


def ask(fn, mid, text):
    """One LLM call plus its audit record, whatever the outcome."""
    llm.last.clear()
    try:
        return fn(text)
    finally:
        if llm.last:
            a = {**llm.last, "id": uuid.uuid4().hex, "message_id": mid}
            try:
                insert_audit(a)
            except psycopg.Error:
                health["storage"] = False
                spool({"kind": "audit", "message_id": mid, "audit": a})


def release(message_id):
    with db() as conn:
        conn.execute("UPDATE messages SET status = 'queued', not_before = NULL WHERE message_id = %s AND status = 'processing'",
                     (message_id,))


# ---------- background threads ----------

def worker():
    # One worker, one LLM call at a time: the local model has ~4 GB of VRAM and cannot serve parallel requests.
    lock, waiting = None, False
    while not stop.is_set():
        if not (health["llm"] and health["storage"]):
            time.sleep(1)
            continue
        try:
            if lock is not None:
                lock.execute("SELECT 1")  # a DB restart drops the session lock with the connection
        except psycopg.Error:
            lock = None
        if lock is None:
            try:
                lock = take_worker_lock()
            except psycopg.Error:
                pass
            if lock is None:
                if not waiting:
                    log.info("waiting for worker lock: another worker is running")
                waiting = True
                time.sleep(2)
                continue
            waiting = False
        try:
            row = claim()
        except psycopg.Error as e:
            log.error("claim failed: %s", e)
            health["storage"] = False
            time.sleep(1)
            continue
        if not row:
            time.sleep(0.5)
            continue
        mid, text, category = row["message_id"], row["text"], row["category"]
        calls, stage = 0, "classify"  # calls = LLM calls made but not yet written to storage
        try:
            if category is None:  # a saved classification is never redone, even after a failed extraction
                category, _ = ask(llm.classify, mid, text)
                calls += 1
                try:
                    save_classification(mid, category, calls)
                    calls = 0
                except psycopg.Error:
                    # Storage died between the two calls: keep the category on disk so it is never recomputed.
                    health["storage"] = False
                    spool({"kind": "class", "message_id": mid, "category": category, "calls": calls})
                    calls = 0
            stage = "extract"
            details, raw = ask(llm.extract, mid, text)
            calls += 1
        except llm.LLMUnavailable as e:
            log.warning("LLM unavailable (%s); pausing, %s goes back to queue without using an attempt", e, mid)
            health["llm"] = False
            try_db(release, mid)
            continue
        except llm.BadOutput as e:
            log.warning("%s attempt %d failed at %s: %s", mid, row["attempts"] + 1, stage, e)
            try_db(record_failure, mid, f"{stage}: {e}", row["attempts"] + 1, calls + 1)
            continue
        fields = {**details, "category": category, "raw": raw[:4000]}
        try:
            save_result(mid, fields, calls)
            log.info("%s completed: %s", mid, category)
        except psycopg.Error:
            health["storage"] = False
            spool({"kind": "result", "message_id": mid, "fields": fields, "calls": calls})
            log.warning("storage down; result for %s spooled to disk", mid)


def try_db(fn, *args):
    try:
        fn(*args)
    except psycopg.Error:
        health["storage"] = False  # the lease will expire and the message is retried


def monitor():
    schema_ok = False
    while not stop.is_set():
        if ROLE != "ingest":  # ingest never calls the LLM
            health["llm"] = llm.healthy()
        try:
            if not schema_ok:
                ensure_schema()
                schema_ok = True
            with db() as conn:
                conn.execute("SELECT 1")
            health["storage"] = True
            replay_spool()
        except (psycopg.Error, OSError) as e:
            if health["storage"]:
                log.error("storage unavailable: %s", type(e).__name__)
            health["storage"] = False
        stop.wait(3)


@asynccontextmanager
async def lifespan(_):
    load_spooled_results()
    for target in (monitor, worker) if ROLE in ("all", "worker") else (monitor,):
        threading.Thread(target=target, daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="Kestrel Ticket Desk", lifespan=lifespan)


@app.middleware("http")
async def only_my_routes(request: Request, call_next):
    """ingest serves only /messages, ops everything else, worker nothing but /healthz."""
    p = request.url.path
    if ROLE == "ingest" and p == "/":  # someone opened the ingest port in a browser
        return JSONResponse({"service": "ingest", "send": "POST /messages", "ops_screen": "port 8001"})
    if ROLE != "all" and p != "/healthz" and (ROLE == "worker" or (p == "/messages") != (ROLE == "ingest")):
        return JSONResponse({"detail": f"not served by the {ROLE} service"}, 404)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    """Process is up. Ingest stays healthy with storage down: it spools."""
    return {"role": ROLE, **health}


# ---------- HTTP ----------

@app.post("/messages", status_code=202)
async def receive(request: Request):
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise HTTPException(413, "message too large")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "message too large")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "body is not valid JSON")
    mid = payload.get("message_id") if isinstance(payload, dict) else None
    if not isinstance(mid, str) or not mid.strip() or len(mid) > MAX_ID_LEN:
        raise HTTPException(422, "message_id must be a non-empty string")
    # Anything with a usable id is kept (bad text becomes 'rejected') so no message disappears.
    text = payload.get("text")
    try:
        new = await run_in_threadpool(store_message, mid, text)
    except psycopg.Error:
        health["storage"] = False
        spool({"kind": "msg", "message_id": mid, "text": text})
        return {"message_id": mid, "accepted": True, "stored": "spool"}
    return {"message_id": mid, "accepted": True, "duplicate": not new}


def check_token(request: Request):
    if OPS_TOKEN and not hmac.compare_digest(request.headers.get("x-ops-token", ""), OPS_TOKEN):
        raise HTTPException(401, "ops token required")


@app.post("/api/retry/{message_id}")
def retry_one(message_id: str, request: Request):
    check_token(request)
    return {"requeued": requeue("AND message_id = %s", (message_id,))}


@app.post("/api/retry-failed")
def retry_all(request: Request):
    check_token(request)
    return {"requeued": requeue("", ())}


@app.post("/api/quarantine/clear")
def clear_quarantine(request: Request):
    """Ops has reviewed the quarantined lines. The file is renamed, never deleted."""
    check_token(request)
    n = 0
    with spool_lock:
        for p in SPOOL.parent.glob("*.bad"):
            n += len(read_lines(p))
            p.rename(p.with_name(f"{p.name}.{int(time.time())}.reviewed"))
    return {"cleared": n}


def requeue(where, params):
    try:
        with db() as conn:
            return conn.execute(
                f"""UPDATE messages SET status = 'queued', attempts = 0, failure_reason = NULL, not_before = NULL,
                       updated_at = now() WHERE status = 'failed' {where}""", params).rowcount
    except psycopg.Error:
        raise HTTPException(503, "storage unavailable, try again shortly")


@app.get("/api/state")
def state():
    global last_state
    bad = quarantined()
    out = {"health": dict(health), "spooled": spool_size(), "quarantined": bad, "stale": False}
    try:
        with db() as conn:
            if bad:  # did the sender resend it since? (a torn line was never acknowledged)
                known = {r["message_id"] for r in conn.execute(
                    "SELECT message_id FROM messages WHERE message_id = ANY(%s)",
                    ([b["message_id"] for b in bad if b["message_id"]],))}
                for b in bad:
                    b["in_storage"] = b["message_id"] in known
            t = conn.execute(
                """SELECT count(*) AS received,
                          count(*) FILTER (WHERE status IN ('queued', 'processing')) AS in_progress,
                          count(*) FILTER (WHERE status = 'completed') AS completed,
                          count(*) FILTER (WHERE status = 'failed') AS failed,
                          count(*) FILTER (WHERE status = 'rejected') AS rejected,
                          coalesce(sum(duplicates), 0) AS duplicates,
                          coalesce(sum(llm_calls), 0) AS llm_calls,
                          coalesce(sum(llm_repeats), 0) AS llm_repeats
                   FROM messages""").fetchone()
            rows = conn.execute(
                """SELECT message_id, status, category, order_id, product, urgency, requested_action,
                          attempts, failure_reason, duplicates, left(text, 160) AS text, updated_at
                   FROM messages ORDER BY received_at DESC LIMIT 500""").fetchall()
        last_state = {"totals": t, "messages": rows}
    except psycopg.Error:
        health["storage"] = False
        out["health"]["storage"] = False
        out["stale"] = True
    return JSONResponse(json.loads(json.dumps({**out, **last_state}, default=str)))


@app.get("/api/results")
def results():
    """The hand-in file: exactly one entry per message."""
    try:
        with db() as conn:
            return conn.execute(
                """SELECT message_id, status, category, order_id, product, urgency, requested_action
                   FROM messages ORDER BY message_id""").fetchall()
    except psycopg.Error:
        raise HTTPException(503, "storage unavailable")


@app.get("/api/llm-audit")
def llm_audit():
    """Credibility score: share of judged calls whose output was used exactly as the model gave it."""
    try:
        with db() as conn:
            counts = conn.execute(
                """SELECT stage, outcome, count(*) AS n, round(avg(latency_ms))::int AS avg_latency_ms
                   FROM llm_audit GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
            fields = conn.execute(
                """SELECT f AS field, count(*) AS n FROM llm_audit, unnest(fields) f
                   GROUP BY 1 ORDER BY 2 DESC""").fetchall()
            recent = conn.execute(
                """SELECT message_id, stage, model, prompt_hash, outcome, reason, latency_ms,
                          left(raw_output, 300) AS raw_output, created_at
                   FROM llm_audit WHERE outcome IN ('corrected', 'rejected')   -- outages are counted, not listed
                   ORDER BY created_at DESC LIMIT 50""").fetchall()
            outages = conn.execute(  # joined to the message's current status: was the call resolved by a retry?
                """SELECT a.message_id, a.stage, a.reason, a.created_at, m.status
                   FROM llm_audit a LEFT JOIN messages m USING (message_id)
                   WHERE a.outcome = 'unavailable' ORDER BY a.created_at DESC LIMIT 50""").fetchall()
            layers = conn.execute(
                """SELECT key AS layer, value AS result, count(*) AS n FROM llm_audit, jsonb_each_text(checks)
                   GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()
            stored = conn.execute(
                "SELECT message_id, category, order_id, product, urgency FROM messages WHERE message_id = ANY(%s)",
                (list(GOLDEN),)).fetchall()
    except psycopg.Error:
        raise HTTPException(503, "storage unavailable")
    return JSONResponse(json.loads(json.dumps(
        {"score": credibility(counts), "by_stage": {s: credibility([c for c in counts if c["stage"] == s])
                                                    for s in {c["stage"] for c in counts}},
         "layers": layers, "golden": golden_score(stored),
         "counts": counts, "corrected_fields": fields, "recent_issues": recent, "outages": outages}, default=str)))


def golden_score(stored) -> dict:
    """Per field: how many processed practice messages match the hand-checked answer."""
    done = [r for r in stored if r["category"] is not None]
    out = {"scored": len(done), "of": len(GOLDEN)}
    for f in GOLDEN_FIELDS:
        out[f] = sum(r[f] == GOLDEN[r["message_id"]][f] for r in done)
    out["misses"] = [f"{r['message_id']} {f}: {r[f]!r}, expected {GOLDEN[r['message_id']][f]!r}"
                     for r in done for f in GOLDEN_FIELDS if r[f] != GOLDEN[r["message_id"]][f]]
    return out


def credibility(counts) -> dict:
    # 'unavailable' is an outage, not the model's answer, so it is not judged.
    n = {o: sum(c["n"] for c in counts if c["outcome"] == o) for o in ("passed", "corrected", "rejected", "unavailable")}
    judged = n["passed"] + n["corrected"] + n["rejected"]
    return {**n, "judged": judged, "passed_unmodified": round(n["passed"] / judged, 3) if judged else None}


@app.get("/")
def index():
    return FileResponse(Path(__file__).with_name("index.html"))
