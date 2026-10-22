"""Local hybrid retrieval: dense + sparse + FTS → RRF → ONNX cross-encoder.

Everything runs on this machine via ONNX Runtime — no torch, no cloud.
Models load local-only from pinned artifacts; a missing model is a setup
error, never a silent fallback.

Backend layout (all under <home>/models/):
  embed  — nomic-ai/nomic-embed-text-v1.5
           onnx/model_quantized.onnx
           (dense 768-dim, normalized, English, 8k ctx)
  sparse — Qdrant/bm25 via fastembed SparseTextEmbedding
           (learned term-weight leg — a purpose-built sparse model covering
           the lexical slot no dense ONNX export provides)
  rerank — mixedbread-ai/mxbai-rerank-xsmall-v1    onnx/model_quantized.onnx
           (sequence-classification logits → relevance score)
"""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

import numpy as np

from .index import Index
from .resources import limits, plan_batches
from .settings import SetupError

EMBED_REPO = "nomic-ai/nomic-embed-text-v1.5"  # Apache-2.0, non-BAAI
EMBED_ONNX = "onnx/model_quantized.onnx"
# nomic's quantized graph is self-contained — no external-data sibling
EMBED_ONNX_DATA = None
SPARSE_MODEL = "Qdrant/bm25"                            # MIT — via fastembed
RERANK_REPO = "mixedbread-ai/mxbai-rerank-xsmall-v1"  # Apache-2.0
RERANK_ONNX = "onnx/model_quantized.onnx"
RERANK_BASE_REPO = "mixedbread-ai/mxbai-rerank-base-v1"  # heavier override

EMBED_MODEL_ID = "nomic-embed-text-v1.5-onnx-quantized"
SPARSE_MODEL_ID = "qdrant-bm25-fastembed"
# Composite fingerprint of every model whose output is PERSISTED (dense +
# sparse vectors). The reranker is deliberately absent: it only scores at
# query time, so a reranker swap must not trigger a full rebuild (#178).
# +taskpre — nomic search_document/search_query prefixes changed the
# embed contract; every old index restamps → auto-recrawl (never
# silently reuses role-less vectors)
MODEL_MARKER = f"{EMBED_MODEL_ID}+taskpre+{SPARSE_MODEL_ID}"

TOP_K_RETRIEVE = 30
TOP_K_EVIDENCE = 8
RERANK_MAX = 25          # bounds cross-encoder latency on CPU
MIN_RERANK_SCORE = 0.1   # noise floor — mxbai-xsmall scores junk 0.02-0.06,
                         # real answers ~0.9+; 0.1 sits an order of magnitude
                         # between the bands (golden-set abstention regressed
                         # when the old 0.02 floor met the new reranker)
LOW_CONFIDENCE_SCORE = 0.3  # below this the hit is surfaced but flagged
MAX_SEARCHABLE_CHUNKS = 100_000  # whole-index search is O(n) in RAM
# Chunking is token-aware at CHUNK_TOKENS; the raised caps below are the
# safety margin for row-atomic chunks that must exceed the budget (a single
# huge table row) — 2048 stays far under the models' 8k context, bounding
# worst-case cross-encoder cost (measured: no p50 change at real chunk sizes).
EMBED_MAX_TOKENS = 2048
RERANK_MAX_TOKENS = 2048
CHUNK_TOKENS = 448       # token-aware split target — leaves room for
                         # heading path + page title inside the embed input


def _models_dir(home: Path) -> Path:
    """Where the pinned local models live. Three services share the same
    byte-identical ONNX set — a per-service copy is ~2.6 GB of duplicated
    download and disk. KNOWLEDGE_MODELS_DIR overrides; otherwise one
    shared store under the shared runtime home (#206)."""
    env = os.environ.get("KNOWLEDGE_MODELS_DIR")
    if env:
        return Path(env).expanduser()
    from .settings import shared_home
    return shared_home() / "models"


def _model_home(home: Path, repo: str) -> Path:
    return _models_dir(home) / repo.split("/")[-1]


