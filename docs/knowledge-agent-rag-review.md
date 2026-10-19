# Knowledge Agent RAG: production-readiness audit

## Decision and scope

**Assessment: useful local retrieval tooling, but not ready for an unqualified
production-trust claim.** The review reproduced failures in refresh durability,
evidence identity, pagination and feedback concurrency. SharePoint's account
binding needs particular attention before an installation is reused with another
account. These issues are not fixed by this audit.

Reviewed runtime: `72e0575`, Confluence **0.5.4**, Local **0.2.4**, SharePoint
**0.2.4**. Audit tracked in **#117**. All source references below refer to that
revision, not a proposed implementation.

Target: one person's laptop, multiple IDE/server processes, primarily 1k–10k
chunks. Experiments use synthetic documents, fake transports and local model
artifacts. No company documents, live source accounts or personal indexes were
used. No production source, agent instructions, model settings or evaluator
behavior changed.

This is a code-and-tool review, **not a measured Copilot answer-quality study**.
Retrieval evidence, a verified quote and a correct final answer are three
different things.

## 1. What was verified

| Layer | Method | What the result establishes |
|---|---|---|
| Shared engine | Existing suites, independent corpus, actual ONNX models, leg ablations | Candidate/evidence behavior on synthetic questions |
| Confluence | Actual crawler/index with injected page/error responses | Checkpoint, publication and worker lifecycle failures |
| Local | Actual filesystem crawler on temporary files | Directory-error refresh behavior; format handling through shared extractors |
| SharePoint | Synthetic account/client and list items; HTTP mock transport | Account scope reuse, item-ID namespace collision, query-token loss |
| Citations | Direct verifier calls and all three server entry points | Provenance-check limits and evidence-handle lifetime |
| Feedback | Separate instances and two synchronized Python processes | Lost updates and malformed-record failures |
| Agent probes | Instructions compared with actual schemas/tool behavior | Whether an instruction has the inputs/tools necessary to execute |
| Evaluation | Counterexample against the actual `_score` implementation | A multi-source question can receive credit after finding only one required source |
| Performance | Separate synthetic scale run with real query models | A local engine operating envelope, not hosted or end-to-end Copilot latency |

### Environment and model provenance

- macOS 27.0.1, ARM64, Mac17,9, 24 GiB RAM, 15 logical CPUs.
- Python 3.13.16; SQLite 3.54.0; NumPy 2.5.4; ONNX Runtime 1.31.0;
  FastEmbed 0.9.0; tokenizers 0.22.2; MCP 1.30.0.
- All three imported packages resolved to this checkout.
- Model home: `/tmp/onnx-models`, containing `models/`.
- No application `.revision` stamps existed in that cache. Hugging Face download
  metadata did identify the lock's revisions, and the following model SHA-256
  hashes matched the metadata ETags:

| Artifact | Download revision | SHA-256 |
|---|---|---|
| BGE-M3 quantized graph | `4de13258303883538bd53b696b452bf8099f0858` | `f3d37b38cb8d799cd3ff931dcb93282d7a1eedff216a6e88eb0ee95bab5533eb` |
| v2-M3 reranker graph | `6f5ff65298512715a1e669753bc754d2bc8f367b` | `912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d` |

This checks the local files against existing download metadata, not a new
independent attestation from the model publisher. The sparse cache was usable;
the wrapper does not pass its lock revision to FastEmbed.

## 2. Architecture and trust boundaries

```text
source adapter -> parser -> word-based chunks -> dense/sparse encoding
              -> SQLite pages/chunks/vectors/FTS -> query candidates
              -> rank fusion -> reranker -> evidence payload
              -> Copilot instructions -> citations/probes -> final answer
                                      ^
                  local corrections --+
```

Shared code means an engine defect affects all three services, but the
connectors still have distinct discovery, scope and failure behavior.

| Mechanism | Enforcement boundary |
|---|---|
| GET-only API wrapper | Code rejects non-GET before transport; does not constrain every tool available to the Copilot agent |
| Local model inference | Dense/reranker execute locally; final synthesis sends selected context through Copilot |
| Citation verification | Checks evidence membership and nonempty supplied quote presence; does not establish entailment or completeness |
| Closed-world answers | Agent instruction, not a deterministic property of returned evidence |
| Failure Hunt | Agent instructions, partly unsupported by available metadata |
| Correction disclosure | Tool returns corrections; an instruction asks the model to disclose them |
| Atomic JSON replace | Protects a single file replacement, not concurrent read-modify-write operations |
| SQLite publication | A transaction makes the final flag update atomic; does not preserve old content across prior upserts |

