# Confluence Knowledge Agent — how it works and why

A plain-language explainer. The goal: anyone — engineers, managers, security
reviewers — should be able to read this and understand what the agent does,
why it was built this way, and where it fits.

## One-paragraph version

The agent answers questions about a Confluence page and all the pages under
it. Instead of guessing, it first reads the documentation (a local copy it
keeps up to date), finds the paragraphs that actually matter, and writes an
answer where **every sentence links back to the exact Confluence page it came
from**. If the documentation doesn't contain the answer, it says so instead
of inventing one. Indexing, embeddings, and retrieval run on the user's own
laptop — no per-question retrieval costs. (Answers are composed by Copilot's
model — evidence chunks become Copilot context, same as any file it reads.)

## The problem it solves

Every team accumulates knowledge in Confluence: processes, runbooks,
architecture decisions, tribal knowledge. The information exists but is hard
to *use*: search finds documents, not answers; page trees are deep; and
nobody is sure whether a page is current.

Meanwhile AI assistants (like GitHub Copilot) answer fluently — but they
don't know *your* Confluence. Left alone they either guess confidently or
have no access at all. The agent connects the two safely.

## What is RAG, and why is it used?

**RAG = Retrieval-Augmented Generation.** Think of it as the difference
between a closed-book and an open-book exam:

- **Without RAG** (closed book): the AI answers from memory. Fluent,
  sometimes wrong, often confidently wrong — it "hallucinates".
- **With RAG** (open book): the AI first *retrieves* the relevant pages,
  then answers *from them* — and can point at what it used.

RAG is the standard industry pattern for "let AI answer questions about my
documents". It was chosen here because it directly solves the three things
users complained about: wrong answers (retrieval grounds every claim), stale
answers (re-crawling refreshes), and untrustworthy answers (citations let you
check the source in one click).

## What makes it *advanced* RAG?

Basic RAG = chop documents into pieces + a single semantic search + answer.
This agent adds a second layer of techniques on top. Each exists because
plain RAG fails in a predictable way:

| Technique | What it does | Why basic RAG isn't enough |
|---|---|---|
| **Hybrid search** | Three retrieval legs: dense semantic vectors (nomic-embed-text-v1.5), learned sparse term-weights (bm25), and literal FTS5 keyword matching | Semantic search misses exact terms ("PCT-4213", error codes); keyword search misses paraphrases ("how do we ship" vs "release process"); the sparse leg catches rare identifiers dense vectors blur. Three legs cover all the ways people ask. |
| **Reciprocal-rank fusion** | Merges the three ranked lists fairly | Needed when combining independent ranking systems that don't share a score scale. |
| **Cross-encoder reranking** | A second, deeper model re-reads the top-25 candidates and re-sorts them | The first pass is fast-but-shallow over thousands of chunks; the reranker is slow-but-accurate on a few. This two-stage pattern is the standard quality/cost trade in serious RAG systems. |
| **Heading-aware chunking** | Splits pages at section boundaries, keeps each chunk's section path | Cutting at a fixed character count slices sentences in half and loses context. Knowing "this paragraph is under *Deployment > Windows*" makes both matching and citations better. |
| **Per-claim citation verification** | Before showing an answer, every claim's quoted text must literally exist in the retrieved chunk | The last line of defence against hallucination — a machine check, not just a polite instruction to the model. |
| **Coverage + freshness reporting** | Every answer states how many pages were indexed, when, and which were unreachable | Users know *which book* the open-book exam used — and whether some pages were missing. |
| **Scoped indexes** | Each root page gets its own index partition | Questions about one space never leak into another; refresh of one scope doesn't touch the rest. |
| **Resumable crawl** | The index queue/checkpoints live in the DB; an interrupted crawl resumes | Real corporate trees have hundreds of pages and flaky networks — losing everything at page 400 of 500 is unacceptable. |
| **Local-only models** | Embeddings + reranker run on-device | The text never leaves the laptop; no per-call cost; works fully offline after a one-time ~1.4 GB download. |

## The steps, in order — what each does and why

### 1. Bootstrap & registration (`setup`, `register`, `doctor`)

**What:** on first use the agent installs a small local service (a Python
program that does the crawling and searching), downloads the local AI models (ONNX — no GPU needed),
stores your Confluence token in the OS credential store, and writes the IDE's
config file itself.

**Why:** the Copilot agent itself is just instructions — it needs a real
program to do the reading. Making it self-installing means a colleague needs
no setup guide; `doctor` is the single "is everything healthy?" check.
`register` exists because asking humans to paste JSON into settings was the
single biggest source of support friction in testing.

**Alternative considered:** a centrally hosted service. Rejected — it would
need a server to run, a shared credential story, network access from every
laptop, and it moves page content off-device. A local service is zero-ops and
inherently private.

### 2. Connect & scope (`connect`)

**What:** you give the agent one root Confluence page. It creates a "scope" —
a key combining *site + account + root page*.

**Why:** that key is the security boundary. Each scope gets its own index,
so asking about Space A can never pull in Space B's content; the account part
means two people sharing a machine don't see each other's pages.

### 3. Crawl (`start_crawl` → background thread → `crawl_step` for progress)

**What:** the service walks the page tree breadth-first — the root, then all
its children, grandchildren, and so on — fetching each page's content. It
runs in a background thread; the agent only polls progress. Progress is
checkpointed after every page.

**Why a background crawl:** early versions did one batch per tool call —
hundreds of calls for a big tree, each one a chance to hit the IDE's timeout
or a network blip. Running it continuously server-side makes the whole thing
one operation that the agent just watches.

