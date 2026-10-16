"""stdio MCP server — the only surface the Copilot agent can reach.

Exposes narrow operations; no arbitrary HTTP/shell/file tools. Scope handles
are opaque and bound to the site+account+root partition.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .auth import get_credential
from .client import SecurityException, make_client
from .crawl import Crawler
from .index import Index
from .retrieval import LocalEmbedder, LocalReranker, check_citations, retrieve
from .settings import SetupError, SiteProfile, default_home, extract_page_id, workspace_index_root

mcp = FastMCP("confluence-knowledge")

# in-process caches (per server lifetime)
_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, list[dict]] = {}
_EMBEDDER = None
_RERANKER = None


def _home() -> Path:
    return default_home()


def _workdir() -> Path:
    return Path(os.environ.get("CONF_KNOWLEDGE_WORKDIR", Path.cwd())).resolve()


def _client_for(profile: SiteProfile):
    token = get_credential(_workdir(), profile.credential_ref)
    if not token:
        raise SetupError(
            "no stored token — run `confluence-knowledge setup` to configure credentials")
    return make_client(profile, token)


def _index_for(scope_key: str) -> Index:
    return Index(workspace_index_root(_workdir()) / f"{scope_key}.db")


def _embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = LocalEmbedder(_home())
    return _EMBEDDER


def _reranker():
    global _RERANKER
    if _RERANKER is None:
        _RERANKER = LocalReranker(_home())
    return _RERANKER


def _scope(scope_handle: str) -> dict:
    s = _scopes.get(scope_handle)
    if s is None:
        raise SetupError("unknown scope — call confluence_knowledge_connect first")
    return s


@mcp.tool()
def status() -> dict:
    """Report setup readiness — never returns secrets."""
    home = _home()
    try:
        profile = SiteProfile.load(home)
        cfg = {"env": profile.env, "site": profile.base_url}
    except SetupError as e:
        cfg = {"error": str(e)}
    models = home / "models"
    return {
        "configured": "error" not in cfg,
        "profile": cfg,
        "embedding_model_ready": (models / "all-MiniLM-L6-v2").exists(),
        "reranker_model_ready": (models / "ms-marco-MiniLM-L6-v2").exists(),
        "index_root": str(workspace_index_root(_workdir())),
        "live_scopes": sorted(_scopes),
    }


@mcp.tool()
def connect(root: str) -> dict:
    """Validate credentials + root readability; bind a scope handle."""
    profile = SiteProfile.load(_home())
    root_id = extract_page_id(root)
    client = _client_for(profile)
    who = client.get("wiki/rest/api/user/current" if profile.env == "cloud"
                     else "rest/api/user/current").json()
    principal = who.get("accountId") or who.get("userKey") or who.get("username") or "?"
    scope_key = profile.scope_key(principal, root_id)
    idx = _index_for(scope_key)
    st = idx.stats()
    handle = f"scope:{scope_key}"
    _scopes[handle] = {"key": scope_key, "profile": profile, "root": root_id}
    return {
        "scope_handle": handle,
        "root_page_id": root_id,
        "principal": principal,
        "cached": st["pages"] > 0,
        "cached_pages": st["pages"],
        "cached_as_of": st["as_of"],
    }


@mcp.tool()
def start_crawl(scope_handle: str, refresh: bool = False) -> dict:
    s = _scope(scope_handle)
    crawler = Crawler(_client_for(s["profile"]), _index_for(s["key"]),
                      s["profile"].env, embedder=_embedder())
    job_id = crawler.start(s["root"])
    return {"job_id": job_id, "state": "running"}


@mcp.tool()
def crawl_step(scope_handle: str, job_id: str, batch: int = 10) -> dict:
    s = _scope(scope_handle)
    crawler = Crawler(_client_for(s["profile"]), _index_for(s["key"]),
                      s["profile"].env, embedder=_embedder())
    return crawler.step(job_id, batch=batch)


@mcp.tool()
def search(scope_handle: str, question: str) -> dict:
    """Hybrid retrieve + local rerank; returns bounded, cited evidence."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    st = idx.stats()
    if st["pages"] == 0:
        return {"evidence": [], "coverage": "empty",
                "note": "no pages indexed — run a crawl first"}
    ev = retrieve(idx, question, _embedder(), _reranker())
    set_id = f"ev:{len(_evidence_sets)}:{s['key'][:8]}"
    _evidence_sets[set_id] = ev
    return {
        "evidence_set": set_id,
        "coverage": "complete" if not _incomplete(idx) else "partial",
        "as_of": st["as_of"],
        "evidence": [
            {"chunk_id": e["chunk_id"], "page_title": e["page_title"],
             "page_url": e["page_url"], "heading": e["heading"],
             "score": e.get("rerank_score"), "text": e["text"][:2000]}
            for e in ev
        ],
    }


def _incomplete(idx: Index) -> bool:
    row = idx.db.execute(
        "SELECT COUNT(*) FROM jobs WHERE state != 'done'").fetchone()
    return bool(row and row[0])


@mcp.tool()
def get_evidence(scope_handle: str, chunk_ids: list[str]) -> dict:
    """Return full text of previously returned chunks — same scope only."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    all_chunks = {c["chunk_id"]: c for c in idx.active_chunks()}
    out = []
    for cid in chunk_ids[:16]:
        c = all_chunks.get(cid)
        if c is None:
            raise SecurityException(f"chunk {cid} not in this scope's index")
        p = idx.active_pages().get(c["page_id"], {})
        out.append({"chunk_id": cid, "text": c["text"],
                    "page_title": p.get("title", ""), "page_url": p.get("url", "")})
    return {"chunks": out}


@mcp.tool()
def verify_citations(scope_handle: str, evidence_set: str,
                     mappings: list[dict]) -> dict:
    _scope(scope_handle)
    ev = _evidence_sets.get(evidence_set)
    if ev is None or not evidence_set.endswith(_scopes[scope_handle]["key"][:8]):
        raise SecurityException("unknown or cross-scope evidence set")
    return check_citations(ev, mappings)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
