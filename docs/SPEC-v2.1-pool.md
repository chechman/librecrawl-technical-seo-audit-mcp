# SPEC v2.1 — Backend Pool (3 concurrent audits)

> **Status:** Phase 1 SHIPPED (3 LibreCrawl backends live on brain.posimyth.com).
> **Status:** Phase 2 PENDING (wrapper code + state schema changes).
> **Owner:** Aditya — execute in dedicated session with fresh context.
> **Hard rule:** "without fail" — full smoke-test gate before declaring done.

---

## Why v2.1 exists

The librecrawl-mcp wrapper today serialises audits through one LibreCrawl
backend. Multiple team members hitting the MCP queue behind a single worker
thread + a single upstream that's hard-coded single-tenant. For team use this
forces serial-only audits and 1-hour+ wait times.

**Goal:** 3 concurrent audits, each on its own LibreCrawl backend, with the
same ephemeral guarantees per audit (cleanup, watchdog, atomic).

## What's already done (Phase 1, shipped on brain.posimyth.com 2026-06-09)

| Surface | State |
|---|---|
| docker-compose.override.yml | 3 services: `librecrawl-1` (port 5080), `librecrawl-2` (5085), `librecrawl-3` (5086). Each with own data dir (`data-1` / `data-2` / `data-3`). |
| LibreCrawl containers | All 3 healthy. ~3 GB RAM each idle. Verified responding HTTP 200 on `/`. |
| Backup | Full rollback set at `/home/posimyth-brain/librecrawl-3backend-rollback-20260609-1043/` (312 MB) — includes compose files, data snapshot, mcp-server snapshot, state.db |
| Wrapper behaviour | UNCHANGED — still routes to port 5080 only. No regression. |

## What Phase 2 must change

### 1. `state.py` — schema migration

Add backend_url column to sessions table:

```sql
ALTER TABLE sessions ADD COLUMN backend_url TEXT;
```

The migration in `init_db()` must be idempotent (use `PRAGMA table_info(sessions)` to check before ALTER). Existing rows backfill to `LIBRECRAWL_POOL[0]` (i.e. port 5080, librecrawl-1) so any in-flight session at deploy time keeps working.

Add helper:

```python
def get_busy_backends() -> set[str]:
    """URLs of backends with an active (non-terminal) session."""
    conn = _connect()
    rows = conn.execute(
        "SELECT DISTINCT backend_url FROM sessions "
        "WHERE status IN ('queued', 'crawling', 'throttled', 'paused') "
        "AND backend_url IS NOT NULL"
    ).fetchall()
    conn.close()
    return {r['backend_url'] for r in rows}
```

`create_session()` signature: add `backend_url` kwarg, insert into row.

### 2. `server.py` — backend pool + ContextVar routing

Replace the single `BASE` + `_client` pattern with a pool-aware setup.

```python
import contextvars

# Existing for back-compat with old configs
BASE = f"http://127.0.0.1:{os.getenv('LIBRECRAWL_PORT', '5080')}"

# New pool config
LIBRECRAWL_POOL = [
    u.strip() for u in os.getenv('LIBRECRAWL_POOL', BASE).split(',') if u.strip()
]

# Multi-backend upstream DB map (for cleanup tools)
# Format: "url:db_path,url:db_path,..."
LIBRECRAWL_UPSTREAM_DBS = {}
for entry in os.getenv('LIBRECRAWL_UPSTREAM_DBS', '').split(','):
    if ':' in entry and entry.strip():
        url, path = entry.strip().split(':', 1)
        LIBRECRAWL_UPSTREAM_DBS[url.strip()] = Path(path.strip())

# Single-backend fallback
if not LIBRECRAWL_UPSTREAM_DBS:
    LIBRECRAWL_UPSTREAM_DBS[LIBRECRAWL_POOL[0]] = Path(
        os.getenv('LIBRECRAWL_UPSTREAM_DB',
                  '/home/posimyth-brain/webapps/librecrawl/data/users.db')
    )

# Per-backend httpx clients
_clients: dict[str, httpx.Client] = {}
_clients_lock = threading.Lock()

# Context-var: which backend the current execution belongs to
_current_backend_var = contextvars.ContextVar('librecrawl_backend', default=None)

def _current_backend() -> str:
    """Backend URL for the current execution context. Falls back to first pool member."""
    return _current_backend_var.get() or LIBRECRAWL_POOL[0]

def get_client():
    """Return authenticated httpx.Client for the current backend."""
    backend = _current_backend()
    with _clients_lock:
        c = _clients.get(backend)
        if c is None or c.is_closed:
            c = httpx.Client(timeout=30, follow_redirects=True)
            c.post(f"{backend}/api/login", json={"username": "mcp-user"}).raise_for_status()
            _clients[backend] = c
        return c

def call(method, path, **kwargs):
    backend = _current_backend()
    r = get_client().request(method, f"{backend}{path}", **kwargs)
    if r.status_code == 401:
        with _clients_lock:
            _clients.pop(backend, None)
        r = get_client().request(method, f"{backend}{path}", **kwargs)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        raise RuntimeError(f"LibreCrawl returned non-JSON ({r.status_code}): {r.text[:200]}")

def pick_backend() -> str | None:
    """Claim an available backend or return None if all busy."""
    busy = state.get_busy_backends()
    for backend in LIBRECRAWL_POOL:
        if backend not in busy:
            return backend
    return None
```

