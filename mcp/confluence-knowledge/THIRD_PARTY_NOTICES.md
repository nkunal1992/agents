# Third-Party Notices — confluence-knowledge

## Models (downloaded at setup; run locally only)

| Model | Source | Revision | License |
|---|---|---|---|
| `sentence-transformers/all-MiniLM-L6-v2` | huggingface.co/sentence-transformers/all-MiniLM-L6-v2 | `e9a7bebe3474e74e8193da1a05abb5b5291dcb6a` | Apache-2.0 |
| `cross-encoder/ms-marco-MiniLM-L6-v2` | huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2 | `233902d25c440f23af6f7d6e94d2946bac0bee0a` | Apache-2.0 |

The Apache License 2.0 text is included inside each downloaded model
directory (`LICENSE`/`LICENSE.txt`) by `prepare-models`, which refuses to
ship a model whose license artifact is missing.

## Python dependencies

Runtime: httpx (BSD-3), beautifulsoup4 (MIT), lxml not required (stdlib
html.parser), numpy (BSD-3), keyring (MIT), mcp (MIT).
Optional `models` extra: sentence-transformers (Apache-2.0),
huggingface-hub (Apache-2.0) — plus their transitive deps; check
`pip licenses`/audit before corporate distribution.

This project's own code is MIT (see LICENSE in the repository root).