def _sha256_ok(dest: Path, spec: dict) -> bool:
    """Verify pinned content hashes where the lock provides them —
    deletes corrupt files so a re-download/refetch actually happens.
    Without this a bad artifact passes `files_ok` forever: prepare says
    'already prepared' while _require rejects it at session open —
    an infinite failure loop."""
    import hashlib
    ok = True
    for rel, want in (spec.get("sha256") or {}).items():
        f = dest / rel
        if f.exists() and \
                hashlib.sha256(f.read_bytes()).hexdigest() != want:
            f.unlink()                     # untrusted — drop for refetch
            ok = False
    return ok


def _require(dest: Path, repo: str, cli: str, needed: list[str]) -> Path:
    if not all((dest / f).exists() for f in needed):
        raise SetupError(
            f"local model '{repo}' not prepared — run `{cli} prepare-models`. "
            "Cloud embeddings are never used.")
    spec = next((m for m in _load_lock()["models"].values()
                 if m.get("repo") == repo), {})
    if not _sha256_ok(dest, spec):
        raise SetupError(
            f"model artifact under '{repo}' failed sha256 — refetching is "
            f"safe: run `{cli} prepare-models`")
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
    """Session options for a respectful background process.

    Threads are capped at ~cores/4 per session (default) — two ORT
    sessions plus fastembed per server process, times one process per
    IDE window, used to oversubscribe every core. Spinning is disabled
    (ORT workers otherwise busy-wait between runs: audible fan), and the
    CPU memory arena is off so a transient peak isn't held for the
    process lifetime."""
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = limits().threads
    so.inter_op_num_threads = 1
    so.log_severity_level = 3  # errors only
    for key in ("session.intra_op.allow_spinning",
                "session.inter_op.allow_spinning"):
        try:
            so.add_session_config_entry(key, "0")
        except Exception:  # noqa: BLE001,S110 — older ORT lacks the key
            pass
    try:
        so.enable_cpu_mem_arena = False
        so.enable_mem_pattern = False
    except Exception:  # noqa: BLE001,S110
        pass
    return so


def _tokenizer(dest: Path):
    from tokenizers import Tokenizer
    return Tokenizer.from_file(str(dest / "tokenizer.json"))