Then sed-replace every inline `f"{BASE}/api/..."` and `f"{BASE}{path}"` in
server.py with `f"{_current_backend()}/api/..."` — about 12 sites
(per the earlier grep). Each is mechanical.

Multi-DB cleanup:

```python
def _wipe_upstream_crawl_record(crawl_id: int, backend_url: str | None = None) -> dict:
    """Delete upstream rows for crawl_id from the correct backend's DB."""
    if crawl_id is None:
        return {"skipped": "no crawl_id"}
    # Resolve which DB to hit
    if backend_url is None:
        backend_url = LIBRECRAWL_POOL[0]
    db_path = LIBRECRAWL_UPSTREAM_DBS.get(backend_url)
    if not db_path or not db_path.exists():
        return {"skipped": f"upstream db not found for backend {backend_url}"}
    # ... existing delete logic, but using db_path instead of LIBRECRAWL_UPSTREAM_DB ...

def _wipe_all_upstream_crawls() -> dict:
    """Truncate crawl tables across ALL backends."""
    out = {}
    for backend_url, db_path in LIBRECRAWL_UPSTREAM_DBS.items():
        if not db_path.exists():
            out[backend_url] = {"skipped": "db not found"}
            continue
        # ... existing per-DB truncate ...
        out[backend_url] = counts
    return out
```

Update `librecrawl_start_chunked_audit` to claim a backend:

```python
backend = pick_backend()
if backend is None:
    busy = state.get_busy_backends()
    return {
        "success": False,
        "error": "all_backends_busy",
        "pool_size": len(LIBRECRAWL_POOL),
        "busy_backends": list(busy),
        "hint": "Wait for an in-flight audit to complete or call librecrawl_wipe_everything",
    }
state.create_session(..., backend_url=backend)
```

### 3. `runner.py` — multi-worker + ContextVar propagation

Change from 1 worker thread to N (where N = pool size).

```python
def _worker_loop():
    """Pick up queued sessions FIFO for THIS worker's backend slot."""
    # Boot recovery — anything in non-terminal state gets re-queued.
    for s in state.find_active_sessions():
        if s["status"] != "queued":
            state.set_status(s["id"], "queued", "boot_recovery_requeue")

    while not _shutdown.is_set():
        queued = state.find_queued_sessions()
        if not queued:
            _wake.wait(timeout=5)
            _wake.clear()
            continue

        # Find a queued session whose backend is currently free
        busy = state.get_busy_backends()
        my_session = None
        for session in queued:
            backend = session.get('backend_url')
            if backend and backend not in busy:
                my_session = session
                break
        if my_session is None:
            _wake.wait(timeout=5)
            _wake.clear()
            continue

        # CLAIM by setting status to 'crawling' BEFORE releasing lock
        # ... (need to design lock-free claim — see "Race condition" below)

        # Set ContextVar for this worker's run
        token = _current_backend_var.set(my_session['backend_url'])
        try:
            _run_session(my_session)
        except Exception as e:
            state.update_session(my_session["id"], last_error=str(e))
            state.set_status(my_session["id"], "failed", f"runner_exception: {e}")
        finally:
            _current_backend_var.reset(token)

def start_runner():
    """Idempotent. Spawn N worker threads (one per backend in pool)."""
    global _runner_threads
    state.init_db()
    if _runner_threads:
        return
    _shutdown.clear()
    pool_size = max(1, len(server.LIBRECRAWL_POOL))
    _runner_threads = []
    for i in range(pool_size):
        t = threading.Thread(target=_worker_loop, name=f"librecrawl-runner-{i}", daemon=True)
        t.start()
        _runner_threads.append(t)
```

### 4. `watchdog.py` — multi-DB awareness

Update to iterate every upstream DB:

