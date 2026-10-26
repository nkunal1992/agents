#!/usr/bin/env python3
"""Seed a ~200-page realistic corpus into a real Confluence Cloud space
(#211 real-data leg). Credentials come from ~/.config/agents/cloud.env —
never committed, never logged.

    CONFLUENCE_CLOUD_SITE  e.g. https://you.atlassian.net
    CONFLUENCE_CLOUD_EMAIL
    CONFLUENCE_CLOUD_TOKEN  id.atlassian.com → API tokens
    CONFLUENCE_CLOUD_SPACE  optional — defaults to E2E

Idempotent: a manifest at ~/.config/agents/seed_manifest.json maps title →
page id so re-runs skip existing pages. --reseed bumps a few page versions
so follow-up crawls exercise the changed-page path. --wipe deletes every
manifested page (fresh slate).

Bodies are Confluence storage-format XHTML — headings, paragraphs, tables,
info/warning macros, a few near-empty pages and two very long pages — the
same mixed surface the fake DC corpus exercises, but served by real Cloud.
"""
from __future__ import annotations

import base64
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / ".config/agents/cloud.env"
MANIFEST = Path.home() / ".config/agents/seed_manifest.json"
SPACE_DEFAULT = "E2E"
PAUSE_S = 0.35          # polite pacing on the free tier
CANARY = "CONFLUENCE_CANARY_FACT_4482"
WAVE2_CANARY = "WAVE2_CANARY_7711"

random.seed(20251022)


def load_env() -> dict:
    env = {}
    if not ENV_FILE.exists():
        sys.exit(f"credentials file missing: {ENV_FILE} — fill in "
                 "CONFLUENCE_CLOUD_SITE/EMAIL/TOKEN")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for k in ("CONFLUENCE_CLOUD_SITE", "CONFLUENCE_CLOUD_EMAIL",
              "CONFLUENCE_CLOUD_TOKEN"):
        if not env.get(k):
            sys.exit(f"{k} empty in {ENV_FILE}")
    env["CONFLUENCE_CLOUD_SPACE"] = \
        env.get("CONFLUENCE_CLOUD_SPACE") or SPACE_DEFAULT
    return env


def api(env: dict, method: str, path: str, body: dict | None = None,
        retry: int = 4):
    """One REST call with 429/5xx backoff — token lives only in headers."""
    auth = base64.b64encode(
        f"{env['CONFLUENCE_CLOUD_EMAIL']}:"
        f"{env['CONFLUENCE_CLOUD_TOKEN']}".encode()).decode()
    url = f"{env['CONFLUENCE_CLOUD_SITE'].rstrip('/')}/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retry):
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Basic {auth}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("Retry-After", 2)) + 1)
                continue
            if e.code >= 500 and attempt < retry - 1:
                time.sleep(1 + attempt)
                continue
            raise


# ------------------------- storage-format bodies -------------------------

def para(text: str) -> str:
    return f"<p>{text}</p>"


def table(rows: list[list[str]]) -> str:
    out = ["<table><tbody>"]
    for i, row in enumerate(rows):
        tag = "th" if i == 0 else "td"
        out.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in row)
                   + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def macro(name: str, body: str) -> str:
    return (f'<ac:structured-macro ac:name="{name}"><ac:rich-text-body>'
            f"<p>{body}</p></ac:rich-text-body></ac:structured-macro>")


def prose(title: str, topic: str, facts: list[str]) -> str:
    """A realistic mixed page — h2 sections, prose, a table, one macro."""
    out = [f"<h2>{title} — overview</h2>",
           para(f"This page describes {topic} as practiced by the "
                f"platform engineering team. It is maintained as the "
                "single source of truth for the procedure and is "
                "reviewed at the start of every release cycle."),
           para(facts[0] if facts else
                "All changes follow the standard change-window policy.")]
    if len(facts) > 1:
        out += ["<h2>Details</h2>", para(facts[1])]
    out += ["<h2>Checklist</h2>",
            table([["Step", "Owner", "Verify"],
                   ["Prepare artifacts", "Release engineer", "CI green"],
                   ["Announce window", "Team lead", "Status page updated"],
                   ["Execute", "On-call", "Monitoring nominal"],
                   ["Verify + sign off", "QA", "Checklist complete"]]),
            macro("info", "Contact #platform-eng in chat for exceptions.")]
    if len(facts) > 2:
        out += ["<h2>Notes</h2>", para(facts[2])]
    return "".join(out)


