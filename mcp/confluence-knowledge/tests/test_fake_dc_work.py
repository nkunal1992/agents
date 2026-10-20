"""Fast deterministic tests for the fake work-DC used by the stress
harness (#169). No models, no network beyond localhost."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _fake_dc_work as fake


def _get(base: str, path: str, token: str = fake.TOKEN):
    req = urllib.request.Request(
        base + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}


def test_tree_exact_size_and_determinism():
    a, b = fake.build_tree(200, seed=7), fake.build_tree(200, seed=7)
    assert len(a.pages) == 200
    assert a.pages.keys() == b.pages.keys()
    assert all(pid.isdigit() for pid in a.pages)


def test_tree_shape_wide_deep_mixed():
    t = fake.build_tree(1000)
    assert len(t.children[fake.ROOT_ID]) == 250  # forces pagination
    kinds = {p.get("kind") for p in t.pages.values() if "kind" in p}
    assert {"normal", "large", "table", "macro", "huge"} <= kinds
    assert max(p["depth"] for p in t.pages.values() if "depth" in p) >= 10


def test_tree_small_n_still_has_huge_and_depth():
    t = fake.build_tree(200)
    assert any(p.get("kind") == "huge" for p in t.pages.values())
    assert max(p["depth"] for p in t.pages.values() if "depth" in p) > 2


def test_children_pagination_limit_start():
    t = fake.build_tree(1000)
    srv, base, _log = fake.serve(t, fake.Hostility(enabled=False))
    try:
        st, d1 = _get(base, "/rest/api/content/1000000/child/page"
                            "?limit=200&start=0")
        st2, d2 = _get(base, "/rest/api/content/1000000/child/page"
                             "?limit=200&start=200")
        assert st == 200 and d1["size"] == 200
        assert st2 == 200 and d2["size"] == 50  # 250 total, DC stop: size<limit
        assert {c["id"] for c in d1["results"]}.isdisjoint(
            {c["id"] for c in d2["results"]})
    finally:
        srv.shutdown()


def test_page_body_dc_shape_and_ancestors():
    t = fake.build_tree(200)
    leaf = next(iter(t.children.get(next(
        iter(t.children[fake.ROOT_ID]), ""), ["1000001"])), None)
    pid = leaf or "1000001"
    srv, base, _log = fake.serve(t, fake.Hostility(enabled=False))
    try:
        st, d = _get(base, f"/rest/api/content/{pid}")
        assert st == 200
        assert d["body"]["storage"]["value"]
        assert d["version"]["number"] >= 1
        assert d["_links"]["webui"].endswith(f"pageId={pid}")
        assert f"service-{pid}" in d["body"]["storage"]["value"]  # canary
    finally:
        srv.shutdown()


def test_auth_required_and_identity():
    t = fake.build_tree(20)
    srv, base, _log = fake.serve(t, fake.Hostility(enabled=False))
    try:
        st, _ = _get(base, "/rest/api/user/current", token="wrong")
        assert st == 401
        st, d = _get(base, "/rest/api/user/current")
        assert st == 200 and d["username"] == "stress"
    finally:
        srv.shutdown()


def test_hostile_mix_injects_errors_and_429():
    t = fake.build_tree(30)
    cfg = fake.Hostility(enabled=True, seed=5, rate_per_s=2.0, burst=2.0,
                         err5xx_p=0.3, slow_outlier_p=0.0, hang_p=0.0)
    srv, base, log = fake.serve(t, cfg)
    try:
        codes = [_get(base, f"/rest/api/content/{1000000 + i}")[0]
                 for i in range(1, 25)]
        assert 429 in codes
        assert any(c in (500, 502, 503) for c in codes)
        assert all(e["status"] in codes for e in log)
    finally:
        srv.shutdown()


def test_mutate_bumps_deletes_moves():
    t = fake.build_tree(200)
    srv, base, _log = fake.serve(t, fake.Hostility(enabled=False))
    try:
        pages_v0 = {pid: p["version"] for pid, p in t.pages.items()}
        req = urllib.request.Request(base + "/_control/mutate?pct=10&seed=3",
                                     method="POST")
        summ = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert len(summ["changed"]) == 20 and len(summ["deleted"]) == 3
        assert len(t.pages) == 197
        bumped = sum(t.pages[p]["version"] for p in summ["changed"])
        assert bumped == sum(pages_v0[p] for p in summ["changed"]) + 20
    finally:
        srv.shutdown()


def test_every_page_reachable_via_dc_api():
    """Crawler-eye view: BFS children listings must discover all pages."""
    t = fake.build_tree(300)
    srv, base, _log = fake.serve(t, fake.Hostility(enabled=False))
    try:
        seen, queue = set(), [fake.ROOT_ID]
        while queue:
            pid, start = queue.pop(), 0
            while True:
                st, d = _get(base, f"/rest/api/content/{pid}/child/page"
                                   f"?limit=200&start={start}")
                assert st == 200
                for ch in d["results"]:
                    if ch["id"] not in seen:
                        seen.add(ch["id"])
                        queue.append(ch["id"])
                if d["size"] < d["limit"]:
                    break
                start += d["size"]
        assert len(seen) + 1 == len(t.pages)  # +1 = root itself
    finally:
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
