"""#210 conversational-eval corpus — a DC-shaped tree built for the
question-intent matrix. Overlapping entities feed comparative questions;
cross-linked pages feed multi-hop; planted facts feed adversarial ones.

Shape matches _fake_dc_work.FakeTree: pages {id: {title, version,
ancestors, body}}, children {id: [ids]}, root. Bodies are storage-format
HTML — what the crawler consumes.
"""

PAGES = {
    "2001": {
        "title": "Engineering Handbook",
        "version": 1, "ancestors": [],
        "body": "<p>Root of the engineering handbook space. Covers "
                "releases, on-call, security and IT procedures.</p>"},
    "2002": {
        "title": "Release Calendar",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Release Calendar</h2><p>Production releases ship on "
                "Tuesdays at 14:00 UTC. The code freeze opens the prior "
                "Friday at 12:00 UTC. Hotfixes may ship any day behind "
                "the hotfix flag.</p>"},
    "2003": {
        "title": "Canary Deployment Guide",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Canary Deployment</h2><p>A canary deploy sends 5% "
                "of traffic to the new build for 30 minutes while the "
                "error budget is watched. If the error rate stays under "
                "0.5% the rollout continues; otherwise it rolls back "
                "automatically. The canary stage always precedes the "
                "full rollout.</p>"},
    "2004": {
        "title": "Blue-Green Deployment Guide",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Blue-Green Deployment</h2><p>A blue-green deploy "
                "provisions a full parallel environment, then switches "
                "100% of traffic at the load balancer. There is no "
                "graduated ramp — the cutover is atomic and the old "
                "environment stays warm for a 2-hour rollback window."
                "</p>"},
    "2005": {
        "title": "Friday Deploy Policy",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Friday Deploy Policy</h2><p>Production deploys are "
                "not permitted on Fridays. The policy exists to keep "
                "weekends incident-quiet; exceptions need director "
                "approval filed 24 hours ahead.</p>"},
    "2006": {
        "title": "VPN Troubleshooting",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>VPN Troubleshooting</h2><p><strong>Symptom: drops "
                "every hour.</strong> The session token defaults to "
                "3600s; file an ACCESS ticket to raise it.</p>"
                "<p><strong>Symptom: connects but no traffic.</strong> "
                "Split-tunnel routes must include the corp subnets, "
                "not only DNS.</p><p><strong>Symptom: MFA rejects."
                "</strong> Re-enroll the authenticator; expired MFA "
                "registrations fail silently.</p>"},
    "2007": {
        "title": "New Hire IT Setup",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>New Hire IT Setup</h2><p>Laptops are collected "
                "from the IT desk on floor 3, open 9:00-16:00 on the "
                "first day. Email is provisioned on day one; VPN needs "
                "a separate ACCESS-42 ticket.</p>"},
    "2008": {
        "title": "Team Ownership Registry",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Team Ownership</h2><p>The payments service is "
                "owned by the checkout team. Search relevance is owned "
                "by the discovery team. The deploy pipeline is owned "
                "by the release engineering team.</p>"},
    "2009": {
        "title": "Deploy Pipeline Overview",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Deploy Pipeline</h2><p>The deploy pipeline builds, "
                "scans and ships images. Ownership details live in the "
                "Team Ownership Registry page.</p>"},
    "2010": {
        "title": "On-Call Escalation",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>On-Call Escalation</h2><p>Sev-1 incidents page "
                "the on-call engineer, who must acknowledge within 5 "
                "minutes. Unacknowledged pages escalate to the team "
                "lead at 15 minutes and the engineering manager at "
                "30.</p>"},
    "2011": {
        "title": "Postmortem Standard",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Postmortem Standard</h2><p>Every Sev-1 incident "
                "gets a blameless postmortem within 72 hours, owned by "
                "the incident commander.</p>"},
    "2012": {
        "title": "Incident Process Overview",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Incident Process</h2><p>Incidents follow the "
                "On-Call Escalation ladder for response and the "
                "Postmortem Standard for follow-up.</p>"},
    "2013": {
        "title": "API Rate Limits",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>API Rate Limits</h2><p>The public API allows 100 "
                "requests per minute per key. Exceeding it returns "
                "HTTP 429 with a Retry-After header; clients must "
                "honour the header.</p>"},
    "2014": {
        "title": "Storage Retention",
        "version": 1, "ancestors": ["2001"],
        "body": "<h2>Storage Retention</h2><p>Object snapshots are "
                "kept for 35 days. WAL archives are kept for 7 days. "
                "Both live in the DR bucket.</p>"},
}

CHILDREN = {
    "2001": ["2002", "2003", "2004", "2005", "2006", "2007", "2008",
             "2009", "2010", "2011", "2012", "2013", "2014"],
}


def tree():
    """(pages, children, root) — FakeTree-compatible."""
    return PAGES, CHILDREN, "2001"