**Why breadth-first and durable:** an interrupted crawl must resume, not
restart; and we need to *prove* which pages are inside the scope — pages are
only indexed when they are reachable through the root's own descendant links.

### 4. Extract & chunk

**What:** each page's storage format (XHTML-ish Confluence markup) is parsed
into clean text and split into chunks at heading boundaries, each bounded to
~448 model tokens (word fallback without the tokenizer) with small overlaps; tables become readable rows; code/expand/info macros
keep their content; dynamic-only macros (jira gadgets etc.) are skipped and
disclosed.

**Why:** the AI reads chunks, not whole pages — a 500-line page is too big
and mostly irrelevant to one question. Heading-aware splitting keeps a
paragraph attached to its section title, which is what makes both matching
and citations meaningful. "Small overlaps" guard against an answer being cut
exactly at a boundary.

**Alternative considered:** fixed-size splitting (simpler) — rejected
because it slices mid-sentence and loses which section a chunk came from.

### 5. Embed & index

**What:** every chunk is turned into a vector (a list of numbers that encodes
its meaning) by a local ONNX model (`nomic-embed-text-v1.5`, quantized). Vectors go
into SQLite alongside a full-text (keyword) index. When a crawl finishes, the
new set of pages is "published" atomically — a half-finished refresh never
clobbers the last good index.

**Why vectors at all:** keyword search can't find "deploy window" when the
page says "release schedule" — vectors understand that paraphrase. Why
SQLite: it's embedded — no database to install, no port, no admin — and a
laptop's index is small enough that a heavyweight vector DB would only add
ops burden.

**Alternative considered:** a dedicated vector database (Chroma, pgvector…)
— rejected as overkill for ≤ tens of thousands of chunks on one machine.

### 6. Search (`search`)

**What:** the question is embedded, candidates are gathered from *three* legs —
dense vector, learned sparse term-weights, and the FTS5 keyword index —
merged fairly (RRF), and the top-25 are re-read by the deeper cross-encoder
model which re-sorts them; the best ~8 become the evidence set.

**Why two stages:** the vector scan is fast over thousands of chunks but
coarse; the cross-encoder is accurate but slow — so it runs only on the
shortlist. This is the standard "retrieve broadly, rank carefully" pattern.

### 7. Answer + verify (`get_evidence`, `verify_citations`)

**What:** the agent writes the answer with a per-sentence citation mapping,
then calls `verify_citations` — which checks, mechanically, that each claim's
cited chunk exists in this evidence set and that its claimed quote appears
verbatim. Anything failing is dropped or rewritten.

**Why:** instructions alone can't guarantee an AI won't cite something it
invented. The verifier turns "please don't make things up" into a checked
constraint — the most important piece of "advanced" here because it protects
trust at the exact point users care about.

## Why it's cost-effective

- **Zero inference cost per question** — the models run on the laptop; there
  are no paid API calls for embeddings, reranking, or generation of the index.
- **No infrastructure** — no server, no database, no shared platform to run.
  Costs = one venv, ~1.4 GB of models, ~1 GB of disk.
- **Reuses what's already licensed** — it lives inside the Copilot seat
  people already have; no extra per-user SaaS bill (contrast with per-seat
  RAG/AI doc products).
- **Cheap to distribute** — one agent file + one zip; install is minutes.
- **One-time setup cost** — the models download once; refreshes are a
  background re-crawl that happens only when asked.

Honest footnote: the *questions* are answered by Copilot's model — the part
we don't pay extra for is retrieval (the expensive recurring bit in hosted
RAG is exactly that, plus the platform running it).

## Where it's genuinely useful

- Onboarding: "how does our release process work?"
- Engineering Q&A: deploy windows, approval gates, ownership
- Runbooks and tribal knowledge ("where are the logs for service X?")
- Spaces too large to read but too important to ignore
- Environments where page content mustn't leave the network

## Where it's *not* the right tool

- **Live data** — it reads a snapshot, not dashboards, tickets, or metrics
- **Authoring** — it is deliberately read-only; it can't write Confluence
- **Beyond the subtree** — it answers only inside the chosen scope; asking
  about another space needs another crawl (or says "not documented")
- **Attachments & embedded media** — PDFs/images/macros it can't parse are a
  disclosed gap, not silently skipped content
- **Hundreds of thousands of pages** — the laptop-sized index has limits;
  organisation-wide search wants a hosted platform instead
- **Questions needing judgement** — it reports what docs say; "should we do
  X" is still a human call

## Security & privacy posture (one slide version)

- Token: hidden terminal input → OS credential store (never in chat/logs/files)
- Network: only your Confluence host, GET requests only, responses size-capped
- Content: never leaves the machine — embeddings, index, answers all local
- Page text: treated as *data*, never instructions (prompt-injection stance)
- Distribution: a zip your team reviews once — no surprise dependencies,
  models pinned by hash and licence-checked

## A 30-second demo script

> "Here's a Confluence page with 200 pages under it. I'll ask the agent a
> question the docs actually answer — and one they don't.
>
> [answer appears with links] — every claim links to the exact page.
>
> [second question] — it *says it doesn't know*, because the docs don't say —
> which is the most important feature of all."

## The honest summary

A hosted RAG platform would answer the same questions — with a server, a
licence, a security review, and a bill. This trades platform polish for
privacy, zero running cost, and a five-minute install — and keeps the part
that matters most: **every answer is grounded in the actual documentation,
and it refuses to fake it when the docs aren't there.**