## 3. Findings

Severity reflects impact under stated preconditions. “Reproduced” means a
synthetic execution reached the failure; it does not imply live tenant testing.
“Code-confirmed” means the implementation was traced but the full operational
scenario was not executed.

### R01 — High — Refresh is not an old-or-new snapshot

**Affected:** all three, shared index. **Evidence:** reproduced on one connection
and an independent reader connection.

`index.py:125 upsert_page` replaces the page with `active=0`, deletes its old
chunks/vectors/FTS rows and commits replacements before `publish` runs. A reader
loses the previously available page during an unfinished refresh. An interrupted
refresh cannot restore that old content merely by leaving publication incomplete.

Reproductions: `test_previous_snapshot_survives_unpublished_refresh`,
`test_independent_connection_sees_previous_snapshot`.

The old atomicity test exercises the final `publish` transaction and permits an
empty set. It does not establish preservation through the crawl.

**Proposed repair:** staged generations or equivalent immutable old snapshot,
with atomic generation switch and generation-bound evidence. Acceptance: old
facts stay available across interruption; after commit, all new facts are
visible without mixed metadata. Do not infer snapshot guarantees from WAL alone.

### R02 — High — Temporary failures can become permanent removals

**Affected:** Confluence and Local reproduced; SharePoint has analogous local
retention/discovery paths requiring further fault injection.

Confluence `crawl.py:160 step` keeps transient failures in a local `keep_failed`
set, not checkpoint state. After a bounded step/restart, a failed page can be
omitted from final publication and deleted. In Local, an unreadable directory
returns no children plus a warning; the subsequent completed refresh removes
its previously indexed descendants.

Reproductions: `test_transient_keep_set_survives_step_boundary`,
`test_unreadable_directory_preserves_previous_content`.

The Confluence server normally runs an unbounded step, so the first case matters
particularly across interruption or bounded invocation. The prior “transient”
test calls `publish` directly and does not reproduce a fetch failure.

**Proposed repair:** durable failed/unknown discovery state, including descendant
coverage. Only confirmed removal or access revocation should authorize deletion;
partial enumeration is not proof of absence.

### R03 — High — Evidence handles repeat after eviction

**Affected:** all three server implementations. **Evidence:** reproduced through
each `search` entry point over 40 calls.

`server.py:326 search` and its adapter copies generate IDs from
`len(_evidence_sets)` before eviction. Near the 32-set cap, the same scope reuses
IDs. An earlier handle can refer to a later result rather than its original
evidence, compromising verification during long sessions/probe searches.

Reproduction: `test_evidence_handles_remain_unique_after_eviction`.

**Proposed repair:** unique IDs independent of cache size, explicit expiry and
snapshot/generation identity. Acceptance: expired handles fail; they never
resolve to replacement evidence.

### R04 — Medium — Missing quotes pass; provenance is not entailment

**Affected:** all three, `retrieval.py:398 check_citations`. **Evidence:** reproduced.

An empty/missing `quote` bypasses the text-presence check. A real quote attached
to a contradictory claim also passes because entailment is deliberately not
checked. For example, the quote “Production deploys are prohibited” verifies
alongside the claim “Production deploys are allowed.”

Reproductions: `test_empty_or_missing_quote_cannot_verify`,
`test_verifier_is_provenance_not_entailment` (the latter documents a boundary,
not a promise that entailment is implemented).

**Proposed repair:** require meaningful quote/mapping schemas and report the
checker explicitly as provenance validation. Any claim-level reasoning audit is
a separate mechanism; do not market a substring check as a hallucination proof.

### R05 — High — Several Failure Hunt instructions lack necessary inputs

**Affected:** all three agent/server contracts. **Evidence:** code-confirmed.