def long_page(topic: str, paragraphs: int = 60) -> str:
    out = [f"<h2>{topic}</h2>"]
    for i in range(paragraphs):
        out.append(para(f"Section {i + 1}: detailed operational notes for "
                        f"{topic}. Configuration item cfg_{i} = {i * 7 % 13}; "
                        f"expected behaviour under load is documented in the "
                        f"capacity planner. Recovery step {i + 1} is verified "
                        "quarterly."))
    return "".join(out)


# ---- enterprise-density bodies — the work-laptop realism profile ----
# A real wiki's page mix: dense tables, macros, code blocks, long section
# trees. Chunk counts per page land ~15-150 (vs the old thin corpus's ~5)
# which is what makes the embed phase dominate like it does on real
# Confluence instances.

def code_macro(lines: int = 14) -> str:
    body = "<![CDATA[" + "\n".join(
        f"step_{i} = run(command_{i}, retries=3, backoff=100*2**i)"
        for i in range(lines)) + "]] >"
    return ('<ac:structured-macro ac:name="code"><ac:plain-text-body>'
            f"{body}</ac:plain-text-body></ac:structured-macro>")


def expand_macro(title: str, paras: int) -> str:
    inner = "".join(para(f"{title} detail {i}: procedure verified during "
                         f"the last drill; owner sign-off recorded in the "
                         "change log.")
                    for i in range(paras))
    return ('<ac:structured-macro ac:name="expand"><ac:parameter '
            f'ac:name="title">{title}</ac:parameter><ac:rich-text-body>'
            f"{inner}</ac:rich-text-body></ac:structured-macro>")


def config_matrix(rows: int = 60) -> str:
    out = [("<table><tbody><tr><th>Parameter</th><th>Value</th>"
           "<th>Owner</th><th>Notes</th></tr>")]
    for i in range(rows):
        out.append(
            f"<tr><td>cfg_{i}</td><td>{i * 7 % 13}.{i % 10}</td>"
            f"<td>team-{i % 5}</td><td>tuned {2020 + i % 5}-Q{i % 4 + 1}"
            "</td></tr>")
    out.append("</tbody></table>")
    return "".join(out)


def medium_body(topic: str, sections: int = 6) -> str:
    """~15-40 chunks: headed sections + tables + one macro each."""
    out = [f"<h2>{topic}</h2>",
           para(f"Operational runbook for {topic}. Reviewed every release "
                "cycle; deviations need staff-engineer sign-off.")]
    for i in range(sections):
        out += [f"<h3>{topic} — part {i + 1}</h3>",
                para(f"Part {i + 1} covers the {topic.lower()} steps that "
                     f"run in window {i + 1}. The checklist below is "
                     "executed in order and verified before the next "
                     "phase begins."),
                table([["Step", "Command", "Verify"],
                       [f"{i + 1}.1", f"deploy --stage {i}",
                        "health check green"],
                       [f"{i + 1}.2", "smoke --suite core",
                        "0 failures"],
                       [f"{i + 1}.3", "annotate --log release",
                        "entry present"]]),
                macro("info" if i % 3 else "warning",
                      f"{topic} part {i + 1}: page the on-call if the "
                      "verify step fails twice."),
                code_macro(lines=8)]
    return "".join(out)


