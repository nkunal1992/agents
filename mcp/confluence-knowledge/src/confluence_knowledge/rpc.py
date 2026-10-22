"""Index-mediated RPC between the thin MCP proxy and the daemon (#189).

The daemon is the brain — it owns the index writer connection, the crawl
workers, and the model pool. The per-window MCP process is a thin proxy:
light tools read the WAL snapshot directly (`Index._read`), mutating
tools become `rpc_requests` rows that the daemon claims and answers via
`rpc_responses`.

Design choices:

- **Separate `rpc.db`** — the index db stays daemon-writer-exclusive;
  the proxy's index handle opens mode=ro, so a stray write is an
  `OperationalError`, not silent corruption (the user's safeguard 2).
- **Crash-free by construction** — a request row is durable the moment
  it's committed; a daemon restart mid-call re-polls the same request
  (claimed rows carry a claim timestamp the takeover re-arms).
- **Bounded latency** — the daemon polls every ~150 ms; control calls
  gain ~one poll tick, invisible next to tool runtimes.
- **No new protocol** — plain SQLite rows, cross-platform, debuggable
  with `sqlite3 rpc.db`.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS rpc_requests(
    id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}',
    service TEXT NOT NULL DEFAULT '',
    created_at REAL,
    deadline REAL,
    claimed_by TEXT,
    claimed_at REAL);
CREATE TABLE IF NOT EXISTS rpc_responses(
    id TEXT PRIMARY KEY,
    result TEXT,
    error TEXT,
    finished_at REAL);
CREATE TABLE IF NOT EXISTS daemon_lease(
    job_id TEXT PRIMARY KEY,   -- always '__daemon__'; reuses the
    owner TEXT,                -- job_lease row shape/takeover semantics
    pid INTEGER,
    heartbeat REAL);
"""

_POLL_S = 0.15
_LEASE_STALE_S = 5.0       # daemon heartbeats every ~1 s
_RESP_TTL_S = 600.0        # answered responses garbage-collected


class DaemonUnavailable(RuntimeError):
    """No live daemon and spawning one failed — caller falls back."""


