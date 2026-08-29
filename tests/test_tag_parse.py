"""Unevidenced or unparseable model tags must not abort a tagging run.

The evidence rule on DocumentTags stays strict. Salvage happens at parse time:
drop the offending tag, keep the rest, skip only a document that cannot be
parsed at all.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.common.schemas import DocumentTags, EvidenceSpan, TaggedDocument
from src.tag.llm_client import parse_tagged_document, parse_tagging_response
from src.tag.taxonomy import BlockerType, IntentClass, OutcomeMentioned, SegmentCue


def _valid_payload(doc_id: str = "d1") -> dict:
    return TaggedDocument(
        doc_id=doc_id,
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
    ).model_dump(mode="json")


def test_unevidenced_tag_is_dropped_and_the_rest_is_kept(caplog):
    payload = _valid_payload("6f6074657b98f")
    payload["segment_cue"] = [SegmentCue.FIRST_TIME_ONLINE_BUYER.value]
    tagged = parse_tagged_document(payload)
    assert tagged is not None
    assert tagged.doc_id == "6f6074657b98f"
    assert tagged.segment_cue == []
    assert tagged.blocker_type == [BlockerType.FIT_SIZE_UNCERTAINTY]
    assert tagged.intent_class == IntentClass.GENUINE_INTENT
    assert "dropped unevidenced tag" in caplog.text
    assert "segment_cue=first_time_online_buyer" in caplog.text
    assert "6f6074657b98f" in caplog.text


def test_direct_document_tags_still_rejects_unevidenced_tags():
    """The schema contract is unchanged; only the run-level parse salvages."""
    import pytest
    from pydantic import ValidationError

    payload = _valid_payload()
    payload.pop("doc_id")
    payload["segment_cue"] = [SegmentCue.FIRST_TIME_ONLINE_BUYER.value]
    with pytest.raises(ValidationError, match="asserted without evidence"):
        DocumentTags.model_validate(payload)


def test_unparseable_document_is_skipped_not_raised(caplog):
    assert parse_tagged_document("not a document") is None
    assert parse_tagged_document({"doc_id": "bad", "blocker_type": "nope"}) is None
    assert "skipping document bad" in caplog.text


def test_unparseable_batch_envelope_returns_empty_and_does_not_raise(caplog):
    assert parse_tagging_response(None) == []
    assert parse_tagging_response("{not json") == []
    assert parse_tagging_response(json.dumps({"oops": []})) == []
    assert "unparseable envelope" in caplog.text


def test_mixed_batch_keeps_valid_drops_unevidenced_skips_garbage():
    valid = _valid_payload("keep")
    unevidenced = _valid_payload("salvage")
    unevidenced["segment_cue"] = [SegmentCue.FIRST_TIME_ONLINE_BUYER.value]
    garbage = {"doc_id": "skip", "not": "a tagged document"}
    tagged = parse_tagging_response(
        json.dumps({"documents": [valid, unevidenced, garbage]})
    )
    assert [t.doc_id for t in tagged] == ["keep", "salvage"]
    salvage = tagged[1]
    assert salvage.segment_cue == []
    assert salvage.blocker_type == [BlockerType.FIT_SIZE_UNCERTAINTY]


def test_tag_batch_does_not_raise_on_one_unevidenced_tag():
    from tests.test_tag_batching import _client

    valid = _valid_payload("keep")
    unevidenced = _valid_payload("salvage")
    unevidenced["segment_cue"] = [SegmentCue.FIRST_TIME_ONLINE_BUYER.value]
    envelope = json.dumps({"documents": [valid, unevidenced]})

    def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=envelope))],
            usage=SimpleNamespace(total_tokens=10, prompt_tokens=8, completion_tokens=2),
        )

    client = _client(create)
    tagged, _ = client.tag_batch(
        "prompt",
        [{"doc_id": "keep", "text": "a"}, {"doc_id": "salvage", "text": "b"}],
    )
    by_id = {t.doc_id: t for t in tagged}
    assert set(by_id) == {"keep", "salvage"}
    assert by_id["salvage"].segment_cue == []
    assert by_id["keep"].blocker_type == [BlockerType.FIT_SIZE_UNCERTAINTY]


def test_tag_batch_continues_when_the_whole_response_is_unparseable():
    from tests.test_tag_batching import _client

    def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="<<<not json>>>"))],
            usage=SimpleNamespace(total_tokens=10, prompt_tokens=8, completion_tokens=2),
        )

    client = _client(create)
    tagged, usage = client.tag_batch("prompt", [{"doc_id": "x", "text": "a"}])
    assert tagged == []
    assert usage["total_tokens"] == 10
