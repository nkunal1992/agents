"""An index built under a different embedder must not serve silently —
search refuses with an actionable note; connect reports stale_model."""

import threading
import types

from confluence_knowledge import server
from confluence_knowledge.settings import SiteProfile


class _FakeClient:
    def get(self, route, **kw):
        class R:
            status_code = 200

            def json(self_):
                if "user" in route:
                    return {"accountId": "acct"}
                return {}

        return R()


def test_stale_index_refuses_search(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workdir = tmp_path / "work"
    home.mkdir(); workdir.mkdir()
    profile = SiteProfile("cloud", "https://x.atlassian.net/wiki",
                          "u@x.test", "r")
    monkeypatch.setenv("CK_HOME", str(home))
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(workdir))
    monkeypatch.setattr(SiteProfile, "load", staticmethod(lambda h: profile))
    monkeypatch.setattr(server, "_client_for", lambda p: _FakeClient())
    monkeypatch.setattr(server, "_embedder", lambda: None)
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(server, "_sparse", lambda: None)
    monkeypatch.setattr(server, "_models_ready", threading.Event())
    server._models_ready.set()
    monkeypatch.setattr(server, "_warm_started", False)
    monkeypatch.setattr(server, "_scopes", {})
    monkeypatch.setattr(server, "_indexes", {})
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_worker_errors", {})

    c = server.connect("1")
    # simulate an index built by the old embedder
    idx = server._index_for(c["scope_handle"].split(":")[1])
    idx.upsert_page({"page_id": "1", "title": "t", "url": "u", "level": 0},
                    [types.SimpleNamespace(chunk_id="1:x", heading_path="",
                                           kind="p", ordinal=0, text="hi")],
                    None)
    idx.publish({"1"})
    idx.set_model_id("minilm-l6")          # ← the OLD model id

    c2 = server.connect("1")
    assert c2["stale_model"] is True
    r = server.search(c2["scope_handle"], "anything")
    assert r["coverage"] == "stale_model" and "start_crawl" in r["note"]