def long_dense_body(topic: str, sections: int = 12) -> str:
    """~60-150 chunks: deep sections, config matrices, expand+code macros."""
    out = [f"<h2>{topic}</h2>",
           para(f"Long-form specification for {topic}. This page is the "
                "detailed record — the summary lives in the parent "
                "runbook.")]
    for i in range(sections):
        out += [f"<h3>{topic} — section {i + 1}</h3>",
                para(f"Section {i + 1} documents the {topic.lower()} "
                     f"behaviour under load profile {i + 1}. Thresholds "
                     "are production-measured; the matrix below is the "
                     "authoritative tuning record."),
                config_matrix(rows=30 + (i % 4) * 15),
                expand_macro(f"{topic} — detail {i + 1}", paras=5),
                code_macro(lines=10)]
    return "".join(out)


def short_body(topic: str) -> str:
    return (f"<h2>{topic}</h2>" + para(f"Brief note on {topic}.") +
            para("Follow-up owner and date recorded in the tracker."))


# ------------------------------- the tree --------------------------------

def rich_corpus() -> list[dict]:
    """~300 pages, enterprise-density mix — ~20% short notes, ~55%
    medium runbooks/guides, ~25% long dense specs. Canary facts live at
    fixed pages so assertions stay deterministic."""
    T = []

    def add(title, parent, body):
        T.append({"title": title, "parent": parent, "body": body})

    root = "Engineering Knowledge Base"
    add(root, None, prose(root, "the engineering knowledge base", [
        ("This space contains release procedures, architecture "
        "specifications, operational runbooks and the team handbook."),
        ("Navigation is hierarchical — each section expands into "
        "sub-pages covering the detailed procedure or specification."),
    ]))

    sections = ["Release & Deployment", "Architecture", "Operations",
                "Team Handbook", "Platform Engineering", "Security"]
    for s in sections:
        add(s, root, prose(s, f"the {s.lower()} area", [
            (f"This section collects the team's {s.lower()} "
            "documentation. Each child page covers one procedure in "
            "operational detail.")]))

    anchor = [
        ("Canary Rollout Guide", "Release & Deployment", prose(
            "Canary Rollout Guide", "canary rollouts", [
             ("Step 1: deploy the blue environment. Step 2: direct 10% "
             "of traffic to blue for 30 minutes. Step 3: raise to 50% "
             "for one hour. Step 4: full cutover. The audit codeword "
             f"is {CANARY}. Pause on error rate >0.5%."),
             ("Canary phases are recorded in the release log with the "
             "codeword so audits can correlate them later."),
             ("Roll back by restoring the previous image tag — "
             "migrations stay backwards-compatible for two releases.")])),
        ("Database Failover Specification", "Architecture", prose(
            "Database Failover Specification", "database failover", [
             ("Failover triggers after 3000ms heartbeat timeout across "
             "three consecutive probes."),
             ("The standby promotes to primary within 12 seconds; "
             "clients retry on the connection-pool level."),
             ("Split-brain is prevented by a consensus lock held on "
             "the coordinator service.")])),
        ("Incident Playbook", "Operations", prose(
            "Incident Playbook", "incident handling", [
             "Sev-1 pages on-call + incident commander; ack in 5 min.",
             ("Escalation ladder: on-call → team lead (15 min) → "
             "engineering manager (30 min) → director (45 min)."),
             "Blameless postmortem within 72 hours."])),
    ]
    for title, parent, body in anchor:
        add(title, parent, body)

    # ~55% medium runbooks/guides — 8 sections × ~20 each
    n_med = 0
    for s in sections:
        for i in range(20):
            n_med += 1
            topic = f"{s} Runbook {i + 1}"
            add(topic, s, medium_body(topic, sections=6 + i % 3))
            # a few get one drill sub-page each (depth + variety)
            if i % 4 == 0:
                add(f"{topic} — Drill", topic,
                    medium_body(f"{topic} drill", sections=3))

    # ~25% long dense specs — config matrices + expand/code macros
    n_long = 0
    for s in sections:
        for i in range(12):
            n_long += 1
            topic = f"{s} Specification {i + 1}"
            add(topic, s, long_dense_body(topic, sections=10 + i % 5))

    # ~20% short notes
    n_short = 0
    for s in sections:
        for i in range(10):
            n_short += 1
            add(f"{s} — Note {i + 1}", s, short_body(
                f"{s.lower()} note {i + 1}"))
    print(f"corpus spec: {len(T)} pages "
          f"({n_med} medium, {n_long} long, {n_short} short)")
    return T


