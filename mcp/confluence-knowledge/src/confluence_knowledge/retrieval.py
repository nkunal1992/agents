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

import json
import math
import os
import platform
import shutil
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .index import Index
from .resources import _int_env, _phys_cores, limits, plan_batches
from .settings import SetupError, app_control_remediation, is_app_control_block

SPARSE_MODEL = "Qdrant/bm25"                            # MIT — via fastembed
RERANK_REPO = "mixedbread-ai/mxbai-rerank-xsmall-v1"  # Apache-2.0
RERANK_ONNX = "onnx/model_quantized.onnx"
RERANK_BASE_REPO = "mixedbread-ai/mxbai-rerank-base-v1"  # heavier override

# Per-embedder runtime spec (#249). The lock file carries the download
# spec (repo/files/sha256) per role key; this dict carries the RUNTIME
# shape — pooling, task prefixes, dims, context cap. English-only,
# non-BAAI per workspace policy. `KNOWLEDGE_EMBED_MODEL` selects;
# default is the fast tier (arctic-xs ≈ 8.6× nomic at probe parity).
EMBED_SPECS = {
    "arctic-xs": {
        "lock_key": "embed",
        "repo": "Snowflake/snowflake-arctic-embed-xs",
        "onnx": "onnx/model_quantized.onnx",
        "onnx_data": None,
        "dim": 384,
        "max_tokens": 512,
        "pool": "cls",                # model card: CLS pooling
        "doc_prefix": "",
        "query_prefix": "Represent this sentence for searching "
                        "relevant passages: ",
        "marker": "snowflake-arctic-embed-xs",
    },
    "nomic": {
        "lock_key": "embed_large",
        "repo": "nomic-ai/nomic-embed-text-v1.5",
        "onnx": "onnx/model_quantized.onnx",
        "onnx_data": None,
        "dim": 768,
        "max_tokens": 2048,
        "pool": "mean",               # masked mean over hidden states
        "doc_prefix": "search_document: ",
        "query_prefix": "search_query: ",
        "marker": "nomic-embed-text-v1.5-onnx-quantized",
    },
}
_EMBED_ALIASES = {"nomic-embed-text-v1.5": "nomic",
                  "snowflake-arctic-embed-xs": "arctic-xs",
                  "arctic-embed-xs": "arctic-xs",
                  "default": "arctic-xs", "fast": "arctic-xs",
                  "large": "nomic"}


def _env_embed_model() -> str:
    raw = os.environ.get("KNOWLEDGE_EMBED_MODEL", "arctic-xs").strip()
    key = _EMBED_ALIASES.get(raw.lower(), raw.lower())
    if key not in EMBED_SPECS:
        raise SetupError(
            f"KNOWLEDGE_EMBED_MODEL '{raw}' isn't a known embedder — "
            f"pick one of: {', '.join(sorted(EMBED_SPECS))}")
    return key


def active_embed_spec() -> dict:
    return EMBED_SPECS[_env_embed_model()]


SPARSE_MODEL_ID = "qdrant-bm25-fastembed"
# Composite fingerprint of every model whose output is PERSISTED (dense +
# sparse vectors). The reranker is deliberately absent: it only scores at
# query time, so a reranker swap must not trigger a full rebuild (#178).
# The marker rides the ACTIVE embed spec — switching models changes it,
# so old indexes flag stale and recrawl under the new model (#249).
# +taskpre is kept as a literal segment for marker compat — indexes
# stamped under nomic's taskpre contract still match when env=nomic.
MODEL_MARKER = (active_embed_spec()["marker"] + "+taskpre+" +
                SPARSE_MODEL_ID)
# Back-compat aliases — a few call sites import the name, not the spec
EMBED_REPO = active_embed_spec()["repo"]
EMBED_MODEL_ID = active_embed_spec()["marker"]
EMBED_ONNX = active_embed_spec()["onnx"]
EMBED_ONNX_DATA = active_embed_spec()["onnx_data"]

TOP_K_RETRIEVE = 30
TOP_K_EVIDENCE = 8


def _rerank_max() -> int:
    """Pairs fed to the cross-encoder per query — ~95% of search latency
    (measured ~950ms for 25 pairs on CPU, #281). 12 leaves margin above
    TOP_K_EVIDENCE for the noise-floor/abstention pass; KNOWLEDGE_RERANK_MAX
    restores the wider window where quality beats latency."""
    raw = os.environ.get("KNOWLEDGE_RERANK_MAX", "12").strip()
    try:
        v = int(raw)
    except ValueError:
        v = 12
    return max(TOP_K_EVIDENCE, min(40, v))


