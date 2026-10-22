# REAL-DATA E2E REPORT — confluence-knowledge

**Stack under test**: `nomic-ai/nomic-embed-text-v1.5` (137M, 768-dim, vendor ONNX int8) + `mixedbread-ai/mxbai-rerank-xsmall-v1` (70M, vendor ONNX int8) + `Qdrant/bm25` (fastembed sparse) — all direct vendor repos, non-BAAI, license artifacts verified.

## What was actually measured (no estimates)

### Model selection matrix — real corpus, real ONNX, CPU
| embedder | params | ms/chunk | probes | ONNX provenance |
|---|---|---|---|---|
| bge-m3 (old stack) | 568M | ~600 | 3/4 | Xenova mirror — BAAI |
| granite-small-r2 | 47M | ~130 | 3/4 | onnx-community — not vendor |
| snowflake-m-v1.5 | 109M | ~160 | **1/4** | vendor |
| **nomic-v1.5** | **137M** | **~280** | **4/4** | **vendor** |
| granite-english-r2 | 149M | ~410 | 3/4 | onnx-community |

nomic was the only candidate to hit 4/4 probes (it surfaced `WAVE2_CANARY` — every other model missed it).

| reranker | params | 4×12 pair scoring | probes |
|---|---|---|---|
| bge-reranker-v2-m3 | 568M | 36.4s | 3/4 |
| **mxbai-rerank-xsmall-v1** | **70M** | **11.2s** | **3/4** |
| mxbai-rerank-base-v1 | 184M | 23.5s | 3/4 |

mxbai-xsmall: same recall at **3.25× faster** scoring.

### Real-cloud crawls (Confluence Cloud, nkunal.atlassian.net, 874-page seeded corpus)
| run | scope | reached | chunks | status |
|---|---|---|---|---|
| nomic soak, battery→AC | full 874 | 42 pages | 9,844 | interrupted (stale-daemon CPU contention found + fixed) |
| scoped subtree (`1605803`) | ~410 | 15 pages | 6,972 | stopped for workspace cleanup |
| release-gate cloud leg | full 874 | *running* | — | in progress |

Dense wave-2 pages produce ~200-250 chunks/page; thin pages ~11. `embed_ms` ≈ 0.28s/chunk on AC (nomic benchmark), 0.42s/chunk on battery (proportional duty).

### Throughput engineering (measured, this session)
- **batch sweep** (`adaptive_bench`): max_batch 8→5.7 chunks/s, 16→5.7, 32→5.3 (+345MB), 64→5.1 — ceiling set to **16** (32 was actively worse, not the request's guess)
- **dedup**: sha256 content-hash dedup — identical chunks share vectors (verified: 5 inputs → 3 unique embeds; roles never collide)
- **graph opts**: `ORT_ENABLE_ALL` on the session
- **physical cores**: `intra_op_num_threads` = physical cores (15 on i7 vs old 28-logical math)
- **chunk eval**: `CHUNK_TOKENS=800` ≈ 10% faster than 448 at equal 3/4 probe recall — **not adopted** (user required 4/4)

### Journey legs verified (real daemon HTTP, real models)
setup → doctor → daemon registration → tool discovery → connect → crawl → `wait_for_crawl` → search → `get_evidence` → `verify_citations` (honest + fabricated) → abstention → feedback → report — all green on the real Cloud space for the earlier 180-page full verification.

## Gate status
`pre_release_check.py` cloud leg — in progress at time of report.

## Known honest caveats
- Staged chunks are invisible to `search` until final atomic `publish()` — the multi-hour crawl's evidence is only searchable at completion (issue #213 tracks incremental publish)
- `~/.knowledge/models/` holds only the 4 locked models (3.8G); ~9GB of stale model dirs purged
- All 7 stale daemons + 64 temp sandboxes cleaned; watchdog supervision verified (3-fail standdown)
