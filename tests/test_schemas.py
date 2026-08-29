"""Phase 1 exit criteria: the contracts reject bad data at the boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.common.config import load_run_config
from src.common.schemas import (
    KNOWN_SOURCES,
    SOURCE_STAGE,
    Document,
    DocumentTags,
    EvidenceSpan,
    PurchaseStage,
    RawRecord,
    purchase_stage,
)
from src.tag.taxonomy import (
    MULTI_LABEL_DIMENSIONS,
    BlockerType,
    EvidenceTag,
    InfoSoughtElsewhere,
    IntentClass,
    OutcomeMentioned,
    UncertaintyType,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def raw(**overrides) -> RawRecord:
    payload = {
        "source": "youtube",
        "source_native_id": "cmt-1",
        "text": "This kurta has been in my wishlist for a month and I still cannot decide",
        "collected_at": NOW,
        "collector_version": "1.0.0",
    }
    payload.update(overrides)
    return RawRecord(**payload)


def tags(**overrides) -> DocumentTags:
    payload = {
        "is_relevant": True,
        "wishlist_motivation": [],
        "blocker_type": [BlockerType.FIT_SIZE_UNCERTAINTY],
        "uncertainty_type": [],
        "info_sought_elsewhere": [],
        "segment_cue": [],
        "intent_class": IntentClass.GENUINE_INTENT,
        "outcome_mentioned": OutcomeMentioned.STILL_DECIDING,
        "severity": 4,
        "actionability_non_monetary": 1,
        "confidence_pct": 80,
        "evidence": [
            EvidenceSpan(tag=EvidenceTag.FIT_SIZE_UNCERTAINTY, quote="not sure if M runs small")
        ],
    }
    payload.update(overrides)
    return DocumentTags(**payload)


# --- RawRecord ------------------------------------------------------------


def test_empty_text_is_rejected_at_collection():
    """Edge case 1.2.1: a blank record must never reach storage."""
    for blank in ("", "   ", "\n\t "):
        with pytest.raises(ValidationError):
            raw(text=blank)


def test_mojibake_is_rejected_but_a_stray_replacement_char_is_kept():
    """Edge case 1.2.6: reject text decoded with the wrong encoding, not text with one bad glyph."""
    with pytest.raises(ValidationError):
        raw(text="\ufffd\ufffd\ufffd \ufffd\ufffd\ufffd \ufffd\ufffd")

    survivor = raw(text="the fabric quality was decent for the price i paid here \ufffd")
    assert survivor.text.endswith("\ufffd")


def test_future_timestamp_is_clamped_and_recorded():
    """Edge case 1.2.3: a skewed device clock must not win the recency weighting."""
    record = raw(created_utc=NOW + timedelta(days=400))
    assert record.created_utc == NOW
    assert record.meta["created_utc_clamped"] is True
    assert "created_utc_original" in record.meta


def test_naive_timestamps_become_utc_aware():
    """Mixing naive and aware datetimes would make Stage 4 comparisons raise."""
    record = raw(created_utc=datetime(2026, 1, 1, 9, 30))
    assert record.created_utc == datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)


def test_unknown_source_is_rejected():
    with pytest.raises(ValidationError):
        raw(source="pinterest")


def test_blank_native_id_is_rejected():
    with pytest.raises(ValidationError):
        raw(source_native_id="  ")


# --- Purchase stage -------------------------------------------------------


def test_source_stage_covers_every_enabled_source():
    """Exit criterion: a new source cannot default to an unknown stage."""
    run_config, _ = load_run_config()
    for source in run_config.collection.enabled_sources():
        assert source in SOURCE_STAGE, f"{source} is enabled but has no purchase stage"


def test_source_stage_and_known_sources_agree():
    assert KNOWN_SOURCES == frozenset(SOURCE_STAGE)


def test_unknown_source_raises_rather_than_defaulting():
    with pytest.raises(ValueError, match="unknown source"):
        purchase_stage("myntra_onsite")


def test_ajio_stage_is_resolved_by_content_type():
    """Edge case 1.1.14: Q&A is pre-purchase, reviews are post-purchase."""
    assert (
        purchase_stage("ajio_onsite", {"content_type": "qa"}) is PurchaseStage.PRE_PURCHASE
    )
    assert (
        purchase_stage("ajio_onsite", {"content_type": "review"}) is PurchaseStage.POST_PURCHASE
    )


def test_ajio_record_without_content_type_is_rejected():
    """Discovering this during quantification would mean re-collecting the source."""
    with pytest.raises(ValidationError, match="content_type"):
        raw(source="ajio_onsite", source_native_id="qa-1")

    record = raw(source="ajio_onsite", source_native_id="qa-1", meta={"content_type": "qa"})
    assert record.purchase_stage is PurchaseStage.PRE_PURCHASE


def test_youtube_is_pre_purchase_and_complaint_boards_are_not():
    """The pre/post split is what keeps refund complaints from posing as wishlist evidence."""
    assert purchase_stage("youtube") is PurchaseStage.PRE_PURCHASE
    assert purchase_stage("complaints_board") is PurchaseStage.POST_PURCHASE
    assert purchase_stage("consumer_complaints_in") is PurchaseStage.POST_PURCHASE


# --- DocumentTags ---------------------------------------------------------


def test_unknown_blocker_value_is_rejected():
    """Exit criterion: an invalid tag cannot be persisted."""
    with pytest.raises(ValidationError):
        tags(blocker_type=["shipping_too_slow"])


def test_asserted_tag_without_evidence_is_rejected():
    """The rule that separates coding from guessing."""
    with pytest.raises(ValidationError, match="asserted without evidence"):
        tags(uncertainty_type=[UncertaintyType.WILL_IT_FIT])


def test_evidence_covering_every_asserted_tag_is_accepted():
    accepted = tags(
        uncertainty_type=[UncertaintyType.WILL_IT_FIT],
        evidence=[
            EvidenceSpan(tag=EvidenceTag.FIT_SIZE_UNCERTAINTY, quote="M runs small"),
            EvidenceSpan(tag=EvidenceTag.WILL_IT_FIT, quote="will it fit me"),
        ],
    )
    assert len(accepted.evidence) == 2


def test_blank_quote_is_rejected():
    with pytest.raises(ValidationError, match="blank quote"):
        tags(evidence=[EvidenceSpan(tag=EvidenceTag.FIT_SIZE_UNCERTAINTY, quote="   ")])


def test_empty_lists_need_no_evidence():
    """`[]` is how a dimension says "does not apply", and it obliges nothing."""
    assert tags(blocker_type=[], evidence=[]).blocker_type == []


def test_info_sought_elsewhere_has_no_none_sentinel():
    """A live call put "none" into wishlist_motivation, where it is not legal.

    An empty list already means "went nowhere", so the redundant sentinel only
    created a token the model could misapply across dimensions.
    """
    assert "none" not in {member.value for member in InfoSoughtElsewhere}
    assert "none" not in {member.value for member in EvidenceTag}


def test_confidence_is_an_integer_percent_exposed_as_a_ratio():
    assert tags(confidence_pct=70).confidence == pytest.approx(0.7)
    with pytest.raises(ValidationError):
        tags(confidence_pct=75)
    with pytest.raises(ValidationError):
        tags(confidence_pct=0.8)


def test_severity_stays_within_the_taxonomy_range():
    for value in (1, 5):
        assert tags(severity=value).severity == value
    for value in (0, 6, 3.5):
        with pytest.raises(ValidationError):
            tags(severity=value)


def test_extra_field_is_rejected():
    """A hallucinated extra key must not be silently absorbed into the corpus."""
    with pytest.raises(ValidationError):
        tags(vibe="positive")


def test_evidence_tag_enum_matches_the_multi_label_dimensions():
    """EvidenceTag is generated; this pins it to the dimensions it claims to cover."""
    expected: set[str] = set()
    for _, enum_cls in MULTI_LABEL_DIMENSIONS:
        expected |= {member.value for member in enum_cls}
    assert {member.value for member in EvidenceTag} == expected


# --- Document -------------------------------------------------------------


def test_document_cannot_be_its_own_duplicate():
    """Edge case 2.9: a self-referencing duplicate would make cluster resolution loop."""
    with pytest.raises(ValidationError, match="itself"):
        Document(doc_id="abc123", source="youtube", source_native_id="c1", text="x", is_duplicate_of="abc123")


def test_document_exclusion_reason_is_constrained():
    Document(
        doc_id="abc123",
        source="youtube",
        source_native_id="c1",
        text="short one",
        exclusion_reason="too_short",
    )
    with pytest.raises(ValidationError):
        Document(
            doc_id="abc123",
            source="youtube",
            source_native_id="c1",
            text="x",
            exclusion_reason="not_english",
        )
