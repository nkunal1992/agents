"""Realistic + hostile fake Confluence Data Center for stress runs (#169).

Unlike `_e2e_common.py` (31 fixed pages, constant latency, happy path), this
generates a deterministic N-page tree whose shape and page bodies look like a
real corporate Confluence: a wide root branch big enough to force start/limit
pagination, a deep chain, mixed body sizes (prose, tables, macros, a few huge
pages), version numbers and ancestor links in the DC response shape.

Hostility is opt-in and seeded: lognormal request latency with slow outliers,
a token-bucket rate limiter answering 429 with Retry-After, intermittent
5xx, requests that hang past the client's 20 s timeout, and a few 403 pages.

Every request is recorded in `log` (path, status, duration, in-flight count)
so the harness can measure real fetch concurrency, retries and dead time.

Run standalone for a harness subprocess:

    python scripts/_fake_dc_work.py --pages 1000 --hostile --port 0 \
        --profile-file /tmp/dc-profile.json
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "stress-token-canary"
ROOT_ID = "1000000"


# --------------------------------------------------------------------------
# tree generator


@dataclass
class FakeTree:
    pages: dict          # id -> {title, body, version, ancestors:[id]}
    children: dict       # parent_id -> [child_id]
    root: str = ROOT_ID

    def delete(self, page_id: str) -> None:
        p = self.pages.pop(page_id, None)
        if p:
            for kids in self.children.values():
                if page_id in kids:
                    kids.remove(page_id)

    def move(self, page_id: str, new_parent: str) -> None:
        for kids in self.children.values():
            if page_id in kids:
                kids.remove(page_id)
        self.children.setdefault(new_parent, []).append(page_id)
        self.pages[page_id]["ancestors"] = \
            self.pages[new_parent]["ancestors"] + [new_parent]


def _body(kind: str, pid: str, rng: random.Random) -> str:
    """Storage-format body by page kind; embeds unique canary facts so the
    harness can verify the page was actually indexed and retrievable."""
    canary = (f"<p>Canary ERR-{pid}: service-{pid} is owned by team-"
              f"{rng.randrange(1, 40)}. On-call runbook rev {pid}.</p>")
    if kind == "normal":
        words = " ".join(rng.choice(
            ["deploy", "release", "approval", "pipeline", "staging", "window",
             "rollback", "sign-off", "freeze", "change", "advisory", "board",
             "runbook", "handover", "escalation", "incident", "review"])
            for _ in range(rng.randrange(120, 600)))
        return (f"<h2>Overview</h2><p>{words}</p>"
                f"<h2>Details</h2><p>{words[:len(words) // 2]}</p>" + canary)
    if kind == "large":
        para = ("<p>The migration checklist requires owners to verify each "
                "integration endpoint before the cutover window opens, then "
                "record results in the shared tracker for audit purposes "
                "and notify downstream consumers of any breaking changes "
                "well ahead of the production deadline.</p>")
        return f"<h2>Runbook</h2>{para * rng.randrange(15, 60)}" + canary
    if kind == "table":
        rows = "".join(
            f"<tr><td>item {i}</td><td>owner-{rng.randrange(60)}</td>"
            f"<td>v{i}.{rng.randrange(9)}</td><td>{'done' if i % 3 else 'pending'}</td></tr>"
            for i in range(rng.randrange(200, 800)))
        return (f"<h2>Inventory</h2><table><tr><th>Item</th><th>Owner</th>"
                f"<th>Version</th><th>State</th></tr>{rows}</table>" + canary)
    if kind == "macro":
        return ("<h2>Notices</h2>"
                '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
                "<p>The maintenance freeze runs from December 20 to "
                "January 5 — no production changes.</p>"
                "</ac:rich-text-body></ac:structured-macro>"
                '<ac:structured-macro ac:name="expand"><ac:parameter '
                'ac:name="title">Runbook detail</ac:parameter>'
                "<ac:rich-text-body><p>" +
                " ".join(["step", "verify", "rollback", "confirm"] * 40) +
                "</p></ac:rich-text-body></ac:structured-macro>"
                '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
                "<![CDATA[for i in steps: run(i)]]>"
                "</ac:plain-text-body></ac:structured-macro>" + canary)
    # huge — a page big enough to hit the token-budget batch bound hard
    cell = "| col a | col b | col c | long descriptive value text |\n"
    return f"<h2>Audit matrix</h2><pre>{cell * 2200}</pre>" + canary


def build_tree(n_pages: int, seed: int = 7) -> FakeTree:
    """Deterministic n-page tree under ROOT_ID.

    Shape: a wide root branch (min(250, n-2) children — >200 forces DC
    pagination at the limit), a 12-level deep chain, and the remainder
    spread over 3–6 levels with branching 2–15. Kinds: 70% normal, 15%
    large, 8% table, 5% macro, 2% huge (at least one).
    """
    rng = random.Random(seed)
    pages: dict = {}
    children: dict = {ROOT_ID: []}
    pages[ROOT_ID] = {"title": "Engineering Handbook", "version": 3,
                      "body": _body("macro", ROOT_ID, rng), "ancestors": []}

    ids = [str(1000000 + i) for i in range(1, n_pages)]
    cursor = 0

    def attach(pid: str, parent: str, depth: int) -> None:
        kind = ("huge" if rng.random() < 0.02 else
                rng.choices(["normal", "large", "table", "macro"],
                            weights=[70, 15, 8, 5])[0])
        pages[pid] = {
            "title": f"{kind.capitalize()} page {pid}",
            "version": rng.randrange(1, 12),
            "body": _body(kind, pid, rng),
            "ancestors": pages[parent]["ancestors"] + [parent],
            "kind": kind, "depth": depth}
        children.setdefault(parent, []).append(pid)
        children.setdefault(pid, [])

    # wide root branch — >200 children forces start/limit pagination
    wide = min(250, max(1, (n_pages - 20) // 2), n_pages - 2)
    for _ in range(wide):
        attach(ids[cursor], ROOT_ID, 1)
        cursor += 1
    # deep chain hanging off the first root child
    parent, depth = ids[0], 2
    chain = min(12, n_pages - cursor - 1)
    for _ in range(chain):
        attach(ids[cursor], parent, depth)
        parent, depth, cursor = ids[cursor], depth + 1, cursor + 1
    # medium random branching for the remainder (3–6 levels, 2–15 kids)
    frontier = [ids[0]] + list(children.get(ROOT_ID, [])[wide // 2:])
    while cursor < len(ids):
        parent = rng.choice(frontier)
        pdepth = pages[parent].get("depth", 1)
        kids = rng.randrange(2, 16)
        for _ in range(kids):
            if cursor >= len(ids):
                break
            attach(ids[cursor], parent, pdepth + 1)
            frontier.append(ids[cursor])
            cursor += 1
    # guarantee at least one huge page even at small n / unlucky seeds
    if not any(p.get("kind") == "huge" for p in pages.values()):
        pid = ids[rng.randrange(len(ids))]
        pages[pid]["kind"] = "huge"
        pages[pid]["body"] = _body("huge", pid, rng)
    return FakeTree(pages=pages, children=children)


def mutate(tree: FakeTree, pct: float = 10.0, seed: int = 99,
           n_delete: int = 3, n_move: int = 3) -> dict:
    """Bump version+body on pct% of pages, delete n_delete, move n_move.
    Returns a summary for the harness's expectations."""
    rng = random.Random(seed)
    ids = [i for i in tree.pages if i != tree.root]
    changed = rng.sample(ids, max(1, round(len(ids) * pct / 100)))
    for pid in changed:
        tree.pages[pid]["version"] += 1
        tree.pages[pid]["body"] += f"<p>Edited rev {pid}.</p>"
    deletable = [i for i in ids if i not in changed and
                 not tree.children.get(i)]
    deleted = rng.sample(deletable, min(n_delete, len(deletable)))
    for pid in deleted:
        tree.delete(pid)
    movable = [i for i in ids if i not in changed and i not in deleted]
    moved = rng.sample(movable, min(n_move, len(movable)))
    for pid in moved:
        tree.move(pid, rng.choice(list(tree.pages)))
    return {"changed": changed, "deleted": deleted, "moved": moved}


