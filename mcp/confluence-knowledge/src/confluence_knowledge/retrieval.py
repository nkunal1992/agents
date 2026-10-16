"""Local hybrid retrieval: vectors + FTS → RRF → local cross-encoder rerank.

Embeddings and reranking run exclusively on this machine. No text ever leaves
the process for retrieval. Models load local_files_only with trust_remote_code
disabled; a missing model is a setup error, never a silent fallback.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np

from .index import Index
from .settings import SetupError

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"   # Apache-2.0
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"     # Apache-2.0

TOP_K_RETRIEVE = 30
TOP_K_EVIDENCE = 8


def _models_dir(home: Path) -> Path:
    return home / "models"


def _load_sentence_transformer(model_key: str, home: Path):
    """Local-only model load — fails closed if artifacts aren't prepared."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise SetupError(
            "sentence-transformers isn't installed — run "
            "`confluence-knowledge prepare-models` (see docs/confluence-knowledge.md)"
        ) from e
    local = _models_dir(home) / model_key.split("/")[-1]
    if not local.exists():
        raise SetupError(
            f"local model '{model_key}' not prepared — run "
            "`confluence-knowledge prepare-models`. Cloud embeddings are never used."
        )
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return SentenceTransformer(
        str(local), local_files_only=True, trust_remote_code=False,
        device="cpu")


class LocalEmbedder:
    def __init__(self, home: Path):
        self._m = _load_sentence_transformer(EMBED_MODEL, home)

    def embed(self, texts: list[str]) -> np.ndarray:
        v = self._m.encode(texts, convert_to_numpy=True, normalize_embeddings=True,
                           show_progress_bar=False)
        return np.asarray(v, dtype=np.float32)


class LocalReranker:
    def __init__(self, home: Path):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as e:
            raise SetupError(
                "sentence-transformers isn't installed — run "
                "`confluence-knowledge prepare-models`"
            ) from e
        local = _models_dir(home) / RERANK_MODEL.split("/")[-1]
        if not local.exists():
            raise SetupError(
                f"local reranker '{RERANK_MODEL}' not prepared — run "
                "`confluence-knowledge prepare-models`.")
        self._m = CrossEncoder(str(local), device="cpu",
                               trust_remote_code=False)

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        return np.asarray(
            self._m.predict([[query, d] for d in docs], show_progress_bar=False),
            dtype=np.float32)


def prepare_models(home: Path) -> list[str]:
    """Download pinned models once (public artifacts; no document text sent)."""
    from huggingface_hub import snapshot_download

    out = []
    lock = _load_lock()
    for key, repo in (("embed", EMBED_MODEL), ("rerank", RERANK_MODEL)):
        rev = lock["models"][key]["revision"]
        dest = _models_dir(home) / repo.split("/")[-1]
        if dest.exists():
            # treat partial/interrupted downloads as not prepared: a finished
            # dir has config + model weights + tokenizer files
            complete = (dest / "config.json").exists() and (
                any(dest.glob("*.safetensors")) or any(dest.rglob("*.safetensors")))
            if complete:
                out.append(f"already prepared: {repo}@{rev[:8]}")
                continue
            shutil.rmtree(dest)  # incomplete — fetch again
        dest.parent.mkdir(parents=True, exist_ok=True)
        snapshot_download(repo_id=repo, revision=rev, local_dir=dest,
                          # safetensors + tokenizer/config only: the ONNX and
                          # OpenVINO variants are ~5x the size and never loaded
                          allow_patterns=["*.json", "*.txt", "*.safetensors",
                                          "*.md", "LICENSE*",
                                          "*.model", "*.spm", "modules.json"],
                          ignore_patterns=["*.bin", "*.msgpack", "*.h5",
                                           "*.pt", "*.ot"])
        if not _license_ok(dest):
            raise SetupError(f"{repo} license artifact missing — refusing to ship")
        out.append(f"prepared {repo}@{rev[:8]} (Apache-2.0)")
    return out


def _license_ok(dest: Path) -> bool:
    """LICENSE* file present, or the model card declares a permissive license."""
    if any(dest.glob("LICENSE*")):
        return True
    for md in dest.glob("*.md"):
        head = md.read_text(errors="ignore")[:6000].lower()
        if "license: apache-2.0" in head or "license: mit" in head:
            return True
    return False


def _load_lock() -> dict:
    import json

    p = Path(__file__).parent / "models.lock.json"  # shipped inside the package
    if p.exists():
        return json.loads(p.read_text())
    # package-data should always be present; refuse rather than float to main
    raise SetupError("models.lock.json missing from package — reinstall the service")


def _cosine(q: np.ndarray, mat: dict[str, np.ndarray]) -> list[tuple[str, float]]:
    if not mat:
        return []
    ids = list(mat)
    m = np.stack([mat[i] for i in ids])
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
    q = q / (np.linalg.norm(q) + 1e-9)
    return sorted(zip(ids, (m @ q).tolist()), key=lambda t: -t[1])


def retrieve(index: Index, query: str, embedder: LocalEmbedder,
             reranker: LocalReranker | None = None) -> list[dict]:
    """Hybrid retrieve → RRF fuse → rerank → bounded evidence."""
    chunks = index.active_chunks()
    if not chunks:
        return []

    # vector leg
    qvec = embedder.embed([query])[0]
    vecs = index.vectors_for([c["chunk_id"] for c in chunks])
    vec_rank = [cid for cid, _ in _cosine(qvec, vecs)][:TOP_K_RETRIEVE]

    # keyword leg
    fts_rank = [cid for cid, _ in index.fts_search(query)][:TOP_K_RETRIEVE]

    # reciprocal-rank fusion
    rrf: dict[str, float] = {}
    for leg in (vec_rank, fts_rank):
        for rank, cid in enumerate(leg):
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)
    fused = sorted(rrf, key=lambda c: -rrf[c])[:TOP_K_RETRIEVE]
    by_id = {c["chunk_id"]: c for c in chunks}
    candidates = [by_id[c] for c in fused if c in by_id]

    if reranker is not None and candidates:
        scores = reranker.score(query, [c["text"] for c in candidates])
        order = np.argsort(-scores)[:TOP_K_EVIDENCE]
        evidence = []
        for i in order:
            c = dict(candidates[i])
            c["rerank_score"] = float(scores[i])
            evidence.append(c)
    else:
        evidence = [dict(c, rerank_score=None) for c in candidates[:TOP_K_EVIDENCE]]

    pages = index.active_pages()
    for e in evidence:
        p = pages.get(e["page_id"], {})
        e["page_title"], e["page_url"], e["level"] = (
            p.get("title", ""), p.get("url", ""), p.get("level"))
    return evidence


def check_citations(evidence: list[dict], mappings: list[dict]) -> dict:
    """Verify claim→chunk citations: chunk exists in this evidence set and the
    claimed quote is literally present in it. Provenance, not entailment."""
    by_id = {e["chunk_id"]: e for e in evidence}
    bad = []
    for m in mappings:
        ch = by_id.get(m.get("chunk_id", ""))
        if ch is None:
            bad.append({"claim": m.get("claim", "")[:80], "reason": "chunk not in evidence set"})
            continue
        quote = m.get("quote", "")
        if quote and quote not in ch["text"]:
            bad.append({"claim": m.get("claim", "")[:80],
                        "reason": "quote not present verbatim in cited chunk"})
    return {"ok": not bad, "violations": bad}
