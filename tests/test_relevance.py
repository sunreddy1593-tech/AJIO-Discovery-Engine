"""Tier-1 keyword triage (plan §3.2.5, `architecture.md` §3).

This file exists because of a gap it would have caught. Lowering the length gate
from 8 words to 3 was supposed to admit *"does this run small?"* — the question
`edge-case.md` §1.1.13e names as the richest pre-purchase content on the roster —
into the corpus. It did. Tier 1 then dropped it for zero keyword hits, because the
vocabulary listed `runs small` and not `run small`, and matching is word-boundary
aware. Two barriers in series, one of them unmeasured because nothing tested this
stage. The end-to-end assertion below is the point of the module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.common.config import get_settings
from src.common.db import init_db
from src.common.schemas import Document
from src.store.exclusions import classify_with_filters
from src.store.relevance import (
    compile_keyword_matcher,
    content_word_count,
    keyword_hits,
    load_keywords,
    run_tier2,
    score_tier1,
)


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def keywords_path(settings):
    return settings.project_root / settings.run.filters.relevance_keywords_path


@pytest.fixture(scope="module")
def matcher(keywords_path):
    return compile_keyword_matcher(load_keywords(keywords_path))


def _doc(text: str) -> Document:
    return Document(
        doc_id="d" + str(abs(hash(text)))[:8],
        source="ajio_manual",
        source_native_id="n" + str(abs(hash(text)))[:8],
        author_hash="a" * 16,
        created_utc=datetime(2026, 8, 20, tzinfo=timezone.utc),
        text=text,
        word_count=len(text.split()),
        char_len=len(text),
    )


# --- the regression that motivated this file ------------------------------


def test_the_short_ajio_question_survives_every_phase_3_gate(settings, matcher):
    """Lowering the length gate is worthless if triage deletes the same text.

    Asserted end to end rather than per-stage, because each stage passed on its own
    while the pipeline as a whole still discarded the question.
    """
    question = "does this run small?"

    assert classify_with_filters(question, settings.run.filters) is None
    assert keyword_hits(question, matcher) > 0


def test_the_vocabulary_covers_the_bare_verb_form_too(matcher):
    """`runs small` cannot match `run small`, so both have to be listed.

    An auxiliary puts the verb in its bare form — "does this run small?" — which is
    the single most natural way to ask the question, and the near-miss was one
    character wide.
    """
    assert keyword_hits("this kurta runs small", matcher) > 0
    assert keyword_hits("does this run small", matcher) > 0


def test_stock_and_availability_questions_are_not_zero_hit(matcher):
    """Rejected-pool audit 2026-08-28: short stock/availability questions were contentless zero-hits."""
    assert keyword_hits("They are out of stock now", matcher) > 0
    assert keyword_hits("Is this still available?", matcher) > 0
    assert keyword_hits("I bought it from a physical store and online", matcher) > 0


# --- the content-word rule attributes, it does not decide -----------------


def test_a_short_signal_is_not_dropped_for_being_mostly_stopwords(keywords_path):
    """"still in my cart" is two content words and an unambiguous wishlist signal.

    Edge-case 2.8 specified a content-word floor of 3 as a *gate*. Enforcing it as
    one at a 3-word length gate deletes exactly the text that lowering the gate
    admitted, so it attributes instead.
    """
    assert content_word_count("still in my cart") == 2

    docs = [_doc("still in my cart")]
    counts = score_tier1(docs, keywords_path=keywords_path, min_content_words=3)

    assert counts["tier1_passed"] == 1
    assert docs[0].is_relevant is not False


def test_the_contentless_example_is_still_dropped_and_now_says_why(keywords_path):
    """Edge-case 2.8's own example, which the zero-hit rule already removed.

    It is dropped either way; the content-word count is what separates "about
    nothing" from "about something else", and only the latter is evidence that the
    vocabulary is too narrow.
    """
    docs = [_doc("this is the one that I was looking at")]
    counts = score_tier1(docs, keywords_path=keywords_path, min_content_words=3)

    assert docs[0].is_relevant is False
    assert counts["tier1_dropped_low_content"] == 1
    assert counts["tier1_dropped_zero_hits"] == 0


def test_excluded_and_duplicate_rows_are_not_scored(keywords_path):
    """Tier 1 only ever sees what survived exclusions and dedup."""
    excluded = _doc("nice")
    excluded.exclusion_reason = "too_short"
    duplicate = _doc("my wishlist is full of dresses i never buy")
    duplicate.is_duplicate_of = "some-other-doc"

    counts = score_tier1(
        [excluded, duplicate], keywords_path=keywords_path, min_content_words=3
    )
    assert counts == {
        "tier1_passed": 0,
        "tier1_dropped_zero_hits": 0,
        "tier1_dropped_low_content": 0,
    }


# --- tier 2: a stopped run has to keep its work ---------------------------
#
# The 2026-08-24 live attempt classified ~1,960 documents across 98 successful
# batches, then hit the daily token cap and exited before persisting any of them
# (plan §3.3). Nothing about that was a bug in the classifier; it was a stage with
# no checkpoint. These tests are about the checkpoint.


def _rate_limit_error():
    """The real exception the SDK raises, built the way the SDK builds it.

    A stand-in ``Exception`` would pass these tests while the live path caught
    nothing, since ``run_tier2`` catches ``groq.RateLimitError`` specifically.
    """
    import httpx
    from groq import RateLimitError

    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return RateLimitError("rate limit reached", response=response, body=None)


class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        return self._owner._respond(kwargs)


class _FakeGroq:
    """A triage model that answers, or raises whatever the test asks it to."""

    def __init__(self, api_key=None, verdicts=None, fail_after=None, tokens=100):
        self.calls = 0
        self.seen_doc_ids = []
        self._verdicts = verdicts or {}
        self._fail_after = fail_after
        self._tokens = tokens
        self.chat = type("chat", (), {"completions": _FakeCompletions(self)})()

    def _respond(self, kwargs):
        if self._fail_after is not None and self.calls >= self._fail_after:
            raise _rate_limit_error()
        self.calls += 1

        payload = kwargs["messages"][1]["content"]
        doc_ids = [line.split("]")[0][1:] for line in payload.splitlines() if line.startswith("[")]
        self.seen_doc_ids.extend(doc_ids)
        documents = [
            {"doc_id": d, "is_relevant": self._verdicts.get(d, True)} for d in doc_ids
        ]
        content = json.dumps({"documents": documents})
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(total_tokens=self._tokens),
        )


def _tier2_settings(tpd=200_000, batch=2):
    return SimpleNamespace(
        credentials=SimpleNamespace(groq_api_key="gsk_live_looking_key"),
        run=SimpleNamespace(
            model=SimpleNamespace(triage_name="openai/gpt-oss-20b", triage_docs_per_request=batch),
            rate_limits=SimpleNamespace(triage=SimpleNamespace(tpd=tpd)),
        ),
    )


def _survivor(i: int) -> Document:
    doc = _doc(f"wishlist item number {i} still saved and i cannot decide")
    doc.doc_id = f"doc{i:03d}"
    doc.is_relevant = None
    return doc


@pytest.fixture
def tier2(monkeypatch):
    """Install a fake Groq client and hand the test the instance it will use."""
    import groq

    holder = {}

    def factory(api_key=None, **kwargs):
        client = _FakeGroq(api_key=api_key, **holder.get("kwargs", {}))
        holder["client"] = client
        return client

    monkeypatch.setattr(groq, "Groq", factory)
    return holder


def test_every_batch_is_written_to_the_cache_as_it_arrives(tmp_path, tier2):
    """Not at the end: the end is where the last run never arrived."""
    conn = init_db(tmp_path / "d.db")
    docs = [_survivor(i) for i in range(6)]

    counts = run_tier2(docs, settings=_tier2_settings(), conn=conn)

    assert counts["tier2_status"] == "ran"
    assert counts["tier2_classified"] == 6
    cached = conn.execute("SELECT COUNT(*) FROM triage_cache").fetchone()[0]
    assert cached == 6
    conn.close()


def test_a_rate_limit_ends_the_stage_and_keeps_what_was_classified(tmp_path, tier2):
    """The whole point: 98 good batches must not die with the 99th."""
    conn = init_db(tmp_path / "d.db")
    tier2["kwargs"] = {"fail_after": 2}  # two batches of 2 succeed, then 429
    docs = [_survivor(i) for i in range(10)]

    counts = run_tier2(docs, settings=_tier2_settings(), conn=conn)

    assert counts["tier2_status"] == "partial"
    assert counts["tier2_stop_reason"] == "rate_limited"
    assert counts["tier2_classified"] == 4
    assert conn.execute("SELECT COUNT(*) FROM triage_cache").fetchone()[0] == 4
    conn.close()


def test_documents_tier_two_never_reached_keep_their_tier_one_verdict(tmp_path, tier2):
    """Left as None they would be untriaged rows the tagger silently skips, so the
    corpus size would depend on where the quota ran out with nothing showing it."""
    conn = init_db(tmp_path / "d.db")
    tier2["kwargs"] = {"fail_after": 1}
    docs = [_survivor(i) for i in range(10)]

    counts = run_tier2(docs, settings=_tier2_settings(), conn=conn)

    assert counts["tier2_unclassified"] == 8
    assert all(d.is_relevant is not None for d in docs)
    assert sum(1 for d in docs if d.is_relevant) == 10
    conn.close()


def test_the_next_run_resumes_from_the_cache_instead_of_re_classifying(tmp_path, tier2):
    """Three days of quota only finish the job if day two starts where day one stopped."""
    conn = init_db(tmp_path / "d.db")
    tier2["kwargs"] = {"fail_after": 2}
    first = [_survivor(i) for i in range(10)]
    run_tier2(first, settings=_tier2_settings(), conn=conn)

    tier2["kwargs"] = {}  # today the quota is fresh
    second = [_survivor(i) for i in range(10)]
    counts = run_tier2(second, settings=_tier2_settings(), conn=conn)

    assert counts["tier2_from_cache"] == 4
    assert counts["tier2_classified"] == 6  # only the six it never reached
    assert counts["tier2_status"] == "ran"
    assert "doc000" not in tier2["client"].seen_doc_ids
    conn.close()


def test_the_run_stops_before_breaching_the_daily_budget(tmp_path, tier2):
    """Stopping on our own count costs nothing; letting the server say 429 costs a call."""
    conn = init_db(tmp_path / "d.db")
    docs = [_survivor(i) for i in range(20)]

    # Room for roughly two batches of 2 at the measured 56 tokens/document.
    counts = run_tier2(docs, settings=_tier2_settings(tpd=250), conn=conn)

    assert counts["tier2_status"] == "partial"
    assert counts["tier2_stop_reason"] == "tpd_budget"
    assert 0 < counts["tier2_classified"] < 20
    conn.close()


def test_a_verdict_of_false_is_what_actually_removes_a_document(tmp_path, tier2):
    conn = init_db(tmp_path / "d.db")
    tier2["kwargs"] = {"verdicts": {"doc000": False, "doc001": False}}
    docs = [_survivor(i) for i in range(4)]

    counts = run_tier2(docs, settings=_tier2_settings(), conn=conn)

    assert counts["tier2_irrelevant"] == 2
    assert docs[0].is_relevant is False
    assert docs[2].is_relevant is True
    conn.close()


def test_a_document_the_model_omits_is_kept_rather_than_dropped(tmp_path, tier2):
    """Tier 2 removes what it has judged; a missing answer is not a judgement."""
    conn = init_db(tmp_path / "d.db")
    docs = [_survivor(i) for i in range(2)]
    docs[0].doc_id = "never-answered"

    run_tier2(docs, settings=_tier2_settings(), conn=conn)

    assert docs[0].is_relevant is True
    conn.close()


def test_without_a_connection_it_still_classifies_but_cannot_checkpoint(tier2):
    """build_corpus always passes one; the offline path must not require it."""
    docs = [_survivor(i) for i in range(4)]
    counts = run_tier2(docs, settings=_tier2_settings(), conn=None)
    assert counts["tier2_status"] == "ran"
    assert counts["tier2_from_cache"] == 0


def test_skipping_tier_two_keeps_the_looser_tier_one_corpus(tmp_path):
    conn = init_db(tmp_path / "d.db")
    docs = [_survivor(i) for i in range(3)]

    counts = run_tier2(docs, settings=_tier2_settings(), enable=False, conn=conn)

    assert counts["tier2_status"] == "skipped"
    assert all(d.is_relevant is True for d in docs)
    conn.close()