# --------------------------------------------------------------------------
# hostility + server


@dataclass
class Hostility:
    """None of this triggers in clean mode (fixed `clean_latency` seconds)."""
    enabled: bool = True
    seed: int = 1234
    clean_latency: float = 0.05
    median_latency: float = 0.25      # lognormal median (seconds)
    sigma: float = 0.6                # lognormal sigma → p95 ≈ 0.8 s
    slow_outlier_p: float = 0.02      # 2% of requests take 3–8 s
    rate_per_s: float = 12.0          # token bucket → 429 + Retry-After
    burst: float = 8.0
    retry_after: tuple = (1, 3)
    err5xx_p: float = 0.01            # intermittent 500/502/503
    hang_p: float = 0.003             # requests that hang past the 20 s timeout
    hang_s: float = 25.0
    forbidden_p: float = 0.01         # pages that 403 on body fetch

    def state(self) -> dict:
        # token bucket shared across handler threads
        return {"tokens": self.burst, "refill_at": time.monotonic(),
                "lock": threading.Lock(),
                "rng": random.Random(self.seed)}


def _throttle(st: dict, cfg: Hostility, rng: random.Random) -> int | None:
    """Returns a forced status code or None (request may proceed)."""
    with st["lock"]:
        now = time.monotonic()
        st["tokens"] = min(cfg.burst, st["tokens"]
                           + (now - st["refill_at"]) * cfg.rate_per_s)
        st["refill_at"] = now
        if st["tokens"] < 1.0:
            return 429
        st["tokens"] -= 1.0
    r = rng.random()
    if r < cfg.hang_p:
        return 900  # sentinel: hang, not a real status
    if r < cfg.hang_p + cfg.err5xx_p:
        return rng.choice([500, 502, 503])
    return None