MIN_RERANK_SCORE = 0.1   # noise floor — mxbai-xsmall scores junk 0.02-0.06,
                         # real answers ~0.9+; 0.1 sits an order of magnitude
                         # between the bands (golden-set abstention regressed
                         # when the old 0.02 floor met the new reranker)
LOW_CONFIDENCE_SCORE = 0.3  # below this the hit is surfaced but flagged
# ColBERT MaxSim normalised per query token — a different scale from the
# sigmoid floor (junk compresses high because every token best-matches
# SOMETHING; separation is relative). Calibrated on the eval golden set
# (#329); KNOWLEDGE_COLBERT_MIN/LOW tune without a rebuild.
COLBERT_MIN_SCORE = 0.62   # sanity guard inside a verified set — the
                           # calibrated noise floor is the top-1 sigmoid
                           # check in retrieve(), not this band
COLBERT_LOW_SCORE = 0.85   # weak tail of a verified set gets flagged —
                           # real evidence sits ~0.9+, marginal ~0.8-0.85
COLBERT_REPO = "lightonai/answerai-colbert-small-v1-onnx"  # MIT export /
                                                         # Apache-2.0 base
COLBERT_ONNX = "model_int8.onnx"
COLBERT_DIM = 96


def _colbert_min() -> float:
    try:
        return float(os.environ.get("KNOWLEDGE_COLBERT_MIN",
                                    str(COLBERT_MIN_SCORE)))
    except ValueError:
        return COLBERT_MIN_SCORE


def _colbert_low() -> float:
    try:
        return float(os.environ.get("KNOWLEDGE_COLBERT_LOW",
                                    str(COLBERT_LOW_SCORE)))
    except ValueError:
        return COLBERT_LOW_SCORE
MAX_SEARCHABLE_CHUNKS = 100_000  # whole-index search is O(n) in RAM
# Chunking is token-aware at CHUNK_TOKENS; the raised caps below are the
# safety margin for row-atomic chunks that must exceed the budget (a single
# huge table row) — 1024 still leaves headroom over the 448+query shape and
# halves worst-case cross-encoder cost per pair vs 2048 (#329).
RERANK_MAX_TOKENS = 1024
def _rerank_batch_tokens() -> int:
    """Rerank-stage attention budget, read live — 12 pairs × ~578 tokens
    fits ONE ONNX run; the generic 2.5M batch_tokens splits the common
    12×470 shape 11+1 into a second forward pass (#329).
    KNOWLEDGE_RERANK_BATCH_TOKENS overrides."""
    return _int_env("KNOWLEDGE_RERANK_BATCH_TOKENS",
                    4_000_000, 500_000, 32_000_000)
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


def _pick_providers(ort) -> list[str]:
    """Execution-provider order — auto GPU when the installed wheel
    supports it (#247).

    CUDA/ROCm genuinely beat CPU for embedding models (~5-10× on dGPU).
    CoreML/DirectML are deliberately NOT auto-selected: independent
    sweeps (termite, fastembed-rb, cherry-studio, onnx_benchy) measure
    them 2-13× SLOWER than plain CPU for this model class — the
    ONNX→native bridge overhead exceeds any accelerator gain.
    `KNOWLEDGE_ONNX_PROVIDERS` overrides for power users. CPU is always
    appended last — ORT falls back per-op for nodes an EP can't run."""
    avail = ort.get_available_providers()
    env = os.environ.get("KNOWLEDGE_ONNX_PROVIDERS", "").strip()
    if env:
        picked = [p.strip() for p in env.split(",")
                  if p.strip() in avail]
        return picked or ["CPUExecutionProvider"]
    order = ("CUDAExecutionProvider", "ROCMExecutionProvider",
             "CPUExecutionProvider")
    return [p for p in order if p in avail] or ["CPUExecutionProvider"]


def active_provider() -> str:
    """Which execution provider ORT sessions will take — doctor surfaces
    it so the field can see whether a machine rode CUDA or fell back
    to CPU (#247)."""
    try:
        import onnxruntime as ort
    except ImportError:
        return "unavailable"
    return _pick_providers(ort)[0]


