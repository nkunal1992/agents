# RENAUDAT AUDIT — mcp/confluence-knowledge

**Branch**: `feature/211-real-data-e2e` (+ `bug-hunt/edge-cases` merged in)
**Scope**: static analysis + unit-level fault injection only; no live DB, no full crawls
**Model stack audited**: `nomic-ai/nomic-embed-text-v1.5` + `mixedbread-ai/mxbai-rerank-xsmall-v1` + `Qdrant/bm25` (fastembed)

---

## Verdict summary

| Pillar | Verdict | Found | Fixed |
|---|---|---|---|
| 1. Supply chain & provenance | **FIXED** | sha256 heal-loop defect; stale BAAI/granite residue in docs/scripts | both |
| 2. SQLite WAL concurrency | **CLEAN** | 0 real | — |
| 3. Tokenizer & vector edges | **FIXED + ENHANCED** | empty-table chunk, prefix contract gap | both |
| 4. Daemon lifecycle & supervision | **FIXED** | stale-PID SIGTERM, mid-stop resurrection | both |
| 5. Failure injection / chaos | **FIXED** | poisoned-RPC crash loop, corrupt-lock traceback, perms | all |

**Total: 6 real defects found, 6 patched with regression tests. 369+8 tests green.**

---

## PILLAR 1 — Supply chain, provenance & licences

### Verified
- All model repos are **direct vendor**: `nomic-ai`, `mixedbread-ai`, `Qdrant` — no mirrors, no BAAI, no `onnx-community`/`xenova` provenance anywhere in the lock
- Revision pinning + `.revision` stamp; wrong-revision dirs are `rmtree`'d and refetched
- `pyproject.toml` deps are all bounded ranges (`>=x,<y`) — no floating `latest`/`*`
- Licence gate: `_license_ok` refuses to ship without an artifact

### Defect fixed — sha256 heal loop
`_require` verified hashes at session-open but `prepare_models` never checked them — a corrupt artifact got "already prepared" forever while every session failed. Infinite failure loop.

**Fix**: `_sha256_ok()` verifies + **deletes** corrupt files; `prepare_models` checks it on the fast-path and re-downloads + reverifies after fetch; hard `SetupError` if the refetched bytes still mismatch.

Tests: `test_corrupt_artifact_refetched_not_stuck`, `test_still_bad_after_refetch_is_a_hard_fail`, `test_require_purges_mismatch_so_prepare_refetches`.

### Residue sweep
Stale `bge-m3`/`granite`/`onnx-community` references removed across: `retrieval.py`/`feedback.py` docstrings, `docs/*.md` (model names, dims 384→768, ~52MB→~137MB, provenance corrections), `agents/confluence-knowledge.agent.md`, `bundle/README.md`, all three services' e2e scripts (dir-name checks → `nomic-embed-text-v1.5`), `mcp/confluence-knowledge/README.md`.

## PILLAR 2 — SQLite WAL concurrency

### Verified clean
- `_LockedRetry` wraps writes (BEGIN IMMEDIATE, 6-try backoff) — the `database is locked` retry was proven to retry then surface cleanly
- Every `self.db.execute` outside a txn was audited: the 9 flagged sites are inside `_LockedRetry`, a post-connect probe, or docstring-enforced "must run inside a transaction" helpers (`_purge_dead_stage`, `_rebuild_term_stats`) called from `publish`'s txn
- `_SearchCache` epoch-swap: checked-and-built off the WAL reader, atomic attribute swap under `self.lock` — a mid-build publish keeps the fresher cache
- `cancel_crawl` propagates `Cancelled` through context managers → rollback → locks released

## PILLAR 3 — Tokenizer & vector edges

### Implemented — nomic task prefixes
`embed(..., query=False)`: documents get `search_document: `, queries `search_query: ` (inside the dedup key — no role collision). `MODEL_MARKER` gained `+taskpre` → every existing index restamps → auto-recrawl, never silently reuses role-less vectors. Call sites: `retrieval.py:583` (search), `server.py:905`/`988` (feedback), crawl flushes stay document-side.

Verified on the real model: same text produces different unit-norm vectors per role; dedup cache holds 2 entries for `x` doc + `x` query.

### Defect fixed — empty-table chunks
A gridless `<table>` returned `""` → `split_table_text("")` → `['']` → an empty `Chunk` with a junk [CLS,SEP] vector into the index. Guarded (`if not t: continue`).

### Verified clean
- Masked-mean zero-length: `np.maximum(…,1e-9)` denominator + dedicated all-pad test
- >8192-token inputs: `EMBED_MAX_TOKENS=2048` truncation bound — chunk budget 448 + title/headroom ≪ cap
- Non-ASCII/CJK/emoji: tokenizer + BeautifulSoup str handling; no byte-level slicing
- Empty/whitespace pages: `flush()` strips + early-returns

### Noted (intentional, not fixed)
- `_vec_cache` grows unbounded (~1.5KB/unique text — ~260MB at 175k chunks on the dense corpus). Acceptable inside a daemon already holding the search cache; flagged for a future cap if corpora grow 10×.

## PILLAR 4 — Daemon lifecycle & supervision

### Defects fixed (bug-hunt branch, merged)
- **Stale-PID SIGTERM**: `stop_daemon`'s `.daemon.pid` fallback could kill a pid-reused innocent process — cmdline verified before signalling on BOTH the file path and lease path
- **Mid-stop resurrection**: `ensure_daemon` unlinked `.watchdog.stop` unconditionally — a request landing mid-stop re-raised the pair. Now only an aged (>5s) stop-file clears; the watchdog re-checks before respawning

### Verified
- 3-failure respawn limit → watchdog exits (test: `test_watchdog_stands_down_after_three_spawn_failures`)
- `status` payload surfaces `stop_cmd` + `cancel_crawl_tool` (off-switches discoverable)
- Mutual supervision is intentional design (IDE tools die without the daemon); `stop`/`cancel_crawl` are the control channels

## PILLAR 5 — Failure injection / chaos

### Defects fixed
- **Poisoned-RPC crash loop**: `poll_requests`' `json.loads` inside the txn rolled back the claim AND killed the daemon → watchdog respawn → crash again on the same row, forever. Now the corrupt row gets an error response in-transaction; good rows dispatch.
- **Corrupt `models.lock.json`**: truncated JSON → raw `JSONDecodeError` traceback. Now `SetupError` with "reinstall the service".

### Verified
- `database is locked` → `_LockedRetry` retries then raises `OperationalError` cleanly
- chmod-000 index dir → surfaces a named error, not silent creation
- 429/`Retry-After` honoured (seconds + HTTP-date); 5xx/transport share the bounded retry budget (`client.py`)
- `DaemonUnavailable` → `{"_error": "daemon unavailable — retry in a few seconds"}` — graceful, actionable

---

## Commits on `feature/211-real-data-e2e`
- `bcfb226` — status surfaces `stop_cmd`/`cancel_crawl_tool`
- `921414e` — bug-hunt: stale-PID SIGTERM, mid-stop race, empty-table chunks
- (this sweep — sha256 heal, prefixes, chaos tests)

## Deferred / noted
- `_vec_cache` soft-cap — intentional for now, revisit if corpora grow
- >2048-token single chunks truncate silently (documented `EMBED_MAX_TOKENS` trade)
- `search_document:`/`search_query:` prefix adoption restamps all indexes — users re-crawl once