class LocalEmbedder:
    """nomic-embed-text-v1.5 dense embeddings via ONNX int8
    (no torch). 768-dim, 8192-ctx, vendor-published artifact —
    replaces BGE-M3 under the non-BAAI workspace policy."""

    def __init__(self, home: Path, cli: str = "confluence-knowledge"):
        needed = [EMBED_ONNX, "tokenizer.json"]
        if EMBED_ONNX_DATA:
            needed.append(EMBED_ONNX_DATA)
        dest = _require(_model_home(home, EMBED_REPO), EMBED_REPO, cli,
                        needed)
        self._sess = _ort_session(dest / EMBED_ONNX, cli)
        # output dim is a session property — 768 on nomic-v1.5
        self._dim = int(self._sess.get_outputs()[-1].shape[-1] or 768)
        self._needs_token_types = any(
            i.name == "token_type_ids" for i in self._sess.get_inputs())
        self._has_sentence_emb = any(
            o.name == "sentence_embedding" for o in
            self._sess.get_outputs())
        self._tok = _tokenizer(dest)
        self._tok.no_padding()  # padding is applied per micro-batch
        self._tok.enable_truncation(EMBED_MAX_TOKENS)

    def count_tokens(self, text: str) -> int:
        """Content-token length under the model tokenizer — used for
        token-aware chunk splitting (R16) instead of word counts. Special
        tokens excluded: packers call this per unit and specials would
        overcount massively; the embed path adds them once per input."""
        return len(self._tok.encode(text, add_special_tokens=False).ids)

    def embed(self, texts: list[str],
              on_batch=None, query: bool = False) -> np.ndarray:
        """nomic-embed-text-v1.5 embeddings — the model card's task
        prefixes are required for retrieval-quality embeddings:
        documents get `search_document: `, queries `search_query: `.
        The prefix sits INSIDE the dedup key — the same raw text never
        collides across roles.

        Length-sorted micro-batches — a batch's attention cost is
        B × Lmax², so an unbounded call padded to one long outlier could
        allocate gigabytes per layer. `batch_tokens`/`max_batch` bound
        every run; results are numerically identical per item.
        `on_batch(done, total)` fires between micro-batches — the crawl
        heartbeats + reports progress through it so a multi-minute dense
        flush doesn't look like a wedged job."""
        if not texts:
            return np.zeros((0, getattr(self, "_dim", 1024)),
                            dtype=np.float32)
        # content-hash dedup — template/boilerplate strings repeat across
        # enterprise pages and embed identically; cache by sha256 so each
        # unique text pays the ONNX cost once per daemon life
        prefix = "search_query: " if query else "search_document: "
        texts = [prefix + t for t in texts]
        import hashlib
        cache = getattr(self, "_vec_cache", None)
        if cache is None:
            cache = self._vec_cache = {}
        keys = [hashlib.sha256(t.encode()).digest() for t in texts]
        miss_idx = [i for i, k in enumerate(keys) if k not in cache]
        uniq_texts, uniq_keys = [], []
        seen = set()
        for i in miss_idx:
            if keys[i] not in seen:
                seen.add(keys[i])
                uniq_texts.append(texts[i])
                uniq_keys.append(keys[i])

        if uniq_texts:
            enc = self._tok.encode_batch(uniq_texts,
                                         add_special_tokens=True)
            lim = limits()
            out = None
            batches = plan_batches([len(e.ids) for e in enc],
                                   lim.batch_tokens, lim.max_batch)
            done = 0
            for batch in batches:
                maxlen = max(len(enc[i].ids) for i in batch)
                ids = np.zeros((len(batch), maxlen), dtype=np.int64)
                mask = np.zeros((len(batch), maxlen), dtype=np.int64)
                for j, i in enumerate(batch):
                    ids[j, :len(enc[i].ids)] = enc[i].ids
                    mask[j, :len(enc[i].attention_mask)] = \
                        enc[i].attention_mask
                feed = {"input_ids": ids, "attention_mask": mask}
                # BERT-family graphs (e.g. nomic) declare a segment-ids
                # input; single-segment pairs → zeros. Feeding it
                # unconditionally is wrong too — ModernBERT exports
                # reject unknown inputs.
                if getattr(self, "_needs_token_types", False):
                    feed["token_type_ids"] = np.zeros_like(ids)
                if getattr(self, "_has_sentence_emb", True):
                    v = np.asarray(self._sess.run(
                        ["sentence_embedding"], feed)[0],
                        dtype=np.float32)
                else:
                    # nomic-style export: hidden states only → masked
                    # mean pooling (the model card's pooling, not CLS)
                    h = np.asarray(self._sess.run(
                        ["last_hidden_state"], feed)[0],
                        dtype=np.float32)
                    m = mask[:, :, None].astype(np.float32)
                    v = (h * m).sum(axis=1) / np.maximum(
                        m.sum(axis=1), 1e-9)
                if out is None:
                    out = np.empty((len(enc), v.shape[1]),
                                   dtype=np.float32)
                for j, i in enumerate(batch):
                    out[i] = v[j]
                done += len(batch)
                if on_batch is not None:
                    on_batch(done, len(enc))
            n = np.linalg.norm(out, axis=1, keepdims=True)
            out = out / (n + 1e-9)  # defensive — quantized graphs drift
            for k, v in zip(uniq_keys, out):
                cache[k] = v
        else:
            enc = []
            if on_batch is not None:
                on_batch(0, 0)
        # map every input back through the cache — duplicates share the
        # one computed vector (identical text ⇒ identical embedding)
        return np.stack([cache[k] for k in keys])