def _ort_session(onnx_path: Path, cli: str, threads: int | None = None):
    try:
        import onnxruntime as ort
    except ImportError as e:
        if is_app_control_block(e):
            raise SetupError(app_control_remediation("onnxruntime")) from e
        raise SetupError("onnxruntime isn't installed — reinstall the "
                         f"service ({cli} bootstrap reinstalls deps)") from e
    try:
        return ort.InferenceSession(
            str(onnx_path), providers=_pick_providers(ort),
            sess_options=_ort_opts(ort, threads))
    except (OSError, RuntimeError) as e:
        if is_app_control_block(e):
            raise SetupError(app_control_remediation("onnxruntime")) from e
        raise


def _ort_opts(ort, threads: int | None = None):
    """Session options for a respectful background process.

    Threads are capped at ~two-thirds physical cores per session (default)
    — two ORT sessions plus fastembed per server process, times one process
    per IDE window, used to oversubscribe every core. Spinning is disabled
    (ORT workers otherwise busy-wait between runs: audible fan), and the
    CPU memory arena is off so a transient peak isn't held for the
    process lifetime."""
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.intra_op_num_threads = threads or limits().threads
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
    """Dense embeddings via ONNX (no torch) — model resolved from
    `KNOWLEDGE_EMBED_MODEL` through EMBED_SPECS (#249): default
    snowflake-arctic-embed-xs (22M, 384-dim, CLS-pooled, English-only);
    `nomic` opts back into the 137M/768-dim masked-mean variant."""

    def __init__(self, home: Path, cli: str = "confluence-knowledge"):
        spec = self._spec = active_embed_spec()
        needed = [spec["onnx"], "tokenizer.json"]
        if spec["onnx_data"]:
            needed.append(spec["onnx_data"])
        dest = _require(_model_home(home, spec["repo"]), spec["repo"], cli,
                        needed)
        self._sess = _ort_session(dest / spec["onnx"], cli)
        self._dim = int(self._sess.get_outputs()[-1].shape[-1]
                        or spec["dim"])
        self._needs_token_types = any(
            i.name == "token_type_ids" for i in self._sess.get_inputs())
        self._has_sentence_emb = any(
            o.name == "sentence_embedding" for o in
            self._sess.get_outputs())
        self._tok = _tokenizer(dest)
        self._tok.no_padding()  # padding is applied per micro-batch
        self._tok.enable_truncation(spec["max_tokens"])

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
        # spec resolves per-instance at __init__; tests that build the
        # object via __new__ (fake sessions) fall back to the active one
        spec = getattr(self, "_spec", None) or active_embed_spec()
        prefix = (spec["query_prefix"] if query
                  else spec["doc_prefix"])
        if prefix:
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
                    h = np.asarray(self._sess.run(
                        ["last_hidden_state"], feed)[0],
                        dtype=np.float32)
                    if spec["pool"] == "cls":
                        # arctic-embed: CLS pooling — first token's
                        # hidden state carries the sequence embedding
                        v = h[:, 0, :].astype(np.float32)
                    else:
                        # nomic: masked mean over hidden states
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


class SparseUnavailable(SetupError):
    """The sparse leg can't run on this platform (#287) — e.g. fastembed's
    `py_rust_stemmers` dep has no win_arm64 wheel. Distinct from
    SetupError so callers can degrade to dense+rerank instead of failing
    the crawl/search."""


def sparse_platform_excluded() -> bool:
    """Platforms where the pip marker skips fastembed entirely (#302) —
    its absence there is the expected wheel gap, not a broken install.
    Keep 1:1 with the marker in pyproject.toml [models]."""
    return sys.platform == "win32" and platform.machine() == "ARM64"


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
            if is_app_control_block(e):
                raise SetupError(
                    app_control_remediation("fastembed")) from e
            if getattr(e, "name", None) != "fastembed" or \
                    sparse_platform_excluded():
                # a missing transitive dep (py_rust_stemmers etc.) — or
                # fastembed itself where the marker skips it (#302) —
                # means the platform can't build it: degrade honestly,
                # never tell the user to reinstall a wheel that doesn't
                # exist
                raise SparseUnavailable(
                    f"sparse leg unavailable: {e.name or e} can't load on "
                    "this platform — dense+rerank legs still used") from e
            raise SetupError("fastembed isn't installed — reinstall the "
                             f"service ({cli} bootstrap reinstalls deps)") from e
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        dest = _models_dir(home) / "fastembed"
        try:
            import onnxruntime as _ort
            providers = _pick_providers(_ort)
        except ImportError:
            providers = None        # ORT absent → fastembed defaults
        try:
            kwargs = {"cache_dir": str(dest), "local_files_only": True,
                      "threads": limits().threads}
            if providers:
                kwargs["providers"] = providers
            self._m = SparseTextEmbedding(SPARSE_MODEL, **kwargs)
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


