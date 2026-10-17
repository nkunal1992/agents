"""Local hybrid retrieval: dense + sparse + FTS → RRF → ONNX cross-encoder.

Everything runs on this machine via ONNX Runtime — no torch, no cloud.
Models load local-only from pinned artifacts; a missing model is a setup
error, never a silent fallback.

Backend layout (all under <home>/models/):
  embed  — Xenova/bge-m3  onnx/sentence_transformers_quantized.onnx
           (dense 1024-dim, normalized, multilingual, 8k ctx)
  sparse — Qdrant/bm25 via fastembed SparseTextEmbedding
           (learned term-weight leg; BGE-M3's lexical head has no ONNX
           export, so a purpose-built sparse model fills that slot)
  rerank — onnx-community/bge-reranker-v2-m3-ONNX  onnx/model_quantized.onnx
           (sequence-classification logits → relevance score)
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np

from .index import Index
from .settings import SetupError

EMBED_REPO = "Xenova/bge-m3"                            # Apache-2.0/MIT
EMBED_ONNX = "onnx/sentence_transformers_quantized.onnx"
SPARSE_MODEL = "Qdrant/bm25"                            # MIT — via fastembed
RERANK_REPO = "onnx-community/bge-reranker-v2-m3-ONNX"  # MIT
RERANK_ONNX = "onnx/model_quantized.onnx"
RERANK_BASE_REPO = "onnx-community/bge-reranker-base-ONNX"  # light override

EMBED_MODEL_ID = "bge-m3-onnx-quantized"

TOP_K_RETRIEVE = 30
TOP_K_EVIDENCE = 8
RERANK_MAX = 25          # bounds cross-encoder latency on CPU
EMBED_MAX_TOKENS = 512   # quantized model input cap (chunks stay under)
RERANK_MAX_TOKENS = 512


def _models_dir(home: Path) -> Path:
    return home / "models"


def _model_home(home: Path, repo: str) -> Path:
    return _models_dir(home) / repo.split("/")[-1]


def _require(dest: Path, repo: str, cli: str, needed: list[str]) -> Path:
    if not all((dest / f).exists() for f in needed):
        raise SetupError(
            f"local model '{repo}' not prepared — run `{cli} prepare-models`. "
            "Cloud embeddings are never used.")
    return dest


def _ort_session(onnx_path: Path, cli: str):
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise SetupError("onnxruntime isn't installed — reinstall the "
                         f"service ({cli} bootstrap reinstalls deps)") from e
    return ort.InferenceSession(
        str(onnx_path), providers=["CPUExecutionProvider"],
        sess_options=_ort_opts(ort))


def _ort_opts(ort):
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
    so.inter_op_num_threads = 1
    so.log_severity_level = 3  # errors only
    return so


def _tokenizer(dest: Path):
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(dest / "tokenizer.json"))


class LocalEmbedder:
    """BGE-M3 dense embeddings via ONNX (no torch). Same interface."""

    def __init__(self, home: Path, cli: str = "confluence-knowledge"):
        dest = _require(_model_home(home, EMBED_REPO), EMBED_REPO, cli,
                        [EMBED_ONNX, "tokenizer.json"])
        self._sess = _ort_session(dest / EMBED_ONNX, cli)
        self._tok = _tokenizer(dest)
        self._tok.enable_padding()
        self._tok.enable_truncation(EMBED_MAX_TOKENS)

    def embed(self, texts: list[str]) -> np.ndarray:
        enc = self._tok.encode_batch(texts, add_special_tokens=True)
        ids = np.asarray([e.ids for e in enc], dtype=np.int64)
        mask = np.asarray([e.attention_mask for e in enc], dtype=np.int64)
        out = self._sess.run(
            ["sentence_embedding"],
            {"input_ids": ids, "attention_mask": mask})[0]
        v = np.asarray(out, dtype=np.float32)
        n = np.linalg.norm(v, axis=1, keepdims=True)
        return v / (n + 1e-9)  # defensive normalize — quantized graphs drift


class LocalSparseEmbedder:
    """Learned term-weight sparse vectors via fastembed (ONNX, no torch).

    Fills the role BGE-M3's lexical head plays on torch — no ONNX export of
    that head exists, so a purpose-built sparse model covers the same leg.
    """

    def __init__(self, home: Path, cli: str = "confluence-knowledge"):
        try:
            from fastembed import SparseTextEmbedding
        except ImportError as e:
            raise SetupError("fastembed isn't installed — reinstall the "
                             f"service ({cli} bootstrap reinstalls deps)") from e
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        dest = _models_dir(home) / "fastembed"
        try:
            self._m = SparseTextEmbedding(SPARSE_MODEL, cache_dir=str(dest),
                                          local_files_only=True)
        except Exception as e:
            raise SetupError(
                f"sparse model '{SPARSE_MODEL}' not prepared — run "
                f"`{cli} prepare-models`.") from e

    def embed(self, texts: list[str]) -> list[dict[int, float]]:
        """One {token_idx: weight} dict per input — the sparse payload."""
        out = []
        for emb in self._m.embed(texts):
            out.append(dict(zip(emb.indices.tolist(),
                                emb.values.tolist())))
        return out


class LocalReranker:
    """bge-reranker-v2-m3 via ONNX sequence-classification logits.

    Override with env `*_EMBED_RERANKER` = "onnx-community/bge-reranker-base-ONNX"
    (lighter, English) for slow machines — doctor surfaces the hint.
    """

    def __init__(self, home: Path, cli: str = "confluence-knowledge"):
        # per-app override → "<APP>_RERANKER" (e.g. SP_KNOWLEDGE_RERANKER)
        # plus a global KNOWLEDGE_RERANKER fallback
        env_key = cli.upper().replace("-", "_") + "_RERANKER"
        repo = os.environ.get(env_key, os.environ.get("KNOWLEDGE_RERANKER",
                                                     RERANK_REPO))
        dest = _require(_model_home(home, repo), repo, cli,
                        [RERANK_ONNX, "tokenizer.json"])
        self._sess = _ort_session(dest / RERANK_ONNX, cli)
        self._tok = _tokenizer(dest)
        self._tok.no_padding()
        self._tok.enable_truncation(RERANK_MAX_TOKENS)
        self.repo = repo

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        pairs = [[query, d] for d in docs]
        enc = self._tok.encode_batch(pairs, add_special_tokens=True)
        maxlen = max(len(e.ids) for e in enc)
        ids = np.zeros((len(enc), maxlen), dtype=np.int64)
        mask = np.zeros((len(enc), maxlen), dtype=np.int64)
        for i, e in enumerate(enc):
            ids[i, :len(e.ids)] = e.ids
            mask[i, :len(e.attention_mask)] = e.attention_mask
        logits = self._sess.run(
            ["logits"], {"input_ids": ids, "attention_mask": mask})[0]
        s = logits.reshape(-1).astype(np.float32)
        return 1.0 / (1.0 + np.exp(-s))  # sigmoid → 0..1 relevance


def prepare_models(home: Path) -> list[str]:
    """Download pinned ONNX artifacts once (public; no document text sent)."""
    from huggingface_hub import snapshot_download

    out = []
    lock = _load_lock()
    models = _models_dir(home)
    models.mkdir(parents=True, exist_ok=True)

    # dense + rerankers: direct HF snapshot (repo + revision pinned in the
    # lock); rerank_light is fetched too so *_RERANKER overrides always work
    for key in ("embed", "rerank", "rerank_light"):
        spec = lock["models"][key]
        dest = _models_dir(home) / spec["repo"].split("/")[-1]
        if dest.exists():
            complete = all((dest / f).exists() for f in spec["files"])
            if complete:
                out.append(f"already prepared: {spec['repo']}")
                continue
            shutil.rmtree(dest)  # partial/interrupted → refetch
        snapshot_download(repo_id=spec["repo"], revision=spec["revision"],
                          local_dir=dest, allow_patterns=spec["files"])
        if not _license_ok(dest):
            _license_ok_upstream(dest, spec, snapshot_download)
        if not _license_ok(dest):
            raise SetupError(
                f"{spec['repo']} license artifact missing — refusing to ship")
        out.append(f"prepared {spec['repo']}@{spec['revision'][:8]} "
                   f"({spec['license']})")

    # sparse: fastembed owns its cache layout — let it download into our dir
    os.environ.pop("HF_HUB_OFFLINE", None)
    spec = lock["models"]["sparse"]
    try:
        from fastembed import SparseTextEmbedding
        SparseTextEmbedding(spec["repo"], cache_dir=str(models / "fastembed"))
        out.append(f"prepared {spec['repo']} ({spec['license']})")
    except Exception as e:
        raise SetupError(f"couldn't fetch sparse model {spec['repo']}: {e}") from e
    return out


def _license_ok_upstream(dest: Path, spec: dict, snapshot_download) -> None:
    """ONNX-community repackages sometimes drop the license line — if the card
    points at a `base_model`, fetch that model's card and judge IT. The
    upstream card is saved next to ours for audit."""
    import re
    for md in dest.glob("*.md"):
        m = re.search(r"base_model:\s*(?:-\s*)?([\w.-]+/[\w.-]+)",
                      md.read_text(errors="ignore")[:6000])
        if not m:
            continue
        try:
            path = snapshot_download(repo_id=m.group(1),
                                     allow_patterns=["README.md", "LICENSE*"],
                                     local_dir=dest / "_upstream")
        except Exception:  # noqa: BLE001 — upstream card optional; license judge stays on local files
            return
        up = Path(path)
        for f in list(up.glob("README.md")) + list(up.glob("LICENSE*")):
            f.rename(dest / f"_upstream_{f.name}")
        shutil.rmtree(up, ignore_errors=True)
        return


def models_complete(home: Path) -> dict:
    """Per-model completeness vs the lock — a dir that exists but lacks a
    locked file is a partial download, not a prepared model."""
    lock = _load_lock()["models"]
    out = {}
    for key in ("embed", "rerank", "rerank_light"):
        spec = lock[key]
        dest = _models_dir(home) / spec["repo"].split("/")[-1]
        missing = [f for f in spec["files"] if not (dest / f).exists()]
        out[key] = {"ok": not missing, "missing": missing}
    fe = _models_dir(home) / "fastembed"
    out["sparse"] = {"ok": fe.exists() and any(fe.iterdir()), "missing": []}
    return out


def _license_ok(dest: Path) -> bool:
    for md in list(dest.glob("*.md")) + list(dest.glob("LICENSE*")):
        head = md.read_text(errors="ignore")[:6000].lower()
        if ("apache" in head or "mit license" in head or "license: mit" in head
                or "cc-by" in head):
            return True
    return any(dest.glob("LICENSE*"))


def _load_lock() -> dict:
    import json

    p = Path(__file__).parent / "models.lock.json"  # shipped inside the package
    if p.exists():
        return json.loads(p.read_text())
    raise SetupError("models.lock.json missing from package — reinstall the service")


def _cosine(q: np.ndarray, mat: dict[str, np.ndarray]) -> list[tuple[str, float]]:
    if not mat:
        return []
    ids = list(mat)
    m = np.stack([mat[i] for i in ids])
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
    q = q / (np.linalg.norm(q) + 1e-9)
    return sorted(zip(ids, (m @ q).tolist()), key=lambda t: -t[1])


def _sparse_rank(q: dict[int, float],
                 docs: dict[str, dict[int, float]]) -> list[tuple[str, float]]:
    """Score = Σ wq[i]·wd[i] over shared token indices — plain dot product."""
    if not docs or not q:
        return []
    scores = [(cid, sum(q[i] * w for i, w in d.items() if i in q))
              for cid, d in docs.items()]
    scores.sort(key=lambda t: -t[1])
    return scores


def retrieve(index: Index, query: str, embedder: LocalEmbedder,
             reranker: LocalReranker | None = None,
             sparse: LocalSparseEmbedder | None = None) -> list[dict]:
    """Dense + sparse + FTS → RRF fuse → rerank → bounded evidence."""
    chunks = index.active_chunks()
    if not chunks:
        return []

    # dense leg
    qvec = embedder.embed([query])[0]
    vecs = index.vectors_for([c["chunk_id"] for c in chunks])
    dense_rank = [cid for cid, _ in _cosine(qvec, vecs)][:TOP_K_RETRIEVE]

    # keyword leg
    fts_rank = [cid for cid, _ in index.fts_search(query)][:TOP_K_RETRIEVE]

    # sparse leg (optional — learned term weights)
    sparse_rank: list[str] = []
    if sparse is not None:
        q_sparse = sparse.embed([query])[0]
        doc_sparse = index.sparse_for([c["chunk_id"] for c in chunks])
        sparse_rank = [cid for cid, _ in
                       _sparse_rank(q_sparse, doc_sparse)][:TOP_K_RETRIEVE]

    # reciprocal-rank fusion over 2-3 legs
    rrf: dict[str, float] = {}
    for leg in (dense_rank, fts_rank, sparse_rank):
        for rank, cid in enumerate(leg):
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)
    fused = sorted(rrf, key=lambda c: -rrf[c])[:TOP_K_RETRIEVE]
    by_id = {c["chunk_id"]: c for c in chunks}
    candidates = [by_id[c] for c in fused if c in by_id]

    if reranker is not None and candidates:
        scored = candidates[:RERANK_MAX]
        scores = reranker.score(query, [c["text"] for c in scored])
        order = np.argsort(-scores)[:TOP_K_EVIDENCE]
        evidence = []
        for i in order:
            c = dict(scored[i])
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