class LocalSparseEmbedder:
    """Learned term-weight sparse vectors via fastembed (ONNX, no torch).

    Fills the lexical-head role dense models expose only on torch — no
    ONNX export of that head exists, so a purpose-built sparse model covers
    the same leg.
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
                                          local_files_only=True,
                                          threads=limits().threads)
        except Exception as e:
            raise SetupError(
                f"sparse model '{SPARSE_MODEL}' not prepared — run "
                f"`{cli} prepare-models`.") from e

    def embed(self, texts: list[str], query: bool = False) -> list[dict[int, float]]:
        """One {token_idx: weight} dict per input — BM25 tf/saturation part
        (IDF is applied at query time from the index's term_stats)."""
        out = []
        for emb in self._m.embed(texts, batch_size=limits().max_batch):
            out.append(dict(zip(emb.indices.tolist(),
                                emb.values.tolist())))
        return out

    def query_embed(self, texts: list[str]) -> list[dict[int, float]]:
        """Query-side encoding — distinct semantics from document embed."""
        out = []
        for emb in self._m.query_embed(texts,
                                       batch_size=limits().max_batch):
            out.append(dict(zip(emb.indices.tolist(),
                                emb.values.tolist())))
        return out


class LocalReranker:
    """mxbai-rerank-xsmall-v1 via ONNX sequence-classification logits.

    Override with env `*_EMBED_RERANKER` = "mixedbread-ai/mxbai-rerank-base-v1"
    (heavier, stronger) for quality-first scopes — doctor surfaces the hint.
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
        """Micro-batched like the embedder — 25 pairs padded to a
        2048-token doc would otherwise allocate ~7 GB in one run."""
        if not docs:
            return np.zeros(0, dtype=np.float32)
        pairs = [[query, d] for d in docs]
        enc = self._tok.encode_batch(pairs, add_special_tokens=True)
        lim = limits()
        out = np.empty(len(enc), dtype=np.float32)
        for batch in plan_batches([len(e.ids) for e in enc],
                                  lim.batch_tokens, lim.max_batch):
            maxlen = max(len(enc[i].ids) for i in batch)
            ids = np.zeros((len(batch), maxlen), dtype=np.int64)
            mask = np.zeros((len(batch), maxlen), dtype=np.int64)
            for j, i in enumerate(batch):
                ids[j, :len(enc[i].ids)] = enc[i].ids
                mask[j, :len(enc[i].attention_mask)] = \
                    enc[i].attention_mask
            logits = self._sess.run(
                ["logits"], {"input_ids": ids, "attention_mask": mask})[0]
            s = logits.reshape(-1).astype(np.float32)
            for j, i in enumerate(batch):
                out[i] = 1.0 / (1.0 + np.exp(-s[j]))
        return out  # sigmoid → 0..1 relevance


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
            # files present AND content-verified — sha256 mismatch drops
            # the corrupt file inside _sha256_ok so files_ok re-checks fail
            files_ok = all((dest / f).exists() for f in spec["files"]) \
                and _sha256_ok(dest, spec)
            stamp = dest / ".revision"
            if files_ok and not stamp.exists():
                # pre-stamp install — verify files, then stamp in place
                stamp.write_text(spec["revision"])
            if files_ok and stamp.read_text().strip() == spec["revision"]:
                out.append(f"already prepared: {spec['repo']}")
                continue
            if stamp.exists() and stamp.read_text().strip() != spec["revision"]:
                shutil.rmtree(dest)  # wrong revision — untrusted, refetch
            # else: missing/corrupt files but right revision —
            # snapshot_download resumes/skips valid; only gaps re-download
        snapshot_download(repo_id=spec["repo"], revision=spec["revision"],
                          local_dir=dest, allow_patterns=spec["files"])
        if not _sha256_ok(dest, spec):
            # downloaded bytes failed the pinned hash — a poisoned mirror
            # or truncated transfer; one clean refetch attempt, then hard fail
            snapshot_download(repo_id=spec["repo"],
                              revision=spec["revision"], local_dir=dest,
                              allow_patterns=spec["files"])
            if not _sha256_ok(dest, spec):
                raise SetupError(
                    f"{spec['repo']} sha256 mismatch after refetch — "
                    "artifact contents differ from the pinned lock; "
                    "refusing to run unverified model bytes")
        (dest / ".revision").write_text(spec["revision"])
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

    # GC: remove model dirs not owned by the current lock — superseded
    # revisions and legacy models (e.g. the old MiniLM stack) otherwise
    # accumulate ~600 MB each in the user's home.
    owned = {lock["models"][k]["repo"].split("/")[-1]
             for k in ("embed", "rerank", "rerank_light")} | {"fastembed"}
    for d in models.iterdir():
        if d.is_dir() and d.name not in owned:
            shutil.rmtree(d, ignore_errors=True)
            out.append(f"removed stale model dir: {d.name}")
    return out


def _license_ok_upstream(dest: Path, spec: dict, snapshot_download) -> None:
    """Upstream repos sometimes drop the license line — if the card
    points at a `base_model`, fetch that model's card and judge IT. The
    upstream card is saved next to ours for audit."""
    import re
    for md in dest.glob("*.md"):
        m = re.search(r"base_model:\s*(?:-\s*)?([\w.-]+/[\w.-]+)",
                      md.read_text(errors="ignore")[:6000])
        if not m:
            continue
        try:
            path = snapshot_download(repo_id=m.group(1),  # nosec B615
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
    import os
    if os.environ.get("KNOWLEDGE_FAKE_EMBEDDINGS") == "1":
        # test-only mode — report ok with a label so status/doctor don't
        # block on model files that will never be downloaded
        return {k: {"ok": True, "missing": [], "mode": "fake-embeddings"}
                for k in ("embed", "rerank", "rerank_light", "sparse")}
    lock = _load_lock()["models"]
    out = {}
    for key in ("embed", "rerank", "rerank_light"):
        spec = lock[key]
        dest = _models_dir(home) / spec["repo"].split("/")[-1]
        missing = [f for f in spec["files"] if not (dest / f).exists()]
        wrong_rev = (dest.exists() and not missing
                     and (dest / ".revision").exists()
                     and (dest / ".revision").read_text().strip()
                         != spec["revision"])
        out[key] = {"ok": not missing and not wrong_rev,
                    "missing": missing or (["<wrong revision>"]
                                            if wrong_rev else [])}
    fe = _models_dir(home) / "fastembed"
    out["sparse"] = {"ok": fe.exists() and any(fe.iterdir()), "missing": []}
    return out


def _license_ok(dest: Path) -> bool:
    for md in list(dest.glob("*.md")) + list(dest.glob("LICENSE*")):
        # cards declare `license:` in YAML front matter OR anywhere in the
        # body — nomic's README puts it at line 2605 of 72KB; a 6KB head
        # scan missed it (CI fresh-download gate fail). Bound at 256KB —
        # model cards are text, the whole card is cheap to scan.
        head = md.read_text(errors="ignore")[:262144].lower()
        if ("apache" in head or "mit license" in head or "license: mit" in head
                or "cc-by" in head):
            return True
    return any(dest.glob("LICENSE*"))


def _load_lock() -> dict:
    import json

    p = Path(__file__).parent / "models.lock.json"  # shipped inside the package
    if p.exists():
        try:
            lock = json.loads(p.read_text())
        except json.JSONDecodeError as e:
            raise SetupError(
                f"models.lock.json is corrupt ({e}) — reinstall the "
                "service to restore the pinned model manifest") from e
        if not isinstance(lock, dict) or "models" not in lock:
            raise SetupError(
                "models.lock.json has no 'models' map — reinstall the "
                "service to restore the pinned model manifest")
        return lock
    raise SetupError("models.lock.json missing from package — reinstall the service")


def _cosine(q: np.ndarray, mat: dict[str, np.ndarray]) -> list[tuple[str, float]]:
    if not mat:
        return []
    ids = list(mat)
    # a model swap can leave mismatched-dim blobs — skip, never crash
    good = [i for i in ids if mat[i].shape == q.shape]
    if len(good) < len(ids):
        ids = good
    if not ids:
        return []
    m = np.stack([mat[i] for i in ids])
    m = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
    q = q / (np.linalg.norm(q) + 1e-9)
    return sorted(zip(ids, (m @ q).tolist()), key=lambda t: -t[1])


def _sparse_rank(q: dict[int, float], docs: dict[str, dict[int, float]],
                 df: dict[int, int] | None = None,
                 n_docs: int = 0) -> list[tuple[str, float]]:
    """BM25 score = Σ_{t ∈ q ∩ d} idf(t)·wq[t]·wd[t].

    wd is fastembed's document weight (tf saturation + length norm); idf comes
    from the index's term_stats (Robertson form, always ≥ 0). Without stats —
    e.g. an index last published before stats existed — falls back to plain
    tf weighting. Documents sharing no term with the query never rank.
    """
    if not docs or not q:
        return []

    scores = []
    for cid, d in docs.items():
        s = sum(q[i] * w * _bm25_idf(df, n_docs, i)
                for i, w in d.items() if i in q)
        if s > 0:
            scores.append((cid, s))
    scores.sort(key=lambda t: -t[1])
    return scores


def _bm25_idf(df: dict[int, int] | None, n_docs: int, t: int) -> float:
    """Robertson idf — shared by _sparse_rank and the cached sparse leg."""
    if not df or not n_docs:
        return 1.0
    d = df.get(t, 0)
    return math.log((n_docs - d + 0.5) / (d + 0.5) + 1.0)


def retrieve(index: Index, query: str, embedder: LocalEmbedder,
             reranker: LocalReranker | None = None,
             sparse: LocalSparseEmbedder | None = None) -> list[dict]:
    """Dense + sparse + FTS → RRF fuse → rerank → bounded evidence.

    Ranks over the index's per-snapshot search cache (dense matrix +
    inverted sparse index + id set) — a query costs one matmul and one
    sparse accumulate, not a whole-scope reload from SQLite.
    """
    cache = index.search_cache()
    if not cache.ids:
        return []
    if len(cache.ids) > MAX_SEARCHABLE_CHUNKS:
        raise SetupError(
            f"index holds {len(cache.ids):,} chunks — searching the whole "
            "scope at once is unsupported. Connect a narrower scope (or "
            "re-index with a smaller root).")

    # dense leg — blocked float32 matmul over the float16 matrix, so a
    # query touches ~61 MB instead of re-decoding every vector blob
    dense_rank: list[str] = []
    qvec = embedder.embed([query], query=True)[0].astype(np.float32)
    if cache.dense.shape[0] and cache.dense.shape[1] == qvec.shape[0]:
        q = qvec / (np.linalg.norm(qvec) + 1e-9)
        scores = np.empty(len(cache.ids), dtype=np.float32)
        for off in range(0, len(cache.ids), 4096):
            scores[off:off + 4096] = \
                cache.dense[off:off + 4096].astype(np.float32) @ q
        k = min(TOP_K_RETRIEVE, len(cache.ids))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        dense_rank = [cache.ids[i] for i in top]

    # keyword leg — FTS filtered by the cached active-id set
    fts_rank = [cid for cid, _ in
                index.fts_search(query, active=cache.ids_set)
                ][:TOP_K_RETRIEVE]

    # sparse leg — accumulate postings only for the query's terms
    sparse_rank: list[str] = []
    if sparse is not None:
        q_sparse = (sparse.query_embed([query])[0]
                    if hasattr(sparse, "query_embed")
                    else sparse.embed([query])[0])
        sscores = np.zeros(len(cache.ids), dtype=np.float32)
        for t, qw in q_sparse.items():
            ent = cache.sparse_idx.get(t)
            if ent is not None:
                rows, wts = ent
                sscores[rows] += np.float32(
                    qw * _bm25_idf(cache.df, cache.n_docs, t)) * wts
        nz = np.flatnonzero(sscores)
        if nz.size:
            top = nz[np.argsort(-sscores[nz])][:TOP_K_RETRIEVE]
            sparse_rank = [cache.ids[i] for i in top]

    # reciprocal-rank fusion over 2-3 legs
    rrf: dict[str, float] = {}
    for leg in (dense_rank, fts_rank, sparse_rank):
        for rank, cid in enumerate(leg):
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)
    fused = sorted(rrf, key=lambda c: -rrf[c])[:TOP_K_RETRIEVE]
    # chunk text is fetched only for fused candidates
    by_id = {c["chunk_id"]: c for c in index.chunks_by_ids(fused)}
    candidates = [by_id[c] for c in fused if c in by_id]

    if reranker is not None and candidates:
        scored = candidates[:RERANK_MAX]
        scores = reranker.score(query, [c["text"] for c in scored])
        order = np.argsort(-scores)[:TOP_K_EVIDENCE]
        evidence = []
        for i in order:
            if scores[i] < MIN_RERANK_SCORE:
                # below the noise floor the reranker is only ranking "least
                # bad" — surfacing it would ground the answer on junk
                continue
            c = dict(scored[i])
            c["rerank_score"] = float(scores[i])
            # borderline matches are surfaced but flagged — the agent can
            # caveat instead of answering on silently-weak evidence
            c["low_confidence"] = bool(scores[i] < LOW_CONFIDENCE_SCORE)
            evidence.append(c)
    else:
        evidence = [dict(c, rerank_score=None) for c in candidates[:TOP_K_EVIDENCE]]

    pages = cache.pages
    for e in evidence:
        p = pages.get(e["page_id"], {})
        e["page_title"], e["page_url"], e["level"], e["page_version"] = (
            p.get("title", ""), p.get("url", ""), p.get("level"),
            p.get("version") or p.get("fetched_at"))
    return evidence


def check_citations(evidence: list[dict], mappings: list[dict]) -> dict:
    """Verify claim→chunk citations: chunk exists in this evidence set and the
    claimed quote is literally present in it. Provenance, not entailment —
    the agent-side support check (Copilot LLM) covers entailment (R04)."""
    by_id = {e["chunk_id"]: e for e in evidence}
    bad = []
    for m in mappings:
        ch = by_id.get(m.get("chunk_id", ""))
        if ch is None:
            bad.append({"claim": m.get("claim", "")[:80], "reason": "chunk not in evidence set"})
            continue
        quote = m.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            bad.append({"claim": m.get("claim", "")[:80],
                        "reason": "quote missing — every claim needs a verbatim quote"})
        elif _norm(quote) not in _norm(ch["text"]):
            bad.append({"claim": m.get("claim", "")[:80],
                        "reason": "quote not present verbatim in cited chunk"})
    return {"ok": not bad, "violations": bad}


def _norm(s: str) -> str:
    """Typography-insensitive form for citation matching — docx extraction
    leaves smart quotes/NBSP/dashes that shouldn't fail a true quote."""
    import unicodedata
    s = unicodedata.normalize("NFKC", s)
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-"), (" ", " "), ("−", "-")):
        s = s.replace(a, b)
    return " ".join(s.split())