```python
UPSTREAM_DBS = os.environ.get('LIBRECRAWL_UPSTREAM_DBS', '').split(',')
# Parse and use list of DBs instead of single
```

### 5. Server-side environment changes

Add to MCP wrapper systemd / PM2 environment:

```bash
LIBRECRAWL_POOL="http://127.0.0.1:5080,http://127.0.0.1:5085,http://127.0.0.1:5086"
LIBRECRAWL_UPSTREAM_DBS="http://127.0.0.1:5080:/home/posimyth-brain/webapps/librecrawl/data-1/users.db,http://127.0.0.1:5085:/home/posimyth-brain/webapps/librecrawl/data-2/users.db,http://127.0.0.1:5086:/home/posimyth-brain/webapps/librecrawl/data-3/users.db"
```

## Race condition: claiming a backend safely

Two workers could read `get_busy_backends()` simultaneously and both claim the
same free backend. Two solutions:

**Option A (simpler):** SELECT...UPDATE in one transaction:

```python
def claim_session(session_id: str, backend_url: str) -> bool:
    """Atomically: only succeed if this backend isn't already in use by
    any other non-terminal session."""
    with _LOCK:
        conn = _connect()
        cur = conn.execute("""
            UPDATE sessions
            SET status = 'crawling', updated_at = ?
            WHERE id = ?
              AND status = 'queued'
              AND NOT EXISTS (
                  SELECT 1 FROM sessions s2
                  WHERE s2.backend_url = ?
                    AND s2.id != sessions.id
                    AND s2.status IN ('crawling', 'throttled', 'paused')
              )
        """, (time.time(), session_id, backend_url))
        conn.close()
        return cur.rowcount > 0
```

**Option B (Postgres style):** A single backend-claims table where you INSERT
with UNIQUE constraint on backend_url.

Option A is simpler given we're already on SQLite WAL.

## Smoke test gate (must pass before declaring done)

```text
1. Wipe to zero baseline (librecrawl_wipe_everything)
2. Start audit A on adityaarsharma.com (25 pages)
3. Start audit B on iana.org (10 pages)         (while A still running)
4. Start audit C on httpbin.org (5 pages)        (while A + B still running)
5. Check state.db: 3 sessions, each on a different backend_url
6. Check docker stats: librecrawl-1/2/3 ALL showing CPU activity
7. Try to start audit D                          (should return "all_backends_busy")
8. Wait for all 3 to complete
9. Verify each zip's contents are correct (8 files, sha256, etc.)
10. Verify post-state: 0 sessions, 0 files, 0 upstream rows across all 3 DBs
11. Wall-clock time of (A∥B∥C) should be ≈ time(slowest) NOT sum(A+B+C)
```

If steps 5, 6, 7, or 11 fail → roll back via the Phase 1 backup dir.

## Rollback procedure

```bash
ssh hetzner-brain "
  BACKUP=/home/posimyth-brain/librecrawl-3backend-rollback-20260609-1043
  cd /home/posimyth-brain/webapps/librecrawl
  docker compose down
  cp \$BACKUP/docker-compose.override.yml ./
  rm -rf data && cp -r \$BACKUP/data-snapshot data
  docker compose up -d
  cp \$BACKUP/librecrawl-state.db /home/posimyth-brain/
  cp -r \$BACKUP/mcp-server-snapshot/* /home/posimyth-brain/webapps/librecrawl/mcp-server/
  pm2 restart librecrawl-mcp --update-env
"
```

Then docker-compose back to single backend + restore original mcp-server code.

## Effort estimate

| Step | Effort |
|---|---|
| state.py changes + migration | 30 min |
| server.py BASE → ContextVar rewrites + helpers | 60 min |
| runner.py multi-worker + claim logic | 45 min |
| watchdog.py multi-DB | 15 min |
| Local syntax checks + dry runs | 15 min |
| Deploy via scp + pm2 restart | 15 min |
| Smoke test (3 parallel + edge cases) | 30 min |
| **Total** | **3-3.5 hours** |

## What this DOES NOT change

- Per-audit cleanup behaviour (still triggers on `audit_zip` with `auto_cleanup=True`)
- Watchdog TTL policy (1h done / 4h crawling / 30m queued / immediate failed)
- Ephemeral guarantees (still zero data after audit completes)
- Bearer token auth
- v2.0.5 hreflang fixes
- Any tool surface area (37 tools stay 37 tools — same names, same signatures)

## Future v2.2+ (out of scope here)

- Auto-scaling pool based on demand
- Per-team-member bearer tokens with audit-log
- Backend health monitoring + auto-mark-dead
- LibreCrawl backend itself becoming multi-tenant (upstream change, not ours)