class RpcDb:
    """The control-plane db shared by every proxy process and the one
    daemon. WAL + busy timeout keep concurrent proxies race-free."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=5000")
        with self.db:
            self.db.executescript(_SCHEMA)
            try:
                self.db.execute("ALTER TABLE rpc_requests "
                                "ADD COLUMN service TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # pre-#206 db already carries the column
        self.lock = threading.Lock()

    # ---- client (proxy) side ----
    def submit(self, tool: str, args: dict, timeout_s: float,
               service: str = "") -> str:
        rid = uuid.uuid4().hex[:16]
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO rpc_requests VALUES (?,?,?,?,?,?,?,?)",
                (rid, tool, json.dumps(args), service, time.time(),
                 time.time() + timeout_s, None, None))
        return rid

    def wait(self, rid: str, timeout_s: float,
             on_tick=None) -> dict:
        """Poll for the response row; daemon death mid-wait raises
        DaemonUnavailable so the proxy can run the call locally."""
        deadline = time.monotonic() + timeout_s
        # wall-clock ceiling: time.monotonic can stall across an OS
        # suspend (platform-dependent) — a request must still die at its
        # real deadline instead of waking into a client timeout (#206)
        wall_deadline = time.time() + timeout_s + 10
        while time.monotonic() < deadline and time.time() < wall_deadline:
            with self.lock:
                row = self.db.execute(
                    "SELECT result,error FROM rpc_responses WHERE id=?",
                    (rid,)).fetchone()
            if row is not None:
                self._gc()
                if row[1]:
                    raise RuntimeError(row[1])
                return json.loads(row[0]) if row[0] else {}
            if not self.daemon_alive():
                raise DaemonUnavailable("daemon lease heartbeat stale")
            if on_tick:
                on_tick()   # keep-alive hook — MCP progress notification
            time.sleep(_POLL_S)
        raise DaemonUnavailable("daemon response timed out")

    # ---- daemon side ----
    def poll_requests(self, owner: str) -> list[tuple[str, str, dict]]:
        """Claim every pending request atomically; expired deadlines are
        dropped without a response (the proxy already timed out)."""
        now = time.time()
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT id,tool,args,service FROM rpc_requests "
                "WHERE claimed_by IS NULL AND deadline > ?", (now,)
            ).fetchall()
            self.db.executemany(
                "UPDATE rpc_requests SET claimed_by=?, claimed_at=? "
                "WHERE id=?", [(owner, now, r[0]) for r in rows])
            # reclaim requests whose claimer died mid-work
            self.db.execute(
                "UPDATE rpc_requests SET claimed_by=NULL, claimed_at=NULL "
                "WHERE claimed_at < ? AND id NOT IN "
                "(SELECT id FROM rpc_responses)", (now - _LEASE_STALE_S * 4,))
        return [(r[0], r[1], json.loads(r[2]), r[3]) for r in rows]

    def respond(self, rid: str, result: dict | None = None,
                error: str | None = None) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO rpc_responses VALUES (?,?,?,?)",
                (rid, json.dumps(result) if result is not None else None,
                 error, time.time()))

    def _gc(self) -> None:
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM rpc_responses WHERE finished_at < ?",
                (time.time() - _RESP_TTL_S,))
            self.db.execute(
                "DELETE FROM rpc_requests WHERE deadline < ?",
                (time.time(),))

    # ---- daemon lease (job_lease pattern, service level) ----
    def acquire_daemon(self, owner: str, pid: int) -> bool:
        """Take the daemon lease — stale heartbeat or dead pid hands it
        over, mirroring Index.acquire_lease semantics."""
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT owner,pid,heartbeat FROM daemon_lease "
                "WHERE job_id='__daemon__'").fetchone()
            if row is not None and row[0] != owner:
                stale = (row[2] or 0) < now - _LEASE_STALE_S
                dead = False
                if not stale:
                    try:
                        import psutil
                        dead = not psutil.pid_exists(int(row[1]))
                    except Exception:    # noqa: BLE001 — can't verify
                        dead = False
                if not (stale or dead):
                    return False
            self.db.execute(
                "INSERT OR REPLACE INTO daemon_lease VALUES ('__daemon__',?,?,?)",
                (owner, pid, now))
            return True

    def heartbeat_daemon(self, owner: str) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE daemon_lease SET heartbeat=? WHERE job_id='__daemon__' "
                "AND owner=?", (time.time(), owner))
            return cur.rowcount > 0

    def release_daemon(self, owner: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                "DELETE FROM daemon_lease WHERE job_id='__daemon__' AND owner=?",
                (owner,))

    def daemon_pid(self) -> int | None:
        """The leased daemon's pid when it's live, else None."""
        with self.lock:
            row = self.db.execute(
                "SELECT pid,heartbeat FROM daemon_lease "
                "WHERE job_id='__daemon__'").fetchone()
        if row is None:
            return None
        pid, beat = int(row[0]), float(row[1])
        if beat < time.time() - _LEASE_STALE_S:
            return None
        try:
            import psutil
            return pid if psutil.pid_exists(pid) else None
        except Exception:                       # noqa: BLE001 —
            return pid                           # trust the heartbeat

    def daemon_alive(self) -> bool:
        """A live heartbeat inside the staleness window — pid liveness is
        checked too so a wedged-but-alive daemon still counts as down."""
        with self.lock:
            row = self.db.execute(
                "SELECT pid,heartbeat FROM daemon_lease "
                "WHERE job_id='__daemon__'").fetchone()
        if row is None:
            return False
        if (row[1] or 0) < time.time() - _LEASE_STALE_S:
            return False
        try:
            import psutil
            return psutil.pid_exists(int(row[0]))
        except Exception:              # noqa: BLE001 — no psutil → trust
            return True                # the heartbeat


def daemon_enabled(service_env: str) -> bool:
    """Proxy mode gate (#189/#196): ON by default — the service env opt-out
    (e.g. LOCAL_KNOWLEDGE_DAEMON=0) is the only off switch. The daemon
    itself never re-proxies (KNOWLEDGE_DAEMON_ROLE=1 in its env)."""
    if os.environ.get("KNOWLEDGE_DAEMON_ROLE"):
        return False
    return os.environ.get(service_env, "1") != "0"


def daemon_owner() -> str:
    # the "daemon:" prefix lets lease-readers label the holder as the
    # brain rather than "another IDE window"
    return f"daemon:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def lease_label(owner: str | None) -> str:
    """Human label for whoever holds a job lease."""
    if owner and owner.startswith("daemon:"):
        return "the knowledge daemon"
    return "another server process"


def ensure_daemon(rpc: RpcDb, env: dict | None = None) -> None:
    """Spawn the shared daemon lazily if no live owner holds the lease.
    Racing proxies may double-spawn — self-correcting: the loser's
    acquire_daemon fails and it exits. The proxy never manages the
    lifecycle beyond this — a dead lease means the next request simply
    respawns it. The daemon anchors at daemon_home(): one process per
    interpreter serving every installed knowledge service (#206)."""
    if rpc.daemon_alive():
        return
    try:
        from .settings import daemon_home
        workdir = daemon_home()
        workdir.mkdir(parents=True, exist_ok=True)
        pid_path = workdir / ".daemon.pid"
        # a deliberate respawn means someone wants the daemon up — a
        # stale stop-file from an earlier `X stop` must not leave this
        # process watchdog-less forever
        (workdir / ".watchdog.stop").unlink(missing_ok=True)
        cli_env = dict(os.environ)
        cli_env["KNOWLEDGE_DAEMON_ROLE"] = "1"   # the brain never proxies
        if env:
            cli_env.update(env)
        proc = subprocess.Popen(
            [sys.executable, "-m", "confluence_knowledge.daemon",
             str(workdir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            # fd stays open for the child's lifetime — Popen owns it
            stderr=open(workdir / "daemon.log", "ab"),   # noqa: SIM115
            env=cli_env,
            start_new_session=True)      # detached — survives IDE exit
        pid_path.write_text(str(proc.pid))
        # wait for the child to take the lease — sequential proxy calls
        # would otherwise each spawn (boot is ~1-2 s)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not rpc.daemon_alive():
            time.sleep(0.1)
        if not rpc.daemon_alive():
            raise DaemonUnavailable("daemon spawned but never took lease")
        # the http endpoint binds right after the lease — and the meta
        # file is written BEFORE uvicorn accepts, so probe the socket,
        # not the file: a caller reading a stale/unbound port once is a
        # failed first connection (#206 cold-boot race)
        import json as _json
        import socket as _sock
        http_deadline = time.monotonic() + 8
        while time.monotonic() < http_deadline:
            try:
                meta = _json.loads((workdir / "http.json").read_text())
                with _sock.socket() as sock:
                    sock.settimeout(0.5)
                    sock.connect(("127.0.0.1", meta["port"]))
                break
            except Exception:                   # noqa: BLE001 —
                time.sleep(0.1)                  # file or bind pending
    except DaemonUnavailable:
        raise
    except Exception as e:
        raise DaemonUnavailable(f"daemon spawn failed: {e}") from e