# ------------------------------------------------------------------
# Test-only seam — KNOWLEDGE_FAKE_EMBEDDINGS=1 swaps every model for a
# deterministic lexical stub. Lets a REAL daemon subprocess crawl and
# answer tools in tests/CI without the 1.5 GB ONNX downloads — the
# process-boundary coverage unit tests can't see (the verify_citations
# class of bugs). Model quality is irrelevant for correctness tests;
# retrieval semantics stay real.

class HashEmbedder:
    """Bag-of-words dense stub — shared terms land in shared dims, so
    cosine similarity and the whole retrieve() path behave for real."""
    dim = 384

    def __init__(self, *_a, **_k):
        pass

    def count_tokens(self, text: str) -> int:
        return max(1, len(text.split()))

    def embed(self, texts: list[str], query: bool = False) -> np.ndarray:
        import hashlib
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in text.lower().split():
                h = int.from_bytes(
                    hashlib.sha256(tok.encode()).digest()[:4], "big")
                out[i, h % self.dim] += 1.0
        n = np.linalg.norm(out, axis=1, keepdims=True)
        return out / (n + 1e-9)


class HashSparse:
    """Lexical sparse stub — {term_id: tf} with hash-derived ids."""

    def __init__(self, *_a, **_k):
        pass

    def embed(self, texts: list[str], query: bool = False) -> list[dict[int, float]]:
        import hashlib
        out = []
        for text in texts:
            vec: dict[int, float] = {}
            for tok in text.lower().split():
                tid = int.from_bytes(
                    hashlib.sha256(tok.encode()).digest()[:4], "big")
                vec[tid] = vec.get(tid, 0.0) + 1.0
            out.append(vec)
        return out


class HashReranker:
    """Lexical-overlap scorer — real ranking semantics (relevant docs
    score high, junk near zero) without a neural session."""

    def __init__(self, *_a, **_k):
        pass

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        q = set(query.lower().split())
        out = np.zeros(len(docs), dtype=np.float32)
        for i, d in enumerate(docs):
            toks = set(d.lower().split())
            if not toks:
                continue
            overlap = len(q & toks) / max(1, len(q))
            out[i] = 0.05 + 0.9 * overlap if overlap else 0.0
        return out


_FAKE = None


def fake_models() -> tuple[HashEmbedder, HashReranker, HashSparse] | None:
    """(embedder, reranker, sparse) stubs, or None when the env flag is
    unset. Callers hold the singleton — model lifecycle is a no-op."""
    global _FAKE
    import os
    if os.environ.get("KNOWLEDGE_FAKE_EMBEDDINGS") != "1":
        return None
    if _FAKE is None:
        import logging
        logging.getLogger(__name__).warning(
            "KNOWLEDGE_FAKE_EMBEDDINGS=1 — hash stubs replacing ONNX "
            "models; test-only mode, answers are lexical")
        _FAKE = (HashEmbedder(), HashReranker(), HashSparse())
    return _FAKE