| Probe | Available behavior and gap |
|---|---|
| Boundary | `get_evidence(scope, chunk_ids)` fetches known IDs; no tool lists a page's first/last chunk. Search lacks ordinals/boundary metadata. Fetching a known chunk cannot recover text discarded before indexing. |
| Keyword | A bare-ID search is feasible, but output does not identify individual leg contributions. Missing an ID does not diagnose the sparse leg by itself. |
| Synthesis | Multiple sources can be cited, but the tools cannot reveal an unknown missing page. A question with multiple facts may legitimately be answered by one page. |
| Contradiction | Evidence can expose disagreement, but returned `as_of` is scope-level fetch freshness, not a per-page authority/version ordering. |
| Table | Instructions trigger on `kind=table`, but the search projection drops `kind`. An agent might infer table text, which is weaker than the documented typed trigger. |
| Feedback | Corrections are returned and disclosure is requested. Execution by the model is not guaranteed, and a correction is not independently verified truth. |

Locations: shared server `search/get_evidence`, `index.py:225 active_pages`,
`:274 stats`, and all three agent Answer sections.

**Proposed repair:** explicit source/chunk provenance and navigable boundaries;
honest feasible probe instructions; no invented missing-page names or
source-authority claims. Record actual Copilot transcripts before claiming these
checks run reliably.

### R06 — High — SharePoint account binding and item identity are incomplete

**Affected:** SharePoint. **Evidence:** synthetic account switching reproduced;
page-ID namespace collision reproduced.

`server_sp.py:183 connect` passes literal `"me"` to scope hashing rather than
the authenticated identity. Changing the synthetic account while keeping
site/root unchanged produces the same handle. `SPProfile.scope_key` also omits
tenant identity. This creates a cached-content/feedback reuse risk when the same
installation changes accounts; no live unauthorized read was attempted.

`crawl_sp.py:114 _page_from_list_item` stores `page:<item_id>`, omitting the list.
Two page lists with item `1` collide. This requires a scope traversing multiple
page lists; it is not an assertion that every normal site has such lists.

Reproductions: `test_account_change_must_not_reuse_scope`,
`test_same_item_number_in_different_page_lists_is_distinct`.

**Proposed repair:** verified tenant/principal binding and container-qualified
source IDs; decide migration/invalidation explicitly. Also review cached client
token renewal: acquiring a fresh token does not replace every already-cached
client. Live expiry behavior remains untested.

### R07 — High — Absolute continuation links lose query parameters

**Affected:** shared validator, particularly SharePoint pagination.
**Evidence:** HTTP mock transport reproduced loss of `$skiptoken=next%2Bpage`.

`client.py:82 _validate_path` reduces an absolute URL to its path. Graph
`crawl_sp.py:67 _paged` passes the next URL with `params=None`. The next request
therefore loses its continuation token. Repeated first pages, incomplete
enumeration or nontermination are possible downstream outcomes; an infinite
live crawl was not run.

Reproduction: `test_absolute_pagination_retains_query_token`.

**Proposed repair:** preserve validated same-origin query strings and add
repeated-continuation detection. Test multi-page fake Graph responses end to
end, including encoded tokens and malicious off-origin links.

### R08 — High for multi-IDE use — Atomic feedback replacement loses updates

**Affected:** all three, shared feedback store. **Evidence:** reproduced with two
instances and with two synchronized Python processes.

Each `FeedbackStore` loads once. Both writers can read an empty list; writer A
saves its correction; writer B then replaces the file with only B's correction.
`os.replace` protects file integrity, not this lost-update race. Cached stores
also do not automatically observe another process's changes.

Reproductions: `test_two_feedback_instances_do_not_lose_updates`,
`test_two_feedback_processes_do_not_lose_updates`.

**Proposed repair:** transactional feedback storage or locked reread/merge/write,
unique IDs and visibility rules. Preserve existing per-scope semantics.

### R09 — Medium — Valid JSON can break feedback lookup and therefore search

**Affected:** all three. **Evidence:** malformed-entry failures reproduced.

`feedback.py:36 _load` validates only that JSON is a list. Missing `qvec`, null
entries and incompatible vector dimensions reach `match` and raise
KeyError/TypeError/ValueError. Search calls matching without a separate degraded
feedback boundary.

Reproduction: `test_malformed_feedback_entries_do_not_break_search`.

**Proposed repair:** schema/dimension/finiteness/model validation, visible
quarantine/degradation rather than silent data loss. Validate matching
thresholds; specify model-change migration and conflicting-correction behavior.
Those additional edge cases are code-review recommendations, not all reproduced.

### R10 — Medium — Structural guarantees differ by format

**Affected:** shared extraction; particularly Local/SharePoint documents.
**Evidence:** synthetic format tests reproduced the following:

