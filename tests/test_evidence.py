"""Evidence selection: PII redaction, markdown escaping, unflagged quotes only."""

from __future__ import annotations

from datetime import datetime, timezone

from src.common.db import init_db, upsert_documents, upsert_tags
from src.common.schemas import Document, DocumentTags, EvidenceSpan
from src.synthesize.evidence import (
    escape_markdown,
    redact_pii,
    select_quotes,
    truncate,
)
from src.tag.taxonomy import (
    TAXONOMY_VERSION,
    BlockerType,
    EvidenceTag,
    IntentClass,
    OutcomeMentioned,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def test_redact_pii_strips_phone_email_order_and_handle():
    text = (
        "Call +91 98765 43210 or me@example.com about order #AJ123456 "
        "and ping @haulqueen on the tracking AWB 1234567890."
    )
    cleaned = redact_pii(text)
    assert "98765" not in cleaned
    assert "me@example.com" not in cleaned
    assert "@haulqueen" not in cleaned
    assert "AJ123456" not in cleaned
    assert "[phone]" in cleaned
    assert "[email]" in cleaned
    assert "[handle]" in cleaned
    assert "[order-id]" in cleaned


def test_escape_markdown_neutralises_pipes_backticks_and_headings():
    assert "\\|" in escape_markdown("a | b")
    assert "`" not in escape_markdown("uses `code` spans")
    assert escape_markdown("# heading").startswith("\\#")
    assert escape_markdown("> quoted").startswith("\\>")


def test_truncate_cuts_on_a_word_boundary():
    words = " ".join(f"word{i}" for i in range(80))
    clipped = truncate(words, limit=40)
    assert len(clipped) <= 41
    assert clipped.endswith("…")
    assert " " not in clipped.rstrip("…")[-1:]


def _document(doc_id: str, source: str, text: str) -> Document:
    return Document(
        doc_id=doc_id,
        source=source,
        source_native_id=f"native-{doc_id}",
        text=text,
        created_utc=NOW,
        word_count=len(text.split()),
        is_relevant=True,
        ingested_at=NOW,
    )


def _tags(blocker: BlockerType, quote: str) -> DocumentTags:
    return DocumentTags(
        is_relevant=True,
        wishlist_motivation=[],
        blocker_type=[blocker],
        uncertainty_type=[],
        info_sought_elsewhere=[],
        segment_cue=[],
        intent_class=IntentClass.GENUINE_INTENT,
        outcome_mentioned=OutcomeMentioned.STILL_DECIDING,
        severity=4,
        actionability_non_monetary=1,
        confidence_pct=80,
        evidence=[EvidenceSpan(tag=EvidenceTag(blocker.value), quote=quote)],
    )


def test_select_quotes_skips_a_span_that_is_not_in_the_document(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    live = "the size chart makes no sense on this kurta"
    upsert_documents(
        conn,
        [
            _document("good000000000001", "youtube", live),
            _document("bad0000000000002", "quora_manual", "unrelated body text here"),
        ],
    )
    kwargs = dict(taxonomy_version=TAXONOMY_VERSION, prompt_version="v1", model="test")
    upsert_tags(
        conn,
        "good000000000001",
        _tags(BlockerType.FIT_SIZE_UNCERTAINTY, "size chart makes no sense"),
        **kwargs,
    )
    upsert_tags(
        conn,
        "bad0000000000002",
        _tags(BlockerType.FIT_SIZE_UNCERTAINTY, "this quote is not in the body"),
        **kwargs,
    )

    quotes = select_quotes(
        conn,
        "fit_size_uncertainty",
        dimension="blocker_type",
        limit=4,
    )
    conn.close()

    assert len(quotes) == 1
    assert quotes[0].doc_id == "good000000000001"
    assert "size chart makes no sense" in quotes[0].text
    assert "not in the body" not in quotes[0].text


def test_selected_quotes_are_redacted_and_escaped(tmp_path):
    conn = init_db(tmp_path / "discovery.db")
    body = "the size chart makes no sense | call 9876543210 #urgent"
    upsert_documents(conn, [_document("pii0000000000001", "youtube", body)])
    upsert_tags(
        conn,
        "pii0000000000001",
        _tags(BlockerType.FIT_SIZE_UNCERTAINTY, body),
        taxonomy_version=TAXONOMY_VERSION,
        prompt_version="v1",
        model="test",
    )
    quotes = select_quotes(conn, "fit_size_uncertainty", dimension="blocker_type")
    conn.close()

    assert quotes
    assert "9876543210" not in quotes[0].text
    assert "[phone]" in quotes[0].text
    assert "\\|" in quotes[0].text
