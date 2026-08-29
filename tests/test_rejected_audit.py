"""The rejected-pool audit: strata, a seeded draw, and a gate that can fail.

Phase 3's fourth exit criterion is the one that catches an over-aggressive filter,
and an over-aggressive filter does not announce itself — it produces a smaller
corpus that still looks reasonable. These tests cover the two halves the script is
responsible for: putting each rejection in the stratum that explains it, and
scoring labels honestly once a human has supplied them. The labelling itself is
deliberately not automated, so there is nothing here that invents a verdict.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.audit_rejected_pool import (
    MAX_FALSE_REJECTION_RATE,
    STRATUM_ORDER,
    draw,
    read_worksheet,
    rejected_pool,
    render_report,
    score,
    write_worksheet,
)
from src.common.db import init_db, upsert_documents
from src.common.schemas import Document

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def _doc(doc_id: str, text: str, **overrides) -> Document:
    payload = {
        "doc_id": doc_id,
        "source": "youtube",
        "source_native_id": f"native-{doc_id}",
        "text": text,
        "is_relevant": False,
        "ingested_at": NOW,
    }
    payload.update(overrides)
    return Document(**payload)


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "discovery.db")
    documents = []
    for i in range(12):
        documents.append(_doc(f"short{i}", "too short", exclusion_reason="too_short"))
        documents.append(
            _doc(f"emoji{i}", f"lovely kurta number {i} and the fit was perfect", exclusion_reason="contains_emoji")
        )
        documents.append(_doc(f"hindi{i}", f"यह कुर्ता अच्छा है {i}", exclusion_reason="hindi_language"))
        # Zero keyword hits, but plenty of content words: "about something else".
        documents.append(
            _doc(f"content{i}", f"the delivery agent called me twice yesterday afternoon {i}", relevance_score=0.0)
        )
        # Zero hits and almost no content words: "about nothing".
        documents.append(_doc(f"empty{i}", "it is what it is", relevance_score=0.0))
        # Matched the vocabulary, then the model said no: only tier 2 can do this.
        documents.append(
            _doc(f"tier2_{i}", f"my wishlist keeps logging me out {i}", relevance_score=0.25)
        )
    # A relevant document, which must never appear in a rejected-pool sample.
    documents.append(_doc("keeper", "i saved this dress but the size chart confused me", is_relevant=True))
    upsert_documents(connection, documents)
    yield connection
    connection.close()


# --- strata ---------------------------------------------------------------


def test_each_rejection_lands_in_the_stratum_that_explains_it(conn):
    pool = rejected_pool(conn, min_content_words=3)
    assert len(pool["too_short"]) == 12
    assert len(pool["contains_emoji"]) == 12
    assert len(pool["hindi_language"]) == 12
    assert len(pool["tier1_zero_hits_contentful"]) == 12
    assert len(pool["tier1_zero_hits_contentless"]) == 12
    assert len(pool["tier2_rejected"]) == 12


def test_a_relevant_document_is_never_in_the_pool(conn):
    pool = rejected_pool(conn, min_content_words=3)
    assert "keeper" not in {c.doc_id for members in pool.values() for c in members}


def test_tier_one_and_tier_two_are_told_apart_by_their_score(conn):
    """The table records no rejecting stage, so the stratum is reconstructed.

    A zero relevance_score means no keyword matched at all; a non-zero score on a
    rejected row can only mean the vocabulary matched and the model then said no.
    """
    pool = rejected_pool(conn, min_content_words=3)
    assert all(c.relevance_score == 0.0 for c in pool["tier1_zero_hits_contentful"])
    assert all(c.relevance_score and c.relevance_score > 0 for c in pool["tier2_rejected"])


def test_the_content_word_split_is_what_min_content_words_decides(conn):
    """With the split off, both zero-hit groups collapse into the contentful one.

    That is the point of the rule per plan §3.1: it attributes, it does not gate.
    """
    pool = rejected_pool(conn, min_content_words=0)
    assert len(pool["tier1_zero_hits_contentless"]) == 0
    assert len(pool["tier1_zero_hits_contentful"]) == 24


# --- the draw -------------------------------------------------------------


def test_the_draw_is_equal_per_stratum_not_proportional(conn):
    sample = draw(rejected_pool(conn, min_content_words=3), per_stratum=10, seed=42)
    counts = {s: sum(1 for c in sample if c.stratum == s) for s in STRATUM_ORDER}
    assert set(counts.values()) == {10}


def test_the_same_seed_draws_the_same_documents(conn):
    pool = rejected_pool(conn, min_content_words=3)
    first = [c.doc_id for c in draw(pool, per_stratum=10, seed=42)]
    second = [c.doc_id for c in draw(pool, per_stratum=10, seed=42)]
    assert first == second


def test_a_thin_stratum_yields_what_it_has_rather_than_raising(conn):
    sample = draw(rejected_pool(conn, min_content_words=3), per_stratum=50, seed=42)
    assert sum(1 for c in sample if c.stratum == "too_short") == 12


def test_an_absent_stratum_is_skipped(conn):
    """Tier 2 has never completed, so its stratum is legitimately empty."""
    pool = rejected_pool(conn, min_content_words=3)
    pool["tier2_rejected"] = []
    sample = draw(pool, per_stratum=10, seed=42)
    assert not [c for c in sample if c.stratum == "tier2_rejected"]


# --- the worksheet --------------------------------------------------------


def test_the_worksheet_ships_unlabelled_and_carries_its_question(conn, tmp_path):
    sample = draw(rejected_pool(conn, min_content_words=3), per_stratum=10, seed=42)
    path = tmp_path / "outputs" / "rejected_pool_audit.jsonl"
    written = write_worksheet(path, sample, seed=42, per_stratum=10)

    rows = read_worksheet(path)
    assert written == len(rows) == 60
    assert all(row["false_rejection"] is None for row in rows)
    assert all(row["question"] for row in rows)
    assert all(row["text"] for row in rows)


def test_the_meta_header_is_not_mistaken_for_a_document(conn, tmp_path):
    sample = draw(rejected_pool(conn, min_content_words=3), per_stratum=2, seed=42)
    path = tmp_path / "sheet.jsonl"
    write_worksheet(path, sample, seed=42, per_stratum=2)

    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert "_meta" in first
    assert all("_meta" not in row for row in read_worksheet(path))


# --- scoring --------------------------------------------------------------


def _labelled(stratum: str, total: int, wrong: int) -> list[dict]:
    return [
        {"stratum": stratum, "source": "youtube", "false_rejection": i < wrong}
        for i in range(total)
    ]


def test_an_unlabelled_worksheet_is_not_scored(conn, tmp_path):
    """A rate over a partial sample is not the measurement the criterion asks for."""
    sample = draw(rejected_pool(conn, min_content_words=3), per_stratum=2, seed=42)
    path = tmp_path / "sheet.jsonl"
    write_worksheet(path, sample, seed=42, per_stratum=2)

    result = score(read_worksheet(path))
    assert result["unlabelled"] == 12
    assert result["audited"] == 0


def test_a_clean_pool_passes_the_gate():
    result = score(_labelled("too_short", 10, 0) + _labelled("contains_emoji", 10, 0))
    assert result["rate"] == 0.0
    assert result["passes"] is True


def test_one_bad_stratum_fails_the_whole_audit_despite_a_good_average():
    """A rule that is wrong half the time must not hide behind four that are not."""
    result = score(
        _labelled("too_short", 10, 0)
        + _labelled("contains_emoji", 10, 5)
        + _labelled("hindi_language", 10, 0)
        + _labelled("tier1_zero_hits_contentful", 10, 0)
    )
    assert result["by_stratum"]["contains_emoji"]["passes"] is False
    assert result["rate"] < MAX_FALSE_REJECTION_RATE * 1.5  # the average looks fine
    assert result["passes"] is False


def test_the_gate_is_strict_at_ten_percent():
    """1 of 10 is 10%, and the criterion says *below* 10%."""
    assert score(_labelled("too_short", 10, 1))["passes"] is False
    assert score(_labelled("too_short", 20, 1))["passes"] is True


def test_scoring_ignores_rows_a_labeller_left_blank():
    rows = _labelled("too_short", 10, 1) + [{"stratum": "too_short", "false_rejection": None}]
    result = score(rows)
    assert result["unlabelled"] == 1
    assert result["by_stratum"]["too_short"]["audited"] == 10


def test_the_report_names_the_failing_stratum_and_its_own_resolution():
    result = score(_labelled("contains_emoji", 10, 5))
    report = render_report(result, seed=42)
    assert "contains_emoji" in report
    assert "FAIL" in report
    # The resolution caveat bounds the conclusion, so it must survive rendering.
    assert "steps of roughly 10%" in report
