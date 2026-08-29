"""Phase 4 offline-verifiable pieces: cache + dry-run estimator (no API key needed)."""

import json
from datetime import datetime, timezone

import pytest

from src.common.db import init_db, upsert_documents
from src.common.schemas import Document
from src.tag import cache
from src.tag.taxonomy import (
    BlockerType,
    IntentClass,
    OutcomeMentioned,
)


def _tags():
    from src.common.schemas import DocumentTags, EvidenceSpan

    return DocumentTags(
        is_relevant=True,
        wishlist_motivation=[],
        blocker_type=[BlockerType.FIT_SIZE_UNCERTAINTY],
        uncertainty_type=[],
        info_sought_elsewhere=[],
        segment_cue=[],
        intent_class=IntentClass.GENUINE_INTENT,
        outcome_mentioned=OutcomeMentioned.STILL_DECIDING,
        severity=4,
        actionability_non_monetary=1,
        confidence_pct=80,
        evidence=[EvidenceSpan(tag=BlockerType.FIT_SIZE_UNCERTAINTY, quote="sizes run small")],
    )


def test_cache_round_trip(tmp_path):
    conn = init_db(tmp_path / "c.db")
    key = cache.cache_key(
        doc_id="d1", text="does this run small", model="m", taxonomy_version="v1", prompt_version="v1"
    )
    assert cache.get(conn, key) is None
    cache.put(conn, key, _tags(), prompt_tokens=100)
    got = cache.get(conn, key)
    assert got is not None
    assert got.blocker_type == [BlockerType.FIT_SIZE_UNCERTAINTY]
    conn.close()


def test_a_tagged_document_round_trips_without_storing_doc_id(tmp_path):
    """``run_tagging`` caches a TaggedDocument; the blob must still read as DocumentTags.

    The batched Groq response carries ``doc_id`` so each coding names its document.
    That field is already in the cache key. Storing it in ``response_json`` made
    every subsequent ``get()`` raise extra_forbidden, which is how ``--dry-run``
    crashed after the first real tagging run.
    """
    from src.common.schemas import TaggedDocument

    conn = init_db(tmp_path / "c.db")
    tagged = TaggedDocument(doc_id="d1", **_tags().model_dump())
    key = cache.cache_key(
        doc_id="d1", text="does this run small", model="m", taxonomy_version="v1", prompt_version="v1"
    )
    cache.put(conn, key, tagged)
    got = cache.get(conn, key)
    assert got is not None
    assert got.blocker_type == tagged.blocker_type
    assert not hasattr(got, "doc_id") or "doc_id" not in got.model_dump()
    stored = conn.execute("SELECT response_json FROM llm_cache WHERE cache_key = ?", (key,)).fetchone()[0]
    assert "doc_id" not in json.loads(stored)
    conn.close()


def test_rows_written_with_a_doc_id_are_still_readable(tmp_path):
    """Yesterday's cache rows already have the extra field; they must not be discarded."""
    conn = init_db(tmp_path / "c.db")
    payload = _tags().model_dump(mode="json")
    payload["doc_id"] = "d1"
    conn.execute(
        "INSERT INTO llm_cache (cache_key, response_json, created_at) VALUES (?, ?, ?)",
        ("legacy-key", json.dumps(payload), "2026-08-26T00:00:00+00:00"),
    )
    got = cache.get(conn, "legacy-key")
    assert got is not None
    assert got.blocker_type == [BlockerType.FIT_SIZE_UNCERTAINTY]
    conn.close()


def test_cache_key_changes_with_prompt_version():
    a = cache.cache_key(doc_id="d1", text="t", model="m", taxonomy_version="v1", prompt_version="v1")
    b = cache.cache_key(doc_id="d1", text="t", model="m", taxonomy_version="v1", prompt_version="v2")
    assert a != b  # bumping the prompt correctly misses the cache


def test_cache_key_changes_with_text():
    a = cache.cache_key(doc_id="d1", text="one", model="m", taxonomy_version="v1", prompt_version="v1")
    b = cache.cache_key(doc_id="d1", text="two", model="m", taxonomy_version="v1", prompt_version="v1")
    assert a != b  # corrected text invalidates stale tags


def test_token_totals_sums(tmp_path):
    conn = init_db(tmp_path / "c.db")
    cache.put(conn, "k1", _tags(), prompt_tokens=100, completion_tokens=50, reasoning_tokens=20)
    cache.put(conn, "k2", _tags(), prompt_tokens=200, completion_tokens=60, reasoning_tokens=30)
    totals = cache.token_totals(conn)
    assert totals["prompt_tokens"] == 300
    assert totals["cached_documents"] == 2
    conn.close()


def _doc(i: int, text: str) -> Document:
    return Document(
        doc_id=f"doc{i}",
        source="youtube",
        source_native_id=str(i),
        text=text,
        is_relevant=True,
        ingested_at=datetime.now(timezone.utc),
    )


def test_dry_run_counts_only_uncached(tmp_path, monkeypatch):
    from src.tag import run_tagging

    db = tmp_path / "discovery.db"
    conn = init_db(db)
    docs = [_doc(i, f"i saved this kurta because the size chart confused me number {i}") for i in range(12)]
    upsert_documents(conn, docs)
    # Pre-cache 4 of them so the estimator should only count 8 as billable.
    for i in range(4):
        key = cache.cache_key(
            doc_id=f"doc{i}", text=docs[i].text, model="openai/gpt-oss-120b",
            taxonomy_version="v1", prompt_version="v1",
        )
        cache.put(conn, key, _tags())
    conn.close()

    class _Model:
        name = "openai/gpt-oss-120b"
        docs_per_request = 6

    class _RL:
        class tagging:
            tpd = 200000
            rpm = 30

    class _Run:
        model = _Model()
        rate_limits = _RL()

    class _Settings:
        interim_db = db
        run = _Run()

    summary = run_tagging.dry_run(_Settings())
    assert summary["relevant_documents"] == 12
    assert summary["already_cached"] == 4
    assert summary["to_tag"] == 8
    assert summary["batches"] == 2  # ceil(8/6)
    assert summary["estimated_tokens"] == 8 * run_tagging.TOKENS_PER_DOC
