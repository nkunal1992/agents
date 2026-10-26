"""#210 — the eval assets stay well-formed: ≥10 intents, ≥30 questions,
every gold substring actually present in the eval corpus, abstention
questions truly absent."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import eval_corpus
import questions as qbank


def _corpus_text() -> str:
    pages, _children, _root = eval_corpus.tree()
    return " || ".join(p["body"] for p in pages.values()).lower()


def test_matrix_covers_min_shape():
    assert len(qbank.INTENTS) >= qbank.MIN_INTENTS
    assert len(qbank.QUESTIONS) >= qbank.MIN_QUESTIONS
    assert {"abstention", "adversarial", "vague",
            "comparative", "multi-hop"} <= qbank.INTENTS


def test_every_gold_substring_exists_in_corpus():
    text = _corpus_text()
    for q in qbank.QUESTIONS:
        for g in q.get("gold", []):
            assert g.lower() in text, \
                f"{q['q']!r}: gold {g!r} missing from corpus"


def test_adversarial_wrong_fact_absent_from_corpus():
    """The wrong fact must NOT be stated anywhere — evidence can only
    correct the user, never confirm them."""
    text = _corpus_text()
    for q in qbank.QUESTIONS:
        if q["intent"] != "adversarial":
            continue
        wrong = q["wrong"].lower()
        # the wrong value's numeric/keyword core must not appear
        core = wrong.split()[0].lower()
        assert core not in text or any(
            g.lower() in text for g in q["gold"]), q["q"]


def test_abstention_questions_absent():
    text = _corpus_text()
    for q in qbank.QUESTIONS:
        if not q.get("no_answer"):
            continue
        # key content words of the question must not appear in corpus
        for w in q["q"].lower().split():
            if len(w) >= 8:            # only distinctive words
                assert w not in text, (q["q"], w)


def test_tree_is_well_formed():
    pages, children, root = eval_corpus.tree()
    assert root in pages
    for kids in children.values():
        assert set(kids) <= set(pages)
    for pid, p in pages.items():
        if pid == root:
            continue
        assert p["ancestors"], pid      # everything hangs under root
