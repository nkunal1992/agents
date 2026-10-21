"""Synthetic Confluence-style corpus for RAG eval — every planted fact has
a gold question/chunk snippet so retrieval is assertable, not vibes.

Pages are Confluence storage-format HTML (what the crawler consumes). Each
entry: (title, html, [qa...]) where qa = (question, gold_substring) — the
gold substring must appear in the chunk that a correct search returns.
"""

CORPUS = [
    (
        "Onboarding Guide",
        """<h1>Onboarding Guide</h1>
        <p>New hires must complete the security attestation within 72 hours
        of their start date. The attestation form is on the IT portal under
        Forms &gt; Security.</p>
        <h2>Accounts</h2>
        <p>Email accounts are provisioned automatically on day one. VPN
        access requires a separate request through ticket type ACCESS-42.</p>
        <h2>Hardware</h2>
        <p>Laptop pickup happens at the IT desk on floor 3 between 9 AM and
        4 PM on your first day. Bring your badge or the pickup email.</p>""",
        [
            ("when do new hires complete the security attestation",
             "within 72 hours"),
            ("how do I get VPN access", "ticket type ACCESS-42"),
            ("where do I pick up my laptop", "floor 3"),
        ],
    ),
    (
        "Password Policy",
        """<h1>Password Policy</h1>
        <p>All systems require a minimum password length of 14 characters.
        Passwords expire every 90 days and the previous 12 passwords may
        not be reused.</p>
        <h2>Reset procedure</h2>
        <p>To reset a forgotten password, visit the self-service portal and
        verify via your registered phone number. Helpdesk resets require a
        manager's approval ticket.</p>""",
        [
            ("what is the minimum password length", "14 characters"),
            ("how often do passwords expire", "every 90 days"),
            ("reset forgotten password steps", "self-service portal"),
        ],
    ),
    (
        "Deployment Checklist",
        """<h1>Deployment Checklist</h1>
        <table>
          <tr><th>Step</th><th>Owner</th><th>Gate</th></tr>
          <tr><td>Code freeze</td><td>Release lead</td><td>T-2 days</td></tr>
          <tr><td>Canary deploy</td><td>SRE</td><td>5% traffic, 30 min</td></tr>
          <tr><td>Full rollout</td><td>SRE</td><td>canary clean</td></tr>
          <tr><td>Rollback window</td><td>Release lead</td><td>2 hours</td></tr>
        </table>
        <p>Post-deploy verification runs the smoke suite against
        /healthz on every pod.</p>
        <p>The canary stage always releases to 5% traffic for 30 minutes
        before the rollout proceeds.</p>""",
        [
            ("how long is the rollback window", "2 hours"),
            ("what percentage is the canary", "5% traffic"),
            ("what endpoint does smoke suite hit", "/healthz"),
        ],
    ),
    (
        "Troubleshooting VPN",
        """<h1>Troubleshooting VPN</h1>
        <p><strong>Symptom: connection drops every hour.</strong> Fix: the
        session timeout defaults to 3600s; ask IT to raise it for your group.</p>
        <p><strong>Symptom: DNS resolves but no traffic.</strong> Fix: check
        that split-tunnel routes include the corp subnets, not just DNS.</p>
        <p><strong>Symptom: auth rejected.</strong> Fix: re-enroll the token;
        expired MFA registrations cause silent rejections.</p>""",
        [
            ("vpn drops every hour fix", "3600s"),
            ("dns works but traffic fails vpn", "split-tunnel"),
            ("auth rejected vpn", "re-enroll the token"),
        ],
    ),
    (
        "Changelog 2025-Q3",
        """<h1>Changelog 2025-Q3</h1>
        <p>v2.14: added bulk export to CSV. v2.15: fixed a memory leak in
        long-running sync jobs. v2.16: deprecated the v1 REST endpoints;
        they are removed in v3.0.</p>""",
        [
            ("when are v1 REST endpoints removed", "v3.0"),
            ("which version fixed the memory leak", "v2.15"),
        ],
    ),
    (
        "深い階層のドキュメント",  # CJK page — sparse-leg language test
        """<h1>データベース移行ガイド</h1>
        <p>移行は3段階で行います。まずスキーマをエクスポートし、
        次に新しいクラスターにリストアし、最後にアプリケーションの
        接続文字列を切り替えます。切り替え時のダウンタイムは
        最大5分を想定してください。</p>""",
        [
            ("移行 ダウンタイム 最大", "5分"),
        ],
    ),
    (
        "Empty Announcement",  # near-empty page — degenerate case
        """<h1>Empty Announcement</h1><p>TBD.</p>""",
        [],
    ),
    (
        "Expense Reimbursement",  # near-duplicate of "Travel Expenses"
        """<h1>Expense Reimbursement</h1>
        <p>Submit receipts within 30 days of the expense date. Meals over
        $75 require itemized receipts. Mileage is reimbursed at $0.655/mile
        for personal vehicles.</p>""",
        [
            ("meal receipt threshold", "$75"),
            ("mileage reimbursement rate", "$0.655/mile"),
        ],
    ),
    (
        "Travel Expenses",  # near-duplicate — must not steal citations
        """<h1>Travel Expenses</h1>
        <p>Submit receipts within 45 days of travel completion. Flights must
        be booked through the corporate portal; direct bookings are not
        reimbursed. Hotel caps vary by city tier.</p>""",
        [
            ("flight booking requirement", "corporate portal"),
            ("travel receipt deadline", "45 days"),
        ],
    ),
    (
        "Big Reference Table",  # huge table → oversized chunk (>8k tok risk)
        "<h1>Ports Reference</h1><table>" + "".join(
            f"<tr><td>svc-{i:04d}</td><td>tcp/{10000+i}</td>"
            f"<td>internal tier {i%5}</td></tr>" for i in range(900))
        + "</table>",
        [
            ("what port does svc-0800 use", "tcp/10800"),
        ],
    ),
    # confusable-domain pairs — the "NIO router" class: two pages sharing
    # a vocabulary where naive retrieval ranks the wrong one (field case:
    # 'nio router' pulled the NIO_TTE interface doc, not the router doc)
    (
        "TNL Router Administration",
        """<h1>TNL Router</h1>
        <p>The TNL router forwards packets between the ingress fabric and
        the spine layer. It routes on destination address prefixes from
        the shared forwarding table and enforces QoS classes on egress
        queues. Reboot requires draining BGP sessions first.</p>
        <h2>Management</h2>
        <p>The router exposes an admin CLI on port 4099; configuration
        changes need operator-level auth and write to flash.</p>""",
        [
            ("how does the router forward packets",
             "destination address prefixes"),
            ("which port is the router admin CLI on", "4099"),
            ("what must happen before router reboot", "draining BGP"),
        ],
    ),
    (
        "TNL Interface Modelling",
        """<h1>TNL Interface (NIO_TNL)</h1>
        <p>NIO_TNL is a data interface — it moves blocks between the
        sequencer and the message controller using TNL descriptors. It
        does NOT route packets; it carries payload frames verbatim.
        Modelling it describes the descriptor layout, not forwarding.</p>""",
        [
            ("does the nio_tnl interface route packets", "does NOT route"),
            ("what does nio_tnl describe", "descriptor layout"),
        ],
    ),
]

# Questions whose answers are NOT in the corpus — every one must return
# empty evidence (after the rerank floor), never a grounded junk pile.
NO_ANSWER = [
    "what is the wifi password for the guest network",
    "who won the office ping pong tournament",
    "meaning of life the universe and everything",
    "気象情報の更新頻度はどのくらいですか",  # CJK no-answer too
]
