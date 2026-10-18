from html import escape


def build_audit_corpus():
    pages, questions = [], []

    def page(pid, text, title=None, raw=False):
        pages.append({"id": pid, "title": title or pid,
                      "html": text if raw else f"<p>{escape(text)}</p>"})
        return pid

    def question(category, i, text, required, facts):
        questions.append({"id": f"{category}-{i}", "category": category,
                          "question": text, "required": required, "facts": facts})

    for i in range(5):
        service = ["Atlas", "Birch", "Cobalt", "Delta", "Ember"][i]
        pid = page(f"paraphrase-{i}",
                   f"{service} account recovery: forgotten credentials are restored "
                   f"through the identity portal. Recovery links expire after {17+i} minutes.")
        question("paraphrase", i, f"I cannot remember my {service} password. "
                 "How long is the recovery link usable?", [pid], [f"{17+i} minutes"])

        pid = page(f"identifier-{i}",
                   f"ERR-X9{i} in {service} means the certificate has expired. "
                   f"Remediation: rotate certificate bundle CB-{70+i}.")
        page(f"identifier-distractor-{i}",
             f"ERR-X8{i} in {service} means disk quota exceeded. Increase volume capacity.")
        question("identifier", i, f"How do I fix ERR-X9{i}?", [pid], [f"CB-{70+i}"])

        pid = page(f"rare-{i}", f"The {service} quasarledger-{i} runbook requires "
                   f"release approval from group ZETA-{30+i}.")
        for j in range(3):
            page(f"rare-noise-{i}-{j}",
                 f"{service} release runbook approval release runbook approval: "
                 "routine changes require standard approval.")
        question("rare", i, f"{service} release runbook approval quasarledger-{i}",
                 [pid], [f"ZETA-{30+i}"])

        rows = "".join(f"<tr><td>svc-{i}-{j:03}</td><td>{12000+i*100+j}</td>"
                       "<td>tcp</td></tr>" for j in range(90))
        pid = page(f"table-{i}", "<table><tr><th>Service</th><th>Port</th>"
                   f"<th>Protocol</th></tr>{rows}</table>", raw=True)
        question("table", i, f"What TCP port does svc-{i}-087 use?",
                 [pid], [str(12087+i*100)])

        technical = " ".join(f"trace_segment_{j:04}_sha256_deadbeef" for j in range(240))
        pid = page(f"tail-{i}", f"{service} troubleshooting trace: {technical}. "
                   f"The emergency recovery code for {service} is RESCUE-{90+i}.")
        question("tail", i, f"What is the {service} emergency recovery code?",
                 [pid], [f"RESCUE-{90+i}"])

        pid = page(f"negation-{i}", f"{service} production does NOT permit direct "
                   f"deployments on Fridays. Only sandbox deployments may use Friday. "
                   f"Production requires change ticket CHG-{600+i}.")
        question("negation", i, f"Can {service} production deploy directly on Fridays?",
                 [pid], ["NOT permit", f"CHG-{600+i}"])

        pid = page(f"units-{i}", f"{service} request timeout is {240+i} milliseconds, "
                   f"not seconds. Maximum retry count is {3+i}.")
        question("units", i, f"What is the {service} request timeout and unit?",
                 [pid], [f"{240+i} milliseconds"])

        pid = page(f"title-{i}", f"The maintenance owner is group OPER-{40+i}.",
                   title=f"{service} ownership register")
        question("title", i, f"Which group owns {service} maintenance?",
                 [pid], [f"OPER-{40+i}"])

        a = page(f"multi-a-{i}", f"{service} deployment window starts at {18+i}:00 UTC.")
        b = page(f"multi-b-{i}", f"{service} incident escalation goes to group PAGE-{50+i}.")
        question("multi", i, f"When does the {service} deployment window start "
                 "and which group handles incident escalation?",
                 [a, b], [f"{18+i}:00", f"PAGE-{50+i}"])

        a = page(f"conflict-old-{i}", f"{service} policy version 2024: retain logs for 30 days.")
        b = page(f"conflict-new-{i}", f"{service} policy version 2026: retain logs for 90 days.")
        question("conflict", i, f"How did {service} log retention change between "
                 "2024 and 2026?", [a, b], ["30 days", "90 days"])

        pid = page(f"language-{i}", f"{service}の移行時の最大停止時間は{7+i}分です。"
                   "移行後に接続文字列を更新してください。")
        question("language", i, f"{service}の移行時の最大停止時間は？",
                 [pid], [f"{7+i}分"])

        page(f"unknown-near-{i}", f"{service} disaster recovery is tested quarterly. "
             "The recovery time objective has not yet been specified.")
        question("unanswerable", i, f"What is the exact disaster recovery time "
                 f"objective in minutes for {service}?", [], [])
    return pages, questions
