"""#176: publish reports {staged, deleted, kept} and the tombstone rules
hold — unseen pages delete, failed-listing pages keep."""

import httpx
import numpy as np

from confluence_knowledge.client import RULES_DC, ConfluenceClient
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index


def _client(handler):
    return ConfluenceClient("https://dc", {}, RULES_DC,
                            session=httpx.Client(
                                transport=httpx.MockTransport(handler)))


def _page(pid, ancestors=None):
    return {"id": pid, "title": f"Page {pid}",
            "body": {"storage": {"value": f"<p>content {pid}</p>"}},
            "version": {"number": 1},
            "ancestors": [{"id": a} for a in (ancestors or [])],
            "_links": {"webui": f"/p/{pid}", "base": "https://dc"}}


class _Embed:
    def embed(self, texts, query: bool = False):
        return np.ones((len(texts), 4), dtype=np.float32)


def _three_page_client(deleted_pid="3"):
    pages = {"1": _page("1"), "2": _page("2", ["1"]), "3": _page("3", ["1"])}
    children = {"1": [{"id": "2"}, {"id": "3"}], "2": [], "3": []}
    gone = {"pid": None}

    def h(req):
        path = req.url.path
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/")[0]
            kids = [k for k in children.get(pid, [])
                    if k["id"] != gone["pid"]]
            return httpx.Response(200, json={
                "results": kids, "size": len(kids), "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        if pid == gone["pid"]:
            return httpx.Response(404)
        return httpx.Response(200, json=pages[pid])

    return _client(h), gone


def test_publish_counts_tombstoned_and_kept(tmp_path):
    client, gone = _three_page_client()
    idx = Index(tmp_path / "s.db")
    c = Crawler(client, idx, "datacenter")
    c._embedder = _Embed()
    job = c.start("1")
    assert c.step(job, batch=None)["state"] == "done"
    first = idx.job_progress(job)["publish"]
    assert first == {"staged": 3, "deleted": 0, "kept": 0}

    gone["pid"] = "3"                      # page 3 vanishes from the tree
    job2 = c.start("1")
    r = c.step(job2, batch=None)
    assert r["state"] == "done"
    second = idx.job_progress(job2)["publish"]
    assert second["deleted"] == 1          # page 3 tombstoned
    assert {row[0] for row in idx.db.execute(
        "select title from pages")} == {"Page 1", "Page 2"}


def test_failed_listing_keeps_rows(tmp_path):
    """A children-listing that 503s past the retry budget keeps the
    previous snapshot — reachable-but-invisible pages are NOT
    tombstoned."""
    pages = {"1": _page("1"), "2": _page("2", ["1"]), "3": _page("3", ["1"])}
    children = {"1": [{"id": "2"}, {"id": "3"}], "2": [], "3": []}
    state = {"flaky": False}

    def h(req):
        path = req.url.path
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/")[0]
            if state["flaky"] and pid == "1":
                return httpx.Response(503)   # root listing dead
            return httpx.Response(200, json={
                "results": children.get(pid, []),
                "size": len(children.get(pid, [])), "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        return httpx.Response(200, json=pages[pid])

    idx = Index(tmp_path / "s.db")
    c = Crawler(_client(h), idx, "datacenter")
    c._embedder = _Embed()
    job = c.start("1")
    assert c.step(job, batch=None)["state"] == "done"
    assert idx.job_progress(job)["publish"] == {"staged": 3,
                                              "deleted": 0, "kept": 0}

    state["flaky"] = True
    job2 = c.start("1")
    r = c.step(job2, batch=None)
    assert r["state"] == "done"
    second = idx.job_progress(job2)["publish"]
    assert second["deleted"] == 0          # nothing tombstoned
    assert second["kept"] == 2             # the invisible subtree carried
    assert {row[0] for row in idx.db.execute(
        "select title from pages")} == {"Page 1", "Page 2", "Page 3"}
