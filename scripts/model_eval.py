#!/usr/bin/env python3
"""Model-swap gate: benchmark + quality-probe embedding candidates on OUR
corpus text — not MTEB. Downloads each candidate's int8 ONNX snapshot into
a scratch dir (the real model store is untouched), embeds the same dense
chunks, and reports speed + retrieval probes.

Probes are hand-built query→relevant-chunk pairs over the synthetic dense
corpus bodies — the gate is "does the right chunk surface", the only
contract a swap must preserve.

Usage: python3 scripts/model_eval.py [--models name,name] [--pages 30]
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                     / "mcp/confluence-knowledge/scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                     / "mcp/confluence-knowledge/src"))
import seed_cloud
from confluence_knowledge.resources import plan_batches

# (key, repo, onnx rel-path, params M) — ONNX+int8 verified on the hub
CANDIDATES = [
    ("arctic-xs (default)", "Snowflake/snowflake-arctic-embed-xs",
     "onnx/model_quantized.onnx", 22),
    ("bge-m3 (old)", "Xenova/bge-m3",
     "onnx/sentence_transformers_quantized.onnx", 568),
    ("granite-small-r2", "onnx-community/granite-embedding-small-english-r2-ONNX",
     "onnx/model_quantized.onnx", 47),
    ("snowflake-m-v1.5", "Snowflake/snowflake-arctic-embed-m-v1.5",
     "onnx/model_quantized.onnx", 109),
    ("nomic-v1.5", "nomic-ai/nomic-embed-text-v1.5",
     "onnx/model_quantized.onnx", 137),
    ("granite-english-r2", "onnx-community/granite-embedding-english-r2-ONNX",
     "onnx/model_quantized.onnx", 149),
]

# (query, substring the correct page's chunks must contain)
PROBES = [
    ("What failover threshold triggers standby promotion?", "3,000"),
    ("What canary percentage goes to the blue deployment?", "10%"),
    ("Who owns escalation during a Sev-1?", "escalation"),
    ("What is the extended-corpus audit codeword?", "WAVE2_CANARY_7711"),
]


def _corpus() -> tuple[list[str], list[int]]:
    """(chunk texts, page index of each chunk) over four dense pages."""
    pages = [
        seed_cloud.prose("Failover Runbook", "the failover runbook", [
            ("The failover threshold is 3,000 milliseconds; standby "
             "promotion triggers after the third missed heartbeat."),
            "The runbook owner pages the on-call escalation lead."]),
        seed_cloud.prose("Blue Deployment Guide", "the blue deployment", [
            ("Step 3: direct 10% of traffic to the blue deployment, "
             "then ramp to 50% and 100% while error budgets hold."),
            "Rollback restores the green deployment in one action."]),
        seed_cloud.prose("Escalation Ladder", "the escalation ladder", [
            ("Sev-1 escalation: the escalation owner is the on-call "
             "incident commander; escalation moves to the duty manager "
             "at +30 minutes.")]),
        seed_cloud.long_dense_body("Extended Audit Record", sections=6)
        + seed_cloud.macro("info", "Audit codeword: WAVE2_CANARY_7711"),
    ]
    texts, owner = [], []
    for pi, body in enumerate(pages):
        c = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        words = c.split()
        step = max(120, len(words) // 8)          # ~448-token-ish chunks
        for i in range(0, len(words), step):
            texts.append(" ".join(words[i:i + step]))
            owner.append(pi)
    return texts, owner


def _load(repo: str, onnx_rel: str, root: Path):
    from huggingface_hub import snapshot_download
    dest = root / repo.split("/")[-1]
    if not (dest / onnx_rel).exists():
        print(f"  downloading {repo} …", flush=True)
        snapshot_download(repo_id=repo, local_dir=dest)
    import onnxruntime as ort
    from tokenizers import Tokenizer
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(2, (os.cpu_count() or 4) // 2)
    so.inter_op_num_threads = 1
    so.log_severity_level = 3
    sess = ort.InferenceSession(str(dest / onnx_rel), sess_options=so,
                                providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(str(dest / "tokenizer.json"))
    tok.no_padding()
    return sess, tok


def _embed(sess, tok, texts: list[str], np) -> tuple:
    """(vecs dict i→unit vec, total embed seconds)."""
    enc = tok.encode_batch(texts, add_special_tokens=True)
    lens = [len(e.ids) for e in enc]
    has_pooled = "sentence_embedding" in \
        [o.name for o in sess.get_outputs()]
    vecs, dt_total = {}, 0.0
    for batch in plan_batches(lens, 2_500_000, 6):
        maxlen = max(lens[i] for i in batch)
        ids = np.zeros((len(batch), maxlen), dtype=np.int64)
        mask = np.zeros((len(batch), maxlen), dtype=np.int64)
        for j, i in enumerate(batch):
            ids[j, :lens[i]] = enc[i].ids
            mask[j, :len(enc[i].attention_mask)] = enc[i].attention_mask
        t0 = time.monotonic()
        feed = {"input_ids": ids, "attention_mask": mask}
        feed["token_type_ids"] = np.zeros_like(ids) \
            if "token_type_ids" in [i.name for i in
                                    sess.get_inputs()] else None
        feed = {k: v for k, v in feed.items() if v is not None}
        out = sess.run(["sentence_embedding"] if has_pooled else
                       ["last_hidden_state"], feed)[0]
        dt_total += time.monotonic() - t0
        out = np.asarray(out, dtype=np.float32)
        for j, i in enumerate(batch):
            v = out[j] if has_pooled else out[j][0]   # CLS fallback
            vecs[i] = v / (np.linalg.norm(v) + 1e-9)
    return vecs, dt_total


RERANK_CANDIDATES = [
    ("bge-reranker-v2-m3 (old)", "onnx-community/bge-reranker-v2-m3-ONNX",
     "onnx/model_quantized.onnx", 568),
    ("mxbai-rerank-xsmall-v1", "mixedbread-ai/mxbai-rerank-xsmall-v1",
     "onnx/model_quantized.onnx", 70),
    ("mxbai-rerank-base-v1", "mixedbread-ai/mxbai-rerank-base-v1",
     "onnx/model_quantized.onnx", 184),
]


def _rerank(sess, tok, query: str, docs: list[str], np) -> list[float]:
    pairs = [[query, d] for d in docs]
    enc = tok.encode_batch(pairs, add_special_tokens=True)
    lens = [len(e.ids) for e in enc]
    out = np.zeros(len(enc), dtype=np.float32)
    for batch in plan_batches(lens, 2_500_000, 6):
        maxlen = max(lens[i] for i in batch)
        ids = np.zeros((len(batch), maxlen), dtype=np.int64)
        mask = np.zeros((len(batch), maxlen), dtype=np.int64)
        for j, i in enumerate(batch):
            ids[j, :lens[i]] = enc[i].ids
            mask[j, :len(enc[i].attention_mask)] = enc[i].attention_mask
        logits = sess.run(None, {"input_ids": ids,
                                 "attention_mask": mask})[0]
        s = np.asarray(logits, dtype=np.float32).reshape(len(batch), -1)
        for j, i in enumerate(batch):
            out[i] = 1.0 / (1.0 + np.exp(-s[j, -1]))
    return out.tolist()


def _rerank_eval(np):
    texts, _owner = _corpus()
    root = Path.home() / ".knowledge" / "models"
    print(f"{'reranker':<26}{'params':>7}  {'hits':>6}")
    for name, repo, rel, params in RERANK_CANDIDATES:
        try:
            sess, tok = _load(repo, rel, root)
        except Exception as e:  # noqa: BLE001
            print(f"{name:<26}{params:>6}M  SKIP — {e}")
            continue
        hits = 0
        t0 = time.monotonic()
        for q, must in PROBES:
            scores = _rerank(sess, tok, q, texts, np)
            best = max(range(len(texts)), key=lambda i: scores[i])
            hits += int(must in texts[best])
        print(f"{name:<26}{params:>6}M  {hits}/{len(PROBES)}"
              f"  ({time.monotonic() - t0:.1f}s for {len(PROBES)}×{len(texts)})")


def _chunk_size_eval(np, sizes: list[int]):
    """Same pages chunked at each token size → probes. The question:
    does a bigger chunk keep the same top-1 recall?"""
    repo, rel = ("nomic-ai/nomic-embed-text-v1.5",
                 "onnx/model_quantized.onnx")
    sess, tok = _load(repo, rel,
                      Path.home() / ".knowledge" / "models")
    print(f"{'chunk_tokens':>12}  {'chunks':>7}  {'ms/chunk':>9}  "
          f"{'probes':>7}")
    for size in sizes:
        texts, _owner = _corpus_at(max(1, int(size * 0.75)))
        vecs, _ = _embed(sess, tok, texts, np)
        hits = 0
        for q, must in PROBES:
            qv, _ = _embed(sess, tok, [q], np)
            best = max(range(len(texts)),
                       key=lambda i: float(np.dot(vecs[i], qv[0])))
            hits += int(must in texts[best])
        t0 = time.monotonic()
        _embed(sess, tok, texts[:8], np)
        dt = (time.monotonic() - t0) / min(8, len(texts))
        print(f"{size:>12}  {len(texts):>7}  {dt * 1000:>9.0f}  "
              f"{hits}/{len(PROBES)}")


def _corpus_at(step: int) -> tuple[list[str], list[int]]:
    """Same pages as _corpus, re-chunked at `step` words (~tokens*0.75)."""
    pages = [
        seed_cloud.prose("Failover Runbook", "the failover runbook", [
            ("The failover threshold is 3,000 milliseconds; standby "
             "promotion triggers after the third missed heartbeat."),
            "The runbook owner pages the on-call escalation lead."]),
        seed_cloud.prose("Blue Deployment Guide", "the blue deployment", [
            ("Step 3: direct 10% of traffic to the blue deployment, "
             "then ramp to 50% and 100% while error budgets hold."),
            "Rollback restores the green deployment in one action."]),
        seed_cloud.prose("Escalation Ladder", "the escalation ladder", [
            ("Sev-1 escalation: the escalation owner is the on-call "
             "incident commander; escalation moves to the duty manager "
             "at +30 minutes.")]),
        seed_cloud.long_dense_body("Extended Audit Record", sections=6)
        + seed_cloud.macro("info", "Audit codeword: WAVE2_CANARY_7711"),
    ]
    texts, owner = [], []
    for pi, body in enumerate(pages):
        c = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        words = c.split()
        for i in range(0, len(words), step):
            texts.append(" ".join(words[i:i + step]))
            owner.append(pi)
    return texts, owner


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=None)
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--rerank", action="store_true",
                    help="evaluate reranker candidates instead")
    ap.add_argument("--chunk-sizes", default=None,
                    help="comma list e.g. 448,800 — probe recall vs "
                         "chunk size on the current model")
    args = ap.parse_args()

    import numpy as np

    if args.rerank:
        _rerank_eval(np)
        return 0
    if args.chunk_sizes:
        _chunk_size_eval(np, [int(x) for x in
                              args.chunk_sizes.split(",")])
        return 0

    texts, owner = _corpus()
    print(f"eval corpus: {len(texts)} chunks across {max(owner) + 1} pages")

    root = Path.home() / ".knowledge" / "models"
    root.mkdir(exist_ok=True)
    print(f"{'model':<22}{'params':>7}  {'embed_s':>8}  {'ms/chunk':>9}  "
          f"{'probes':>8}")
    for name, repo, rel, params in CANDIDATES:
        if args.models and name not in args.models.split(","):
            continue
        try:
            sess, tok = _load(repo, rel, root)
        except Exception as e:  # noqa: BLE001
            print(f"{name:<22}{params:>6}M  SKIP — {e}")
            continue
        vecs, emb_s = _embed(sess, tok, texts, np)
        hits = 0
        for q, must in PROBES:
            qv, _ = _embed(sess, tok, [q], np)
            q = qv[0]
            best = max(range(len(texts)),
                       key=lambda i: float(np.dot(vecs[i], q)))
            hits += int(must in texts[best])
        print(f"{name:<22}{params:>6}M  {emb_s:>8.1f}"
              f"  {emb_s / len(texts) * 1000:>9.0f}  {hits}/{len(PROBES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
