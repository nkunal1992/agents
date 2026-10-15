# codebase-guide

A read-only codebase explainer. Ask it *how* something works, *where* a
responsibility lives, or *why* code exists — it answers with `file:line`
evidence, never modifies code, and is engineered to stay cheap on large repos.

- **Grounded**: every claim cites the code it came from. It flags stale comments
  and docs that contradict the implementation.
- **Safe**: cannot write files; its shell access is whitelisted to read-only
  git commands (`log`, `show`, `blame`, `diff`) and directory listings.
- **Cheap**: search-first navigation, a bounded file budget, and a
  low-cost model pin (`GPT-6 Luna` → `GPT-5 mini` → `GPT-4.1`).

## Quick start

```bash
scripts/install.sh --only codebase-guide /path/to/your-repo
# optional, powers the handoff buttons:
scripts/install.sh --only codebase-guide --only docs-writer --only java-modernizer --only debugger /path/to/your-repo
```

Then open the repo in VS Code or IntelliJ, pick **codebase-guide** in the
Copilot Chat agent dropdown, and ask a question.

## Ways to use it

| You type | What it does |
|---|---|
| `how does authentication work?` | Traces the flow end-to-end, every step anchored `file:line` |
| `where is order validation handled?` | Ranked list of locations with a one-line role each |
| `why was this retry logic added?` | Git archaeology — `git log`/`blame`/`show` on the implicated code |
| `tour` | Repo overview: module map, layering, entry points, glossary |
| `/codebase-explain <topic>` | Same as a how-question, from a slash command |
| `/codebase-where <thing>` | Same as a where-question, extra concise |
| `/codebase-tour <area?>` | Same as "tour", optionally scoped |

Ask follow-ups to drill down — "expand on the persistence layer" — instead of
restating context.

## Answer format

```
## Short answer      the direct answer in 2-3 sentences
## How it works      the flow, every step anchored file:line
## Key files         file · role table
## Gotchas           stale comments, dead code, surprising behavior
## Confidence        proven vs inferred · untraced areas · follow-ups
```

`where`-questions get the ranked location list; `tour` swaps in module map,
layering, entry points, cross-cutting concerns, and glossary.

## Enterprise notes

- **Large repos / monorepos**: it detects modules, scopes to the relevant one,
  and names what it excluded. Per-question exploration is bounded (~15 source
  files); if it hits the limit it tells you what remains untraced rather than
  silently going shallow.
- **Read-only guarantee**: no `edit` tool; `execute` is restricted by
  instruction to read-only git + listing commands. It cannot change your repo.
- **Secrets**: it reads config/logs to understand the system but never echoes
  secret values — it reports that a secret exists, not its content.
- **Honesty over confidence theatre**: "inferred, not proven" is a normal part
  of its answers. Treat the Confidence section as the audit trail.

## Sample interaction

> **you:** `where is the order total calculated?`
>
> **codebase-guide:**
> ## Short answer
> `OrderService.computeTotal()` sums line prices and applies the customer
> discount — `src/order/OrderService.java:142`.
>
> ## Key files
> | File | Role |
> |---|---|
> | `src/order/OrderService.java:142` | `computeTotal()` — entry point |
> | `src/order/LineItem.java:31` | per-line price incl. quantity |
> | `src/pricing/DiscountPolicy.java:58` | discount rules; static singleton |
> | `src/order/OrderController.java:74` | REST caller |
>
> ## Gotchas
> `DiscountPolicy` is a mutable singleton (`DiscountPolicy.java:44`) — totals
> can vary between calls in the same request if config reloads mid-flight.
>
> ## Confidence
> Proven: call chain and summation logic. Not traced: currency conversion in
> `LineItem` (delegates to `fx/Converter` — ask to expand).

## Handoffs

Buttons that appear after an answer (target agent must be installed):

- **Document this** → docs-writer: turn the explanation into real docs
- **Modernize this area** → java-modernizer: technical-debt plan for the code just explained
- **Debug this flow** → debugger: when the explanation reveals a bug

## Troubleshooting

| Problem | Fix |
|---|---|
| Agent not in dropdown | File must be in `.github/agents/`; reload the IDE |
| `/codebase-*` commands missing | Prompt files live in `.github/prompts/`; JetBrains prompt support is preview |
| Handoff button does nothing | Install the target agent (docs-writer / java-modernizer / debugger) |
| Answers feel shallow on a huge repo | Ask follow-ups to drill in, or scope with "in module X" — the per-question budget is intentional |

## Limitations

- Explains code as it exists; it cannot run the app or verify runtime behavior.
- Execute access is restricted by prompt instructions, not by the platform.
- Coverage is evidence-bounded: what it didn't read, it says it didn't read.