def wave2_corpus() -> list[dict]:
    """+~400 pages, fatter than wave 1 — giant config matrices, deep
    expand trees, wide children groups. Wave-2 canary lives on the
    anchor spec page."""
    T = []

    def add(title, parent, body):
        T.append({"title": title, "parent": parent, "body": body})

    anchor_parent = "Extended Corpus"
    add(anchor_parent, "Engineering Knowledge Base", prose(
        "Extended Corpus", "the extended corpus", [
            ("Second-wave corpus — deeper, denser pages exercising the "
            "crawler at enterprise scale."),
            ("Each section below holds long-form specifications with "
            "large tables, macros and deep expand trees.")]))

    add("Extended Spec — Audit Record", anchor_parent, prose(
        "Extended Spec — Audit Record", "the extended audit record", [
            (f"The wave-2 audit codeword is {WAVE2_CANARY} — verify it "
            "appears in the audit log within the same release cycle."),
            ("Long records below reproduce the density of a real audit "
            "evidence page.")]))
    # the anchor spec gets a genuinely giant body (200+ chunks)
    T[-1]["body"] = long_dense_body("Extended Audit Record", 16) + \
        macro("info", f"Audit codeword: {WAVE2_CANARY}")

    # 8 deep-dive sections × ~50 pages, heavier long-page share (~40%)
    sections = ["Reliability Engineering", "Platform Internals",
                "Delivery Process", "Data Platform",
                "Developer Tooling", "Quality Engineering",
                "Service Operations", "Enterprise Integrations"]
    for s in sections:
        add(s, anchor_parent, prose(s, f"the {s.lower()} area", [
            (f"Extended {s.lower()} documentation — long-form "
            "specifications and runbook archives.")]))
        for i in range(25):          # 20 long + 20 medium + 10 short
            topic = f"{s} Spec {i + 1}"
            add(topic, s, long_dense_body(topic, sections=12 + i % 6))
        for i in range(20):
            topic = f"{s} Runbook {i + 1}"
            add(topic, s, medium_body(topic, sections=6 + i % 4))
        for i in range(5):
            add(f"{s} Note {i + 1}", s, short_body(
                f"{s.lower()} note {i + 1}"))
    print(f"wave-2 corpus spec: {len(T)} pages")
    return T


def corpus(wave: int = 1) -> list[dict]:
    return wave2_corpus() if wave == 2 else rich_corpus()


def find_by_title(env, space_key: str, title: str):
    """Server-truth idempotence: the manifest can lag behind a crashed
    run — a page that exists remotely but not locally gets adopted."""
    from urllib.parse import urlencode
    q = urlencode({"title": title, "spaceKey": space_key,
                   "expand": "version"})
    try:
        res = api(env, "GET", f"wiki/rest/api/content?{q}")["results"]
    except urllib.error.HTTPError:
        return None
    return res[0] if res else None


def ensure_space(env) -> str:
    spaces = api(env, "GET", "wiki/api/v2/spaces?limit=100")["results"]
    key = env["CONFLUENCE_CLOUD_SPACE"]
    for s in spaces:
        if s["key"] == key:
            return s["id"]
    raise SystemExit(f"space {key} not found — create it first "
                     f"(the space id was 65665 for E2E)")


