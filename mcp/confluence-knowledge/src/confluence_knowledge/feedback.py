"""Per-scope human-feedback store — saved corrections keyed by query
embedding. Local-first: one JSON file per scope under the index dir;
`clean` removes them along with the index (documented semantics).

Design rules (from issue #107): corrections are disclosed ALONGSIDE
documentation — "the docs say X, but a saved correction says Y" —
never silently override, never silently dropped.
"""

import json
import os
import tempfile
import time
from pathlib import Path

import numpy as np

# measured on real bge-m3: identical question 1.00, close paraphrase ~0.77,
# same-topic different-intent ~0.68, unrelated ~0.39 → 0.75 catches natural
# rephrases without leaking across topics
DEFAULT_MIN_SIM = 0.75


class FeedbackStore:
    """JSON list under the index dir; same integrity discipline as Index
    (tolerant load, atomic writes)."""

    def __init__(self, index_root: Path, scope_key: str, cli: str):
        self.path = index_root / f"{scope_key}.feedback.json"
        env_key = cli.upper().replace("-", "_") + "_FEEDBACK_MIN_SIM"
        self.min_sim = float(os.environ.get(
            env_key, os.environ.get("KNOWLEDGE_FEEDBACK_MIN_SIM",
                                    DEFAULT_MIN_SIM)))
        self._entries = self._load()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []  # corrupt file → start fresh, don't crash the server

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self._entries, f)
        os.replace(tmp, self.path)  # atomic — a crash mid-write can't corrupt

    def add(self, question: str, qvec: np.ndarray, correction: str,
            note: str = "") -> dict:
        entry = {"id": f"fb{len(self._entries):04d}",
                 "question": question,
                 "qvec": qvec.astype(np.float32).tolist(),
                 "correction": correction, "note": note,
                 "ts": int(time.time())}
        self._entries.append(entry)
        self._save()
        return entry

    def match(self, qvec: np.ndarray, k: int = 3) -> list[dict]:
        """Cosine-similar corrections ≥ min_sim (default 0.75 — catches
        paraphrases without leaking across topics), best first."""
        if not self._entries:
            return []
        mat = np.array([e["qvec"] for e in self._entries], dtype=np.float32)
        q = qvec / (np.linalg.norm(qvec) + 1e-9)
        m = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        sims = m @ q
        out = []
        for i in np.argsort(-sims):
            if sims[i] < self.min_sim or len(out) >= k:
                break
            e = dict(self._entries[int(i)])
            e.pop("qvec", None)
            e["similarity"] = float(sims[i])
            e["age_days"] = int((time.time() - e["ts"]) / 86400)
            out.append(e)
        return out

    def report(self) -> dict:
        """Aggregate view for 'which pages keep getting corrected' — the
        agent summarizes these as outdated/confusing doc candidates."""
        out = []
        for e in self._entries:
            out.append({"question": e["question"], "correction": e["correction"],
                        "note": e["note"], "age_days":
                        int((time.time() - e["ts"]) / 86400)})
        return {"count": len(out), "corrections": out}
