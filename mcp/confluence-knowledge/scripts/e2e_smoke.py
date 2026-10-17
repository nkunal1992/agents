"""End-to-end smoke test: the real stdio MCP server with the real local models
against a fake Confluence Data Center served on localhost.

Runs the tool sequence the Copilot agent uses (status → connect → start_crawl →
crawl_step… → search → get_evidence → verify_citations) through the MCP client
SDK with an IDE-like per-request timeout, so protocol, threading, timeout and
packaging problems surface here instead of on a user's laptop.

Usage (models must be prepared in CONF_KNOWLEDGE_HOME first):
    CONF_KNOWLEDGE_HOME=/tmp/ck/home python scripts/e2e_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).parent))
from _e2e_common import TOKEN, call, check, serve

REQUEST_TIMEOUT = 20  # seconds; IDE clients time out around this order




async def main() -> None:
    home = Path(os.environ.get("CONF_KNOWLEDGE_HOME", "")).expanduser()
    if not (home / "models" / "bge-m3").exists():
        raise SystemExit("prepare models first: CONF_KNOWLEDGE_HOME=<dir> "
                         "confluence-knowledge prepare-models")
    srv, base, _hits = serve()
    (home / "profile.json").write_text(json.dumps(
        {"env": "datacenter", "base_url": base, "email": None, "credential_ref": "e2e"}))
    work = Path(tempfile.mkdtemp(prefix="ck-e2e-work-")).resolve()
    env = {**os.environ, "CONF_KNOWLEDGE_HOME": str(home),
           "CONF_KNOWLEDGE_WORKDIR": str(work), "CONF_KNOWLEDGE_TOKEN": TOKEN,
           "CONF_KNOWLEDGE_STEP_SECONDS": "8"}
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "confluence_knowledge.server"], env=env,
                                   cwd=tempfile.gettempdir())
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check({"status", "connect", "start_crawl", "crawl_step", "search",
               "get_evidence", "verify_citations"} <= tools, f"tools registered: {sorted(tools)}")

        st = await call(session, "status")
        check(st["configured"] and st["index_root"].startswith(str(work)),
              "status: configured, index under CONF_KNOWLEDGE_WORKDIR", st)

        c = await call(session, "connect", {"root": f"{base}/pages/viewpage.action?pageId=100"})
        scope = c["scope_handle"]
        check(not c["cached"], "connect: fresh scope, nothing cached")

        job = await call(session, "start_crawl", {"scope_handle": scope})
        # background crawl — crawl_step is an instant progress poll now
        steps, r, waited = 0, {"state": "running"}, 0.0
        while r["state"] != "done":
            steps += 1
            check(waited <= 90, "background crawl finishes within 90s", r)
            await asyncio.sleep(0.4)
            waited += 0.4
            r = await call(session, "crawl_step", {"scope_handle": scope, "job_id": job["job_id"]})
        check(r["pages_seen"] == 32 and not r["errors"],
              f"crawl: all 32 pages incl. grandchild indexed ({steps} polls)", r)

        res = await call(session, "search", {"scope_handle": scope,
                                             "question": "When are production deploys allowed?"})
        top = res["evidence"][0]
        check(res["coverage"] == "complete", "search: coverage complete")
        check(top["page_title"] == "Deploy Windows" and "Thursday" in top["text"],
              f"search: grandchild ranked first ({top['page_title']})", res)
        check(top["page_url"].startswith(base + "/pages/"), "search: citation URL is absolute")

        res2 = await call(session, "search", {"scope_handle": scope,
                                              "question": "Who signs off a production release?"})
        check(res2["evidence"][0]["page_title"] == "Release Handbook",
              "search: parent-page fact retrieved")

        ev = await call(session, "get_evidence", {"scope_handle": scope,
                                                  "chunk_ids": [top["chunk_id"]]})
        check("Thursdays" in ev["chunks"][0]["text"], "get_evidence: full chunk text")

        res3 = await call(session, "search", {"scope_handle": scope,
                                              "question": "maintenance freeze dates"})
        check(any("maintenance freeze" in e["text"] for e in res3["evidence"]),
              "search: macro body content is indexed", res3["evidence"])

        ok = await call(session, "verify_citations", {
            "scope_handle": scope, "evidence_set": res["evidence_set"],
            "mappings": [{"claim": "Deploys are on Thursdays", "chunk_id": top["chunk_id"],
                          "quote": "Production deploys happen only on Thursdays"}]})
        bad = await call(session, "verify_citations", {
            "scope_handle": scope, "evidence_set": res["evidence_set"],
            "mappings": [{"claim": "Deploys are on Mondays", "chunk_id": top["chunk_id"],
                          "quote": "deploys happen on Mondays"}]})
        check(ok["ok"] and not bad["ok"], "verify_citations: accepts real quote, rejects invented one")

        again = await call(session, "connect", {"root": "100"})
        check(again["cached"] and again["cached_pages"] == 32, "reconnect: cache reused")

    leaked = [p for p in work.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()]
    check(not leaked, "no token written to the work dir")
    srv.shutdown()
    print("\nE2E OK")


if __name__ == "__main__":
    asyncio.run(main())
