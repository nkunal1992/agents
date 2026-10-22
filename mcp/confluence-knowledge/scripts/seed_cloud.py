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

random.seed(20251022)


def load_env() -> dict:
    env = {}
    if not ENV_FILE.exists():
        sys.exit(f"credentials file missing: {ENV_FILE} — fill in "
                 "CONFLUENCE_CLOUD_SITE/EMAIL/TOKEN")
    for line in ENV_FILE.read_text().splitlines():
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


# ------------------------------- the tree --------------------------------

def corpus() -> list[dict]:
    """Returns a flat spec: {title, parent_title, body} — parents come
    first so ids resolve by the time children are created."""
    T = []

    def add(title, parent, body):
        T.append({"title": title, "parent": parent, "body": body})

    root = "Engineering Knowledge Base"
    add(root, None, prose(root, "the engineering knowledge base", [
        ("This space contains release procedures, architecture "
        "specifications, operational runbooks and the team handbook."),
        ("Navigation is hierarchical — each section below expands into "
        "sub-pages covering the detailed procedure or specification."),
    ]))

    sections = {
        "Release & Deployment": [
            ("Release Procedure v4.2", ["Releases ship Thursday 18:00.",
             "The release engineer owns the end-to-end window.",
             "A failed smoke test inside 15 minutes triggers rollback."]),
            ("Canary Rollout Guide", [
                (f"Step 1: deploy the blue environment. Step 2: direct "
                f"10% of traffic to blue for 30 minutes. Step 3: raise "
                f"to 50% for one hour. Step 4: full cutover. The canary "
                f"codeword for audit trails is {CANARY}."),
                ("If error rate exceeds 0.5% at any stage, pause the "
                "rollout and page the release engineer."),
                ("Canary phases are recorded in the release log with the "
                "codeword so audits can correlate them later.")]),
            ("Hotfix Policy", [("Hotfixes bypass the Thursday window with "
                               "staff-engineer approval."),
                               ("Every hotfix lands behind a feature flag "
                               "defaulted off.")]),
            ("Rollback Playbook", [
                ("Rollback restores the previous container image tag; "
                "database migrations are always backwards-compatible "
                "for two releases."),
                ("The on-call engineer executes rollback within 15 "
                "minutes of a red deployment signal.")]),
        ],
        "Architecture": [
            ("Database Failover Specification", [
                ("Failover triggers after 3000ms of heartbeat timeout "
                "across three consecutive probes."),
                ("The standby promotes to primary within 12 seconds; "
                "clients retry on the connection-pool level."),
                ("Split-brain is prevented by a consensus lock held on "
                "the coordinator service.")]),
            ("Service Mesh Topology", [
                ("All service-to-service traffic traverses the mesh; "
                "east-west TLS is mandatory."),
                ("Retries are capped at three attempts with exponential "
                "backoff starting at 100ms.")]),
            ("Event Schema Registry", [
                ("Schemas evolve under backward-compatibility rules "
                "enforced by CI."),
                "Deprecated fields are removed after two minor releases."]),
        ],
        "Operations": [
            ("Incident Playbook", [
                ("Sev-1 pages the on-call engineer and the incident "
                "commander simultaneously; acknowledgment required "
                "inside 5 minutes."),
                ("Escalation ladder: on-call → team lead (15 min) → "
                "engineering manager (30 min) → director (45 min)."),
                ("Every incident ends with a blameless postmortem within "
                "72 hours.")]),
            ("On-Call Handbook", [
                ("The rotation is weekly, handoff Monday 10:00 with a "
                "written summary."),
                ("On-call carries the escalation phone and the runbook "
                "tablet.")]),
            ("Monitoring Baselines", [
                ("Alert thresholds: p99 latency 800ms, error rate 0.5%, "
                "queue depth 5000."),
                ("Dashboards refresh every 30 seconds; anomaly review "
                "happens in the weekly ops sync.")]),
        ],
        "Team Handbook": [
            ("Onboarding Checklist", [
                ("Week one: environment setup, first PR merged, buddy "
                "introductions complete."),
                ("Access requests route through the IT portal and close "
                "within one business day.")]),
            ("Code Review Norms", [
                ("Two approvals on protected branches; the author "
                "addresses comments or marks them resolved with a reply."),
                "Reviews aim to start within four business hours."]),
            ("Meeting Cadence", [
                ("Standup is asynchronous in chat; the weekly sync is "
                "Wednesday 14:00 for 45 minutes."),
                "Retros happen every second Friday, alternating hosts."]),
        ],
    }

    for section, pages in sections.items():
        add(section, root, prose(section, f"the {section.lower()} area", [
            (f"This section collects the team's {section.lower()} "
            "documentation. Each child page covers one procedure in "
            "operational detail.")]))
        for title, facts in pages:
            add(title, section, prose(title,
                                      title.lower(), facts))
            # each top page gets a batch of sub-pages — real depth
            for i in range(1, 13):
                kind = random.choice(["checklist", "notes", "drill",
                                      "template"])
                add(f"{title} — {kind.title()} {i}", title,
                    prose(f"{kind.title()} {i}",
                          f"{title.lower()} ({kind} {i})", [
                        (f"This {kind} belongs to {title} and is "
                        f"maintained with it."),
                        (f"Checklist item {i}: verify the procedure "
                        f"end-to-end in staging before production."),
                    ]))

    # stress shapes: two very long pages + a few near-empty ones
    add("Capacity Planning — Long Record", "Architecture",
        long_page("capacity planning"))
    add("Ops Runbook — Extended Log", "Operations",
        long_page("extended operations log", 40))
    for i in range(4):
        add(f"Scratch Note {i}", "Team Handbook",
            para(f"Placeholder — to be expanded. Ref note {i}."))
    return T


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
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
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
        MANIFEST.write_text("{}")
        print("wiped")
        return 0

    # create parents first — spec order guarantees that
    tree = corpus()
    id_of = {t: m["id"] for t, m in manifest.items()}
    created = skipped = 0
    for spec in tree:
        title = spec["title"]
        if title in id_of:
            skipped += 1
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
                                       "parent": spec["parent"]}
                    id_of[title] = page["id"]
                    print(f"adopted existing {title}")
                    continue
            print(f"FAIL create {title}: HTTP {e.code} — "
                  f"{e.read()[:200]}")
            continue
        manifest[title] = {"id": page["id"],
                           "version": page["version"]["number"],
                           "parent": spec["parent"]}
        id_of[title] = page["id"]
        created += 1
        if created % 25 == 0:
            MANIFEST.write_text(json.dumps(manifest, indent=1))
            print(f"  {created} pages created…")
        time.sleep(PAUSE_S)
    MANIFEST.write_text(json.dumps(manifest, indent=1))
    print(f"done — {created} created, {skipped} skipped, "
          f"{len(manifest)} total in space {space_key}")
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
