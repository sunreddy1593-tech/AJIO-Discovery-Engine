"""Storage must be idempotent, so a rate-limited run can simply be re-run."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src.common.db import init_db, run_log, upsert_documents, upsert_tags
from src.common.schemas import Document, DocumentTags, EvidenceSpan
from src.tag.taxonomy import (
    TAXONOMY_VERSION,
    BlockerType,
    EvidenceTag,
    IntentClass,
    OutcomeMentioned,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    connection = init_db(tmp_path / "nested" / "discovery.db")
    yield connection
    connection.close()


def document(doc_id: str = "aaaa111122223333", **overrides) -> Document:
    payload = {
        "doc_id": doc_id,
        "source": "youtube",
        "source_native_id": f"cmt-{doc_id}",
        "text": "wishlisted this kurta but the size chart makes no sense to me",
        "created_utc": NOW,
        "meta": {"video_id": "xyz"},
        "word_count": 12,
        "ingested_at": NOW,
    }
    payload.update(overrides)
    return Document(**payload)


def tags() -> DocumentTags:
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
        evidence=[EvidenceSpan(tag=EvidenceTag.FIT_SIZE_UNCERTAINTY, quote="size chart makes no sense")],
    )


def test_init_db_is_idempotent_and_creates_missing_directories(tmp_path):
    path = tmp_path / "deep" / "nested" / "discovery.db"
    init_db(path).close()
    init_db(path).close()
    assert path.exists()


def test_wal_and_foreign_keys_are_enabled(conn):
    """Foreign keys default to off in SQLite and must be set per connection."""
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_expected_indexes_exist(conn):
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {
        "idx_documents_source",
        "idx_documents_is_relevant",
        "idx_doc_tags_taxonomy",
    } <= names


def test_inserting_the_same_document_twice_leaves_one_row(conn):
    """Phase 1 exit criterion, and the basis of resumability."""
    assert upsert_documents(conn, [document()]) == 1
    assert upsert_documents(conn, [document()]) == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_same_source_native_id_under_a_different_doc_id_is_rejected_as_duplicate(conn):
    """The UNIQUE constraint is the backstop when an id derivation changes."""
    upsert_documents(conn, [document("aaaa111122223333")])
    clash = document("bbbb444455556666", source_native_id="cmt-aaaa111122223333")
    assert upsert_documents(conn, [clash]) == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_meta_and_timestamps_round_trip(conn):
    upsert_documents(conn, [document()])
    row = conn.execute("SELECT * FROM documents").fetchone()
    assert json.loads(row["meta_json"]) == {"video_id": "xyz"}
    assert row["created_utc"].startswith("2026-08-19T12:00")
    assert row["ingested_at"] is not None


def test_is_relevant_is_stored_as_a_nullable_integer(conn):
    """Untriaged documents must be distinguishable from rejected ones."""
    upsert_documents(conn, [document("cccc1111", is_relevant=None)])
    upsert_documents(conn, [document("dddd2222", is_relevant=False)])
    upsert_documents(conn, [document("eeee3333", is_relevant=True)])
    values = {
        row["doc_id"]: row["is_relevant"]
        for row in conn.execute("SELECT doc_id, is_relevant FROM documents")
    }
    assert values["cccc1111"] is None
    assert values["dddd2222"] == 0
    assert values["eeee3333"] == 1


def test_empty_batch_is_a_no_op(conn):
    assert upsert_documents(conn, []) == 0


def test_duplicate_marking_requires_the_target_to_exist(conn):
    """Marking duplicates only works after the batch is inserted, per the FK."""
    upsert_documents(conn, [document("aaaa1111")])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE documents SET is_duplicate_of = ? WHERE doc_id = ?",
            ("does-not-exist", "aaaa1111"),
        )

    upsert_documents(conn, [document("bbbb2222")])
    conn.execute(
        "UPDATE documents SET is_duplicate_of = ? WHERE doc_id = ?", ("aaaa1111", "bbbb2222")
    )
    assert (
        conn.execute("SELECT is_duplicate_of FROM documents WHERE doc_id='bbbb2222'").fetchone()[0]
        == "aaaa1111"
    )


# --- tags -----------------------------------------------------------------


def test_tags_for_an_unknown_document_are_rejected(conn):
    """An orphan tag row would inflate prevalence with a document that does not exist."""
    with pytest.raises(sqlite3.IntegrityError):
        upsert_tags(
            conn,
            "missing-doc",
            tags(),
            taxonomy_version=TAXONOMY_VERSION,
            prompt_version="v1",
            model="openai/gpt-oss-120b",
        )


def test_tagging_the_same_document_twice_leaves_one_row(conn):
    upsert_documents(conn, [document()])
    kwargs = {
        "taxonomy_version": TAXONOMY_VERSION,
        "prompt_version": "v1",
        "model": "openai/gpt-oss-120b",
    }
    assert upsert_tags(conn, "aaaa111122223333", tags(), **kwargs) == 1
    assert upsert_tags(conn, "aaaa111122223333", tags(), **kwargs) == 0


def test_a_new_prompt_version_adds_a_row_rather_than_overwriting(conn):
    """History has to survive a prompt change or past reports stop being reproducible."""
    upsert_documents(conn, [document()])
    upsert_tags(
        conn,
        "aaaa111122223333",
        tags(),
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v1",
        model="openai/gpt-oss-120b",
    )
    upsert_tags(
        conn,
        "aaaa111122223333",
        tags(),
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v2",
        model="openai/gpt-oss-120b",
    )
    assert conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()[0] == 2


def test_stored_tags_reparse_into_the_model(conn):
    upsert_documents(conn, [document()])
    upsert_tags(
        conn,
        "aaaa111122223333",
        tags(),
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v1",
        model="openai/gpt-oss-120b",
    )
    stored = conn.execute("SELECT tags_json FROM doc_tags").fetchone()[0]
    assert DocumentTags.model_validate_json(stored) == tags()


# --- run log --------------------------------------------------------------


def test_run_log_records_counts(conn):
    with run_log(conn, "run-1", "collect", "abc123") as entry:
        entry.records_in = 100
        entry.records_out = 87
        entry.note("play_store skipped: manifest exists")

    row = conn.execute("SELECT * FROM run_log").fetchone()
    assert (row["run_id"], row["stage"], row["config_hash"]) == ("run-1", "collect", "abc123")
    assert (row["records_in"], row["records_out"]) == (100, 87)
    assert "manifest exists" in row["notes"]
    assert row["started_at"] <= row["finished_at"]


def test_run_log_records_a_failed_stage_and_reraises(conn):
    """A stage stopped by a rate limit is the normal case; it must still leave a trace."""
    with pytest.raises(RuntimeError, match="daily token limit"):
        with run_log(conn, "run-2", "tag", "abc123") as entry:
            entry.records_in = 50
            entry.records_out = 12
            raise RuntimeError("daily token limit reached")

    row = conn.execute("SELECT * FROM run_log WHERE run_id='run-2'").fetchone()
    assert row["records_out"] == 12
    assert "daily token limit" in row["notes"]


def test_run_log_with_no_notes_stores_null(conn):
    with run_log(conn, "run-3", "quantify", "abc123"):
        pass
    assert conn.execute("SELECT notes FROM run_log WHERE run_id='run-3'").fetchone()[0] is None


def test_force_rebuild_keeps_tags_when_documents_are_replaced(conn):
    """``DELETE FROM documents`` with FKs on fails after tagging (2026-08-28)."""
    from src.store.build_corpus import replace_document_rows

    upsert_documents(conn, [document()])
    upsert_tags(
        conn,
        "aaaa111122223333",
        tags(),
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v1",
        model="openai/gpt-oss-120b",
    )
    replacement = document(is_relevant=False, exclusion_reason="too_short")
    added = replace_document_rows(conn, [replacement])
    assert added == 1
    assert conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()[0] == 1
    assert conn.execute("SELECT is_relevant FROM documents").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