def load_sparse_or_none(home: Path,
                        cli: str = "confluence-knowledge"
                        ) -> LocalSparseEmbedder | None:
    """Sparse embedder when the platform can run it, else None (#320).

    SparseUnavailable only fires by design (platform-excluded wheels —
    #287/#302); a broken install on a supported platform still raises
    SetupError, so catching here can't mask a real fault. Callers pass
    the None straight to retrieve()/upsert — the degraded path."""
    try:
        return LocalSparseEmbedder(home, cli)
    except SparseUnavailable:
        return None


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
        # rerank is foreground query latency (unlike background embed) —
        # its session takes all physical cores for the ~ms burst (#329)
        self._sess = _ort_session(dest / RERANK_ONNX, cli,
                                  threads=min(16, _phys_cores()))
        self._tok = _tokenizer(dest)
        self._tok.no_padding()
        self._tok.enable_truncation(RERANK_MAX_TOKENS)
        self.repo = repo
        self.last: dict = {}            # call stats — see score() (#329)

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        """Micro-batched like the embedder — 25 pairs padded to a
        2048-token doc would otherwise allocate ~7 GB in one run."""
        if not docs:
            return np.zeros(0, dtype=np.float32)
        pairs = [[query, d] for d in docs]
        enc = self._tok.encode_batch(pairs, add_special_tokens=True)
        lim = limits()
        lens = [len(e.ids) for e in enc]
        batches = plan_batches(lens, _rerank_batch_tokens(), lim.max_batch)
        out = np.empty(len(enc), dtype=np.float32)
        t0 = time.monotonic()
        for batch in batches:
            maxlen = max(lens[i] for i in batch)
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
        # call stats — bench_query/doctor read the batch-split and token
        # shape behind a rerank leg without re-deriving them (#329)
        self.last = {"pairs": len(enc), "runs": len(batches),
                     "batch_sizes": [len(b) for b in batches],
                     "tok_min": min(lens), "tok_med":
                     sorted(lens)[len(lens) // 2], "tok_max": max(lens),
                     "ms": round((time.monotonic() - t0) * 1000, 1)}
        return out  # sigmoid → 0..1 relevance


class ColbertUnavailable(SetupError):
    """The late-interaction scorer can't load (#329) — missing int8
    artifacts or a native-load block. Callers degrade to the
    cross-encoder; the search still answers."""


class LocalColbert:
    """answerai-colbert-small-v1 INT8 ONNX — late interaction (#329).

    Doc token vectors are encoded once at index time (doc_tokvecs, fp16
    [L,96]); a query costs one tiny encode + MaxSim matmuls — tens of ms
    vs ~420ms for the 12-pair cross-encoder pass it replaces. The
    encoding contract (markers, length caps, punctuation skiplist) rides
    the artifact's own config_sentence_transformers.json so an export
    swap can't silently drift the pipeline. Scores are per-query-token
    mean max-sim → ~[0,1]; `KNOWLEDGE_COLBERT=0` disables.
    """

    def __init__(self, home: Path, cli: str = "confluence-knowledge"):
        repo = COLBERT_REPO
        try:
            dest = _require(_model_home(home, repo), repo, cli,
                            [COLBERT_ONNX, "tokenizer.json",
                             "config_sentence_transformers.json"])
        except SetupError as e:
            raise ColbertUnavailable(str(e)) from e
        try:
            cfg = json.loads(
                (dest / "config_sentence_transformers.json").read_text(
                    encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ColbertUnavailable(
                f"colbert config unreadable under '{repo}' — refetch via "
                f"`{cli} prepare-models`") from e
        # doc encodes are background batch work; query encodes are
        # foreground bursts — the foreground thread profile serves both
        self._sess = _ort_session(dest / COLBERT_ONNX, cli,
                                  threads=min(16, _phys_cores()))
        self._tok = _tokenizer(dest)
        self._tok.no_padding()
        self._qmark = int(cfg.get("query_prefix_id", 1))
        self._dmark = int(cfg.get("document_prefix_id", 2))
        self._q_len = int(cfg.get("query_length", 32))
        self._d_len = int(cfg.get("document_length", 300))
        self._dim = int(cfg.get("embedding_dim", COLBERT_DIM))
        self._cls = self._tok.token_to_id("[CLS]")
        self._sep = self._tok.token_to_id("[SEP]")
        if self._cls is None or self._sep is None:
            raise ColbertUnavailable(
                f"colbert tokenizer under '{repo}' lacks [CLS]/[SEP] — "
                "artifact mismatch, refetch via prepare-models")
        skip = {self._cls, self._sep, self._qmark, self._dmark,
                int(cfg.get("pad_token_id", 0))}
        for w in cfg.get("skiplist_words", []):
            skip.update(self._tok.encode(w, add_special_tokens=False).ids)
        self._skip = skip
        self._in_names = {i.name for i in self._sess.get_inputs()}
        self._out_name = self._sess.get_outputs()[0].name
        # signature stamped on persisted token vectors — a repo/revision/
        # dim change makes stored rows silently incompatible (#329)
        rev = _load_lock()["models"].get("colbert", {}).get("revision", "?")
        self.sig = f"{repo}@{rev}:{self._dim}"
        self.last: dict = {}            # call stats — see score() (#329)

    def _encode(self, texts: list[str], marker: int, cap: int
                ) -> list[tuple[np.ndarray, list[int]]]:
        """Per-token fp32 mats + their token ids — [(L_i,dim), ids]."""
        seqs: list[list[int]] = []
        for t in texts:
            core = self._tok.encode(t, add_special_tokens=False).ids
            seqs.append([self._cls, marker] + core[:cap - 3] + [self._sep])
        lens = [len(s) for s in seqs]
        out: list[tuple[np.ndarray, list[int]]] = [None] * len(seqs)
        lim = limits()
        for batch in plan_batches(lens, lim.batch_tokens, lim.max_batch):
            maxlen = max(lens[i] for i in batch)
            ids = np.zeros((len(batch), maxlen), dtype=np.int64)
            mask = np.zeros((len(batch), maxlen), dtype=np.int64)
            for j, i in enumerate(batch):
                ids[j, :lens[i]] = seqs[i]
                mask[j, :lens[i]] = 1
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._in_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            emb = self._sess.run([self._out_name], feeds)[0]
            for j, i in enumerate(batch):
                m = emb[j, :lens[i]].astype(np.float32)
                n = np.linalg.norm(m, axis=1, keepdims=True)
                out[i] = (m / (n + 1e-9), seqs[i])
        return out

    def encode_query(self, text: str) -> np.ndarray:
        """[Lq,96] fp32 — every query token participates (no skiplist)."""
        return self._encode([text], self._qmark, self._q_len)[0][0]

    def encode_docs(self, texts: list[str]) -> list[np.ndarray]:
        """[L_i,96] mats for persistence — markers/special/punctuation
        rows dropped per the export's skiplist."""
        out = []
        for mat, ids in self._encode(texts, self._dmark, self._d_len):
            keep = [i for i, t in enumerate(ids) if t not in self._skip]
            out.append(mat[keep].astype(np.float16)
                       if keep else np.zeros((0, self._dim), np.float16))
        return out

    def score(self, query: str | np.ndarray,
              doc_mats: list[np.ndarray]) -> np.ndarray:
        """MaxSim per doc normalised by query length → ~[0,1]."""
        q = (self.encode_query(query) if isinstance(query, str)
             else np.asarray(query, dtype=np.float32))
        t0 = time.monotonic()
        out = np.zeros(len(doc_mats), dtype=np.float32)
        for i, dm in enumerate(doc_mats):
            if dm.shape[0]:
                out[i] = (q @ dm.astype(np.float32).T).max(axis=1).sum()
        out /= max(1, q.shape[0])
        self.last = {"docs": len(doc_mats), "qtok": int(q.shape[0]),
                     "ms": round((time.monotonic() - t0) * 1000, 1)}
        return out


def colbert_disabled() -> bool:
    return os.environ.get("KNOWLEDGE_COLBERT", "1").strip().lower() in \
        ("0", "false", "off", "no")


def load_colbert_or_none(home: Path,
                         cli: str = "confluence-knowledge"
                         ) -> LocalColbert | None:
    """Colbert scorer when artifacts exist, else None (#329) — mirrors
    load_sparse_or_none: degrade to the cross-encoder, never crash."""
    if colbert_disabled():
        return None
    try:
        return LocalColbert(home, cli)
    except (ColbertUnavailable, SetupError):
        return None


def backfill_tokvecs(index: Index, colbert_factory,
                     batch: int = 48) -> int:
    """Encode active chunks missing token vectors (#329).

    Runs on index.POST_PUBLISH's idle-time thread; the cross-encoder
    covers whatever coverage hasn't arrived yet. `colbert_factory` is a
    callable so the model loads lazily on the first backfill rather
    than at warm-up. One pass per index at a time — concurrent
    publishes re-check the live missing set per iteration, so a second
    caller just exits."""
    lock = getattr(index, "_tokvec_lock", None)
    if lock is None:
        lock = index._tokvec_lock = threading.Lock()
    if not lock.acquire(blocking=False):
        return 0
    # Writes MUST go through a dedicated connection owned by this thread —
    # teardown paths that close index.db directly (bypassing Index.close's
    # warm-thread join) would free the shared handle mid-execute (#329 —
    # segfaulted the suite twice).
    wdb = None
    swept = False
    try:
        enc = None
        n = 0
        while True:
            # abort between batches — a closing/torn-down index must not
            # hold Index.close()'s warm-thread join (#257/#329)
            if index._closed or index._readonly or \
                    not index._path.exists():
                return n
            if enc is None:
                enc = colbert_factory()
            if enc is None:                # degraded — cross-encoder covers
                return n
            # sig-aware: rows under a different model count as missing
            # so a colbert upgrade re-encodes on the next publish
            missing = index.tokvecs_missing(batch, enc.sig)
            if not missing:
                return n
            if wdb is None:
                wdb = index._open_rw_conn()
            if not swept:
                index.gc_tokvecs(conn=wdb)
                swept = True
            mats = enc.encode_docs([t for _c, t in missing])
            index.put_tokvecs({c: m for (c, _t), m in zip(missing, mats)},
                              sig=enc.sig, conn=wdb)
            n += len(missing)
    finally:
        if wdb is not None:
            wdb.close()
        lock.release()


def prepare_models(home: Path) -> list[str]:
    """Download pinned ONNX artifacts once (public; no document text sent)."""
    from huggingface_hub import snapshot_download

    out = []
    lock = _load_lock()
    models = _models_dir(home)
    models.mkdir(parents=True, exist_ok=True)

    # dense + rerankers: direct HF snapshot (repo + revision pinned in the
    # lock); rerank_light is fetched too so *_RERANKER overrides always work.
    # The dense role fetches the ACTIVE spec — embed (arctic-xs default)
    # or embed_large (KNOWLEDGE_EMBED_MODEL=nomic) — not both (#249).
    embed_key = active_embed_spec()["lock_key"]
    for key in (embed_key, "rerank", "rerank_light", "colbert"):
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
    if sparse_platform_excluded():
        # marker-skipped at install (#302) — nothing can ever consume this
        # download on this platform, so don't fetch or fail on it
        out.append(f"skipped {spec['repo']} — no sparse wheel on this "
                   "platform (dense+rerank still prepared)")
    else:
        try:
            from fastembed import SparseTextEmbedding
            SparseTextEmbedding(spec["repo"],
                                cache_dir=str(models / "fastembed"))
            out.append(f"prepared {spec['repo']} ({spec['license']})")
        except Exception as e:
            raise SetupError(
                f"couldn't fetch sparse model {spec['repo']}: {e}") from e

    # GC: remove model dirs not owned by the current lock — superseded
    # revisions and legacy models (e.g. the old MiniLM stack) otherwise
    # accumulate ~600 MB each in the user's home. The inactive embed spec
    # is deliberately NOT owned: leaving it means every arctic→nomic
    # switcher carries a stale 137M dir forever; opting back re-downloads.
    owned = {lock["models"][k]["repo"].split("/")[-1]
             for k in (embed_key, "rerank", "rerank_light", "colbert")} \
        | {"fastembed"}
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
                for k in ("embed", "rerank", "rerank_light", "sparse",
                          "colbert")}
    lock = _load_lock()["models"]
    embed_key = active_embed_spec()["lock_key"]
    out = {}
    for key in (embed_key, "rerank", "rerank_light", "colbert"):
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
    if sparse_platform_excluded():
        # pip marker-skips fastembed here (#302) — absent is expected
        out["sparse"] = {"ok": True, "missing": [],
                         "mode": "platform-degraded"}
    else:
        fe = _models_dir(home) / "fastembed"
        out["sparse"] = {"ok": fe.exists() and any(fe.iterdir()),
                         "missing": []}
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
             sparse: LocalSparseEmbedder | None = None,
             allow_chunks: set[str] | None = None,
             colbert: LocalColbert | None = None) -> list[dict]:
    """Dense + sparse + FTS → RRF fuse → rerank → bounded evidence.

    Ranks over the index's per-snapshot search cache (dense matrix +
    inverted sparse index + id set) — a query costs one matmul and one
    sparse accumulate, not a whole-scope reload from SQLite.

    `allow_chunks` restricts candidates to a subset — the #240
    descendent-alias filter: a virtual scope reads its ancestor's index
    through its own subtree's chunk set, so sibling pages never bleed
    into evidence.
    """
    cache = index.search_cache()
    if not cache.ids:
        return []
    if allow_chunks is None:
        allowed = cache.ids
        pos = None                    # identity — whole cache
    else:
        allowed = [c for c in cache.ids if c in allow_chunks]
        pos = np.asarray([cache.row[c] for c in allowed],
                         dtype=np.int64)
    if not allowed:
        return []
    over_cap = len(allowed) > MAX_SEARCHABLE_CHUNKS
    if over_cap and not index.ann_available():
        raise SetupError(
            f"index holds {len(allowed):,} chunks — searching the whole "
            "scope at once is unsupported. Connect a narrower scope (or "
            "re-index with a smaller root).")
    ann = over_cap or cache.dense.shape[0] == 0

    # dense leg — at scope sizes the vec_chunks ANN table answers top-k
    # directly; under it the blocked float32 matmul over the float16
    # matrix stays (exact, and the matrix is already resident)
    dense_rank: list[str] = []
    qvec = embedder.embed([query], query=True)[0].astype(np.float32)
    if ann:
        allow_set = None if allow_chunks is None else set(allowed)
        dense_rank = [cid for cid, _s in index.ann_topk(
            qvec, TOP_K_RETRIEVE, allowed=allow_set)]
        dense = np.zeros((0, 0), dtype=np.float16)
    else:
        dense = cache.dense if pos is None else cache.dense[pos]
    if dense.shape[0] and dense.shape[1] == qvec.shape[0]:
        q = qvec / (np.linalg.norm(qvec) + 1e-9)
        scores = np.empty(len(allowed), dtype=np.float32)
        for off in range(0, len(allowed), 4096):
            scores[off:off + 4096] = \
                dense[off:off + 4096].astype(np.float32) @ q
        k = min(TOP_K_RETRIEVE, len(allowed))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        dense_rank = [allowed[i] for i in top]

    # keyword leg — FTS filtered by the cached active-id set; subtree
    # scopes over-fetch inside fts_search so in-scope hits aren't starved
    # by higher-ranked out-of-scope siblings (#281)
    fts_active = cache.ids_set if allow_chunks is None else set(allowed)
    fts_rank = [cid for cid, _ in
                index.fts_search(query, active=fts_active)
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
        if pos is not None:
            nz = np.intersect1d(nz, pos, assume_unique=True)
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

    if (reranker is not None or colbert is not None) and candidates:
        # ColBERT scores the whole fused pool — extra rows are ~free
        # (stored mats + matmul); the cross-encoder is bounded by the
        # per-pair cost, so its pool always stays capped at
        # _rerank_max(), including when it covers a partial-coverage
        # fallback.
        pool = candidates if colbert is not None else []
        scored = candidates[:_rerank_max()]
        # ColBERT late interaction (#329): MaxSim over persisted token
        # vectors — only when EVERY scored candidate has coverage; a
        # mixed score scale can't compare, so partial coverage falls
        # back to the cross-encoder for the whole call.
        tmats = (index.tokvecs_for([c["chunk_id"] for c in pool],
                                   colbert.sig)
                 if colbert is not None else {})
        if colbert is not None and pool and len(tmats) == len(pool):
            cscores = colbert.score(
                query, [tmats[c["chunk_id"]] for c in pool])
            order = np.argsort(-cscores)
            # MaxSim scores aren't a calibrated floor — junk compresses
            # high because every token best-matches SOMETHING, and the
            # real/junk bands overlap (measured on the golden set). The
            # noise floor therefore stays on the cross-encoder, verified
            # against the top late-interaction pick only — one pair ≈
            # ~35ms vs ~35ms×12 for the full pass.
            verified = (
                reranker.score(query, [pool[int(order[0])]["text"]])[0]
                >= MIN_RERANK_SCORE
            ) if reranker is not None else True
            # Evidence membership is the UNION of colbert's top-K and the
            # fusion legs' top-K (#336): token-wise MaxSim dilutes the
            # discriminating tokens of fact-binding questions ("which team
            # owns service-2") in keyword-dense corpora — the fused hits
            # that dense+FTS agreed on stay in the window. Display order
            # follows the fused rank: multi-leg consensus first.
            picks = {int(i) for i in order[:TOP_K_EVIDENCE]}
            picks.update(range(min(TOP_K_EVIDENCE, len(pool))))
            evidence = []
            for i in sorted(picks):
                # below the noise floor the scorer is only ranking "least
                # bad" — surfacing it would ground the answer on junk
                if not verified or cscores[i] < _colbert_min():
                    continue
                c = dict(pool[i])
                c["rerank_score"] = float(cscores[i])
                c["scorer"] = "colbert"
                # borderline matches are surfaced but flagged — the agent
                # can caveat instead of answering on silently-weak evidence
                c["low_confidence"] = bool(cscores[i] < _colbert_low())
                evidence.append(c)
        elif reranker is not None:
            scores = reranker.score(query, [c["text"] for c in scored])
            order = np.argsort(-scores)[:TOP_K_EVIDENCE]
            evidence = []
            for i in order:
                if scores[i] < MIN_RERANK_SCORE:
                    # below the noise floor the reranker is only ranking
                    # "least bad" — surfacing it would ground the answer
                    # on junk
                    continue
                c = dict(scored[i])
                c["rerank_score"] = float(scores[i])
                c["scorer"] = "cross-encoder"
                # borderline matches are surfaced but flagged — the agent
                # can caveat instead of answering on silently-weak evidence
                c["low_confidence"] = bool(scores[i] < LOW_CONFIDENCE_SCORE)
                evidence.append(c)
        else:
            evidence = [dict(c, rerank_score=None)
                        for c in candidates[:TOP_K_EVIDENCE]]
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


class HashColbert:
    """Late-interaction stub — per-token one-hot hash vectors + cosine
    MaxSim (#329). A shared word lands on an identical row in both mats,
    so a query token's best doc match is exactly 1.0 when the word is
    present. Scores are mapped onto the real MaxSim band — any overlap
    lands at ≥0.7 (over COLBERT_MIN like the real scorer's relevant
    hits) and zero overlap stays 0 — so fake-embeddings runs keep the
    same abstention semantics as the real models AND stay off the ONNX
    artifact entirely."""

    sig = "hash-fake:96"
    dim = 96

    def __init__(self, *_a, **_k):
        self.last: dict = {}

    def _tokvec(self, tok: str) -> np.ndarray:
        import hashlib
        h = int.from_bytes(
            hashlib.sha256(tok.encode()).digest()[:4], "big")
        v = np.zeros(self.dim, np.float32)
        v[h % self.dim] = 1.0
        return v

    def encode_docs(self, texts: list[str]) -> list[np.ndarray]:
        out = []
        for t in texts:
            toks = t.lower().split()
            m = (np.stack([self._tokvec(w) for w in toks]) if toks
                 else np.zeros((0, self.dim), np.float32))
            out.append(m.astype(np.float16))
        return out

    def encode_query(self, text: str) -> np.ndarray:
        return self.encode_docs([text])[0].astype(np.float32)

    def score(self, query, doc_mats: list[np.ndarray]) -> np.ndarray:
        import time as _t
        q = (self.encode_query(query) if isinstance(query, str)
             else np.asarray(query, np.float32))
        t0 = _t.monotonic()
        out = np.zeros(len(doc_mats), np.float32)
        for i, dm in enumerate(doc_mats):
            if not dm.shape[0]:
                continue
            sims = (q @ dm.astype(np.float32).T).max(axis=1)
            matched = int((sims > 0.5).sum())
            if matched:
                out[i] = 0.7 + 0.29 * matched / max(1, q.shape[0])
        self.last = {"docs": len(doc_mats), "qtok": int(q.shape[0]),
                     "ms": round((_t.monotonic() - t0) * 1000, 1)}
        return out


_FAKE = None


def fake_models() -> tuple[HashEmbedder, HashReranker, HashSparse,
                           HashColbert] | None:
    """(embedder, reranker, sparse, colbert) stubs, or None when the env
    flag is unset. Callers hold the singleton — model lifecycle is a
    no-op."""
    global _FAKE
    import os
    if os.environ.get("KNOWLEDGE_FAKE_EMBEDDINGS") != "1":
        return None
    if _FAKE is None:
        import logging
        logging.getLogger(__name__).warning(
            "KNOWLEDGE_FAKE_EMBEDDINGS=1 — hash stubs replacing ONNX "
            "models; test-only mode, answers are lexical")
        _FAKE = (HashEmbedder(), HashReranker(), HashSparse(),
                 HashColbert())
    return _FAKE
