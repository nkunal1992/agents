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
    ("bge-m3", "Xenova/bge-m3",
     "onnx/sentence_transformers_quantized.onnx", 568),
    ("granite-english-r2", "onnx-community/granite-embedding-english-r2-ONNX",
     "onnx/model_quantized.onnx", 149),
    ("granite-small-r2", "onnx-community/granite-embedding-small-english-r2-ONNX",
     "onnx/model_quantized.onnx", 47),
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
        out = sess.run(["sentence_embedding"] if has_pooled else
                       ["last_hidden_state"],
                       {"input_ids": ids, "attention_mask": mask})[0]
        dt_total += time.monotonic() - t0
        out = np.asarray(out, dtype=np.float32)
        for j, i in enumerate(batch):
            v = out[j] if has_pooled else out[j][0]   # CLS fallback
            vecs[i] = v / (np.linalg.norm(v) + 1e-9)
    return vecs, dt_total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=None)
    ap.add_argument("--pages", type=int, default=30)
    args = ap.parse_args()

    import numpy as np

    texts, owner = _corpus()
    print(f"eval corpus: {len(texts)} chunks across {max(owner) + 1} pages")

    root = Path.home() / ".confluence-knowledge" / "models"
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