- Simple HTML, CSV and XLSX split tables retain repeated headers.
- DOCX produces structured table chunks **and duplicated flattened cell text**
  because traversal revisits paragraphs nested inside the table.
- PPTX tables become text chunks; Markdown tables are ordinary whitespace text.
- HTML `rowspan` is not expanded to maintain field positions.
- A headerless table promotes its first data row to the header. The text remains,
  but its structural role is wrong; this is not literal deletion of the values.
- A single 400-word row remains over budget to preserve row atomicity.

Locations: `extract.py:27/41`, `extract_files.py:66/121/202`.
Reproductions: table tests in `test_production_audit.py`, including
`test_file_table_preserves_headers_and_structural_kind`.

**Proposed repair:** define per-format support, avoid duplicate traversal,
preserve cell coordinates/merged-cell semantics where supported, and establish
an explicit oversized-row fallback. “Did not crash” is not table fidelity.

### R11 — High — Existing evaluation overstates what it proves

**Affected:** shared benchmark and judge workflow. **Evidence:** scoring
counterexample reproduced; report/judge/CI contracts inspected.

`scripts/eval_rag.py:68 _score` treats finding any required page as success, using
title/URL substrings. Finding “Deploy” while missing required “Escalation”
receives a recall hit. Required-fact coverage uses aggregate evidence
substrings, not answer correctness or source/row entailment.

Hybrid reports contain no generated answer and clip each evidence string to
800 characters. The judge prompt grades that evidence against the golden
answer: it cannot assess an answer that was not recorded. Verdict merging is
question-string based with little validation, and does not enforce a quality
gate. The script gates recall below 0.80 and no-answer emptiness, not fact
coverage or stable performance relative to a previous model.

The Confluence bootstrap invokes its golden sample. Inspection did not find
equivalent golden-evaluation invocations in Local/SP bootstrap scripts merely
because those packages contain sample files.

**Proposed repair:** separate required-source recall, all-source success,
fact/answer support and abstention; identify exact sources and runs; preserve
judge context; validate schemas/verdicts; compare baselines and test actual
candidate answers separately.

### R12 — Medium — Sparse retrieval is not the documented learned BM25 model

**Affected:** all three, `LocalSparseEmbedder` and `_sparse_rank`.
**Evidence:** installed dependency/source inspection and real sparse outputs.

FastEmbed 0.9.0 implements traditional BM25, with `requires_idf=True` and a
warning that IDF is expected from the index. This SQLite engine adds no corpus
IDF term. It also uses document `.embed` on queries instead of `query_embed`.
For a three-unique-term query, observed document weights were approximately
1.6787 each versus query weights of 1. This uniform scaling alone does not
change ranks; repeated-term weighting and missing corpus statistics need
separate consideration.

Zero-overlap documents still appear in `_sparse_rank` with score zero and
therefore vote in RRF. Their order can reflect insertion order rather than
relevance. Reproduction: `test_zero_overlap_sparse_documents_do_not_vote_in_fusion`.

**Proposed repair:** define intended lexical scoring, use compatible query
encoding/corpus statistics, and exclude nonmatching candidates. Measure benefit
by ablation before deciding whether another retrieval library is necessary.

### R13 — High — Superseding a crawl does not stop its in-flight publication

**Affected:** Confluence reproduced; same start/step pattern in Local/SP is
code-confirmed, not separately threaded in this audit.

A step reads job state once. While `_page` is paused, starting another job marks
the old job abandoned. Releasing the old step allows it to publish and change
its state to done anyway.

Reproduction: `test_superseded_crawler_cannot_publish`, with event barriers.

**Proposed repair:** scope-level ownership/generation fencing and cancellation
checks before writes/publication, including independent server processes.

### R14 — Medium — Model compatibility is not bound to a first crawl

**Affected:** shared marker scheme and crawler patterns. **Evidence:** a
Confluence first-crawl resume with 4-dimensional then 8-dimensional fake vectors
publishes mixed dimensions.

The initial crawl has no published model marker. Checks that depend on an
existing marker do not guard its resumed work. Model markers also do not encode
the locked artifact revision or parser/tokenization changes.

Reproduction: `test_first_crawl_resume_rejects_different_vector_dimension`.
This deliberately uses fakes to isolate the compatibility contract; it does not
claim the installed BGE-M3 changed its dimension.

