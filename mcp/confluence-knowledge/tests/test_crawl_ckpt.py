"""Checkpoint throttling: progress must persist without O(n²) rewrites —
bounded by ~5 s / 50 items plus the final checkpoint (issue #159)."""

import httpx

from confluence_knowledge.client import RULES_DC, ConfluenceClient
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index

# root "1" → children "20".."29" → grandkids "3<i><j>" (11 each): 121 pages
_CHILDREN = {"1": [f"2{i}" for i in range(10)]}
for _i in range(10):
    _CHILDREN[f"2{_i}"] = [f"3{_i}{j}" for j in range(11)]


def _fan_client():
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/")[0]
            kids = _CHILDREN.get(pid, [])
            return httpx.Response(200, json={
                "results": [{"id": k} for k in kids],
                "size": len(kids), "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        if pid == "1" or pid in {k for ks in _CHILDREN.values() for k in ks}:
            if pid == "1":
                ancestors = []
            elif pid in _CHILDREN["1"]:
                ancestors = [{"id": "1"}]
            else:
                parent = next(c for c, ks in _CHILDREN.items()
                              if c != "1" and pid in ks)
                ancestors = [{"id": "1"}, {"id": parent}]
            return httpx.Response(200, json={
                "id": pid, "title": f"page {pid}",
                "body": {"storage": {"value": f"<p>body {pid}</p>"}},
                "version": {"number": 1}, "ancestors": ancestors,
                "_links": {"webui": f"/pages/{pid}", "base": "https://dc"}})
        return httpx.Response(404, json={})
    return ConfluenceClient("https://dc", {}, RULES_DC,
                            session=httpx.Client(
                                transport=httpx.MockTransport(handler)))


def test_checkpoints_are_throttled(tmp_path, monkeypatch):
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(), idx, "datacenter")
    calls = []
    real = Crawler._checkpoint
    monkeypatch.setattr(Crawler, "_checkpoint",
                        lambda self, *a, **k: calls.append(1)
                        or real(self, *a, **k))
    job = c.start("1")
    r = c.step(job, batch=None)
    assert r["state"] == "done" and r["pages_seen"] == 121
    # 121 pages → in-loop checkpoints ≤ ceil(121/50); plus the final one
    assert len(calls) <= (121 // 50) + 3


def test_checkpointed_resume_still_completes(tmp_path):
    """Pages processed between the last checkpoint and a crash are
    re-fetched and re-staged idempotently — resume converges."""
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(), idx, "datacenter")
    job = c.start("1")
    r1 = c.step(job, batch=30)          # partial: root + some children
    assert r1["state"] == "running"
    # fresh crawler over the checkpointed state (crash-equivalent)
    r2 = Crawler(_fan_client(), idx, "datacenter").step(job, batch=None)
    assert r2["state"] == "done" and r2["pages_seen"] == 121
    assert idx.stats()["pages"] == 121