def main() -> int:
    reseed = "--reseed" in sys.argv
    wipe = "--wipe" in sys.argv
    env = load_env()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    ensure_space(env)
    space_key = env["CONFLUENCE_CLOUD_SPACE"]

    if wipe:
        for title, meta in list(manifest.items()):
            try:
                api(env, "DELETE", f"wiki/rest/api/content/{meta['id']}")
                print("deleted", title)
            except urllib.error.HTTPError as e:
                print("delete failed", title, e.code)
            time.sleep(PAUSE_S)
        MANIFEST.write_text("{}", encoding="utf-8")
        print("wiped")
        return 0

    # create parents first — spec order guarantees that
    import hashlib
    wave = 2 if "--wave2" in sys.argv else 1
    tree = corpus(wave)
    id_of = {t: m["id"] for t, m in manifest.items()}
    created = skipped = updated = 0
    for spec in tree:
        title = spec["title"]
        bhash = hashlib.sha256(spec["body"].encode()).hexdigest()[:12]
        if title in id_of:
            if manifest[title].get("body_hash") == bhash:
                skipped += 1
                continue
            # body changed (e.g. thin → dense upgrade) — bump the page
            # version so the crawl's changed-page path gets exercised
            cur = manifest[title]
            try:
                api(env, "PUT", f"wiki/rest/api/content/{cur['id']}", {
                    "id": cur["id"], "type": "page", "title": title,
                    "space": {"key": space_key},
                    "version": {"number": cur["version"] + 1},
                    "body": {"storage": {"value": spec["body"],
                                         "representation": "storage"}}})
            except urllib.error.HTTPError as e:
                print(f"FAIL update {title}: HTTP {e.code}")
                continue
            cur["version"] += 1
            cur["body_hash"] = bhash
            updated += 1
            if updated % 20 == 0:
                MANIFEST.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
                print(f"  {updated} pages upgraded…")
            time.sleep(PAUSE_S)
            continue
        parent_id = id_of.get(spec["parent"]) if spec["parent"] else None
        if spec["parent"] and parent_id is None:
            print(f"skip {title} — parent {spec['parent']} missing")
            continue
        body = {"type": "page", "title": title,
                "space": {"key": space_key},
                "body": {"storage": {"value": spec["body"],
                                     "representation": "storage"}}}
        if parent_id:
            body["ancestors"] = [{"id": parent_id}]
        try:
            page = api(env, "POST", "wiki/rest/api/content", body)
        except urllib.error.HTTPError as e:
            if e.code == 400:               # title already exists
                page = find_by_title(env, space_key, title)
                if page is not None:
                    manifest[title] = {"id": page["id"],
                                       "version": page["version"]
                                       ["number"],
                                       "parent": spec["parent"],
                                       "body_hash": bhash}
                    id_of[title] = page["id"]
                    print(f"adopted existing {title}")
                    continue
            print(f"FAIL create {title}: HTTP {e.code} — "
                  f"{e.read()[:200]}")
            continue
        manifest[title] = {"id": page["id"],
                           "version": page["version"]["number"],
                           "parent": spec["parent"],
                           "body_hash": bhash}
        id_of[title] = page["id"]
        created += 1
        if created % 25 == 0:
            MANIFEST.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
            print(f"  {created} pages created…")
        time.sleep(PAUSE_S)
    MANIFEST.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"done — {created} created, {updated} upgraded, "
          f"{skipped} skipped, {len(manifest)} total in space "
          f"{space_key}")
    root = manifest.get("Engineering Knowledge Base")
    if root:
        print(f"root page id: {root['id']}  "
              f"({env['CONFLUENCE_CLOUD_SITE']}/wiki/pages/viewpage."
              f"action?pageId={root['id']})")

    if reseed:
        # bump a handful of pages — next crawl exercises the delta path
        bumped = 0
        for title in random.sample(sorted(manifest), k=8):
            meta = manifest[title]
            spec = next((s for s in tree if s["title"] == title), None)
            if not spec:
                continue
            api(env, "PUT", f"wiki/rest/api/content/{meta['id']}", {
                "id": meta["id"], "type": "page", "title": title,
                "space": {"key": space_key},
                "version": {"number": meta["version"] + 1},
                "body": {"storage": {
                    "value": spec["body"] + para(
                        f"Revised {time.strftime('%Y-%m-%d %H:%M')} — "
                        "reseed bump."),
                    "representation": "storage"}}})
            bumped += 1
            time.sleep(PAUSE_S)
        print(f"reseeded {bumped} page versions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