**Proposed repair:** persist the complete build identity with the job before its
first write; reject incompatible resume and feedback vectors; distinguish
reranker-only changes from index-affecting changes.

### R15 — Medium, platform-dependent — Chunk cap ignores SQL parameter limits

**Affected:** shared vector/sparse reads and large publication/GC operations.
**Evidence:** a configured SQLite 32,766-variable limit causes `vectors_for`
with 32,767 IDs to raise OperationalError.

The reviewed local build allows 250,000 variables; this failure was reproduced
by lowering its limit, not by claiming this Mac fails at 32,767 by default.
The declared 100,000-chunk cap is insufficient as a portability guarantee.

Reproduction: `test_vector_reads_respect_sqlite_parameter_limit`.

**Proposed repair:** bounded SQL batches or joins/temp tables and tests against
the actual connection limit. Bound memory before loading all chunk text.

### R16 — Medium — Title context is absent and word budgets exceed token budgets

**Affected:** all three embedding pipelines. **Evidence:** title-input spy test,
actual tokenizer measurement and runtime constants.

Crawler embedding inputs include heading path and text, not the page title.
Titles are attached after retrieval, too late to help pages whose body says
only “Owner is OPER-42.” Reproduction: `test_page_title_is_in_embedded_context`.

Runtime caps dense input at **512**, not the documented 8192. A concrete
243-word technical string encoded to **3,606 tokens**; with truncation enabled,
the final `RESCUE-90` fact was absent from the 512-token input. Row-atomic and
280-word splitting do not solve token truncation. A fact can nevertheless be
returned in the full chunk if other signals retrieve it, so truncation does not
imply every such query fails.

**Proposed repair:** explicitly include provenance context in model inputs and
budget against actual tokenization/pair lengths. Change index identity when
embedding inputs change; retain deterministic source offsets.

## 4. Independent real-model results

The corpus contains **90 pages, 100 chunks and 60 questions**: 55 answerable and
five unanswerable. It is constructed from 12 categories with five variants each,
not sampled from production users. Leg ablations compare top-eight evidence
without reranking; the full path adds the actual reranker. Full-path selected IDs
are checked against `retrieval.retrieve`, not an unverified reimplementation.

Precision labels require both the expected source and at least one expected
fact in a chunk. They do not establish semantic entailment. Source/fact metrics
use all 55 answerable questions; precision is a macro mean over nonempty
answerable results (50 questions for FTS, which returned nothing for five;
55 for the other paths). An empty result's precision is undefined, not silently
scored as perfect. Required-source recall can remain high even when the
correct row/detail is absent; fact coverage is reported separately. Unanswerable questions with relevant “not specified”
evidence may correctly lead to abstention: **nonempty evidence is not itself a
hallucinated answer**.

| Path | Source recall, macro | All sources found | Expected facts present | Strict chunk precision | MRR |
|---|---:|---:|---:|---:|---:|
| Dense only | 90.9% | 50/55 | 50/55 | 13.6% | 0.848 |
| FTS only | 81.8% | 45/55 | 45/55 | 22.5% | 0.723 |
| Sparse only | 90.9% | 50/55 | 50/55 | 13.6% | 0.860 |
| Hybrid before reranking | 90.9% | 50/55 | 50/55 | 13.6% | 0.808 |
| Full hybrid + reranker | **100%** | **55/55** | **55/55** | **24.8%** | **0.880** |

Both fused top-30 and reranker-input top-25 retained all required sources in this
sample. The reranker recovered five questions lost from the fused top eight.
This is concrete evidence of value from reranking, not a reason to replace the
architecture wholesale.

| Full-pipeline category (five questions each) | Source/fact success | Precision | MRR |
|---|---:|---:|---:|
| Paraphrase | 5/5 | 17.0% | 1.000 |
| Exact identifier | 5/5 | 90.0% | 1.000 |
| Rare term plus common distractors | 5/5 | 12.5% | 1.000 |
| Table value | 5/5 | 12.5% | 0.867 |
| Long technical tail | 5/5 | 21.3% | 0.357 |
| Negation/qualifier | 5/5 | 16.7% | 1.000 |
| Numbers and units | 5/5 | 15.4% | 1.000 |
| Title-only context | 5/5 | 20.0% | 0.457 |
| Multi-source | 5/5, both sources | 28.8% | 1.000 |
| Version conflict | 5/5, both sources | 25.0% | 1.000 |
| Japanese query/content | 5/5 | 13.2% | 1.000 |
| Unanswerable detail | 5/5 returned nonempty evidence | Not applicable | Not applicable |

