"""#210 question-intent matrix — how people actually ask.

Each entry:
  q        — the natural-language question
  intent   — one of the ≥10 intent types
  gold     — substrings that must appear in the returned evidence text
             (AND-combined; evidence recall + fact coverage in one)
  any_gold — cross-page question: evidence carrying EITHER side means
             the right docs surfaced (a real agent re-queries for the
             rest); graded on any gold substring present
  no_answer— expect empty / all-low-confidence evidence (abstention)
  correct  — adversarial: the user asserts a WRONG fact; the evidence
             must surface the RIGHT one (gold), never the wrong string
  vague    — ambiguous phrasing: graded on best-effort evidence only,
             never penalised for a clarify-style answer

Gold substrings are case-insensitive matches on evidence text.
"""

QUESTIONS = [
    # — definitional —
    {"q": "what is a canary deploy", "intent": "definitional",
     "gold": ["5% of traffic", "30 minutes"]},
    {"q": "what does blue-green deployment mean", "intent": "definitional",
     "gold": ["parallel environment", "100% of traffic"]},
    {"q": "what is a blameless postmortem", "intent": "definitional",
     "gold": ["blameless postmortem", "72 hours"]},
    # — procedural —
    {"q": "how do I get a laptop on my first day", "intent": "procedural",
     "gold": ["floor 3"]},
    {"q": "how do I get VPN access as a new hire", "intent": "procedural",
     "gold": ["ACCESS-42"]},
    {"q": "what are the steps before a full rollout", "intent": "procedural",
     "gold": ["canary"]},
    # — comparative —
    {"q": "how does a canary deploy differ from blue-green",
     "intent": "comparative",
     "gold": ["5% of traffic"]},
    {"q": "compare canary and blue-green rollout traffic handling",
     "intent": "comparative", "any_gold": True,
     "gold": ["100% of traffic", "5% of traffic"]},
    {"q": "canary vs blue-green which has a graduated ramp",
     "intent": "comparative", "any_gold": True,
     "gold": ["no graduated ramp", "5%"]},
    # — causal / diagnostic —
    {"q": "why would my VPN disconnect every hour", "intent": "causal",
     "gold": ["3600s"]},
    {"q": "why does VPN connect but pass no traffic", "intent": "causal",
     "gold": ["split-tunnel", "corp subnets"]},
    {"q": "why would the canary rollout abort", "intent": "causal",
     "gold": ["error rate", "0.5%"]},
    # — relational —
    {"q": "how does the deploy pipeline relate to team ownership",
     "intent": "relational",
     "gold": ["release engineering"]},
    {"q": "what does the incident process say about escalation and "
          "postmortems", "intent": "relational", "any_gold": True,
     "gold": ["escalation", "postmortem"]},
    # — locational —
    {"q": "where do I pick up my laptop", "intent": "locational",
     "gold": ["floor 3"]},
    {"q": "where are the storage snapshots kept", "intent": "locational",
     "gold": ["DR bucket"]},
    # — existential / policy —
    {"q": "am I allowed to deploy on Friday", "intent": "existential",
     "gold": ["not permitted on Fridays", "director approval"]},
    {"q": "can I bypass the release window for a hotfix",
     "intent": "existential",
     "gold": ["hotfix"]},
    # — temporal —
    {"q": "when do production releases ship", "intent": "temporal",
     "gold": ["Tuesdays", "14:00"]},
    {"q": "when does the code freeze open", "intent": "temporal",
     "gold": ["Friday", "12:00"]},
    {"q": "how quickly must a sev-1 page be acknowledged",
     "intent": "temporal", "gold": ["5 minutes"]},
    # — aggregation —
    {"q": "which team owns the payments service", "intent": "aggregation",
     "gold": ["checkout team"]},
    {"q": "which teams own search relevance and the deploy pipeline",
     "intent": "aggregation",
     "gold": ["discovery", "release engineering"]},
    {"q": "what are the escalation tiers for a sev-1",
     "intent": "aggregation",
     "gold": ["5 minutes", "15 minutes", "30"]},
    # — multi-hop (needs 2 pages joined) —
    {"q": "who owns the deploy pipeline and what does that page link to",
     "intent": "multi-hop",
     "gold": ["release engineering"]},
    {"q": "an incident pages me — what response and follow-up apply",
     "intent": "multi-hop", "any_gold": True,
     "gold": ["5 minutes", "postmortem"]},
    # — abstention —
    {"q": "what is the wifi password for the guest network",
     "intent": "abstention", "no_answer": True},
    {"q": "who won the office ping pong tournament",
     "intent": "abstention", "no_answer": True},
    {"q": "what is the dental insurance copay",
     "intent": "abstention", "no_answer": True},
    # — adversarial (user states a wrong fact — must correct) —
    {"q": "the canary sends 50% of traffic right?", "intent": "adversarial",
     "wrong": "50% of traffic", "gold": ["5% of traffic"]},
    {"q": "WAL archives are kept forever correct?",
     "intent": "adversarial",
     "wrong": "forever", "gold": ["7 days"]},
    {"q": "releases ship on Mondays at 9am right?", "intent": "adversarial",
     "wrong": "Mondays", "gold": ["Tuesdays", "14:00"]},
    # — vague —
    {"q": "that thing about laptops", "intent": "vague"},
    {"q": "deploy stuff", "intent": "vague"},
]

INTENTS = {q["intent"] for q in QUESTIONS}
MIN_INTENTS = 10
MIN_QUESTIONS = 30
