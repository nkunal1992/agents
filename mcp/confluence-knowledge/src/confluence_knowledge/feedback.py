"""Per-scope human-feedback store — saved corrections keyed by query
embedding. Local-first: one SQLite file per scope under the index dir;
`clean` removes them along with the index (documented semantics).

WAL mode serialises concurrent writers (two IDE servers on the same scope)
and every read is a fresh query — a correction another process wrote is
visible immediately. Legacy `<scope>.feedback.json` files are migrated once
and renamed `.migrated`.

Design rules (from issue #107): corrections are disclosed ALONGSIDE
documentation — "the docs say X, but a saved correction says Y" —
never silently override, never silently dropped.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

import numpy as np

# measured on a real dense embedder (bge-m3-era numbers; re-verify on nomic-embed-text-v1.5): identical question 1.00, close paraphrase ~0.77,
# same-topic different-intent ~0.68, unrelated ~0.39 → 0.75 catches natural
# rephrases without leaking across topics
DEFAULT_MIN_SIM = 0.75

_DDL = """CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL, qvec BLOB NOT NULL,
  correction TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  ts INTEGER NOT NULL)"""


def _valid_entry(e) -> bool:
    """An entry is usable only with a real vector and text fields."""
    return (isinstance(e, dict)
            and isinstance(e.get("question"), str)
            and isinstance(e.get("correction"), str)
            and isinstance(e.get("qvec"), list) and e["qvec"]
            and all(isinstance(x, (int, float)) for x in e["qvec"]))


class FeedbackStore:
    """One `<scope>.feedback.db` per scope under the index dir."""

    def __init__(self, index_root: Path, scope_key: str, cli: str):
        self.path = index_root / f"{scope_key}.feedback.db"
        self._json_path = index_root / f"{scope_key}.feedback.json"
        env_key = cli.upper().replace("-", "_") + "_FEEDBACK_MIN_SIM"
        self.min_sim = float(os.environ.get(
            env_key, os.environ.get("KNOWLEDGE_FEEDBACK_MIN_SIM",
                                    DEFAULT_MIN_SIM)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=30,
                                   check_same_thread=False)
        # journal-mode switch + DDL are write ops — two processes racing a
        # fresh file get SQLITE_BUSY despite the busy timeout; retry briefly
        for attempt in range(50):
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute(_DDL)
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 49:
                    raise
                time.sleep(0.1)
        self._migrate_json()

    def _migrate_json(self) -> None:
        """One-shot import of a legacy JSON store; skips malformed entries
        (R09) and renames the file so it never re-imports."""
        if not self._json_path.exists():
            return
        try:
            data = json.loads(self._json_path.read_text())
        except (json.JSONDecodeError, OSError):
            data = []
        if not isinstance(data, list):
            data = []
        try:
            with self._db:
                if self._db.execute(
                        "SELECT COUNT(*) FROM feedback").fetchone()[0] == 0:
                    self._db.executemany(
                        "INSERT INTO feedback(question,qvec,correction,note,ts)"
                        " VALUES (?,?,?,?,?)",
                        [(e["question"],
                          np.asarray(e["qvec"], dtype=np.float32).tobytes(),
                          e["correction"], str(e.get("note") or ""),
                          int(e.get("ts") or time.time()))
                         for e in data if _valid_entry(e)])
        finally:
            # renamed even on failure — a corrupt file must not wedge the store
            self._json_path.replace(
                self._json_path.with_suffix(".json.migrated"))

    def _rows(self) -> list[dict]:
        return [{"id": r[0], "question": r[1],
                 "qvec": np.frombuffer(r[2], dtype=np.float32),
                 "correction": r[3], "note": r[4], "ts": r[5]}
                for r in self._db.execute(
                    "SELECT id,question,qvec,correction,note,ts "
                    "FROM feedback ORDER BY id")]

    def add(self, question: str, qvec: np.ndarray, correction: str,
            note: str = "") -> dict:
        ts = int(time.time())
        with self._db:
            cur = self._db.execute(
                "INSERT INTO feedback(question,qvec,correction,note,ts)"
                " VALUES (?,?,?,?,?)",
                (question, np.asarray(qvec, dtype=np.float32).tobytes(),
                 correction, note or "", ts))
        return {"id": f"fb{cur.lastrowid:04d}", "question": question,
                "correction": correction, "note": note or "", "ts": ts}

    def match(self, qvec: np.ndarray, k: int = 3) -> list[dict]:
        """Cosine-similar corrections ≥ min_sim (default 0.75 — catches
        paraphrases without leaking across topics), best first."""
        # entries written under a different embedder can never match —
        # quarantine them rather than crash on a ragged matmul (R14)
        entries = [e for e in self._rows() if len(e["qvec"]) == len(qvec)]
        if not entries:
            return []
        mat = np.array([e["qvec"] for e in entries], dtype=np.float32)
        q = qvec / (np.linalg.norm(qvec) + 1e-9)
        m = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        sims = m @ q
        out = []
        for i in np.argsort(-sims):
            if sims[i] < self.min_sim or len(out) >= k:
                break
            e = dict(entries[int(i)])
            e.pop("qvec", None)
            e["similarity"] = float(sims[i])
            e["age_days"] = int((time.time() - e["ts"]) / 86400)
            out.append(e)
        return out

    def report(self) -> dict:
        """Flat correction list. Entries carry no page link — the agent
        infers the outdated page from the corrected question's context."""
        out = []
        for e in self._rows():
            out.append({"question": e["question"], "correction": e["correction"],
                        "note": e["note"], "age_days":
                        int((time.time() - e["ts"]) / 86400)})
        return {"count": len(out), "corrections": out}