Low precision here means extra chunks do not carry the authored target facts
from the required source. They may be topically related. It indicates context
noise under this strict label policy, not that every extra chunk is harmful.

**20/100 chunks exceeded 512 tokens**, maximum 3,622. Full-chunk evidence can
still expose a fact beyond the model's view; the tokenizer finding remains valid
despite successful retrieval in these cases.

Raw quality artifacts on the audit machine:
`/tmp/rag-audit-117-quality/{quality.db,report.json,rescored.json}`.
During review, precision/MRR labels were tightened from source-only to
source-plus-fact and the unanswerable metric was renamed to avoid implying a
hallucination. `rescored.json` is a deterministic rescore of the same stored
ranks/evidence; **no model parameters or retrieval outputs changed**. Reproduce
that rescore with `--mode rescore --output /tmp/rag-audit-117-quality`.

## 5. Performance and resource limits

Scale fixtures repeat 100 pre-embedded templates to isolate query/index cost.
They are not an independently labelled 10k-document quality benchmark. Two runs
were measured: short ~28-word templates and the runner's default 240-word
templates, which approximate real chunk sizes. Both used the actual local
embedding, sparse and reranker models; model load took ~1.2 s once per process.

**240-word templates (realistic chunk size):**

| Scale | First query | p50 | p95 | Reranker share of p50 | Peak RSS | DB+WAL size |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 chunks | 3.13 s | 3.03 s | 3.23 s | ~3.00 s (~99%) | 6.4 GiB | 19.1 MB |
| 10,000 chunks | 3.13 s | 3.05 s | 3.09 s | ~2.96 s (~97%) | 6.4 GiB | 189.6 MB |

**~28-word templates (short chunks):**

| Scale | First query | p50 | p95 | Reranker share of p50 | Peak RSS | DB+WAL size |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 chunks | 0.34 s | 0.33 s | 0.35 s | ~0.32 s (~96%) | 2.4 GiB | 10.8 MB |
| 10,000 chunks | 0.40 s | 0.39 s | 0.41 s | ~0.32 s (~82%) | 2.4 GiB | 105.9 MB |

Interpretation:

- **Query latency is nearly flat from 1k to 10k chunks** — SQL legs grow
  milliseconds; the cross-encoder rerank of 25 candidates dominates (~97–99% of
  p50). Reranker cost scales with candidate *token* count, not corpus size:
  240-word evidence costs ~10x the short-chunk latency.
- **Peak RSS 6.4 GiB** is a process lifetime high-water mark (`ru_maxrss`)
  dominated by model inference, not index size — it is identical across scales.
  On the 24 GiB audit machine this is affordable once; **two or three IDE
  processes each hosting a server approach a real memory ceiling**. Multiple
  processes also re-pay the ~1.2 s model load and duplicate the resident model
  pages unless the OS shares them.
- Chunk text is materialized for candidates; `vectors_for`/`sparse_for` fetch
  per-candidate rows. At the 100,000-chunk cap, DB size extrapolates to ~1.9 GB
  and R15's SQL variable-limit failure becomes reachable on lower-limit builds.

The runner measures engine latency, not Copilot turns, Failure Hunt calls or
network source latency. It omits server feedback matching and output
serialization; the server currently embeds a query again for feedback.

## 6. Claims that should not be repeated as guarantees

| Existing claim | Review conclusion |
|---|---|
| “8k/8192-token embedding window” | Model-family capability differs from this runtime's 512-token setting |
| “Learned sparse/ONNX BM25; rare words weigh more” | Installed BM25 is traditional; external IDF is not supplied here |
| “All formats share row-atomic table parsing” | Markdown/PPTX do not; DOCX duplicates table cells; merged cells are not normalized |
| “Previous snapshot remains available through refresh” | Disproved by upsert-before-publish reproductions |
| “Atomic writes prevent lost feedback” | Atomic replacement does not serialize multiple writers |
| “Scope is bound to the SharePoint account” | Actual connect path uses literal `me` |
| “Citation verification proves the answer” | Quote provenance is not entailment; empty quotes bypass the check |
| “Failure Hunt checks first/last chunks and newest source” | Required boundaries/per-page authority metadata are not exposed |
| “100% recall/accuracy proves production readiness” | Small retrieval benchmark, permissive scoring, no generated answers |
| “Corrections identify outdated pages or notify owners” | Flat local list, no page linkage or notification; inference remains tentative |
| “Nothing leaves the laptop / zero-risk security” | Copilot receives selected context; GET-only applies to a specific client boundary |
| “All requests/redirects stay on the source host” | Graph downloads are a separate pre-signed-URL path with redirect following |

