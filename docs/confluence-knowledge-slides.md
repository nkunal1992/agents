---
marp: true
theme: default
paginate: true
style: |
  section { font-size: 30px; }
  h1 { color: #1a5fb4; }
  blockquote { border-left: 6px solid #1a5fb4; padding: 0.3em 0 0.3em 0.8em; background: #f6f9fc; }
  table { font-size: 24px; }
---

# Grounded AI Answers from Our Own Confluence

**confluence-knowledge** — a Copilot agent that answers only from the
documentation it can prove it read.

Presenter notes: *keep the intro under a minute — the demo does the talking.*

---

## The everyday problem

- Our knowledge lives in Confluence — runbooks, processes, decisions, tribal knowledge
- Confluence **search finds documents, not answers**
- AI assistants answer fluently — **but they don't know our Confluence**
- When they don't know, they **guess confidently** anyway

> The gap: the answer exists in a page nobody can find — and the AI we already have can't read it.

---

## What this is

One Copilot agent you can drop into any repository.

You give it **one Confluence root page** — it reads that page and **every
page underneath it**, keeps a local index, and answers questions:

- with a **citation on every claim** (click → opens the source page)
- with a **freshness note** ("as of today, 212 pages")
- with an **honest refusal** when the docs don't say

---

# Live demo

*(switch to IntelliJ / VS Code)*

```
Set up → ask → watch the crawl → answer with citations
→ ask something the docs DON'T contain
```

The refusal is the feature.

---

## Demo beats — narrate as it runs

1. "It's crawling — you can watch `pages_seen` climb; it runs in the background"
2. "Notice every sentence ends with a link — you can verify it right now"
3. "It also tells you **coverage** — how much of the subtree it indexed"
4. "Now I'll ask something that isn't documented…"

> Expected answer: "the provided documentation does not contain the information needed."

---

## How it works — the one-slide version

```
Confluence subtree
       │  GET-only crawl (background, resumable)
       ▼
Local index on the laptop — SQLite
   FTS5 keywords + learned sparse terms + meaning vectors
       │  question → all three legs → merge → deep re-rank
       ▼
Top evidence chunks → answer with per-claim citations
       │  mechanical citation check
       ▼
Verified answer — or an honest "not documented"
```

**Everything below the Confluence page runs on your machine** — Copilot (which you already use) composes the final answer.

---

## Why RAG — the open-book exam

**Closed book (no RAG):** AI answers from memory. Fluent. Sometimes wrong. Confidently wrong.

**Open book (RAG = Retrieval-Augmented Generation):** AI *retrieves* the
relevant paragraphs first, answers *only* from them, and shows its work.

> We didn't teach the model anything about our docs.
> We just made it read them before answering.

---

## "Advanced RAG" — what we added and why

| Basic RAG gap | What this adds |
|---|---|
| Semantic search misses exact codes ("PCT-4213") | **Hybrid search** — keyword + meaning, merged fairly |
| Coarse first-pass ranking | **Cross-encoder reranker** — a deeper model re-reads the shortlist |
| Chunks lose context | **Heading-aware splitting** — each chunk keeps its section path |
| AI can cite what it invented | **`verify_citations`** — every quote must literally exist in the evidence |

The last row matters most: instructions ask nicely; the verifier *checks*.

---

## The trust layer — what users actually see

- **Citations per claim** — `[Source: Deploy Windows](https://…)`
- **Coverage note** — "212 pages · full subtree" or "partial: 3 pages unreachable"
- **Freshness** — "as of <date>"; refresh is one word away
- **Refusals** — a fixed, honest sentence — never a bluff

> Users trust it because they can check it — and because it admits when it can't answer.

---

## Security & privacy — for the InfoSec folks

- **Token**: entered once, hidden input → **OS credential store** (Keychain/Credential Manager). Never in chat, files or logs.
- **Network**: only your Confluence host, **GET requests only** — writes are refused before the call.
- **Indexing/content**: embeddings + retrieval run 100% on the laptop — no
  cloud embeddings, no external API calls. Retrieved evidence becomes context
  for the Copilot model you already use (same as any file Copilot reads).
- **Prompt injection**: Confluence page text is treated as *data*, never commands.
- **Corporate proxies**: uses the OS cert store (TLS-inspection friendly).

*The agent can only ever do less harm than reading a page in a browser.*

---

## Cost — why this is cheap

| Item | Cost |
|---|---|
| Infrastructure | **None** — runs on the user's laptop |
| Per-question API | **None** — embeddings/reranking are local |
| Licence | Reuses the **Copilot seat** they already have |
| Models | One-time ~1.4 GB download (MIT/Apache-2.0, ONNX — no GPU needed) |
| Ops | One zip to distribute; `doctor` self-checks |

> Compare: hosted RAG platforms charge per seat **and** run a shared index on someone else's server — including our page content.

---

## Built for our constraints

- Windows / macOS / Linux; VS Code **and** IntelliJ
- Works behind **TLS-inspecting proxies** (bundles `pip-system-certs`)
- Survives **ARM64/x64 confusion** (diagnosed + documented)
- **Resumable** crawl — network blips don't lose progress
- **Self-bootstrapping** — the agent installs and registers itself from chat; one terminal command (`setup`) for the token, one IDE reload

Every bullet is a real failure we hit, reproduced, and fixed.

---

## Where it shines

- Onboarding — "how does our release process work?"
- Engineering Q&A — deploy windows, approval gates, ownership
- Runbooks, conventions, tribal knowledge
- Spaces too large to read but too important to ignore
- Teams where docs must not leave the network

---

## Where it's *not* the answer

- **Live data** — snapshots, not dashboards/tickets/metrics
- **Writing to Confluence** — deliberately read-only
- **Outside the chosen subtree** — it says "not documented"
- **Attachments/media** it can't parse — disclosed as gaps
- **Org-wide search at huge scale** — that's a hosted platform's job
- **Judgement calls** — it reports what docs say; decisions stay human

---

## How we'd roll it out

1. Store upload / one agent file + one bundle zip per team
2. First run: agent self-installs (≈ 5 min) → `setup` for the token → reload
3. Pick a root page per space; refresh when docs change
4. `doctor` = one-line health check; `EXPLAINED.md` answers the deep questions

**Pilot group**: one team, one big space → measure "questions answered without asking a human".

---

## The takeaway

> We already pay for the AI. We already have the documentation.
> This just makes the AI **read the docs first** — and prove it.

- **Trustworthy**: every claim cited, verified, or refused
- **Private**: crawling + indexing + embeddings never leave the laptop
  (the Copilot model you already use sees the cited excerpts)
- **Cheap**: zero marginal cost per question
- **Simple**: a zip and a five-minute first run

---

## Questions?

Demo machine is open — try breaking it:

- ask about a page *outside* the crawled subtree
- ask for something the docs don't say
- ask it to *change* a page
