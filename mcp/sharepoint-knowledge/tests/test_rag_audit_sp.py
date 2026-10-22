from sharepoint_knowledge import server_sp
from sharepoint_knowledge.settings_sp import SPProfile

from confluence_knowledge.index import Index


def test_account_change_must_not_reuse_scope(tmp_path, monkeypatch):
    idx = Index(tmp_path / "scope.db")
    profile = SPProfile("tenant", "app", "https://audit.sharepoint.com/sites/team", "ref")
    monkeypatch.setattr(server_sp.SPProfile, "load", lambda: profile)
    monkeypatch.setattr(server_sp, "_index_for", lambda key, **_kw: idx)
    monkeypatch.setattr(server_sp, "_start_warmup", lambda: None)
    monkeypatch.setattr(server_sp, "_scopes", {})
    account = ["alice"]

    class Client:
        def get(self, path):
            return type("Response", (), {"json": lambda self: {"id": account[0]}})()

    monkeypatch.setattr(server_sp, "_client_for", lambda key: Client())
    first = server_sp.connect("site:synthetic")["scope_handle"]
    account[0] = "bob"
    second = server_sp.connect("site:synthetic")["scope_handle"]
    idx.db.close()
    assert first != second


def test_same_item_number_in_different_page_lists_is_distinct():
    from sharepoint_knowledge.crawl_sp import SPCrawler

    class Client:
        def get(self, path, params):
            return type("Response", (), {"json": lambda self: {
                "fields": {"Title": path, "CanvasContent1": "<p>policy</p>"}}})()

    crawler = SPCrawler(Client(), None)
    first, _, _ = crawler._page_from_list_item("site-a", "list-a", "1")
    second, _, _ = crawler._page_from_list_item("site-a", "list-b", "1")
    assert first["page_id"] != second["page_id"]