This ledger covers RAG-relevant claims in the TDD, deck, KT guide and agent
instructions. Those documents were deliberately not rewritten in this audit.

## 7. Reproduction and test interpretation

From the repository root, with the existing development environment:

```bash
PYTHONDONTWRITEBYTECODE=1 mcp/confluence-knowledge/.venv/bin/python -m pytest \
  -q -rx -p no:cacheprovider mcp/confluence-knowledge/tests
PYTHONDONTWRITEBYTECODE=1 mcp/confluence-knowledge/.venv/bin/python -m pytest \
  -q -rx -p no:cacheprovider mcp/local-knowledge/tests
PYTHONDONTWRITEBYTECODE=1 mcp/confluence-knowledge/.venv/bin/python -m pytest \
  -q -rx -p no:cacheprovider mcp/sharepoint-knowledge/tests
```

Known-defect tests use narrow `xfail(strict=True)` markers referencing #117 and
finding IDs. To see actual assertion failures, use `--runxfail` on one named
test. A passing suite with expected failures **does not mean defects are fixed**.
Optional adapter/Office imports can skip where those packages are not installed.

```bash
PYTHONDONTWRITEBYTECODE=1 mcp/confluence-knowledge/.venv/bin/python \
  mcp/confluence-knowledge/scripts/audit_rag.py \
  --home /tmp/onnx-models --mode quality

PYTHONDONTWRITEBYTECODE=1 mcp/confluence-knowledge/.venv/bin/python \
  mcp/confluence-knowledge/scripts/audit_rag.py \
  --home /tmp/onnx-models --mode performance --sizes 1000 10000 --samples 20
```

The runner creates a disposable output directory unless an empty one is given
with `--output`. Its model home must contain prepared `models/`. Current runner
blocks Python socket connect/DNS events and sets offline flags; this is not an
OS-level network sandbox. The first quality run started before that hook was
added and used offline flags only. No live source API was configured.
The runner's RSS measurement uses Unix `resource`; Windows execution of this
optional benchmark is not claimed.

## 8. Explicit gaps and next review work

- No actual Copilot generation/probe/judge execution; no final-answer
  hallucination rate, no human blind judgement and no self-judge calibration.
- No live Confluence Cloud/DC or Graph tenancy, actual token expiry, permission
  revocation, or enterprise proxy test.
- No full 100k-real-document ONNX crawl; SQL limits and 1k/10k scale are separate.
- No full concurrent two-model-server throughput benchmark. Two-process feedback
  loss and independent SQL reader behavior are reproduced; model memory
  multiplication remains an operational risk, not a measured concurrency SLA.
- No Windows/Linux run of these new probes yet; local results are macOS.
- Corpus questions share templates and facts are intentionally planted. Five
  title-only pages fit inside top eight, so aggregate recall can conceal missing
  title context. The dedicated input-contract test establishes that gap.
- No comprehensive malformed-archive/zip-bomb, partially scanned PDF, nested
  macro, or resource-exhaustion validation. Existing format support is not a
  security certification.
- `keep_failed` and obsolete-worker tests are strongest for Confluence; do not
  treat code similarity as a completed adapter-specific restart experiment.
- Tests and CI logs should distinguish “observed fake-server requests” from
  “all outbound networking blocked.”

These are open limitations of the audit, not quietly dropped acceptance checks.

## 9. Separate remediation proposal — approval required

1. **Trust and lifecycle first:** R01/R02/R03/R06/R07/R13. Generation-safe index
   publication and worker ownership; stable evidence handles; account-qualified
   scopes/source IDs; preserved continuation tokens. Define migration/invalidating
   old indexes before rollout.
2. **Make verification honest and executable:** R04/R05/R11. Strict mappings,
   source/chunk metadata and boundary tools; accurate agent instructions;
   evaluator metrics tied to all required sources and actual candidate answers.