def make_handler(tree: FakeTree, log: list, cfg: Hostility,
                 log_file: str | None = None):
    _log_fh = (open(log_file, "a")  # noqa: SIM115 — lives for the server
               if log_file else None)   # lifetime; closed on process exit
    st = cfg.state()
    rng = random.Random(cfg.seed + 5)
    forbidden = {pid for pid in tree.pages
                 if pid != tree.root and rng.random() < cfg.forbidden_p}
    in_flight = [0]
    flight_lock = threading.Lock()

    class FakeWorkDC(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            t0 = time.monotonic()
            with flight_lock:
                in_flight[0] += 1
                now_flying = in_flight[0]
            status = 200
            try:
                status = self._serve()
            finally:
                with flight_lock:
                    in_flight[0] -= 1
                entry = {"ts": time.time(), "path": self.path,
                         "status": status,
                         "dur_s": round(time.monotonic() - t0, 3),
                         "in_flight": now_flying}
                log.append(entry)
                if _log_fh:
                    _log_fh.write(json.dumps(entry) + "\n")
                    _log_fh.flush()
            return status

        def do_POST(self):
            """Harness control plane (never hit by the confluence client,
            which is GET-only). /_control/mutate?pct=&seed= edits pages."""
            path = urlparse(self.path).path.rstrip("/")
            qs = parse_qs(urlparse(self.path).query)
            if path == "/_control/mutate":
                summary = mutate(
                    tree, pct=float(qs.get("pct", ["10"])[0]),
                    seed=int(qs.get("seed", ["99"])[0]))
                self._json(200, summary)
                return
            self._json(404, {})

        def _serve(self) -> int:
            if cfg.enabled:
                forced = _throttle(st, cfg, rng)
                if forced == 900:
                    time.sleep(cfg.hang_s)
                    return 504
                if forced == 429:
                    self.send_response(429)
                    self.send_header(
                        "Retry-After", str(rng.randrange(*cfg.retry_after)))
                    self.end_headers()
                    return 429
                if forced:
                    self._json(forced, {})
                    return forced
                time.sleep(max(0.01, rng.lognormvariate(
                    _ln_mu(cfg), cfg.sigma)))
                if rng.random() < cfg.slow_outlier_p:
                    time.sleep(rng.uniform(3.0, 8.0))
            else:
                time.sleep(cfg.clean_latency)

            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json(401, {})
                return 401
            path = urlparse(self.path).path.rstrip("/")
            qs = parse_qs(urlparse(self.path).query)
            if path == "/_control/forbidden":
                self._json(200, {"forbidden": sorted(forbidden)})
                return 200
            if path == "/rest/api/user/current":
                self._json(200, {"username": "stress",
                                 "displayName": "Stress User"})
                return 200

            parts = path.split("/")
            if len(parts) >= 5 and parts[1:4] == ["rest", "api", "content"]:
                pid = parts[4]
                if path.endswith("/child/page"):
                    kids = tree.children.get(pid, [])
                    limit = int(qs.get("limit", ["200"])[0])
                    start = int(qs.get("start", ["0"])[0])
                    page = kids[start:start + limit]
                    self._json(200, {
                        "results": [{"id": k} for k in page],
                        "size": len(page), "limit": limit, "start": start})
                    return 200
                if pid in forbidden:
                    self._json(403, {"message": "not permitted"})
                    return 403
                p = tree.pages.get(pid)
                if p:
                    self._json(200, {
                        "id": pid, "title": p["title"],
                        "version": {"number": p["version"]},
                        "body": {"storage": {"value": p["body"]}},
                        "ancestors": [{"id": a} for a in p["ancestors"]],
                        "_links": {
                            "webui": f"/pages/viewpage.action?pageId={pid}",
                            "base": f"http://127.0.0.1:"
                                    f"{self.server.server_port}"}})
                    return 200
            self._json(404, {})
            return 404

    return FakeWorkDC


def _ln_mu(cfg: Hostility) -> float:
    import math
    return math.log(cfg.median_latency)


def serve(tree: FakeTree, cfg: Hostility | None = None,
          port: int = 0, log_file: str | None = None
          ) -> tuple[ThreadingHTTPServer, str, list]:
    """In-process server (used by unit tests). The stress harness spawns
    `main()` in a subprocess instead, so fake-DC CPU never pollutes the
    measured MCP server process."""
    cfg = cfg or Hostility()
    log: list = []
    srv = ThreadingHTTPServer(("127.0.0.1", port),
                              make_handler(tree, log, cfg, log_file))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", log


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--hostile", action="store_true")
    ap.add_argument("--clean-latency", type=float, default=0.05)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--profile-file", default=None,
                    help="write {'base_url', 'token'} here once listening")
    ap.add_argument("--log-file", default=None,
                    help="append every request as a JSON line")
    args = ap.parse_args()
    tree = build_tree(args.pages, args.seed)
    cfg = Hostility(enabled=args.hostile, clean_latency=args.clean_latency)
    srv, base, _log = serve(tree, cfg, log_file=args.log_file)
    if args.profile_file:
        with open(args.profile_file, "w") as f:
            json.dump({"base_url": base, "token": TOKEN}, f)
    print(f"fake-work-dc ready {base} pages={len(tree.pages)} "
          f"hostile={cfg.enabled}", flush=True)
    try:
        while True:  # serve() already runs the accept loop on a thread
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