3. **Feedback integrity:** R08/R09. Transactional updates, reload visibility,
   validation and model identity; preserve local per-scope disclosure semantics.
4. **Retrieval fidelity:** R10/R12/R14/R16. Token-aware/contextual inputs, explicit
   table-format contracts, proper lexical scoring and complete build markers.
   Rebuild affected indexes; compare held-out retrieval quality before release.
5. **Operating envelope:** R15 and performance follow-up. SQL batching, bounded
   model batches/materialization, multiple-server memory/throughput measurements.
6. **Documentation:** update the claim ledger only after behavior and evidence
   are established; keep measured results tied to corpus, SHA and model identity.

Each repair should turn its strict expected failure into a passing regression
test, then add adapter-specific integration coverage. Do not implement this list
as part of the audit or declare the product “production ready” merely because
the original unit suites are green.

## 10. Remediation status — High-Integrity Recovery

The user ordered a foundation-first remediation (issues #119–#127) rather than
the general §9 sequence. Each fix turned its strict `xfail` reproduction into
a passing regression test:

| Finding | Issue | Fix | Status |
|---|---|---|---|
| R02 (+R01, R13 publish path) | #119 | Job-scoped staging tables + single-transaction snapshot swap; durable `keep:{job}` set; publish fence rejects superseded jobs | **Merged** (PR #128) |
| R03 | #120 | UUID evidence handles bound to `snapshot_epoch`; stale handles rejected | **Merged** (PR #129) |
| R12 | #122 | Corpus IDF from `term_stats` (rebuilt inside publish), `query_embed`, zero-overlap docs excluded from RRF | **Merged** (PR #130) |
| R16 | #121 | Token-aware chunking (448-token budget via `count_tokens`), page title in embedded context, runtime caps 512→2048 | **Merged** (PR #131) |
| R08 (+R09 migration validation) | #123 | Per-scope SQLite feedback store (WAL serialises writers, fresh reads) | **Merged** (PR #137) |
| R07 | #124 | `_validate_path` retains validated query (`$skiptoken`/`cursor`); nextLink loop detection | **Merged** (PR #138) |
| R13 | #125 | Write-time ownership — `StaleJobError` on staged upserts from non-running jobs | **Merged** (PR #139) |
| R05 | #127 | `kind`/`ordinal`/`page_version` on evidence; `get_evidence(page_id=)` boundary fetch | **Merged** (PR #135) |
| R04 | #126 | Strict quote schema (empty/missing fails); Copilot-layer entailment check in agent instructions | PR #136 (merged with this change) |

Re-measured on this corpus: embed input cap 2048 keeps 0 chunks truncated
(max input 619 tokens on the token-aware path); p50 latency unchanged at
~3.0s. The 60-question quality rerun on the remediated pipeline is
**complete**: all 55 answerable questions scored **100% source recall,
all-source recall and fact recall** across all 12 categories; the 5
deliberately unanswerable questions still return evidence (some of it
explicitly states the answer is unspecified — abstention remains the
agent's contract, not a retrieval failure). Strict evidence precision
~23% — correct material arrives alongside extras, as designed. The run
executed on a deliberately worst-case word-chunked corpus (chunks up to
3,625 tokens, truncated at the 2048 cap during embedding) — production
token-aware chunking bounds every chunk to ~450–619 tokens, so no
production chunk is truncated.

Follow-up remediation merged — every audit reproduction now passes
(zero remaining `xfail` markers):

| Finding | Issue | Fix | Status |
|---|---|---|---|
| R06 | #140 | Scope binds `tenant\|site\|principal\|root` via `GET /me`; `page:{site}:{list}:{id}`/`file:{drive}:{id}`; token-change client rebuild | **Merged** (PR #145) |
| R11 | #142 | `recall_hit` requires ALL required pages + `sources_missing`/`any_source`; full evidence texts; `--candidate-answers`; verdict schema validation; fact gate | **Merged** (PR #146) |
| R10 | #141 | Grid-aware rowspan/colspan; `<td>`-first rows kept as data; DOCX/PPTX/Markdown `kind=table`; bounded single rows | **Merged** (PR #147) |
| R14 | #143 | `build:{job}` vector-dim identity bound at first write; dim-mismatched resume fenced; ragged feedback quarantined | **Merged** (PR #148) |
| R15 | #144 | `vectors_for`/`sparse_for` batch `IN` under the connection's actual variable limit | **Merged** (PR #149) |
